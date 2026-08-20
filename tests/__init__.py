"""テストパッケージの初期化処理 (保険としての本番DB隔離ガード)。

主たる防御は tests/test_00_db_path_guard.py にある。あちらの docstring に
理由を書いた通り、CI/scripts/run_checks.sh が実際に使う
`python -m unittest discover -s tests` (トップレベルディレクトリ未指定) では
unittest の仕様上ここ (tests/__init__.py) は import されない。

それでもこのファイルを置いておくのは、`-t .` を付けて実行した場合や
pytest 等 tests を実パッケージとして import するツールで走らせた場合に
备えるため。実装は test_00_db_path_guard.py と同じ内容で、二重に
差し替えても実害はない (どちらも一時ディレクトリを向くだけ)。
"""
from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

import database

_guard_tmpdir = tempfile.TemporaryDirectory(prefix="werewolf-tests-guard-")
database.DB_PATH = str(Path(_guard_tmpdir.name) / "guard-default.db")

atexit.register(_guard_tmpdir.cleanup)
