"""本番DB隔離ガード。

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


if __name__ == "__main__":
    unittest.main()
