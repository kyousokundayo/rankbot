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
    async def test_missing_mute_marker_enters_recoverable_safety_stop(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        runner.state.guild = FakeGuild([], [])
        runner.state.voice_channel = SimpleNamespace(id=50, members=[])
        runner.state.mute_marker_enabled = True
        runner._ensure_mute_marker_role = AsyncMock(return_value=None)

        with self.assertRaisesRegex(StateDurabilityError, "確保できません"):
            await runner._sync_server_mutes(set())

        self.assertTrue(runner.state.paused)
        self.assertEqual(runner.state.phase, Phase.PAUSED)
        self.assertEqual(runner.state.recovery_phase, Phase.DAY_DISCUSSION)
        self.assertTrue(runner.state.recovered_from_restart)
        runner._safe_village_send.assert_awaited()

    async def test_midgame_elimination_pauses_before_committing_death(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        runner.state.phase = Phase.DAY_DISCUSSION
        runner.state.check_win = lambda: None
        runner._confirm_surrender_after_roster_change = AsyncMock(
            return_value=False
        )
        sequence: list[str] = []
        keep_game_alive = asyncio.Event()
        runner.state.game_task = asyncio.create_task(keep_game_alive.wait())

        async def pause_for_elimination(_reason: str) -> str:
            self.assertTrue(player.alive)
            sequence.append("pause")
            runner.state.paused = True
            runner.state.phase_before_pause = runner.state.phase
            runner.state.phase = Phase.PAUSED
            runner.state.pause_event.clear()
            return "paused"

        async def verify_all_muted(_speakers: set[int], _context: str) -> None:
            self.assertTrue(player.alive)
            self.assertTrue(runner.state.paused)
            self.assertFalse(runner.state.pause_event.is_set())
            sequence.append("mute-verified")

        async def apply_death(_effect: dict) -> None:
            self.assertFalse(player.alive)
            sequence.append("death-effect")

        runner.pause_game = AsyncMock(side_effect=pause_for_elimination)
        runner._ensure_mute_state_or_stop = AsyncMock(
            side_effect=verify_all_muted
        )
        runner._apply_death_effect = AsyncMock(side_effect=apply_death)
        try:
            completed = await runner._eliminate_player_mid_game(
                player, "GM除外"
            )
        finally:
            runner.state.game_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner.state.game_task

        self.assertTrue(completed)
        self.assertEqual(sequence, ["pause", "mute-verified", "death-effect"])
        self.assertTrue(runner.state.paused)
        self.assertFalse(runner.state.pause_event.is_set())

    async def test_manual_mute_does_not_block_the_speech_gate(self) -> None:
        """手動muteの相手を待たない。喋れないだけで進行は続ける。

        Botは自分が付けていないmuteを保護して外さないので、待つと外部の
        操作ひとつでゲームが復帰不能になる。fail-closedで守るのは
        「喋ってはいけない人が喋れる」側だけ。
        """
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.phase = Phase.DAY_RUNOFF_SPEECH
        runner.state.current_speaker_id = speaker.user_id
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        speaker.member.voice = FakeVoiceState(mute=True, channel=voice_channel)
        runner.state.bot_muted_ids = set()

        with patch("room_runner.MUTE_GRACE_TIME", 0):
            await asyncio.wait_for(
                runner._grant_speaker(speaker.member), timeout=0.5
            )

        self.assertFalse(runner.state.paused)
        # 保護しているので勝手に外さない。
        self.assertTrue(speaker.member.voice.mute)
        self.assertEqual(runner._mute_state_issues({speaker.user_id}), [])

    async def test_bot_owned_mute_that_will_not_clear_still_stops(self) -> None:
        """Bot自身が付けたmuteを外せないのはBot側の失敗なので止める。

        こちらはGMが権限・ロール順位を直せば解消できる。手動muteと違って
        待つ意味があるため、従来どおりfail-closedのまま残す。
        """
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.phase = Phase.DAY_RUNOFF_SPEECH
        runner.state.current_speaker_id = speaker.user_id
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        speaker.member.voice = FakeVoiceState(mute=True, channel=voice_channel)
        runner.state.bot_muted_ids = {speaker.user_id}

        issues = runner._mute_state_issues({speaker.user_id})

        self.assertTrue(any("mute解除未反映" in issue for issue in issues))

    async def test_participant_gm_manual_mute_is_left_alone(self) -> None:
        """参加者兼任GMの手動muteも、外さないし待たない。"""
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.gm_id = speaker.user_id
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        speaker.member.voice = FakeVoiceState(mute=True, channel=voice_channel)
        runner.state.bot_muted_ids = set()

        self.assertEqual(runner._mute_state_issues({speaker.user_id}), [])
        speaker.member.edit.assert_not_awaited()

    async def test_gm_self_mute_is_not_announced(self) -> None:
        """GMの自己ミュートは通常運用なので告知しない。

        クロストーク議論ではGMも発言枠を持つため、告知すると
        GMがミュートし直すたびに #昼 が鳴ってしまう。
        """
        runner = make_runner()
        gm = add_player(runner, 1)
        runner.state.gm_id = gm.user_id
        runner.state.phase = Phase.DAY_DISCUSSION
        voice_channel = SimpleNamespace(id=50, members=[gm.member])
        runner.state.voice_channel = voice_channel
        before = FakeVoiceState(mute=False, channel=voice_channel)
        after = FakeVoiceState(mute=True, channel=voice_channel)
        gm.member.voice = after
        runner.state.bot_muted_ids = set()
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())
        try:
            await runner.on_voice_state_update(gm.member, before, after)

            self.assertFalse(runner.state.paused)
            runner._safe_village_send.assert_not_awaited()
            gm.member.edit.assert_not_awaited()
        finally:
            runner.state.game_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner.state.game_task

    async def test_external_mute_of_speaker_announces_without_pausing(self) -> None:
        """発言者への手動muteでは止めない。喋れないだけなので知らせて続行する。

        止めてしまうと、モデレーターの操作ひとつでゲームが復帰不能になる
        (Botは自分が付けていないmuteを保護するので自動では解けない)。
        """
        runner = make_runner()
        player = add_player(runner, 1)
        runner.state.phase = Phase.DAY_RUNOFF_SPEECH
        runner.state.current_speaker_id = player.user_id
        voice_channel = SimpleNamespace(id=50, members=[player.member])
        runner.state.voice_channel = voice_channel
        before = FakeVoiceState(mute=False, channel=voice_channel)
        after = FakeVoiceState(mute=True, channel=voice_channel)
        player.member.voice = after
        runner.state.bot_muted_ids = set()
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())
        try:
            await runner.on_voice_state_update(player.member, before, after)

            self.assertFalse(runner.state.paused)
            self.assertTrue(player.member.voice.mute)
            announced = "\n".join(
                str(call.args[0])
                for call in runner._safe_village_send.await_args_list
            )
            self.assertIn("手動でサーバーミュート", announced)
            self.assertIn("進行はこのまま続きます", announced)
        finally:
            runner.state.game_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner.state.game_task

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

    async def test_gateway_mute_before_ownership_checkpoint_does_not_false_pause(self) -> None:
        """HTTP応答前のGateway更新もBot自身のフェーズ切替として扱う。"""
        runner = make_runner()
        speaker = add_player(runner, 1)
        runner.state.phase = Phase.DAY_DISCUSSION
        voice_channel = SimpleNamespace(id=50, members=[speaker.member])
        runner.state.voice_channel = voice_channel
        speaker.member.voice = FakeVoiceState(mute=False, channel=voice_channel)
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())

        async def edit_and_deliver_gateway_first(**kwargs):
            before = FakeVoiceState(mute=False, channel=voice_channel)
            after = FakeVoiceState(mute=kwargs["mute"], channel=voice_channel)
            speaker.member.voice = after
            # 実際に起こり得る順序: RESTのawaitが戻る前にGateway更新を処理する。
            await runner.on_voice_state_update(speaker.member, before, after)
            return speaker.member

        speaker.member.edit = AsyncMock(side_effect=edit_and_deliver_gateway_first)
        try:
            await runner._sync_server_mutes(set())

            self.assertFalse(runner.state.paused)
            self.assertTrue(speaker.member.voice.mute)
            self.assertIn(speaker.user_id, runner.state.bot_muted_ids)
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

    async def test_teardown_announces_durable_automatic_unmute_retry(self) -> None:
        runner = make_runner()
        member = FakeMember(1)
        member.voice = None
        guild = FakeGuild([member], [])
        member.guild = guild
        runner.state.guild = guild
        runner.state.bot_muted_ids = {member.id}
        runner.manager.register_pending_unmutes = AsyncMock(return_value=True)

        completed = await runner._teardown_game_roles_and_perms()

        self.assertTrue(completed)
        self.assertEqual(runner.state.bot_muted_ids, set())
        notice = "\n".join(
            str(call.args[0])
            for call in runner._safe_village_send.await_args_list
            if call.args
        )
        self.assertIn("2分ごと", notice)
        self.assertIn("次のVC入室時", notice)

    async def test_stale_pending_row_never_reunmutes_later_manual_mute(self) -> None:
        manager = object.__new__(GameCog)
        manager.rooms = {}
        manager.pending_unmutes = {123: {1}}
        manager.pending_unmute_locks = {}
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

    async def test_periodic_pending_retry_recovers_only_safe_connected_members(self) -> None:
        manager = object.__new__(GameCog)
        manager.rooms = {}
        manager.pending_unmutes = {123: {1, 2, 3}}
        manager.pending_unmute_locks = {}
        manager._startup_active_vc_rooms = {30: "active-room"}

        async def paced_call(func, *args, **kwargs):
            return await func(*args, **kwargs)

        manager.paced_discord_api_call = AsyncMock(side_effect=paced_call)
        marker = FakeRole(10, "人狼Botミュート:old-room")
        recoverable = FakeMember(1)
        recoverable.roles = [marker]
        recoverable.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=20)
        )
        protected = FakeMember(2)
        protected.roles = [marker]
        protected.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=30)
        )
        disconnected = FakeMember(3)
        disconnected.roles = [marker]
        guild = FakeGuild(
            [recoverable, protected, disconnected], [marker]
        )
        for member in guild.members:
            member.guild = guild

        async def apply_unmute(**kwargs):
            recoverable.voice.mute = kwargs["mute"]
            recoverable.roles = kwargs["roles"]
            return recoverable

        recoverable.edit = AsyncMock(side_effect=apply_unmute)
        with patch(
            "game.database.remove_pending_unmute", new=AsyncMock()
        ) as remove_pending:
            attempted, resolved = await manager._retry_pending_unmutes(guild)

        self.assertEqual((attempted, resolved), (1, 1))
        self.assertFalse(recoverable.voice.mute)
        self.assertEqual(manager.pending_unmutes[123], {2, 3})
        protected.edit.assert_not_awaited()
        disconnected.edit.assert_not_awaited()
        remove_pending.assert_awaited_once_with(123, recoverable.id)

    async def test_voice_event_and_periodic_retry_do_not_double_unmute(self) -> None:
        manager = object.__new__(GameCog)
        manager.rooms = {}
        manager.pending_unmutes = {123: {1}}
        manager.pending_unmute_locks = {}
        manager._startup_active_vc_rooms = {}

        async def paced_call(func, *args, **kwargs):
            return await func(*args, **kwargs)

        manager.paced_discord_api_call = AsyncMock(side_effect=paced_call)
        marker = FakeRole(10, "人狼Botミュート:old-room")
        member = FakeMember(1)
        member.roles = [marker]
        member.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=20)
        )
        guild = FakeGuild([member], [marker])
        member.guild = guild
        patch_started = asyncio.Event()
        release_patch = asyncio.Event()

        async def apply_unmute(**kwargs):
            patch_started.set()
            await release_patch.wait()
            member.voice.mute = kwargs["mute"]
            member.roles = kwargs["roles"]
            return member

        member.edit = AsyncMock(side_effect=apply_unmute)
        with patch(
            "game.database.remove_pending_unmute", new=AsyncMock()
        ) as remove_pending:
            first = asyncio.create_task(manager._resolve_pending_unmute(member))
            await asyncio.wait_for(patch_started.wait(), timeout=1)
            second = asyncio.create_task(manager._resolve_pending_unmute(member))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            release_patch.set()
            await asyncio.gather(first, second)

        self.assertEqual(member.edit.await_count, 1)
        remove_pending.assert_awaited_once_with(123, member.id)
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


