"""_sync_server_mutesの「無言の失敗」を可視化するログの回帰テスト。

voice_channel未解決・対象0件のケースで例外にならずログが出ること、
および通常のミュート同期が従来どおり動くことを確認する。
"""
from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

from tests.test_turn_discussion import add_players, make_runner


class MuteSyncLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_voice_channel_logs_warning_and_returns_empty(self) -> None:
        runner = make_runner()
        add_players(runner, 1)
        runner.state.voice_channel = None

        with self.assertLogs("room_runner", level="WARNING") as cm:
            result = await runner._sync_server_mutes(set())

        self.assertEqual(result, [])
        self.assertTrue(
            any("VC未解決のためミュート同期をスキップしました" in m for m in cm.output)
        )
        self.assertTrue(any(runner.state.room_name in m for m in cm.output))

    async def test_no_targets_logs_breakdown_counts(self) -> None:
        runner = make_runner()
        speaker, muted_already, joiner = add_players(runner, 3)
        voice_channel = SimpleNamespace(
            id=50, members=[speaker.member, muted_already.member, joiner.member]
        )
        runner.state.voice_channel = voice_channel

        # speaker: 発言可対象で既にunmute -> 変更不要
        speaker.member.bot = False
        speaker.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=False)
        # muted_already: 発言不可対象で既にmute -> 変更不要
        muted_already.member.bot = False
        muted_already.member.voice = SimpleNamespace(channel=voice_channel, mute=True, suppress=False)
        # joiner: 発言不可対象だがsuppressで既に発言不可
        joiner.member.bot = False
        joiner.member.voice = SimpleNamespace(channel=voice_channel, mute=False, suppress=True)

        with self.assertLogs("room_runner", level="INFO") as cm:
            result = await runner._sync_server_mutes({speaker.user_id})

        self.assertEqual(result, [])
        info_lines = [m for m in cm.output if "ミュート同期: 対象0件" in m]
        self.assertEqual(len(info_lines), 1)
        line = info_lines[0]
        self.assertIn("VC接続3人", line)
        self.assertIn("suppress=1", line)
        self.assertIn("既にmute=2", line)
        self.assertIn("bot=0", line)
        self.assertIn("gm=0", line)
        self.assertIn("VC不一致=0", line)

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
