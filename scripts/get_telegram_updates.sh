#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" scripts/telegram_get_updates.py "$@"
