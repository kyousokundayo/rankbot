"""v0.56のリセット・廃村・除外後UIを固定する回帰契約。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

import database
from config import Phase, Role, RoomDefinition
from game import GameCog
from models import Player
from room_runner import RoomRunner
from views import GMControlView, RemovePlayerSelectView


def _member(user_id: int, name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=name or f"user-{user_id}",
        nick=None,
        roles=[],
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )


class ResetAndForceEndContractTest(unittest.IsolatedAsyncioTestCase):
    def _runner(self) -> RoomRunner:
        runner = RoomRunner(
            None, SimpleNamespace(), RoomDefinition("lifecycle", "ライフサイクル", rated=False)
        )
        guild = SimpleNamespace(
            id=1,
            get_member=lambda user_id: runner.state.players.get(user_id).member
            if user_id in runner.state.players
            else None,
        )
        runner.state.guild = guild
        return runner

    def test_reset_roster_keeps_players_and_gm_but_clears_game_fields(self) -> None:
        runner = self._runner()
        first = _member(10, "一人目")
        second = _member(20, "二人目")
        runner.state.players = {
            10: Player(10, first, Role.WEREWOLF, alive=False, number=7, base_name="一人目"),
            20: Player(20, second, Role.SEER, alive=True, number=3, base_name="二人目"),
        }
        runner.state.gm_id = 99

        target = runner._make_empty_lobby_state(
            runner.state, preserve_players=True, preserve_gm=True,
        )

        self.assertEqual(target.gm_id, 99)
        self.assertEqual(set(target.players), {10, 20})
        for player in target.players.values():
            self.assertIsNone(player.role)
            self.assertTrue(player.alive)
            self.assertEqual(player.number, 0)
        self.assertIs(target.players[10].member, first)
        self.assertIs(target.players[20].member, second)

    async def test_force_end_contract_passes_gm_only_to_empty_lobby(self) -> None:
        runner = self._runner()
        runner.room_def = RoomDefinition(
            "lifecycle", "ライフサイクル", private_owner_id=1, rated=False,
        )
        runner.state.phase = Phase.DAY_DISCUSSION
        runner.state.game_run_id = "run-1"
        runner.state.gm_id = 99
        runner.state.players[10] = Player(10, _member(10), Role.VILLAGER, number=1)
        runner.state.players[99] = Player(99, _member(99), Role.VILLAGER, number=2)
        runner.state.village_channel = None
        runner._close_game_views_for_shutdown = AsyncMock()
        runner._persist_room_state = AsyncMock()
        runner._safe_village_send = AsyncMock(return_value=None)
        runner._restore_nicknames = AsyncMock(return_value={})
        runner._teardown_game_roles_and_perms = AsyncMock(return_value=True)
        runner._safe_timer_edit = AsyncMock()
        runner._retry_pending_ui_cleanup = AsyncMock()
        runner.manager.spawn_bg_task = Mock(side_effect=lambda coro: coro.close())
        runner._transition_to_empty_lobby = AsyncMock(return_value=True)

        self.assertTrue(await runner.force_end("テスト廃村"))

        kwargs = runner._transition_to_empty_lobby.await_args.kwargs
        self.assertFalse(kwargs["preserve_players"])
        self.assertTrue(kwargs["preserve_gm"])
        self.assertEqual(runner.state.lobby_return_mode, "gm")
        self.assertTrue(runner.state.pending_recruitment_reopen)

    async def test_game_over_restart_restores_saved_lobby_return_mode(self) -> None:
        for mode, expected_players, expected_gm in (
            ("roster", True, True),
            ("gm", False, True),
        ):
            with self.subTest(mode=mode):
                room_def = RoomDefinition(
                    f"restart-{mode}", f"再起動-{mode}", rated=False,
                )
                member = _member(10)
                source = RoomRunner(None, SimpleNamespace(), room_def)
                source.state.players[10] = Player(
                    10, member, Role.VILLAGER, alive=True, number=1,
                )
                source.state.gm_id = 99
                source.state.phase = Phase.GAME_OVER
                source.state.ending = True
                source.state.lobby_return_mode = mode
                payload = source._build_room_snapshot()
                payload["phase"] = Phase.GAME_OVER.name

                restored = RoomRunner(None, SimpleNamespace(), room_def)
                restored.state.guild = SimpleNamespace(
                    id=1,
                    roles=[],
                    get_member=lambda user_id: member if user_id == 10 else None,
                    get_channel=lambda _channel_id: None,
                )
                restored._close_game_views_for_shutdown = AsyncMock()
                restored._restore_nicknames = AsyncMock(return_value={})
                restored._teardown_game_roles_and_perms = AsyncMock(return_value=True)
                restored._transition_to_empty_lobby = AsyncMock(return_value=True)

                await restored.restore_from_snapshot(payload)

                kwargs = restored._transition_to_empty_lobby.await_args.kwargs
                self.assertEqual(kwargs["preserve_players"], expected_players)
                self.assertEqual(kwargs["preserve_gm"], expected_gm)


class LobbyDatabasePreservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-v054-lifecycle-")
        self._old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "lifecycle.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    async def test_database_accepts_only_reset_roster_shape_and_saves_gm(self) -> None:
        runner = RoomRunner(
            None, SimpleNamespace(), RoomDefinition("db-lifecycle", "DB村", rated=False)
        )
        runner.state.guild = SimpleNamespace(id=1, get_member=lambda _user_id: None)
        runner.state.gm_id = 99
        runner.state.players[10] = Player(
            10, _member(10), Role.WEREWOLF, alive=False, number=4,
        )
        target = runner._make_empty_lobby_state(
            runner.state, preserve_players=True, preserve_gm=True,
        )
        payload = runner._build_snapshot_for_state(target)

        await database.archive_linked_recruitment_and_save_lobby_state(
            1, "db-lifecycle", None, payload,
            preserve_players=True, preserve_gm=True,
        )
        saved = (await database.load_room_states(1))["db-lifecycle"]
        self.assertEqual(saved["phase"], Phase.LOBBY.name)
        self.assertEqual(saved["gm_id"], 99)
        self.assertEqual(saved["players"][0]["user_id"], 10)
        self.assertIsNone(saved["players"][0]["role"])
        self.assertTrue(saved["players"][0]["alive"])
        self.assertEqual(saved["players"][0]["number"], 0)

    async def test_database_rejects_player_preservation_without_gm(self) -> None:
        with self.assertRaisesRegex(ValueError, "preserving players also requires"):
            await database.archive_linked_recruitment_and_save_lobby_state(
                1, "db-lifecycle", None,
                {"players": [{"user_id": 10}], "gm_id": None},
                preserve_players=True, preserve_gm=False,
            )


class PrivateForceEndAndPanelRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_force_end_reopens_recruitment_with_retained_gm(self) -> None:
        recruitment_manager = SimpleNamespace(
            recover_pending_recruitment=AsyncMock(return_value="前回設定で受付を再開しました"),
        )
        manager = SimpleNamespace(recruitment_manager=recruitment_manager)
        runner = RoomRunner(
            None, manager,
            RoomDefinition("private-life", "名前村", private_owner_id=1, rated=False),
        )
        runner.state.game_run_id = "run-1"
        runner.state.phase = Phase.DAY_DISCUSSION
        runner.state.gm_id = 99

        async def fake_force_end(_reason: str) -> bool:
            # 実際のforce_endはGMを残したLOBBYへ差し替え、募集再開待ちを立てる。
            runner.state.phase = Phase.LOBBY
            runner.state.pending_recruitment_reopen = True
            runner.state.lobby_return_mode = "gm"
            return True

        runner.force_end = AsyncMock(side_effect=fake_force_end)
        view = GMControlView(runner)
        initial = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await view.end_btn.callback(initial)
        confirmation = initial.response.send_message.await_args.kwargs["view"]
        confirm = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await confirmation.confirm_btn.callback(confirm)

        runner.force_end.assert_awaited_once_with("GMにより強制終了されました。")
        recruitment_manager.recover_pending_recruitment.assert_awaited_once_with(
            runner,
        )
        self.assertIn(
            "前回設定で受付を再開しました",
            confirm.followup.send.await_args.args[0],
        )

    async def test_three_lobby_panel_failures_schedule_background_recovery(self) -> None:
        runner = RoomRunner(
            None, SimpleNamespace(), RoomDefinition("panel-life", "パネル村", rated=False)
        )
        runner.state.guild = SimpleNamespace(id=1)
        runner.state.recruitment_id = 42
        runner._retry_pending_ui_cleanup = AsyncMock()
        runner._post_lobby_ui = AsyncMock(side_effect=RuntimeError("temporary"))
        runner._schedule_lobby_panel_recovery = Mock()
        with patch("room_runner.database.archive_linked_recruitment_and_save_lobby_state", AsyncMock()):
            with patch("room_runner.asyncio.sleep", AsyncMock()):
                self.assertTrue(
                    await runner._transition_to_empty_lobby(
                        runner.state, log_label="テスト終了", preserve_gm=False,
                    )
                )

        self.assertEqual(runner._post_lobby_ui.await_count, 3)
        runner._schedule_lobby_panel_recovery.assert_called_once()
        self.assertEqual(
            runner._schedule_lobby_panel_recovery.call_args.args[0], runner.state,
        )
        self.assertEqual(
            runner._schedule_lobby_panel_recovery.call_args.kwargs["recruitment_id"],
            42,
        )

    async def test_lobby_restart_failure_schedules_panel_recovery(self) -> None:
        room_def = RoomDefinition("restart-panel", "再起動パネル", rated=False)
        source = RoomRunner(None, SimpleNamespace(), room_def)
        source.state.phase = Phase.LOBBY
        payload = source._build_room_snapshot()

        restored = RoomRunner(None, SimpleNamespace(), room_def)
        restored.state.guild = SimpleNamespace(
            id=1,
            roles=[],
            get_member=lambda _user_id: None,
            get_channel=lambda _channel_id: None,
        )
        restored._retry_pending_ui_cleanup = AsyncMock()
        restored._delete_alive_role = AsyncMock()
        restored._post_lobby_ui = AsyncMock(side_effect=RuntimeError("temporary"))
        restored._schedule_lobby_panel_recovery = Mock()
        restored._persist_room_state = AsyncMock()

        await restored.restore_from_snapshot(payload)

        restored._schedule_lobby_panel_recovery.assert_called_once_with(
            restored.state,
            recruitment_id=None,
            log_label="再起動復元",
        )
        restored._persist_room_state.assert_awaited_once()

    async def test_lobby_restart_resumes_pending_private_recruitment(self) -> None:
        room_def = RoomDefinition(
            "restart-recruitment",
            "再起動募集",
            private_owner_id=1,
            rated=False,
        )
        source = RoomRunner(None, SimpleNamespace(), room_def)
        source.state.phase = Phase.LOBBY
        source.state.gm_id = 2
        source.state.lobby_return_mode = "gm"
        source.state.pending_recruitment_reopen = True
        payload = source._build_room_snapshot()
        payload["phase"] = Phase.LOBBY.name

        restored = RoomRunner(None, SimpleNamespace(), room_def)
        restored.state.guild = SimpleNamespace(
            id=1,
            roles=[],
            get_member=lambda _user_id: None,
            get_channel=lambda _channel_id: None,
        )
        restored._retry_pending_ui_cleanup = AsyncMock()
        restored._delete_alive_role = AsyncMock()
        restored._post_lobby_ui = AsyncMock()
        restored._persist_room_state = AsyncMock()
        restored._schedule_recruitment_recovery = Mock()

        await restored.restore_from_snapshot(payload)

        restored._schedule_recruitment_recovery.assert_called_once_with(
            restored.state,
        )


class DeletionAndLobbyExclusionTest(unittest.IsolatedAsyncioTestCase):
    def test_game_over_private_room_is_not_deletable(self) -> None:
        cog = GameCog.__new__(GameCog)
        cog.rooms = {
            "private-life": SimpleNamespace(
                state=SimpleNamespace(phase=Phase.GAME_OVER),
            )
        }
        row = {"room_id": "private-life", "room_name": "名前村"}
        self.assertTrue(cog._private_room_phase_blocks_delete(row))
        cog.rooms["private-life"].state.phase = Phase.LOBBY
        self.assertFalse(cog._private_room_phase_blocks_delete(row))

    async def test_exclusion_select_defers_before_waiting_on_action_lock(self) -> None:
        """除外セレクトは、action_lockを待つ前にinteractionを受理する。

        action_lockはフェーズ切替のmute整列などで数秒以上握られる。先に
        deferしないとDiscordの3秒期限を越えて「応答しませんでした」になり、
        以降のsend_messageも Unknown interaction で失敗する。
        """
        player = Player(10, _member(10, "参加者"), number=0)
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            gm_id=99,
            game_run_id="",
            players={10: player},
            lobby_message=None,
        )
        lock = asyncio.Lock()
        cog = SimpleNamespace(
            state=state,
            action_lock=lock,
            is_current_game_view=lambda _run_id: True,
            _persist_room_state=AsyncMock(),
            _post_lobby_ui=AsyncMock(),
            _schedule_lobby_panel_recovery=Mock(),
        )
        order: list[str] = []
        view = RemovePlayerSelectView(
            cog, [discord.SelectOption(label="参加者", value="10")],
        )

        async def record_defer(**_kwargs) -> None:
            order.append("defer")

        initial = SimpleNamespace(
            user=SimpleNamespace(id=99),
            data={"values": ["10"]},
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=record_defer),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        # 進行中の処理がaction_lockを握ったままでも、受理だけは先に返る。
        await lock.acquire()
        task = asyncio.create_task(view.select_callback(initial))
        for _ in range(20):
            if order:
                break
            await asyncio.sleep(0)
        self.assertEqual(order, ["defer"], "ロック待ちより先にdeferしていない")
        self.assertFalse(task.done())
        order.append("lock-released")
        lock.release()
        await task

        # 応答済みなので、以降の返信はfollowup側だけを使う。
        initial.response.send_message.assert_not_awaited()
        initial.followup.send.assert_awaited()
        self.assertEqual(order, ["defer", "lock-released"])

    async def test_lobby_exclusion_edit_failure_reposts_and_schedules_retry(self) -> None:
        player = Player(10, _member(10, "参加者"), number=0)
        message = SimpleNamespace(
            edit=AsyncMock(
                side_effect=discord.NotFound(
                    SimpleNamespace(status=404, reason="gone"), "gone"
                )
            )
        )
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            gm_id=99,
            game_run_id="",
            players={10: player},
            lobby_message=message,
        )
        cog = SimpleNamespace(
            state=state,
            action_lock=asyncio.Lock(),
            is_current_game_view=lambda _run_id: True,
            _persist_room_state=AsyncMock(),
            _post_lobby_ui=AsyncMock(side_effect=RuntimeError("temporary")),
            _schedule_lobby_panel_recovery=Mock(),
        )
        view = RemovePlayerSelectView(
            cog, [discord.SelectOption(label="参加者", value="10")],
        )
        initial = SimpleNamespace(
            user=SimpleNamespace(id=99),
            data={"values": ["10"]},
            # ロック待ちで応答期限を越えないよう、選択の受理は defer が先。
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("views.LobbyView") as lobby_view:
            lobby_view.return_value._build_embed.return_value = object()
            await view.select_callback(initial)
            confirmation = initial.followup.send.await_args.kwargs["view"]
            confirm = SimpleNamespace(
                user=SimpleNamespace(id=99),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            await confirmation.confirm_btn.callback(confirm)

        self.assertNotIn(10, state.players)
        cog._persist_room_state.assert_awaited_once()
        cog._post_lobby_ui.assert_awaited_once()
        cog._schedule_lobby_panel_recovery.assert_called_once()
        self.assertIn("参加を取り消しました", confirm.followup.send.await_args.args[0])


    async def test_lobby_exclusion_reposts_panel_when_message_is_missing(self) -> None:
        """lobby_messageを失っていても、除外後に参加受付を掲示し直す。"""
        player = Player(10, _member(10, "参加者"), number=0)
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            gm_id=99,
            game_run_id="",
            players={10: player},
            lobby_message=None,
        )
        cog = SimpleNamespace(
            state=state,
            action_lock=asyncio.Lock(),
            is_current_game_view=lambda _run_id: True,
            _persist_room_state=AsyncMock(),
            _post_lobby_ui=AsyncMock(),
            _schedule_lobby_panel_recovery=Mock(),
        )
        view = RemovePlayerSelectView(
            cog, [discord.SelectOption(label="参加者", value="10")],
        )
        initial = SimpleNamespace(
            user=SimpleNamespace(id=99),
            data={"values": ["10"]},
            # ロック待ちで応答期限を越えないよう、選択の受理は defer が先。
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("views.LobbyView") as lobby_view:
            lobby_view.return_value._build_embed.return_value = object()
            await view.select_callback(initial)
            confirmation = initial.followup.send.await_args.kwargs["view"]
            confirm = SimpleNamespace(
                user=SimpleNamespace(id=99),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            await confirmation.confirm_btn.callback(confirm)

        self.assertNotIn(10, state.players)
        cog._post_lobby_ui.assert_awaited_once()
        cog._schedule_lobby_panel_recovery.assert_not_called()
        self.assertIn("参加を取り消しました", confirm.followup.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
