"""プレシーズン削除スクリプトの稼働中Bot保護。"""
from __future__ import annotations

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import purge_preseason_stats


class PurgePreseasonStatsLockTest(unittest.TestCase):
    def test_execute_guard_rejects_busy_bot_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="purge-lock-test-") as temp_dir:
            lock_path = Path(temp_dir) / "bot.lock"
            with (
                patch.dict(os.environ, {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)}),
                patch.object(
                    purge_preseason_stats.fcntl,
                    "flock",
                    side_effect=BlockingIOError,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "人狼Botが稼働中"):
                    with purge_preseason_stats._bot_stopped_guard():
                        self.fail("busy lock must not enter")

    def test_execute_guard_holds_and_releases_bot_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="purge-lock-test-") as temp_dir:
            lock_path = Path(temp_dir) / "bot.lock"
            calls: list[int] = []

            def record_lock(_fd: int, operation: int) -> None:
                calls.append(operation)

            with (
                patch.dict(os.environ, {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)}),
                patch.object(purge_preseason_stats.fcntl, "flock", record_lock),
            ):
                with purge_preseason_stats._bot_stopped_guard():
                    self.assertTrue(lock_path.exists())

            self.assertEqual(calls, [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN])


if __name__ == "__main__":
    unittest.main()
