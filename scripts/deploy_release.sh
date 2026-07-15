#!/bin/zsh
set -euo pipefail

MODE="forward"
if [[ "${1:-}" == "--mode" ]]; then MODE="${2:?missing deployment mode}"; shift 2; fi
RELEASE="${1:?usage: deploy_release.sh [--mode forward|rollback] /absolute/release/path}"
[[ "$MODE" == "forward" || "$MODE" == "rollback" ]] || { print -u2 -- "mode must be forward or rollback"; exit 2; }
STATE_ROOT="$HOME/Library/Application Support/TradingAgent"
RUNTIME="$HOME/TradingAgentRuntime"
[[ -f "$RELEASE/release-manifest.json" ]] || { print -u2 -- "missing release manifest"; exit 2; }
[[ -f "$RELEASE/release-file-inventory.sha256" && -f "$RELEASE/dependency-inventory.txt" ]] || { print -u2 -- "release inventory is incomplete"; exit 2; }
[[ -f "$RELEASE/artifact-test-results.json" && -f "$RELEASE/requirements-hashes.lock" ]] || { print -u2 -- "artifact test or hash-lock evidence is missing"; exit 2; }
[[ "$RELEASE" == "$HOME/TradingAgentReleases/"* ]] || { print -u2 -- "release must be an immutable release path"; exit 2; }

cd "$RELEASE"
# Every artifact byte, environment package, configuration hash, schema/formula
# identity, test result and Python version is verified before any pointer write.
shasum -a 256 -c release-file-inventory.sha256
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
print -- "$MODE runtime pointer switched to $RELEASE; jobs remain stopped"
