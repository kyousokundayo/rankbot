"""v0.50: レート推移グラフ用の系列取得・相性集計・夜行動ログ読み出しの回帰テスト。

DBテストの雛形は tests/test_game_stats_database.py:22 に倣う。
seed は精算APIを通さず直接INSERTする——played_at や陣営構成を1試合ずつ
指定したいのが目的で、精算経路の検証は既存テストが担っているため。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

import database
import stats_image
from config import Role, Team


class _SeededDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-v050-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "v050.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    async def _add_game(
        self,
        game_id: int,
        *,
        played_at: str,
        players: list[tuple[int, str, int]],
        guild_id: int = 1,
        room_id: str = "beginner",
        winner_team: str = Team.VILLAGE.value,
        started_at: str | None = None,
        gm_id: int | None = None,
    ) -> None:
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO games (game_id, guild_id, variant_id, ladder_id, room_id, "
                "room_name, game_run_id, gm_id, winner_team, played_at, started_at) "
                "VALUES (?, ?, 'v13_cross', 'l13', ?, '初心者村', ?, ?, ?, ?, ?)",
                (
                    game_id, guild_id, room_id, f"run-{game_id}", gm_id,
                    winner_team, played_at, started_at,
                ),
            )
            for player_id, team, won in players:
                await db.execute(
                    "INSERT INTO game_players "
                    "(game_id, player_id, role, team, won) VALUES (?, ?, ?, ?, ?)",
                    (
                        game_id, player_id,
                        Role.WEREWOLF.value if team == Team.WOLF.value
                        else Role.VILLAGER.value,
                        team, won,
                    ),
                )
            await db.commit()


class RatingSeriesTest(_SeededDatabaseTest):
    async def _seed_history(self) -> None:
        ratings = [(1200, 1215), (1215, 1205), (1205, 1230)]
        for index, (before, after) in enumerate(ratings, start=1):
            await self._add_game(
                index,
                played_at=f"2026-06-0{index} 12:00:00",
                players=[(11, Team.VILLAGE.value, int(after > before))],
            )
            async with aiosqlite.connect(database.DB_PATH) as db:
                await db.execute(
                    "INSERT INTO rating_history (player_id, guild_id, game_id, "
                    "variant_id, ladder_id, rating_before, rating_after, elo_delta) "
                    "VALUES (11, 1, ?, 'v13_cross', 'l13', ?, ?, ?)",
                    (index, before, after, after - before),
                )
                await db.commit()
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO player_ratings (player_id, guild_id, ladder_id, "
                "rating, peak_rating, games, wins, season_games, season_wins) "
                "VALUES (11, 1, 'l13', 1230, 1240, 3, 2, 3, 2)",
            )
            await db.commit()

    async def test_series_is_ordered_and_carries_win_flags(self) -> None:
        await self._seed_history()
        series = await database.get_rating_series(11, 1)

        self.assertEqual(
            [point["rating_after"] for point in series["points"]], [1215, 1205, 1230],
        )
        self.assertEqual(
            [point["won"] for point in series["points"]], [True, False, True],
        )
        self.assertEqual(series["current_rating"], 1230)
        self.assertEqual(series["peak_rating"], 1240)
        self.assertFalse(series["truncated"])

    async def test_unrated_rooms_are_excluded(self) -> None:
        """練習卓 (レート対象外) はレート推移にも出さない。"""
        await self._seed_history()
        await self._add_game(
            9, played_at="2026-06-09 12:00:00", room_id="nate",
            players=[(11, Team.VILLAGE.value, 1)],
        )
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO rating_history (player_id, guild_id, game_id, "
                "variant_id, ladder_id, rating_before, rating_after, elo_delta) "
                "VALUES (11, 1, 9, 'v13_cross', 'l13', 1230, 1299, 69)",
            )
            await db.commit()

        series = await database.get_rating_series(11, 1)
        self.assertNotIn(1299, [point["rating_after"] for point in series["points"]])

    async def test_season_reset_marks_the_next_game(self) -> None:
        await self._seed_history()
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO season_resets (guild_id, executed_by, reset_at, "
                "affected_players) VALUES (1, 99, '2026-06-02 18:00:00', 1)",
            )
            await db.commit()

        series = await database.get_rating_series(11, 1)
        data = stats_image.format_rating_chart_data(
            display_name="テスト", series=series, variant_label="13人（公開）",
        )
        # 6/03の試合 (index 2) の手前にリセット線を引く
        self.assertEqual(data["reset_indexes"], [2])
        self.assertEqual(data["recent_delta"], 30)  # 1200 -> 1230

    @unittest.skipUnless(stats_image.font_available(), "日本語フォントが無い環境")
    async def test_chart_renders_png(self) -> None:
        await self._seed_history()
        series = await database.get_rating_series(11, 1)
        data = stats_image.format_rating_chart_data(
            display_name="テスト", series=series, variant_label="13人（公開）",
        )
        png = stats_image.render_rating_chart(data)
        self.assertTrue(png.startswith(b"\x89PNG"))

    @unittest.skipUnless(stats_image.font_available(), "日本語フォントが無い環境")
    async def test_chart_renders_without_any_game(self) -> None:
        series = await database.get_rating_series(11, 1)
        data = stats_image.format_rating_chart_data(
            display_name="テスト", series=series, variant_label="13人（公開）",
        )
        self.assertTrue(stats_image.render_rating_chart(data).startswith(b"\x89PNG"))


class CompatibilityTest(_SeededDatabaseTest):
    async def _seed_pair(self) -> None:
        """自分(1)の陣営別勝率を 村0.5 / 狼1.0 にし、相手(2)を全試合へ入れる。

        同陣営: g1(村・勝) と g5(狼・勝) → 実測1.00 / 期待0.75
        敵対  : g2(村・負) と g6(狼・勝) → 実測0.50 / 期待0.75
        """
        village = Team.VILLAGE.value
        wolf = Team.WOLF.value
        layout = [
            (1, "2026-06-01 12:00:00", [(1, village, 1), (2, village, 1)]),
            (2, "2026-06-02 12:00:00", [(1, village, 0), (2, wolf, 1)]),
            (3, "2026-06-03 12:00:00", [(1, village, 1), (2, wolf, 0)]),
            (4, "2026-06-04 12:00:00", [(1, village, 0), (2, wolf, 1)]),
            (5, "2026-06-05 12:00:00", [(1, wolf, 1), (2, wolf, 1)]),
            (6, "2026-06-06 12:00:00", [(1, wolf, 1), (2, village, 0)]),
        ]
        for game_id, played_at, players in layout:
            await self._add_game(game_id, played_at=played_at, players=players)

    async def test_expected_value_is_taken_from_own_team_win_rates(self) -> None:
        await self._seed_pair()
        data = await database.get_player_compatibility(1, 1, min_games=1)

        self.assertAlmostEqual(data["win_rate"], 4 / 6)
        self.assertAlmostEqual(data["team_win_rates"][Team.VILLAGE.value], 0.5)
        self.assertAlmostEqual(data["team_win_rates"][Team.WOLF.value], 1.0)

        same = {row["player_id"]: row for row in data["same"]}[2]
        self.assertEqual((same["games"], same["wins"]), (2, 2))
        self.assertAlmostEqual(same["expected"], 0.75)
        self.assertAlmostEqual(same["diff"], 0.25)

        opposite = {row["player_id"]: row for row in data["opposite"]}[2]
        self.assertEqual((opposite["games"], opposite["wins"]), (4, 2))
        self.assertAlmostEqual(opposite["rate"], 0.5)
        self.assertAlmostEqual(opposite["expected"], 0.625)  # 村3戦+狼1戦の加重

    async def test_no_games_returns_empty_shape(self) -> None:
        data = await database.get_player_compatibility(999, 1)
        self.assertEqual(data["games"], 0)
        self.assertEqual(data["same"], [])
        self.assertEqual(data["opposite"], [])


class NightActionRecordsTest(_SeededDatabaseTest):
    async def test_record_once_is_idempotent_and_listing_filters(self) -> None:
        for _ in range(3):
            await database.record_night_action_once(
                1, "beginner", "run-x",
                event_seq=0, night_number=0, actor_id=11, actor_number=1,
                actor_role="占い師", action="初日白",
                target_id=12, target_number=2, result="村人",
            )
        await database.record_night_action(
            1, "beginner", "run-x",
            event_seq=5, night_number=1, actor_id=11, actor_number=1,
            actor_role="占い師", action="占い",
            target_id=13, target_number=3, result="人狼",
        )
        await database.record_night_action(
            1, "beginner", "run-x",
            event_seq=6, night_number=1, actor_id=14, actor_number=4,
            actor_role="狩人", action="護衛",
            target_id=13, target_number=3, result=None,
        )

        rows = await database.list_night_actions_for_run(
            1, "beginner", "run-x", actor_id=11,
        )
        self.assertEqual([row["action"] for row in rows], ["初日白", "占い"])

        guard_rows = await database.list_night_actions_for_run(
            1, "beginner", "run-x", actions=("護衛",),
        )
        self.assertEqual(len(guard_rows), 1)
        self.assertEqual(guard_rows[0]["actor_id"], 14)


if __name__ == "__main__":
    unittest.main()
