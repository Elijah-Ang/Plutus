#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"

RUNTIME="$ROOT/logs/runtime"
ERRORS="$ROOT/logs/errors"
mkdir -p "$RUNTIME" "$ERRORS"

LOCKDIR="$RUNTIME/agent.lockdir"
RECOVERED=0
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  set +e
  LOCK_STATE=$("$ROOT/.venv/bin/python" -m app.run_lock "$LOCKDIR" 2>/dev/null)
  LOCK_RC=$?
  set -e
  if [[ "$LOCK_RC" -ne 10 || "$LOCK_STATE" != "stale" ]]; then
    # Active or recently ambiguous locks fail closed and preserve overlap protection.
    exit 0
  fi
  RECOVERY_DIR="$RUNTIME/agent.lockdir.stale.$$"
  if ! mv "$LOCKDIR" "$RECOVERY_DIR" 2>/dev/null; then exit 0; fi
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    mv "$RECOVERY_DIR" "$LOCKDIR" 2>/dev/null || true
    exit 0
  fi
  rm -rf "$RECOVERY_DIR"
  RECOVERED=1
  print -r -- "$(date '+%Y-%m-%dT%H:%M:%S%z') recovered stale run lock" >> "$RUNTIME/agent.log"
fi
print -r -- "$$" > "$LOCKDIR/pid"
date +%s > "$LOCKDIR/started_at_epoch"
cleanup_lock() {
  if [[ -f "$LOCKDIR/pid" && "$(<"$LOCKDIR/pid")" == "$$" ]]; then
    rm -f "$LOCKDIR/pid" "$LOCKDIR/started_at_epoch"
    rmdir "$LOCKDIR" 2>/dev/null || true
  fi
}
trap cleanup_lock EXIT INT TERM

export TRADING_AGENT_LOCK_HELD=1
export TRADING_AGENT_STALE_LOCK_RECOVERED="$RECOVERED"
"$ROOT/.venv/bin/python" -m app.main
