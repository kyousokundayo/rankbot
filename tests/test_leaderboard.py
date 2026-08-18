"""項目別ランキング (database.get_metric_leaderboard) の単体テスト

実行: .venv/bin/python -m unittest discover -s tests -v
(scripts/run_checks.sh と CI からも実行される)

値は決め打ちで作る。乱数で作ると「0人しか載らない」のがデータ都合なのか
クエリの誤りなのか切り分けられないため。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database
from config import Role, Team

GUILD_ID = 999

# 13人固定構成。player_id は役職ごとに固定し、各試合で使い回す
WOLF_IDS = (1, 2, 3)
MADMAN_ID = 4
SEER_ID = 5
MEDIUM_ID = 6
GUARD_ID = 7
VILLAGER_IDS = (8, 9, 10, 11, 12, 13)

_ROLE_BY_ID = {
    **{pid: Role.WEREWOLF for pid in WOLF_IDS},
    MADMAN_ID: Role.MADMAN,
    SEER_ID: Role.SEER,
    MEDIUM_ID: Role.MEDIUM,
    GUARD_ID: Role.GUARD,
    **{pid: Role.VILLAGER for pid in VILLAGER_IDS},
}
_WOLF_TEAM_IDS = set(WOLF_IDS) | {MADMAN_ID}


def build_records(
    winner: Team,
    deaths: dict[int, tuple[int, str]] | None = None,
) -> list[dict]:
    """deaths は player_id -> (死亡日, 死因)。載らない人は最後まで生存。"""
    deaths = deaths or {}
    records = []
    for player_id, role in _ROLE_BY_ID.items():
        team = Team.WOLF if player_id in _WOLF_TEAM_IDS else Team.VILLAGE
        died_on_day, death_cause = deaths.get(player_id, (None, None))
        records.append({
            "player_id": player_id,
            "role": role.value,
            "team": team.value,
            "won": team is winner,
            "died_on_day": died_on_day,
            "death_cause": death_cause,
            "rank_at_game": "ゴールド",
            "rank_provisional": False,
        })
    return records


class LeaderboardTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-leaderboard-")
        self.original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name) / "leaderboard.db")
        await database.init_db()
        self.run_seq = 0

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def play(
        self,
        winner: Team,
        deaths: dict[int, tuple[int, str]] | None = None,
        *,
        wolf_guesses: dict[int, list[int]] | None = None,
        variant_id: str = "v13_cross",
    ) -> int:
        self.run_seq += 1
        run_id = f"run-{self.run_seq}"
        records = build_records(winner, deaths)
        await database.stage_game_settlement(
            GUILD_ID, "open", run_id,
            room_name="総合", rated=True, winner_team=winner.value,
            player_records=records,
            variant_id=variant_id,
            bonus_facts={
                "days": 5,
                "wolf_guesses": {
                    str(pid): list(targets)
                    for pid, targets in (wolf_guesses or {}).items()
                },
            },
        )
        game_id, _results, _created = await database.settle_game_settlement(
            GUILD_ID, "open", run_id,
        )
        return game_id

    async def board(self, metric: str, **kwargs) -> dict:
        kwargs.setdefault("min_samples", 1)
        return await database.get_metric_leaderboard(GUILD_ID, metric, **kwargs)

    @staticmethod
    def value_of(board: dict, player_id: int) -> float | None:
        for entry in board["top"]:
            if entry["player_id"] == player_id:
                return entry["value"]
        return None


class TestVillageDay1Executed(LeaderboardTestBase):
    async def test_counts_only_day1_executions_of_village_side(self):
        # 村人8: 初日処刑 / 初日襲撃 / 2日目処刑 / 生存 の4戦 → 1/4
        await self.play(Team.WOLF, {8: (1, "処刑")})
        await self.play(Team.WOLF, {8: (1, "襲撃")})
        await self.play(Team.WOLF, {8: (2, "処刑")})
        await self.play(Team.VILLAGE)
        board = await self.board("village_day1_executed")
        self.assertAlmostEqual(self.value_of(board, 8), 0.25)

    async def test_wolf_side_is_excluded(self):
        """狼陣営で初日に吊られても、この指標には入らない (狂人も含む)"""
        await self.play(Team.VILLAGE, {WOLF_IDS[0]: (1, "処刑")})
        await self.play(Team.VILLAGE, {MADMAN_ID: (1, "処刑")})
        board = await self.board("village_day1_executed")
        listed = {entry["player_id"] for entry in board["top"]}
        self.assertNotIn(WOLF_IDS[0], listed)
        self.assertNotIn(MADMAN_ID, listed)
        self.assertIn(SEER_ID, listed)


class TestWolfSurviveOnWin(LeaderboardTestBase):
    async def test_survival_rate_among_wolf_wins(self):
        # 人狼1: 狼勝ち2戦のうち1戦だけ生存 → 0.5
        await self.play(Team.WOLF)                              # 生存で勝ち
        await self.play(Team.WOLF, {WOLF_IDS[0]: (3, "処刑")})   # 死んで勝ち
        board = await self.board("wolf_survive_on_win")
        self.assertAlmostEqual(self.value_of(board, WOLF_IDS[0]), 0.5)

    async def test_losses_are_not_counted(self):
        """負けた試合は分母に入らない (狼勝利時に限る指標のため)"""
        await self.play(Team.VILLAGE)   # 狼は生存しているが敗北
        await self.play(Team.WOLF)      # 生存で勝ち
        board = await self.board("wolf_survive_on_win")
        entry = next(e for e in board["top"] if e["player_id"] == WOLF_IDS[0])
        self.assertEqual(entry["denominator"], 1)
        self.assertAlmostEqual(entry["value"], 1.0)

    async def test_madman_is_not_included(self):
        """狂人は人狼ではないのでこの指標に入らない"""
        await self.play(Team.WOLF)
        board = await self.board("wolf_survive_on_win")
        listed = {entry["player_id"] for entry in board["top"]}
        self.assertEqual(listed, set(WOLF_IDS))


class TestRoleWinrate(LeaderboardTestBase):
    async def test_win_rate_for_the_selected_role(self):
        await self.play(Team.WOLF)
        await self.play(Team.WOLF)
        await self.play(Team.VILLAGE)
        wolf_board = await self.board("role_winrate", role=Role.WEREWOLF.value)
        self.assertAlmostEqual(self.value_of(wolf_board, WOLF_IDS[0]), 2 / 3)
        seer_board = await self.board("role_winrate", role=Role.SEER.value)
        self.assertAlmostEqual(self.value_of(seer_board, SEER_ID), 1 / 3)

    async def test_role_is_required(self):
        with self.assertRaises(ValueError):
            await database.get_metric_leaderboard(GUILD_ID, "role_winrate")


class TestVotesReceived(LeaderboardTestBase):
    async def test_average_votes_per_game(self):
        game_id = await self.play(Team.VILLAGE)
        await self.play(Team.VILLAGE)
        voters = {SEER_ID, MEDIUM_ID, GUARD_ID}
        await database.create_game_recommendation_ballots(
            game_id, GUILD_ID, voters, timeout_seconds=180, kind="postgame",
        )
        for voter_id in voters:
            await database.confirm_game_recommendation(
                game_id, GUILD_ID, voter_id, WOLF_IDS[0], kind="postgame",
            )
        await database.finalize_game_recommendations(game_id, GUILD_ID)

        board = await self.board("votes_received")
        # 3票を2戦で割る
        self.assertAlmostEqual(self.value_of(board, WOLF_IDS[0]), 1.5)
        self.assertAlmostEqual(self.value_of(board, VILLAGER_IDS[0]), 0.0)


class TestWolfGuessAccuracy(LeaderboardTestBase):
    async def test_hits_over_slots(self):
        # 村人8: 1戦目は3人中2人的中、2戦目は0人的中 → 2/6
        await self.play(
            Team.WOLF, {8: (2, "襲撃")},
            wolf_guesses={8: [WOLF_IDS[0], WOLF_IDS[1], MADMAN_ID]},
        )
        await self.play(
            Team.WOLF, {8: (3, "処刑")},
            wolf_guesses={8: [MADMAN_ID, SEER_ID, GUARD_ID]},
        )
        board = await self.board("wolf_guess_accuracy")
        entry = next(e for e in board["top"] if e["player_id"] == 8)
        self.assertEqual(entry["numerator"], 2)
        self.assertEqual(entry["denominator"], 6)
        self.assertEqual(entry["samples"], 2)

    async def test_non_submitters_are_absent(self):
        """未提出は分母にも入らない (NULLのまま)"""
        await self.play(
            Team.WOLF, {8: (2, "襲撃"), 9: (2, "襲撃")},
            wolf_guesses={8: list(WOLF_IDS)},
        )
        board = await self.board("wolf_guess_accuracy")
        listed = {entry["player_id"] for entry in board["top"]}
        self.assertEqual(listed, {8})

    async def test_madman_never_counts_as_a_hit(self):
        await self.play(
            Team.WOLF, {8: (2, "襲撃")},
            wolf_guesses={8: [WOLF_IDS[0], MADMAN_ID, SEER_ID]},
        )
        board = await self.board("wolf_guess_accuracy")
        self.assertEqual(board["top"][0]["numerator"], 1)

    async def test_madman_submission_counts_but_actual_wolf_is_excluded(self):
        await self.play(
            Team.VILLAGE,
            {
                MADMAN_ID: (2, "処刑"),
                WOLF_IDS[0]: (3, "処刑"),
            },
            wolf_guesses={
                MADMAN_ID: list(WOLF_IDS),
                WOLF_IDS[0]: list(WOLF_IDS),
            },
        )

        board = await self.board("wolf_guess_accuracy")
        entries = {entry["player_id"]: entry for entry in board["top"]}
        self.assertEqual(entries[MADMAN_ID]["numerator"], 3)
        self.assertEqual(entries[MADMAN_ID]["denominator"], 3)
        self.assertNotIn(WOLF_IDS[0], entries)

    async def test_nine_player_variant_uses_two_guess_slots(self):
        await self.play(
            Team.WOLF,
            {8: (2, "襲撃")},
            wolf_guesses={8: [WOLF_IDS[0], WOLF_IDS[1], WOLF_IDS[2]]},
            variant_id="v9_cross",
        )
        board = await self.board("wolf_guess_accuracy", variant_id="v9_cross")
        entry = next(e for e in board["top"] if e["player_id"] == 8)
        self.assertEqual(entry["numerator"], 2)
        self.assertEqual(entry["denominator"], 2)
        self.assertEqual(board["label"], "人狼予想の的中率")

    async def test_default_board_does_not_mix_variants(self):
        await self.play(
            Team.WOLF,
            {8: (2, "襲撃")},
            wolf_guesses={8: [WOLF_IDS[0], WOLF_IDS[1]]},
            variant_id="v9_cross",
        )
        self.assertEqual((await self.board("wolf_guess_accuracy"))["top"], [])


class TestLeaderboardShape(LeaderboardTestBase):
    async def test_min_samples_filters_but_viewer_still_gets_own_value(self):
        """掲載外でも本人の生データは返す (あと何回で載るかを出すため)"""
        await self.play(Team.WOLF, {8: (1, "処刑")})
        board = await database.get_metric_leaderboard(
            GUILD_ID, "village_day1_executed", viewer_id=8, min_samples=5,
        )
        self.assertEqual(board["top"], [])
        self.assertIsNone(board["viewer_position"])
        self.assertIsNotNone(board["viewer"])
        self.assertEqual(board["viewer"]["samples"], 1)

    async def test_ties_break_by_sample_size_then_id(self):
        """同率なら母数の多い順。試行回数が多いほうが確からしいため"""
        await self.play(Team.VILLAGE, {8: (1, "処刑"), 9: (1, "処刑")})
        await self.play(Team.VILLAGE, {8: (1, "処刑")})
        # 8は2/2、9は1/2。9より8が上
        board = await self.board("village_day1_executed")
        order = [entry["player_id"] for entry in board["top"]]
        self.assertLess(order.index(8), order.index(9))

    async def test_unknown_metric_raises(self):
        with self.assertRaises(ValueError):
            await database.get_metric_leaderboard(GUILD_ID, "nope")

    async def test_every_metric_runs_on_an_empty_guild(self):
        for metric, spec in database.LEADERBOARD_METRICS.items():
            with self.subTest(metric=metric):
                kwargs = {"role": Role.WEREWOLF.value} if spec.get("needs_role") else {}
                board = await database.get_metric_leaderboard(
                    GUILD_ID, metric, viewer_id=8, **kwargs,
                )
                self.assertEqual(board["top"], [])
                self.assertIsNone(board["viewer"])


if __name__ == "__main__":
    unittest.main()


class TestDropoutCounts(LeaderboardTestBase):
    """途中離脱 (GM除外) の集計。運営メニューだけに出す指標"""

    async def test_counts_only_removals(self):
        await self.play(Team.WOLF, {8: (2, "除外"), 9: (1, "処刑")})
        await self.play(Team.WOLF, {8: (1, "除外")})
        await self.play(Team.WOLF, {10: (3, "除外")})
        await self.play(Team.VILLAGE)
        rows = await database.get_dropout_counts(GUILD_ID)
        by_id = {row["player_id"]: row for row in rows}
        self.assertEqual(by_id[8]["dropouts"], 2)
        self.assertEqual(by_id[8]["games"], 4)
        self.assertAlmostEqual(by_id[8]["rate"], 0.5)
        self.assertEqual(by_id[10]["dropouts"], 1)
        # 処刑死しかしていない9は載らない
        self.assertNotIn(9, by_id)

    async def test_sorted_by_count_then_fewer_games(self):
        await self.play(Team.WOLF, {8: (1, "除外"), 9: (1, "除外")})
        await self.play(Team.WOLF, {8: (1, "除外")})
        rows = await database.get_dropout_counts(GUILD_ID)
        self.assertEqual([row["player_id"] for row in rows], [8, 9])

    async def test_empty_when_nobody_dropped(self):
        await self.play(Team.WOLF, {8: (1, "処刑")})
        self.assertEqual(await database.get_dropout_counts(GUILD_ID), [])


class TestGrandmasterHistory(LeaderboardTestBase):
    """シーズンごとのグランドマスター (現行ランキングとは別枠)"""

    async def _add_season(self, reset_id: int, reset_at: str, rows: list[tuple]) -> None:
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO season_resets (id, guild_id, executed_by, reset_at, "
                "affected_players) VALUES (?, ?, ?, ?, ?)",
                (reset_id, GUILD_ID, 1, reset_at, len(rows)),
            )
            for player_id, rating, rank_name, position in rows:
                await db.execute(
                    "INSERT INTO rating_snapshots (season_reset_id, player_id, guild_id, "
                    "rating_before, rating_after, season_rank, rank_position, "
                    "top_percent, season_games, season_wins) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (reset_id, player_id, GUILD_ID, rating, 1500 + (rating - 1500) // 2,
                     rank_name, position, 1.0, 40, 25),
                )
            await db.commit()

    async def test_groups_by_season_newest_first(self):
        await self._add_season(1, "2026-02-01T00:00:00", [
            (101, 2100, "グランドマスター", 1),
            (102, 2050, "グランドマスター", 2),
            (103, 1900, "マスター", 3),
        ])
        await self._add_season(2, "2026-05-01T00:00:00", [
            (102, 2200, "グランドマスター", 1),
        ])
        seasons = await database.get_grandmaster_history(GUILD_ID)
        self.assertEqual([s["season_number"] for s in seasons], [2, 1])
        self.assertEqual(
            [m["player_id"] for m in seasons[0]["members"]], [102],
        )
        # 同シーズン内は順位順。マスターは含まない
        self.assertEqual(
            [m["player_id"] for m in seasons[1]["members"]], [101, 102],
        )

    async def test_season_without_grandmasters_is_skipped(self):
        await self._add_season(1, "2026-02-01T00:00:00", [
            (101, 1900, "マスター", 1),
        ])
        self.assertEqual(await database.get_grandmaster_history(GUILD_ID), [])

    async def test_empty_before_any_reset(self):
        self.assertEqual(await database.get_grandmaster_history(GUILD_ID), [])


class TestFeedbackOperationsNotice(unittest.IsolatedAsyncioTestCase):
    """報告を #運営 へ流すときの整形。

    本文は利用者が自由に書けるので、通知本体に見える文面を自作されないよう
    囲いが効いているかを見る。メンションは抑制しない (到達範囲はチャンネルの
    可視性で閉じるため)。
    """

    def setUp(self) -> None:
        import recruitment
        self.recruitment = recruitment

    def test_wraps_body_in_a_code_block(self):
        block = self.recruitment._as_quoted_block("普通の本文", 900)
        self.assertEqual(block, "```\n普通の本文\n```")

    def test_neutralises_backticks_so_the_notice_cannot_be_forged(self):
        block = self.recruitment._as_quoted_block("```py\nimport os\n```", 900)
        self.assertNotIn("```py", block)
        self.assertEqual(block.count("```"), 2)

    def test_truncates_long_bodies(self):
        block = self.recruitment._as_quoted_block("あ" * 50, 10)
        self.assertIn("…", block)
        self.assertLess(len(block), 40)

    def test_empty_body_produces_nothing(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(self.recruitment._as_quoted_block(value, 900), "")

    async def test_notice_fits_in_one_message(self):
        sent: list[tuple] = []

        class FakeChannel:
            async def send(self, content=None, **kwargs):
                sent.append((content, kwargs))

        class FakeMember:
            display_name = "テストユーザー"

        class FakeGuild:
            id = 1

            def get_member(self, _player_id):
                return FakeMember()

        manager = self.recruitment.RecruitmentManager.__new__(
            self.recruitment.RecruitmentManager
        )
        manager.operations_channel = FakeChannel()
        manager.operations_log_channel = None
        await manager.notify_feedback_report(FakeGuild(), {
            "report_id": 7,
            "user_id": 12345,
            "category": "不具合",
            "summary": "@everyone " + "あ" * 1000,
            "details": "い" * 1000,
            "room_name": "総合",
            "phase": "DAY_VOTE",
            "bot_version": "v0.36",
        })
        content, _kwargs = sent[0]
        # 本文は summary/details とも最大1000字。囲いと見出しを足しても
        # Discordの1メッセージ上限 (2000字) を超えないこと
        self.assertLess(len(content), 2000)
        self.assertIn("あ", content)

    async def test_missing_operations_channel_is_a_noop(self):
        manager = self.recruitment.RecruitmentManager.__new__(
            self.recruitment.RecruitmentManager
        )
        manager.operations_channel = None
        manager.operations_log_channel = None
        await manager.notify_feedback_report(object(), {"report_id": 1})
