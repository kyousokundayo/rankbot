"""#統計 の4変種セレクトと2ラダー表示の回帰テスト。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from config import Team, USER_VISIBLE_VARIANT_IDS, VARIANT_DEFINITIONS
from views import (
    LeaderboardView,
    OverallRoomStatsSelect,
    OverallStatsFilterView,
    PlayerHistoryVariantView,
    PlayerStatsVariantView,
    RecentGamesVariantView,
    SeasonHistoryView,
    StatsVariantSelect,
    _rating_swing,
    build_rank_spec_embeds,
)


VARIANT_IDS = USER_VISIBLE_VARIANT_IDS


def _guild() -> SimpleNamespace:
    return SimpleNamespace(id=123, get_member=lambda _member_id: None)


class StatsVariantSelectTest(unittest.TestCase):
    def test_every_scoped_view_keeps_only_public_variant_options(self) -> None:
        user = SimpleNamespace(id=9, display_name="tester")
        views = (
            PlayerStatsVariantView(SimpleNamespace(), 123, user),
            RecentGamesVariantView(123),
            PlayerHistoryVariantView(123, 9),
            LeaderboardView(123, 9),
            OverallStatsFilterView(123),
            SeasonHistoryView(),
        )
        for view in views:
            with self.subTest(view=type(view).__name__):
                selector = next(
                    item for item in view.children
                    if isinstance(item, StatsVariantSelect)
                )
                self.assertEqual(
                    tuple(option.value for option in selector.options),
                    VARIANT_IDS,
                )
                self.assertEqual(selector.row, 0)

    def test_overall_room_options_follow_variant_and_reset_stale_room(self) -> None:
        view = OverallStatsFilterView(123, variant_id="v9_turn")
        room_select = next(
            item for item in view.children
            if isinstance(item, OverallRoomStatsSelect)
        )
        self.assertEqual(
            [option.value for option in room_select.options],
            ["all", "open_9_turn"],
        )

        view.room_id = "open_9_turn"
        view.set_variant("v13_cross")
        self.assertIsNone(view.room_id)
        room_select = next(
            item for item in view.children
            if isinstance(item, OverallRoomStatsSelect)
        )
        self.assertNotIn("open_9_turn", [option.value for option in room_select.options])


class StatsVariantDatabaseRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_player_stats_use_variant_but_rating_and_season_use_ladder(self) -> None:
        user = SimpleNamespace(id=9, display_name="tester")
        view = PlayerStatsVariantView(
            SimpleNamespace(), 123, user, variant_id="v9_turn",
        )
        rating_info = {
            "rank_name": "グランドマスター",
            "emoji": "👑",
            "color": 0xF39C12,
            "rating": 1900,
            "provisional": False,
            "top_percent": 1.0,
            "position": 1,
            "active_count": 30,
            "season_games": 10,
            "season_wins": 6,
            "season_winrate": 60.0,
            "peak_rating": 1950,
        }
        with (
            patch("database.get_player_stats", new=AsyncMock(return_value=None)) as stats,
            patch(
                "database.get_player_current_rank_info",
                new=AsyncMock(return_value=rating_info),
            ) as rank,
            patch(
                "database.get_player_latest_season_result",
                new=AsyncMock(return_value=None),
            ) as season,
        ):
            embed = await view.load_embed(_guild())

        stats.assert_awaited_once_with(9, 123, variant_id="v9_turn")
        rank.assert_awaited_once_with(9, 123, ladder_id="l9")
        season.assert_awaited_once_with(9, 123, ladder_id="l9")
        rendered = str(embed.to_dict())
        self.assertIn("この変種はまだプレイしていません", rendered)
        self.assertIn("9人村ラダーで共通", rendered)
        self.assertIn("グランドマスター（9人村）", rendered)

    async def test_rating_leaderboard_uses_ladder_and_other_metrics_use_variant(self) -> None:
        view = LeaderboardView(123, 9, variant_id="v13_turn")
        with patch(
            "database.get_current_season_leaderboard",
            new=AsyncMock(return_value=[]),
        ) as rating_board:
            embed = await view.load_embed(_guild())
        rating_board.assert_awaited_once_with(123, limit=20, ladder_id="l13")
        self.assertIn("13人村ラダーで共通", str(embed.to_dict()))

        view.metric = "village_day1_executed"
        board = {
            "metric": view.metric,
            "label": "村で初日に吊られた率",
            "unit": "percent",
            "note": "note",
            "variant_id": "v13_turn",
            "role": None,
            "top": [],
            "ranked_count": 0,
            "min_samples": 5,
            "viewer": None,
            "viewer_position": None,
        }
        with patch(
            "database.get_metric_leaderboard",
            new=AsyncMock(return_value=board),
        ) as metric_board:
            metric_embed = await view.load_embed(_guild())
        metric_board.assert_awaited_once_with(
            123,
            "village_day1_executed",
            role=None,
            viewer_id=9,
            variant_id="v13_turn",
        )
        self.assertNotIn("（人狼）", metric_embed.title)

    async def test_overall_stats_pass_variant_to_both_axes(self) -> None:
        view = OverallStatsFilterView(123, variant_id="v9_cross")
        with (
            patch(
                "database.get_overall_game_stats",
                new=AsyncMock(return_value={"games": 0}),
            ) as games,
            patch(
                "database.get_rank_player_stats",
                new=AsyncMock(return_value={}),
            ) as ranks,
            patch(
                "views.build_overall_stats_embed",
                return_value=discord.Embed(title="全体データ"),
            ),
        ):
            await view.load_embed(_guild())
        games.assert_awaited_once_with(123, room_id=None, variant_id="v9_cross")
        ranks.assert_awaited_once_with(123, rank_name=None, variant_id="v9_cross")

    async def test_season_views_route_both_modes_to_the_selected_ladder(self) -> None:
        view = SeasonHistoryView(variant_id="v9_cross")
        with patch(
            "database.get_latest_season_results",
            new=AsyncMock(return_value=(0, [])),
        ) as previous:
            previous_embed = await view.load_embed(_guild())
        previous.assert_awaited_once_with(123, limit=20, ladder_id="l9")
        self.assertIn("9人村ラダーで共通", str(previous_embed.to_dict()))

        view.mode = "grandmasters"
        with patch(
            "database.get_grandmaster_history",
            new=AsyncMock(return_value=[]),
        ) as history:
            history_embed = await view.load_embed(_guild())
        history.assert_awaited_once_with(123, ladder_id="l9")
        self.assertIn("グランドマスター（9人村）", str(history_embed.to_dict()))

    async def test_recent_and_personal_history_keep_selector_when_empty(self) -> None:
        recent = RecentGamesVariantView(123, variant_id="v9_turn")
        personal = PlayerHistoryVariantView(123, 9, variant_id="v9_turn")
        with (
            patch("database.get_recent_games", new=AsyncMock(return_value=[])) as recent_db,
            patch(
                "database.get_player_recent_games",
                new=AsyncMock(return_value=[]),
            ) as personal_db,
        ):
            recent_embed = await recent.load_embed(_guild())
            personal_embed = await personal.load_embed(_guild())
        recent_db.assert_awaited_once_with(123, limit=10, variant_id="v9_turn")
        personal_db.assert_awaited_once_with(9, 123, limit=10, variant_id="v9_turn")
        self.assertIn("まだありません", str(recent_embed.to_dict()))
        self.assertIn("まだプレイしていません", str(personal_embed.to_dict()))
        self.assertNotIn("**", recent_embed.footer.text)
        self.assertNotIn("**", personal_embed.footer.text)
        self.assertIn("9人村ラダーで共通", recent_embed.footer.text)
        self.assertIn("9人村ラダーで共通", personal_embed.footer.text)
        self.assertTrue(any(isinstance(item, StatsVariantSelect) for item in recent.children))
        self.assertTrue(any(isinstance(item, StatsVariantSelect) for item in personal.children))


class RankSpecVariantTest(unittest.TestCase):
    def test_rating_swing_uses_each_variant_team_size_and_pool(self) -> None:
        self.assertEqual(
            _rating_swing(VARIANT_DEFINITIONS["v13_cross"], Team.VILLAGE),
            ("+21", "-45"),
        )
        self.assertEqual(
            _rating_swing(VARIANT_DEFINITIONS["v9_cross"], Team.VILLAGE),
            ("+16", "-30"),
        )
        self.assertEqual(
            _rating_swing(VARIANT_DEFINITIONS["v13_turn"], Team.WOLF),
            ("+31", "-13〜-14"),
        )
        self.assertEqual(
            _rating_swing(VARIANT_DEFINITIONS["v9_turn"], Team.WOLF),
            ("+37〜+38", "-18〜-19"),
        )

    def test_rank_spec_lists_public_variant_values_and_both_ladders(self) -> None:
        rendered = str([embed.to_dict() for embed in build_rank_spec_embeds()])
        for variant_id in USER_VISIBLE_VARIANT_IDS:
            variant = VARIANT_DEFINITIONS[variant_id]
            with self.subTest(variant=variant.variant_id):
                self.assertIn(variant.label, rendered)
                self.assertIn(f"村勝ちプール **{variant.village_win_pool}**", rendered)
                self.assertIn(f"狼勝ちプール **{variant.wolf_win_pool}**", rendered)
                self.assertIn(f"人狼予想 **{variant.wolf_guess_slots}人**", rendered)
                self.assertIn(
                    f"終盤ボーナス **{variant.final_day_threshold}回目**",
                    rendered,
                )
        self.assertNotIn(VARIANT_DEFINITIONS["v13_turn"].label, rendered)
        self.assertIn("13人村ラダー", rendered)
        self.assertIn("9人村ラダー", rendered)
        self.assertIn("グランドマスター（9人村）", rendered)


if __name__ == "__main__":
    unittest.main()
