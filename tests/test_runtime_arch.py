"""Apple Silicon実行環境ガードの回帰テスト。"""
from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_runtime.py"
SPEC = importlib.util.spec_from_file_location("verify_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class TestRuntimeArchitecture(unittest.TestCase):
    def test_apple_silicon_rejects_rosetta_python(self):
        probe = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.object(runtime, "_sysctl_int", return_value=None),
            mock.patch.object(runtime.subprocess, "run", return_value=probe),
            mock.patch.object(runtime.platform, "machine", return_value="x86_64"),
        ):
            with self.assertRaisesRegex(RuntimeError, "arm64 Python"):
                runtime.verify_runtime()

    def test_apple_silicon_accepts_arm64_python(self):
        probe = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.object(runtime.subprocess, "run", return_value=probe),
            mock.patch.object(runtime.platform, "machine", return_value="arm64"),
            mock.patch("builtins.print"),
        ):
            runtime.verify_runtime()

    def test_intel_mac_does_not_require_arm64(self):
        probe = subprocess.CompletedProcess([], 1)
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.object(runtime, "_sysctl_int", return_value=None),
            mock.patch.object(runtime.subprocess, "run", return_value=probe),
            mock.patch.object(runtime.platform, "machine", return_value="x86_64"),
            mock.patch("builtins.print"),
        ):
            runtime.verify_runtime()

    def test_rosetta_sysctl_rejects_x86_when_arch_probe_is_unavailable(self):
        with (
            mock.patch.object(runtime.sys, "platform", "darwin"),
            mock.patch.object(runtime, "_sysctl_int", return_value=1),
            mock.patch.object(runtime.platform, "machine", return_value="x86_64"),
            mock.patch.object(runtime.subprocess, "run") as probe,
        ):
            with self.assertRaisesRegex(RuntimeError, "arm64 Python"):
                runtime.verify_runtime()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
