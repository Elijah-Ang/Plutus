#!/bin/zsh
# scripts/restart_telegram_listener.sh - Safely restart the Telegram listener service.
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"

PLIST="$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
LOCKDIR="$ROOT/logs/runtime/listener.lockdir"
IDENTITY_FILE="$ROOT/logs/runtime/telegram_listener_identity.json"

echo "=== Safely Restarting Telegram Listener ==="

# 1. Stop/unload old listener safely
if launchctl print "gui/$(id -u)/com.elijah.tradingagent.telegram" >/dev/null 2>&1; then
  echo "Unloading com.elijah.tradingagent.telegram..."
  launchctl bootout "gui/$(id -u)" "$PLIST" || true
  sleep 2
fi

# 2. Check if any listener python process is still alive
OLD_PID=""
if [[ -f "$IDENTITY_FILE" ]]; then
  OLD_PID=$(python3 -c "import json; print(json.load(open('$IDENTITY_FILE')).get('pid', ''))" 2>/dev/null || true)
fi

if [[ -n "$OLD_PID" ]]; then
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Warning: Listener process $OLD_PID is still alive. Sending SIGTERM..."
    kill -15 "$OLD_PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Error: Process $OLD_PID refused to terminate. Sending SIGKILL..."
      kill -9 "$OLD_PID" 2>/dev/null || true
    fi
  fi
fi

# Double check running listener python processes
STALE_PIDS=$(ps -ef | grep -E "python.*app.main.*--mode listener" | grep -v grep | awk '{print $2}' || true)
if [[ -n "$STALE_PIDS" ]]; then
  echo "Killing other stale listener processes: $STALE_PIDS"
  echo "$STALE_PIDS" | xargs kill -9 2>/dev/null || true
fi

# 3. Remove stale listener lock only if no listener process is alive
if [[ -d "$LOCKDIR" ]]; then
  echo "Removing stale lock directory..."
  rmdir "$LOCKDIR" 2>/dev/null || true
fi

# 4. Start/load listener safely
echo "Loading com.elijah.tradingagent.telegram..."
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3

# 5. Confirm new listener PID and status
NEW_PID=$(ps -ef | grep -E "python.*app.main.*--mode listener" | grep -v grep | awk '{print $2}' | head -n 1 || true)


if [[ -z "$NEW_PID" ]]; then
  echo "❌ Error: Failed to start Telegram listener."
  exit 1
fi

echo "New listener PID: $NEW_PID"

# 6. Confirm details from identity file
if [[ ! -f "$IDENTITY_FILE" ]]; then
  echo "❌ Error: New listener identity file was not created."
  exit 1
fi

NEW_PID_CONFIRMED=$(python3 -c "import json; print(json.load(open('$IDENTITY_FILE')).get('pid', ''))" 2>/dev/null || true)
NEW_START_TIME=$(python3 -c "import json; print(json.load(open('$IDENTITY_FILE')).get('start_time', ''))" 2>/dev/null || true)
NEW_ROOT=$(python3 -c "import json; print(json.load(open('$IDENTITY_FILE')).get('project_root', ''))" 2>/dev/null || true)
NEW_COMMIT=$(python3 -c "import json; print(json.load(open('$IDENTITY_FILE')).get('commit', ''))" 2>/dev/null || true)
CURRENT_HEAD=$(git rev-parse HEAD)

if [[ "$NEW_PID_CONFIRMED" != "$NEW_PID" ]]; then
  echo "❌ Error: Confirmed PID mismatch ($NEW_PID_CONFIRMED vs $NEW_PID)."
  exit 1
fi

echo "New listener start time: $NEW_START_TIME"
echo "New listener root: $NEW_ROOT"
echo "New listener commit: $NEW_COMMIT"
echo "Current HEAD commit: $CURRENT_HEAD"

if [[ "$NEW_ROOT" != "$ROOT" ]]; then
  echo "❌ Error: Root directory mismatch ($NEW_ROOT vs $ROOT)."
  exit 1
fi

if [[ "$NEW_COMMIT" != "$CURRENT_HEAD" ]]; then
  echo "❌ Error: Commit mismatch ($NEW_COMMIT vs $CURRENT_HEAD)."
  exit 1
fi

echo "✅ Success: Telegram listener safely restarted and matches current HEAD."
exit 0
