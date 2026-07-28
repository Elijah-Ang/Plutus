# launchd setup

Production launchd definitions belong to the active immutable release selected
by `$HOME/TradingAgentRuntime`. The scanner runs
`scripts/run_once.sh` every 600 seconds; the listener runs
`scripts/run_telegram_listener.sh` continuously. Mutable logs, locks,
heartbeats, process identities, database files, environment and kill-switch
state live below `$HOME/Library/Application Support/TradingAgent`.

`install_launchd.sh` must be invoked through the active runtime symlink. It
verifies the release artifact, tracked source, exact remote forward/rollback
authority and plist bytes, installs owner-only plists, and deliberately does
not load them:

```zsh
"$HOME/TradingAgentRuntime/scripts/install_launchd.sh"
plutil -p "$HOME/Library/LaunchAgents/com.elijah.tradingagent.plist"
plutil -p "$HOME/Library/LaunchAgents/com.elijah.tradingagent.telegram.plist"
```

Start services only as the last step of the controlled deployment/start
procedure, after release, CI, database-migration, paper/manual-only and
stopped-writer gates pass. A source checkout is never launch authority.

To stop and remove the exact launchd services, run
`"$HOME/TradingAgentRuntime/scripts/uninstall_launchd.sh"`. The helper refuses
symlinked or missing authority files, verifies each service is absent before
removing its plist, and never signals a process by pattern.

After startup, wait for one listener poll and one scanner cycle, then retain:

```zsh
"$HOME/TradingAgentRuntime/scripts/check_runtime_freshness.sh" --json
```
