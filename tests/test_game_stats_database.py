"""統計スキーマと精算トランザクションの回帰テスト。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from config import Role, Team


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

    async def test_init_db_migrates_legacy_game_tables_without_backfill(self) -> None:
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

        database.DB_PATH = legacy_path
        await database.init_db()
        async with database.connect_db() as db:
            game_columns = {
                row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")
            }
            player_columns = {
                row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_players)")
            }
            old_player = (await db.execute_fetchall(
                "SELECT died_on_day, death_cause, rank_at_game, rank_provisional "
                "FROM game_players WHERE id = 1"
            ))[0]
            old_stats = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM game_stats WHERE game_id = 1"
            ))[0][0]
        self.assertIn("gm_id", game_columns)
        self.assertIn("base_room_id", game_columns)
        self.assertIn("recruitment_id", game_columns)
        self.assertTrue({"died_on_day", "death_cause", "rank_at_game", "rank_provisional"} <= player_columns)
        self.assertEqual(old_player, (None, None, None, None))
        self.assertEqual(old_stats, 0)


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
