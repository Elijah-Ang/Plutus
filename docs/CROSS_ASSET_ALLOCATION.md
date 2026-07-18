# Cross-Asset Allocation

## Scope and authority

The cross-asset allocator compares equity, ETF, and crypto profitability evidence on one normalized basis. Its current mode is `research_advisory`. A plan is immutable and independently reproducible, but it is never proposal, approval, intent, reservation, or broker authority.

The crypto lane remains `research_only`. A crypto candidate can receive hypothetical research allocation, but the allocator cannot make it approvable or executable. Enabling supervised crypto proposals or paper execution requires separate evidence, configuration, display-authority, approval, intent, recovery, and exact-head review work.

## Ranking model

Nominal expected profit is not a ranking shortcut. Every candidate supplies exact-decimal evidence for:

- expected and conservative net R;
- expected net profit and capital efficiency;
- expected R per holding day;
- marginal portfolio contribution;
- positive-return and severe-loss probabilities;
- uncertainty and execution-cost burden;
- liquidity, correlation, and marginal drawdown.

The allocator independently verifies the identities:

```text
expected net R       = expected net profit / modeled economic downside
capital efficiency   = expected net profit / proposed capital
expected R per day   = expected net R / expected holding days
```

Modeled economic downside and the cost-inclusive maximum-loss/stop-risk amount are separate fields. Expected R uses the former; every heat and trade-risk ceiling uses the latter, which may never be smaller. It also requires conservative net R not to exceed expected net R, marginal contribution not to exceed conservative net R, and severe-loss probability not to exceed the total nonpositive-return probability. All score components and weights are displayed in the durable plan. Inputs must be `Decimal`, integers, or decimal strings; binary floats and non-finite values fail closed.

## Portfolio constraints

The greedy allocation order is deterministic: descending normalized score, then stable candidate identity. Each candidate can receive no more than the tightest remaining capacity. The policy preserves the existing hard controls:

- 50% total gross and equity exposure;
- 1.75% total stop heat;
- 6% per-symbol exposure;
- 15% correlation-cluster exposure;
- 1% crypto gross exposure;
- 0.05% crypto stop heat;
- 0.01% crypto stop risk per research trade;
- 0.35% equity stop risk per trade;
- 0.10% exploration-strategy stop heat;
- 0.6125% active-strategy stop heat;
- 0.45 equity/ETF and 1.50 crypto annualized-volatility ceilings;
- 20% cash reserve and cash-funded buying-power limit;
- three equity, two crypto, and five total positions;
- daily, weekly, and drawdown halts;
- drawdown throttling before the halt;
- fresh loss evidence and healthy kill-switch, database, internet, power, and broker controls.

The authoritative portfolio snapshot must reconcile position counts, gross exposure by symbol/cluster/asset class, and stop heat by asset class/strategy. Missing or inconsistent totals fail closed. Candidate run identity, canonical symbol identity, held-position status, and entry-versus-add action must agree with that snapshot. The audited maxima are also enforced in code, so a different but internally self-consistent config hash cannot widen the paper/research limits.

## Durability and verification

`cross_asset_allocation_plans` binds:

- run, capture, and expiry identities;
- paper-account and portfolio-snapshot identities;
- the complete canonical candidate set;
- the complete policy and configuration hash;
- formula and schema versions;
- every ranking component, allocation, constraint, and blocker;
- the final plan fingerprint;
- `execution_authorized=0`.

Normal application access cannot update or delete a plan; SQLite triggers enforce immutability. Loading a plan recomputes every fingerprint and the complete allocation result. If an administrator bypasses the trigger, changing plan JSON and merely regenerating a local fingerprint still fails because the plan ID remains bound to the original fingerprint and independent recomputation must match. As with every local SQLite record, an administrator who can replace the schema and every bound input is outside this local tamper-evidence boundary; the plan therefore remains advisory and never order authority.

A plan expires at the earliest of its own TTL, the portfolio snapshot's TTL, and every candidate evidence TTL. Near-expiry source evidence therefore cannot be repackaged into a fresh full-duration plan.

Portfolio diagnostics include expected net return on equity, probability-weighted tail stop risk, expected marginal drawdown dollars, liquidity utilization, asset-class concentration, and a conservative annualized-volatility upper bound. The volatility bound uses the current portfolio volatility, absolute candidate-to-portfolio correlations, and perfect correlation among newly allocated candidates; it is intentionally conservative rather than an unstable unconstrained covariance estimate.

Generated reports include a `Cross Asset Allocation` sheet. Durable-execution integrity checks reject malformed plans or any attempt to set execution authority.

## Current limitation and next authority step

The engine is ready to compare complete profitability records across assets, but the current crypto strategies do not yet have validated operational profitability evidence. That absence is intentional: raw target reward or setup score is not substituted for expected net value. The next integration step must construct exact crypto profitability evidence from point-in-time outcomes and cost models before any crypto candidate can be considered beyond research-only allocation.
