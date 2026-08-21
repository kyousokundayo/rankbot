"""プレシーズン削除スクリプトの誤実行・稼働中Bot・バックアップ保護。"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, nullcontext
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
from scripts import bot_runtime_guard
from scripts import purge_preseason_stats
from scripts import purge_preseason_stats as purge


class PurgePreseasonStatsLockTest(unittest.TestCase):
    def test_execute_guard_rejects_busy_bot_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="purge-lock-test-") as temp_dir:
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
                patch.object(bot_runtime_guard.fcntl, "flock", record_lock),
            ):
                with purge_preseason_stats._bot_stopped_guard():
                    self.assertTrue(lock_path.exists())

            self.assertEqual(calls, [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN])


class PurgePreseasonStatsConfirmationTest(unittest.IsolatedAsyncioTestCase):
    async def test_execute_requires_explicit_season1_confirmation(self) -> None:
        run = AsyncMock(return_value=0)
        with (
            patch.object(sys, "argv", ["purge_preseason_stats.py", "--execute"]),
            patch.object(purge_preseason_stats, "_run", run),
        ):
            result = await purge_preseason_stats.main()

        self.assertEqual(result, 2)
        run.assert_not_awaited()

    async def test_exact_confirmation_allows_guarded_execution(self) -> None:
        run = AsyncMock(return_value=0)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "purge_preseason_stats.py",
                    "--execute",
                    "--confirm-season1",
                    "ERASE-PRESEASON",
                ],
            ),
            patch.object(purge_preseason_stats, "_run", run),
            patch.object(
                purge_preseason_stats,
                "_bot_stopped_guard",
                return_value=nullcontext(),
            ),
        ):
            result = await purge_preseason_stats.main()

        self.assertEqual(result, 0)
        run.assert_awaited_once()


class PurgePreseasonStatsBackupTest(unittest.IsolatedAsyncioTestCase):
    async def test_backup_must_be_readable_and_match_target_counts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="purge-backup-test-") as temp_dir:
            backup_path = Path(temp_dir) / "backup.db"
            with closing(sqlite3.connect(backup_path)) as db:
                db.execute("CREATE TABLE games (game_id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO games (game_id) VALUES (1)")
                db.commit()

            await purge_preseason_stats._verify_backup(
                str(backup_path),
                ("games",),
                {"games": 1},
            )

            with self.assertRaisesRegex(RuntimeError, "対象件数"):
                await purge_preseason_stats._verify_backup(
                    str(backup_path),
                    ("games",),
                    {"games": 2},
                )

    async def test_backup_foreign_key_violation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="purge-backup-fk-test-") as temp_dir:
            backup_path = Path(temp_dir) / "backup.db"
            with closing(sqlite3.connect(backup_path)) as db:
                db.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
                db.execute(
                    "CREATE TABLE games ("
                    "game_id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER REFERENCES parents(id))"
                )
                db.execute("INSERT INTO games (game_id, parent_id) VALUES (1, 999)")
                db.commit()

            with self.assertRaisesRegex(RuntimeError, "foreign_key_check"):
                await purge_preseason_stats._verify_backup(
                    str(backup_path),
                    ("games",),
                    {"games": 1},
                )


if __name__ == "__main__":
    unittest.main()


class PurgePreseasonStatsPendingSettlementTest(unittest.IsolatedAsyncioTestCase):
    """未精算の試合が残っている間は消させない。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-purge-pending-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "pending.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    async def test_pending_settlement_blocks_execution(self) -> None:
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO game_settlements "
                "(guild_id, room_id, game_run_id, variant_id, ladder_id, room_name, "
                " rated, winner_team, player_records, status) "
                "VALUES (1, 'private_1', 'run-1', 'v13_cross', 'l13', 'GM村', "
                " 1, '村陣営', '[]', 'pending')"
            )
            await db.commit()

        args = SimpleNamespace(execute=False, reset_ratings=False)
        code = await purge._run(args)

        # 未精算が残る間は dry-run でも中止する (誤って --execute を足す前に気づける)
        self.assertEqual(code, 1)
