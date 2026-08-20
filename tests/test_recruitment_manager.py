"""募集の開催反映と通知をDiscord APIなしで検証する。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import database
from config import (
    CH_OPERATIONS,
    GM_ROLE_NAME,
    OPERATIONS_CATEGORY_NAME,
    RECRUITMENT_NOTIFICATION_ROLE_NAME,
    TEMP_GM_ROLE_NAME,
    Phase,
)
from recruitment import (
    RecruitmentCardView,
    RecruitmentCreateModal,
    RecruitmentManager,
    RecruitmentOptionsView,
    PlayerBlockSettingsView,
    RecruitmentScheduleView,
)
import recruitment as recruitment_lib
from room_config import RoomDefinition


class RecruitmentManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-recruitment-manager-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "manager.db")
        await database.init_db()
        self.room_id = "private_1"
        await database.save_private_room(
            1, self.room_id, 1, "GM村", "v13_cross",
        )
        await database.mark_private_room_active(
            1, self.room_id, category_id=101,
        )

        self.members = {
            user_id: SimpleNamespace(
                id=user_id, nick=None, display_name=f"user-{user_id}",
                send=AsyncMock(),
            )
            for user_id in [1, *range(100, 113)]
        }
        self.guild = SimpleNamespace(
            id=1, get_member=lambda user_id: self.members.get(user_id),
        )
        self.state = SimpleNamespace(
            phase=Phase.LOBBY, players={}, gm_id=None, recruitment_id=None,
            room_id=self.room_id, room_name="GM村", lobby_channel=None,
            pending_recruitment_reopen=False,
        )
        self.room = SimpleNamespace(
            state=self.state,
            room_def=SimpleNamespace(
                room_id=self.room_id,
                name="GM村",
                variant_id="v13_cross",
                private_owner_id=1,
                allowed_ranks=None, allowed_gm_user_ids=None, owner_only_gm=False,
                access_role_names=None, strict_access_role_names=None,
            ),
            is_private_room=lambda: True,
            action_lock=asyncio.Lock(),
            _is_game_in_progress=lambda: False,
            validate_gm_claim=AsyncMock(return_value=None),
            _persist_room_state=AsyncMock(),
            _post_lobby_ui=AsyncMock(),
        )
        self.cog = SimpleNamespace(rooms={self.room_id: self.room})
        self.manager = RecruitmentManager(SimpleNamespace(), self.cog)
        self.manager.channel = SimpleNamespace(guild=self.guild)
        self.manager.refresh_message = AsyncMock()
        self.manager.validate_candidate = AsyncMock(return_value=None)

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    async def _full_recruitment(self, *, gm_id=None) -> int:
        recruitment_id = await database.create_recruitment(
            1, 1, title="開催テスト",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id=self.room_id, variant_id="v13_cross",
            streaming=False, allowed_ranks=None,
        )
        for user_id in range(100, 113):
            await database.add_recruitment_entry(recruitment_id, user_id)
        if gm_id is not None:
            await database.set_recruitment_gm(recruitment_id, gm_id)
        return recruitment_id

    async def test_host_can_remove_an_entry_and_backup_is_promoted(self) -> None:
        """主催者の「参加者を除外」は、本人の参加取消と同じ繰り上げを通る。"""
        recruitment_id = await self._full_recruitment(gm_id=1)
        # 13人の定員を超えて登録し、補欠を1人作る
        await database.add_recruitment_entry(recruitment_id, 113)
        self.members[113] = SimpleNamespace(
            id=113, nick=None, display_name="user-113", send=AsyncMock(),
        )

        host_view = recruitment_lib.RecruitmentHostView(
            self.manager, recruitment_id, 1,
        )
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            data={"values": ["100"]},
        )
        remove_button = discord.utils.get(
            host_view.children, label="参加者を除外",
        )
        # 主催者以外は開けない
        await remove_button.callback(interaction)

        self.assertIn(
            "主催者だけ", interaction.response.send_message.await_args.args[0],
        )

        host_interaction = SimpleNamespace(
            user=self.members[1],
            guild=self.guild,
            response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            data={"values": ["100"]},
        )
        await remove_button.callback(host_interaction)
        select_view = host_interaction.response.send_message.await_args.kwargs["view"]
        # 主催者自身は選択肢に出さない (GM不在の募集が残ってしまうため)
        self.assertNotIn(
            "1", {option.value for option in select_view.children[0].options},
        )

        await select_view._selected(host_interaction)

        entries = await database.list_recruitment_entries(recruitment_id)
        by_user = {int(e["user_id"]): e["kind"] for e in entries}
        self.assertNotIn(100, by_user)
        # 空いた参加枠へ補欠が繰り上がり、本人へDMが飛ぶ
        self.assertEqual(by_user[113], "参加")
        self.members[113].send.assert_awaited()

    async def test_reopen_previous_recruitment_copies_settings_only(self) -> None:
        source = await database.create_recruitment(
            1,
            1,
            title="前回の村",
            scheduled_at=datetime.now(timezone.utc) - timedelta(hours=2),
            room_id=self.room_id,
            variant_id="v9_cross",
            streaming=True,
            allowed_ranks={"ゴールド"},
            note="前回備考",
        )
        await database.add_recruitment_entry(source, 100)
        self.assertTrue(await database.set_recruitment_status(
            source, database.RECRUITMENT_ARCHIVED,
        ))
        self.room.room_def = RoomDefinition(
            self.room_id,
            "GM村",
            private_owner_id=1,
            variant_id="v13_cross",
        )
        self.state.guild = self.guild
        host = MagicMock(spec=discord.Member)
        host.id = 1
        host.roles = [SimpleNamespace(name=GM_ROLE_NAME)]
        interaction = SimpleNamespace(guild=self.guild, user=host)
        self.manager.publish_new_recruitment = AsyncMock()

        result = await self.manager.reopen_previous_recruitment(
            interaction, self.room,
        )

        self.assertIn("再開しました", result)
        self.assertIsNotNone(self.state.recruitment_id)
        self.assertNotEqual(self.state.recruitment_id, source)
        self.assertEqual(self.state.gm_id, 1)
        new_row = await database.get_recruitment(self.state.recruitment_id)
        self.assertEqual(new_row["title"], "前回の村")
        self.assertEqual(new_row["variant_id"], "v9_cross")
        self.assertTrue(new_row["streaming"])
        self.assertEqual(new_row["allowed_ranks"], frozenset({"ゴールド"}))
        self.assertEqual(new_row["note"], "前回備考")
        self.assertEqual(
            await database.list_recruitment_entries(self.state.recruitment_id),
            [],
        )
        self.assertEqual(
            (await database.get_recruitment(source))["status"],
            database.RECRUITMENT_ARCHIVED,
        )
        self.manager.publish_new_recruitment.assert_awaited_once_with(
            self.guild, self.state.recruitment_id, announce=False,
        )

    async def test_force_end_recovery_keeps_delegated_gm_and_publishes_empty_card(self) -> None:
        source = await database.create_recruitment(
            1,
            1,
            title="前回の村",
            scheduled_at=datetime.now(timezone.utc) - timedelta(hours=2),
            room_id=self.room_id,
            variant_id="v9_turn",
            streaming=False,
            allowed_ranks=None,
        )
        self.assertTrue(await database.set_recruitment_status(
            source, database.RECRUITMENT_ARCHIVED,
        ))
        self.room.room_def = RoomDefinition(
            self.room_id,
            "GM村",
            private_owner_id=1,
            variant_id="v13_cross",
        )
        self.state.guild = self.guild
        self.state.gm_id = 2
        self.state.pending_recruitment_reopen = True
        self.manager.ensure_recruitment_message = AsyncMock()

        result = await self.manager.recover_pending_recruitment(self.room)

        self.assertIn("参加者0人", result)
        self.assertFalse(self.state.pending_recruitment_reopen)
        self.assertEqual(self.state.gm_id, 2)
        row = await database.get_recruitment(self.state.recruitment_id)
        self.assertEqual(row["gm_id"], 2)
        self.assertEqual(row["variant_id"], "v9_turn")
        self.assertEqual(await database.list_recruitment_entries(row["id"]), [])
        self.manager.ensure_recruitment_message.assert_awaited_once()

    async def test_force_end_recovery_adopts_open_row_after_crash(self) -> None:
        source = await database.create_recruitment(
            1,
            1,
            title="前回の村",
            scheduled_at=datetime.now(timezone.utc) - timedelta(hours=2),
            room_id=self.room_id,
            variant_id="v9_cross",
            streaming=False,
            allowed_ranks=None,
        )
        self.assertTrue(await database.set_recruitment_status(
            source, database.RECRUITMENT_ARCHIVED,
        ))
        existing_id, _ = await database.create_recruitment_from_previous_settings(
            1,
            self.room_id,
            1,
            scheduled_at=datetime.now(timezone.utc),
            gm_id=2,
        )
        self.room.room_def = RoomDefinition(
            self.room_id,
            "GM村",
            private_owner_id=1,
            variant_id="v13_cross",
        )
        self.state.guild = self.guild
        self.state.gm_id = 2
        self.state.recruitment_id = None
        self.state.pending_recruitment_reopen = True
        self.manager.ensure_recruitment_message = AsyncMock()

        await self.manager.recover_pending_recruitment(self.room)

        self.assertEqual(self.state.recruitment_id, existing_id)
        self.assertFalse(self.state.pending_recruitment_reopen)
        open_rows = await database.list_open_recruitments(1)
        self.assertEqual([row["id"] for row in open_rows], [existing_id])

    async def test_force_end_recovery_cancels_when_gm_rebuilt_the_roster(self) -> None:
        """「次村」で参加者を組み直した後は、0人カードの再掲待ちを解除する。"""
        self.room.room_def = RoomDefinition(
            self.room_id,
            "GM村",
            private_owner_id=1,
            variant_id="v13_cross",
        )
        self.state.guild = self.guild
        self.state.gm_id = 2
        self.state.players = {100: object()}
        self.state.pending_recruitment_reopen = True
        self.state.lobby_return_mode = "gm"
        self.manager.ensure_recruitment_message = AsyncMock()

        result = await self.manager.recover_pending_recruitment(self.room)

        self.assertIn("取り消しました", result)
        self.assertFalse(self.state.pending_recruitment_reopen)
        self.assertEqual(self.state.lobby_return_mode, "empty")
        self.assertIsNone(self.state.recruitment_id)
        self.manager.ensure_recruitment_message.assert_not_awaited()
        self.room._persist_room_state.assert_awaited_once()

    async def test_host_menu_timeout_disables_controls_and_points_back_to_card(self) -> None:
        view = recruitment_lib.RecruitmentHostView(self.manager, 10, 1)
        view.message = SimpleNamespace(edit=AsyncMock())

        await view.on_timeout()

        self.assertTrue(all(item.disabled for item in view.children))
        kwargs = view.message.edit.await_args.kwargs
        self.assertIn("開き直してください", kwargs["content"])
        self.assertIs(kwargs["view"], view)

    async def test_valid_held_lobby_recruitment_is_not_archived_as_orphan(self) -> None:
        recruitment_id = await self._full_recruitment(gm_id=1)
        self.assertTrue(await database.set_recruitment_status(
            recruitment_id, database.RECRUITMENT_HELD,
        ))
        self.state.recruitment_id = recruitment_id
        self.state.gm_id = 1
        self.state.players = {
            user_id: object() for user_id in range(100, 113)
        }

        await self.manager._archive_orphaned_held_recruitments(self.guild)

        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_HELD)

    async def test_player_block_view_pages_all_candidates_and_registered_users(self) -> None:
        members = [
            SimpleNamespace(
                id=user_id,
                display_name=f"user-{user_id:03d}",
                name=f"user-{user_id:03d}",
                bot=False,
            )
            for user_id in range(1, 62)
        ]
        by_id = {member.id: member for member in members}
        guild = SimpleNamespace(
            id=1,
            members=members,
            get_member=lambda user_id: by_id.get(user_id),
        )
        manager = SimpleNamespace(
            bot=SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 1 else None),
            notify_block_added=AsyncMock(),
        )

        candidate_ids: set[int] = set()
        for page in range(3):
            view = PlayerBlockSettingsView(
                manager, 1, 1, [], add_page=page,
            )
            select = discord.utils.get(
                view.children, custom_id="player_blocks:add_page",
            )
            self.assertIsNotNone(select)
            candidate_ids.update(int(option.value) for option in select.options)
        self.assertEqual(candidate_ids, set(range(2, 62)))

        blocked_ids = list(range(2, 52))
        visible_blocked_ids: set[int] = set()
        for page in range(2):
            view = PlayerBlockSettingsView(
                manager, 1, 1, blocked_ids, remove_page=page,
            )
            select = discord.utils.get(
                view.children, custom_id="player_blocks:remove",
            )
            self.assertIsNotNone(select)
            visible_blocked_ids.update(int(option.value) for option in select.options)
        self.assertEqual(visible_blocked_ids, set(blocked_ids))

    async def _insert_disabled_recruitment(
        self,
        *,
        status: str = database.RECRUITMENT_OPEN,
        room_id: str = "open_9_cross",
        variant_id: str = "v9_cross",
    ) -> int:
        """現行APIが拒否する無効固定卓の異常DB行を安全性テスト用に再現する。"""
        variant = recruitment_lib.VARIANT_DEFINITIONS[variant_id]
        async with database.connect_db() as db:
            cursor = await db.execute(
                "INSERT INTO recruitments "
                "(guild_id, host_id, title, scheduled_at, room_id, streaming, "
                "status, variant_id, capacity, backup_capacity, occupancy_minutes) "
                "VALUES (1, 1, '旧固定卓募集', ?, ?, 0, ?, ?, ?, 3, ?)",
                (
                    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    room_id,
                    status,
                    variant_id,
                    variant.player_count,
                    variant.recruitment_occupancy_minutes,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    def _install_private_room(
        self,
        room_id: str,
        *,
        variant_id: str,
        lobby_channel=None,
    ):
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={},
            gm_id=1,
            recruitment_id=None,
            room_id=room_id,
            room_name="GM村",
            lobby_channel=lobby_channel,
            lobby_message=None,
            guild=self.guild,
        )
        room = SimpleNamespace(
            state=state,
            room_def=SimpleNamespace(
                room_id=room_id,
                name="GM村",
                variant_id=variant_id,
                private_owner_id=1,
                allowed_ranks=None,
                allowed_gm_user_ids=None,
                access_role_names=None,
                strict_access_role_names=None,
            ),
            is_private_room=lambda: True,
            action_lock=asyncio.Lock(),
            _is_game_in_progress=lambda: False,
            _persist_room_state=AsyncMock(),
            _post_lobby_ui=AsyncMock(),
        )
        self.cog.rooms[room_id] = room
        return room

    def _interaction(self):
        return SimpleNamespace(
            user=self.members[1], guild=self.guild,
        )

    async def test_start_registers_thirteen_and_auto_host_gm(self) -> None:
        recruitment_id = await self._full_recruitment()
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("13人とGM", result)
        self.assertEqual(len(self.state.players), 13)
        self.assertEqual(self.state.gm_id, 1)
        self.assertEqual(self.state.recruitment_id, recruitment_id)
        self.room._persist_room_state.assert_awaited_once()
        self.room._post_lobby_ui.assert_awaited_once()
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_HELD)

    async def test_auto_host_gm_condition_failure_stops_before_lobby_change(self) -> None:
        recruitment_id = await self._full_recruitment()
        self.room.validate_gm_claim.return_value = "この卓のGM条件外です。"
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("GM user-1", result)
        self.assertEqual(self.state.players, {})
        self.assertIsNone(self.state.gm_id)
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_OPEN)

    async def test_rank_recheck_failure_stops_before_lobby_change(self) -> None:
        recruitment_id = await self._full_recruitment()

        async def validate(_guild, _row, member):
            return "開催時ランク条件外" if member.id == 105 else None

        self.manager.validate_candidate.side_effect = validate
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("user-105", result)
        self.assertEqual(self.state.players, {})
        self.room._persist_room_state.assert_not_awaited()

    async def test_nonempty_lobby_requires_explicit_reset(self) -> None:
        recruitment_id = await self._full_recruitment()
        self.state.players[999] = object()
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertEqual(result, "LOBBY_NOT_EMPTY")
        self.room._persist_room_state.assert_not_awaited()

        result = await self.manager.transfer(
            self._interaction(), recruitment_id, reset_lobby=True,
        )
        self.assertIn("13人とGM", result)
        self.assertNotIn(999, self.state.players)

    async def test_card_transfer_confirms_only_after_game_start_succeeds(self) -> None:
        """開始前ではなく、LOBBY離脱とゲームループ設定後だけ成功を返す。"""
        recruitment_id = await self._full_recruitment()
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[1],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def start_game(_interaction) -> None:
            self.state.phase = Phase.PREPARATION
            self.state.game_task = object()

        self.room.start_game = AsyncMock(side_effect=start_game)

        await card.transfer(interaction)

        self.room.start_game.assert_awaited_once_with(interaction)
        interaction.followup.send.assert_awaited_once_with(
            "✅ ゲーム開始処理へ進みました。", ephemeral=True,
        )

    async def test_card_transfer_leaves_start_rejection_to_room_runner(self) -> None:
        recruitment_id = await self._full_recruitment()
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[1],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def reject_start(start_interaction) -> None:
            await start_interaction.followup.send(
                "村の状態が変わったためゲームを開始しませんでした。",
                ephemeral=True,
            )

        self.room.start_game = AsyncMock(side_effect=reject_start)

        await card.transfer(interaction)

        self.room.start_game.assert_awaited_once_with(interaction)
        interaction.followup.send.assert_awaited_once_with(
            "村の状態が変わったためゲームを開始しませんでした。",
            ephemeral=True,
        )

    async def test_reset_confirmation_starts_game_after_transfer(self) -> None:
        """受付リセット確認も通常の開催反映後と同じ開始経路へ進む。"""
        recruitment_id = await self._full_recruitment()
        self.state.players[999] = object()
        view = recruitment_lib.RecruitmentLobbyResetConfirmView(
            self.manager, recruitment_id, self.members[1].id,
        )
        interaction = SimpleNamespace(
            user=self.members[1],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        async def start_game(_interaction) -> None:
            self.state.phase = Phase.PREPARATION
            self.state.game_task = object()

        self.room.start_game = AsyncMock(side_effect=start_game)
        confirm = discord.utils.get(view.children, label="受付をリセットして開催")

        await confirm.callback(interaction)

        self.assertNotIn(999, self.state.players)
        self.room.start_game.assert_awaited_once_with(interaction)
        interaction.followup.send.assert_awaited_once_with(
            "✅ ゲーム開始処理へ進みました。", ephemeral=True,
        )

    async def test_fly_in_lobby_entry_during_validation_is_not_silently_cleared(self) -> None:
        recruitment_id = await self._full_recruitment()
        injected = False

        async def validate(_guild, _row, _member):
            nonlocal injected
            if not injected:
                injected = True
                self.state.players[999] = object()
            return None

        self.manager.validate_candidate.side_effect = validate

        result = await self.manager.transfer(self._interaction(), recruitment_id)

        self.assertEqual(result, "LOBBY_NOT_EMPTY")
        self.assertIn(999, self.state.players)
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_OPEN)

    async def test_start_can_retry_after_lobby_checkpoint_failure(self) -> None:
        recruitment_id = await self._full_recruitment()
        self.room._persist_room_state.side_effect = RuntimeError("DB down")

        with self.assertRaisesRegex(RuntimeError, "DB down"):
            await self.manager.transfer(self._interaction(), recruitment_id)

        self.assertEqual(self.state.players, {})
        self.assertIsNone(self.state.gm_id)
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_HELD)

        self.room._persist_room_state.side_effect = None
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("13人とGM", result)
        self.assertEqual(len(self.state.players), 13)

    async def test_due_notification_is_sent_once_and_gm_warning_goes_to_host(self) -> None:
        recruitment_id = await database.create_recruitment(
            1, 1, title="通知テスト",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            room_id=self.room_id, variant_id="v13_cross",
            streaming=False, allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        await self.manager.process_notifications(self.guild)
        self.members[100].send.assert_awaited_once()
        self.members[1].send.assert_awaited_once()
        await self.manager.process_notifications(self.guild)
        self.members[100].send.assert_awaited_once()
        self.members[1].send.assert_awaited_once()

    async def test_notification_just_after_start_is_still_sent_once(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc)
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="今すぐ通知",
            scheduled_at=now - timedelta(seconds=1),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)

        await self.manager.process_notifications(self.guild, now=now)
        await self.manager.process_notifications(self.guild, now=now + timedelta(minutes=1))

        self.members[100].send.assert_awaited_once()
        self.assertIn("開催予定時刻を迎えています", self.members[100].send.await_args.args[0])

    async def test_late_participant_is_notified_without_waiting_for_next_poll(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="後参加通知",
            scheduled_at=now + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        await self.manager.process_notifications(self.guild, now=now)
        await database.add_recruitment_entry(recruitment_id, 101)

        sent = await self.manager.notify_participant_if_due(
            self.guild, recruitment_id, 101, now=now,
        )
        await self.manager.process_notifications(self.guild, now=now)

        self.assertTrue(sent)
        self.members[100].send.assert_awaited_once()
        self.members[101].send.assert_awaited_once()

    async def test_immediate_and_periodic_notification_are_serialized(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="通知競合",
            scheduled_at=now + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)

        await asyncio.gather(
            self.manager.notify_participant_if_due(
                self.guild, recruitment_id, 100, now=now,
            ),
            self.manager.process_notifications(self.guild, now=now),
        )

        self.members[100].send.assert_awaited_once()

    async def test_cancel_committed_before_poll_lock_prevents_stale_dm(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="取消競合",
            scheduled_at=now + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)

        await self.manager.lock.acquire()
        try:
            polling = asyncio.create_task(
                self.manager.process_notifications(self.guild, now=now)
            )
            await asyncio.sleep(0)
            await database.remove_recruitment_entry(recruitment_id, 100)
        finally:
            self.manager.lock.release()
        await polling

        self.members[100].send.assert_not_awaited()

    async def test_forbidden_notification_is_not_retried_every_poll(self) -> None:
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="DM拒否",
            scheduled_at=now + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied",
        )
        self.members[100].send.side_effect = denied

        await self.manager.process_notifications(self.guild, now=now)
        await self.manager.process_notifications(self.guild, now=now + timedelta(minutes=1))

        self.members[100].send.assert_awaited_once()
        self.assertEqual(
            await database.list_pending_recruitment_notification_user_ids(recruitment_id),
            [],
        )

    async def test_temporary_failure_is_retried_after_start_while_slot_is_active(
        self,
    ) -> None:
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="開催直前の一時失敗",
            scheduled_at=now + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        temporary = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable", headers={}),
            "retry",
        )
        self.members[100].send.side_effect = [temporary, None]

        sent = await self.manager.notify_participant_if_due(
            self.guild, recruitment_id, 100, now=now,
        )
        self.assertFalse(sent)
        self.assertTrue(
            await database.set_recruitment_status(
                recruitment_id, database.RECRUITMENT_HELD
            )
        )

        await self.manager.process_notifications(
            self.guild, now=now + timedelta(minutes=1)
        )

        self.assertEqual(self.members[100].send.await_count, 2)
        self.assertEqual(
            await database.list_pending_recruitment_notification_user_ids(recruitment_id),
            [],
        )

    async def test_notification_failure_does_not_turn_successful_join_into_error(self) -> None:
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="参加成功優先",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        self.manager.notify_participant_if_due = AsyncMock(
            side_effect=RuntimeError("notification DB down")
        )
        self.manager.build_embed = AsyncMock(return_value=discord.Embed())
        self.manager.notify_ready_if_needed = AsyncMock()

        with patch.object(recruitment_lib.discord, "Member", SimpleNamespace):
            await card.join(interaction)

        entries = await database.list_recruitment_entries(recruitment_id)
        self.assertEqual([entry["user_id"] for entry in entries], [100])
        self.assertIn("参加として登録しました", interaction.followup.send.await_args.args[0])
        self.manager.notify_participant_if_due.assert_awaited_once_with(
            self.guild, recruitment_id, 100
        )

    async def test_join_succeeds_when_stale_card_was_deleted_after_db_update(self) -> None:
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="削除済みカードへの参加",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found", headers={}), "missing",
        )
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock(side_effect=missing)),
        )
        self.manager.build_embed = AsyncMock(return_value=discord.Embed())
        self.manager.notify_ready_if_needed = AsyncMock()

        with patch.object(recruitment_lib.discord, "Member", SimpleNamespace):
            await card.join(interaction)

        self.assertEqual(
            [entry["user_id"] for entry in await database.list_recruitment_entries(recruitment_id)],
            [100],
        )
        self.assertIn("参加として登録しました", interaction.followup.send.await_args.args[0])

    async def test_leave_succeeds_when_stale_card_was_deleted_after_db_update(self) -> None:
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="削除済みカードからの取消",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found", headers={}), "missing",
        )
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock(side_effect=missing)),
        )
        self.manager.build_embed = AsyncMock(return_value=discord.Embed())

        await card.leave(interaction)

        self.assertEqual(await database.list_recruitment_entries(recruitment_id), [])
        self.assertIn("参加を取り消しました", interaction.followup.send.await_args.args[0])

    async def test_entry_change_succeeds_when_card_refresh_is_unavailable(self) -> None:
        """削除済み以外のDiscord API失敗でも、DB確定済みの応答を優先する。"""
        refresh_failures = (
            (
                "join",
                discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden", headers={}),
                    "denied",
                ),
                "参加として登録しました",
            ),
            (
                "leave",
                discord.HTTPException(
                    SimpleNamespace(status=503, reason="Unavailable", headers={}),
                    "retry",
                ),
                "参加を取り消しました",
            ),
        )
        for action, refresh_error, success_text in refresh_failures:
            with self.subTest(action=action):
                recruitment_id = await database.create_recruitment(
                    1,
                    1,
                    title=f"カード更新失敗 {action}",
                    scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    room_id=self.room_id,
                    variant_id="v13_cross",
                    streaming=False,
                    allowed_ranks=None,
                )
                if action == "leave":
                    await database.add_recruitment_entry(recruitment_id, 100)
                card = RecruitmentCardView(self.manager, recruitment_id)
                interaction = SimpleNamespace(
                    user=self.members[100],
                    guild=self.guild,
                    response=SimpleNamespace(
                        defer=AsyncMock(), send_message=AsyncMock(),
                    ),
                    followup=SimpleNamespace(send=AsyncMock()),
                    message=SimpleNamespace(
                        edit=AsyncMock(side_effect=refresh_error),
                    ),
                )
                self.manager.build_embed = AsyncMock(return_value=discord.Embed())
                self.manager.notify_ready_if_needed = AsyncMock()

                if action == "join":
                    with patch.object(recruitment_lib.discord, "Member", SimpleNamespace):
                        await card.join(interaction)
                    entries = await database.list_recruitment_entries(recruitment_id)
                    self.assertEqual([entry["user_id"] for entry in entries], [100])
                else:
                    await card.leave(interaction)
                    self.assertEqual(
                        await database.list_recruitment_entries(recruitment_id), [],
                    )
                self.assertIn(success_text, interaction.followup.send.await_args.args[0])
                self.assertTrue(await database.set_recruitment_status(
                    recruitment_id, database.RECRUITMENT_ARCHIVED,
                ))

    async def test_join_revalidates_current_conditions_inside_manager_lock(self) -> None:
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="変更後は参加不可",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        self.manager.validate_candidate = AsyncMock(
            side_effect=[None, "形式変更後のランク条件を満たしていません。"],
        )

        with patch.object(recruitment_lib.discord, "Member", SimpleNamespace):
            await card.join(interaction)

        self.assertEqual(await database.list_recruitment_entries(recruitment_id), [])
        self.assertEqual(self.manager.validate_candidate.await_count, 2)
        self.assertIn(
            "形式変更後のランク条件を満たしていません。",
            interaction.followup.send.await_args.args[0],
        )

    async def test_promoted_backup_is_connected_to_immediate_notification(self) -> None:
        recruitment_id = await self._full_recruitment()
        promoted = SimpleNamespace(
            id=200, nick=None, display_name="user-200", send=AsyncMock()
        )
        self.members[200] = promoted
        await database.add_recruitment_entry(recruitment_id, 200)
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        self.manager.notify_participant_if_due = AsyncMock(return_value=True)
        self.manager.build_embed = AsyncMock(return_value=discord.Embed())

        await card.leave(interaction)

        promoted.send.assert_awaited_once_with("補欠から参加者へ繰り上がりました。")
        self.manager.notify_participant_if_due.assert_awaited_once_with(
            self.guild, recruitment_id, 200
        )

    async def test_participant_leave_updates_roster_without_access_role(self) -> None:
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="公開村の参加取消",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id=self.room_id,
            variant_id="v13_cross",
            streaming=False,
            allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = SimpleNamespace(
            user=self.members[100],
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await card.leave(interaction)

        entries = await database.list_recruitment_entries(recruitment_id)
        self.assertEqual(entries, [])
        self.assertIn("参加を取り消しました", interaction.followup.send.await_args.args[0])
        interaction.message.edit.assert_awaited_once()

    async def test_operations_channel_preserves_manual_view_allows(self) -> None:
        class Target:
            def __init__(self, target_id: int, *, administrator: bool = False) -> None:
                self.id = target_id
                self.permissions = discord.Permissions.none()
                self.permissions.administrator = administrator

        category = SimpleNamespace(name=OPERATIONS_CATEGORY_NAME)
        default = Target(1)
        bot_member = Target(2)
        stale = Target(3)
        administrator = Target(4, administrator=True)
        channel = SimpleNamespace(
            name=CH_OPERATIONS,
            category=category,
            overwrites={
                stale: discord.PermissionOverwrite(view_channel=True, manage_messages=True),
                administrator: discord.PermissionOverwrite(view_channel=True),
            },
        )

        async def edit_channel(*, overwrites, reason=None):
            channel.overwrites = dict(overwrites)
            return channel

        channel.edit = AsyncMock(side_effect=edit_channel)
        guild = SimpleNamespace(
            categories=[category],
            text_channels=[channel],
            default_role=default,
            me=bot_member,
            create_text_channel=AsyncMock(),
        )

        result = await self.manager._ensure_operations_channel(guild)

        self.assertIs(result, channel)
        channel.edit.assert_awaited_once()
        self.assertNotIn(default, channel.overwrites)
        self.assertTrue(channel.overwrites[bot_member].view_channel)
        self.assertTrue(channel.overwrites[stale].view_channel)
        self.assertTrue(channel.overwrites[stale].manage_messages)
        self.assertTrue(channel.overwrites[administrator].view_channel)

    async def test_operations_channel_edit_failure_is_fail_closed(self) -> None:
        class Target:
            def __init__(self, target_id: int) -> None:
                self.id = target_id

        category = SimpleNamespace(name=OPERATIONS_CATEGORY_NAME)
        default = Target(1)
        bot_member = Target(2)
        stale = Target(3)
        channel = SimpleNamespace(
            name=CH_OPERATIONS,
            category=category,
            overwrites={
                stale: discord.PermissionOverwrite(view_channel=True),
            },
            edit=AsyncMock(
                side_effect=discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden", headers={}),
                    "denied",
                )
            ),
        )
        guild = SimpleNamespace(
            categories=[category],
            text_channels=[channel],
            default_role=default,
            me=bot_member,
            create_text_channel=AsyncMock(),
        )

        result = await self.manager._ensure_operations_channel(guild)

        self.assertIsNone(result)
        channel.edit.assert_awaited_once()

    async def test_new_recruitment_is_archived_if_card_cannot_be_published(self) -> None:
        room_id = "private_1"
        recruitment_id = await database.create_recruitment(
            1, 1, title="掲示失敗テスト",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id=room_id, variant_id="v13_cross",
            streaming=False, allowed_ranks=None,
        )
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied"
        )
        lobby_channel = SimpleNamespace(
            guild=self.guild,
            send=AsyncMock(side_effect=denied),
        )
        self._install_private_room(
            room_id, variant_id="v13_cross", lobby_channel=lobby_channel,
        )

        with self.assertRaisesRegex(RuntimeError, "作成を取り消しました"):
            await self.manager.publish_new_recruitment(self.guild, recruitment_id)

        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_ARCHIVED)

    async def test_disabled_recruitment_is_never_published(self) -> None:
        recruitment_id = await self._insert_disabled_recruitment()
        channel = SimpleNamespace(guild=self.guild, send=AsyncMock())
        self.manager.channel = channel

        with self.assertRaisesRegex(RuntimeError, "作成を取り消しました"):
            await self.manager.publish_new_recruitment(self.guild, recruitment_id)

        channel.send.assert_not_awaited()
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_ARCHIVED)

    async def test_existing_disabled_card_is_deleted(self) -> None:
        recruitment_id = await self._insert_disabled_recruitment()
        await database.set_recruitment_message_id(recruitment_id, 999)
        message = SimpleNamespace(delete=AsyncMock())
        self.manager.channel = SimpleNamespace(
            guild=self.guild,
            fetch_message=AsyncMock(return_value=message),
        )
        self._install_private_room(
            "open_9_cross",
            variant_id="v9_cross",
            lobby_channel=self.manager.channel,
        )
        row = await database.get_recruitment(recruitment_id)

        await self.manager.ensure_recruitment_message(self.guild, row)

        message.delete.assert_awaited_once()
        self.assertIsNone((await database.get_recruitment(recruitment_id))["message_id"])

    async def test_setup_removes_held_rollout_card_after_room_is_hidden_again(self) -> None:
        recruitment_id = await self._insert_disabled_recruitment(
            status=database.RECRUITMENT_HELD,
        )
        await database.set_recruitment_message_id(recruitment_id, 1001)
        message = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(
            guild=self.guild,
            fetch_message=AsyncMock(return_value=message),
        )
        self._install_private_room(
            "open_9_cross", variant_id="v9_cross", lobby_channel=channel,
        )
        self.manager._ensure_operations_channel = AsyncMock(return_value=None)
        self.manager._ensure_operations_log_channel = AsyncMock(return_value=None)
        self.manager._upsert_panel = AsyncMock()

        await self.manager.setup(self.guild)

        message.delete.assert_awaited_once()
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_ARCHIVED)
        self.assertIsNone(row["message_id"])

    async def test_notification_loop_retries_hidden_card_after_discord_error(self) -> None:
        recruitment_id = await self._insert_disabled_recruitment(
            status=database.RECRUITMENT_HELD,
        )
        await database.set_recruitment_message_id(recruitment_id, 1002)
        unavailable = discord.HTTPException(
            SimpleNamespace(
                status=503, reason="Service Unavailable", headers={},
            ),
            "retry",
        )
        message = SimpleNamespace(
            delete=AsyncMock(side_effect=[unavailable, None]),
        )
        self.manager.channel = SimpleNamespace(
            guild=self.guild,
            fetch_message=AsyncMock(return_value=message),
        )
        self._install_private_room(
            "open_9_cross",
            variant_id="v9_cross",
            lobby_channel=self.manager.channel,
        )
        await self.manager.process_notifications(self.guild)
        self.assertEqual(
            (await database.get_recruitment(recruitment_id))["message_id"], 1002,
        )

        await self.manager.process_notifications(self.guild)
        self.assertEqual(message.delete.await_count, 2)
        self.assertIsNone(
            (await database.get_recruitment(recruitment_id))["message_id"]
        )

    async def test_stale_hidden_card_rejects_host_menu(self) -> None:
        """削除失敗で残ったカードからも、段階導入中卓を操作できない。"""
        room_id = "private_hidden"
        recruitment_id = await self._insert_disabled_recruitment(
            room_id=room_id, variant_id="v13_turn",
        )
        self._install_private_room(room_id, variant_id="v13_turn")
        card = RecruitmentCardView(self.manager, recruitment_id)
        menu_interaction = SimpleNamespace(
            user=self.members[1],
            guild=self.guild,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await card.host_menu(menu_interaction)

        text = menu_interaction.response.send_message.await_args.args[0]
        self.assertIn("段階導入中", text)
        self.assertIn("主催者メニュー", text)

    async def test_embed_uses_saved_variant_capacity_and_occupancy(self) -> None:
        self._install_private_room("private_1", variant_id="v9_cross")
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="9人snapshot",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id="private_1",
            variant_id="v9_cross",
            streaming=False,
            allowed_ranks=None,
        )
        embed = await self.manager.build_embed(self.guild, recruitment_id)
        self.assertIn("定員: **9人**", embed.description)
        self.assertIn("占有時間: 90分", embed.footer.text)

    async def test_public_nine_player_recruitment_is_published(self) -> None:
        channel = SimpleNamespace(guild=self.guild, send=AsyncMock())
        self._install_private_room(
            "private_1", variant_id="v9_cross", lobby_channel=channel,
        )
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="公開9人募集",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id="private_1",
            variant_id="v9_cross",
            streaming=False,
            allowed_ranks=None,
        )
        message = SimpleNamespace(id=900, delete=AsyncMock())
        channel.send.return_value = message
        self.manager._notify_new_recruitment = AsyncMock()

        await self.manager.publish_new_recruitment(self.guild, recruitment_id)

        channel.send.assert_awaited_once()
        self.assertEqual(
            (await database.get_recruitment(recruitment_id))["message_id"], 900,
        )

    async def test_config_drift_blocks_start_without_touching_lobby(self) -> None:
        recruitment_id = await self._full_recruitment()
        current = recruitment_lib.VARIANT_DEFINITIONS["v13_cross"]
        with patch.dict(
            recruitment_lib.VARIANT_DEFINITIONS,
            {"v13_cross": replace(current, player_count=9)},
        ):
            result = await self.manager.transfer(
                self._interaction(), recruitment_id,
            )
        self.assertIn("現在の卓設定が一致しません", result)
        self.assertEqual(self.state.players, {})
        self.room._persist_room_state.assert_not_awaited()

    async def test_hidden_variant_ready_dm_is_suppressed(self) -> None:
        room_id = "private_hidden"
        recruitment_id = await self._insert_disabled_recruitment(
            room_id=room_id, variant_id="v13_turn",
        )
        self._install_private_room(room_id, variant_id="v13_turn")
        for user_id in range(100, 113):
            await database.add_recruitment_entry(recruitment_id, user_id)
        await database.set_recruitment_gm(recruitment_id, 1)
        row = await database.get_recruitment(recruitment_id)

        await self.manager.notify_ready_if_needed(row)

        self.members[1].send.assert_not_awaited()

    async def test_stale_room_creation_form_without_variant_is_rejected(self) -> None:
        class FakeMember:
            def __init__(self) -> None:
                self.id = 1
                self.roles = [SimpleNamespace(name=GM_ROLE_NAME)]
                self.guild_permissions = SimpleNamespace(administrator=True)

        tomorrow = datetime.now(recruitment_lib.JST) + timedelta(days=1)
        modal = RecruitmentCreateModal(
            self.manager,
            1,
            {
                "date": tomorrow.date().isoformat(),
                "hour": str(tomorrow.hour),
                "minute": "0",
                "room": "open_9_cross",
                "streaming": "0",
                "allowed_ranks": [],
            },
        )
        interaction = SimpleNamespace(
            user=FakeMember(),
            guild=SimpleNamespace(id=1, owner_id=1),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(
            recruitment_lib.discord, "Member", FakeMember,
        ):
            await modal.on_submit(interaction)
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("ゲーム形式が見つからない", text)
        self.assertIn("作成できません", text)

    async def test_temp_gm_can_open_recruitment_creation(self) -> None:
        class FakeMember:
            def __init__(self) -> None:
                self.id = 1
                self.roles = [SimpleNamespace(name=TEMP_GM_ROLE_NAME)]
                self.send = AsyncMock()

        interaction = SimpleNamespace(
            user=FakeMember(),
            guild=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(recruitment_lib.discord, "Member", FakeMember):
            await self.manager.start_village_creation(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.user.send.assert_awaited_once()
        self.assertIsInstance(
            interaction.followup.send.await_args.kwargs["view"], RecruitmentScheduleView
        )

    async def test_operations_block_notice_contains_no_mentions(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        self.manager.operations_channel = channel
        await database.add_player_block(1, 100, 101)
        await self.manager.notify_block_added(self.guild, 100, 101, 1)
        text = channel.send.await_args.args[0]
        self.assertNotIn("<@", text)
        self.assertIn("ID: 100", text)
        self.assertIn("ID: 101", text)

    async def test_integrated_card_and_rank_preset_options(self) -> None:
        card = RecruitmentCardView(self.manager, 42)
        self.assertIsNone(card.timeout)
        self.assertEqual(
            {item.custom_id for item in card.children},
            {
                "recruitment:42:join", "recruitment:42:leave",
                "recruitment:42:transfer", "recruitment:42:host",
                "recruitment:42:notify",
                # 受付中のGM村で規定を読める唯一の導線
                "recruitment:42:rule", "recruitment:42:help",
                "recruitment:42:call",
            },
        )
        # 受付が閉じても、読むだけのルール・ヘルプは押せるままにする
        closed = RecruitmentCardView(self.manager, 42, active=False)
        enabled = {
            item.custom_id for item in closed.children if not item.disabled
        }
        self.assertEqual(
            enabled, {"recruitment:42:rule", "recruitment:42:help"},
        )
        base = {
            "date": "2026-08-03", "hour": "20", "minute": "0",
            "streaming": "0",
        }
        unrestricted = RecruitmentOptionsView(
            self.manager, 1, base,
        )
        selects = [
            item for item in unrestricted.children
            if isinstance(item, discord.ui.Select)
        ]
        self.assertEqual(len(selects), 1)
        self.assertEqual(
            [option.value for option in selects[0].options],
            ["all", "beginner", "intermediate", "advanced", "custom"],
        )

        beginner = RecruitmentOptionsView(
            self.manager,
            1,
            {
                **base,
                "allowed_ranks": list(
                    recruitment_lib._RANK_PRESET_RANKS["beginner"]
                ),
            },
        )
        beginner_select = next(
            item for item in beginner.children
            if isinstance(item, discord.ui.Select)
        )
        self.assertTrue(
            next(
                option for option in beginner_select.options
                if option.value == "beginner"
            ).default
        )
        self.assertIn(
            "ランク未設定",
            beginner.values["allowed_ranks"],
        )

        custom = RecruitmentOptionsView(
            self.manager, 1, {**base, "allowed_ranks": ["ダイヤ"]},
        )
        custom_selects = [
            item for item in custom.children if isinstance(item, discord.ui.Select)
        ]
        self.assertEqual(len(custom_selects), 2)
        rank_select = next(
            item for item in custom_selects
            if item.placeholder == "個別に参加可能ランクを選択"
        )
        self.assertEqual(rank_select.min_values, 1)
        self.assertEqual(rank_select.max_values, 10)
        self.assertEqual(len(rank_select.options), 10)

        schedule = RecruitmentScheduleView(self.manager, 1)
        self.assertFalse(
            any(getattr(item, "key", None) == "room" for item in schedule.children)
        )
        variant_select = next(
            item for item in schedule.children
            if getattr(item, "key", None) == "variant"
        )
        self.assertEqual(
            {option.value for option in variant_select.options},
            set(recruitment_lib.USER_VISIBLE_VARIANT_IDS),
        )

    async def test_notification_role_button_toggles_without_refetch(self) -> None:
        role = MagicMock(spec=discord.Role)
        role.id = 808
        role.name = RECRUITMENT_NOTIFICATION_ROLE_NAME
        role.permissions = discord.Permissions.none()
        role.managed = False
        role.mentionable = False
        role.is_assignable.return_value = True
        default_role = SimpleNamespace(id=1)
        bot_member = SimpleNamespace(id=2)
        guild = MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.roles = [role]
        guild.channels = []
        guild.default_role = default_role
        guild.me = bot_member
        role.guild = guild

        member = MagicMock(spec=discord.Member)
        member.id = 100
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        interaction = SimpleNamespace(
            guild=guild,
            user=member,
            response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def paced(func, *args, **kwargs):
            return await func(*args, **kwargs)

        self.cog.paced_discord_api_call = paced
        await self.manager.toggle_notification_role(interaction)
        await self.manager.toggle_notification_role(interaction)

        member.add_roles.assert_awaited_once_with(
            role, reason="本人が募集通知をON",
        )
        member.remove_roles.assert_awaited_once_with(
            role, reason="本人が募集通知をOFF",
        )
        self.assertIn("ON", interaction.followup.send.await_args_list[0].args[0])
        self.assertIn("OFF", interaction.followup.send.await_args_list[1].args[0])

    async def test_new_recruitment_mentions_only_notification_role_once(self) -> None:
        role = MagicMock(spec=discord.Role)
        role.id = 809
        role.name = RECRUITMENT_NOTIFICATION_ROLE_NAME
        role.permissions = discord.Permissions.none()
        role.managed = False
        role.mentionable = False
        role.mention = "<@&809>"
        role.members = [SimpleNamespace(id=100)]
        role.is_assignable.return_value = True
        default_role = SimpleNamespace(id=1)
        bot_member = SimpleNamespace(id=2)
        channel = SimpleNamespace(
            name="参加受付",
            overwrites={},
            send=AsyncMock(),
            permissions_for=lambda _member: SimpleNamespace(mention_everyone=True),
        )
        guild = MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.roles = [role]
        guild.channels = [channel]
        guild.default_role = default_role
        guild.me = bot_member
        role.guild = guild
        room = SimpleNamespace(
            state=SimpleNamespace(
                lobby_channel=channel,
                lobby_message=SimpleNamespace(jump_url="https://discord.test/card"),
            )
        )
        self.cog.rooms = {"private_1": room}
        row = {"id": 42, "room_id": "private_1"}

        await self.manager._notify_new_recruitment(guild, row)
        await self.manager._notify_new_recruitment(guild, row)

        channel.send.assert_awaited_once()
        content = channel.send.await_args.args[0]
        allowed = channel.send.await_args.kwargs["allowed_mentions"]
        self.assertTrue(content.startswith("<@&809>"))
        self.assertNotIn("@everyone", content)
        self.assertEqual(allowed.roles, [role])
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.replied_user)

    async def test_recent_notification_role_opt_in_is_not_treated_as_no_subscribers(self) -> None:
        role = MagicMock(spec=discord.Role)
        role.id = 809
        role.name = RECRUITMENT_NOTIFICATION_ROLE_NAME
        role.permissions = discord.Permissions.none()
        role.managed = False
        role.mentionable = False
        role.mention = "<@&809>"
        role.members = []
        role.is_assignable.return_value = True
        default_role = SimpleNamespace(id=1)
        bot_member = SimpleNamespace(id=2)
        channel = SimpleNamespace(
            name="参加受付",
            overwrites={},
            send=AsyncMock(),
            permissions_for=lambda _member: SimpleNamespace(mention_everyone=True),
        )
        guild = MagicMock(spec=discord.Guild)
        guild.id = 1
        guild.roles = [role]
        guild.channels = [channel]
        guild.default_role = default_role
        guild.me = bot_member
        role.guild = guild
        self.cog.rooms = {
            "private_1": SimpleNamespace(
                state=SimpleNamespace(lobby_channel=channel, lobby_message=None),
            )
        }
        self.manager._notification_membership_intent = {
            100: (True, recruitment_lib.time.monotonic()),
        }

        await self.manager._notify_new_recruitment(
            guild, {"id": 43, "room_id": "private_1"},
        )

        channel.send.assert_awaited_once()

    async def test_gm_room_uses_selected_rank_names_and_keeps_unranked_distinct(self) -> None:
        permissions = SimpleNamespace(administrator=False, manage_guild=False)
        member = SimpleNamespace(id=200, guild_permissions=permissions, roles=[])
        guild = SimpleNamespace(id=1, owner_id=999)
        row = {
            "id": 1,
            "room_id": self.room_id,
            "variant_id": "v13_cross",
            "capacity": 13,
            "backup_capacity": 3,
            "occupancy_minutes": 90,
            "allowed_ranks": frozenset({"ダイヤ"}),
        }

        with patch(
            "database.get_player_current_rank_info",
            AsyncMock(return_value={"rank_name": "ダイヤ"}),
        ):
            self.assertIsNone(
                await RecruitmentManager.validate_candidate(self.manager, guild, row, member)
            )

        with patch(
            "database.get_player_current_rank_info",
            AsyncMock(return_value={"rank_name": "シルバー"}),
        ):
            error = await RecruitmentManager.validate_candidate(self.manager, guild, row, member)
            self.assertIn("シルバー", error)

        with patch("database.get_player_current_rank_info", AsyncMock(return_value=None)):
            error = await RecruitmentManager.validate_candidate(self.manager, guild, row, member)
            self.assertIn("ランク未設定", error)
            unranked_row = {
                **row,
                "allowed_ranks": frozenset({"ランク未設定"}),
            }
            self.assertIsNone(
                await RecruitmentManager.validate_candidate(
                    self.manager, guild, unranked_row, member,
                )
            )

class ScheduleRangeTest(unittest.TestCase):
    """日付セレクトが提示する候補と、上限判定の範囲を一致させる。"""

    def test_last_offered_day_is_accepted_at_any_hour(self) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from config import RECRUITMENT_MAX_DAYS_AHEAD
        from recruitment import _schedule_out_of_range

        jst = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 8, 3, 10, 0, tzinfo=jst)
        # 日付セレクトは offset=0..MAX の日付を候補に出す
        last_day = (now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD)).date()

        for hour in (0, 9, 10, 11, 23):
            with self.subTest(hour=hour):
                local_start = datetime(
                    last_day.year, last_day.month, last_day.day, hour, tzinfo=jst
                )
                self.assertFalse(
                    _schedule_out_of_range(local_start, now),
                    f"提示された最終日の{hour}時が拒否された",
                )

    def test_past_and_beyond_last_day_are_rejected(self) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from config import RECRUITMENT_MAX_DAYS_AHEAD
        from recruitment import _schedule_out_of_range

        jst = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 8, 3, 10, 0, tzinfo=jst)

        # 過去・現在ちょうどは拒否
        self.assertTrue(_schedule_out_of_range(now, now))
        self.assertTrue(
            _schedule_out_of_range(now - timedelta(minutes=1), now)
        )
        # 候補外の翌日は拒否 (上限は日付単位でも、その先まで緩めない)
        beyond = now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD + 1)
        self.assertTrue(_schedule_out_of_range(beyond, now))


if __name__ == "__main__":
    unittest.main()
