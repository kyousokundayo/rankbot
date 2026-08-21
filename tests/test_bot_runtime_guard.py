"""実Discord・本番DB用補助処理のBot停止ロックを検証する。"""
from __future__ import annotations

import fcntl
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import bot_runtime_guard


class BotStoppedGuardTest(unittest.TestCase):
    def test_guard_holds_and_releases_private_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bot-runtime-guard-") as temp_dir:
            lock_path = Path(temp_dir) / "bot.lock"
            calls: list[int] = []

            def record_lock(_file_descriptor: int, operation: int) -> None:
                calls.append(operation)

            with (
                patch.dict(os.environ, {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)}),
                patch.object(bot_runtime_guard.fcntl, "flock", record_lock),
            ):
                with bot_runtime_guard.bot_stopped_guard():
                    self.assertTrue(lock_path.is_file())
                    self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

            self.assertEqual(calls, [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN])

    def test_busy_bot_lock_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bot-runtime-guard-") as temp_dir:
            lock_path = Path(temp_dir) / "bot.lock"
            with (
                patch.dict(os.environ, {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)}),
                patch.object(
                    bot_runtime_guard.fcntl,
                    "flock",
                    side_effect=BlockingIOError,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "人狼Botが稼働中"):
                    with bot_runtime_guard.bot_stopped_guard():
                        self.fail("busy lock must not enter")

    def test_symlink_lock_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bot-runtime-guard-") as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.write_text("keep", encoding="utf-8")
            lock_path = root / "bot.lock"
            lock_path.symlink_to(target)

            with patch.dict(
                os.environ,
                {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)},
            ):
                with self.assertRaisesRegex(RuntimeError, "安全なBotロック"):
                    with bot_runtime_guard.bot_stopped_guard():
                        self.fail("symlink lock must not enter")

            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_group_writable_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bot-runtime-guard-") as temp_dir:
            parent = Path(temp_dir) / "shared"
            parent.mkdir(mode=0o770)
            parent.chmod(0o770)
            lock_path = parent / "bot.lock"

            with patch.dict(
                os.environ,
                {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)},
            ):
                with self.assertRaisesRegex(RuntimeError, "ロック用ディレクトリ"):
                    with bot_runtime_guard.bot_stopped_guard():
                        self.fail("writable parent must not enter")

            self.assertFalse(lock_path.exists())

    def test_lock_os_error_is_reported_as_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bot-runtime-guard-") as temp_dir:
            lock_path = Path(temp_dir) / "bot.lock"
            with (
                patch.dict(os.environ, {"WEREWOLF_BOT_LOCK_FILE": str(lock_path)}),
                patch.object(
                    bot_runtime_guard.fcntl,
                    "flock",
                    side_effect=OSError("lock unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "停止状態"):
                    with bot_runtime_guard.bot_stopped_guard():
                        self.fail("lock error must not enter")


if __name__ == "__main__":
    unittest.main()
