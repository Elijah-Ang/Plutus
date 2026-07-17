# Profitability validation and realized attribution

This document defines the evidence authority used by operational paper-strategy
policy. It does not enable live trading, autonomous execution, larger risk, or
crypto execution. A strategy may progress only when its current evidence passes
this authority and all existing portfolio, approval, execution, and paper-only
controls still pass.

## Admissible evidence

Only canonical `shadow_oos` and `actual_paper` strategy-trade records may enter
the operational validation family. Records must use the current evidence and
FIFO-accounting versions, have a completed outcome interval, and contain a
finite net R-multiple. Counterfactual, future, incomplete, mixed-version, and
unavailable-attribution records are excluded. Actual-paper rows are generated
from closed FIFO position lifecycles; they are never inferred from current
positions or broker account totals.

Every validation run predeclares the complete set of known operational strategy
versions. Weak or insufficient members remain in the family with p-value 1;
they cannot be dropped after results are observed. The immutable family binds
its hypotheses, observations, policy, configuration hash, evidence/formula
versions, decisions, folds, timestamps, and fingerprints. Policy and candidate
economics reload and recompute this authority before use.

## Chronological validation

The validator uses expanding walk-forward folds. Identical observation times
remain in one indivisible group. Before each test window it:

1. leaves the configured number of observation-time groups as an embargo;
2. removes every training label whose outcome end is at or after the test start;
3. requires the configured minimum purged training population; and
4. records exact train/test IDs, raw and purged counts, boundaries, and a fold
   fingerprint.

This prevents overlapping outcome labels from leaking test-period information
into training. The design follows the general data-snooping concern formalized
by White's [Reality Check](https://onlinelibrary.wiley.com/doi/abs/10.1111/1468-0262.00152).

Dependence-aware uncertainty uses a deterministic circular moving-block
bootstrap. The stored result includes a two-sided 95% percentile interval and a
one-sided centered-null p-value. A strategy needs a strictly positive lower
bound and the configured fraction of positive chronological test folds.

The complete predeclared family is corrected with the Benjamini-Hochberg
step-up procedure and monotone q-values, based on the original
[false-discovery-rate method](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x).
The operational default FDR is 10%. This controls expected false discoveries
across the tested family; it is not a guarantee that any individual result is
true.

Related parameter variants share a stability group. A group passes only when
the configured fraction of variants has positive mean and positive bootstrap
lower bound. A singleton explicitly means that no parameter search was
performed for that operational version; it is recorded as not applicable and
must not be described as a sensitivity result.

## Decision states and policy binding

- `validated`: sample, fold, lower-bound, FDR, positive-fold, and parameter-
  stability gates all pass.
- `failed`: mature evidence exists but one or more profitability gates fail.
- `insufficient`: the predeclared member lacks the required sample or folds.

A failed strategy is `SUSPENDED`. A mature strategy without `validated`
authority is also `SUSPENDED`. Insufficient authority is allowed only for the
existing tightly bounded `RESEARCH_ONLY` or `PROBE` states. The policy snapshot
and policy decision must contain the same family ID, decision ID, status, and
decision fingerprint. Candidate economics additionally requires the complete
current family, matching config/formula identities, and a validation state
compatible with the strategy policy.

## Expected-versus-realized attribution

Candidate economics is immutable before proposal use. When an actual paper
position lifecycle closes, FIFO lots and lot consumptions are projected into an
immutable attribution record. For each consumed quantity `q`:

```text
realized gross P&L = sum q * (actual exit - actual entry)
realized fee drag = sum allocated buy fees + allocated sell fees
realized adjustments = sum signed allocated accounting adjustments
realized net P&L = realized gross P&L - realized fee drag + realized adjustments

reference market P&L = sum q * (final exit bid - expected entry)
combined entry drag = sum q * (actual entry - expected entry)
exit execution drag = sum q * (final exit bid - actual exit)

realized net P&L
  = reference market P&L
  - combined entry drag
  - exit execution drag
  - realized fee drag
  + realized adjustments
```

When entry final ask is available, combined entry drag is split into approval-
delay price drag and fill slippage. Expected economics is scaled by consumed
quantity, so partial fills and multi-lot exits do not duplicate the original
proposal expectation. A weighted approval delay is reported only when every
attributed quantity has delay evidence; partial coverage is recorded explicitly
and the aggregate remains unavailable.

The variance reconciliation is also exact:

```text
expected net = expected gross - expected execution
               - expected holding/opportunity - expected uncertainty
expected-vs-realized variance = realized net - expected net
market variance = reference market P&L - expected gross
execution-cost variance = observed execution drag - expected execution
noncash reserve release = expected holding/opportunity + expected uncertainty

expected-vs-realized variance
  = market variance - execution-cost variance + noncash reserve release
    + realized adjustments
```

Both reconciliation residuals must be exactly zero under Decimal arithmetic.
If compatible expected economics is absent but FIFO actual P&L is complete, the
record is explicitly `partial` with `verified_actual_only` confidence; expected
fields remain null. Structural accounting gaps are `unavailable` and do not
enter strategy evidence. Persistence and verified reload run in a coherent
SQLite transaction and independently bind every usable leg to the exact closed
lifecycle, FIFO lot consumption, entry/exit intent quote, consumed approval,
and immutable historical trade-economics authority. Caller-computed arithmetic
without those durable rows is rejected.

## Known limits

This implementation does not claim Deflated Sharpe Ratio or Probability of
Backtest Overfitting results. Those require an honest record of multiple tried
configurations and, for PBO, a suitable cross-validation result matrix. The
relevant methods are described in Bailey and López de Prado's
[Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
and Bailey et al.'s
[Probability of Backtest Overfitting](https://escholarship.org/uc/item/4w1110bb).
The system records these conclusions as unavailable rather than inventing
trial counts or independence assumptions.

Validation also cannot repair missing point-in-time historical data. Until a
strategy accumulates sufficient current-version OOS/actual evidence, the
result remains insufficient and the existing conservative state ceiling binds.

## Audit surfaces

SQLite stores validation families, decisions, folds, trade economics, strategy
trade records, scorecards, policies, and profit attribution separately. The
Excel export exposes each table on a dedicated sheet. The read-only integrity
report checks orphaned/incomplete validation authority, policy/snapshot linkage,
orphaned attribution, reconciliation residuals, strategy-trade linkage, and
counterfactual contamination.
