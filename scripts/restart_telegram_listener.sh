#!/bin/zsh
# Restart only the launchd-owned listener from the active immutable release.
set -euo pipefail

ROOT="${0:A:h:h}"
ACTIVE_LINK="$HOME/TradingAgentRuntime"
STATE_ROOT="$HOME/Library/Application Support/TradingAgent"
PLIST="$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
LABEL="com.elijah.tradingagent.telegram"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

fail() {
  print -u2 -- "$1"
  exit 2
}

[[ -L "$ACTIVE_LINK" ]] || fail "active runtime pointer is not a symlink"
ACTIVE_ROOT="${ACTIVE_LINK:A}"
[[ "$ROOT" == "$ACTIVE_ROOT" ]] || {
  fail "restart must be invoked from the active immutable runtime"
}
[[ "$ACTIVE_ROOT" == "$HOME/TradingAgentReleases/"* ]] || {
  fail "active runtime is not an immutable release"
}
[[ -x "$ROOT/.venv/bin/python" ]] || fail "release interpreter is unavailable"
[[ -f "$PLIST" && ! -L "$PLIST" ]] || fail "installed listener plist is missing or unsafe"
cmp -s "$ROOT/launchd/com.elijah.tradingagent.telegram.plist" "$PLIST" || {
  fail "installed listener plist does not match the active release"
}
plutil -lint "$PLIST" >/dev/null

cd "$ROOT"
MODE=$("$ROOT/.venv/bin/python" -c \
  'import json; print(json.load(open("release-manifest.json"))["release_authority"]["mode"])')
[[ "$MODE" == "forward" || "$MODE" == "rollback" ]] || {
  fail "release deployment authority mode is invalid"
}
shasum -a 256 -c release-file-inventory.sha256 >/dev/null
"$ROOT/.venv/bin/python" scripts/verify_source_tree.py \
  --root "$ROOT" --inventory "$ROOT/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus >/dev/null
"$ROOT/.venv/bin/python" scripts/verify_release_artifact.py "$ROOT" >/dev/null
"$ROOT/.venv/bin/python" scripts/verify_deployment_authority.py \
  --manifest "$ROOT/release-manifest.json" --mode "$MODE" >/dev/null

launchctl print "$SERVICE" >/dev/null 2>&1 || {
  fail "listener is not currently launchd-owned; refusing a hidden start"
}

IDENTITY="$STATE_ROOT/runtime/telegram_listener_identity.json"
OLD_RUN_ID=$("$ROOT/.venv/bin/python" - "$IDENTITY" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    print(str(json.loads(path.read_text(encoding="utf-8")).get("run_id") or ""))
except Exception:
    print("")
PY
)

launchctl bootout "$DOMAIN" "$PLIST"
for _ in {1..20}; do
  if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
launchctl print "$SERVICE" >/dev/null 2>&1 && {
  fail "listener launchd service did not stop; no signal was sent"
}
[[ ! -d "$STATE_ROOT/locks/listener.lockdir" ]] || {
  fail "listener lock remains after launchd stop; refusing PID or lock manipulation"
}

launchctl bootstrap "$DOMAIN" "$PLIST"
for _ in {1..60}; do
  if "$ROOT/.venv/bin/python" - "$IDENTITY" "$OLD_RUN_ID" "$ROOT" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
old_run_id = sys.argv[2]
root = pathlib.Path(sys.argv[3]).resolve()
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    pid = int(value["pid"])
    os.kill(pid, 0)
except Exception:
    raise SystemExit(1)
if (
    not value.get("run_id")
    or str(value["run_id"]) == old_run_id
    or pathlib.Path(value.get("project_root", "")).resolve() != root
):
    raise SystemExit(1)
raise SystemExit(0)
PY
  then
    break
  fi
  sleep 1
done

"$ROOT/scripts/check_runtime_freshness.sh" --json
