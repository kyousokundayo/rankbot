"""戦績カード一括出力先の安全性テスト。"""
from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts import generate_season_cards as exporter


class SeasonCardOutputSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="season-card-output-test-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_new_directory_and_png_are_private(self) -> None:
        output_dir = exporter._prepare_output_directory(self.root / "cards")
        output_file = output_dir / "1_player.png"
        exporter._write_private_png(output_file, b"png")

        self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(output_file.stat().st_mode), 0o600)
        self.assertEqual(output_file.read_bytes(), b"png")

    def test_existing_empty_directory_is_hardened(self) -> None:
        output_dir = self.root / "cards"
        output_dir.mkdir(mode=0o755)

        prepared = exporter._prepare_output_directory(output_dir)

        self.assertEqual(prepared, output_dir.absolute())
        self.assertEqual(stat.S_IMODE(prepared.stat().st_mode), 0o700)

    def test_nonempty_or_symlink_directory_is_rejected(self) -> None:
        output_dir = self.root / "cards"
        output_dir.mkdir()
        (output_dir / "old.png").write_bytes(b"old")
        with self.assertRaisesRegex(RuntimeError, "空ではありません"):
            exporter._prepare_output_directory(output_dir)

        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "シンボリックリンク"):
            exporter._prepare_output_directory(link)

    def test_existing_png_is_never_overwritten(self) -> None:
        output_file = self.root / "card.png"
        output_file.write_bytes(b"old")

        with self.assertRaisesRegex(RuntimeError, "既に存在"):
            exporter._write_private_png(output_file, b"new")

        self.assertEqual(output_file.read_bytes(), b"old")


class SeasonCardRuntimeGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_busy_bot_stops_before_client_creation(self) -> None:
        with (
            patch.object(sys, "argv", ["generate_season_cards.py"]),
            patch.object(
                exporter,
                "_bot_stopped_guard",
                side_effect=RuntimeError("人狼Botが稼働中です"),
            ),
            patch.object(exporter, "SeasonCardExportClient") as client_class,
        ):
            result = await exporter._main()

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

        client = DummyClient()
        with (
            patch.object(sys, "argv", ["generate_season_cards.py"]),
            patch.object(exporter, "_bot_stopped_guard", guarded),
            patch.object(exporter, "load_dotenv"),
            patch.object(exporter.os, "getenv", return_value="token"),
            patch.object(exporter, "SeasonCardExportClient", return_value=client),
        ):
            result = await exporter._main()

        self.assertEqual(result, 0)
        self.assertFalse(state["locked"])


if __name__ == "__main__":
    unittest.main()
