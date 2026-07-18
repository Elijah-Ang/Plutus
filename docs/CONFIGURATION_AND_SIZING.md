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
- `adaptive_minimum_ceiling_notional_v4_operational_paper`: shared canonical
  ceilings consumed by operational Adaptive Sizing.
- `risk_engine_adaptive_ceiling_stop_probe_v4`: final hard validation.
- `phase3_adaptive_modes_v4_operational_paper` and
  `phase4_multi_strategy_allocation_v7_stop_risk_dollars`: operational mode and
  evidence-aware allocation boundaries.
- `fifo_equity_unrealized_cashflow_v2_decimal`: canonical Decimal FIFO P&L, account-equity
  change, unrealized change, and external cash-flow separation.
- `phase1_outcome_v2_exit_session`: corrected early-exit attribution.
- `profitability_validation_v1`: immutable purged walk-forward folds, circular
  block-bootstrap uncertainty, complete-family Benjamini-Hochberg FDR, and
  parameter-neighborhood stability.
- `profit_attribution_v1`: exact Decimal expected-versus-realized FIFO lifecycle
  reconciliation without invented expected values.
- `candidate_trade_economics_v1` and `candidate_profitability_ranking_v1`:
  immutable candidate economics and conservative ranking inputs bound to exact
  strategy policy and validation authority.

## Effective paper sizing

The final executable notional is the minimum of Adaptive Conviction stop risk,
mode heat/gross capacity, validated canonical risk/exposure/cash/buying-power
ceilings, durable reservations and every Phase 3/PROBE limit. Every value is a
ceiling; no constrained result is raised to meet an executable minimum. The
legacy fixed-dollar stage values do not participate in the operational-paper ceiling path.

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

Phase 4 records adaptive allocation, bounded `PROBE`/`EXPLORATION`, strategy sleeves,
and unallocated risk separately. Every risk value carries `risk_value` and `risk_unit`;
`pct_equity` is converted to canonical `stop_risk_dollars` using fresh authoritative
equity and a persisted formula version. Kelly is a ceiling diagnostic; covariance/overlap is an operational
constraint. Actual before/after heat, gross/symbol/cluster exposure, pending
risk, reserved risk, binding caps, evidence versions, formula version, and
configuration hash are persisted.

Operational profitability validation is configured in
`profitability_validation`. The current minimums are 50 observations, two
purged chronological folds, 30 training observations, 10 test observations,
one embargo group, five-observation circular blocks, 2,000 bootstrap draws, a
10% family FDR, 60% positive folds, and 60% parameter-neighborhood support.
These are evidence gates, not sizing multipliers. Insufficient evidence cannot
be converted into ACTIVE/THROTTLED authority by a caller hint.

See [PROFITABILITY_VALIDATION_AND_ATTRIBUTION.md](PROFITABILITY_VALIDATION_AND_ATTRIBUTION.md)
for exact evidence admission, formulas, state transitions, attribution
reconciliation, and known statistical limits.

## Schema and execution compatibility

Runtime startup requires the complete migration ledger and all mandatory columns,
including the P1 execution-safety schema, before opening the production runtime.
Deployment-only `Storage.apply_explicit_migrations()` applies additive
migrations; ordinary runtime initialization cannot satisfy the gate by itself.
Execution remains paper-only, durable-intent and reservation backed, bounded by
fresh quote-derived limits, manually approved through Telegram, reconciled by
broker/client order ID, and protected by narrowly gated paper exits. Shadow and
research lanes cannot create operational proposals or orders.
