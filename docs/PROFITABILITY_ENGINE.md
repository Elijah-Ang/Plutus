# Plutus Profitability Engine

## Candidate trade-economics foundation

`app.trade_economics` is the candidate-level economic boundary between a
versioned strategy-performance decision and any future proposal-ranking or
allocation consumer. It does not submit orders, grant execution authority, or
change the existing paper/manual-only controls.

Every record is immutable and binds:

- the candidate, strategy, setup, asset class, and regime identities;
- the exact strategy-performance snapshot and policy decision;
- current configuration, evidence, formula, cost-model, and estimation-model
  versions;
- quantity, notional, entry estimate, limit, stop, target, and maximum approved
  loss;
- expected and conservative win probabilities;
- expected average win and loss;
- spread, slippage, fees, regulatory cost, crypto transaction cost, market
  impact, implementation shortfall, adverse selection, missed-fill cost,
  opportunity cost, approval-delay cost, holding cost, model uncertainty, and
  estimation uncertainty;
- complete derived economics and a tamper-evident fingerprint.

Money, price, quantity, probability, and ratio values use `Decimal`. Binary
floating-point inputs are rejected at the subsystem boundary.

## Version 1 formulas

For expected average win \(W\), expected average loss \(L\), expected win
probability \(p\), conservative win probability \(p_c\), and complete expected
cost \(C\):

```text
expected gross profit       = p × W - (1 - p) × L
expected net profit         = expected gross profit - C
conservative net profit     = p_c × W - (1 - p_c) × L - C
after-cost break-even p     = (L + C) / (W + L)
expected net R              = expected net profit / displayed stop risk
capital efficiency          = expected net profit / proposed notional
R per day                   = expected net R / expected holding days
cost-to-gross-edge ratio    = C / expected gross profit
```

The worst-reasonable loss uses the displayed BUY limit rather than the entry
estimate, then adds complete expected costs and an explicit stress-cost
increment. It must not exceed the maximum approved loss.

An economically valid record remains ineligible when its expected or
uncertainty-adjusted net edge is nonpositive, its after-cost break-even
probability is too high, costs consume too much gross edge, or its marginal
portfolio contribution is below policy. Hard risk controls remain downstream
and authoritative.

## Evidence and leakage boundary

The durable store verifies that the performance snapshot and strategy policy
match the candidate's strategy, state, configuration, evidence, and formula
identities. Snapshot and policy timestamps must not be later than the candidate
estimate, preventing future strategy evidence from being attached to an earlier
candidate.

Proposal-linked records additionally bind the exact proposal candidate through
the economics input fingerprint. A changed proposal, formula identity, strategy
authority, or candidate identity cannot reuse the record.

## Persistence

`candidate_trade_economics_schema_v1` adds:

- `trade_economics_records`, containing queryable canonical inputs, costs,
  outputs, authority IDs, and fingerprints;
- `trade_proposals.trade_economics_id`, an optional immutable proposal link.

Migration is additive and idempotent. Ordinary runtime initialization does not
record the production migration; deployment must continue using the explicit
migration path.

This foundation intentionally stops before operational ranking, Telegram
display, or execution enforcement. Those consumers must be added in reviewed
follow-up changes and must load a verified record rather than recalculate from
untrusted proposal payloads.
