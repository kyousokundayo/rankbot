"""GM／仮GMロールの用意と、両ロールが同じ権限を持つことを確認する。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import database

from config import (
    GM_ROLE_NAME,
    PRIVATE_ROOM_CREATOR_ROLE_LABEL,
    RoomDefinition,
    TEMP_GM_ROLE_NAME,
)
from game import GameCog
from recruitment import _has_private_room_creator_role as recruitment_creator_role
from room_runner import RoomRunner


class _Role:
    def __init__(self, role_id: int, name: str, guild, *, position: int = 0) -> None:
        self.id = role_id
        self.name = name
        self.guild = guild
        self.position = position
        self.edit_calls: list[dict] = []

    async def edit(self, **kwargs):
        self.edit_calls.append(dict(kwargs))
        if "position" in kwargs:
            self.guild.move_role(self, int(kwargs["position"]))
        return self


class _Guild:
    def __init__(self, roles: list[tuple[str, int]]) -> None:
        self.id = 1
        self.roles: list[_Role] = []
        for role_id, (name, position) in enumerate(roles, 1):
            self.roles.append(_Role(role_id, name, self, position=position))
        self.created_roles: list[_Role] = []

    async def create_role(self, *, name: str, reason=None):
        role = _Role(
            max((item.id for item in self.roles), default=0) + 1,
            name,
            self,
            position=max((item.position for item in self.roles), default=0) + 1,
        )
        self.roles.append(role)
        self.created_roles.append(role)
        return role

    def move_role(self, target: _Role, new_position: int) -> None:
        """Discordのrole.edit(position=...)に必要な最低限の並び替えを再現する。"""
        old_position = target.position
        if new_position > old_position:
            for role in self.roles:
                if role is not target and old_position < role.position <= new_position:
                    role.position -= 1
        elif new_position < old_position:
            for role in self.roles:
                if role is not target and new_position <= role.position < old_position:
                    role.position += 1
        target.position = new_position

    def role_named(self, name: str) -> _Role | None:
        return next((role for role in self.roles if role.name == name), None)


class GmStaffRoleTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> GameCog:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0
        return manager

    async def test_missing_roles_are_created_with_gm_above_temp_gm(self) -> None:
        guild = _Guild([("@everyone", 0), ("通常ロール", 1)])

        await self._manager()._ensure_gm_staff_roles(guild)

        gm_role = guild.role_named(GM_ROLE_NAME)
        temp_gm_role = guild.role_named(TEMP_GM_ROLE_NAME)
        self.assertIsNotNone(gm_role)
        self.assertIsNotNone(temp_gm_role)
        self.assertEqual(
            {role.name for role in guild.created_roles},
            {GM_ROLE_NAME, TEMP_GM_ROLE_NAME},
        )
        self.assertGreater(gm_role.position, temp_gm_role.position)

    async def test_gm_is_placed_above_temp_gm_without_recreating_either_role(self) -> None:
        guild = _Guild(
            [
                ("@everyone", 0),
                (GM_ROLE_NAME, 1),
                ("通常ロール", 2),
                (TEMP_GM_ROLE_NAME, 3),
            ]
        )
        gm_role = guild.role_named(GM_ROLE_NAME)
        temp_gm_role = guild.role_named(TEMP_GM_ROLE_NAME)
        assert gm_role is not None
        assert temp_gm_role is not None

        await self._manager()._ensure_gm_staff_roles(guild)

        self.assertEqual(guild.created_roles, [])
        self.assertGreater(gm_role.position, temp_gm_role.position)
        self.assertTrue(any("position" in call for call in gm_role.edit_calls))

    async def test_gm_is_placed_above_temp_gm_when_positions_match(self) -> None:
        guild = _Guild(
            [
                ("@everyone", 0),
                (GM_ROLE_NAME, 2),
                (TEMP_GM_ROLE_NAME, 2),
            ]
        )
        gm_role = guild.role_named(GM_ROLE_NAME)
        temp_gm_role = guild.role_named(TEMP_GM_ROLE_NAME)
        assert gm_role is not None
        assert temp_gm_role is not None

        await self._manager()._ensure_gm_staff_roles(guild)

        self.assertGreater(gm_role.position, temp_gm_role.position)

    async def test_duplicate_gm_role_does_not_stop_startup(self) -> None:
        """同名ロールが2つあっても起動を止めず、最上位を正本として使う。"""
        guild = _Guild(
            [
                ("@everyone", 0),
                (GM_ROLE_NAME, 1),
                (GM_ROLE_NAME, 5),
                (TEMP_GM_ROLE_NAME, 3),
            ]
        )
        manager = self._manager()

        await manager._ensure_gm_staff_roles(guild)

        self.assertEqual(guild.created_roles, [])
        self.assertEqual(manager._gm_staff_roles[GM_ROLE_NAME].position, 5)

    async def test_role_creation_failure_does_not_stop_startup(self) -> None:
        """ロールを作れなくても、他機能を巻き添えにせず起動を続ける。"""

        class _FailingGuild(_Guild):
            async def create_role(self, *, name: str, reason=None):
                raise discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden", headers={}),
                    "ロールを作成できません",
                )

        guild = _FailingGuild([("@everyone", 0)])
        manager = self._manager()

        await manager._ensure_gm_staff_roles(guild)

        self.assertEqual(manager._gm_staff_roles, {})

    async def test_position_edit_failure_does_not_stop_startup(self) -> None:
        """並び順は表示上の慣習なので、変更できなくても起動を続ける。"""
        guild = _Guild(
            [("@everyone", 0), (GM_ROLE_NAME, 1), (TEMP_GM_ROLE_NAME, 3)]
        )
        gm_role = guild.role_named(GM_ROLE_NAME)
        assert gm_role is not None

        async def _refuse(**kwargs):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden", headers={}),
                "階層が足りません",
            )

        gm_role.edit = _refuse

        await self._manager()._ensure_gm_staff_roles(guild)

        self.assertLess(gm_role.position, guild.role_named(TEMP_GM_ROLE_NAME).position)

    async def test_legacy_access_role_cleanup_requires_stable_id_and_name(self) -> None:
        guild = _Guild([("@everyone", 0), ("旧GM村", 1)])
        guild.default_role = guild.role_named("@everyone")
        legacy_role = guild.role_named("旧GM村")
        assert legacy_role is not None
        legacy_role.delete = AsyncMock()
        guild.get_role = lambda role_id: legacy_role if role_id == legacy_role.id else None
        manager = self._manager()
        row = {
            "room_id": "private_10",
            "role_id": legacy_role.id,
            "role_name": "旧GM村",
        }

        with patch.object(
            database, "load_private_rooms", AsyncMock(return_value=[row]),
        ), patch.object(
            database,
            "clear_private_room_access_role",
            AsyncMock(return_value=True),
        ) as clear_role:
            await manager._cleanup_legacy_private_room_access_roles(
                guild, "private_10",
            )

        legacy_role.delete.assert_awaited_once()
        clear_role.assert_awaited_once_with(
            guild.id, "private_10", expected_role_id=legacy_role.id,
        )

    def test_gm_and_temp_gm_are_both_private_room_creator_roles(self) -> None:
        manager = self._manager()
        for role_name in (GM_ROLE_NAME, TEMP_GM_ROLE_NAME):
            with self.subTest(role_name=role_name):
                member = SimpleNamespace(roles=[SimpleNamespace(name=role_name)])
                self.assertTrue(manager._has_private_room_creator_role(member))
                self.assertTrue(recruitment_creator_role(member))

        outsider = SimpleNamespace(roles=[SimpleNamespace(name="一般参加者")])
        self.assertFalse(manager._has_private_room_creator_role(outsider))
        self.assertFalse(recruitment_creator_role(outsider))

    async def test_both_roles_can_manage_and_claim_a_private_room(self) -> None:
        manager = SimpleNamespace(
            find_user_room=lambda _user_id, *, exclude_room_id=None: None,
        )
        runner = RoomRunner(
            None,
            manager,
            RoomDefinition(
                "private_10",
                "テスト専用村",
                private_owner_id=10,
            ),
        )
        for role_name in (GM_ROLE_NAME, TEMP_GM_ROLE_NAME):
            with self.subTest(role_name=role_name):
                owner = SimpleNamespace(
                    id=10,
                    roles=[SimpleNamespace(name=role_name)],
                )
                self.assertTrue(runner.can_manage_private_room(owner))
                self.assertIsNone(await runner.validate_gm_claim(owner))

        outsider = SimpleNamespace(
            id=10,
            roles=[SimpleNamespace(name="一般参加者")],
        )
        self.assertFalse(runner.can_manage_private_room(outsider))
        self.assertIn(
            PRIVATE_ROOM_CREATOR_ROLE_LABEL,
            await runner.validate_gm_claim(outsider),
        )


if __name__ == "__main__":
    unittest.main()
