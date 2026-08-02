"""起動・停止ランチャーが無関係なプロセスを扱わないことを検証する。"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "start_bot_detached.py"
SPEC = importlib.util.spec_from_file_location("start_bot_detached", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class TestPidValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-launcher-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_read_pid_rejects_invalid_and_symlink_markers(self):
        marker = self.root / "bot.pid"
        for invalid in ("", "abc", "-1", "0", "1", "12 13"):
            with self.subTest(invalid=invalid):
                marker.write_text(invalid, encoding="utf-8")
                self.assertIsNone(launcher._read_pid(marker))

        target = self.root / "target"
        target.write_text("123", encoding="utf-8")
        marker.unlink()
        marker.symlink_to(target)
        self.assertIsNone(launcher._read_pid(marker))

    def test_process_match_requires_python_and_exact_bot_argument(self):
        def completed(command: str):
            return subprocess.CompletedProcess([], 0, stdout=command, stderr="")

        with mock.patch.object(
            launcher.subprocess,
            "run",
            return_value=completed(f"/usr/bin/python3 {launcher.BOT_FILE}"),
        ):
            self.assertTrue(launcher._process_matches_bot(123))

        for unrelated in (
            "/usr/bin/python3 /tmp/other.py",
            f"/usr/bin/vim {launcher.BOT_FILE}",
            f"/usr/bin/python3 {launcher.BOT_FILE}.backup",
            f"/usr/bin/python3 -c 'print(1)' {launcher.BOT_FILE}",
        ):
            with self.subTest(unrelated=unrelated), mock.patch.object(
                launcher.subprocess, "run", return_value=completed(unrelated)
            ):
                self.assertFalse(launcher._process_matches_bot(123))

    def test_real_python_process_matches_absolute_and_relative_script(self):
        try:
            probe = subprocess.run(
                ["/bin/ps", "-p", str(os.getpid()), "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            self.skipTest("この実行環境ではプロセス情報の参照が許可されていません")
        if probe.returncode != 0:
            self.skipTest("この実行環境ではプロセス情報の参照が許可されていません")
        script = self.root / "worker.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        original_bot_file = launcher.BOT_FILE
        launcher.BOT_FILE = script.resolve()
        try:
            for argument in (str(script), script.name):
                with self.subTest(argument=argument):
                    process = subprocess.Popen(
                        [sys.executable, argument],
                        cwd=str(self.root),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            if launcher._process_matches_bot(process.pid):
                                break
                            time.sleep(0.05)
                        self.assertTrue(launcher._process_matches_bot(process.pid))
                    finally:
                        process.terminate()
                        process.wait(timeout=2)
        finally:
            launcher.BOT_FILE = original_bot_file

    def test_real_python_process_matches_checkout_path_with_spaces(self):
        spaced_root = self.root / "bot path with spaces"
        spaced_root.mkdir()
        script = spaced_root / "bot.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        original_bot_dir = launcher.BOT_DIR
        original_bot_file = launcher.BOT_FILE
        launcher.BOT_DIR = spaced_root.resolve()
        launcher.BOT_FILE = script.resolve()
        process = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(spaced_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    matched = launcher._process_matches_bot(process.pid)
                except launcher.ProcessInspectionError:
                    self.skipTest("この実行環境ではプロセス情報の参照が許可されていません")
                if matched:
                    break
                time.sleep(0.05)
            try:
                raw = subprocess.run(
                    ["/bin/ps", "-ww", "-p", str(process.pid), "-o", "command="],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertTrue(
                    launcher._process_matches_bot(process.pid),
                    f"ps command={raw!r}; cwd={launcher._process_cwd(process.pid)!r}",
                )
            except launcher.ProcessInspectionError:
                self.skipTest("この実行環境ではプロセス情報の参照が許可されていません")
        finally:
            process.terminate()
            process.wait(timeout=2)
            launcher.BOT_DIR = original_bot_dir
            launcher.BOT_FILE = original_bot_file

    def test_runtime_directory_rejects_symlink(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "runtime"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "シンボリックリンク"):
            launcher._prepare_runtime_dir(link)

    def test_launcher_operation_lock_serializes_concurrent_operations(self):
        runtime = self.root / "runtime"
        launcher._prepare_runtime_dir(runtime)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first():
            with launcher._launcher_operation_lock(runtime):
                first_entered.set()
                release_first.wait(timeout=2)

        def second():
            first_entered.wait(timeout=2)
            with launcher._launcher_operation_lock(runtime):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        second_thread.start()
        self.assertTrue(first_entered.wait(timeout=1))
        self.assertFalse(second_entered.wait(timeout=0.1))
        release_first.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertTrue(second_entered.is_set())


class TestSafeStop(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-stop-test-")
        self.runtime = Path(self.temp_dir.name) / "runtime"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_stale_marker_never_sends_signal(self):
        self.runtime.mkdir()
        (self.runtime / "bot.pid").write_text(str(os.getpid()), encoding="utf-8")
        with (
            mock.patch.object(launcher, "_runtime_dir", return_value=self.runtime),
            mock.patch.object(launcher, "_candidate_bot_pids", return_value=[]),
            mock.patch.object(launcher.os, "kill") as kill,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(launcher.stop(), 0)
        kill.assert_not_called()
        self.assertFalse((self.runtime / "bot.pid").exists())

    def test_validated_bot_pid_receives_term_only(self):
        checks = iter((True, False))
        with (
            mock.patch.object(launcher, "_runtime_dir", return_value=self.runtime),
            mock.patch.object(launcher, "_candidate_bot_pids", return_value=[4321]),
            mock.patch.object(
                launcher, "_process_matches_bot", side_effect=lambda _pid: next(checks)
            ),
            mock.patch.object(launcher.os, "kill") as kill,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(launcher.stop(), 0)
        kill.assert_called_once_with(4321, signal.SIGTERM)


class TestStatus(unittest.TestCase):
    def test_status_reports_validated_running_process(self):
        with (
            mock.patch.object(launcher, "_runtime_dir", return_value=Path("/tmp/test")),
            mock.patch.object(launcher, "_candidate_bot_pids", return_value=[4321]),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(launcher.status(), 0)
        output.assert_called_once_with("running")

    def test_status_reports_no_validated_process(self):
        with (
            mock.patch.object(launcher, "_runtime_dir", return_value=Path("/tmp/test")),
            mock.patch.object(launcher, "_candidate_bot_pids", return_value=[]),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(launcher.status(), 1)
        output.assert_called_once_with("not_running")

    def test_status_reports_unknown_when_process_inspection_fails(self):
        with (
            mock.patch.object(launcher, "_runtime_dir", return_value=Path("/tmp/test")),
            mock.patch.object(
                launcher,
                "_candidate_bot_pids",
                side_effect=launcher.ProcessInspectionError("pgrep failed"),
            ),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(launcher.status(), 2)
        output.assert_called_once_with("unknown")


if __name__ == "__main__":
    unittest.main()
