"""rating.py の単体テスト

実行: .venv/bin/python -m unittest discover -s tests -v
(scripts/run_checks.sh と CI からも実行される)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rating as rating_lib
import database
from config import (
    GRANDMASTER_PERCENTAGE,
    GRANDMASTER_SLOTS,
    INITIAL_RATING,
    RANK_SPECS,
    RATING_FLOOR,
    ROOM_DEFINITION_MAP,
    SEASON_RANK_MIN_GAMES,
    SEASON_RANK_PERCENTAGES,
    VILLAGE_WIN_FIXED_POOL,
    WIN_PARTICIPATION_BONUS,
    WOLF_WIN_FIXED_POOL,
)


def make_players(
    winner_count: int,
    loser_count: int,
    rating: int = INITIAL_RATING,
) -> list[dict]:
    players = []
    for i in range(winner_count):
        players.append({"player_id": 100 + i, "rating": rating, "won": True})
    for i in range(loser_count):
        players.append({"player_id": 200 + i, "rating": rating, "won": False})
    return players


class TestCalculateGameResults(unittest.TestCase):
    def test_adopted_rating_constants(self):
        self.assertEqual(INITIAL_RATING, 1500)
        self.assertEqual(RATING_FLOOR, 1000)
        self.assertEqual(WIN_PARTICIPATION_BONUS, 1)

    def test_wolf_win_pool_is_zero_sum_except_bonus(self):
        """狼勝ち (勝者4/敗者9): elo_deltaはゼロサム、deltaはボーナス分だけ正"""
        results = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        elo_sum = sum(r["elo_delta"] for r in results)
        delta_sum = sum(r["delta"] for r in results)
        self.assertEqual(elo_sum, 0)
        self.assertEqual(delta_sum, 4 * WIN_PARTICIPATION_BONUS)

    def test_village_win_pool_is_zero_sum_except_bonus(self):
        """村勝ち (勝者9/敗者4): elo_deltaはゼロサム、deltaはボーナス分だけ正"""
        results = rating_lib.calculate_game_results(
            make_players(9, 4), winner_team="村陣営"
        )
        elo_sum = sum(r["elo_delta"] for r in results)
        delta_sum = sum(r["delta"] for r in results)
        self.assertEqual(elo_sum, 0)
        self.assertEqual(delta_sum, 9 * WIN_PARTICIPATION_BONUS)

    def test_pool_selection_by_winner_count(self):
        """勝者が少数側 (狼勝ち) なら60プール、多数側 (村勝ち) なら90プール"""
        wolf_win = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        winners_gain = sum(r["elo_delta"] for r in wolf_win if r["elo_delta"] > 0)
        self.assertEqual(winners_gain, WOLF_WIN_FIXED_POOL)

        village_win = rating_lib.calculate_game_results(
            make_players(9, 4), winner_team="村陣営"
        )
        winners_gain = sum(r["elo_delta"] for r in village_win if r["elo_delta"] > 0)
        self.assertEqual(winners_gain, VILLAGE_WIN_FIXED_POOL)

    def test_bonus_only_for_winners(self):
        results = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        for r in results:
            if r["player_id"] >= 200:  # 敗者
                self.assertEqual(r["bonus"], 0)
                self.assertLess(r["delta"], 0)
            else:
                self.assertEqual(r["bonus"], WIN_PARTICIPATION_BONUS)
                self.assertGreater(r["delta"], 0)

    def test_rating_after_consistency(self):
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=1500), winner_team="村陣営"
        )
        for r in results:
            self.assertEqual(r["rating_after"], r["rating_before"] + r["delta"])

    def test_remainder_split_is_deterministic_and_fair(self):
        """端数の分配は決定的で、各人の差は最大1"""
        players = make_players(9, 4)
        first = rating_lib.calculate_game_results(players, winner_team="村陣営")
        second = rating_lib.calculate_game_results(players, winner_team="村陣営")
        self.assertEqual(first, second)

        winner_deltas = [r["elo_delta"] for r in first if r["player_id"] < 200]
        self.assertLessEqual(max(winner_deltas) - min(winner_deltas), 1)
        loser_deltas = [r["elo_delta"] for r in first if r["player_id"] >= 200]
        self.assertLessEqual(max(loser_deltas) - min(loser_deltas), 1)

    def test_floor_clamps_losses(self):
        """フロア上の敗者はそれ以上下がらない (delta=0)、勝者は通常どおり上がる"""
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=RATING_FLOOR), winner_team="村陣営"
        )
        for r in results:
            self.assertGreaterEqual(r["rating_after"], RATING_FLOOR)
            if r["player_id"] >= 200:  # 敗者
                self.assertEqual(r["rating_after"], RATING_FLOOR)
                self.assertEqual(r["delta"], 0)
            else:
                self.assertGreater(r["delta"], 0)

    def test_floor_partial_clamp(self):
        """フロア直上の敗者は損失がフロアまでで打ち止めになる"""
        start = RATING_FLOOR + 5
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=start), winner_team="村陣営"
        )
        for r in results:
            if r["player_id"] >= 200:  # 村勝ち時の狼側敗者: 名目-22前後 → 実際は-5
                self.assertEqual(r["rating_after"], RATING_FLOOR)
                self.assertEqual(r["delta"], RATING_FLOOR - start)
                self.assertLess(r["elo_delta"], r["delta"])  # 名目値は実適用より大きい損失

    def test_below_floor_is_frozen_not_lifted(self):
        """フロア未満の既存レートは敗北で引き上げられず据え置き、勝利では通常どおり上がる"""
        start = RATING_FLOOR - 100
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=start), winner_team="村陣営"
        )
        for r in results:
            if r["player_id"] >= 200:  # 敗者: 据え置き
                self.assertEqual(r["rating_after"], start)
                self.assertEqual(r["delta"], 0)
            else:  # 勝者: 通常どおり上昇
                self.assertGreater(r["delta"], 0)

    def test_no_clamp_above_floor(self):
        """フロアから十分離れていればゼロサムが保たれる (既存テストの前提確認)"""
        results = rating_lib.calculate_game_results(
            make_players(4, 9, rating=INITIAL_RATING), winner_team="狼陣営"
        )
        self.assertEqual(sum(r["elo_delta"] for r in results), 0)
        self.assertEqual(
            sum(r["delta"] for r in results), 4 * WIN_PARTICIPATION_BONUS
        )

    def test_all_winners_or_all_losers_is_noop(self):
        """勝者か敗者が空 (廃村相当の防御) は全員変動なし"""
        for results in (
            rating_lib.calculate_game_results(
                make_players(13, 0), winner_team="村陣営"
            ),
            rating_lib.calculate_game_results(
                make_players(0, 13), winner_team="狼陣営"
            ),
        ):
            for r in results:
                self.assertEqual(r["delta"], 0)
                self.assertEqual(r["rating_after"], r["rating_before"])


def make_rows(count: int, games: int = SEASON_RANK_MIN_GAMES) -> list[dict]:
    # レートは player_id が小さいほど高い (順位が一意に決まる)
    # ランクの母集団は通算 (games) で決まる
    return [
        {
            "player_id": i + 1,
            "rating": 2000 - i,
            "games": games,
            "season_games": games,
            "season_wins": games // 2,
        }
        for i in range(count)
    ]


class TestBuildRankContextMap(unittest.TestCase):
    def test_under_threshold_is_provisional_bronze(self):
        rows = make_rows(10, games=SEASON_RANK_MIN_GAMES - 1)
        ctx = rating_lib.build_rank_context_map(rows)
        for c in ctx.values():
            self.assertTrue(c.provisional)
            self.assertEqual(c.rank_name, "ブロンズ")
            self.assertIsNone(c.position)

    def test_rank_survives_a_season_reset(self):
        """ハーフリセットで今季戦績が0になってもランクと順位は維持される。

        レート変換は単調増加なので順位関係が保たれる。母集団を通算で
        数えることで、リセット直後に全員が暫定ブロンズへ落ちない。
        """
        rows = make_rows(100)
        before = rating_lib.build_rank_context_map(rows)

        # season_half_reset と同じ変換: レート半減 + 今季戦績ゼロ (通算は残す)
        after_rows = [
            {
                **row,
                "rating": INITIAL_RATING + (row["rating"] - INITIAL_RATING) // 2,
                "season_games": 0,
                "season_wins": 0,
            }
            for row in rows
        ]
        after = rating_lib.build_rank_context_map(after_rows)

        for player_id, ctx in before.items():
            self.assertEqual(ctx.rank_name, after[player_id].rank_name, player_id)
            self.assertEqual(ctx.position, after[player_id].position, player_id)
            self.assertFalse(after[player_id].provisional, player_id)

    def test_brand_new_player_is_still_provisional_bronze(self):
        """通算0戦だけが暫定ブロンズ (初心者卓へ入れる状態にしておく)。"""
        rows = make_rows(50)
        rows.append({
            "player_id": 999,
            "rating": INITIAL_RATING,
            "games": 0,
            "season_games": 0,
            "season_wins": 0,
        })

        ctx = rating_lib.build_rank_context_map(rows)

        self.assertTrue(ctx[999].provisional)
        self.assertEqual(ctx[999].rank_name, "ブロンズ")
        self.assertIsNone(ctx[999].position)

    def test_everyone_gets_context(self):
        rows = make_rows(100)
        ctx = rating_lib.build_rank_context_map(rows)
        self.assertEqual(len(ctx), 100)
        self.assertTrue(all(not c.provisional for c in ctx.values()))

    def test_grandmaster_slots(self):
        """アクティブ200人 → 上位5%=10人がGM、残り10人がマスター"""
        rows = make_rows(200)
        ctx = rating_lib.build_rank_context_map(rows)
        gm = [c for c in ctx.values() if c.rank_name == "グランドマスター"]
        master = [c for c in ctx.values() if c.rank_name == "マスター"]
        expected_gm = int(200 * GRANDMASTER_PERCENTAGE)
        self.assertEqual(len(gm), expected_gm)
        self.assertEqual(len(master), 20 - expected_gm)
        self.assertEqual(
            sorted(c.position for c in gm), list(range(1, expected_gm + 1))
        )

    def test_grandmaster_count_is_capped_at_thirteen(self):
        rows = make_rows(400)
        ctx = rating_lib.build_rank_context_map(rows)
        gm = [c for c in ctx.values() if c.rank_name == "グランドマスター"]
        master = [c for c in ctx.values() if c.rank_name == "マスター"]
        self.assertEqual(len(gm), GRANDMASTER_SLOTS)
        self.assertEqual(len(master), 40 - GRANDMASTER_SLOTS)

    def test_small_population_gm_capped_by_master_zone(self):
        """少人数でも上位5%相当だけをGMにし、マスターを残す"""
        rows = make_rows(30)  # マスター帯 = 10% = 3人
        ctx = rating_lib.build_rank_context_map(rows)
        gm = [c for c in ctx.values() if c.rank_name == "グランドマスター"]
        self.assertEqual(len(gm), 2)
        master = [c for c in ctx.values() if c.rank_name == "マスター"]
        self.assertEqual(len(master), 1)

    def test_emerald_rank_is_part_of_production_distribution(self):
        rows = make_rows(100)
        ctx = rating_lib.build_rank_context_map(rows)
        emerald = [c for c in ctx.values() if c.rank_name == "エメラルド"]
        self.assertEqual(len(emerald), 10)
        # 絵文字・色は運用で変わりうるので値は固定しない。
        # 「RANK_SPECS に1件だけ在り、絵文字と色が空でない」ことだけ保証する
        specs = [spec for spec in RANK_SPECS if spec[0] == "エメラルド"]
        self.assertEqual(len(specs), 1)
        _, emoji, color_hex = specs[0]
        self.assertTrue(emoji)
        self.assertTrue(color_hex.startswith("#"))
        self.assertIn(
            "エメラルド", ROOM_DEFINITION_MAP["intermediate"].allowed_ranks
        )

    def test_rank_distribution_sums_to_population(self):
        rows = make_rows(137)
        ctx = rating_lib.build_rank_context_map(rows)
        counts: dict[str, int] = {}
        for c in ctx.values():
            counts[c.rank_name] = counts.get(c.rank_name, 0) + 1
        self.assertEqual(sum(counts.values()), 137)
        # 配分比率の合計が1なので全員に行き渡る (取り残しゼロ)
        self.assertAlmostEqual(sum(SEASON_RANK_PERCENTAGES.values()), 1.0)

    def test_ordering_by_rating(self):
        """順位はレート降順 (同点はseason_wins, player_idで安定)"""
        rows = make_rows(50)
        ctx = rating_lib.build_rank_context_map(rows)
        by_position = sorted(ctx.values(), key=lambda c: c.position)
        ratings = [rows[c.player_id - 1]["rating"] for c in by_position]
        self.assertEqual(ratings, sorted(ratings, reverse=True))


class TestSeasonHalfResetProduction(unittest.IsolatedAsyncioTestCase):
    """式の写しではなく、本番DB処理を実行して検証する。"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-rating-test-")
        self.original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name) / "rating.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_pulls_actual_database_rows_toward_initial(self):
        guild_id = 1234
        rows = (
            (1, 1500, 1500, 10, 5),
            (2, 1901, 2000, 20, 12),
            (3, 1099, 1500, 7, 2),
        )
        async with database.connect_db() as db:
            await db.executemany(
                "INSERT INTO player_ratings "
                "(player_id, guild_id, rating, peak_rating, season_games, season_wins) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(pid, guild_id, rating, peak, games, wins)
                 for pid, rating, peak, games, wins in rows],
            )
            await db.commit()

        reset_id, affected = await database.season_half_reset(
            guild_id, executed_by=999, note="unit-test"
        )
        self.assertGreater(reset_id, 0)
        self.assertEqual(affected, 3)

        async with database.connect_db() as db:
            actual = await db.execute_fetchall(
                "SELECT player_id, rating, peak_rating, season_games, season_wins "
                "FROM player_ratings WHERE guild_id = ? ORDER BY player_id",
                (guild_id,),
            )
            snapshots = await db.execute_fetchall(
                "SELECT player_id, rating_before, rating_after FROM rating_snapshots "
                "WHERE season_reset_id = ? ORDER BY player_id",
                (reset_id,),
            )

        self.assertEqual(
            actual,
            [
                (1, 1500, 1500, 0, 0),
                (2, 1700, 2000, 0, 0),
                (3, 1299, 1500, 0, 0),
            ],
        )
        self.assertEqual(
            snapshots,
            [(1, 1500, 1500), (2, 1901, 1700), (3, 1099, 1299)],
        )

    async def test_no_players_is_noop(self):
        self.assertEqual(
            await database.season_half_reset(9876, executed_by=999),
            (0, 0),
        )


