#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
RUNTIME="$ROOT/logs/runtime"
ERRORS="$ROOT/logs/errors"
mkdir -p "$RUNTIME" "$ERRORS"
exec 9>"$RUNTIME/agent.lock"
if ! /usr/bin/flock -n 9 2>/dev/null; then
  # macOS normally lacks flock; mkdir provides an atomic fallback.
  LOCKDIR="$RUNTIME/agent.lockdir"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then exit 0; fi
  trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT INT TERM
fi
POWER="$(/usr/bin/pmset -g batt 2>/dev/null || true)"
if [[ "${POWER:l}" != *"ac power"* ]]; then
  print -r -- "$(date '+%Y-%m-%dT%H:%M:%S%z') safe exit: AC power not confirmed" >> "$RUNTIME/agent.log"
  exit 0
fi
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  print -r -- "Virtual environment missing; run scripts/setup_venv.sh" >> "$ERRORS/launchd.err"
  exit 1
fi
export TRADING_AGENT_LOCK_HELD=1
cd "$ROOT"
"$ROOT/.venv/bin/python" -m app.main >> "$RUNTIME/launchd.out" 2>> "$ERRORS/launchd.err"
