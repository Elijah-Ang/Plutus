# Phase 3 adaptive operational-paper risk

Phase 3 has two explicit layers. The hard envelope is immutable: paper-only,
manual approval, no margin or shorting, 0.35% maximum stop risk per trade,
1.75% portfolio heat, 50% gross exposure, 6% per symbol, 15% per cluster,
authoritative cash/buying power, fresh liquid quotes, valid stop geometry,
daily and weekly loss halts, drawdown scaling (75% at 2%, 50% at 4%, halt at
6%), reservation-adjusted capacity, and final revalidation.

Inside that envelope, `AdaptiveConvictionEngine` selects the operational mode:

| Mode | Trade risk | Heat target | Gross target | Position target |
|---|---:|---:|---:|---:|
| DEFENSIVE | 0.15% | 0.50% | 20% | 2 |
| NORMAL | 0.20% | 1.25% | 30% | 3 |
| OPPORTUNISTIC | 0.30% | 1.50% | 40% | 4 |
| AGGRESSIVE | 0.35% | 1.75% | 50% | 5 |

Position counts are operating targets, not arbitrary hard rejection gates.
Expansion requires complete calibrated evidence, favourable regime, healthy
account/loss state, healthy reconciliation, strong execution and available
diversification capacity. Missing optional expansion evidence falls back to
NORMAL or lower; missing critical account, quote, stop, exposure, reservation,
or integrity state fails closed. A favourable Phase 3 regime multiplier is
1.15, subject to the hard envelope.

The executable `rule_based_v2` strategy receives the authorised executable
risk sleeve. Research strategies retain independent state/evidence but cannot
be allocated executable risk. Durable intents, atomic reservations,
reconciliation, recovery and Executor remain the sole route to a paper order.

Schema: `phase3_adaptive_operational_paper_risk_v2`. Formula:
`phase3_adaptive_modes_v4_operational_paper`.
