# Safety rules

- Paper mode is the only supported v1 operating mode. Three independent config values guard live initialization.
- AI is explanatory and has no broker tool. ML is advisory and its order method always raises.
- Deterministic system, market-data, portfolio, signal, execution, and approval gates run before execution and again immediately before submission.
- Unknown or ambiguous state blocks. Orders are never retried after an uncertain transport failure.
- A proposal must be single, pending, unexpired, authorized, matching, unused, and finally revalidated.
- Create `config/KILL_SWITCH` or run `scripts/stop_agent.sh` to block activity.
- Cash-out is a recommendation only; API withdrawals always raise.
- Secrets stay in Keychain or `.env`, never source, Git, logs, AI prompts, or reports.
- This software is educational infrastructure, not financial advice.
