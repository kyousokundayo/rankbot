"""参加受付・終了復旧の耐久性をv0.52以降の回帰契約として固定する。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from config import Phase, Role, RoomDefinition
from models import Player
from room_runner import RoomRunner
from views import LobbyView, RemovePlayerSelectView


def _member(user_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=name,
        roles=[],
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )


class WaitingRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_join_and_gm_claim_do_not_consult_other_waiting_rooms(self) -> None:
        runner = RoomRunner.__new__(RoomRunner)
        runner.state = SimpleNamespace(guild=SimpleNamespace(owner_id=999))
        runner.room_def = SimpleNamespace(
            access_role_names=None,
            allowed_ranks=None,
            allowed_gm_user_ids=None,
            owner_only_gm=False,
        )
        runner.manager = SimpleNamespace(
            find_user_room=Mock(side_effect=AssertionError("待機卓は所属競合にしない")),
        )
        runner._strict_access_error = Mock(return_value=None)
        runner.is_private_room = Mock(return_value=False)

        member = _member(10, "参加者")
        self.assertIsNone(await runner.validate_join(member))
        self.assertIsNone(await runner.validate_gm_claim(member))
        runner.manager.find_user_room.assert_not_called()


class StartConflictTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_rejects_a_player_or_gm_in_another_active_game(self) -> None:
        runner = RoomRunner.__new__(RoomRunner)
        player = _member(10, "プレイヤー")
        gm = _member(20, "GM")
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            ending=False,
            room_id="target",
            players={player.id: SimpleNamespace(member=player)},
            gm_id=gm.id,
        )
        runner.state = state
        runner._postgame_vote_pending = False
        active = SimpleNamespace(state=SimpleNamespace(room_name="進行中の村"))
        calls: list[int] = []

        def find_active(user_id: int, *, exclude_room_id: str):
            calls.append(user_id)
            self.assertEqual(exclude_room_id, "target")
            return active

        runner.manager = SimpleNamespace(
            rooms={"target": runner},
            find_active_user_room=find_active,
        )
        sent = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                get_member=lambda user_id: {10: player, 20: gm}.get(user_id),
            ),
            followup=SimpleNamespace(send=sent),
        )

        await runner._start_game_locked(interaction)

        self.assertEqual(calls, [10, 20])
        message = sent.await_args.args[0]
        self.assertIn("進行中の村", message)
        self.assertIn("プレイヤー", message)
        self.assertIn("GM", message)

    async def test_start_rejects_registered_player_outside_game_vc(self) -> None:
        runner = RoomRunner.__new__(RoomRunner)
        game_vc = SimpleNamespace(id=500)
        connected = _member(10, "接続済み")
        missing = _member(11, "未接続")
        gm = _member(20, "GM")
        connected.voice = SimpleNamespace(channel=game_vc)
        missing.voice = SimpleNamespace(channel=None)
        gm.voice = SimpleNamespace(channel=game_vc)
        players = {
            connected.id: Player(connected.id, connected),
            missing.id: Player(missing.id, missing),
        }
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            ending=False,
            room_id="target",
            room_name="対象村",
            players=players,
            gm_id=gm.id,
            original_nicknames={},
            vc_default_permissions_captured=False,
            vc_gm_speak_captured=False,
            voice_channel=game_vc,
        )
        runner.state = state
        runner.room_def = SimpleNamespace(variant_id="test-two")
        runner._postgame_vote_pending = False
        runner.manager = SimpleNamespace(
            rooms={"target": runner},
            find_active_user_room=lambda *_args, **_kwargs: None,
        )
        members = {member.id: member for member in (connected, missing, gm)}
        sent = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(get_member=members.get),
            followup=SimpleNamespace(send=sent),
        )

        with patch(
            "room_runner.get_variant_definition",
            return_value=SimpleNamespace(player_count=2),
        ):
            await runner._start_game_locked(interaction)

        self.assertEqual(state.phase, Phase.LOBBY)
        self.assertFalse(hasattr(state, "game_run_id"))
        message = sent.await_args.args[0]
        self.assertIn("ゲームVC", message)
        self.assertIn("未接続", message)
        self.assertNotIn("接続済み", message)


class LobbyReopenAndPanelTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_lobby_has_reopen_button_and_wires_to_manager(self) -> None:
        recruitment_manager = SimpleNamespace(
            reopen_previous_recruitment=AsyncMock(return_value="再開しました"),
        )
        cog = SimpleNamespace(
            state=SimpleNamespace(
                room_id="gm-room", phase=Phase.LOBBY, players={}, gm_id=None,
            ),
            manager=SimpleNamespace(recruitment_manager=recruitment_manager),
            is_private_room=lambda: True,
        )
        view = LobbyView(cog)
        button = next(
            item for item in view.children
            if getattr(item, "custom_id", "").startswith("reopen_previous_recruitment:")
        )
        self.assertEqual(button.label, "前回設定で参加受付を再開")

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await view.reopen_previous_recruitment(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        recruitment_manager.reopen_previous_recruitment.assert_awaited_once_with(
            interaction, cog,
        )
        interaction.followup.send.assert_awaited_once_with("再開しました", ephemeral=True)

    async def test_lobby_send_failure_does_not_purge_existing_panel(self) -> None:
        runner = RoomRunner.__new__(RoomRunner)
        channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("send failed")))
        runner.state = SimpleNamespace(lobby_channel=channel)
        runner.manager = SimpleNamespace(_startup_in_progress=False)
        runner._purge_bot_messages = AsyncMock()

        with patch("room_runner.LobbyView") as lobby_view:
            lobby_view.return_value = SimpleNamespace(_build_embed=lambda: object())
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await runner._post_lobby_ui()

        runner._purge_bot_messages.assert_not_awaited()

    async def test_postgame_vote_completion_refreshes_existing_lobby_panel(self) -> None:
        runner = RoomRunner.__new__(RoomRunner)
        runner.state = SimpleNamespace(
            phase=Phase.LOBBY,
            ending=False,
            room_name="対象村",
        )
        runner._postgame_vote_pending = True
        runner._run_postgame_recommendations = AsyncMock()
        runner._post_lobby_ui = AsyncMock()

        await runner._run_postgame_recommendations_task(
            SimpleNamespace(),
            1,
            {(10, "recommend")},
            {20},
            ladder_id="l13",
        )

        self.assertFalse(runner._postgame_vote_pending)
        runner._post_lobby_ui.assert_awaited_once_with(reuse_existing=True)


class NicknameAndRemovalSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_restore_nicknames_returns_absent_member_as_pending(self) -> None:
        runner = RoomRunner.__new__(RoomRunner)
        state = SimpleNamespace(
            guild=SimpleNamespace(roles=[], get_member=lambda _user_id: None),
            room_id="room",
            players={},
            original_nicknames={10: "元の名前"},
            bot_muted_ids=set(),
            voice_channel=None,
        )

        self.assertEqual(await runner._restore_nicknames(state), {10: "元の名前"})

    async def test_game_removal_false_does_not_report_success(self) -> None:
        player = SimpleNamespace(user_id=10, alive=True, display_name="対象")
        state = SimpleNamespace(
            game_run_id="run",
            gm_id=1,
            phase=Phase.DAY_DISCUSSION,
            get_player=lambda user_id: player if user_id == 10 else None,
        )
        cog = SimpleNamespace(
            state=state,
            action_lock=__import__("asyncio").Lock(),
            is_current_game_view=lambda run_id: run_id == "run",
            _eliminate_player_mid_game=AsyncMock(return_value=False),
        )
        view = RemovePlayerSelectView(
            cog,
            [__import__("discord").SelectOption(label="対象", value="10")],
        )
        initial = SimpleNamespace(
            user=SimpleNamespace(id=1),
            data={"values": ["10"]},
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await view.select_callback(initial)

        confirmation = initial.response.send_message.await_args.kwargs["view"]
        confirm_interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        button = next(item for item in confirmation.children if item.label == "ゲームから除外")
        await button.callback(confirm_interaction)

        message = confirm_interaction.followup.send.await_args.args[0]
        self.assertIn("除外しませんでした", message)
        self.assertNotIn("ゲームから除外しました", message)


class GameOverRestoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_game_over_restore_keeps_rematch_data_and_runs_cleanup(self) -> None:
        room_def = RoomDefinition("restore", "復旧村", rated=False)
        source = RoomRunner(None, SimpleNamespace(), room_def)
        member = SimpleNamespace(id=10, display_name="参加者", nick="01.参加者")
        source.state.players[member.id] = Player(
            user_id=member.id,
            member=member,
            role=Role.VILLAGER,
            alive=True,
            number=1,
            original_nickname="元の名前",
            base_name="参加者",
        )
        source.state.gm_id = 20
        payload = source._build_room_snapshot()
        payload["phase"] = Phase.GAME_OVER.name
        payload["prep_panel_message_id"] = 111
        payload["speech_panel_message_id"] = 222
        payload["wolf_dm_message_ids"] = [
            {"user_id": member.id, "message_ids": [333]},
        ]

        restored = RoomRunner(None, SimpleNamespace(), room_def)
        restored.state.guild = SimpleNamespace(
            id=123,
            roles=[],
            get_member=lambda user_id: member if user_id == member.id else None,
            get_channel=lambda _channel_id: None,
        )
        call_order: list[str] = []

        async def close_views():
            call_order.append("close_views")
            self.assertEqual(restored.state.prep_panel_message_id, 111)
            self.assertEqual(restored.state.speech_panel_message_id, 222)
            self.assertEqual(restored.state.wolf_dm_message_ids, {member.id: [333]})

        async def restore_names(_state):
            call_order.append("restore_nicknames")
            return {member.id: "元の名前"}

        async def teardown():
            call_order.append("teardown")

        async def transition(_state, **kwargs):
            call_order.append("transition")
            self.assertEqual(
                kwargs["nickname_failures"],
                {member.id: "元の名前"},
            )
            self.assertEqual(kwargs["log_label"], "再起動時の終了処理回収")
            return True

        restored._restore_nicknames = AsyncMock(side_effect=restore_names)
        restored._teardown_game_roles_and_perms = AsyncMock(side_effect=teardown)
        restored._transition_to_empty_lobby = AsyncMock(side_effect=transition)
        restored._close_game_views_for_shutdown = AsyncMock(side_effect=close_views)

        await restored.restore_from_snapshot(payload)

        self.assertEqual(restored.last_game_roster, [member.id])
        self.assertEqual(restored.last_game_gm, 20)
        self.assertEqual(
            call_order,
            ["close_views", "restore_nicknames", "teardown", "transition"],
        )
        restored._close_game_views_for_shutdown.assert_awaited_once()
        restored._transition_to_empty_lobby.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
