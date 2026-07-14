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
expanded evidence-aware allocation. Only `rule_based_v2` is executable today;
the architecture does not fabricate multi-strategy diversification.

Schema: `phase4_evidence_aware_operational_paper_v3`. Allocator:
`adaptive_paper_allocator_v3_evidence_aware`. Formula:
`phase4_evidence_aware_allocation_v4_operational_paper`.
