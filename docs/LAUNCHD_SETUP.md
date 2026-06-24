# launchd setup

The scanner plist schedules `scripts/run_once.sh` every 600 seconds. The starter confirms AC power, prevents overlapping runs, activates the project venv, and writes stdout/stderr under `logs/`.

After manual testing, install the expanded plist without loading it:

```zsh
./scripts/install_launchd.sh
plutil -p "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
launchctl print "gui/$(id -u)/com.elijah.tradingagent"
```

Unload with `launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"`, or run `./scripts/uninstall_launchd.sh`. Inspect `logs/runtime/launchd.out` and `logs/errors/launchd.err`. Test once on battery: the log must show a safe exit and no broker order.
