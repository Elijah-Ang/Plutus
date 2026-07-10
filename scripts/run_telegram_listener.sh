#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"

RUNTIME="$ROOT/logs/runtime"
ERRORS="$ROOT/logs/errors"
mkdir -p "$RUNTIME" "$ERRORS"

LOCKDIR="$RUNTIME/listener.lockdir"
RECOVERED=0
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  set +e
  LOCK_STATE=$("$ROOT/.venv/bin/python" -m app.run_lock "$LOCKDIR" 2>/dev/null)
  LOCK_RC=$?
  set -e
  if [[ "$LOCK_RC" -ne 10 || "$LOCK_STATE" != "stale" ]]; then
    print -r -- "$(date '+%Y-%m-%dT%H:%M:%S%z') listener overlap skipped lock_state=${LOCK_STATE:-unknown}" >> "$RUNTIME/listener.out"
    exit 0
  fi
  RECOVERY_DIR="$RUNTIME/listener.lockdir.stale.$$"
  if ! mv "$LOCKDIR" "$RECOVERY_DIR" 2>/dev/null; then exit 0; fi
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    mv "$RECOVERY_DIR" "$LOCKDIR" 2>/dev/null || true
    exit 0
  fi
  rm -rf "$RECOVERY_DIR"
  RECOVERED=1
  print -r -- "$(date '+%Y-%m-%dT%H:%M:%S%z') recovered stale listener lock" >> "$RUNTIME/listener.out"
fi
print -r -- "$$" > "$LOCKDIR/pid"
date +%s > "$LOCKDIR/started_at_epoch"
print -r -- "$ROOT" > "$LOCKDIR/repository_path"
git rev-parse HEAD > "$LOCKDIR/commit" 2>/dev/null || print -r -- "unknown" > "$LOCKDIR/commit"
cleanup_lock() {
  if [[ -f "$LOCKDIR/pid" && "$(<"$LOCKDIR/pid")" == "$$" ]]; then
    rm -f "$LOCKDIR/pid" "$LOCKDIR/started_at_epoch" "$LOCKDIR/repository_path" "$LOCKDIR/commit"
    rmdir "$LOCKDIR" 2>/dev/null || true
  fi
}
trap cleanup_lock EXIT INT TERM

export TRADING_AGENT_LOCK_HELD=1
export TRADING_AGENT_STALE_LOCK_RECOVERED="$RECOVERED"
"$ROOT/.venv/bin/python" -m app.main --mode listener >> "$RUNTIME/listener.out" 2>> "$ERRORS/listener.err"
