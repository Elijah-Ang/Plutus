#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
SOURCE="$ROOT/launchd/com.elijah.tradingagent.plist"
TARGET="$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
sed "s|__PROJECT_ROOT__|$ROOT|g" "$SOURCE" > "$TARGET"
plutil -lint "$TARGET"
echo "Plist installed but NOT loaded. After review, run: launchctl bootstrap gui/$(id -u) '$TARGET'"
