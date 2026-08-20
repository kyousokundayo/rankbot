#!/bin/zsh
# コミット前の静的確認 + ユニットテスト
set -euo pipefail

cd "$(dirname "$0")/.."

PY=.venv/bin/python
[[ -x $PY ]] || PY=python3

echo "== Python runtime =="
"$PY" -c 'import sys; print(sys.version); raise SystemExit(0 if sys.version_info[:2] == (3, 14) else "Python 3.14 is required")'
"$PY" scripts/verify_runtime.py

echo "== py_compile =="
find . -type f -name '*.py' -not -path './.venv/*' -print0 | xargs -0 "$PY" -m py_compile
echo "OK"

echo "== dependency consistency =="
"$PY" -m pip check

echo "== unit tests =="
# -t . が無いと tests/ 自身が top-level 扱いになり tests/__init__.py の
# 本番DB隔離ガードが読み込まれない (unittest discover の仕様)。外さないこと。
"$PY" -m unittest discover -s tests -t .

echo "== all checks passed =="