class ResumeWithDisconnectedSpeakersTest(unittest.IsolatedAsyncioTestCase):
    """VC未接続の話者が「発言制御の不整合」に数えられないことの回帰テスト。

    一時停止は全員がVCを抜けた状態で解除されることが多い。未接続者を
    不整合として数えると `_sync_server_mutes` が触れない相手を待ち続け、
    再開が恒久的に拒否されて廃村以外の出口が無くなる。
    """

    def _runner_in_paused_discussion(self):
        runner = make_runner()
        vc = SimpleNamespace(id=50, members=[])
        runner.state.voice_channel = vc
        runner.state.guild = FakeGuild([], [])
        present = add_player(runner, 1)
        absent = add_player(runner, 2)
        present.member.voice = FakeVoiceState(mute=True, channel=vc)
        absent.member.voice = None
        vc.members.append(present.member)
        # Bot所有のmute。手動muteは待たない仕様なので、解除待ちを見る
        # テストでは所有記録を持たせる必要がある。
        runner.state.bot_muted_ids = {present.user_id}
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.DAY_DISCUSSION
        runner.state.paused = True
        runner.state.pause_event.clear()
        runner.state.disconnected_players = {absent.user_id}
        return runner, present, absent

    def test_absent_speaker_is_not_an_issue(self) -> None:
        runner, present, absent = self._runner_in_paused_discussion()
        present.member.voice.mute = False

        self.assertEqual(
            runner._mute_state_issues({present.user_id, absent.user_id}), []
        )

    def test_present_speaker_still_checked_while_others_are_absent(self) -> None:
        runner, present, absent = self._runner_in_paused_discussion()

        issues = runner._mute_state_issues({present.user_id, absent.user_id})

        self.assertTrue(any("mute解除未反映" in issue for issue in issues))

    async def test_resume_succeeds_while_a_player_is_still_out_of_vc(self) -> None:
        runner, present, absent = self._runner_in_paused_discussion()

        async def unmute(speakers, **_kwargs):
            present.member.voice.mute = present.user_id not in speakers
            return []

        runner._sync_server_mutes = AsyncMock(side_effect=unmute)

        with patch("room_runner.MUTE_GRACE_TIME", 0):
            result = await runner.resume_game()

        self.assertIn("再開", result)
        self.assertFalse(runner.state.paused)
        self.assertEqual(runner.state.phase, Phase.DAY_DISCUSSION)
        self.assertTrue(runner.state.pause_event.is_set())
        # 未復帰者がいることはGMへ告知するが、再開そのものは止めない。
        self.assertTrue(
            any(
                "未復帰" in str(call.args[0])
                for call in runner._safe_village_send.await_args_list
            )
        )


