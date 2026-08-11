"""アプリコマンドの差分同期を実Discordなしで検証する。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
from discord import app_commands

from command_sync import _desired_schema, _remote_schema, sync_application_commands


class FakeLocalCommand:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def to_dict(self, tree) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "type": 1,
            "options": [],
            "nsfw": False,
            "dm_permission": True,
            "default_member_permissions": None,
            "contexts": None,
            "integration_types": None,
        }


class FakeRemoteCommand:
    def __init__(self, name: str, description: str, command_id: int) -> None:
        self.name = name
        self.description = description
        self.id = command_id
        self.default_member_permissions = None
        self.dm_permission = True
        self.nsfw = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "application_id": 999,
            "type": 1,
            "name": self.name,
            "description": self.description,
            "name_localizations": {},
            "description_localizations": {},
            "contexts": None,
            "integration_types": None,
            "options": [],
        }


class FakeTree:
    def __init__(
        self,
        commands: list[FakeLocalCommand],
        *,
        remote_guild: list[FakeRemoteCommand] | None = None,
        remote_global: list[FakeRemoteCommand] | None = None,
        application_id: int = 999,
    ) -> None:
        self.client = SimpleNamespace(application_id=application_id)
        self.translator = None
        self._global_commands = list(commands)
        self._guild_commands: list[FakeLocalCommand] = []
        self.remote_guild = list(remote_guild or [])
        self.remote_global = list(remote_global or [])
        self.sync_scopes: list[int | None] = []
        self.fetch_scopes: list[int | None] = []

    def copy_global_to(self, *, guild) -> None:
        self._guild_commands = list(self._global_commands)

    def clear_commands(self, *, guild=None) -> None:
        if guild is None:
            self._global_commands = []

    def get_commands(self, *, guild=None):
        return list(self._global_commands if guild is None else self._guild_commands)

    async def sync(self, *, guild=None):
        scope = None if guild is None else guild.id
        self.sync_scopes.append(scope)
        if guild is None:
            self.remote_global = []
            return []
        self.remote_guild = [
            FakeRemoteCommand(command.name, command.description, 1000 + index)
            for index, command in enumerate(self._guild_commands)
        ]
        return list(self.remote_guild)

    async def fetch_commands(self, *, guild=None):
        scope = None if guild is None else guild.id
        self.fetch_scopes.append(scope)
        return list(self.remote_global if guild is None else self.remote_guild)


class MetaStore:
    def __init__(self) -> None:
        self.values: dict[tuple[int, str], str] = {}

    async def get(self, guild_id: int, key: str):
        return self.values.get((guild_id, key))

    async def set(self, guild_id: int, key: str, value: str) -> None:
        self.values[(guild_id, key)] = value


class CommandSyncTest(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)

    async def _seed(self, commands=None):
        guild = type("Guild", (), {"id": 42})()
        store = MetaStore()
        tree = FakeTree(commands or [FakeLocalCommand("season_reset", "reset")])
        result = await sync_application_commands(
            tree, guild, get_meta=store.get, set_meta=store.set, now=self.NOW,
        )
        self.assertEqual(tree.sync_scopes, [None, guild.id])
        self.assertEqual(tree.fetch_scopes, [])
        self.assertEqual(result.guild_action, "synced")
        self.assertEqual(result.global_action, "cleared")
        return guild, store, tree

    async def test_first_start_syncs_but_fresh_restart_only_fetches_guild(self) -> None:
        guild, store, seeded = await self._seed()
        restarted = FakeTree(
            [FakeLocalCommand("season_reset", "reset")],
            remote_guild=seeded.remote_guild,
        )

        result = await sync_application_commands(
            restarted, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(hours=1),
        )

        self.assertEqual(restarted.fetch_scopes, [guild.id])
        self.assertEqual(restarted.sync_scopes, [])
        self.assertEqual(result.guild_action, "verified")
        self.assertEqual(result.global_action, "skipped")

    async def test_local_schema_change_syncs_guild_without_preflight_get(self) -> None:
        guild, store, seeded = await self._seed()
        changed = FakeTree(
            [FakeLocalCommand("season_reset", "new description")],
            remote_guild=seeded.remote_guild,
        )

        result = await sync_application_commands(
            changed, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(hours=1),
        )

        self.assertEqual(changed.fetch_scopes, [])
        self.assertEqual(changed.sync_scopes, [guild.id])
        self.assertEqual(result.guild_action, "synced")

    async def test_external_guild_deletion_is_repaired_on_restart(self) -> None:
        guild, store, _seeded = await self._seed()
        drifted = FakeTree(
            [FakeLocalCommand("season_reset", "reset")], remote_guild=[],
        )

        await sync_application_commands(
            drifted, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(hours=1),
        )

        self.assertEqual(drifted.fetch_scopes, [guild.id])
        self.assertEqual(drifted.sync_scopes, [guild.id])
        self.assertEqual(len(drifted.remote_guild), 1)

    async def test_global_commands_are_audited_weekly_and_cleared_if_present(self) -> None:
        guild, store, seeded = await self._seed()
        polluted = FakeTree(
            [FakeLocalCommand("season_reset", "reset")],
            remote_guild=seeded.remote_guild,
            remote_global=[FakeRemoteCommand("old_global", "old", 555)],
        )

        result = await sync_application_commands(
            polluted, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(days=8),
        )

        self.assertEqual(polluted.fetch_scopes, [None, guild.id])
        self.assertEqual(polluted.sync_scopes, [None])
        self.assertEqual(polluted.remote_global, [])
        self.assertEqual(result.global_action, "cleared")

    async def test_expired_empty_global_is_verified_without_put(self) -> None:
        guild, store, seeded = await self._seed()
        restarted = FakeTree(
            [FakeLocalCommand("season_reset", "reset")],
            remote_guild=seeded.remote_guild,
        )

        result = await sync_application_commands(
            restarted, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(days=8),
        )

        self.assertEqual(restarted.fetch_scopes, [None, guild.id])
        self.assertEqual(restarted.sync_scopes, [])
        self.assertEqual(result.global_action, "verified")

    async def test_application_change_invalidates_both_cached_scopes(self) -> None:
        guild, store, seeded = await self._seed()
        changed_app = FakeTree(
            [FakeLocalCommand("season_reset", "reset")],
            remote_guild=seeded.remote_guild,
            application_id=1001,
        )

        await sync_application_commands(
            changed_app, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(hours=1),
        )

        self.assertEqual(changed_app.fetch_scopes, [])
        self.assertEqual(changed_app.sync_scopes, [None, guild.id])

    async def test_meta_read_failure_falls_back_to_both_syncs(self) -> None:
        guild = SimpleNamespace(id=42)
        store = MetaStore()
        tree = FakeTree([FakeLocalCommand("season_reset", "reset")])

        async def broken_get(guild_id: int, key: str):
            raise RuntimeError("db unavailable")

        result = await sync_application_commands(
            tree, guild,
            get_meta=broken_get, set_meta=store.set, now=self.NOW,
        )

        self.assertEqual(tree.sync_scopes, [None, guild.id])
        self.assertEqual(result.guild_action, "synced")

    async def test_meta_write_failure_does_not_cancel_successful_sync(self) -> None:
        guild = SimpleNamespace(id=42)
        store = MetaStore()
        tree = FakeTree([FakeLocalCommand("season_reset", "reset")])

        async def broken_set(guild_id: int, key: str, value: str) -> None:
            raise RuntimeError("db unavailable")

        result = await sync_application_commands(
            tree, guild,
            get_meta=store.get, set_meta=broken_set, now=self.NOW,
        )

        self.assertEqual(tree.sync_scopes, [None, guild.id])
        self.assertEqual(result.command_count, 1)

    async def test_fetch_failure_propagates_without_changing_cache(self) -> None:
        guild, store, seeded = await self._seed()
        before = dict(store.values)
        restarted = FakeTree(
            [FakeLocalCommand("season_reset", "reset")],
            remote_guild=seeded.remote_guild,
        )

        async def broken_fetch(*, guild=None):
            raise RuntimeError("discord unavailable")

        restarted.fetch_commands = broken_fetch
        with self.assertRaisesRegex(RuntimeError, "discord unavailable"):
            await sync_application_commands(
                restarted, guild,
                get_meta=store.get, set_meta=store.set,
                now=self.NOW + timedelta(hours=1),
            )

        self.assertEqual(store.values, before)

    async def test_sync_failure_propagates_without_writing_cache(self) -> None:
        guild = SimpleNamespace(id=42)
        store = MetaStore()
        tree = FakeTree([FakeLocalCommand("season_reset", "reset")])

        async def broken_sync(*, guild=None):
            raise RuntimeError("discord unavailable")

        tree.sync = broken_sync
        with self.assertRaisesRegex(RuntimeError, "discord unavailable"):
            await sync_application_commands(
                tree, guild,
                get_meta=store.get, set_meta=store.set, now=self.NOW,
            )

        self.assertEqual(store.values, {})

    async def test_top_level_registration_order_does_not_force_sync(self) -> None:
        first = FakeLocalCommand("a", "A")
        second = FakeLocalCommand("b", "B")
        guild, store, seeded = await self._seed([first, second])
        restarted = FakeTree(
            [second, first], remote_guild=seeded.remote_guild,
        )

        result = await sync_application_commands(
            restarted, guild,
            get_meta=store.get, set_meta=store.set,
            now=self.NOW + timedelta(hours=1),
        )

        self.assertEqual(restarted.sync_scopes, [])
        self.assertEqual(result.guild_action, "verified")

    async def test_real_discord_command_schema_changes_without_manual_version(self) -> None:
        async def build_hash(description: str) -> str:
            client = discord.Client(intents=discord.Intents.none())
            tree = app_commands.CommandTree(client)

            @tree.command(name="season_reset", description=description)
            @app_commands.default_permissions(manage_guild=True)
            async def season_reset(interaction: discord.Interaction) -> None:
                pass

            guild = discord.Object(id=42)
            tree.copy_global_to(guild=guild)
            _commands, schema_hash = await _desired_schema(tree, guild)
            await client.close()
            return schema_hash

        self.assertNotEqual(await build_hash("old"), await build_hash("new"))

    def test_real_remote_command_hash_ignores_discord_ids(self) -> None:
        def command(command_id: str) -> app_commands.AppCommand:
            return app_commands.AppCommand(
                data={
                    "id": command_id,
                    "application_id": "999",
                    "guild_id": "42",
                    "type": 1,
                    "name": "season_reset",
                    "description": "reset",
                    "default_member_permissions": "32",
                    "dm_permission": True,
                    "nsfw": False,
                    "options": [],
                },
                state=SimpleNamespace(),
            )

        self.assertEqual(
            _remote_schema([command("100")]),
            _remote_schema([command("200")]),
        )


if __name__ == "__main__":
    unittest.main()
