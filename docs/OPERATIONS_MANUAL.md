# Operations manual

## Daily

Keep the Mac plugged in, confirm the external runtime kill switch is inactive only when operation is intended, and review the newest preflight/risk/audit rows. Confirm proposals in Alpaca paper mode before responding. Use `/status`, `/pending`, `/report`, `/cashout`, and `/help`; cashout only reports a suggestion.

## Weekly and monthly

Weekly: inspect rejected/unknown orders, loss gates, data freshness, Telegram authorization, database backups, and paper fills. Run `scripts/backup_db.sh` and export Excel. Monthly: retain a SQLite backup plus Excel/CSV exports in a configured archive; never relocate the active DB to iCloud. Review drawdown, profit factor, failure logs, and whether assumptions still hold.

## Pause, resume, and incidents

Pause production through the active immutable runtime with
`"$HOME/TradingAgentRuntime/scripts/stop_agent.sh"`. This enables the
owner-only external switch at
`$HOME/Library/Application Support/TradingAgent/runtime/KILL_SWITCH`; it does
not modify the release or unload launchd. Resume only after diagnosing the
cause and reviewing audit logs, using
`"$HOME/TradingAgentRuntime/scripts/start_agent.sh" "CONFIRM PAPER RESUME"`.
The resume helper revalidates paper/manual-only release authority and does not
load or restart jobs. Telegram `/resume CONFIRM PAPER RESUME` uses the same
external switch and cannot resume live mode. On an unknown order status, do not
rerun execution: inspect Alpaca by client order ID and reconcile manually.
Rotate logs with `scripts/rotate_logs.sh`.

Stay in paper mode until a long, reviewed paper record exists and independent security/risk review is complete. This v1 does not provide a supported live migration procedure.

## Deployment and process freshness

The scanner is periodic and the Telegram listener is long-running. Deployment is not proven merely because launchd accepted both jobs: the active runtime pointer, immutable release manifest, process identities, database heartbeats, and listener lock must all identify the same release.

### Checking freshness

After the listener has polled and one scanner cycle has completed, run:

```bash
./scripts/check_runtime_freshness.sh
```

Use `./scripts/check_runtime_freshness.sh --json` for retained deployment evidence. The command uses the immutable release's Python 3.13.9 interpreter and fails closed unless:

- `$HOME/TradingAgentRuntime` is an owner-controlled symlink to a release directory whose name and manifest ID agree;
- the manifest binds paper-only, manual-only, live-disabled controls, successful artifact tests, exact CI, configuration, schema/formula, Git tree, and tracked-source authority;
- the scanner's owner-only identity matches the release and is recent, and its database heartbeat is `healthy` or safely `blocked`, recent, and bound to the same run and commit;
- the listener's owner-only identity matches the release and has a live PID, its `healthy` poll heartbeat is recent and bound to the same run and commit, and its active lock binds the same PID, release root, and commit;
- the production SQLite database opens read-only and passes a bounded `PRAGMA quick_check(1)`.

Missing, malformed, stale, future-dated, permissively readable, symlinked, wrong-root, wrong-commit, wrong-run, dead-process, or mismatched-lock evidence returns nonzero. A market-closed scanner cycle may report `blocked` and still count as fresh; a failed or unknown cycle does not. `/status` remains a useful application health view, but it is not a substitute for this deployment evidence gate.

### Restarting runtime jobs

If either process is stale, stop the launchd jobs and follow the controlled
immutable-release install/start procedure. A listener-only restart may use
`"$HOME/TradingAgentRuntime/scripts/restart_telegram_listener.sh"` only when the
same exact listener is already launchd-owned. That helper verifies the active
release, installed plist and remote forward/rollback authority, uses only
`launchctl bootout/bootstrap`, refuses PID signalling or lock deletion, and
finishes with the complete freshness gate. Then wait for both new heartbeats
and retain the complete freshness report. Do not use a source checkout's Git
HEAD as runtime authority; the active immutable release manifest and runtime
pointer are authoritative.

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

