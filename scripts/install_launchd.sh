#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
SOURCE="$ROOT/launchd/com.elijah.tradingagent.plist"
TARGET="$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
sed "s|__PROJECT_ROOT__|$ROOT|g" "$SOURCE" > "$TARGET"
plutil -lint "$TARGET"

SOURCE_TG="$ROOT/launchd/com.elijah.tradingagent.telegram.plist"
TARGET_TG="$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
sed "s|__PROJECT_ROOT__|$ROOT|g" "$SOURCE_TG" > "$TARGET_TG"
plutil -lint "$TARGET_TG"

echo "Plists installed but NOT loaded. After review, run:"
echo "  launchctl bootstrap gui/$(id -u) '$TARGET'"
echo "  launchctl bootstrap gui/$(id -u) '$TARGET_TG'"
