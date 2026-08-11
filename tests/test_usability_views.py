"""常設パネルの段数と入口を守る回帰テスト。"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import BOT_VERSION, Phase, SLOW_INTERACTION_SECONDS
from recruitment import OperationsView
from views import (
    DangerConfirmView,
    GMControlView,
    GMPanelEntryView,
    InteractionTimer,
    LobbyView,
    StatsView,
    build_help_embeds,
    build_rank_spec_embeds,
    build_rule_embeds,
    build_vote_result_embed,
)


def _assert_within_three_rows(test: unittest.TestCase, view) -> None:
    rows: dict[int, int] = {}
    for item in view.children:
        test.assertIsNotNone(item.row)
        rows[item.row] = rows.get(item.row, 0) + 1
    test.assertLessEqual(len(rows), 3)
    test.assertLessEqual(max(rows, default=0), 2)
    test.assertTrue(all(item_count <= 5 for item_count in rows.values()))


class UsabilityViewLayoutTest(unittest.TestCase):
    @staticmethod
    def _lobby_cog(*, private: bool):
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={},
            gm_id=None,
            room_id="private_1" if private else "beginner",
        )
        return SimpleNamespace(state=state, is_private_room=lambda: private)

    def test_lobby_panel_stays_within_three_rows(self) -> None:
        for private in (False, True):
            with self.subTest(private=private):
                view = LobbyView(self._lobby_cog(private=private))
                _assert_within_three_rows(self, view)
                self.assertIsNotNone(
                    next(
                        item
                        for item in view.children
                        if item.custom_id == "lobby_gm_menu"
                    )
                )
                notification = next(
                    item for item in view.children
                    if item.custom_id == "recruitment_notification_toggle"
                )
                self.assertEqual(notification.row, 0)

        public = LobbyView(self._lobby_cog(private=False))
        rows = {
            row: [item.custom_id for item in public.children if item.row == row]
            for row in (0, 1)
        }
        self.assertEqual(
            rows[0],
            [
                "join_game", "leave_game", "get_gm", "release_gm",
                "recruitment_notification_toggle",
            ],
        )
        self.assertEqual(
            rows[1],
            ["start_game", "rematch_game", "lobby_gm_menu", "rule_btn", "help_btn"],
        )

    def test_stats_panel_stays_within_three_rows(self) -> None:
        view = StatsView(SimpleNamespace())

        _assert_within_three_rows(self, view)
        self.assertIsNotNone(
            next(item for item in view.children if item.custom_id == "feedback_report")
        )
        self.assertFalse(
            any(item.custom_id == "stats_variant_balance" for item in view.children)
        )

    def test_variant_balance_is_in_operations_panel(self) -> None:
        manager = SimpleNamespace()
        view = OperationsView(manager)

        _assert_within_three_rows(self, view)
        self.assertIsNotNone(
            next(
                item for item in view.children
                if item.custom_id == "operations:variant_balance"
            )
        )

    def test_public_gm_panel_is_one_button_and_private_menu_is_two_rows(self) -> None:
        state = SimpleNamespace(
            game_run_id="run-1",
            phase=Phase.DAY_DISCUSSION,
            phase_before_pause=None,
            paused=False,
            ending=False,
            pending_winner=None,
        )
        cog = SimpleNamespace(state=state, register_game_view=lambda _view: None)

        entry = GMPanelEntryView(cog)
        menu = GMControlView(cog)

        self.assertEqual(len(entry.children), 1)
        _assert_within_three_rows(self, menu)
        self.assertEqual({item.row for item in menu.children}, {0, 1})


class DangerConfirmationTest(unittest.IsolatedAsyncioTestCase):
    async def test_action_runs_only_after_same_user_confirms(self) -> None:
        action = AsyncMock()
        view = DangerConfirmView(1, action)
        wrong_user = SimpleNamespace(
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        confirm = next(item for item in view.children if item.label == "実行する")

        await confirm.callback(wrong_user)

        action.assert_not_awaited()
        wrong_user.response.send_message.assert_awaited_once()

        actor = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
        )
        await confirm.callback(actor)

        actor.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        action.assert_awaited_once_with(actor)


class HelpAndRuleEmbedTest(unittest.TestCase):
    """ヘルプ・ルールがDiscordの上限を超えず、実装と食い違わないこと。"""

    def _all_embeds(self):
        return [
            *build_rule_embeds(),
            *build_help_embeds(),
            *build_rank_spec_embeds(),
        ]

    def test_embeds_fit_discord_limits(self) -> None:
        for embed in self._all_embeds():
            total = (
                len(embed.title or "")
                + len(embed.description or "")
                + sum(len(f.name) + len(f.value) for f in embed.fields)
            )
            self.assertLessEqual(total, 6000, embed.title)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024, f"{embed.title}/{field.name}")

    def test_no_stale_reference_to_the_morning_panel_being_in_dms(self) -> None:
        """「朝を迎える」は #昼 のパネルへ移った。DM前提の説明を残さない。"""
        for embed in self._all_embeds():
            for field in embed.fields:
                self.assertNotRegex(
                    field.value,
                    r"DM[^\n]*朝を迎える|朝を迎える[^\n]*DM",
                    f"{embed.title}/{field.name}",
                )

    def test_help_shows_current_release_and_all_room_log_policy(self) -> None:
        help_embed = build_help_embeds()[0]
        fields = {field.name: field.value for field in help_embed.fields}
        self.assertIn(f"{BOT_VERSION}の変更", fields)
        self.assertIn("GM村と募集", fields[f"{BOT_VERSION}の変更"])
        self.assertIn("募集通知", fields[f"{BOT_VERSION}の変更"])
        self.assertIn("正常終了した全村", fields[f"{BOT_VERSION}の変更"])
        self.assertIn("役職確認を締切", fields["GMの操作"])
        self.assertIn("全村", fields["終わった試合を読み返す"])
        self.assertIn("書き込みはできません", fields["終わった試合を読み返す"])

    def test_release_version_is_consistent_across_current_documents(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        spec = (root / "SPEC.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn(f"現在のバージョン: **{BOT_VERSION}**", readme)
        self.assertIn(f"対応Bot: **{BOT_VERSION}**", spec)
        self.assertIn(f"## {BOT_VERSION}", changelog)

    def test_vote_help_explains_timeout_instead_of_claiming_no_abstention(self) -> None:
        rule_embed = build_rule_embeds()[0]
        fields = {field.name: field.value for field in rule_embed.fields}
        vote_help = fields["投票と処刑"]
        self.assertIn("棄権ボタン", vote_help)
        self.assertIn("既投票分だけで集計", vote_help)
        self.assertIn("1票もなければ処刑なし", vote_help)
        self.assertNotIn("棄権もできません", vote_help)


class InteractionTimerTest(unittest.TestCase):
    """遅いボタン押下だけをログへ残す (平常時に13人分を毎晩出さない)。"""

    def test_fast_press_logs_nothing_and_slow_press_reports_each_stage(self) -> None:
        timer = InteractionTimer("朝を迎える", 42)
        timer.mark("ack")
        with self.assertNoLogs("views", level="WARNING"):
            timer.finish()

        slow = InteractionTimer("朝を迎える", 42)
        # 押下からの経過を閾値超えに見せかける
        slow._started -= SLOW_INTERACTION_SECONDS + 1
        slow.mark("ack")
        slow.mark("lock")
        with self.assertLogs("views", level="WARNING") as captured:
            slow.finish(note="committed=True")

        message = captured.output[0]
        self.assertIn("朝を迎える", message)
        self.assertIn("user=42", message)
        self.assertIn("ack=", message)
        self.assertIn("lock=", message)
        self.assertIn("committed=True", message)


class VoteResultOrderTest(unittest.TestCase):
    """投票結果は得票順ではなく番号順に並べる (名簿と同じ並びで追えるように)。"""

    @staticmethod
    def _players() -> dict:
        # 参加順(キーの順)と番号をわざとずらす
        spec = [(10, 7, True), (20, 2, True), (30, 11, False)]
        return {
            user_id: SimpleNamespace(
                user_id=user_id,
                number=number,
                alive=alive,
                display_name=f"{number:02d}.p{user_id}",
            )
            for user_id, number, alive in spec
        }

    def test_tally_lines_and_details_follow_player_numbers(self) -> None:
        players = self._players()
        # 投票が届いた順もばらばらにする
        votes = {10: 20, 30: 20, 20: 10}

        embed = build_vote_result_embed(votes, players)

        lines = [
            line for line in embed.description.strip("`\n").splitlines() if line
        ]
        # 得票のある生存者だけが番号順で並ぶ (11.p30 は死亡かつ0票)
        self.assertEqual(
            [line.split(" ")[0] for line in lines],
            ["02.p20", "07.p10"],
        )
        # 内訳は投票者の番号順 (dictの反復順=投票が届いた順のままだと、
        # 誰が先に投票したかまで公開される)
        detail = embed.fields[0].value.splitlines()
        self.assertEqual(
            [row.split(" ")[0] for row in detail],
            ["02.p20", "07.p10", "11.p30"],
        )

    def test_alive_player_with_no_votes_is_still_listed(self) -> None:
        players = self._players()
        votes = {10: 20}

        embed = build_vote_result_embed(votes, players)

        lines = embed.description
        self.assertIn("07.p10", lines)   # 0票の生存者も出す
        self.assertIn("02.p20", lines)
        self.assertNotIn("11.p30", lines)  # 死亡かつ0票は出さない


if __name__ == "__main__":
    unittest.main()