The system uses the canonical, versioned policy in `config/config.yaml` when `position_sizing.mode: risk_portfolio` is enabled. Operational size comes from validated stop risk, Phase 3 hard limits, Phase 4 strategy sleeves, Adaptive Conviction, and Adaptive Sizing. A missing validated ATR/technical stop, malformed pending buy exposure, incomplete MA50/MA200 evidence, missing risk unit, or stale conversion equity blocks the affected entry/add.

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
5.  **Strategy Sleeve Cap**: Phase 4 allocates canonical stop-risk dollars and notional to an authorized strategy sleeve. A candidate cannot borrow another sleeve's capacity.

### 4. Small-account and blocking rules

To facilitate testing on small accounts:
*   `$5.00` is an executable minimum, not a clamp. A constrained result below it is blocked.
*   Every normal order uses a fresh quote and a bounded limit price.
*   All ordinary entries/adds require manual Telegram approval and final revalidation.

The configured Alpaca equity quote feed is explicit and is part of proposal
authority. A proposal is never displayed unless that feed returns a fresh,
non-crossed, two-sided quote within the configured spread bound. If Telegram reports
an IEX spread failure, do not retry the old approval, clear an exit blocker, or relax
the threshold. IEX is a single-exchange feed; wait for a new spread-valid quote and a
new proposal, or provision SIP entitlement and roll out a separately reviewed feed
configuration. The audit trail records feed, bid, ask, timestamp, age, spread, limit,
and the zero-invocation outcome.

See [CONFIGURATION_AND_SIZING.md](CONFIGURATION_AND_SIZING.md) for the formula and schema version contract.

## Approval, exit, and recovery workflow

Every order requires a current manual approval. `YES ALL` means “validate every candidate independently,” not “execute the batch blindly.” A submitted BUY supersedes only an equivalent or mutually exclusive proposal; unrelated BUYs remain pending until their own approval-time account, position, order, reservation, Phase 3, sleeve, exposure, registry, expiry, quote, and exit-first checks complete.

SELL validation is directional. A lower refreshed price does not by itself block an ordinary, partial-profit, trailing-stop, profit-protection, time-stop, emergency, or rotation exit. The system still requires an open market, fresh quote, a current held long position, quantity no greater than holdings, no conflicting SELL, an unexpired proposal, and a bounded paper SELL mechanism. Manual approval and no-shorting remain mandatory.

Take-profit levels are fill ledgers, not proposal flags. Partial fills retain only their cumulative quantity/fraction; cancellation retains that actual progress. Duplicate or restart reconciliation cannot double-count a broker fill event. Unknown submission outcomes keep their reservation and enter reconciliation; operators must never approve a blind retry.

## Release verification, migration, and rollback

Run `python scripts/check_release_eligibility.py` from a clean checkout. The command reports the exact commit, configuration hash, required schema versions, additive-migration compatibility/idempotence, paper-only identity, local compile/test result, and exact-commit GitHub CI result. `unverified`, `pending`, or failed remote CI is not eligible.

Before deployment, back up the production-paper SQLite database and run the migration proof on a clone. Migrations are additive and must be applied by the explicit deployment workflow; ordinary runtime initialization is not a production migration mechanism. The production migration command must run from the exact immutable release environment, observe both launchd writers stopped, create a new exclusive backup only after its disk-capacity gate, and produce matching first/second-pass schema, row-count, version, and page-count evidence. Verify the immutable release manifest, paper account identity, scanner/listener commit freshness, durable-integrity report, and migration ledger before cutover.

Rollback switches scanner and listener together to the prior immutable release whose configuration and schema are compatible. Additive tables may remain. If an exact database rollback is required, stop both writers and restore the verified pre-migration backup. Never migrate production, restart processes, or move the runtime pointer as part of code review.
