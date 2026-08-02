"""SEの依存確認とVC後始末の回帰テスト。"""
from __future__ import annotations

import asyncio
import builtins
import unittest
from types import SimpleNamespace
from unittest import mock

import sounds


class FakeVoiceClient:
    def __init__(self, channel, *, playback_error=None, hang_graceful=False):
        self.channel = channel
        self.playback_error = playback_error
        self.hang_graceful = hang_graceful
        self.connected = True
        self.disconnect_calls: list[bool] = []

    def play(self, _source, *, after):
        after(self.playback_error)

    def is_connected(self):
        return self.connected

    async def disconnect(self, *, force):
        self.disconnect_calls.append(force)
        if self.hang_graceful and not force:
            await asyncio.Event().wait()
        self.connected = False


class FakeVoiceChannel:
    def __init__(self, *, playback_error=None, hang_graceful=False):
        self.id = 10
        self.name = "テストVC"
        self.guild = SimpleNamespace(id=20, voice_client=None)
        self.client = FakeVoiceClient(
            self,
            playback_error=playback_error,
            hang_graceful=hang_graceful,
        )

    async def connect(self):
        return self.client


class TestVoiceReadiness(unittest.TestCase):
    def test_bundled_opus_is_tried_before_default_discovery(self):
        bundled = sounds._OPUS_PATHS[0]
        with (
            mock.patch.object(sounds.discord.opus, "is_loaded", return_value=False),
            mock.patch.object(sounds.Path, "exists", return_value=True),
            mock.patch.object(sounds.discord.opus, "load_opus") as load_opus,
            mock.patch.object(sounds.discord.opus, "_load_default") as load_default,
        ):
            self.assertIsNone(sounds._ensure_opus_loaded())

        load_opus.assert_called_once_with(bundled)
        load_default.assert_not_called()

    def test_default_discovery_remains_fallback_without_explicit_library(self):
        with (
            mock.patch.object(
                sounds.discord.opus, "is_loaded", side_effect=[False, True]
            ),
            mock.patch.object(sounds.Path, "exists", return_value=False),
            mock.patch.object(sounds.discord.opus, "_load_default") as load_default,
        ):
            self.assertIsNone(sounds._ensure_opus_loaded())

        load_default.assert_called_once_with()

    def test_missing_davey_disables_sound(self):
        original_import = builtins.__import__

        def import_without_davey(name, *args, **kwargs):
            if name == "davey" or name.startswith("davey."):
                raise ImportError("simulated missing davey")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=import_without_davey):
            self.assertFalse(sounds._ensure_voice_ready())

    def test_discord_voice_client_must_also_recognize_davey(self):
        with mock.patch("discord.voice_client.has_dave", False):
            error = sounds._voice_dependency_error()

        self.assertIsNotNone(error)
        self.assertIn("discord.pyがdaveyを認識", error)

    def test_real_dave_and_all_scene_opus_encoding(self):
        sounds.require_voice_ready()
        encoder = sounds.discord.opus.Encoder()

        for scene in sounds._SCENES:
            with self.subTest(scene=scene):
                pcm = sounds.SoundPlayer()._get_pcm(scene)
                packet = encoder.encode(
                    pcm[:sounds.discord.opus.Encoder.FRAME_SIZE],
                    sounds.discord.opus.Encoder.SAMPLES_PER_FRAME,
                )
                self.assertTrue(packet)


class TestSoundPlayerCleanup(unittest.IsolatedAsyncioTestCase):
    async def _play(self, channel):
        player = sounds.SoundPlayer()
        player._available = True
        with mock.patch.object(sounds.discord, "PCMAudio", return_value=object()):
            return await player.play(channel, "morning")

    async def test_successful_playback_returns_true_and_disconnects(self):
        channel = FakeVoiceChannel()

        played = await self._play(channel)

        self.assertTrue(played)
        self.assertEqual(channel.client.disconnect_calls, [False])

    async def test_playback_callback_error_is_logged_and_disconnected(self):
        channel = FakeVoiceChannel(playback_error=RuntimeError("encoder failed"))
        with self.assertLogs("sounds", level="WARNING") as captured:
            await self._play(channel)

        self.assertIn("SE再生エラー", "\n".join(captured.output))
        self.assertEqual(channel.client.disconnect_calls, [False])
        self.assertFalse(channel.client.connected)

    async def test_disconnect_timeout_falls_back_to_force(self):
        channel = FakeVoiceChannel(hang_graceful=True)
        with (
            mock.patch.object(sounds, "VOICE_OPERATION_TIMEOUT", 0.01),
            mock.patch.object(sounds, "VOICE_FORCE_DISCONNECT_TIMEOUT", 0.01),
            self.assertLogs("sounds", level="WARNING") as captured,
        ):
            await self._play(channel)

        self.assertIn("SE用VC切断失敗", "\n".join(captured.output))
        self.assertEqual(channel.client.disconnect_calls, [False, True])
        self.assertFalse(channel.client.connected)

    async def test_stale_queued_sound_is_dropped(self):
        channel = FakeVoiceChannel()
        player = sounds.SoundPlayer()
        player._available = True
        lock = player._locks.setdefault(channel.guild.id, asyncio.Lock())
        await lock.acquire()
        try:
            with (
                mock.patch.object(sounds, "VOICE_QUEUE_MAX_WAIT", 0.01),
                mock.patch.object(channel, "connect", wraps=channel.connect) as connect,
            ):
                await player.play(channel, "morning")
        finally:
            lock.release()

        connect.assert_not_awaited()
        self.assertEqual(channel.client.disconnect_calls, [])


if __name__ == "__main__":
    unittest.main()
