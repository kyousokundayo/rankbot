"""運用・シミュレータの回帰テスト。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from config import Phase, RoomDefinition
from game import GameCog
from room_runner import RoomRunner
from simulate_games import (
    FakeGuild,
    FakeMember,
    FakeMessage,
    FakeRole,
    _make_fast_game_methods,
    _wait_for_game_task,
)


class TestFakeDiscordFidelity(unittest.IsolatedAsyncioTestCase):
    async def test_message_edit_distinguishes_omitted_from_explicit_none(self):
        marker_view = discord.ui.View(timeout=None)
        message = FakeMessage(
            author=object(), channel=object(), content="before", view=marker_view
        )

        await message.edit(content="after")
        self.assertIs(message.view, marker_view)

        await message.edit(content=None, view=None)
        self.assertIsNone(message.content)
        self.assertIsNone(message.view)

    async def test_text_channel_creation_preserves_overwrites(self):
        controller = SimpleNamespace(on_channel_message=lambda _message: None)
        guild = FakeGuild(1, "guild", controller)
        role = FakeRole("private")
        overwrite = discord.PermissionOverwrite(view_channel=False)

        channel = await guild.create_text_channel(
            "secret", overwrites={role: overwrite}
        )

        self.assertIs(channel.overwrites[role.id], overwrite)

    async def test_category_creation_preserves_overwrites(self):
        controller = SimpleNamespace(on_channel_message=lambda _message: None)
        guild = FakeGuild(1, "guild", controller)
        role = FakeRole("private")
        overwrite = discord.PermissionOverwrite(view_channel=False)

        category = await guild.create_category(
            "secret", overwrites={role: overwrite}
        )

        self.assertIs(category.overwrites[role.id], overwrite)

    async def test_member_can_emulate_delayed_gateway_mute_update(self):
        controller = SimpleNamespace(on_channel_message=lambda _message: None)
        guild = FakeGuild(1, "guild", controller)
        member = FakeMember(guild, 10, "member")
        member.mute_apply_delay = 0.01

        await member.edit(mute=True, reason="test")
        self.assertFalse(member.voice.mute)
        await asyncio.sleep(0.02)
        self.assertTrue(member.voice.mute)
        self.assertEqual(member.edit_calls[-1]["mute"], True)

    async def test_member_can_inject_edit_failure(self):
        controller = SimpleNamespace(on_channel_message=lambda _message: None)
        guild = FakeGuild(1, "guild", controller)
        member = FakeMember(guild, 10, "member")
        member.edit_failures.append(RuntimeError("simulated HTTP failure"))

        with self.assertRaisesRegex(RuntimeError, "simulated HTTP failure"):
            await member.edit(mute=True)
        self.assertFalse(member.voice.mute)

    @patch("game.database.set_meta", new_callable=AsyncMock)
    @patch("game.database.get_meta", new_callable=AsyncMock)
    async def test_stats_id_is_reused_without_moving_channels(
        self,
        get_meta: AsyncMock,
        set_meta: AsyncMock,
    ):
        controller = SimpleNamespace(on_channel_message=lambda _message: None)
        guild = FakeGuild(1, "guild", controller)
        await guild.create_text_channel("総合")
        open_category = await guild.create_category("総合")
        original = await guild.create_text_channel("統計", category=open_category)
        manually_placed_notice = await guild.create_text_channel(
            "既存のお知らせ", category=open_category
        )
        get_meta.return_value = str(original.id)
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0

        selected = await manager._ensure_stats_channel(guild)

        self.assertIs(selected, original)
        self.assertIs(selected.category, open_category)
        self.assertIs(manually_placed_notice.category, open_category)
        self.assertEqual(
            [channel for channel in guild.text_channels if channel.name == "統計"],
            [original],
        )
        get_meta.assert_awaited_once_with(guild.id, "stats_channel_id")
        set_meta.assert_awaited_once_with(
            guild.id, "stats_channel_id", str(original.id)
        )

    async def test_active_snapshot_does_not_publish_empty_lobby_before_restore(self):
        controller = SimpleNamespace(on_channel_message=lambda _message: None)
        guild = FakeGuild(1, "guild", controller)
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0
        runner = RoomRunner(
            SimpleNamespace(user=SimpleNamespace(id=999)),
            manager,
            RoomDefinition("open", "総合"),
        )
        category = await guild.create_category("総合")
        lobby = await guild.create_text_channel("参加受付", category=category)
        voice = await guild.create_voice_channel("人狼ゲーム", category=category)
        runner._post_lobby_ui = AsyncMock()
        snapshot = {
            "phase": Phase.NIGHT.name,
            "channel_ids": {
                "category": category.id,
                "lobby": lobby.id,
                "voice": voice.id,
            },
        }

        await runner.setup_channels(guild, snapshot=snapshot)

        runner._post_lobby_ui.assert_not_awaited()
        self.assertIs(runner.state.category, category)
        self.assertIs(runner.state.lobby_channel, lobby)
        self.assertIs(runner.state.voice_channel, voice)


class TestStrictMorningGate(unittest.IsolatedAsyncioTestCase):
    async def test_wait_forever_fails_if_real_gate_did_not_open(self):
        state = SimpleNamespace(morning_ready_ids=set())
        cog = SimpleNamespace(
            state=state,
            _morning_required_ids=lambda: {1},
        )

        class Controller:
            async def drain(self):
                return None

        _make_fast_game_methods(cog, Controller())

        with self.assertRaisesRegex(AssertionError, "morning gate did not open"):
            await cog._pausable_wait_forever(asyncio.Event())

    async def test_wait_forever_accepts_event_set_by_real_handler(self):
        state = SimpleNamespace(morning_ready_ids={1})
        cog = SimpleNamespace(
            state=state,
            _morning_required_ids=lambda: {1},
        )
        event = asyncio.Event()

        class Controller:
            async def drain(self):
                event.set()

        _make_fast_game_methods(cog, Controller())
        await cog._pausable_wait_forever(event)

    async def test_wait_forever_fails_if_preparation_gate_did_not_open(self):
        prep_event = asyncio.Event()
        state = SimpleNamespace(
            morning_ready_ids=set(),
            prep_ready_ids=set(),
            prep_ready_event=prep_event,
        )
        cog = SimpleNamespace(
            state=state,
            _morning_required_ids=lambda: {1},
            _prep_required_ids=lambda: {1},
        )

        class Controller:
            async def drain(self):
                return None

        _make_fast_game_methods(cog, Controller())

        with self.assertRaisesRegex(AssertionError, "preparation gate did not open"):
            await cog._pausable_wait_forever(prep_event)


class TestSimulationTaskTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_swallowed_cancellation_still_reports_timeout(self):
        async def swallow_cancellation():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(swallow_cancellation())

        with self.assertRaisesRegex(AssertionError, "exceeded 0.0 seconds"):
            await _wait_for_game_task(task, timeout=0.01)

        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
