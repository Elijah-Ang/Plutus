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

## Position Sizing and Portfolio Constraints

The system uses the canonical, versioned policy in `config/config.yaml` when `position_sizing.mode: risk_portfolio` is enabled. The configured moderate-paper defaults are `$250` initial and `$100` add; they are targets, not guarantees. A missing validated ATR/technical stop, malformed pending buy exposure, or incomplete MA50/MA200 evidence blocks the affected entry/add.

### 1. Risk-Based Sizing Formula

The base trade size is driven by account equity and the configured per-trade risk percentage, rather than buying power:

$$\text{risk\_budget} = \text{equity} \times \frac{\text{risk\_per\_trade\_pct}}{100}$$

$$\text{raw\_risk\_based\_notional} = \frac{\text{risk\_budget} \times \text{entry\_price}}{\text{stop\_distance\_dollars}}$$

*   **Equity**: Derived from broker account equity (e.g. `$100,000.00`).
*   **Risk per trade**: Configured as a percentage of authoritative equity.
*   **Stop Distance**: Calculated from validated ATR or technical evidence, bounded by `min_stop_pct`/`max_stop_pct`; percentage/fixed fallback stops are not executable.

### 2. Sizing target

The raw risk-based size is adjusted only by the configured volatility regime for operational sizing:

$$\text{target\_notional} = \text{raw\_risk\_based\_notional} \times \text{volatility\_multiplier}$$

Score does not increase Phase 3/4 operational risk. Any diagnostic score fields are persisted separately.
*   **Volatility Multipliers**:
    *   normal: `1.00x`
    *   too quiet: `0.75x`
    *   elevated: `0.50x`
    *   high: `0.25x`
    *   extreme: `0.00x` (blocks entry)

### 3. Sizing Constraints and Caps

The final notional is the minimum of every applicable ceiling: phase stage, validated stop risk, equity, cash, cash available, cash usage, buying power, symbol, cluster, portfolio, allocation, exploration, optional absolute cap, and Phase 3/4 heat/gross limits. Each maximum is only a ceiling. If the result is below `minimum_executable_notional_usd`, the system blocks instead of raising it.

1.  **Cash Reserve Cap**: Sizing is strictly limited by available cash minus a configured minimum reserve percentage:
    $$\text{usable\_cash} = \text{cash} - (\text{equity} \times \text{min\_cash\_reserve\_pct})$$
    $$\text{cash\_cap} = \min(\text{usable\_cash}, \text{equity} \times \text{max\_cash\_usage\_pct})$$
    Buying power/margin is ignored for cash reserves (`max_margin_usage_pct = 0.0`).
2.  **Single Position Cap**: Limits maximum symbol exposure (e.g., `2.0%` of equity).
3.  **Portfolio Exposure Cap**: Limits total active portfolio exposure (e.g., `6.0%` of equity).
4.  **Cluster Exposure Cap**: Limits total exposure to a single sector/cluster (e.g., `5.0%` of equity).
5.  **Stage Dollar Cap**: Enforces the configured initial/add stage ceilings. The temporary `$50` absolute cap is not part of the current policy.

### 4. Small-account and blocking rules

To facilitate testing on small accounts:
*   `$5.00` is an executable minimum, not a clamp. A constrained result below it is blocked.
*   Every normal order uses a fresh quote and a bounded limit price.
*   All ordinary entries/adds require manual Telegram approval and final revalidation.

See [CONFIGURATION_AND_SIZING.md](CONFIGURATION_AND_SIZING.md) for the formula and schema version contract.

