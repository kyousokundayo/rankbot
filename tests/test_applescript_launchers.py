"""Finder用AppleScriptの配置選択契約を静的に検査する。"""
from __future__ import annotations

import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent.parent


class AppleScriptPathSelectionTest(unittest.TestCase):
    def test_explicit_invalid_directory_never_falls_back(self) -> None:
        for filename in ("start_main.applescript", "stop_main.applescript"):
            with self.subTest(filename=filename):
                source = (BOT_DIR / "scripts" / filename).read_text(encoding="utf-8")
                self.assertIn('if configuredDir is not "" then', source)
                self.assertIn("if containsBot(configuredDir) then", source)
                self.assertIn("WEREWOLF_BOT_DIR にBotが見つかりません", source)
                self.assertNotIn(
                    'if configuredDir is not "" and containsBot(configuredDir) then',
                    source,
                )


if __name__ == "__main__":
    unittest.main()
