"""#運営 の手動閲覧設定と、運営メニューを操作できる相手を検証する。

Discordで手動設定したロール・メンバーoverwriteは起動時も保持する。
メニュー操作は別判定で、Administrator・所有者・設定運営ロールだけに限る。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import recruitment as recruitment_lib
from config import CH_OPERATIONS, OPERATIONS_CATEGORY_NAME


class _Target:
    """overwritesのキーになるロール/メンバー。"""

    def __init__(self, target_id: int, name: str) -> None:
        self.id = target_id
        self.name = name


class _Channel:
    def __init__(self, name: str, category, overwrites: dict) -> None:
        self.name = name
        self.category = category
        self.overwrites = overwrites
        self.edit_calls: list[dict] = []

    async def edit(self, **kwargs):
        self.edit_calls.append(dict(kwargs))
        self.overwrites = kwargs.get("overwrites", self.overwrites)
        return self


def _view_allowed(overwrite: discord.PermissionOverwrite) -> bool:
    return overwrite.view_channel is True


class OperationsChannelAccessTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> recruitment_lib.RecruitmentManager:
        return recruitment_lib.RecruitmentManager(SimpleNamespace(), SimpleNamespace())

    def _guild(self, *, existing_overwrites: dict, roles: list[_Target]):
        category = SimpleNamespace(name=OPERATIONS_CATEGORY_NAME)
        bot_member = _Target(2, "bot")
        default_role = _Target(1, "@everyone")
        channel = _Channel(CH_OPERATIONS, category, existing_overwrites)
        guild = SimpleNamespace(
            categories=[category],
            text_channels=[channel],
            roles=[default_role, *roles],
            me=bot_member,
            default_role=default_role,
        )
        return guild, channel, default_role, bot_member

    async def test_manual_role_allow_is_preserved_without_configuration(self) -> None:
        staff = _Target(3, "ねいと")
        manual = discord.PermissionOverwrite(
            view_channel=True, send_messages=False, manage_messages=True,
        )
        guild, channel, default_role, _bot = self._guild(
            existing_overwrites={staff: manual},
            roles=[staff],
        )

        with patch.object(recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset()):
            result = await self._manager()._ensure_operations_channel(guild)

        self.assertIsNotNone(result)
        self.assertEqual(channel.overwrites[staff].pair(), manual.pair())
        self.assertTrue(_view_allowed(channel.overwrites[staff]))
        self.assertNotIn(default_role, channel.overwrites)

    async def test_missing_existing_channel_is_not_created(self) -> None:
        default_role = _Target(1, "@everyone")
        bot_member = _Target(2, "bot")
        staff = _Target(3, "運営")
        category = SimpleNamespace(name=OPERATIONS_CATEGORY_NAME)
        create_text_channel = AsyncMock()
        guild = SimpleNamespace(
            categories=[category],
            text_channels=[],
            roles=[default_role, staff],
            me=bot_member,
            default_role=default_role,
            create_text_channel=create_text_channel,
        )

        result = await self._manager()._ensure_operations_channel(guild)

        self.assertIsNone(result)
        create_text_channel.assert_not_awaited()

    async def test_missing_operations_category_is_not_created(self) -> None:
        create_category = AsyncMock()
        create_text_channel = AsyncMock()
        guild = SimpleNamespace(
            categories=[],
            text_channels=[],
            me=_Target(2, "bot"),
            create_category=create_category,
            create_text_channel=create_text_channel,
        )

        result = await self._manager()._ensure_operations_channel(guild)

        self.assertIsNone(result)
        create_category.assert_not_awaited()
        create_text_channel.assert_not_awaited()

    async def test_manual_overwrites_are_preserved_while_bot_access_is_added(self) -> None:
        staff = _Target(3, "ねいと")
        outsider = _Target(4, "一般参加者")
        guild, channel, default_role, bot_member = self._guild(
            existing_overwrites={
                staff: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True,
                ),
                outsider: discord.PermissionOverwrite(view_channel=True),
            },
            roles=[staff, outsider],
        )

        with patch.object(
            recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset({"ねいと"})
        ):
            result = await self._manager()._ensure_operations_channel(guild)

        self.assertIsNotNone(result)
        self.assertTrue(_view_allowed(channel.overwrites[staff]))
        self.assertTrue(channel.overwrites[staff].send_messages)
        # 設定外でも、Discordで手動allowした対象はそのまま保持する。
        self.assertTrue(_view_allowed(channel.overwrites[outsider]))
        self.assertNotIn(default_role, channel.overwrites)
        self.assertTrue(_view_allowed(channel.overwrites[bot_member]))

    async def test_configured_roles_are_not_added_to_channel_permissions(self) -> None:
        """設定ロールは操作認可だけに使い、閲覧権限は手動管理する。"""
        staff_a = _Target(3, "ねいと")
        staff_b = _Target(4, "ねいと")
        guild, channel, _default, _bot = self._guild(
            existing_overwrites={}, roles=[staff_a, staff_b]
        )

        with patch.object(
            recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset({"ねいと"})
        ):
            await self._manager()._ensure_operations_channel(guild)

        self.assertNotIn(staff_a, channel.overwrites)
        self.assertNotIn(staff_b, channel.overwrites)

    async def test_no_api_call_when_permissions_already_match(self) -> None:
        """変更が無ければDiscord APIを叩かない (既存の節約を壊さない)。"""
        staff = _Target(3, "ねいと")
        guild, channel, _default, _bot = self._guild(
            existing_overwrites={}, roles=[staff]
        )

        with patch.object(
            recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset({"ねいと"})
        ):
            await self._manager()._ensure_operations_channel(guild)
            first_calls = len(channel.edit_calls)
            await self._manager()._ensure_operations_channel(guild)

        self.assertEqual(first_calls, 1)
        self.assertEqual(len(channel.edit_calls), 1)

class OperationsMenuAuthorizationTest(unittest.TestCase):
    """運営メニューを操作できる相手を固定する。"""

    def _member(
        self,
        member_id: int,
        *,
        administrator: bool = False,
        role_names: tuple[str, ...] = (),
    ):
        member = MagicMock(spec=discord.Member)
        member.id = member_id
        member.guild_permissions = SimpleNamespace(administrator=administrator)
        member.roles = [SimpleNamespace(name=name) for name in role_names]
        return member

    def _interaction(self, member, *, owner_id: int = 999):
        return SimpleNamespace(user=member, guild=SimpleNamespace(owner_id=owner_id))

    def test_server_owner_can_operate_without_administrator(self) -> None:
        owner = self._member(10, administrator=False)

        with patch.object(recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset()):
            allowed = recruitment_lib.OperationsView._is_admin(
                self._interaction(owner, owner_id=10)
            )

        self.assertTrue(allowed)

    def test_administrator_can_operate(self) -> None:
        admin = self._member(11, administrator=True)

        with patch.object(recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset()):
            allowed = recruitment_lib.OperationsView._is_admin(
                self._interaction(admin)
            )

        self.assertTrue(allowed)

    def test_configured_staff_role_can_operate(self) -> None:
        staff = self._member(12, administrator=False, role_names=("ねいと",))

        with patch.object(
            recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset({"ねいと"})
        ):
            allowed = recruitment_lib.OperationsView._is_admin(
                self._interaction(staff)
            )

        self.assertTrue(allowed)

    def test_same_role_without_configuration_cannot_operate(self) -> None:
        """設定から外せば、ロールを持っていても操作できない。"""
        staff = self._member(12, administrator=False, role_names=("ねいと",))

        with patch.object(recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset()):
            allowed = recruitment_lib.OperationsView._is_admin(
                self._interaction(staff)
            )

        self.assertFalse(allowed)

    def test_ordinary_member_cannot_operate(self) -> None:
        outsider = self._member(13, administrator=False, role_names=("一般参加者",))

        with patch.object(
            recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset({"ねいと"})
        ):
            allowed = recruitment_lib.OperationsView._is_admin(
                self._interaction(outsider)
            )

        self.assertFalse(allowed)

    def test_non_member_user_cannot_operate(self) -> None:
        """DMなどMemberでない相手は常に拒否する。"""
        with patch.object(
            recruitment_lib, "OPERATIONS_STAFF_ROLE_NAMES", frozenset({"ねいと"})
        ):
            allowed = recruitment_lib.OperationsView._is_admin(
                SimpleNamespace(user=SimpleNamespace(id=14), guild=None)
            )

        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
