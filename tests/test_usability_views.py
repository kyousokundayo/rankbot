"""常設パネルの段数と入口を守る回帰テスト。"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import stats_image
import views
from config import BOT_VERSION, Phase, SLOW_INTERACTION_SECONDS
from recruitment import OperationsView
from views import (
    DangerConfirmView,
    GMControlView,
    GMPanelEntryView,
    InteractionTimer,
    LobbyGMMenuView,
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
            recruitment_id=None,
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

        # GM村は参加・GM登録だけを募集カードへ寄せる。連戦のたびに募集を
        # 作り直させないため、次村はロビーに残す。
        private_view = LobbyView(self._lobby_cog(private=True))
        private_ids = [item.custom_id for item in private_view.children]
        self.assertIn("rematch_game", private_ids)
        self.assertNotIn("join_game", private_ids)
        self.assertNotIn("leave_game", private_ids)
        self.assertNotIn("get_gm", private_ids)

    def test_private_rematch_keeps_leave_and_postgame_wait_disables_start(self) -> None:
        player = SimpleNamespace(member=SimpleNamespace(display_name="参加者"))
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={1: player},
            gm_id=99,
            room_id="private_1",
            recruitment_id=None,
            guild=SimpleNamespace(
                get_member=lambda user_id: (
                    SimpleNamespace(display_name="GM") if user_id == 99 else None
                ),
            ),
            room_name="GM村",
        )
        cog = SimpleNamespace(
            state=state,
            variant=SimpleNamespace(player_count=1, label="1人テスト"),
            room_def=SimpleNamespace(
                allowed_ranks=None,
                allowed_gm_user_ids=None,
                owner_only_gm=False,
            ),
            is_private_room=lambda: True,
            _postgame_vote_pending=True,
        )

        view = LobbyView(cog)
        ids = [item.custom_id for item in view.children]
        start = next(item for item in view.children if item.custom_id == "start_game")

        self.assertIn("leave_game", ids)
        self.assertTrue(start.disabled)
        self.assertIn("終了後投票を受付中", view._build_embed().description)

    def test_stats_panel_stays_within_three_rows(self) -> None:
        view = StatsView(SimpleNamespace())

        _assert_within_three_rows(self, view)
        self.assertIsNotNone(
            next(item for item in view.children if item.custom_id == "feedback_report")
        )
        self.assertFalse(
            any(item.custom_id == "stats_variant_balance" for item in view.children)
        )

    def test_stats_panel_image_button_follows_font_availability(self) -> None:
        # フォントが使えてもSTATS_CARD_BUTTON_ENABLEDが既定OFFの間はボタンを出さない
        # (シーズン1開始前のデータ項目・レイアウト未確定のための一時措置)。
        with patch.object(stats_image, "font_available", return_value=True):
            view = StatsView(SimpleNamespace())
            _assert_within_three_rows(self, view)
            self.assertFalse(
                any(item.custom_id == "stats_card_image" for item in view.children)
            )

        # フォントが使え、かつ有効化されていればボタンを出し、3行制約も崩さない。
        with patch.object(stats_image, "font_available", return_value=True), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = StatsView(SimpleNamespace())
            _assert_within_three_rows(self, view)
            self.assertIsNotNone(
                next(
                    item for item in view.children
                    if item.custom_id == "stats_card_image"
                )
            )

        # フォントが無い環境ではボタンごと出さない (押しても失敗するボタンを見せない)。
        with patch.object(stats_image, "font_available", return_value=False), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = StatsView(SimpleNamespace())
            _assert_within_three_rows(self, view)
            self.assertFalse(
                any(item.custom_id == "stats_card_image" for item in view.children)
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
            initial_night_completed=False,
            vote_slot_active=False,
            current_speaker_id=None,
            morning_ready_open=False,
        )
        cog = SimpleNamespace(state=state, register_game_view=lambda _view: None)

        entry = GMPanelEntryView(cog)
        menu = GMControlView(cog)

        self.assertEqual(len(entry.children), 1)
        _assert_within_three_rows(self, menu)
        self.assertEqual({item.row for item in menu.children}, {0, 1})


class LobbyInteractionExpiryTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_acknowledges_before_waiting_for_action_lock(self) -> None:
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={1: object()},
            gm_id=99,
            room_id="beginner",
        )
        action_lock = asyncio.Lock()
        cog = SimpleNamespace(
            state=state,
            action_lock=action_lock,
            variant=SimpleNamespace(player_count=1),
            is_private_room=lambda: False,
            start_game=AsyncMock(),
        )
        view = LobbyView(cog)
        start_button = next(
            item for item in view.children if item.custom_id == "start_game"
        )
        acknowledged = asyncio.Event()

        async def acknowledge() -> None:
            acknowledged.set()

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=acknowledge),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await action_lock.acquire()
        callback_task = asyncio.create_task(start_button.callback(interaction))
        try:
            await asyncio.wait_for(acknowledged.wait(), timeout=1)
            self.assertFalse(callback_task.done())
            interaction.response.defer.assert_awaited_once_with()

            # 待っている間に別の開始処理が村を進行中へ変えた場合でも、
            # 期限切れの初回responseではなくfollowupで結果を返す。
            state.phase = Phase.DAY_DISCUSSION
        finally:
            action_lock.release()

        await asyncio.wait_for(callback_task, timeout=1)
        interaction.followup.send.assert_awaited_once_with(
            "現在ゲーム中です。", ephemeral=True,
        )
        interaction.response.send_message.assert_not_awaited()
        cog.start_game.assert_not_awaited()

    async def test_start_edits_panel_after_early_acknowledgement(self) -> None:
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={1: object()},
            gm_id=99,
            room_id="beginner",
        )

        async def start_game(_interaction) -> None:
            state.phase = Phase.PREPARATION

        cog = SimpleNamespace(
            state=state,
            action_lock=asyncio.Lock(),
            variant=SimpleNamespace(player_count=1),
            is_private_room=lambda: False,
            start_game=AsyncMock(side_effect=start_game),
        )
        view = LobbyView(cog)
        start_button = next(
            item for item in view.children if item.custom_id == "start_game"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(
                defer=AsyncMock(),
                edit_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await start_button.callback(interaction)

        interaction.response.defer.assert_awaited_once_with()
        interaction.message.edit.assert_awaited_once_with(view=view)
        interaction.response.edit_message.assert_not_awaited()
        cog.start_game.assert_awaited_once_with(interaction)


class PrivateLobbyRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_rematch_player_can_cancel_from_private_lobby(self) -> None:
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={
                1: SimpleNamespace(member=SimpleNamespace(display_name="参加者")),
            },
            gm_id=99,
            room_id="private_1",
            recruitment_id=None,
            guild=SimpleNamespace(
                get_member=lambda user_id: (
                    SimpleNamespace(display_name="GM") if user_id == 99 else None
                ),
            ),
            room_name="GM村",
        )
        cog = SimpleNamespace(
            state=state,
            action_lock=asyncio.Lock(),
            variant=SimpleNamespace(player_count=1, label="1人テスト"),
            room_def=SimpleNamespace(
                allowed_ranks=None,
                allowed_gm_user_ids=None,
                owner_only_gm=False,
            ),
            is_private_room=lambda: True,
            _postgame_vote_pending=False,
            _persist_room_state=AsyncMock(),
        )
        view = LobbyView(cog)
        self.assertFalse(any(
            str(getattr(item, "custom_id", "")).startswith(
                "reopen_previous_recruitment:"
            )
            for item in view.children
        ))
        leave = next(item for item in view.children if item.custom_id == "leave_game")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await leave.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True,
        )
        self.assertNotIn(1, state.players)
        cog._persist_room_state.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            "参加を取り消しました。", ephemeral=True,
        )

    async def test_rematch_cancel_rolls_back_when_snapshot_save_fails(self) -> None:
        player = SimpleNamespace(member=SimpleNamespace(display_name="参加者"))
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={1: player},
            gm_id=99,
            room_id="private_1",
            recruitment_id=None,
            guild=SimpleNamespace(get_member=lambda _user_id: None),
            room_name="GM村",
        )
        cog = SimpleNamespace(
            state=state,
            action_lock=asyncio.Lock(),
            variant=SimpleNamespace(player_count=1, label="1人テスト"),
            room_def=SimpleNamespace(
                allowed_ranks=None,
                allowed_gm_user_ids=None,
                owner_only_gm=False,
            ),
            is_private_room=lambda: True,
            _postgame_vote_pending=False,
            _persist_room_state=AsyncMock(side_effect=RuntimeError("DB down")),
        )
        view = LobbyView(cog)
        leave = next(item for item in view.children if item.custom_id == "leave_game")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await leave.callback(interaction)

        self.assertIs(state.players[1], player)
        interaction.message.edit.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "参加取消を保存できませんでした。もう一度お試しください。",
            ephemeral=True,
        )

    async def test_private_reset_reopens_previous_recruitment(self) -> None:
        recruitment_manager = SimpleNamespace(
            reopen_previous_recruitment=AsyncMock(
                return_value="✅ 前回設定で参加受付を再開しました。",
            ),
        )
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={1: object()},
            gm_id=99,
            room_id="private_1",
        )
        cog = SimpleNamespace(
            state=state,
            manager=SimpleNamespace(recruitment_manager=recruitment_manager),
            is_private_room=lambda: True,
            reset_game=AsyncMock(return_value="🔄 参加受付をリセットしました。"),
        )
        view = LobbyGMMenuView(cog)
        reset = next(item for item in view.children if item.label == "受付をリセット")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await reset.callback(interaction)

        sent = interaction.response.send_message.await_args
        self.assertIn("空の募集カード", sent.args[0])
        confirm_view = sent.kwargs["view"]
        confirm = next(item for item in confirm_view.children if item.label == "受付をリセット")
        confirm_interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await confirm.callback(confirm_interaction)

        cog.reset_game.assert_awaited_once()
        recruitment_manager.reopen_previous_recruitment.assert_awaited_once_with(
            confirm_interaction, cog,
        )
        result = confirm_interaction.followup.send.await_args.args[0]
        self.assertIn("参加受付をリセット", result)
        self.assertIn("前回設定で参加受付を再開", result)


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

    def test_help_omits_release_notes_and_keeps_gm_and_log_policy(self) -> None:
        """ヘルプはリリースノートを持たない。

        見出しへ BOT_VERSION を埋めた「変更点」フィールドは、版を上げるだけで
        中身が古いまま見出しだけ新しくなる (実際にv0.41の内容がv0.47として
        表示されていた)。変更履歴はCHANGELOG.mdに一本化する。
        """
        help_embed = build_help_embeds()[0]
        fields = {field.name: field.value for field in help_embed.fields}
        self.assertNotIn(f"{BOT_VERSION}の変更", fields)
        self.assertFalse([name for name in fields if "の変更" in name])
        self.assertIn("役職確認を締切", fields["GMの操作"])
        self.assertIn("スキップ", fields["GMの操作"])
        self.assertIn("全村", fields["終わった試合を読み返す"])
        self.assertIn("書き込みはできません", fields["終わった試合を読み返す"])
        self.assertIn("GMのミュートは手動", fields["発言とミュート"])
        self.assertNotIn("専任GM", fields["発言とミュート"])
        self.assertIn("リセットは同じ参加者・GMを維持", fields["GMの操作"])
        self.assertIn("強制終了はGMだけを残して参加者0人", fields["GMの操作"])

    def test_release_version_is_consistent_across_current_documents(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        spec = (root / "SPEC.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn(f"現在のバージョン: **{BOT_VERSION}**", readme)
        self.assertIn(f"対応Bot: **{BOT_VERSION}**", spec)
        self.assertIn(f"## {BOT_VERSION}", changelog)

    def test_vote_help_explains_current_crosstalk_vote_flow(self) -> None:
        rule_embed = build_rule_embeds()[0]
        fields = {field.name: field.value for field in rule_embed.fields}
        vote_help = fields["投票と処刑"]
        daily_flow = fields["1日の流れ"]
        self.assertIn("投票参加", vote_help)
        self.assertIn("1人30秒", vote_help)
        self.assertIn("発言終了", vote_help)
        self.assertIn("発言終了SEと約2秒", vote_help)
        self.assertIn("本人専用の確認", vote_help)
        self.assertIn("候補選択に時間制限はなく", vote_help)
        self.assertIn("自動棄権はしません", vote_help)
        self.assertIn("1人30秒の投票発言", daily_flow)
        self.assertIn("1票もなければ処刑なし", vote_help)
        self.assertIn("候補者以外が一斉", vote_help)
        self.assertNotIn("1人20秒", vote_help)
        self.assertNotIn("時間切れは棄権", vote_help)


class GMControlRefreshTest(unittest.IsolatedAsyncioTestCase):
    """停止/再開後も同じGMパネルで逆操作を続けられること。"""

    @staticmethod
    def _button(view, custom_id: str):
        return next(item for item in view.children if item.custom_id == custom_id)

    async def test_pause_and_resume_replace_the_same_ephemeral_panel(self) -> None:
        state = SimpleNamespace(
            game_run_id="run-1",
            gm_id=99,
            phase=Phase.DAY_DISCUSSION,
            phase_before_pause=None,
            paused=False,
            ending=False,
            pending_winner=None,
            initial_night_completed=False,
            vote_slot_active=False,
            vote_slot_token=0,
            turn_slot_token=0,
            current_speaker_id=None,
            morning_ready_open=False,
        )

        async def pause_game() -> str:
            state.paused = True
            state.phase_before_pause = state.phase
            state.phase = Phase.PAUSED
            return "⏸️ 一時停止しました。"

        async def resume_game() -> str:
            state.paused = False
            state.phase = state.phase_before_pause
            state.phase_before_pause = None
            return "▶️ 再開しました。"

        cog = SimpleNamespace(
            state=state,
            pause_game=AsyncMock(side_effect=pause_game),
            resume_game=AsyncMock(side_effect=resume_game),
        )
        original = GMControlView(cog)
        pause_interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with patch.object(views, "build_gm_status_embed", return_value=object()):
            await self._button(original, "gm_pause").callback(pause_interaction)

            pause_interaction.response.defer.assert_awaited_once_with()
            pause_interaction.edit_original_response.assert_awaited_once()
            self.assertTrue(original.is_finished())
            paused_view = pause_interaction.edit_original_response.await_args.kwargs["view"]
            self.assertTrue(self._button(paused_view, "gm_pause").disabled)
            self.assertFalse(self._button(paused_view, "gm_resume").disabled)

            resume_interaction = SimpleNamespace(
                user=SimpleNamespace(id=99),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                edit_original_response=AsyncMock(),
            )
            await self._button(paused_view, "gm_resume").callback(resume_interaction)

        resume_interaction.response.defer.assert_awaited_once_with()
        resume_interaction.edit_original_response.assert_awaited_once()
        self.assertTrue(paused_view.is_finished())
        resumed_view = resume_interaction.edit_original_response.await_args.kwargs["view"]
        self.assertFalse(self._button(resumed_view, "gm_pause").disabled)
        self.assertTrue(self._button(resumed_view, "gm_resume").disabled)


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
