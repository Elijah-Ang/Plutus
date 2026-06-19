#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"

RUNTIME="$ROOT/logs/runtime"
ERRORS="$ROOT/logs/errors"
mkdir -p "$RUNTIME" "$ERRORS"

LOCKDIR="$RUNTIME/agent.lockdir"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # Overlapping run detected, exit silently
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT INT TERM

export TRADING_AGENT_LOCK_HELD=1
"$ROOT/.venv/bin/python" -m app.main

