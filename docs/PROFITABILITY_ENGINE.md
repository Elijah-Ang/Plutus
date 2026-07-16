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

## Conservative edge estimation

`strategy_performance_v2_3_gross_edge` separates gross and net evidence so
candidate costs are not counted twice. The immutable strategy snapshot now
includes:

- gross sample, win, loss, and flat counts;
- gross expectancy, win rate, average win R, and average loss R;
- paired gross/net cost drag;
- average and median observed holding periods.

Candidate win probability uses a Beta posterior with a neutral 50% prior:

```text
alpha                = observed wins + prior samples × 0.50
beta                 = observed non-wins + prior samples × 0.50
posterior win p      = alpha / (alpha + beta)
conservative win p   = max(0, posterior p - 1.96 × sqrt(posterior variance))
```

Average gross win and loss R are independently shrunk toward 1R priors. The
conservative probability, not the point estimate, controls candidate
eligibility. A positive point estimate with a nonpositive conservative
after-cost estimate is rejected.

For the binary positive-outcome posterior, flat gross outcomes count as
non-wins. They cannot silently disappear from the sample denominator and
inflate the estimated hit rate.

## Complete equity cost model

The candidate estimator uses the current best bid and ask, historical
implementation-shortfall evidence when available, and versioned conservative
assumptions for slippage, market impact, adverse selection, missed fills,
approval delay, holding opportunity cost, and model/estimation uncertainty.

The regulatory schedule is pinned as
`alpaca_equity_cost_model_2026_07_01_v1`:

- Section 31: $20.60 per $1,000,000 of covered equity sales;
- FINRA TAF: $0.000195 per sold share, capped at $9.79;
- FINRA CAT: $0.000003 per executed equivalent share on each side.

Section 31 is calculated from the displayed target sale notional, not the lower
entry notional, so the recorded complete-trade cost does not understate the fee
when the profitable outcome is realised.

Primary references:

- [SEC FY2026 Section 31 fee advisory](https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2)
- [FINRA 2026 Trading Activity Fee schedule](https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule)
- [Alpaca brokerage fee schedule](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf)
- [Alpaca latest quote definition](https://docs.alpaca.markets/us/v1.4.2/reference/stocklatestquotesingle-1)

Changing the schedule or any candidate-model assumption requires a new
configuration and cost-model version.

## Profitability Quality Score and ranking

`candidate_profitability_ranking_v1` creates a score distinct from the setup
score. Its components and weights are persisted and displayed:

- conservative net expectancy: 25%;
- expected net expectancy: 15%;
- execution-cost resilience: 10%;
- holding-period efficiency: 10%;
- evidence maturity: 10%;
- strategy evidence quality: 10%;
- payoff asymmetry: 7%;
- regime breadth: 5%;
- drawdown resilience: 4%;
- portfolio diversification: 4%.

The score cannot override an economics rejection or any hard risk control.
Eligible BUY candidates sort deterministically by:

1. conservative expected net R;
2. expected net R;
3. expected R per day;
4. expected capital efficiency;
5. marginal portfolio contribution R;
6. Profitability Quality Score;
7. setup score;
8. symbol.

Nominal expected dollar profit is deliberately absent from the primary ordering
key. A larger trade therefore cannot outrank a better normalized edge merely
because more dollars were proposed.

Marginal portfolio contribution starts from conservative after-cost R and then
applies the persisted symbol/cluster diversification factor. Gross edge is not
reintroduced at the portfolio-ranking boundary. The exposure inputs are
projected exposures after adding the candidate, not merely current holdings.

## Operational boundary

`candidate_profitability_decisions_v1` persists the exact score context,
components, rank key, configuration/formula identities, linked trade-economics
record, and tamper-evident fingerprint.

Operational ranking applies only when the complete Phase 3 profitability stack
is active. Each BUY is estimated before ranking and recomputed after risk and
adaptive sizing so the displayed economics use the final displayed quantity.
That quantity is rounded down to the supported eight-decimal equity precision;
its exact notional and stop risk may only stay equal or decrease. The proposal,
proposal-class economics record, and profitability decision are then committed
in one SQLite transaction. An authority mismatch rolls all three back. Missing
or stale quote/evidence authority rejects the BUY. SELL and protective exit
paths bypass candidate profitability completely.

These records grant no order authority. Proposal display, manual approval,
authoritative final risk validation, durable intent/reservation creation, and
pre-broker validation remain independent mandatory gates.
