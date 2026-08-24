"""発言制御が不完全なまま進行しないことの回帰テスト。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from config import Phase
from game import GameCog
from room_runner import StateDurabilityError
from tests.test_gameplay_durability import (
    FakeGuild,
    FakeMember,
    FakeRole,
    FakeVoiceState,
    add_player,
    make_runner,
)


class MuteFailClosedTest(unittest.IsolatedAsyncioTestCase):
    async def test_manual_mute_blocks_speech_gate_until_corrected(self) -> None:
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.phase = Phase.DAY_RUNOFF_SPEECH
        runner.state.current_speaker_id = speaker.user_id
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        speaker.member.voice = FakeVoiceState(
            mute=True, channel=voice_channel
        )

        with patch("room_runner.MUTE_GRACE_TIME", 0):
            grant = asyncio.create_task(runner._grant_speaker(speaker.member))
            for _ in range(10):
                await asyncio.sleep(0)
                if runner.state.paused:
                    break

            self.assertTrue(runner.state.paused)
            self.assertFalse(grant.done())

            # モデレーターが手動muteを外し、GMが再開した状態を再現する。
            speaker.member.voice.mute = False
            runner.state.paused = False
            runner.state.phase = Phase.DAY_RUNOFF_SPEECH
            runner.state.pause_event.set()
            await asyncio.wait_for(grant, timeout=0.2)

        self.assertFalse(speaker.member.voice.mute)

    async def test_participant_gm_is_manual_but_still_checked_when_speaking(self) -> None:
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.gm_id = speaker.user_id
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        speaker.member.voice = FakeVoiceState(
            mute=True, channel=voice_channel
        )

        issues = runner._mute_state_issues({speaker.user_id})

        self.assertTrue(any("mute解除未反映" in issue for issue in issues))
        speaker.member.edit.assert_not_awaited()

    async def test_external_unmute_of_non_speaker_immediately_pauses(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        runner.state.phase = Phase.NIGHT
        voice_channel = SimpleNamespace(id=50, members=[player.member])
        runner.state.voice_channel = voice_channel
        before = FakeVoiceState(mute=True, channel=voice_channel)
        after = FakeVoiceState(mute=False, channel=voice_channel)
        player.member.voice = after
        runner.state.bot_muted_ids = {player.user_id}
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())

        async def apply_mute(**kwargs):
            player.member.voice.mute = kwargs["mute"]
            return player.member

        player.member.edit = AsyncMock(side_effect=apply_mute)
        try:
            await runner.on_voice_state_update(player.member, before, after)

            self.assertTrue(runner.state.paused)
            self.assertEqual(runner.state.phase, Phase.PAUSED)
            self.assertTrue(player.member.voice.mute)
            self.assertFalse(runner.state.recovered_from_restart)
        finally:
            runner.state.game_task.cancel()

    async def test_bot_owned_transition_event_does_not_false_pause(self) -> None:
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.phase = Phase.DAY_DISCUSSION
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        before = FakeVoiceState(mute=False, channel=voice_channel)
        after = FakeVoiceState(mute=True, channel=voice_channel)
        speaker.member.voice = after
        runner.state.bot_muted_ids = {speaker.user_id}
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())
        try:
            await runner.on_voice_state_update(speaker.member, before, after)
            self.assertFalse(runner.state.paused)
        finally:
            runner.state.game_task.cancel()

    async def test_observer_join_api_exhaustion_safety_stops(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.NIGHT
        observer = FakeMember(90, "observer")
        marker = FakeRole(99, runner._mute_marker_role_name())
        voice_channel = SimpleNamespace(id=50, members=[observer])
        observer.voice = FakeVoiceState(
            mute=False, channel=voice_channel, suppress=True
        )
        observer.guild = FakeGuild([observer], [marker])
        runner.state.guild = observer.guild
        runner.state.voice_channel = voice_channel
        runner.state.mute_marker_enabled = True
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())
        unavailable = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable", headers={}),
            "retry failed",
        )
        observer.edit = AsyncMock(side_effect=unavailable)
        before = FakeVoiceState(mute=False, channel=None)
        try:
            with patch("room_runner.asyncio.sleep", new=AsyncMock()), self.assertRaises(
                StateDurabilityError
            ):
                await runner.on_voice_state_update(observer, before, observer.voice)

            self.assertTrue(runner.state.paused)
            self.assertEqual(observer.edit.await_count, 3)
            self.assertFalse(runner.state.recovered_from_restart)
        finally:
            runner.state.game_task.cancel()

    async def test_unsuppressed_observer_join_pauses_before_member_patch_wait(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.NIGHT
        observer = FakeMember(91, "observer")
        marker = FakeRole(99, runner._mute_marker_role_name())
        voice_channel = SimpleNamespace(id=50, members=[observer])
        observer.voice = FakeVoiceState(
            mute=False, channel=voice_channel, suppress=False
        )
        observer.guild = FakeGuild([observer], [marker])
        runner.state.guild = observer.guild
        runner.state.voice_channel = voice_channel
        runner.state.mute_marker_enabled = True
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())

        async def apply_edit(**kwargs):
            if "mute" in kwargs:
                observer.voice.mute = kwargs["mute"]
            if "roles" in kwargs:
                observer.roles = kwargs["roles"]
            if "nick" in kwargs:
                observer.nick = kwargs["nick"]
            return observer

        observer.edit = AsyncMock(side_effect=apply_edit)
        before = FakeVoiceState(mute=False, channel=None)
        try:
            await runner.on_voice_state_update(observer, before, observer.voice)

            self.assertTrue(runner.state.paused)
            self.assertTrue(observer.voice.mute)
            first_kwargs = observer.edit.await_args_list[0].kwargs
            self.assertTrue(first_kwargs["mute"])
            self.assertNotIn("mute", observer.edit.await_args_list[1].kwargs)
        finally:
            runner.state.game_task.cancel()

    async def test_vote_timer_does_not_start_if_speaker_grant_fails(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        runner.state.day_generation = 1
        runner.state.vote_day_generation = 1
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_index = 0

        async def confirm_vote(_event) -> None:
            runner.state.votes[voter.user_id] = voter.user_id

        runner._pausable_wait_forever = AsyncMock(side_effect=confirm_vote)
        runner._grant_speaker = AsyncMock(
            side_effect=StateDurabilityError("unmute failed")
        )
        runner._pausable_countdown = AsyncMock()

        with self.assertRaises(StateDurabilityError):
            await runner._day_vote()

        runner._pausable_countdown.assert_not_awaited()

    async def test_teardown_keeps_ownership_if_pending_ledger_is_not_durable(self) -> None:
        runner = make_runner()
        member = FakeMember(1)
        member.voice = None
        guild = FakeGuild([member], [])
        member.guild = guild
        runner.state.guild = guild
        runner.state.bot_muted_ids = {member.id}
        runner.manager.register_pending_unmutes = AsyncMock(return_value=False)

        completed = await runner._teardown_game_roles_and_perms()

        self.assertFalse(completed)
        self.assertEqual(runner.state.bot_muted_ids, {member.id})

    async def test_stale_pending_row_never_reunmutes_later_manual_mute(self) -> None:
        manager = object.__new__(GameCog)
        manager.rooms = {}
        manager.pending_unmutes = {123: {1}}
        manager._startup_active_vc_rooms = {}

        async def paced_call(func, *args, **kwargs):
            return await func(*args, **kwargs)

        manager.paced_discord_api_call = AsyncMock(side_effect=paced_call)
        member = FakeMember(1)
        marker = FakeRole(2, "人狼Botミュート:old-room")
        member.roles = [marker]
        member.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=20)
        )
        member.guild = FakeGuild([member], [marker])

        async def apply_unmute(**kwargs):
            member.voice.mute = kwargs["mute"]
            member.roles = kwargs["roles"]
            return member

        member.edit = AsyncMock(side_effect=apply_unmute)
        with (
            patch("game.asyncio.sleep", new=AsyncMock()),
            patch(
                "game.database.remove_pending_unmute",
                new=AsyncMock(side_effect=RuntimeError("DB down")),
            ),
        ):
            await manager._resolve_pending_unmute(member)

        self.assertIn(member.id, manager.pending_unmutes[123])
        self.assertFalse(member.voice.mute)
        self.assertEqual(member.edit.await_count, 1)

        # Discord側成功後にDBだけ古い窓。後から付いた手動muteは保護する。
        member.voice.mute = True
        with patch(
            "game.database.remove_pending_unmute", new=AsyncMock()
        ):
            await manager._resolve_pending_unmute(member)

        self.assertTrue(member.voice.mute)
        self.assertEqual(member.edit.await_count, 1)
        self.assertNotIn(member.id, manager.pending_unmutes[123])

    async def test_pending_ledger_write_exhaustion_is_reported(self) -> None:
        manager = object.__new__(GameCog)
        manager.pending_unmutes = {}
        guild = SimpleNamespace(id=123)
        with (
            patch("game.asyncio.sleep", new=AsyncMock()),
            patch(
                "game.database.add_pending_unmutes",
                new=AsyncMock(side_effect=RuntimeError("DB down")),
            ) as add_pending,
        ):
            durable = await manager.register_pending_unmutes(guild, {1, 2})

        self.assertFalse(durable)
        self.assertEqual(add_pending.await_count, 3)
        self.assertEqual(manager.pending_unmutes[123], {1, 2})

    async def test_pending_ledger_read_failure_stops_startup_path(self) -> None:
        manager = object.__new__(GameCog)
        manager.pending_unmutes = {}
        guild = SimpleNamespace(id=123)
        with (
            patch(
                "game.database.load_pending_unmute_ids",
                new=AsyncMock(side_effect=RuntimeError("DB down")),
            ),
            self.assertRaisesRegex(RuntimeError, "起動を停止"),
        ):
            await manager.load_pending_unmutes(guild)


if __name__ == "__main__":
    unittest.main()
