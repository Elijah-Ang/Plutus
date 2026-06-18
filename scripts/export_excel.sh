#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m app.reports
