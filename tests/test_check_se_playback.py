"""実Discord SE確認の明示確認とBot停止ロックを検証する。"""
from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from scripts import check_se_playback as playback


class CheckSePlaybackSafetyTest(unittest.IsolatedAsyncioTestCase):
    def _argv(self) -> list[str]:
        return [
            "check_se_playback.py",
            "--confirm-live-playback",
            playback._LIVE_PLAYBACK_CONFIRMATION,
        ]

    async def test_explicit_live_confirmation_is_required(self) -> None:
        with (
            patch.object(sys, "argv", ["check_se_playback.py"]),
            patch.object(playback, "_bot_stopped_guard") as guard,
        ):
            result = await playback._main()

        self.assertEqual(result, 2)
        guard.assert_not_called()

    async def test_busy_bot_stops_before_client_creation(self) -> None:
        with (
            patch.object(sys, "argv", self._argv()),
            patch.object(
                playback,
                "_bot_stopped_guard",
                side_effect=RuntimeError("人狼Botが稼働中です"),
            ),
            patch.object(playback, "PlaybackClient") as client_class,
        ):
            result = await playback._main()

        self.assertEqual(result, 2)
        client_class.assert_not_called()

    async def test_lock_is_held_until_discord_client_stops(self) -> None:
        state = {"locked": False}

        @contextmanager
        def guarded():
            state["locked"] = True
            try:
                yield
            finally:
                state["locked"] = False

        class DummyClient:
            exit_code = 0

            async def start(self, _token: str, *, reconnect: bool) -> None:
                if not state["locked"] or reconnect:
                    raise AssertionError("client must run while the guard is held")

        with (
            patch.object(sys, "argv", self._argv()),
            patch.object(playback, "_bot_stopped_guard", guarded),
            patch.object(playback, "load_dotenv"),
            patch.object(
                playback.os,
                "getenv",
                side_effect=lambda key: "token" if key == "DISCORD_TOKEN" else "",
            ),
            patch.object(playback.sounds, "require_voice_ready"),
            patch.object(playback, "PlaybackClient", return_value=DummyClient()),
        ):
            result = await playback._main()

        self.assertEqual(result, 0)
        self.assertFalse(state["locked"])


if __name__ == "__main__":
    unittest.main()
