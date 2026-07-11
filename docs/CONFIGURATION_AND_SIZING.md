# Configuration, sizing, and evidence contract

## Runtime source of truth

`config/config.yaml` is the documented and validated runtime configuration.
`config/strategies.yaml` is retained only as an archived compatibility note and
is not read by runtime code. There is no active `config/risk_limits.yaml` file;
risk, sizing, execution, and phase limits are validated from `config.yaml`.

Configuration is loaded through `app.configuration.load_config()` and validated
before a runtime object or database is opened. Strict mode rejects unknown
safety-critical keys, contradictory paper/live settings, invalid numeric units,
and incomplete canonical sizing sections. The effective normalized configuration
and SHA-256 hash are persisted in `config_snapshots` for every run.

## Versioned formulas

The active versions are defined in `app/formula_versions.py` and stored with
decisions and evidence:

- `validated_atr_or_technical_stop_v2`: executable entries/adds require a valid
  ATR or technical stop; percentage/fixed/default fallback stops are blocked.
- `canonical_minimum_ceiling_notional_v2`: one canonical notional policy and
  minimum-of-ceilings sizing.
- `risk_engine_ceiling_and_stop_v2`: final risk validation and ceiling checks.
- `phase3_risk_audit_v2` and `phase4_allocation_audit_v2`: operational audit
  records and binding caps.
- `fifo_equity_unrealized_cashflow_v1`: realized FIFO P&L, account-equity
  change, unrealized change, and external cash-flow separation.
- `phase1_outcome_v2_exit_session`: corrected early-exit attribution.

## Effective paper sizing

The final executable notional is the minimum of target, stage, validated stop
risk, equity, cash, buying power, symbol, cluster, portfolio, allocation,
exploration, optional absolute, and Phase 3/4 heat/gross ceilings. Every value
is a ceiling; no constrained result is raised to meet the executable minimum.
The temporary `$50` cap is removed. The configured moderate-paper defaults are
`$250` initial and `$100` add, subject to manual Telegram approval.

Pending buy exposure must contain a positive notional, reference price, stop
distance/risk, symbol, and cluster. A malformed or incomplete row makes risk
unknown and blocks every new entry/add until reconciled. Healthy-pullback adds
require valid MA50 and MA200 trend evidence.

## Evidence and allocation

Executable outcomes stop MFE, MAE, benchmark return, costs, and holding period
at the simulated exit session. Fixed-horizon observation/shadow outcomes keep
their full-horizon fields in a separate outcome class and never enter the
operational Phase 3/4 evidence population. Old outcome rows are invalidated and
recomputed under the current evidence version; mixed versions are excluded from
Phase 3/4 decisions.

Phase 4 records adaptive allocation, bounded exploration, and unallocated risk
separately. Kelly and covariance are diagnostics only; `operational_kelly_used`
remains false. Actual before/after heat, gross/symbol/cluster exposure, pending
risk, reserved risk, binding caps, evidence versions, formula version, and
configuration hash are persisted.

## Schema and execution compatibility

Runtime startup requires the complete migration ledger and all mandatory columns,
including the P1 execution-safety schema, before opening the production runtime.
Deployment-only `Storage.apply_explicit_migrations()` applies additive
migrations; ordinary runtime initialization cannot satisfy the gate by itself.
Execution remains paper-only, durable-intent and reservation backed, bounded by
fresh quote-derived limits, manually approved through Telegram, reconciled by
broker/client order ID, and protected by narrowly gated paper exits. Shadow and
research lanes cannot create operational proposals or orders.