class SettlementPrepFailureTest(unittest.IsolatedAsyncioTestCase):
    """精算stage前に落ちても村が「進行中でも終了済みでもない」状態に残らない。

    `ending` を立てたまま抜けると force_end が二重実行ガードで即Falseを返し、
    自動廃村もGMの強制終了・リセット・除外も効かず、pending_winner が無いので
    「再開」も精算再試行にならない。GM操作が全滅した村だけが残る。
    """

    async def test_ending_is_released_when_prep_raises(self) -> None:
        from config import Team

        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.DAY_DISCUSSION
        runner._collect_settlement_inputs = AsyncMock(
            side_effect=RuntimeError("DB down")
        )

        with self.assertRaisesRegex(RuntimeError, "DB down"):
            await runner._end_game(Team.VILLAGE)

        self.assertFalse(runner.state.ending)
        self.assertIsNone(runner.state.pending_winner)
        # force_end / _settlement_locked の入口が塞がっていないこと。
        self.assertFalse(
            runner.state.phase in (Phase.LOBBY, Phase.GAME_OVER)
            or runner.state.ending
        )

    async def test_ending_stays_set_once_the_winner_is_staged(self) -> None:
        """pending_winner を立てた後は ending を残す (「再開」で精算を再試行する)。"""
        from config import Team

        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.DAY_DISCUSSION
        runner._collect_settlement_inputs = AsyncMock(
            return_value=([], {}, None)
        )
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))

        await runner._end_game(Team.VILLAGE)

        self.assertEqual(runner.state.pending_winner, Team.VILLAGE)
        self.assertTrue(runner.state.paused)
        self.assertEqual(runner.state.phase, Phase.PAUSED)


class MuteStopDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    """安全停止の告知が、原因にたどり着ける内容になっていること。

    以前は「Botのメンバーミュート権限とロール順位を確認し」しか出しておらず、
    実際の原因がモデレーターの手動ミュートでもGMがそこへ行き着けなかった。
    Botは自分が付けていないミュートを保護する = 自動では解けないので、
    誰のどの状態が残っているかを出さないと停止したまま原因不明になる。
    """

    def _runner_with_stuck_owned_mute(self):
        runner = make_runner()
        vc = SimpleNamespace(id=50, members=[])
        runner.state.voice_channel = vc
        runner.state.guild = FakeGuild([], [])
        speaker = add_player(runner, 1)
        speaker.member.voice = FakeVoiceState(mute=True, channel=vc)
        vc.members.append(speaker.member)
        # Bot自身が付けたmuteを外せない状態 (権限・ロール順位など)。
        # 手動muteは待たずに進むので、停止するのはこちらだけ。
        runner.state.bot_muted_ids = {speaker.user_id}
        return runner, speaker

    def test_detail_names_the_member_and_the_cause(self) -> None:
        runner, speaker = self._runner_with_stuck_owned_mute()

        detail = runner._mute_stop_detail({speaker.user_id})

        self.assertIn(speaker.member.display_name, detail)
        self.assertIn("mute解除未反映", detail)
        self.assertIn("権限", detail)

    def test_detail_is_empty_when_nothing_is_pending(self) -> None:
        runner, speaker = self._runner_with_stuck_owned_mute()
        speaker.member.voice.mute = False

        self.assertEqual(runner._mute_stop_detail({speaker.user_id}), "")

    async def test_failed_resume_names_the_speaker_it_tried_to_unmute(self) -> None:
        """再開失敗の告知に、解除できなかった話者が載ること。

        停止状態では `_current_speaker_ids()` が空集合を返すので、告知の中で
        取り直すと肝心の相手が抜ける。再開しようとした話者集合を渡す。
        """
        runner, speaker = self._runner_with_stuck_owned_mute()

        await runner._restore_pause_after_failed_mute_resume(
            Phase.DAY_DISCUSSION, "再開時の発言制御", {speaker.user_id}
        )

        self.assertTrue(runner.state.paused)
        announced = "\n".join(
            str(call.args[0])
            for call in runner._safe_village_send.await_args_list
        )
        self.assertIn(speaker.member.display_name, announced)
        self.assertIn("mute解除未反映", announced)

    async def test_safety_stop_announcement_carries_the_detail(self) -> None:
        runner, speaker = self._runner_with_stuck_owned_mute()
        runner.state.phase = Phase.DAY_RUNOFF_SPEECH
        runner.state.current_speaker_id = speaker.user_id
        runner._sync_server_mutes = AsyncMock(return_value=[])

        with patch("room_runner.MUTE_GRACE_TIME", 0):
            stop = asyncio.create_task(
                runner._wait_for_mute_sync_or_pause(
                    [], {speaker.user_id}, "テスト用の発言制御"
                )
            )
            for _ in range(20):
                await asyncio.sleep(0)
                if runner.state.paused:
                    break
            self.assertTrue(runner.state.paused)
            announced = "\n".join(
                str(call.args[0])
                for call in runner._safe_village_send.await_args_list
            )
            self.assertIn(speaker.member.display_name, announced)
            self.assertIn("mute解除未反映", announced)
            # 原因が分からないまま権限だけを疑わせない。
            self.assertNotIn(
                "Botのメンバーミュート権限とロール順位を確認し、解消後に", announced
            )
        finally_speaker = speaker
        finally_speaker.member.voice.mute = False
        runner.state.paused = False
        runner.state.pause_event.set()
        await asyncio.wait_for(stop, timeout=0.5)
