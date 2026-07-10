# Phase 1 Evidence Report

Point-in-time as of: `2026-07-11T00:50:00+00:00`
Outcome engine: `phase1_outcome_v1`
Cost model: `cli_cost_assumption_v1` (8.00 bps round trip; source: CLI parameters; operator must replace assumptions with observed quote/fill evidence)

## Evidence verdict

Strategy support after costs: **inconclusive**. This report requires at least 100 OOS observations and a positive 95% bootstrap lower bound.
OOS n=0; mean=None; 95% interval=None.
Score-based sizing, AI gating, Phase 2 activation, and Phase 3 risk expansion remain unsupported unless separately proven by positive OOS incremental evidence.

## Coverage

| status | n |
|---|---:|
| maturing | 5804 |
| unavailable | 3538 |

| status | reason | n |
|---|---|---:|
| maturing | exchange_session_horizon_not_elapsed | 5804 |
| unavailable | asset_session_bars_missing | 3526 |
| unavailable | missing_or_invalid_entry_price | 12 |

## Opportunity types

| execution classification | n |
|---|---:|
| actual_fill | 12 |
| blocked_hypothetical | 980 |
| observation_only | 1351 |
| proposal_unfilled | 2 |
| shadow_hypothetical | 769 |

## OOS grouped results

| strategy | regime | execution | n | mean net return | win rate |
|---|---|---|---:|---:|---:|
| unavailable | unavailable | unavailable | 0 | unavailable | unavailable |

## Score calibration

| score band | n | predicted | observed wins | Brier |
|---|---:|---:|---:|---:|
| 0-59 | 0 | None | None | None |
| 60-69 | 0 | None | None | None |
| 70-79 | 0 | None | None | None |
| 80-89 | 0 | None | None | None |
| 90-100 | 0 | None | None | None |

## Blocker and AI-gate evidence

Incremental blocker and AI-gate value is **inconclusive** because no completed OOS labels are available. Unknown gate values remain unknown; they are not treated as passes, failures, or zero returns.

## Walk-forward, sensitivity, and overfitting

The implementation supports deterministic expanding-window simulation, purged/embargoed walk-forward folds, cost sensitivity, ablation group comparisons, bootstrap uncertainty, score calibration/Brier analysis, and probabilistic Sharpe. With zero completed OOS observations, numerical walk-forward and sensitivity conclusions are unavailable. Deflated Sharpe and PBO are deliberately unavailable without multiple independently tested configurations; no synthetic value is emitted.

## Assumptions and data quality

- Round-trip costs are 8.00 bps from `cli_cost_assumption_v1`; provenance is `CLI parameters; operator must replace assumptions with observed quote/fill evidence`.
- SPY is the benchmark. Benchmark-relative values stay null when aligned SPY sessions are missing.
- All returns are decimal returns in the canonical ledger; legacy report projections use percentage points.
- An unavailable row is terminal for the current immutable input fingerprint. A later data bundle creates a new reproducible calculation fingerprint.
- Maturing rows are not failures; their exchange-session horizon had not elapsed at the report timestamp.

## Limitations

- No delisting or corporate-action adjustment is claimed unless encoded in the supplied point-in-time bar bundle.
- Rows without an immutable historical universe snapshot retain an unknown or legacy universe version and are not strong survivorship-bias evidence.
- Daily OHLC cannot order a stop and target touched in the same bar; the calculator applies conservative stop-first ordering and labels it ambiguous.
- Deflated Sharpe and probability of backtest overfitting are unavailable when the evidence set does not contain multiple independently tested configurations.
- The default cost model is an explicit assumption, not observed fill calibration.

## Integrity controls

Outcomes use exchange-session horizons, immutable version labels, point-in-time prefixes, explicit unavailable values, conservative daily-bar barrier ordering, traceable costs, and purged/embargoed walk-forward splits. Delisted/corporate-action evidence is unavailable unless supplied by the input bundle; it is never inferred from current membership.
