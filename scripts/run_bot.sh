#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
BOT_DIR=${SCRIPT_DIR:h}
cd "$BOT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  print -u2 "Missing virtual environment. Run ./scripts/setup_venv.sh first."
  exit 1
fi

exec "$BOT_DIR/.venv/bin/python" "$BOT_DIR/bot.py" "$@"
