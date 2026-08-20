"""v0.51: レート推移グラフ用の系列取得・相性集計・夜行動ログ読み出しの回帰テスト。

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
        """自分(1)の対戦相手を2人(2・3)にし、期待値が leave-one-out で計算されることを検証する。

        相手2はg1-g6(自分の村4戦・狼2戦すべてに登場)、相手3はg7-g8
        (村1戦・狼1戦)にのみ登場する。相手2を評価する期待値は「相手2との
        共戦を除いた」村0.5戦0.5戦・狼0.5戦の勝率、つまりg7/g8だけから
        計算されるべきで、逆に相手3を評価する期待値はg1-g6だけから
        計算されるべき (test_leave_one_out_excludes_the_opponent_own_games で検証)。
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
            (7, "2026-06-07 12:00:00", [(1, village, 1), (3, village, 0)]),
            (8, "2026-06-08 12:00:00", [(1, wolf, 0), (3, village, 1)]),
        ]
        for game_id, played_at, players in layout:
            await self._add_game(game_id, played_at=played_at, players=players)

    async def test_leave_one_out_excludes_the_opponent_own_games(self) -> None:
        """期待値は「その相手との共戦を除いた」自分の陣営別勝率から計算される。

        修正前は自分の陣営別勝率(相手の分を含む)をそのまま期待値にしていたため、
        相手2のように自分の試合の大半(6/8)を占める相手ほど期待値が実測へ
        漸近して diff が潰れていた。leave-one-out ならその歪みが起きない。
        """
        await self._seed_pair()
        data = await database.get_player_compatibility(1, 1, min_games=1)

        self.assertAlmostEqual(data["win_rate"], 5 / 8)
        # team_win_rates は「相手を除かない」自分の陣営別勝率 (参考値として表示に使う)。
        self.assertAlmostEqual(data["team_win_rates"][Team.VILLAGE.value], 0.6)
        self.assertAlmostEqual(
            data["team_win_rates"][Team.WOLF.value], 2 / 3,
        )

        same = {row["player_id"]: row for row in data["same"]}
        # 相手2: g1(村)+g5(狼) 2戦2勝。期待値はg7/g8(相手2を除いた分)から。
        self.assertEqual((same[2]["games"], same[2]["wins"]), (2, 2))
        self.assertAlmostEqual(same[2]["expected"], 0.5)
        self.assertAlmostEqual(same[2]["diff"], 0.5)
        # 相手3: g7(村) 1戦1勝。期待値はg1-g6(相手3を除いた分)から。
        self.assertEqual((same[3]["games"], same[3]["wins"]), (1, 1))
        self.assertAlmostEqual(same[3]["expected"], 0.5)
        self.assertAlmostEqual(same[3]["diff"], 0.5)

        opposite = {row["player_id"]: row for row in data["opposite"]}
        self.assertEqual((opposite[2]["games"], opposite[2]["wins"]), (4, 2))
        self.assertAlmostEqual(opposite[2]["expected"], 0.75)
        self.assertAlmostEqual(opposite[2]["diff"], -0.25)
        self.assertEqual((opposite[3]["games"], opposite[3]["wins"]), (1, 0))
        self.assertAlmostEqual(opposite[3]["expected"], 1.0)
        self.assertAlmostEqual(opposite[3]["diff"], -1.0)

    async def test_no_games_returns_empty_shape(self) -> None:
        data = await database.get_player_compatibility(999, 1)
        self.assertEqual(data["games"], 0)
        self.assertEqual(data["same"], [])
        self.assertEqual(data["opposite"], [])

    async def test_full_coplay_no_longer_collapses_diff_to_zero(self) -> None:
        """自己参照バイアスの再現ケース: 相手Aと村で100%共戦(10戦9勝)。

        修正前は baseline[村] が相手Aの分だけで出来ていたため、
        expected == rate == 0.9 で diff が 0.0 に潰れていた
        (実勝率90%なのに「差なし」と出る)。修正後は村タイプの共戦が
        Aで全部埋まっている場合、陣営を問わない leave-one-out
        (自分の狼試合だけ) にフォールバックするため、期待値が実測へ
        機械的に一致しなくなる。
        """
        village = Team.VILLAGE.value
        wolf = Team.WOLF.value
        layout = [
            (1, "2026-06-01 12:00:00", [(1, village, 1), (2, village, 1)]),
            (2, "2026-06-02 12:00:00", [(1, village, 1), (2, village, 1)]),
            (3, "2026-06-03 12:00:00", [(1, village, 1), (2, village, 1)]),
            (4, "2026-06-04 12:00:00", [(1, village, 1), (2, village, 1)]),
            (5, "2026-06-05 12:00:00", [(1, village, 1), (2, village, 1)]),
            (6, "2026-06-06 12:00:00", [(1, village, 1), (2, village, 1)]),
            (7, "2026-06-07 12:00:00", [(1, village, 1), (2, village, 1)]),
            (8, "2026-06-08 12:00:00", [(1, village, 1), (2, village, 1)]),
            (9, "2026-06-09 12:00:00", [(1, village, 1), (2, village, 1)]),
            (10, "2026-06-10 12:00:00", [(1, village, 0), (2, village, 0)]),
            # 別陣営(狼)での共戦。村の比較基準には使わない。
            (11, "2026-06-11 12:00:00", [(1, wolf, 0), (3, wolf, 1)]),
            (12, "2026-06-12 12:00:00", [(1, wolf, 0), (3, wolf, 1)]),
            (13, "2026-06-13 12:00:00", [(1, wolf, 0), (3, wolf, 1)]),
            (14, "2026-06-14 12:00:00", [(1, wolf, 0), (3, wolf, 1)]),
            (15, "2026-06-15 12:00:00", [(1, wolf, 1), (3, wolf, 1)]),
        ]
        for game_id, played_at, players in layout:
            await self._add_game(game_id, played_at=played_at, players=players)

        data = await database.get_player_compatibility(1, 1, min_games=1)
        same = {row["player_id"]: row for row in data["same"]}
        # 村の10戦が全て相手Aとの共戦なので、Aを除くと同じ陣営の比較材料が
        # 残らない。修正前は expected=0.9 で diff=0.0 に潰れていたが、
        # 陣営を跨いで狼の勝率を基準にすると相性とは無関係な差が出るため、
        # 判定できない行として出さないのが正しい。
        self.assertNotIn(2, same)
        # 狼の5戦も全て相手Cとの共戦なので、同じ理由でCも判定できない。
        # この編成では「比較材料が1つも無い」ため、どの行も出さないのが正しい。
        shown = {row["player_id"] for row in (*data["same"], *data["opposite"])}
        self.assertEqual(shown, set())
        # 判定できる相手がいる場合に diff が出ることは
        # test_leave_one_out_excludes_the_opponent_own_games 側で担保する。

    async def test_diff_magnitude_reflects_the_real_gap_not_the_share_of_games(
        self,
    ) -> None:
        """主戦相手(共戦が多い)ほど diff が過小評価されない、少数相手ほど
        誇張されない、という修正前のバイアスが直っていることを検証する。

        相手2: 40戦36勝(90%)、相手3: 10戦1勝(10%)。すべて村の同陣営。
        修正前はそれぞれ diff +0.16 / -0.64 と、主戦相手(実態90%)の方が
        弱く出て少数相手(実態10%)の方が誇張されていた。
        leave-one-outでは互いの相手を除いた分がもう一方の相手の実測其のもの
        になるため、diffの絶対値は両者で揃い、実態の差(90%対10%)を
        どちらも過小評価しない。
        """
        village = Team.VILLAGE.value
        game_id = 0
        layout: list[tuple[int, str, list[tuple[int, str, int]]]] = []

        def add(opponent_id: int, me_won: bool) -> None:
            nonlocal game_id
            game_id += 1
            layout.append(
                (
                    game_id,
                    f"2026-07-{(game_id % 28) + 1:02d} 12:00:00",
                    [
                        (1, village, int(me_won)),
                        (opponent_id, village, int(me_won)),
                    ],
                )
            )

        for _ in range(36):
            add(2, True)
        for _ in range(4):
            add(2, False)
        for _ in range(1):
            add(3, True)
        for _ in range(9):
            add(3, False)

        for gid, played_at, players in layout:
            await self._add_game(gid, played_at=played_at, players=players)

        data = await database.get_player_compatibility(1, 1, min_games=1)
        same = {row["player_id"]: row for row in data["same"]}

        self.assertAlmostEqual(same[2]["rate"], 0.9)
        self.assertAlmostEqual(same[3]["rate"], 0.1)
        # 相手を除いた分がもう一方の相手そのものになるので、期待値は
        # 「相手を除いた実測」と一致する。
        self.assertAlmostEqual(same[2]["expected"], 0.1)
        self.assertAlmostEqual(same[3]["expected"], 0.9)
        self.assertAlmostEqual(same[2]["diff"], 0.8)
        self.assertAlmostEqual(same[3]["diff"], -0.8)
        # 主戦相手(共戦が多い)の diff の絶対値が、少数相手より小さく
        # 評価されてはいけない (修正前バグの再現条件)。
        self.assertGreaterEqual(abs(same[2]["diff"]), abs(same[3]["diff"]))


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
