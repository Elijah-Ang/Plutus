# Phase 4 evidence-aware operational-paper allocation

Phase 4 consumes completed operational out-of-sample or actual paper evidence.
It persists shrinkage toward zero, conservative expected return, uncertainty,
data quality, current-regime performance, deterioration, execution quality,
covariance, marginal risk, overlap/correlation and stress results.

For authorised mature candidates, deterministic ranking is operational:

```text
conviction = mean(setup quality, evidence quality, regime alignment, execution quality)
rank = 100 × (0.30 × conviction
            + 0.20 × conservative expected-value score
            + 0.20 × diversification capacity
            + 0.15 × execution quality
            + 0.15 × risk efficiency)
       - 15 × uncertainty
       - 30 × deterioration
```

All inputs are clamped to bounded unit scores. Symbol/cluster overlap lowers
diversification; strong low-overlap opportunities rank higher. The strategy
optimizer uses conservative expected return divided by shrunk covariance risk,
then applies evidence, uncertainty, deterioration, overlap, regime, drawdown,
stress and concentration penalties before normalising within Phase 3 limits.

Fractional Kelly (maximum 0.25; configured 0.20) is only a ceiling diagnostic.
It never directly produces an order quantity, and unreliable Kelly inputs
cannot expand risk. Full Kelly is forbidden.

Operational policy states remain distinct: RESEARCH_ONLY and SUSPENDED receive
zero entry risk; PROBE receives 0.03% per trade with 0.10% aggregate heat,
2.5% gross, one active/reserved probe and no ADD; EXPLORATION receives bounded
immature allocation; THROTTLED is reduced; ACTIVE may receive normal or
expanded evidence-aware allocation. The explicit execution registry currently
covers rule-based, pullback, breakout, trend, momentum, and ETF-rotation
strategies. Each must independently pass implementation, evidence, policy,
paper-eligibility, and human/config authorization gates before receiving a sleeve.

Schema: `phase4_multi_strategy_operational_paper_v5_units`. Allocator:
`multi_strategy_paper_allocator_v6_explicit_units`. Formula:
`phase4_multi_strategy_allocation_v7_stop_risk_dollars`.

## Persisted execution authority

An executable allocation is not authorized merely because a row says
`ALLOCATE` or contains a positive sleeve. The allocator first reloads and
deterministically verifies the exact persisted strategy-registry snapshot for
the same run. Its payload then carries
`phase4_allocation_authority_v1_exact_registry`, the exact replayed registry
evaluation under `strategy_execution_registry_formula_v2_full_policy`, the
registry snapshot ID, complete policy-decision and performance-snapshot
identities, quality/maturity/metrics evidence, ordered strategy family,
authorized strategy order, evidence-version inventory, sleeve limits,
configuration hash, and formula identities.

At intent creation and rotation approval, the system reloads both persisted
records and recomputes every registry fingerprint, decision identity,
allocation replay fingerprint, and allocation ID inside the authoritative
transaction. The allocation must reference the same run, registry snapshot,
strategy, configuration, and exact registry evaluation. Regenerating a local
evidence fingerprint is insufficient: a separate authority fingerprint binds
the complete allocation payload and is itself part of the allocation ID.
Altered policy, registry, sleeve, or payload evidence therefore cannot retain
the displayed allocation identity. Missing, stale, malformed, or cross-run
authority fails before an intent or reservation is inserted.

Historical allocation rows created before the explicit exact-registry authority
version remain available as audit evidence. They are never executable. This
compatibility does not grant authority: every execution consumer requires the
current authority version and an exact verified registry binding.

Candidate, sleeve, global-budget, allocation, reservation, and reconciliation
risk values carry `risk_value` and `risk_unit`. Supported source units are
`stop_risk_dollars` and `pct_equity`; every operational calculation normalizes
to stop-risk dollars using current authoritative equity, its timestamp, and the
persisted conversion formula version. Missing/unknown units, stale equity,
non-finite/negative values, and zero requested candidate risk fail closed.

`max_strategy_weight`, the conservative `0.175` ACTIVE fallback ceiling,
`max_allocated_risk_fraction`, and `max_stress_loss` are portfolio allocation
fractions. The selected strategy weight is divided by `max_strategy_weight`
before it becomes a unitless `0..1` strategy-risk multiplier. It is never read
as a stop-risk percentage; only Adaptive Conviction can expand the resulting
base, and Phase 3 still caps that expansion at `0.35%` of equity.
