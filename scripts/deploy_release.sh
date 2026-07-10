#!/bin/zsh
set -euo pipefail

RELEASE="${1:?usage: deploy_release.sh /absolute/release/path}"
STATE_ROOT="$HOME/Library/Application Support/TradingAgent"
RUNTIME="$HOME/TradingAgentRuntime"
[[ -f "$RELEASE/release-manifest.json" ]] || { print -u2 -- "missing release manifest"; exit 2; }
[[ "$("$RELEASE/.venv/bin/python" -c 'import json; print(json.load(open("release-manifest.json"))["mode"])')" == "paper" ]] || { print -u2 -- "release is not paper-only"; exit 2; }
[[ "$(launchctl print gui/$(id -u)/com.elijah.tradingagent 2>&1 || true)" == *"Could not find service"* ]] || { print -u2 -- "scanner must be stopped"; exit 2; }
[[ "$(launchctl print gui/$(id -u)/com.elijah.tradingagent.telegram 2>&1 || true)" == *"Could not find service"* ]] || { print -u2 -- "listener must be stopped"; exit 2; }
[[ "$RELEASE" == "$HOME/TradingAgentReleases/"* ]] || { print -u2 -- "release must be immutable release path"; exit 2; }
ln -sfn "$RELEASE" "$RUNTIME.next"
mv -f "$RUNTIME.next" "$RUNTIME"
cp "$RELEASE/launchd/com.elijah.tradingagent.plist" "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
cp "$RELEASE/launchd/com.elijah.tradingagent.telegram.plist" "$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
mkdir -p "$STATE_ROOT/release"
chmod 700 "$STATE_ROOT/release"
cp "$RELEASE/release-manifest.json" "$STATE_ROOT/release/active-release.json"
chmod 600 "$STATE_ROOT/release/active-release.json"
print -- "runtime pointer switched to $RELEASE; jobs remain stopped"
