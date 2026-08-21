"""完全固定依存lockの照合テスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import verify_dependency_lock as verifier


class DependencyLockTest(unittest.TestCase):
    def _lock_file(self, body: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        temp_dir = tempfile.TemporaryDirectory(prefix="dependency-lock-test-")
        path = Path(temp_dir.name) / "requirements-lock.txt"
        path.write_text(body, encoding="utf-8")
        return temp_dir, path

    def test_all_exact_pins_match_after_name_normalization(self) -> None:
        temp_dir, path = self._lock_file(
            "# complete lock\nDiscord.py==2.7.1\ncffi==2.1.1\n"
        )
        self.addCleanup(temp_dir.cleanup)

        locked = verifier.load_locked_versions(path)

        self.assertEqual(
            verifier.find_mismatches(
                locked,
                {"discord-py": "2.7.1", "cffi": "2.1.1", "unlisted": "1"},
            ),
            [],
        )

    def test_missing_and_wrong_versions_are_reported(self) -> None:
        temp_dir, path = self._lock_file("aiohttp==3.14.3\ncffi==2.1.1\n")
        self.addCleanup(temp_dir.cleanup)
        locked = verifier.load_locked_versions(path)

        mismatches = verifier.find_mismatches(locked, {"cffi": "2.1.0"})

        self.assertEqual(
            mismatches,
            [
                "aiohttp: 未導入 (lock=3.14.3)",
                "cffi: installed=2.1.0 / lock=2.1.1",
            ],
        )

    def test_direct_extras_pin_must_exist_at_same_version_in_lock(self) -> None:
        temp_dir, direct_path = self._lock_file(
            "discord.py[voice]==2.7.1\naiohttp==3.14.3\n"
        )
        self.addCleanup(temp_dir.cleanup)
        direct = verifier.load_direct_versions(direct_path)
        locked = {
            "discord-py": ("discord.py", "2.7.0"),
        }

        self.assertEqual(
            verifier.find_direct_lock_mismatches(direct, locked),
            [
                "aiohttp: requirements=3.14.3 / lock=未登録",
                "discord.py[voice]: requirements=2.7.1 / lock=2.7.0",
            ],
        )

    def test_non_exact_or_duplicate_pin_is_rejected(self) -> None:
        for body in (
            "aiohttp>=3.14\n",
            "aio-http==3.14.3\naio_http==3.14.3\n",
            "--extra-index-url https://example.invalid\n",
        ):
            with self.subTest(body=body):
                temp_dir, path = self._lock_file(body)
                try:
                    with self.assertRaises(verifier.LockFormatError):
                        verifier.load_locked_versions(path)
                finally:
                    temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
