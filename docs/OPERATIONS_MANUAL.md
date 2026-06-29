# Operations manual

## Daily

Keep the Mac plugged in, confirm `config/KILL_SWITCH` is absent only when operation is intended, run once manually, and review the newest preflight/risk/audit rows. Confirm proposals in Alpaca paper mode before responding. Use `/status`, `/pending`, `/report`, `/cashout`, and `/help`; cashout only reports a suggestion.

## Weekly and monthly

Weekly: inspect rejected/unknown orders, loss gates, data freshness, Telegram authorization, database backups, and paper fills. Run `scripts/backup_db.sh` and export Excel. Monthly: retain a SQLite backup plus Excel/CSV exports in a configured archive; never relocate the active DB to iCloud. Review drawdown, profit factor, failure logs, and whether assumptions still hold.

## Pause, resume, and incidents

Pause with `scripts/stop_agent.sh`. Resume only after diagnosing the cause, reviewing audit logs, and locally deleting `config/KILL_SWITCH`; Telegram cannot resume live mode. On an unknown order status, do not rerun execution: inspect Alpaca by client order ID and reconcile manually. Rotate logs with `scripts/rotate_logs.sh`.

Stay in paper mode until a long, reviewed paper record exists and independent security/risk review is complete. This v1 does not provide a supported live migration procedure.

## Deployment and process freshness

The Telegram listener runs as a daemon/long-running process, holding code in memory. To prevent running stale code after git commits/pulls (while scanner reloads code dynamically), we implement a freshness verification mechanism.

### Checking freshness
Run the check script to verify if the running Telegram listener matches the git HEAD commit:
```bash
./scripts/check_runtime_freshness.sh
```
Or check directly via Telegram command `/status`, which reports listener fresh/stale status.

### Restarting the listener
If the listener is reported stale, restart it safely using the dedicated restart helper:
```bash
./scripts/restart_telegram_listener.sh
```
This script unloads the stale listener, removes stale lock directory markers (only when no process is running), starts the new listener daemon, and verifies its PID and startup commit hash match the repository HEAD.

### Stale listener guard
If an approval reply (e.g. `yes AMX`) is received while the listener is running stale code:
1. The approval is blocked and marked `blocked` in the database.
2. No broker order is placed, and a warning is sent back to the Telegram chat.
3. An audit event `listener_stale_code_blocked_approval` is logged.

### Case study: AMX final validation incident
During the June 2026 deployment:
- A final-validation bug was fixed in the codebase.
- The scanner ran successfully with the new code, producing a dynamic proposal for AMX.
- The Telegram listener was not restarted and ran old code in-memory, causing the final validation path for the approval to fail with `no matching market profile found for symbol AMX`.
- Stale process restarts are now guarded programmatically and integrated into commit checks.

