"""v0.51: 運営ダッシュボード (稼働・定着・離脱・回転・通知・レート) の回帰テスト。

時刻依存の指標なので now を注入して固定する。JSTの日付境界を跨がないよう、
seed する played_at は UTC 03:00 (= JST 12:00) に揃えてある。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

import database
from config import Role, Team
from recruitment import (
    OperationsDashboardView,
    _build_ops_activity_embed,
    _build_ops_churn_embed,
    _build_ops_delivery_embed,
    _build_ops_rating_embed,
    _build_ops_retention_embed,
    _build_ops_throughput_embed,
)

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
GUILD_ID = 1


class OpsDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-ops-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "ops.db")
        await database.init_db()
        await self._seed_games()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    async def _seed_games(self) -> None:
        # (game_id, played_at, started_at, [player_id...])
        layout = [
            (1, "2026-08-20 03:00:00", "2026-08-20 02:00:00", [101, 103, 104]),
            (2, "2026-08-17 03:00:00", "2026-08-17 02:20:00", [101]),
            (3, "2026-07-31 03:00:00", None, [104]),
            (4, "2026-07-11 03:00:00", None, [102]),
            (5, "2026-07-16 03:00:00", None, [102]),
        ]
        async with aiosqlite.connect(database.DB_PATH) as db:
            for game_id, played_at, started_at, players in layout:
                await db.execute(
                    "INSERT INTO games (game_id, guild_id, variant_id, ladder_id, "
                    "room_id, room_name, game_run_id, gm_id, winner_team, played_at, "
                    "started_at) VALUES (?, ?, 'v13_cross', 'l13', 'beginner', "
                    "'初心者村', ?, 900, ?, ?, ?)",
                    (
                        game_id, GUILD_ID, f"run-{game_id}", Team.VILLAGE.value,
                        played_at, started_at,
                    ),
                )
                for player_id in players:
                    await db.execute(
                        "INSERT INTO game_players "
                        "(game_id, player_id, role, team, won, death_cause) "
                        "VALUES (?, ?, ?, ?, 1, ?)",
                        (
                            game_id, player_id, Role.VILLAGER.value,
                            Team.VILLAGE.value,
                            "除外" if (game_id, player_id) == (1, 103) else None,
                        ),
                    )
            await db.commit()

    # --------------------------------------------------------
    # 稼働・定着・離脱
    # --------------------------------------------------------
    async def test_activity_counts_are_jst_based(self) -> None:
        stats = await database.get_ops_activity_stats(GUILD_ID, now=NOW)

        self.assertEqual(stats["today"], "2026-08-20")
        self.assertEqual(stats["dau"], 3)      # 101/103/104
        self.assertEqual(stats["wau"], 3)
        self.assertEqual(stats["mau"], 3)      # 102は35日前が最後
        self.assertEqual(stats["total_players"], 4)
        self.assertEqual(stats["games_today"], 1)
        self.assertEqual(stats["games_7d"], 2)
        self.assertEqual(stats["games_30d"], 3)
        self.assertAlmostEqual(stats["stickiness"], 1.0)
        self.assertEqual(len(stats["daily"]), 14)
        self.assertEqual(stats["daily"][-1]["date"], "2026-08-20")
        self.assertEqual(stats["daily"][-1]["games"], 1)

    async def test_new_players_and_cohorts(self) -> None:
        stats = await database.get_ops_activity_stats(GUILD_ID, now=NOW)

        self.assertEqual(stats["new_today"], 1)   # 103 だけが本日初参加
        self.assertEqual(stats["new_7d"], 2)      # + 101 (8/17が初参加)
        self.assertEqual(stats["new_30d"], 3)     # + 104 (7/31が初参加)
        # 初参加から30日以上経ったのは102だけ。2戦目に到達している。
        self.assertEqual(stats["second_game_sample"], 1)
        self.assertAlmostEqual(stats["second_game_rate"], 1.0)
        cohorts = {row["week"]: row for row in stats["cohorts"]}
        self.assertEqual(len(cohorts), 1)
        cohort = next(iter(cohorts.values()))
        self.assertEqual((cohort["size"], cohort["w1"], cohort["w4"]), (1, 1, 0))

    async def test_dormancy_gaps_and_returning(self) -> None:
        stats = await database.get_ops_activity_stats(GUILD_ID, now=NOW)

        self.assertEqual(stats["dormant_14"], 1)   # 102 のみ
        self.assertEqual(stats["dormant_30"], 1)
        self.assertEqual(stats["dormant_60"], 0)
        # 104 は20日空けて本日復帰。101 は3日前なので復帰に数えない。
        self.assertEqual(stats["returning_7d"], 1)
        self.assertEqual(stats["last_play_buckets"]["0-1日"], 3)
        self.assertEqual(stats["last_play_buckets"]["31-60日"], 1)
        self.assertAlmostEqual(stats["gap_median"], 5.0)  # 3日/5日/20日
        self.assertEqual(stats["longest_streaks"], [])    # 連続プレイなし

    # --------------------------------------------------------
    # 回転
    # --------------------------------------------------------
    async def _seed_recruitments(self) -> None:
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO recruitments (id, guild_id, host_id, title, scheduled_at, "
                "room_id, status, capacity, created_at, ready_notified_at) "
                "VALUES (1, ?, 900, '募集A', '2026-08-19T21:00:00+09:00', 'beginner', "
                "?, 13, '2026-08-19 03:00:00', '2026-08-19 03:30:00')",
                (GUILD_ID, database.RECRUITMENT_HELD),
            )
            await db.execute(
                "INSERT INTO recruitments (id, guild_id, host_id, title, scheduled_at, "
                "room_id, status, capacity, created_at) "
                "VALUES (2, ?, 900, '募集B', '2026-08-21T21:00:00+09:00', 'beginner', "
                "?, 13, '2026-08-18 03:00:00')",
                (GUILD_ID, database.RECRUITMENT_OPEN),
            )
            for user_id in range(1, 14):
                await db.execute(
                    "INSERT INTO recruitment_entries (recruitment_id, user_id, kind) "
                    "VALUES (1, ?, '参加')", (user_id,),
                )
            await db.commit()

    async def test_throughput_covers_recruitments_and_games(self) -> None:
        await self._seed_recruitments()
        stats = await database.get_ops_throughput_stats(GUILD_ID, days=30, now=NOW)

        self.assertEqual(stats["recruitments"], 2)
        self.assertAlmostEqual(stats["held_rate"], 0.5)
        self.assertAlmostEqual(stats["ready_wait_median_min"], 30.0)
        self.assertEqual(stats["games"], 3)  # 直近30日は g1/g2/g3
        self.assertEqual(stats["duration_sample"], 2)
        self.assertAlmostEqual(stats["duration_median_min"], 50.0)  # 60分と40分
        self.assertEqual(stats["dropouts"], 1)
        self.assertEqual(stats["seats"], 5)
        self.assertEqual(stats["gm_top"][0]["player_id"], 900)

    # --------------------------------------------------------
    # 通知
    # --------------------------------------------------------
    async def test_delivery_failure_rate_and_opt_out(self) -> None:
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO recruitments (id, guild_id, host_id, title, scheduled_at, "
                "room_id, created_at) VALUES (1, ?, 900, '募集A', "
                "'2026-08-19T21:00:00+09:00', 'beginner', '2026-08-19 03:00:00')",
                (GUILD_ID,),
            )
            await db.execute(
                "INSERT INTO recruitment_calls (id, recruitment_id, guild_id, host_id, "
                "called_on, recipients) VALUES (1, 1, ?, 900, '2026-08-19', 4)",
                (GUILD_ID,),
            )
            for user_id, status in ((1, "sent"), (2, "sent"), (3, "failed"), (4, "blocked")):
                await db.execute(
                    "INSERT INTO recruitment_call_deliveries "
                    "(call_id, user_id, notified_at, delivery_status) "
                    "VALUES (1, ?, '2026-08-19 03:05:00', ?)",
                    (user_id, status),
                )
            await db.execute(
                "INSERT INTO user_notification_prefs "
                "(guild_id, user_id, allow_notifications, notify_on_create, notify_on_call) "
                "VALUES (?, 1, 0, 0, 0)", (GUILD_ID,),
            )
            await db.execute(
                "INSERT INTO user_notification_prefs "
                "(guild_id, user_id, allow_notifications, notify_on_create, notify_on_call) "
                "VALUES (?, 2, 1, 1, 1)", (GUILD_ID,),
            )
            await db.commit()

        stats = await database.get_ops_delivery_stats(GUILD_ID, days=30, now=NOW)
        self.assertEqual(stats["call_dm"]["total"], 4)
        self.assertAlmostEqual(stats["call_dm"]["failure_rate"], 0.5)
        self.assertEqual(stats["prefs_configured"], 2)
        self.assertEqual(stats["prefs_opted_out"], 1)
        self.assertAlmostEqual(stats["opt_out_rate"], 0.5)

    # --------------------------------------------------------
    # レート健全性
    # --------------------------------------------------------
    async def test_rating_health_histogram_and_monthly(self) -> None:
        async with aiosqlite.connect(database.DB_PATH) as db:
            for player_id, rating, season_games in (
                (101, 1180, 9), (102, 1240, 2), (103, 1305, 30),
            ):
                await db.execute(
                    "INSERT INTO player_ratings (player_id, guild_id, ladder_id, "
                    "rating, peak_rating, games, wins, season_games, season_wins) "
                    "VALUES (?, ?, 'l13', ?, ?, 10, 5, ?, 3)",
                    (player_id, GUILD_ID, rating, rating + 20, season_games),
                )
            await db.execute(
                "INSERT INTO rating_history (player_id, guild_id, game_id, variant_id, "
                "ladder_id, rating_before, rating_after, elo_delta) "
                "VALUES (101, ?, 1, 'v13_cross', 'l13', 1170, 1180, 10)",
                (GUILD_ID,),
            )
            await db.commit()

        stats = await database.get_ops_rating_health(GUILD_ID)
        self.assertEqual(stats["players"], 3)
        self.assertEqual(stats["min"], 1180)
        self.assertEqual(stats["max"], 1305)
        self.assertEqual(stats["provisional"], 1)  # season_games 2 < 5
        floors = {row["floor"]: row["count"] for row in stats["histogram"]}
        self.assertEqual(floors[1100], 1)
        self.assertEqual(floors[1200], 1)
        self.assertEqual(floors[1300], 1)
        self.assertEqual(stats["monthly"][-1]["month"], "2026-08")

    async def test_rating_health_monthly_uses_jst_month_not_utc_month(self) -> None:
        """月別集計はJST基準で切ること (他の運営指標と同じ基準)。

        games.played_at はSQLiteのCURRENT_TIMESTAMP=tz無しUTCで保存される。
        ここでは UTC 16:00〜23:59台の played_at (= JST では翌日・翌月) を
        入れて、月がUTCのまま "2026-07" にならず JST の "2026-08" に
        集計されることを確認する。既存の test_rating_health_histogram_and_monthly
        は docstring の通りUTC 03:00 (=JST日付は同日) に揃えて月境界を
        踏まないようにしてあるため、ここでは意図的に境界を跨ぐ時刻を使う。
        """
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO player_ratings (player_id, guild_id, ladder_id, "
                "rating, peak_rating, games, wins, season_games, season_wins) "
                "VALUES (101, ?, 'l13', 1180, 1200, 10, 5, 9, 3)",
                (GUILD_ID,),
            )
            # UTC 2026-07-31 16:00:00 = JST 2026-08-01 01:00:00 (月をまたぐ)
            await db.execute(
                "INSERT INTO games (game_id, guild_id, variant_id, ladder_id, "
                "room_id, room_name, game_run_id, gm_id, winner_team, played_at) "
                "VALUES (6, ?, 'v13_cross', 'l13', 'beginner', '初心者村', "
                "'run-6', 900, ?, '2026-07-31 16:00:00')",
                (GUILD_ID, Team.VILLAGE.value),
            )
            await db.execute(
                "INSERT INTO rating_history (player_id, guild_id, game_id, variant_id, "
                "ladder_id, rating_before, rating_after, elo_delta) "
                "VALUES (101, ?, 6, 'v13_cross', 'l13', 1170, 1180, 10)",
                (GUILD_ID,),
            )
            await db.commit()

        stats = await database.get_ops_rating_health(GUILD_ID)
        months = {row["month"]: row["settlements"] for row in stats["monthly"]}
        self.assertNotIn("2026-07", months)  # UTC基準ならここに紛れ込む
        self.assertEqual(months.get("2026-08"), 1)

    # --------------------------------------------------------
    # 表示
    # --------------------------------------------------------
    async def test_every_panel_builds_an_embed(self) -> None:
        await self._seed_recruitments()
        guild = None
        activity = await database.get_ops_activity_stats(GUILD_ID, now=NOW)
        throughput = await database.get_ops_throughput_stats(GUILD_ID, now=NOW)
        delivery = await database.get_ops_delivery_stats(GUILD_ID, now=NOW)
        rating = await database.get_ops_rating_health(GUILD_ID)

        embeds = [
            _build_ops_activity_embed(activity),
            _build_ops_retention_embed(activity),
            _build_ops_churn_embed(activity),
            _build_ops_throughput_embed(throughput, guild),
            _build_ops_delivery_embed(delivery),
            _build_ops_rating_embed(rating),
        ]
        for embed in embeds:
            self.assertTrue(embed.title)
            for field in embed.fields:
                # Discordの1フィールド上限。集計が伸びても切れないこと。
                self.assertLessEqual(len(field.value), 1024)

    async def test_dashboard_view_switches_panels(self) -> None:
        view = OperationsDashboardView(GUILD_ID)
        self.assertEqual(len(view.children), 1)
        for key, _label, _description in getattr(
            __import__("recruitment"), "_OPS_DASHBOARD_PANELS",
        ):
            view.panel = key
            embed = await view.load_embed(None)
            self.assertTrue(embed.title)


if __name__ == "__main__":
    unittest.main()
