#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --requirement requirements.lock
.venv/bin/python -m pip install --no-deps -e .
echo "Locked environment installed for Python $($PYTHON_BIN --version 2>&1)."
