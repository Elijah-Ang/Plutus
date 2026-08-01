#!/bin/zsh
set -euo pipefail

MODE="forward"
if [[ "${1:-}" == "--mode" ]]; then MODE="${2:?missing deployment mode}"; shift 2; fi
RELEASE="${1:?usage: deploy_release.sh [--mode forward|rollback] /absolute/release/path}"
[[ "$MODE" == "forward" || "$MODE" == "rollback" ]] || { print -u2 -- "mode must be forward or rollback"; exit 2; }
STATE_ROOT="$HOME/Library/Application Support/TradingAgent"
RUNTIME="$HOME/TradingAgentRuntime"
[[ -f "$RELEASE/release-manifest.json" ]] || { print -u2 -- "missing release manifest"; exit 2; }
[[ -f "$RELEASE/release-file-inventory.sha256" && -f "$RELEASE/dependency-inventory.txt" && -f "$RELEASE/tracked-source-inventory.json" ]] || { print -u2 -- "release inventory is incomplete"; exit 2; }
[[ -f "$RELEASE/artifact-test-results.json" && -f "$RELEASE/requirements-hashes.lock" ]] || { print -u2 -- "artifact test or hash-lock evidence is missing"; exit 2; }
[[ "$RELEASE" == "$HOME/TradingAgentReleases/"* ]] || { print -u2 -- "release must be an immutable release path"; exit 2; }

cd "$RELEASE"
# Every artifact byte, environment package, configuration hash, schema/formula
# identity, test result and Python version is verified before any pointer write.
shasum -a 256 -c release-file-inventory.sha256
"$RELEASE/.venv/bin/python" scripts/verify_source_tree.py \
  --root "$RELEASE" --inventory "$RELEASE/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus
"$RELEASE/.venv/bin/python" scripts/verify_release_artifact.py "$RELEASE"
"$RELEASE/.venv/bin/python" scripts/verify_deployment_authority.py \
  --manifest "$RELEASE/release-manifest.json" --mode "$MODE"

COMMIT=$("$RELEASE/.venv/bin/python" -c 'import json; print(json.load(open("release-manifest.json"))["release_commit"])')
CI_HEAD=$("$RELEASE/.venv/bin/python" -c 'import json; print(json.load(open("release-manifest.json"))["ci"]["head_sha"])')
[[ "$CI_HEAD" == "$COMMIT" ]] || { print -u2 -- "release CI identity is mismatched"; exit 2; }
"$RELEASE/.venv/bin/python" - <<'PY'
import json, os, urllib.request
m=json.load(open("release-manifest.json", encoding="utf-8"))
run_id=str(m["ci"]["run_id"])
headers={"Accept":"application/vnd.github+json","User-Agent":"plutus-deploy-verifier"}
if os.getenv("GITHUB_TOKEN"): headers["Authorization"]="Bearer "+os.environ["GITHUB_TOKEN"]
def get(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=15) as r: return json.load(r)
run=get(f"https://api.github.com/repos/Elijah-Ang/Plutus/actions/runs/{run_id}")
assert run["name"] == "CI" and run["head_sha"] == m["release_commit"]
assert run["status"] == "completed" and run["conclusion"] == "success"
jobs=get(f"https://api.github.com/repos/Elijah-Ang/Plutus/actions/runs/{run_id}/jobs?per_page=100")["jobs"]
offline=[j for j in jobs if j["name"] == "offline-tests"]
assert len(offline) == 1 and offline[0]["status"] == "completed" and offline[0]["conclusion"] == "success"
PY

[[ "$(launchctl print gui/$(id -u)/com.elijah.tradingagent 2>&1 || true)" == *"Could not find service"* ]] || { print -u2 -- "scanner must be stopped"; exit 2; }
[[ "$(launchctl print gui/$(id -u)/com.elijah.tradingagent.telegram 2>&1 || true)" == *"Could not find service"* ]] || { print -u2 -- "listener must be stopped"; exit 2; }
"$RELEASE/.venv/bin/python" - "$STATE_ROOT/runtime" <<'PY'
import os
import pathlib
import stat
import sys

runtime = pathlib.Path(sys.argv[1])
metadata = runtime.lstat()
assert (
    runtime.is_absolute()
    and not runtime.is_symlink()
    and stat.S_ISDIR(metadata.st_mode)
    and metadata.st_uid == os.getuid()
    and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
), "external runtime state directory must be owner-only"
PY
if [[ -L "$RUNTIME" && -e "${RUNTIME:A}/config/KILL_SWITCH" && ! -e "$STATE_ROOT/runtime/KILL_SWITCH" ]]; then
  print -u2 -- "legacy active kill switch must be preserved in external runtime state before deployment"
  exit 2
fi
[[ ! -L "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist" ]] || { print -u2 -- "scanner plist target is a symlink"; exit 2; }
[[ ! -L "$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist" ]] || { print -u2 -- "listener plist target is a symlink"; exit 2; }
# Validate and install both launchd definitions while the old runtime pointer
# is still active.  A failed install or lint therefore cannot leave the
# pointer aimed at a release whose writers cannot be started safely.
for name in com.elijah.tradingagent.plist com.elijah.tradingagent.telegram.plist; do
  plutil -lint "$RELEASE/launchd/$name" >/dev/null
  /usr/bin/install -m 600 "$RELEASE/launchd/$name" "$HOME/Library/LaunchAgents/$name"
  plutil -lint "$HOME/Library/LaunchAgents/$name" >/dev/null
  cmp -s "$RELEASE/launchd/$name" "$HOME/Library/LaunchAgents/$name" || {
    print -u2 -- "installed $name differs from the release"; exit 2
  }
done
ln -sfn "$RELEASE" "$RUNTIME"
[[ "$(readlink "$RUNTIME")" == "$RELEASE" ]] || { print -u2 -- "runtime pointer switch failed"; exit 2; }
mkdir -p "$STATE_ROOT/release"
chmod 700 "$STATE_ROOT/release"
cp "$RELEASE/release-manifest.json" "$STATE_ROOT/release/active-release.json"
chmod 600 "$STATE_ROOT/release/active-release.json"
print -- "$MODE runtime pointer switched to $RELEASE; jobs remain stopped"
