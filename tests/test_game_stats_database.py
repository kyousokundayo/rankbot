"""統計スキーマと精算トランザクションの回帰テスト。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from config import Role, Team, VARIANT_DEFINITIONS


NINE_WOLF_IDS = (2000, 2001)
NINE_MADMAN_ID = 2002
NINE_SEER_ID = 2003
NINE_MEDIUM_ID = 2004
NINE_GUARD_ID = 2005
NINE_VILLAGER_IDS = (2006, 2007, 2008)


class GameStatsDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-game-stats-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "stats.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    @staticmethod
    def _records() -> list[dict]:
        roles = [
            Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
            Role.MADMAN, Role.SEER, Role.MEDIUM, Role.GUARD,
            *([Role.VILLAGER] * 6),
        ]
        records = []
        for index, role in enumerate(roles):
            wolf_team = index < 4
            records.append({
                "player_id": 1000 + index,
                "role": role.value,
                "team": Team.WOLF.value if wolf_team else Team.VILLAGE.value,
                "won": int(not wolf_team),
                "died_on_day": 1 if index == 0 else None,
                "death_cause": "処刑" if index == 0 else None,
                "rank_at_game": None,
                "rank_provisional": None,
            })
        return records

    @staticmethod
    def _nine_records(deaths: dict[int, dict] | None = None) -> list[dict]:
        """9人村の実配役。13人 fixture の先頭9人を流用しない。"""
        layout = (
            [(player_id, Role.WEREWOLF) for player_id in NINE_WOLF_IDS]
            + [(NINE_MADMAN_ID, Role.MADMAN), (NINE_SEER_ID, Role.SEER)]
            + [(NINE_MEDIUM_ID, Role.MEDIUM), (NINE_GUARD_ID, Role.GUARD)]
            + [(player_id, Role.VILLAGER) for player_id in NINE_VILLAGER_IDS]
        )
        records = []
        for player_id, role in layout:
            team = Team.WOLF if role in {Role.WEREWOLF, Role.MADMAN} else Team.VILLAGE
            record = {
                "player_id": player_id,
                "role": role.value,
                "team": team.value,
                "won": int(team is Team.VILLAGE),
                "died_on_day": None,
                "death_cause": None,
                "rank_at_game": None,
                "rank_provisional": None,
            }
            record.update((deaths or {}).get(player_id, {}))
            records.append(record)
        return records

    @staticmethod
    def _stats() -> dict:
        return {
            "days": 4,
            "peaceful_mornings": 2,
            "guard_successes": 1,
            "guard_checks": 3,
            "seer_checks": 3,
            "seer_wolf_hits": 1,
            "day1_execution_was_wolf": 1,
            "executions_total": 4,
            "executions_wolf": 3,
            "night1_kill_had_role": 1,
            "wolf_alive_by_day": [3, 2, 2, 1],
            "rank_bucket": None,
        }

    async def test_settlement_writes_stats_gm_deaths_and_live_rank_snapshot(self) -> None:
        await database.stage_game_settlement(
            1, "open", "run-stats", room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value, player_records=self._records(),
            game_stats=self._stats(), gm_id=999,
        )
        rank_records = {
            1000 + index: {
                "rank_at_game": "ダイヤ",
                "rank_provisional": index == 0,
            }
            for index in range(13)
        }
        game_id, _results, created = await database.settle_game_settlement(
            1, "open", "run-stats",
            rank_records=rank_records,
            rank_bucket="ダイヤ",
        )

        self.assertTrue(created)
        async with database.connect_db() as db:
            game = (await db.execute_fetchall(
                "SELECT gm_id FROM games WHERE game_id = ?", (game_id,)
            ))[0]
            player = (await db.execute_fetchall(
                "SELECT died_on_day, death_cause, rank_at_game, rank_provisional "
                "FROM game_players WHERE game_id = ? AND player_id = 1000",
                (game_id,),
            ))[0]
            stats = (await db.execute_fetchall(
                "SELECT days, peaceful_mornings, guard_successes, guard_checks, seer_checks, "
                "seer_wolf_hits, executions_total, executions_wolf, "
                "wolf_alive_by_day, rank_bucket FROM game_stats WHERE game_id = ?",
                (game_id,),
            ))[0]
        self.assertEqual(game[0], 999)
        self.assertEqual(player, (1, "処刑", "ダイヤ", 1))
        self.assertEqual(stats[:8], (4, 2, 1, 3, 3, 1, 4, 3))
        self.assertEqual(json.loads(stats[8]), [3, 2, 2, 1])
        self.assertEqual(stats[9], "ダイヤ")

        overall = await database.get_overall_game_stats(1, room_id="open")
        ranked = await database.get_rank_player_stats(1, rank_name="ダイヤ")
        player = await database.get_player_stats(1004, 1)
        self.assertEqual(overall["games"], 1)
        self.assertEqual(overall["detailed_games"], 1)
        self.assertEqual(overall["peaceful"]["numerator"], 2)
        self.assertEqual(ranked["seer"]["checks"], 3)
        self.assertEqual(ranked["seer"]["wolf_hits"], 1)
        self.assertEqual(ranked["guard"]["checks"], 3)
        self.assertEqual(ranked["guard"]["successes"], 1)
        self.assertEqual(player["seer_checks"], 3)
        self.assertEqual(player["seer_wolf_hits"], 1)

    async def test_non_rated_settlement_keeps_rank_fields_and_bucket_null(self) -> None:
        await database.stage_game_settlement(
            1, "private:1", "run-private", room_name="専用村", rated=False,
            winner_team=Team.VILLAGE.value, player_records=self._records(),
            game_stats=self._stats(), gm_id=999,
        )
        game_id, _results, _created = await database.settle_game_settlement(
            1, "private:1", "run-private",
        )
        async with database.connect_db() as db:
            ranks = await db.execute_fetchall(
                "SELECT DISTINCT rank_at_game, rank_provisional FROM game_players "
                "WHERE game_id = ?", (game_id,),
            )
            bucket = (await db.execute_fetchall(
                "SELECT rank_bucket FROM game_stats WHERE game_id = ?", (game_id,),
            ))[0][0]
        self.assertEqual(ranks, [(None, None)])
        self.assertIsNone(bucket)

    async def test_pending_recovery_keeps_rank_snapshot_staged_before_crash(self) -> None:
        records = self._records()
        for index, record in enumerate(records):
            record["rank_at_game"] = "ダイヤ"
            record["rank_provisional"] = index == 0
        stats = {**self._stats(), "rank_bucket": "ダイヤ"}
        await database.stage_game_settlement(
            1, "open", "run-rank-recovery", room_name="総合", rated=True,
            winner_team=Team.VILLAGE.value, player_records=records,
            game_stats=stats, gm_id=999,
        )

        # プロセスがstage直後に落ちた想定。rank_records引数なしで
        # 起動時回収しても、stage内の試合時ランクが正本として残る。
        game_id, _results, created = await database.settle_game_settlement(
            1, "open", "run-rank-recovery",
        )

        self.assertTrue(created)
        async with database.connect_db() as db:
            ranks = await db.execute_fetchall(
                "SELECT DISTINCT rank_at_game FROM game_players WHERE game_id=?",
                (game_id,),
            )
            bucket = (await db.execute_fetchall(
                "SELECT rank_bucket FROM game_stats WHERE game_id=?", (game_id,),
            ))[0][0]
        self.assertEqual(ranks, [("ダイヤ",)])
        self.assertEqual(bucket, "ダイヤ")

    async def test_nine_cross_settlement_isolated_in_its_ladder(self) -> None:
        records = self._nine_records()
        primary_player_id = NINE_WOLF_IDS[0]
        voter_id = NINE_WOLF_IDS[1]
        variant = VARIANT_DEFINITIONS["v9_cross"]
        await database.stage_game_settlement(
            1,
            "open_9_cross",
            "run-nine",
            room_name="総合-9クロストーク",
            rated=True,
            winner_team=Team.VILLAGE.value,
            player_records=records,
            variant_id="v9_cross",
        )
        game_id, _results, created = await database.settle_game_settlement(
            1, "open_9_cross", "run-nine",
        )
        self.assertTrue(created)
        async with database.connect_db() as db:
            game = (await db.execute_fetchall(
                "SELECT variant_id, ladder_id FROM games WHERE game_id=?",
                (game_id,),
            ))[0]
            history = await db.execute_fetchall(
                "SELECT DISTINCT variant_id, ladder_id FROM rating_history "
                "WHERE game_id=?",
                (game_id,),
            )
            settlement_parameters = (await db.execute_fetchall(
                "SELECT village_win_pool, wolf_win_pool, wolf_guess_slots, "
                "final_day_threshold FROM game_settlements "
                "WHERE guild_id=1 AND room_id='open_9_cross' AND game_run_id='run-nine'"
            ))[0]
            elo_sum = (await db.execute_fetchall(
                "SELECT SUM(elo_delta) FROM rating_history WHERE game_id=?",
                (game_id,),
            ))[0][0]
        self.assertEqual(game, ("v9_cross", "l9_cross"))
        self.assertEqual(history, [("v9_cross", "l9_cross")])
        self.assertEqual(
            settlement_parameters,
            (
                variant.village_win_pool,
                variant.wolf_win_pool,
                variant.wolf_guess_slots,
                variant.final_day_threshold,
            ),
        )
        self.assertEqual(elo_sum, 0)
        self.assertEqual(len(await database.get_all_player_ratings(1, "l9_cross")), 9)
        self.assertEqual(await database.get_all_player_ratings(1, "l9_turn"), [])
        self.assertEqual(await database.get_all_player_ratings(1), [])

        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO player_ratings "
                "(player_id, guild_id, ladder_id, rating, peak_rating) "
                "VALUES (?, 1, 'l13', 2222, 2222)",
                (primary_player_id,),
            )
            await db.execute(
                "INSERT INTO game_recommendations "
                "(game_id, guild_id, voter_id, kind, recipient_id, status, expires_at) "
                "VALUES (?, 1, ?, 'recommend', ?, 'confirmed', '2026-01-01')",
                (game_id, voter_id, primary_player_id),
            )
            before_l9_cross = (await db.execute_fetchall(
                "SELECT rating FROM player_ratings "
                "WHERE player_id=? AND guild_id=1 AND ladder_id='l9_cross'",
                (primary_player_id,),
            ))[0][0]
            await db.commit()

        await database.finalize_game_recommendations(game_id, 1)
        async with database.connect_db() as db:
            ratings = await db.execute_fetchall(
                "SELECT ladder_id, rating FROM player_ratings "
                "WHERE player_id=? AND guild_id=1 ORDER BY ladder_id",
                (primary_player_id,),
            )
            histories = await db.execute_fetchall(
                "SELECT ladder_id, recommendation_bonus FROM rating_history "
                "WHERE game_id=? AND player_id=?",
                (game_id, primary_player_id),
            )
        self.assertEqual(
            ratings, [("l13", 2222), ("l9_cross", before_l9_cross + 1)],
        )
        self.assertEqual(histories, [("l9_cross", 1)])

    async def test_nine_discussion_modes_keep_same_players_in_separate_ladders(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            room_id = f"room-{variant_id}"
            run_id = f"run-{variant_id}-split"
            await database.stage_game_settlement(
                1,
                room_id,
                run_id,
                room_name=variant_id,
                rated=True,
                winner_team=Team.VILLAGE.value,
                player_records=self._nine_records(),
                variant_id=variant_id,
            )
            _game_id, _results, created = await database.settle_game_settlement(
                1, room_id, run_id,
            )
            self.assertTrue(created)

        async with database.connect_db() as db:
            ratings = await db.execute_fetchall(
                "SELECT ladder_id, games FROM player_ratings "
                "WHERE guild_id=1 AND player_id=? ORDER BY ladder_id",
                (NINE_WOLF_IDS[0],),
            )
            histories = await db.execute_fetchall(
                "SELECT DISTINCT ladder_id FROM rating_history "
                "WHERE guild_id=1 ORDER BY ladder_id"
            )
        self.assertEqual(ratings, [("l9_cross", 1), ("l9_turn", 1)])
        self.assertEqual(histories, [("l9_cross",), ("l9_turn",)])

    async def test_nine_variants_share_rated_settlement_play_and_postgame_bonuses(self) -> None:
        """9人クロストークとターン制は、実配役でも精算ルールを共有する。"""
        outcomes: dict[str, dict] = {}
        deaths = {
            NINE_SEER_ID: {"died_on_day": 1, "death_cause": "襲撃"},
            NINE_VILLAGER_IDS[0]: {"died_on_day": 1, "death_cause": "処刑"},
        }
        bonus_facts = {
            "days": 4,
            "guard_successes": 1,
            "night1_kill_target": NINE_SEER_ID,
            "executions": [
                {"day": 2, "target": NINE_WOLF_IDS[0], "voters": [NINE_VILLAGER_IDS[1]]},
            ],
            "wolf_guesses": {
                NINE_VILLAGER_IDS[0]: list(NINE_WOLF_IDS),
            },
        }

        for guild_id, variant_id in enumerate(("v9_cross", "v9_turn"), start=1):
            room_id = {
                "v9_cross": "open_9_cross",
                "v9_turn": "open_9_turn",
            }[variant_id]
            await database.stage_game_settlement(
                guild_id,
                room_id,
                f"run-{variant_id}-bonuses",
                room_name=variant_id,
                rated=True,
                winner_team=Team.VILLAGE.value,
                player_records=self._nine_records(deaths),
                variant_id=variant_id,
                bonus_facts=bonus_facts,
            )
            game_id, settlement_results, created = await database.settle_game_settlement(
                guild_id,
                room_id,
                f"run-{variant_id}-bonuses",
            )
            self.assertTrue(created)
            self.assertIsNotNone(settlement_results)

            await database.create_game_recommendation_ballots(
                game_id, guild_id, {NINE_VILLAGER_IDS[1]},
                timeout_seconds=60, kind="postgame",
            )
            self.assertEqual(
                await database.confirm_game_recommendation(
                    game_id, guild_id, NINE_VILLAGER_IDS[1], NINE_WOLF_IDS[0],
                    kind="postgame",
                ),
                "confirmed",
            )
            postgame_results = await database.finalize_game_recommendations(game_id, guild_id)

            async with database.connect_db() as db:
                game = (await db.execute_fetchall(
                    "SELECT variant_id, ladder_id FROM games WHERE game_id=?",
                    (game_id,),
                ))[0]
                parameters = (await db.execute_fetchall(
                    "SELECT village_win_pool, wolf_win_pool, wolf_guess_slots, "
                    "final_day_threshold FROM game_settlements "
                    "WHERE guild_id=? AND room_id=? AND game_run_id=?",
                    (guild_id, room_id, f"run-{variant_id}-bonuses"),
                ))[0]
                history = await db.execute_fetchall(
                    "SELECT player_id, rating_before, rating_after, elo_delta, bonus, "
                    "play_bonus, recommendation_bonus FROM rating_history "
                    "WHERE game_id=? ORDER BY player_id",
                    (game_id,),
                )
                wolf_guess_hits = (await db.execute_fetchall(
                    "SELECT wolf_guess_hits FROM game_players "
                    "WHERE game_id=? AND player_id=?",
                    (game_id, NINE_VILLAGER_IDS[0]),
                ))[0][0]

            self.assertEqual(
                game, (variant_id, VARIANT_DEFINITIONS[variant_id].ladder_id),
            )
            self.assertEqual(parameters[2:], (2, 4))
            self.assertEqual(wolf_guess_hits, 2)
            outcomes[variant_id] = {
                "parameters": parameters,
                "settlement_results": settlement_results,
                "history": history,
                "postgame_results": postgame_results,
            }

        cross = outcomes["v9_cross"]
        turn = outcomes["v9_turn"]
        self.assertEqual(cross["parameters"], turn["parameters"])
        self.assertEqual(cross["settlement_results"], turn["settlement_results"])
        self.assertEqual(cross["history"], turn["history"])
        self.assertEqual(cross["postgame_results"], turn["postgame_results"])

        play_bonuses = {
            player_id: play_bonus
            for player_id, _before, _after, _elo, _bonus, play_bonus, _recommendation
            in cross["history"]
            if play_bonus
        }
        self.assertEqual(
            play_bonuses,
            {
                NINE_WOLF_IDS[0]: 3,  # 4日目到達 + 初夜占い師襲撃
                NINE_WOLF_IDS[1]: 3,
                NINE_GUARD_ID: 1,
                NINE_VILLAGER_IDS[0]: 4,  # 早期の狼2人全的中
                NINE_VILLAGER_IDS[1]: 2,  # 人狼への処刑投票
            },
        )
        self.assertEqual(len(cross["postgame_results"]), 1)
        postgame = cross["postgame_results"][0]
        self.assertEqual(postgame["player_id"], NINE_WOLF_IDS[0])
        self.assertEqual(postgame["bonus"], 1)
        self.assertEqual(postgame["rating_after"], postgame["rating_before"] + 1)

    async def test_restaging_same_run_cannot_change_variant_or_parameters(self) -> None:
        records = self._nine_records()
        await database.stage_game_settlement(
            1,
            "open_9_cross",
            "immutable-run",
            room_name="総合-9クロストーク",
            rated=True,
            winner_team=Team.VILLAGE.value,
            player_records=records,
            variant_id="v9_cross",
        )
        with self.assertRaisesRegex(ValueError, "cannot change"):
            await database.stage_game_settlement(
                1,
                "open_9_cross",
                "immutable-run",
                room_name="総合-9クロストーク",
                rated=True,
                winner_team=Team.VILLAGE.value,
                player_records=records,
                variant_id="v9_cross",
                village_win_pool=VARIANT_DEFINITIONS["v9_cross"].village_win_pool + 1,
            )
        with self.assertRaisesRegex(ValueError, "cannot change"):
            await database.stage_game_settlement(
                1,
                "open_9_cross",
                "immutable-run",
                room_name="総合-9クロストーク",
                rated=True,
                winner_team=Team.VILLAGE.value,
                player_records=records,
                variant_id="v13_cross",
            )

    async def test_stats_and_recent_history_filter_variants(self) -> None:
        await database.stage_game_settlement(
            1, "open", "run-v13", room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value, player_records=self._records(),
        )
        await database.settle_game_settlement(1, "open", "run-v13")
        await database.stage_game_settlement(
            1,
            "open_9_cross",
            "run-v9-stats",
            room_name="総合-9クロストーク",
            rated=False,
            winner_team=Team.VILLAGE.value,
            player_records=self._nine_records(),
            variant_id="v9_cross",
        )
        await database.settle_game_settlement(
            1, "open_9_cross", "run-v9-stats",
        )

        self.assertEqual((await database.get_overall_game_stats(1))["games"], 1)
        self.assertEqual(
            (await database.get_overall_game_stats(
                1, variant_id="v9_cross",
            ))["games"],
            1,
        )
        self.assertEqual(len(await database.get_recent_games(1)), 1)
        recent_nine = await database.get_recent_games(1, variant_id="v9_cross")
        self.assertEqual(recent_nine[0]["room_name"], "総合-9クロストーク")
        self.assertIsNone(await database.get_player_stats(NINE_WOLF_IDS[0], 1))
        self.assertEqual(
            (await database.get_player_stats(
                NINE_WOLF_IDS[0], 1, variant_id="v9_cross",
            ))["total"],
            1,
        )

    async def test_player_recommendations_exclude_unrated_rooms(self) -> None:
        """個人統計の推薦票も、ほかの集計と同じく非レート卓を除く。"""
        async with database.connect_db() as db:
            game_ids: dict[str, int] = {}
            for room_id in ("rated", "nate"):
                cursor = await db.execute(
                    "INSERT INTO games "
                    "(guild_id, variant_id, ladder_id, room_id, room_name, winner_team) "
                    "VALUES (1, 'v13_cross', 'l13', ?, ?, ?)",
                    (room_id, room_id, Team.VILLAGE.value),
                )
                game_id = int(cursor.lastrowid)
                game_ids[room_id] = game_id
                await db.execute(
                    "INSERT INTO game_players "
                    "(game_id, player_id, role, team, won) VALUES (?, 42, ?, ?, 1)",
                    (game_id, Role.VILLAGER.value, Team.VILLAGE.value),
                )
            for room_id, bonus in (("rated", 2), ("nate", 7)):
                await db.execute(
                    "INSERT INTO rating_history "
                    "(player_id, guild_id, game_id, variant_id, ladder_id, "
                    "rating_before, rating_after, elo_delta, recommendation_bonus) "
                    "VALUES (42, 1, ?, 'v13_cross', 'l13', 1500, 1500, 0, ?)",
                    (game_ids[room_id], bonus),
                )
            await db.commit()

        with patch.object(database, "UNRATED_ROOM_IDS", frozenset({"nate"})):
            stats = await database.get_player_stats(42, 1)

        assert stats is not None
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["recommendations_received"], 2)

    async def test_stats_write_failure_does_not_rollback_core_settlement(self) -> None:
        await database.stage_game_settlement(
            1, "open", "run-stats-fail", room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value, player_records=self._records(),
            game_stats=self._stats(), gm_id=999,
        )
        with patch(
            "database._write_game_stats",
            new=AsyncMock(side_effect=RuntimeError("optional stats unavailable")),
        ):
            game_id, _results, created = await database.settle_game_settlement(
                1, "open", "run-stats-fail",
            )

        self.assertTrue(created)
        async with database.connect_db() as db:
            game_count = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM games WHERE game_id = ?", (game_id,),
            ))[0][0]
            player_count = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM game_players WHERE game_id = ?", (game_id,),
            ))[0][0]
            settlement = (await db.execute_fetchall(
                "SELECT status FROM game_settlements WHERE game_id = ?", (game_id,),
            ))[0][0]
        self.assertEqual(game_count, 1)
        self.assertEqual(player_count, 13)
        self.assertEqual(settlement, "settled")

        # 付加統計が無くても試合時ランク付きの役職・勝敗は消さない。
        async with database.connect_db() as db:
            await db.execute(
                "UPDATE game_players SET rank_at_game='ダイヤ', rank_provisional=0 "
                "WHERE game_id=?",
                (game_id,),
            )
            await db.commit()
        ranked = await database.get_rank_player_stats(1, rank_name="ダイヤ")
        self.assertEqual(ranked["players"], 13)
        self.assertEqual(sum(item["count"] for item in ranked["roles"].values()), 13)
        self.assertEqual(ranked["seer"]["checks"], 0)

    async def test_init_db_rejects_legacy_game_tables_without_changing_them(self) -> None:
        legacy_path = str(Path(self._tmp.name) / "legacy.db")
        async with database.aiosqlite.connect(legacy_path) as db:
            await db.execute(
                "CREATE TABLE games (game_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, "
                "winner_team TEXT NOT NULL, played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            await db.execute(
                "CREATE TABLE game_players (id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL, "
                "player_id INTEGER NOT NULL, role TEXT NOT NULL, team TEXT NOT NULL, won INTEGER NOT NULL)"
            )
            await db.execute(
                "INSERT INTO games (game_id, guild_id, winner_team) VALUES (1, 1, '村陣営')"
            )
            await db.execute(
                "INSERT INTO game_players (id, game_id, player_id, role, team, won) "
                "VALUES (1, 1, 10, '村人', '村陣営', 1)"
            )
            await db.commit()
            schema_version_before = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
            journal_mode_before = (await db.execute_fetchall(
                "PRAGMA journal_mode"
            ))[0][0]

        database.DB_PATH = legacy_path
        with self.assertRaisesRegex(RuntimeError, "未移行のDBスキーマ"):
            await database.init_db()
        async with database.connect_db() as db:
            tables = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            old_player = await db.execute_fetchall(
                "SELECT id, game_id, player_id, role, team, won FROM game_players"
            )
            schema_version_after = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
            journal_mode_after = (await db.execute_fetchall(
                "PRAGMA journal_mode"
            ))[0][0]
        self.assertEqual(tables, [("game_players",), ("games",)])
        self.assertEqual(old_player, [(1, 1, 10, "村人", "村陣営", 1)])
        self.assertEqual(schema_version_after, schema_version_before)
        self.assertEqual(journal_mode_after, journal_mode_before)

    async def test_init_db_rejects_unmigrated_settlement_parameters_without_backfill(self) -> None:
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO game_settlements "
                "(guild_id, room_id, game_run_id, variant_id, ladder_id, room_name, "
                "rated, winner_team, player_records) "
                "VALUES (1, 'legacy', 'run-legacy', 'v9_cross', 'l9_cross', "
                "'旧9人村', 1, '村陣営', '[]')"
            )
            await db.commit()

        with self.assertRaisesRegex(RuntimeError, "レート用スナップショット"):
            await database.init_db()
        async with database.connect_db() as db:
            parameters = (await db.execute_fetchall(
                "SELECT village_win_pool, wolf_win_pool, wolf_guess_slots, "
                "final_day_threshold FROM game_settlements "
                "WHERE guild_id=1 AND room_id='legacy' AND game_run_id='run-legacy'"
            ))[0]
        self.assertEqual(parameters, (None, None, None, None))

    async def test_init_db_rejects_reintroduced_legacy_l9_without_rewriting_it(self) -> None:
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO games "
                "(guild_id, variant_id, ladder_id, room_id, winner_team) "
                "VALUES (1, 'v9_cross', 'l9', 'legacy-l9', '村陣営')"
            )
            await db.commit()

        with self.assertRaisesRegex(RuntimeError, "旧l9"):
            await database.init_db()

        async with database.connect_db() as db:
            ladder_id = (await db.execute_fetchall(
                "SELECT ladder_id FROM games WHERE room_id='legacy-l9'"
            ))[0][0]
        self.assertEqual(ladder_id, "l9")


class VariantBalanceStatsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-balance-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "balance.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    async def test_aggregates_public_nine_player_variants_without_writes(self) -> None:
        async with database.connect_db() as db:
            for game_id, variant_id, winner in (
                (1, "v9_cross", Team.WOLF.value),
                (2, "v9_cross", Team.VILLAGE.value),
                (3, "v9_turn", Team.WOLF.value),
                (4, "v13_cross", Team.WOLF.value),
            ):
                await db.execute(
                    "INSERT INTO games "
                    "(game_id, guild_id, variant_id, ladder_id, room_id, room_name, "
                    "game_run_id, winner_team) VALUES (?, 1, ?, ?, 'private', '村', ?, ?)",
                    (
                        game_id,
                        variant_id,
                        VARIANT_DEFINITIONS[variant_id].ladder_id,
                        f"run-{game_id}",
                        winner,
                    ),
                )
            await db.commit()

        async with database.connect_db() as db:
            before_games = await db.execute_fetchall(
                "SELECT game_id, variant_id, winner_team FROM games ORDER BY game_id"
            )

        rows = await database.get_variant_balance_stats(1)

        async with database.connect_db() as db:
            after_games = await db.execute_fetchall(
                "SELECT game_id, variant_id, winner_team FROM games ORDER BY game_id"
            )

        self.assertEqual(
            rows,
            [
                {"variant_id": "v9_cross", "games": 2, "wolf_wins": 1},
                {"variant_id": "v9_turn", "games": 1, "wolf_wins": 1},
            ],
        )
        self.assertEqual(after_games, before_games)


class GameSequenceNumberTest(unittest.IsolatedAsyncioTestCase):
    """表示用の通し番号は game_id の欠番を詰めて1から数える。

    game_id はAUTOINCREMENTなので、開発中の検証で消費したぶんや、
    精算に失敗した試合のぶんが欠番として残る。そのまま出すと
    「3, 69, 70, 71」のように飛んで見える。
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-game-seq-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "seq.db")
        await database.init_db()
        # game_id を 3 / 69 / 70 と飛ばして作る (本番で起きた並びを再現)
        async with database.connect_db() as db:
            for game_id, winner in ((3, Team.VILLAGE.value), (69, Team.WOLF.value), (70, Team.WOLF.value)):
                await db.execute(
                    "INSERT INTO games (game_id, guild_id, winner_team, room_id, room_name) "
                    "VALUES (?, ?, ?, 'open', '総合')",
                    (game_id, 1, winner),
                )
                await db.execute(
                    "INSERT INTO game_players (game_id, player_id, role, team, won) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (game_id, 555, Role.VILLAGER.value, Team.VILLAGE.value, 1),
                )
            # 別サーバーの試合は番号に混ぜない
            await db.execute(
                "INSERT INTO games (game_id, guild_id, winner_team, room_id, room_name) "
                "VALUES (999, 2, ?, 'open', '総合')",
                (Team.WOLF.value,),
            )
            await db.commit()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    async def test_recent_games_number_from_one_ignoring_gaps(self) -> None:
        rows = await database.get_recent_games(1, limit=10)

        # 新しい順に出しつつ、番号は古い試合が1
        self.assertEqual([row["seq"] for row in rows], [3, 2, 1])
        self.assertEqual([row["game_id"] for row in rows], [70, 69, 3])

    async def test_player_history_uses_the_same_numbers(self) -> None:
        rows = await database.get_player_recent_games(555, 1, limit=10)

        # 自分の履歴でも「サーバー全体で何試合目か」で揃える
        self.assertEqual([row["seq"] for row in rows], [3, 2, 1])
        self.assertEqual([row["game_id"] for row in rows], [70, 69, 3])

    async def test_numbering_is_per_guild(self) -> None:
        rows = await database.get_recent_games(2, limit=10)

        self.assertEqual([row["seq"] for row in rows], [1])
        self.assertEqual([row["game_id"] for row in rows], [999])


if __name__ == "__main__":
    unittest.main()
