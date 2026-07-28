#!/bin/zsh
# Route kill-switch changes through the active immutable runtime.
set -euo pipefail

ACTIVE_LINK="$HOME/TradingAgentRuntime"
STATE_ROOT="$HOME/Library/Application Support/TradingAgent"

# Emergency pause must remain available even when release verification or the
# release interpreter is broken. This path only creates a fail-closed external
# sentinel; it cannot clear one or start any process.
if [[ "${1:-}" == "enable" && "$#" -eq 1 ]]; then
  exec /usr/bin/python3 - "$STATE_ROOT/runtime" <<'PY'
import json
import os
import pathlib
import stat
import sys

runtime = pathlib.Path(sys.argv[1])
metadata = runtime.lstat()
if (
    runtime.is_symlink()
    or not stat.S_ISDIR(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or stat.S_IMODE(metadata.st_mode) & 0o077
):
    raise SystemExit("production runtime state directory is missing or unsafe")
path = runtime / "KILL_SWITCH"
if path.is_symlink():
    raise SystemExit("kill-switch path must not be a symlink")
if path.exists():
    existing = path.stat()
    if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
        raise SystemExit("kill-switch path must be an owner-controlled regular file")
flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(path, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory = os.open(runtime, os.O_RDONLY | os.O_CLOEXEC)
try:
    os.fsync(directory)
finally:
    os.close(directory)
print(json.dumps({"action": "enable", "active": True, "kill_switch_path": str(path)}, sort_keys=True))
PY
fi

[[ -L "$ACTIVE_LINK" ]] || { print -u2 -- "active runtime pointer is not a symlink"; exit 2; }
ACTIVE_ROOT="${ACTIVE_LINK:A}"
[[ "$ACTIVE_ROOT" == "$HOME/TradingAgentReleases/"* ]] || {
  print -u2 -- "active runtime is not an immutable release"
  exit 2
}
[[ -x "$ACTIVE_ROOT/.venv/bin/python" ]] || {
  print -u2 -- "active release interpreter is unavailable"
  exit 2
}
[[ -f "$ACTIVE_ROOT/release-manifest.json" ]] || {
  print -u2 -- "active release manifest is unavailable"
  exit 2
}

export TRADING_AGENT_RUNTIME=production-paper
export TRADING_AGENT_PROJECT_ROOT="$ACTIVE_ROOT"
export TRADING_AGENT_STATE_ROOT="$STATE_ROOT"
export TRADING_AGENT_ENV_FILE="$STATE_ROOT/runtime/production.env"
export PYTHONDONTWRITEBYTECODE=1

if [[ "${1:-}" == "disable" ]]; then
  cd "$ACTIVE_ROOT"
  MODE=$("$ACTIVE_ROOT/.venv/bin/python" -c \
    'import json; print(json.load(open("release-manifest.json"))["release_authority"]["mode"])')
  [[ "$MODE" == "forward" || "$MODE" == "rollback" ]] || {
    print -u2 -- "release deployment authority mode is invalid"
    exit 2
  }
  shasum -a 256 -c release-file-inventory.sha256 >/dev/null
  "$ACTIVE_ROOT/.venv/bin/python" scripts/verify_source_tree.py \
    --root "$ACTIVE_ROOT" --inventory "$ACTIVE_ROOT/tracked-source-inventory.json" \
    --repository Elijah-Ang/Plutus >/dev/null
  "$ACTIVE_ROOT/.venv/bin/python" scripts/verify_release_artifact.py "$ACTIVE_ROOT" >/dev/null
  "$ACTIVE_ROOT/.venv/bin/python" scripts/verify_deployment_authority.py \
    --manifest "$ACTIVE_ROOT/release-manifest.json" --mode "$MODE" >/dev/null
fi

exec "$ACTIVE_ROOT/.venv/bin/python" "$ACTIVE_ROOT/scripts/manage_kill_switch.py" "$@"
