#!/bin/zsh
# Stop exact launchd-owned services and remove only their trusted plist files.
set -euo pipefail

DOMAIN="gui/$(id -u)"
TARGET_DIR="$HOME/Library/LaunchAgents"
for label in com.elijah.tradingagent.telegram com.elijah.tradingagent; do
  target="$TARGET_DIR/$label.plist"
  [[ ! -L "$target" ]] || {
    print -u2 -- "refusing symlinked launchd plist: $target"
    exit 2
  }
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    [[ -f "$target" ]] || {
      print -u2 -- "loaded service has no trusted plist: $label"
      exit 2
    }
    launchctl bootout "$DOMAIN" "$target"
  fi
  launchctl print "$DOMAIN/$label" >/dev/null 2>&1 && {
    print -u2 -- "service remains loaded; plist was preserved: $label"
    exit 2
  }
  [[ ! -e "$target" ]] || rm "$target"
done

print -- "Exact LaunchAgents are stopped and removed."
