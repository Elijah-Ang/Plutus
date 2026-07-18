# Performance Lab Lifecycle Classification

## Accounting boundary

Performance Lab opportunity evidence is descriptive. A proposal is not a
trade, an approval is not a submission, and a submission is not a fill. The
only class admitted to the actual-trade population is `actual_fill`, backed by
a durable `fills` row and positive entry quantity and price evidence.

Nonactual populations remain distinct:

- `shadow`;
- `proposal_unfilled` and `approved_unfilled`;
- `blocked_unfilled` and `approved_blocked`;
- `rejected_unfilled`, `expired_unfilled`, and `superseded_unfilled`;
- `intent_unsubmitted`;
- `submitted_unfilled` and `submitted_cancelled_unfilled`;
- `ambiguous_submission`;
- `filled_missing_fill_evidence`, `invalid_fill_evidence`, and
  `unclassified_unfilled`.

None of these labels creates proposal, approval, intent, reservation, or broker
authority. In particular, ambiguous submission remains nonactual until durable
fill reconciliation proves otherwise.

## Migration and integrity

The additive `performance_lab_fill_bound_classification_v1` migration
reclassifies legacy proposal-linked Performance Lab rows from durable proposal,
authorized-approval, order, and fill evidence. It recomputes actual-trade
summary counts and is idempotent: already-correct rows retain their timestamps.

Integrity checks reject both directions of accounting drift:

- an `actual` or `actual_fill` outcome without matching fill evidence; and
- a fill-linked outcome not classified as `actual_fill`;
- invalid nonpositive fill evidence; and
- a setup that claims it was proposed but has no matching durable proposal.

Blocked shadow rows may retain a candidate identifier in `proposal_id` for
research linkage. They are not migrated into proposal lifecycle classes unless
that identifier resolves to a durable `trade_proposals` row.

The `Performance Lifecycle` report groups populations by asset class and
evidence class, preventing equity, ETF, and crypto opportunities—or proposed
and executed opportunities—from silently collapsing into one result set.

## Remaining Performance Lab work

This checkpoint repairs lifecycle population integrity. Full portfolio-level
accounting analytics, continuous-session crypto outcome measurement, benchmark
and risk-adjusted governance metrics, and profit-retention recommendations
remain separate reviewed changes. No strategy code is rewritten automatically.
