#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
"$ROOT/.venv/bin/python" "$ROOT/scripts/check_runtime_freshness.py"
