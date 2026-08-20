"""通知3チェックの設定パネル (RecruitmentNotificationSettingsView) を検証する。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

import database
from config import RECRUITMENT_NOTIFICATION_ROLE_NAME
from recruitment import RecruitmentCardView, RecruitmentManager, RecruitmentNotificationSettingsView


def _make_role() -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = 900
    role.name = RECRUITMENT_NOTIFICATION_ROLE_NAME
    role.permissions = discord.Permissions.none()
    role.managed = False
    role.mentionable = False
    role.is_assignable.return_value = True
    return role


def _make_interaction(guild, member, *, edit_original_response=None):
    return SimpleNamespace(
        guild=guild,
        user=member,
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=edit_original_response or AsyncMock(),
    )


class NotificationSettingsPanelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-notify-panel-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "notify.db")
        await database.init_db()

        self.role = _make_role()
        default_role = SimpleNamespace(id=1)
        bot_member = SimpleNamespace(id=2)
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.id = 1
        self.guild.roles = [self.role]
        self.guild.channels = []
        self.guild.default_role = default_role
        self.guild.me = bot_member
        self.role.guild = self.guild

        self.member = MagicMock(spec=discord.Member)
        self.member.id = 100
        self.member.roles = []
        self.member.add_roles = AsyncMock(
            side_effect=lambda role, reason=None: self.member.roles.append(role)
        )
        self.member.remove_roles = AsyncMock(
            side_effect=lambda role, reason=None: (
                self.member.roles.remove(role) if role in self.member.roles else None
            )
        )

        cog = SimpleNamespace(rooms={})

        async def paced(func, *args, **kwargs):
            return await func(*args, **kwargs)

        cog.paced_discord_api_call = paced
        self.manager = RecruitmentManager(SimpleNamespace(), cog)

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    def _states(self, view: RecruitmentNotificationSettingsView) -> dict:
        return {
            button.label: button.label.startswith("✅")
            for button in view.children
        }

    async def test_three_toggles_are_independent(self) -> None:
        await self.manager.send_notification_settings_panel(
            _make_interaction(self.guild, self.member)
        )
        interaction = _make_interaction(self.guild, self.member)
        await self.manager.send_notification_settings_panel(interaction)
        view = interaction.followup.send.await_args.kwargs["view"]

        # 初期状態: 許可=ON(既定)、村ができた時=OFF、この村=OFF
        self.assertTrue(view.prefs["allow_notifications"])
        self.assertFalse(view.prefs["notify_on_create"])
        self.assertFalse(view.prefs["notify_on_call"])

        call_button = next(
            b for b in view.children if "この村を通知を受け取る" in b.label
        )
        await call_button.callback(_make_interaction(self.guild, self.member))
        self.assertTrue(view.prefs["notify_on_call"])
        # 他の2項目は変化しない
        self.assertTrue(view.prefs["allow_notifications"])
        self.assertFalse(view.prefs["notify_on_create"])

        create_button = next(
            b for b in view.children if "村ができた時に通知" in b.label
        )
        await create_button.callback(_make_interaction(self.guild, self.member))
        self.assertTrue(view.prefs["notify_on_create"])
        self.assertIn(self.role, self.member.roles)
        # notify_on_callはそのまま
        self.assertTrue(view.prefs["notify_on_call"])

        persisted = await database.get_user_notification_prefs(1, 100)
        self.assertEqual(persisted, view.prefs)

    async def test_master_off_removes_role(self) -> None:
        self.member.roles = [self.role]
        await database.set_user_notification_prefs(
            1, 100, allow_notifications=True, notify_on_create=True, notify_on_call=True,
        )
        interaction = _make_interaction(self.guild, self.member)
        await self.manager.send_notification_settings_panel(interaction)
        view = interaction.followup.send.await_args.kwargs["view"]
        self.assertTrue(view.prefs["notify_on_create"])

        allow_button = next(b for b in view.children if "通知の許可" in b.label)
        await allow_button.callback(_make_interaction(self.guild, self.member))

        self.assertFalse(view.prefs["allow_notifications"])
        self.assertFalse(view.prefs["notify_on_create"])
        self.assertNotIn(self.role, self.member.roles)
        persisted = await database.get_user_notification_prefs(1, 100)
        self.assertFalse(persisted["allow_notifications"])
        self.assertFalse(persisted["notify_on_create"])

    async def test_role_mismatch_is_reconciled_to_role_on_open(self) -> None:
        """ロールを持っているのにprefsがOFFのままなら、開いた時点でONへ揃う。"""
        self.member.roles = [self.role]
        await database.set_user_notification_prefs(1, 100, notify_on_create=False)

        interaction = _make_interaction(self.guild, self.member)
        await self.manager.send_notification_settings_panel(interaction)
        view = interaction.followup.send.await_args.kwargs["view"]

        self.assertTrue(view.prefs["notify_on_create"])
        persisted = await database.get_user_notification_prefs(1, 100)
        self.assertTrue(persisted["notify_on_create"])

    async def test_create_toggle_is_rejected_while_master_is_off(self) -> None:
        """マスターOFFのまま「村ができた時に通知」をONにできない (指摘4)。

        禁止しないと _notify_new_recruitment はDB設定を見ずロールを
        メンションするため、UI上OFF表示のまま実際にはピンを受け取ってしまう。
        """
        await database.set_user_notification_prefs(
            1, 100, allow_notifications=False, notify_on_create=False,
        )
        interaction = _make_interaction(self.guild, self.member)
        await self.manager.send_notification_settings_panel(interaction)
        view = interaction.followup.send.await_args.kwargs["view"]
        self.assertFalse(view.prefs["allow_notifications"])

        create_button = next(
            b for b in view.children if "村ができた時に通知" in b.label
        )
        reject_interaction = _make_interaction(self.guild, self.member)
        await create_button.callback(reject_interaction)

        # トグルされていないこと (ロールも付かない・DBも変化しない)。
        self.assertFalse(view.prefs["notify_on_create"])
        self.assertNotIn(self.role, self.member.roles)
        self.member.add_roles.assert_not_awaited()
        reject_interaction.followup.send.assert_awaited_once_with(
            "先に「通知の許可」をONにしてください。", ephemeral=True,
        )
        persisted = await database.get_user_notification_prefs(1, 100)
        self.assertFalse(persisted["notify_on_create"])

    async def test_room_notify_button_is_rejected_while_master_is_off(self) -> None:
        """ルーム側の「通知」ボタン (toggle_notification_role) でも同じ穴を塞ぐ。"""
        await database.set_user_notification_prefs(
            1, 100, allow_notifications=False,
        )
        interaction = _make_interaction(self.guild, self.member)
        await self.manager.toggle_notification_role(interaction)

        self.assertNotIn(self.role, self.member.roles)
        self.member.add_roles.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "先に設定パネルの「通知の許可」をONにしてください。", ephemeral=True,
        )

    async def test_card_notify_button_opens_settings_panel(self) -> None:
        card = RecruitmentCardView(self.manager, 42)
        notify_button = discord.utils.get(
            card.children, custom_id="recruitment:42:notify",
        )
        interaction = _make_interaction(self.guild, self.member)
        await notify_button.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIsInstance(
            interaction.followup.send.await_args.kwargs["view"],
            RecruitmentNotificationSettingsView,
        )
