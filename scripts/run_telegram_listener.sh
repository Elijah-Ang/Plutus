#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"

STATE_ROOT="${TRADING_AGENT_STATE_ROOT:-$HOME/Library/Application Support/TradingAgent}"
RUNTIME="$STATE_ROOT/locks"
LOGS="$STATE_ROOT/logs"
mkdir -p "$RUNTIME" "$LOGS"
chmod 700 "$RUNTIME" "$LOGS"

LOCKDIR="$RUNTIME/listener.lockdir"
RELEASE_ID=$("$ROOT/.venv/bin/python" -c 'import json; print(json.load(open("release-manifest.json"))["release_id"])')
CURRENT_COMMIT=$("$ROOT/.venv/bin/python" -c 'import json; print(json.load(open("release-manifest.json"))["release_commit"])')
RECOVERED=0
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  set +e
  LOCK_STATE=$("$ROOT/.venv/bin/python" -m app.run_lock "$LOCKDIR" \
    --expected-command "run_telegram_listener.sh" \
    --expected-repository "$ROOT" --expected-commit "$CURRENT_COMMIT" 2>/dev/null)
  LOCK_RC=$?
  set -e
  if [[ "$LOCK_RC" -ne 10 || "$LOCK_STATE" != "stale" ]]; then
    print -r -- "$(date '+%Y-%m-%dT%H:%M:%S%z') listener overlap skipped lock_state=${LOCK_STATE:-unknown}" >> "$LOGS/listener.out"
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
  print -r -- "$(date '+%Y-%m-%dT%H:%M:%S%z') recovered stale listener lock" >> "$LOGS/listener.out"
fi
"$ROOT/.venv/bin/python" -m app.run_lock "$LOCKDIR" --write-owner --pid "$$" \
  --expected-repository "$ROOT" --expected-commit "$CURRENT_COMMIT"
cleanup_lock() {
  if [[ -f "$LOCKDIR/pid" && "$(<"$LOCKDIR/pid")" == "$$" ]]; then
    rm -f "$LOCKDIR/pid" "$LOCKDIR/started_at_epoch" "$LOCKDIR/repository_path" "$LOCKDIR/commit" \
      "$LOCKDIR/command_identity" "$LOCKDIR/process_start_token"
    rmdir "$LOCKDIR" 2>/dev/null || true
  fi
}
trap cleanup_lock EXIT INT TERM

export TRADING_AGENT_LOCK_HELD=1
export TRADING_AGENT_STALE_LOCK_RECOVERED="$RECOVERED"
export TRADING_AGENT_RUNTIME=production-paper
export TRADING_AGENT_STATE_ROOT="$STATE_ROOT"
export TRADING_AGENT_DATABASE_PATH="$STATE_ROOT/database/trading_agent.sqlite3"
export TRADING_AGENT_RELEASE_ID="$RELEASE_ID"
export TRADING_AGENT_LOCK_ROOT="$RUNTIME"
export TRADING_AGENT_LOG_ROOT="$LOGS"
"$ROOT/.venv/bin/python" -m app.main --mode listener >> "$LOGS/listener.out" 2>> "$LOGS/listener.err"
