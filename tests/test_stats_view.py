"""StatsViewのinteraction応答期限に関する回帰テスト。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from views import StatsView, build_overall_stats_embed


class StatsViewInteractionTest(unittest.IsolatedAsyncioTestCase):
    def _interaction(self):
        events: list[str] = []

        async def defer(**kwargs):
            events.append("defer")

        async def followup_send(*args, **kwargs):
            events.append("followup")

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=123, get_member=lambda _member_id: None),
            user=SimpleNamespace(id=456, display_name="tester"),
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=defer),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock(side_effect=followup_send)),
        )
        return interaction, events

    @staticmethod
    def _item(view: StatsView, custom_id: str):
        return next(item for item in view.children if item.custom_id == custom_id)

    async def test_every_database_button_defers_before_query_and_uses_ephemeral_followup(self):
        cases = (
            ("stats_self", "database.get_player_stats", None),
            ("stats_all", "database.get_current_season_leaderboard", []),
            ("stats_previous", "database.get_latest_season_results", (0, [])),
            ("stats_recent_games", "database.get_recent_games", []),
            ("stats_my_history", "database.get_player_recent_games", []),
        )

        for custom_id, target, result in cases:
            with self.subTest(custom_id=custom_id):
                interaction, events = self._interaction()

                async def query(*args, **kwargs):
                    self.assertEqual(events, ["defer"])
                    events.append("query")
                    return result

                view = StatsView(SimpleNamespace())
                with patch(target, new=AsyncMock(side_effect=query)) as db_query:
                    await self._item(view, custom_id).callback(interaction)

                interaction.response.defer.assert_awaited_once_with(
                    ephemeral=True, thinking=True
                )
                db_query.assert_awaited_once()
                interaction.followup.send.assert_awaited_once()
                self.assertTrue(
                    interaction.followup.send.await_args.kwargs["ephemeral"]
                )
                self.assertEqual(events, ["defer", "query", "followup"])
                interaction.response.send_message.assert_not_awaited()

    async def test_user_select_also_defers_before_database_query(self):
        interaction, events = self._interaction()
        target_user = SimpleNamespace(id=789, display_name="target")
        view = StatsView(SimpleNamespace())
        select = self._item(view, "stats_user_select")
        select._values = [target_user]

        async def query(*args, **kwargs):
            self.assertEqual(events, ["defer"])
            events.append("query")
            return None

        with patch(
            "database.get_player_stats", new=AsyncMock(side_effect=query)
        ) as db_query:
            await select.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True
        )
        db_query.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(events, ["defer", "query", "followup"])

    async def test_overall_data_defers_before_loading_two_axis_view(self):
        interaction, events = self._interaction()

        async def load_embed(_guild):
            self.assertEqual(events, ["defer"])
            events.append("query")
            return discord.Embed(title="全体データ")

        view = StatsView(SimpleNamespace())
        with patch(
            "views.OverallStatsFilterView.load_embed",
            new=AsyncMock(side_effect=load_embed),
        ) as load:
            await self._item(view, "stats_overall_data").callback(interaction)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True,
        )
        load.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(events, ["defer", "query", "followup"])

    async def test_rank_spec_has_no_database_wait_and_replies_immediately(self):
        interaction, _events = self._interaction()
        view = StatsView(SimpleNamespace())

        await self._item(view, "stats_rank_spec").callback(interaction)

        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs["ephemeral"]
        )

    def test_overall_embed_marks_small_samples_and_explains_relative_rank(self):
        game_stats = {
            "games": 1,
            "detailed_games": 1,
            "wins": {"村陣営": 1, "狼陣営": 0},
            "days": {"count": 1, "average": 4.0},
            "peaceful": {"numerator": 1, "denominator": 4, "sample_games": 1},
            "day1_execution": {"numerator": 1, "denominator": 1},
            "executions": {"numerator": 2, "denominator": 4, "sample_games": 1},
            "night1_role_kill": {"numerator": 1, "denominator": 1},
            "wolf_alive_by_day": [
                {"day": 1, "average": 3.0, "count": 1},
            ],
            "time_counts": {"夜 18–23時": 1},
            "gm_counts": [(99, 1)],
        }
        rank_stats = {
            "provisional_excluded": 2,
            "roles": {},
            "seer": {"checks": 1, "wolf_hits": 1, "survival_count": 1, "survival_average": 3.0},
            "guard": {"checks": 1, "successes": 1},
            "wolf": {"survival_count": 3, "survival_average": 2.0},
        }
        guild = SimpleNamespace(get_member=lambda _member_id: None)

        embed = build_overall_stats_embed(
            game_stats, rank_stats,
            room_label="総合", rank_label="ダイヤ", guild=guild,
        )
        rendered = str(embed.to_dict())

        self.assertIn("試合数不足", rendered)
        self.assertIn("相対評価", embed.description)
        self.assertNotIn("rank_bucket", rendered)


if __name__ == "__main__":
    unittest.main()
