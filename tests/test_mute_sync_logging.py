"""_sync_server_mutesが無言で失敗せず、安全停止することの回帰テスト。"""
from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from room_runner import StateDurabilityError
from tests.test_turn_discussion import add_players, make_runner


class MuteSyncLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_voice_channel_safety_stops_active_game(self) -> None:
        runner = make_runner()
        add_players(runner, 1)
        runner.state.voice_channel = None

        with self.assertLogs("room_runner", level="ERROR") as cm, self.assertRaises(
            StateDurabilityError
        ):
            await runner._sync_server_mutes(set())

        self.assertTrue(runner.state.paused)
        self.assertTrue(
            any("サーバーミュート同期に失敗" in m for m in cm.output)
        )
        self.assertTrue(any(runner.state.room_name in m for m in cm.output))

    async def test_no_targets_logs_breakdown_counts(self) -> None:
        runner = make_runner()
        speaker, muted_already = add_players(runner, 2)
        voice_channel = SimpleNamespace(
            id=50, members=[speaker.member, muted_already.member]
        )
        runner.state.voice_channel = voice_channel

        # speaker: 発言可対象で既にunmute -> 変更不要
        speaker.member.bot = False
        speaker.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=False)
        # muted_already: 発言不可対象で既にmute -> 変更不要
        muted_already.member.bot = False
        muted_already.member.voice = SimpleNamespace(channel=voice_channel, mute=True, suppress=False)

        with self.assertLogs("room_runner", level="INFO") as cm:
            result = await runner._sync_server_mutes({speaker.user_id})

        self.assertEqual(result, [])
        info_lines = [m for m in cm.output if "ミュート同期: 対象0件" in m]
        self.assertEqual(len(info_lines), 1)
        line = info_lines[0]
        self.assertIn("VC接続2人", line)
        self.assertIn("suppressだが対象化=0", line)
        self.assertIn("既にmute=2", line)
        self.assertIn("bot=0", line)
        self.assertIn("gm=0", line)
        self.assertIn("VC不一致=0", line)

    async def test_suppressed_speaker_is_still_muted_and_logged(self) -> None:
        """suppress中でも発言不可対象なら必ずサーバーミュートする (取りこぼし対策)。"""
        runner = make_runner()
        (joiner,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[joiner.member])
        runner.state.voice_channel = voice_channel

        joiner.member.bot = False
        joiner.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=True)

        async def edit_member(*, mute=None, _member=joiner.member, **_kwargs):
            if mute is not None:
                _member.voice.mute = mute
            return _member

        joiner.member.edit = edit_member

        with self.assertLogs("room_runner", level="INFO") as cm:
            changed = await runner._sync_server_mutes(set())

        self.assertTrue(joiner.member.voice.mute)
        changed_ids = {m.id for m, _ in changed}
        self.assertEqual(changed_ids, {joiner.user_id})
        self.assertTrue(
            any("suppress中の相手もミュート対象に含めました" in m for m in cm.output)
        )

    async def test_empty_vc_members_still_finds_targets_via_state_players(self) -> None:
        """vc.membersが空(キャッシュ漏れ)でも、state.players側からミュート対象を拾える。"""
        runner = make_runner()
        (victim,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[])
        runner.state.voice_channel = voice_channel
        marker_role = SimpleNamespace(id=99, name=runner._mute_marker_role_name())
        runner.state.mute_marker_enabled = True
        runner.state.guild = SimpleNamespace(
            get_member=lambda uid: victim.member if uid == victim.user_id else None,
            roles=[marker_role],
        )

        victim.member.bot = False
        victim.member.roles = []
        victim.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=False)

        async def edit_member(*, mute=None, _member=victim.member, **_kwargs):
            if mute is not None:
                _member.voice.mute = mute
            return _member

        victim.member.edit = edit_member

        changed = await runner._sync_server_mutes(set())

        self.assertTrue(victim.member.voice.mute)
        changed_ids = {m.id for m, _ in changed}
        self.assertEqual(changed_ids, {victim.user_id})

    async def test_thirteen_player_empty_cache_keeps_participant_gm_manual_mute(self) -> None:
        """13人村でも参加者兼GMだけはBotのmute対象から外す。"""
        runner = make_runner()
        players = add_players(runner, 13)
        voice_channel = SimpleNamespace(id=50, members=[])
        runner.state.voice_channel = voice_channel
        runner.state.gm_id = players[-1].user_id
        marker_role = SimpleNamespace(id=99, name=runner._mute_marker_role_name())
        runner.state.mute_marker_enabled = True
        by_id = {player.user_id: player.member for player in players}
        runner.state.guild = SimpleNamespace(
            get_member=by_id.get,
            roles=[marker_role],
        )

        for player in players:
            player.member.bot = False
            player.member.roles = []
            player.member.voice = SimpleNamespace(
                channel=voice_channel, mute=False, suppress=False
            )

            async def edit_member(*, mute=None, _member=player.member, **_kwargs):
                if mute is not None:
                    _member.voice.mute = mute
                return _member

            player.member.edit = AsyncMock(side_effect=edit_member)

        changed = await runner._sync_server_mutes(set())

        self.assertEqual(len(changed), 12)
        self.assertTrue(all(player.member.voice.mute for player in players[:-1]))
        self.assertFalse(players[-1].member.voice.mute)
        players[-1].member.edit.assert_not_awaited()

    async def test_retry_exhaustion_logs_final_failed_members(self) -> None:
        runner = make_runner()
        (victim,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[victim.member])
        runner.state.voice_channel = voice_channel
        victim.member.bot = False
        victim.member.voice = SimpleNamespace(
            channel=voice_channel, mute=False, suppress=False
        )
        unavailable = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable", headers={}),
            "retry failed",
        )
        runner._paced_discord_api_call = AsyncMock(side_effect=unavailable)
        # 実装はDiscord Member.editをペーサー経由で呼ぶため、
        # この失敗経路でも本番Member相当の属性を用意しておく。
        victim.member.edit = AsyncMock()

        with (
            patch("room_runner.asyncio.sleep", new=AsyncMock()),
            self.assertLogs("room_runner", level="ERROR") as cm,
            self.assertRaises(StateDurabilityError),
        ):
            await runner._sync_server_mutes(set())

        self.assertTrue(runner.state.paused)
        self.assertEqual(runner._paced_discord_api_call.await_count, 2)
        final_lines = [line for line in cm.output if "ミュート最終失敗" in line]
        self.assertEqual(len(final_lines), 1)
        self.assertIn(victim.member.display_name, final_lines[0])
        self.assertIn("mute=True", final_lines[0])

    async def test_participant_gm_is_not_auto_muted_when_not_speaking(self) -> None:
        runner = make_runner()
        (gm,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[gm.member])
        runner.state.voice_channel = voice_channel
        runner.state.gm_id = gm.user_id

        gm.member.bot = False
        gm.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=False)
        async def edit_member(*, mute=None, **_kwargs):
            if mute is not None:
                gm.member.voice.mute = mute
            return gm.member

        gm.member.edit = AsyncMock(side_effect=edit_member)

        changed = await runner._sync_server_mutes(set())

        self.assertEqual(changed, [])
        self.assertFalse(gm.member.voice.mute)
        gm.member.edit.assert_not_awaited()

    async def test_participant_gm_keeps_manual_mute_without_bot_ownership(self) -> None:
        """マーカーも所有記録もないGMの手動muteは解除しない。"""
        runner = make_runner()
        (gm,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[gm.member])
        runner.state.voice_channel = voice_channel
        runner.state.gm_id = gm.user_id
        gm.member.bot = False
        gm.member.roles = []
        gm.member.voice = SimpleNamespace(
            channel=voice_channel, mute=True, suppress=False,
        )
        gm.member.edit = AsyncMock()

        changed = await runner._sync_server_mutes({gm.user_id})

        self.assertEqual(changed, [])
        self.assertTrue(gm.member.voice.mute)
        self.assertNotIn(gm.user_id, runner.state.bot_muted_ids)
        gm.member.edit.assert_not_awaited()

    async def test_participant_gm_releases_only_legacy_bot_owned_mute(self) -> None:
        """旧版のBot所有muteは解除し、マーカーと所有記録も捨てる。"""
        runner = make_runner()
        (gm,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[gm.member])
        runner.state.voice_channel = voice_channel
        runner.state.gm_id = gm.user_id
        marker = SimpleNamespace(id=99, name=runner._mute_marker_role_name())
        runner.state.mute_marker_enabled = True
        runner.state.guild = SimpleNamespace(
            get_member=lambda user_id: gm.member if user_id == gm.user_id else None,
            roles=[marker],
        )
        runner.state.bot_muted_ids = {gm.user_id}
        runner.state.bot_mute_intent_ids = {gm.user_id}
        gm.member.bot = False
        gm.member.roles = [marker]
        gm.member.voice = SimpleNamespace(
            channel=voice_channel, mute=True, suppress=False,
        )

        async def edit_member(*, mute=None, roles=None, **_kwargs):
            if mute is not None:
                gm.member.voice.mute = mute
            if roles is not None:
                gm.member.roles = roles
            return gm.member

        gm.member.edit = AsyncMock(side_effect=edit_member)

        changed = await runner._sync_server_mutes(set())

        self.assertEqual(changed, [(gm.member, False)])
        self.assertFalse(gm.member.voice.mute)
        self.assertNotIn(marker, gm.member.roles)
        self.assertNotIn(gm.user_id, runner.state.bot_muted_ids)
        self.assertNotIn(gm.user_id, runner.state.bot_mute_intent_ids)
        self.assertEqual(gm.member.edit.await_args.kwargs["mute"], False)

    async def test_dedicated_gm_is_not_muted(self) -> None:
        runner = make_runner()
        (gm,) = add_players(runner, 1)
        runner.state.players.pop(gm.user_id)
        voice_channel = SimpleNamespace(id=50, members=[gm.member])
        runner.state.voice_channel = voice_channel
        runner.state.gm_id = gm.user_id

        gm.member.bot = False
        gm.member.voice = SimpleNamespace(
            channel=voice_channel, mute=False, suppress=False,
        )
        gm.member.edit = AsyncMock()

        changed = await runner._sync_server_mutes(set())

        self.assertEqual(changed, [])
        self.assertFalse(gm.member.voice.mute)
        gm.member.edit.assert_not_awaited()

    async def test_already_muted_target_is_not_edited_again(self) -> None:
        """既にBotがmute済み(=対象外)の相手には二重にmember.editしない。"""
        runner = make_runner()
        (silent,) = add_players(runner, 1)
        voice_channel = SimpleNamespace(id=50, members=[silent.member])
        runner.state.voice_channel = voice_channel

        silent.member.bot = False
        silent.member.voice = SimpleNamespace(channel=voice_channel, mute=True, suppress=False)
        runner.state.bot_muted_ids = {silent.user_id}
        silent.member.edit = AsyncMock()

        changed = await runner._sync_server_mutes(set())

        self.assertEqual(changed, [])
        self.assertTrue(silent.member.voice.mute)
        silent.member.edit.assert_not_awaited()

    async def test_normal_mute_sync_still_mutes_and_unmutes_as_before(self) -> None:
        """挙動そのもの (mute/unmuteの実行結果) が変わっていないことの確認。"""
        runner = make_runner()
        speaker, other = add_players(runner, 2)
        voice_channel = SimpleNamespace(id=50, members=[speaker.member, other.member])
        runner.state.voice_channel = voice_channel

        for player in (speaker, other):
            player.member.bot = False
            player.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=False)

            async def edit_member(*, mute=None, _member=player.member, **_kwargs):
                if mute is not None:
                    _member.voice.mute = mute
                return _member

            player.member.edit = edit_member

        changed = await runner._sync_server_mutes({speaker.user_id})

        self.assertFalse(speaker.member.voice.mute)
        self.assertTrue(other.member.voice.mute)
        changed_ids = {m.id for m, _ in changed}
        self.assertEqual(changed_ids, {other.user_id})


if __name__ == "__main__":
    unittest.main()
