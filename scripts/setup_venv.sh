#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
BOT_DIR=${SCRIPT_DIR:h}
cd "$BOT_DIR"

APPLE_SILICON=false
if [[ "$(/usr/bin/uname -s)" == "Darwin" ]] && \
    /usr/bin/arch -arm64 /usr/bin/true >/dev/null 2>&1; then
  APPLE_SILICON=true
fi

if [[ -n ${PYTHON_BIN:-} ]]; then
  PYTHON=$PYTHON_BIN
elif $APPLE_SILICON && [[ -x /opt/homebrew/opt/python@3.14/bin/python3.14 ]]; then
  # PATHにIntel Homebrewが残っていても、Apple Siliconではネイティブ版を優先する。
  PYTHON=/opt/homebrew/opt/python@3.14/bin/python3.14
elif command -v python3.14 >/dev/null 2>&1; then
  PYTHON=$(command -v python3.14)
elif [[ -x /opt/homebrew/opt/python@3.14/bin/python3.14 ]]; then
  PYTHON=/opt/homebrew/opt/python@3.14/bin/python3.14
elif ! $APPLE_SILICON && [[ -x /usr/local/opt/python@3.14/bin/python3.14 ]]; then
  PYTHON=/usr/local/opt/python@3.14/bin/python3.14
else
  PYTHON=python3
fi

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else "Python 3.14 is required")'
"$PYTHON" "$BOT_DIR/scripts/verify_runtime.py"

# 稼働中のPythonが削除済みvenvを掴む状態を作らない。停止はオーナーが明示的に行う。
if "$PYTHON" "$BOT_DIR/scripts/start_bot_detached.py" --status >/dev/null 2>&1; then
  print -u2 "Botが稼働中のためvenvを変更しません。先に ./scripts/stop_bot.sh を実行してください。"
  exit 1
else
  STATUS_CODE=$?
  if [[ $STATUS_CODE -ne 1 ]]; then
    print -u2 "Botの稼働状態を安全に確認できないためvenvを変更しません。"
    exit 1
  fi
fi

# 別のPython系列・別アーキテクチャで作られたvenvは安全に流用できないため作り直す。
# バージョンだけを見ていると、Apple Silicon機でIntel版Python(Rosetta)の
# venvがそのまま残り、同梱のarm64 libopusが読めない状態に気付けない。
TARGET_ARCH=$("$PYTHON" -c 'import platform; print(platform.machine())')
if [[ -x .venv/bin/python ]]; then
  if ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)'; then
    print -r -- "既存のvenvがPython 3.14ではないため作り直します。"
    rm -rf "$BOT_DIR/.venv"
  elif [[ "$(.venv/bin/python -c 'import platform; print(platform.machine())')" != "$TARGET_ARCH" ]]; then
    print -r -- "既存のvenvのアーキテクチャが $TARGET_ARCH と異なるため作り直します。"
    rm -rf "$BOT_DIR/.venv"
  fi
fi

"$PYTHON" -m venv .venv
./.venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else "The virtual environment must use Python 3.14")'
./.venv/bin/python -c "import platform, sys; raise SystemExit(0 if platform.machine() == '$TARGET_ARCH' else f'The virtual environment must be {\"$TARGET_ARCH\"} (got {platform.machine()})')"
./.venv/bin/python "$BOT_DIR/scripts/verify_runtime.py"
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt -c requirements-lock.txt
./.venv/bin/python -m pip check
./.venv/bin/python -m discord --version
# SE_ENABLED=Trueなら、discord.pyがdavey/PyNaClを認識していることと
# DAVEネイティブ初期化・libopusロードまでを環境構築時に検証する。
./.venv/bin/python -c 'from config import SE_ENABLED; import sounds; sounds.require_voice_ready() if SE_ENABLED else None'
