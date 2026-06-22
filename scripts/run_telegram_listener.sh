#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"

RUNTIME="$ROOT/logs/runtime"
ERRORS="$ROOT/logs/errors"
mkdir -p "$RUNTIME" "$ERRORS"

LOCKDIR="$RUNTIME/listener.lockdir"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # Already running
  exit 0
fi
cleanup_lock() {
  rmdir "$LOCKDIR" 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM

export TRADING_AGENT_LOCK_HELD=1
"$ROOT/.venv/bin/python" -m app.main --mode listener >> "$RUNTIME/listener.out" 2>> "$ERRORS/listener.err"
