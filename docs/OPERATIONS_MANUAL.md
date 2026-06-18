# Operations manual

## Daily

Keep the Mac plugged in, confirm `config/KILL_SWITCH` is absent only when operation is intended, run once manually, and review the newest preflight/risk/audit rows. Confirm proposals in Alpaca paper mode before responding. Use `/status`, `/pending`, `/report`, `/cashout`, and `/help`; cashout only reports a suggestion.

## Weekly and monthly

Weekly: inspect rejected/unknown orders, loss gates, data freshness, Telegram authorization, database backups, and paper fills. Run `scripts/backup_db.sh` and export Excel. Monthly: retain a SQLite backup plus Excel/CSV exports in a configured archive; never relocate the active DB to iCloud. Review drawdown, profit factor, failure logs, and whether assumptions still hold.

## Pause, resume, and incidents

Pause with `scripts/stop_agent.sh`. Resume only after diagnosing the cause, reviewing audit logs, and locally deleting `config/KILL_SWITCH`; Telegram cannot resume live mode. On an unknown order status, do not rerun execution: inspect Alpaca by client order ID and reconcile manually. Rotate logs with `scripts/rotate_logs.sh`.

Stay in paper mode until a long, reviewed paper record exists and independent security/risk review is complete. This v1 does not provide a supported live migration procedure.
