"""GM／仮GMロールへの安全な移行を確認する。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from config import (
    GM_ROLE_NAME,
    LEGACY_MAYOR_ROLE_NAME,
    PRIVATE_ROOM_CREATOR_ROLE_LABEL,
    RoomDefinition,
    TEMP_GM_ROLE_NAME,
)
from game import GameCog
from recruitment import _has_private_room_creator_role as recruitment_creator_role
from recruitment import build_recruitment_help_embed
from room_runner import RoomRunner


class _Role:
    def __init__(self, role_id: int, name: str, guild, *, position: int = 0) -> None:
        self.id = role_id
        self.name = name
        self.guild = guild
        self.position = position
        self.deleted = False
        self.edit_calls: list[dict] = []

    async def edit(self, **kwargs):
        self.edit_calls.append(dict(kwargs))
        if "position" in kwargs:
            self.guild.move_role(self, int(kwargs["position"]))
        return self

    async def delete(self, *, reason=None) -> None:
        self.deleted = True
        self.guild.roles.remove(self)


class _Member:
    def __init__(self, member_id: int, roles=None) -> None:
        self.id = member_id
        self.display_name = f"member-{member_id}"
        self.roles = list(roles or [])
        self.add_calls: list[tuple[object, ...]] = []
        self.remove_calls: list[tuple[object, ...]] = []

    async def add_roles(self, *roles, reason=None) -> None:
        self.add_calls.append(roles)
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None) -> None:
        self.remove_calls.append(roles)
        remove_ids = {role.id for role in roles}
        self.roles = [role for role in self.roles if role.id not in remove_ids]


class _FailingMember(_Member):
    async def add_roles(self, *roles, reason=None) -> None:
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}),
            "GMロールを付与できません",
        )


class _StaleCacheMember(_Member):
    """HTTP成功後もrolesキャッシュが更新されないDiscord状態を再現する。"""

    async def add_roles(self, *roles, reason=None) -> None:
        self.add_calls.append(roles)


class _Guild:
    def __init__(self, roles: list[tuple[str, int]], members: list[_Member]) -> None:
        self.id = 1
        self.roles: list[_Role] = []
        self.members = members
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


class GmRoleMigrationTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> GameCog:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0
        return manager

    async def test_legacy_mayor_members_move_to_gm_before_legacy_role_deletion(self) -> None:
        member = _Member(10)
        guild = _Guild(
            [("@everyone", 0), ("通常ロール", 1), (LEGACY_MAYOR_ROLE_NAME, 2)],
            [member],
        )
        legacy_role = guild.role_named(LEGACY_MAYOR_ROLE_NAME)
        ordinary_role = guild.role_named("通常ロール")
        assert legacy_role is not None
        assert ordinary_role is not None
        member.roles = [ordinary_role, legacy_role]

        await self._manager()._ensure_gm_staff_roles(guild)

        gm_role = guild.role_named(GM_ROLE_NAME)
        temp_gm_role = guild.role_named(TEMP_GM_ROLE_NAME)
        self.assertIsNotNone(gm_role)
        self.assertIsNotNone(temp_gm_role)
        self.assertIn(gm_role, member.roles)
        self.assertIn(ordinary_role, member.roles)
        self.assertNotIn(legacy_role, member.roles)
        self.assertTrue(legacy_role.deleted)
        self.assertNotIn(legacy_role, guild.roles)
        self.assertGreater(gm_role.position, temp_gm_role.position)

    async def test_gm_is_placed_above_temp_gm_without_recreating_either_role(self) -> None:
        guild = _Guild(
            [
                ("@everyone", 0),
                (GM_ROLE_NAME, 1),
                ("通常ロール", 2),
                (TEMP_GM_ROLE_NAME, 3),
            ],
            [],
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
            ],
            [],
        )
        gm_role = guild.role_named(GM_ROLE_NAME)
        temp_gm_role = guild.role_named(TEMP_GM_ROLE_NAME)
        assert gm_role is not None
        assert temp_gm_role is not None

        await self._manager()._ensure_gm_staff_roles(guild)

        self.assertGreater(gm_role.position, temp_gm_role.position)

    async def test_failed_gm_grant_keeps_legacy_role_and_stops_migration(self) -> None:
        member = _FailingMember(10)
        guild = _Guild(
            [("@everyone", 0), (LEGACY_MAYOR_ROLE_NAME, 1)],
            [member],
        )
        legacy_role = guild.role_named(LEGACY_MAYOR_ROLE_NAME)
        assert legacy_role is not None
        member.roles = [legacy_role]

        with self.assertRaises(RuntimeError):
            await self._manager()._ensure_gm_staff_roles(guild)

        self.assertIn(legacy_role, member.roles)
        self.assertFalse(legacy_role.deleted)
        self.assertIn(legacy_role, guild.roles)

    async def test_stale_member_role_cache_does_not_block_legacy_migration(self) -> None:
        member = _StaleCacheMember(10)
        guild = _Guild(
            [("@everyone", 0), (LEGACY_MAYOR_ROLE_NAME, 1)],
            [member],
        )
        legacy_role = guild.role_named(LEGACY_MAYOR_ROLE_NAME)
        assert legacy_role is not None
        member.roles = [legacy_role]

        await self._manager()._ensure_gm_staff_roles(guild)

        self.assertEqual(len(member.add_calls), 1)
        self.assertEqual(len(member.remove_calls), 1)
        self.assertTrue(legacy_role.deleted)

    async def test_startup_cleanup_keeps_a_migrated_owner_with_stale_role_cache(self) -> None:
        manager = self._manager()
        owner = _StaleCacheMember(10)
        guild = SimpleNamespace(
            id=1,
            get_member=lambda member_id: owner if member_id == owner.id else None,
        )
        manager._legacy_migration_owner_ids.add(owner.id)
        manager._delete_private_room_by_row = AsyncMock()
        row = {"owner_id": owner.id, "room_name": "テスト専用村"}

        with patch("game.database.load_private_rooms", AsyncMock(return_value=[row])):
            await manager._cleanup_private_rooms_without_creator_role(guild)

        manager._delete_private_room_by_row.assert_not_awaited()

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

    def test_recruitment_help_identifies_both_gm_roles(self) -> None:
        embed = build_recruitment_help_embed()
        create_field = next(field for field in embed.fields if field.name == "募集を作る")
        self.assertIn(PRIVATE_ROOM_CREATOR_ROLE_LABEL, create_field.value)

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
                private_role_name="テスト専用村",
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
