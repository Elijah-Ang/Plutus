# Safety rules

- Paper mode is the only supported v1 operating mode. Three independent config values guard live initialization.
- AI is explanatory and has no broker tool. ML is advisory and its order method always raises.
- Deterministic system, market-data, portfolio, signal, execution, and approval gates run before execution and again immediately before submission.
- Unknown or ambiguous state blocks. Orders are never retried after an uncertain transport failure.
- Atomic reservations and idempotent order intents are authoritative across restart and reconciliation.
- Final BUY quantity, notional, and stop risk may only stay the same, reduce, or block. Shorting, margin expansion, and averaging down are prohibited.
- Take-profit milestones advance from unique linked fills only; proposal or terminal order status with zero fill cannot consume a level.
- Risk-reducing SELLs use directional movement validation. Further downside does not by itself block the exit, but freshness, holdings, conflict, expiry, market-open, bounded-price, and manual-approval gates still apply.
- Initial R, winner expansion, and R-based management require current position-lifecycle fill provenance and never use symbol-only historical proposals.
- Phase 4 risk values always carry `risk_value` and `risk_unit` and normalize to stop-risk dollars with fresh authoritative equity.
- A proposal must be single, pending, unexpired, authorized, matching, unused, and finally revalidated.
- Create `config/KILL_SWITCH` or run `scripts/stop_agent.sh` to block activity.
- Cash-out is a recommendation only; API withdrawals always raise.
- Secrets stay in Keychain or `.env`, never source, Git, logs, AI prompts, or reports.
- This software is educational infrastructure, not financial advice.
