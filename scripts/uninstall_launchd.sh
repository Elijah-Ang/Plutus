#!/bin/zsh
set -euo pipefail
TARGET="$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
launchctl bootout "gui/$(id -u)" "$TARGET" 2>/dev/null || true
rm -f "$TARGET"
echo "LaunchAgent unloaded and removed."
