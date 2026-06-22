#!/bin/zsh
set -euo pipefail
TARGET="$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
launchctl bootout "gui/$(id -u)" "$TARGET" 2>/dev/null || true
rm -f "$TARGET"

TARGET_TG="$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
launchctl bootout "gui/$(id -u)" "$TARGET_TG" 2>/dev/null || true
rm -f "$TARGET_TG"

echo "LaunchAgents unloaded and removed."