class TestRatingScaleMigration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-rating-migration-")
        self.original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name) / "migration.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_existing_values_shift_once_without_changing_deltas(self):
        async with database.connect_db() as db:
            game_cursor = await db.execute(
                "INSERT INTO games (guild_id, winner_team) VALUES (1, '村人陣営')"
            )
            game_id = int(game_cursor.lastrowid)
            reset_cursor = await db.execute(
                "INSERT INTO season_resets (guild_id, executed_by) VALUES (1, 999)"
            )
            reset_id = int(reset_cursor.lastrowid)
            await db.execute(
                "INSERT INTO player_ratings "
                "(player_id, guild_id, rating, peak_rating) VALUES (10, 1, 1200, 1600)"
            )
            await db.execute(
                "INSERT INTO rating_history "
                "(player_id, guild_id, game_id, rating_before, rating_after, elo_delta, bonus) "
                "VALUES (10, 1, ?, 1200, 1213, 10, 3)",
                (game_id,),
            )
            await db.execute(
                "INSERT INTO rating_snapshots "
                "(season_reset_id, player_id, guild_id, rating_before, rating_after) "
                "VALUES (?, 10, 1, 1600, 1400)",
                (reset_id,),
            )
            await db.commit()

        self.assertTrue(await database.rating_scale_migration_needed())
        affected = await database.migrate_rating_scale_to_1500()
        self.assertEqual(affected, 1)
        self.assertFalse(await database.rating_scale_migration_needed())

        async with database.connect_db() as db:
            current = await db.execute_fetchall(
                "SELECT rating, peak_rating FROM player_ratings WHERE player_id = 10"
            )
            history = await db.execute_fetchall(
                "SELECT rating_before, rating_after, elo_delta, bonus "
                "FROM rating_history WHERE player_id = 10"
            )
            snapshot = await db.execute_fetchall(
                "SELECT rating_before, rating_after FROM rating_snapshots "
                "WHERE player_id = 10"
            )

        self.assertEqual(current, [(1500, 1900)])
        self.assertEqual(history, [(1500, 1513, 10, 3)])
        self.assertEqual(snapshot, [(1900, 1700)])
        self.assertEqual(await database.migrate_rating_scale_to_1500(), 0)


if __name__ == "__main__":
    unittest.main()
