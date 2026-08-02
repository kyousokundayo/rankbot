#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
BOT_DIR=${SCRIPT_DIR:h}
PYTHON_FILE="$BOT_DIR/.venv/bin/python"
CONTROL_FILE="$SCRIPT_DIR/start_bot_detached.py"

if [[ ! -x "$PYTHON_FILE" ]]; then
  print -u2 "Missing virtual environment. Run ./scripts/setup_venv.sh first."
  exit 1
fi

exec "$PYTHON_FILE" "$CONTROL_FILE" --stop
