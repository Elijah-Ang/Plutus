#!/bin/zsh
set -euo pipefail

RELEASE="${1:?usage: deploy_release.sh /absolute/release/path}"
STATE_ROOT="$HOME/Library/Application Support/TradingAgent"
RUNTIME="$HOME/TradingAgentRuntime"
[[ -f "$RELEASE/release-manifest.json" ]] || { print -u2 -- "missing release manifest"; exit 2; }
[[ -f "$RELEASE/release-file-inventory.sha256" && -f "$RELEASE/dependency-inventory.txt" ]] || { print -u2 -- "release inventory is incomplete"; exit 2; }
cd "$RELEASE"
shasum -a 256 -c release-file-inventory.sha256
"$RELEASE/.venv/bin/python" - <<'PY'
import hashlib, json
from pathlib import Path
m=json.load(open("release-manifest.json", encoding="utf-8"))
assert m["mode"] == "paper" and m["manual_approval_only"] is True and m["live_capability"] is False
assert hashlib.sha256(Path("requirements.lock").read_bytes()).hexdigest() == m["requirements_lock_sha256"]
assert hashlib.sha256(Path("dependency-inventory.txt").read_bytes()).hexdigest() == m["dependency_inventory_sha256"]
PY
COMMIT=$("$RELEASE/.venv/bin/python" -c 'import json; print(json.load(open("release-manifest.json"))["release_commit"])')
REMOTE_MAIN=$(git ls-remote git@github.com:Elijah-Ang/Plutus.git refs/heads/main | awk '{print $1}')
[[ -n "$REMOTE_MAIN" && "$COMMIT" == "$REMOTE_MAIN" ]] || { print -u2 -- "release is not the exact current remote main SHA"; exit 2; }
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
[[ "$RELEASE" == "$HOME/TradingAgentReleases/"* ]] || { print -u2 -- "release must be immutable release path"; exit 2; }
# BSD ln replaces the symlink entry itself; this does not traverse the old
# runtime target and keeps the pointer switch as one filesystem operation.
ln -sfn "$RELEASE" "$RUNTIME"
[[ "$(readlink "$RUNTIME")" == "$RELEASE" ]] || { print -u2 -- "runtime pointer switch failed"; exit 2; }
cp "$RELEASE/launchd/com.elijah.tradingagent.plist" "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
cp "$RELEASE/launchd/com.elijah.tradingagent.telegram.plist" "$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
mkdir -p "$STATE_ROOT/release"
chmod 700 "$STATE_ROOT/release"
cp "$RELEASE/release-manifest.json" "$STATE_ROOT/release/active-release.json"
chmod 600 "$STATE_ROOT/release/active-release.json"
print -- "runtime pointer switched to $RELEASE; jobs remain stopped"
