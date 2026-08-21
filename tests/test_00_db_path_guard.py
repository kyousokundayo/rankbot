"""本番DB・本番ログ隔離ガード。

なぜここに置くか:
  tests/__init__.py に同種のガードを置いても、CIやscripts/run_checks.shが
  実行する `python -m unittest discover -s tests` (トップレベルディレクトリ
  未指定) では、unittest の仕様上 start_dir==top_level_dir の場合その
  ディレクトリ自身は「パッケージ」として扱われず __init__.py は一切
  importされない (unittest.loader の実装コメントにも明記されている:
  「name is '.' when start_dir == top_level_dir」)。つまり tests/__init__.py
  だけに頼ると、CIの実際の起動コマンドではガードが発火しない。

  一方 discover は各 test*.py を `sorted(os.listdir(...))` の順で読み込む
  ため、ファイル名を早いソート順にしておけば、他のどのテストファイルよりも
  先に必ず import される。ファイル名の "00" はそのための細工であり、
  他のテストとの依存関係を作らないためモジュールレベルで即座に
  database.DB_PATH を書き換える。

  tests/__init__.py 側のガードは `-t .` 付き実行 (pytest 相当の挙動) や
  将来 `tests` をパッケージとして import するケースのための保険として
  残してあるが、本命の防御はこちらのファイル。
"""
from __future__ import annotations

import atexit
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path

import database

# --- モジュールレベルで即座に本番DBから隔離する ------------------------
# 個々のテストが database.DB_PATH の差し替えを忘れても、このファイルが
# 最初に import された時点で安全なパスへ切り替わっているため、
# 本番DB (data/werewolf_stats.db) には絶対に書き込まれない。
_guard_tmpdir = tempfile.TemporaryDirectory(prefix="werewolf-tests-guard-")
database.DB_PATH = str(Path(_guard_tmpdir.name) / "guard-default.db")
atexit.register(_guard_tmpdir.cleanup)
# ------------------------------------------------------------------------

# --- モジュールレベルで即座に本番ログからも隔離する ----------------------
# bot.py はモジュール読み込み時点 (import 時点) で
# logs/bot.log へ RotatingFileHandler を張ってしまう。tests/test_startup_
# resilience.py が bot を import するため、何もしなければテスト実行が
# 稼働中Botと同じログファイルを掴み、ローテーションで本番ログを潰して
# しまう。WEREWOLF_LOG_DIR を先回りしてテスト専用の一時ディレクトリへ
# 向けておくことで、bot.py 側 (import時) がこの一時ディレクトリを使う
# ようにする。ファイル名を "00" にして最初に import させているのは
# database.DB_PATH の隔離と同じ理由 (モジュールdocstring参照)。
_log_guard_tmpdir = tempfile.TemporaryDirectory(prefix="werewolf-tests-log-guard-")
os.environ["WEREWOLF_LOG_DIR"] = _log_guard_tmpdir.name
atexit.register(_log_guard_tmpdir.cleanup)


def _detach_handlers_pointing_at_production_log() -> None:
    """念のための保険: 本番 logs/bot.log を指すハンドラが既に張られていたら外す。

    通常は WEREWOLF_LOG_DIR の先回り設定だけで十分だが、何らかの事情で
    bot モジュールが本ファイルより先に import されていた場合に備え、
    ルートロガーから本番ログファイルを指すハンドラを検出して除去する。
    挙動 (テストの成否) には影響しない防御的処理。
    """
    production_log = str((Path(__file__).resolve().parent.parent / "logs" / "bot.log").resolve())
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        base_filename = getattr(handler, "baseFilename", None)
        if base_filename and str(Path(base_filename).resolve()) == production_log:
            root_logger.removeHandler(handler)
            handler.close()


_detach_handlers_pointing_at_production_log()
# ------------------------------------------------------------------------


class DbPathGuardTest(unittest.TestCase):
    """このガードが働いていることの回帰テスト。"""

    def test_default_db_path_is_not_the_production_db(self) -> None:
        production_path = Path(database.DATA_DIR) / "werewolf_stats.db"
        self.assertNotEqual(
            Path(database.DB_PATH).resolve(),
            production_path.resolve(),
            "database.DB_PATH が本番DBを指しています。"
            "本ファイル (tests/test_00_db_path_guard.py) が最初に"
            "importされていない可能性があります。",
        )

    def test_default_db_path_lives_under_a_temporary_directory(self) -> None:
        self.assertIn("werewolf-tests-guard-", database.DB_PATH)


class LogPathGuardTest(unittest.TestCase):
    """このログガードが働いていることの回帰テスト。"""

    def test_werewolf_log_dir_env_points_to_a_temporary_directory(self) -> None:
        self.assertIn("werewolf-tests-log-guard-", os.environ.get("WEREWOLF_LOG_DIR", ""))

    def test_bot_module_uses_the_guarded_log_dir_if_imported(self) -> None:
        # bot をここで import しても (tests/test_startup_resilience.py が
        # 既に import 済みでも、本ファイル単体実行で未import でも)、
        # production の logs/bot.log ではなくガードされた一時ディレクトリを
        # 掴んでいるはずであること。DISCORD_TOKEN 未設定でも import が
        # 失敗しないよう、test_startup_resilience.py と同様に無害な値を
        # 一時的に渡す (既に import 済みならこの値は使われない)。
        from unittest.mock import patch

        with patch.dict(os.environ, {"DISCORD_TOKEN": "unit-test-token"}):
            import bot as bot_module

        production_log_dir = Path(bot_module.BASE_DIR) / "logs"
        self.assertNotEqual(
            Path(bot_module.LOG_DIR).resolve(),
            production_log_dir.resolve(),
            "bot.LOG_DIR が本番ログディレクトリを指しています。"
            "WEREWOLF_LOG_DIR による退避が効いていない可能性があります。",
        )
        self.assertEqual(stat.S_IMODE(Path(bot_module.LOG_DIR).stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((Path(bot_module.LOG_DIR) / "bot.log").stat().st_mode),
            0o600,
        )

    def test_existing_rotated_logs_are_hardened_and_symlinks_rejected(self) -> None:
        from unittest.mock import patch

        with patch.dict(os.environ, {"DISCORD_TOKEN": "unit-test-token"}):
            import bot as bot_module

        rotated_logs = [
            Path(bot_module.LOG_DIR) / f"bot.log.{index}" for index in range(1, 4)
        ]
        for path in rotated_logs:
            path.write_bytes(b"old")
            path.chmod(0o644)

        bot_module._harden_existing_bot_logs()

        for path in rotated_logs:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        target = Path(bot_module.LOG_DIR) / "target.log"
        target.write_bytes(b"target")
        rotated_logs[0].unlink()
        rotated_logs[0].symlink_to(target)
        try:
            with self.assertRaisesRegex(RuntimeError, "安全な既存ログ"):
                bot_module._harden_existing_bot_logs()
        finally:
            rotated_logs[0].unlink()


if __name__ == "__main__":
    unittest.main()
