# Fixed-point accounting authority

Plutus records broker fill quantities and prices, FIFO lots, allocated fees,
cost basis, realized P&L, and account reconciliation components as normalized
decimal strings. These `*_decimal` columns are the accounting authority for
new records. Calculations parse the original source value with `Decimal` and do
not first convert it to a binary float.

Existing SQLite `REAL` columns remain compatibility projections for reports and
operational code that has not yet moved to decimal output. They do not override
canonical decimal evidence. Profit attribution and realized-P&L summaries read
the canonical value first. Integrity checks detect malformed or missing
canonical evidence, incompatible REAL projections, invalid FIFO geometry,
realized-P&L formula mismatch, and event-to-consumption reconciliation mismatch.

## Historical migration

The additive `fixed_point_fifo_accounting_schema_v1` migration never claims
that an old binary float was exact. It converts the stored display value to a
normalized decimal string and marks the row
`reconstructed_from_sqlite_real`. New source-preserving records are marked
`exact_source_decimal`. Legacy broker-fill fees and adjustments were not
persisted on the fill-event row, so the migration leaves those canonical fields
unavailable instead of inventing zero evidence.

The original REAL values are not rewritten. Applying the migration repeatedly
is idempotent.

## Arithmetic and compatibility

- Positive fills require a finite positive price and quantity.
- Fees must be finite and nonnegative; adjustments may be signed but finite.
- Manual basis adjustments require positive quantity and, when supplied,
  positive unit cost.
- FIFO fee allocation assigns any division residual to the final consumption,
  preserving exact aggregate reconciliation.
- Daily and weekly realized P&L are summed from canonical Decimal values.
- Existing percentage risk gates receive explicit float projections only after
  canonical accounting has completed.

The accounting formula identity is
`fifo_equity_unrealized_cashflow_v2_decimal`; the canonical evidence identity is
`fixed_point_fifo_accounting_v1`.
