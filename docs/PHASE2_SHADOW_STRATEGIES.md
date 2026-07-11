# Phase 2 shadow strategies

Phase 2 deploys five independent daily-bar research sleeves in `SHADOW_ONLY` mode:
cross-sectional momentum, time-series trend, pullback in an established uptrend,
ETF sector rotation, and breakout continuation. Each sleeve has an explicit `v1`
strategy identifier and emits the same frozen `ShadowInsight` contract.

## Structural boundary

`ShadowStrategyEngine` receives only storage, a run identifier, point-in-time bar
snapshots already fetched by the production scanner, and an observation timestamp.
It has no broker, Telegram, risk-engine, approval, proposal, reservation, intent, or
execution dependency. Its writer is limited to `shadow_*`, `research_opportunities`,
and `research_outcomes`. Database triggers reject updates or deletes of persisted
insights. Configuration repeats the boundary with all execution surfaces disabled.

Phase 2 never creates proposals or approval messages. It never sizes or reserves
risk, creates order intents, or calls a broker. The existing rule-based strategy and
its proposal/execution path remain unchanged.

## Point-in-time and Phase 1 integration

Features use only the daily-bar prefix available at `observed_at`. Each insight
stores the input fingerprint, feature and universe versions, regime and regime
version, provider provenance, and adjustment-data limitation. Active insights are
inserted into Phase 1 `research_opportunities`; 1/5/20-session rows use the canonical
`phase1_outcome_v1` pipeline and conservative cost model. The existing bounded
outcome updater matures shadow outcomes alongside legacy outcomes.

Active insights also create equal-weight comparative sleeve portfolio observations
and same-symbol sleeve-overlap records. Promotion assessments are initialized as
`NOT_ELIGIBLE`; no automated or manual promotion path exists in Phase 2.

## Deployment and rollback

The required schema is `phase2_shadow_strategies_v1`. Migration is additive and
idempotent and includes the Phase 1 research schema. Production migration must use
the explicit deployment command, which creates and verifies a point-in-time SQLite
backup before any schema write. Code rollback is compatible with the additive
tables; exact database rollback requires stopped writers and restoration of the
verified pre-migration backup.
