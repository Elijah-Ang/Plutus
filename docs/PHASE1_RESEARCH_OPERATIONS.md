# Phase 1 research and validation operations

Phase 1 is a dormant, offline research lane. It does not authorize proposals, approvals, orders, sizing changes, risk changes, live trading, deployment, or production database migration.

## Reproducible workflow

1. Create a point-in-time SQLite backup with the source opened read-only. Verify `PRAGMA integrity_check` on the clone.
2. Prepare an offline bar directory. Each symbol has a `SYMBOL.csv` file; `/` becomes `_`. Required columns are `timestamp` (or `date`/`time`), `open`, `high`, `low`, and `close`. Include `SPY.csv`. Bars must be the point-in-time data version being tested, with corporate-action/delisting provenance supplied separately. Do not silently use a revised current-universe dataset.
3. Run only against the clone:

   ```sh
   .venv/bin/python scripts/phase1_evidence.py \
     --db /tmp/tradingagent-phase1-clone.sqlite3 \
     --bars /tmp/phase1-point-in-time-bars \
     --report /tmp/PHASE1_EVIDENCE_REPORT.md \
     --as-of 2026-07-11T00:50:00Z \
     --limit 10000 \
     --spread-bps 4 \
     --entry-slippage-bps 2 \
     --exit-slippage-bps 2
   ```

4. Record the validation fingerprint, source database hash, bar-bundle hash, cost provenance, report hash, and git commit. Repeating identical inputs is idempotent. Interrupted jobs resume after their durable cursor; `--limit` bounds each invocation.

The committed evidence report used a fresh read-only SQLite backup of the paper-production database and an intentionally empty offline bar directory. It classified 3,114 duplicate-free opportunities into 9,342 horizon rows: 5,804 maturing and 3,538 unavailable. It did not fetch provider data or mutate the source. This is data-coverage evidence, not profitability evidence.

## Outcome semantics

- `completed`: the full exchange-session horizon and required asset bars exist.
- `maturing`: the correct NYSE regular-session horizon has not elapsed.
- `unavailable`: the horizon elapsed, but immutable required input is absent or invalid. Reprocessing requires a new input fingerprint.
- `failed`: a bounded calculation/provider error occurred. The error category is safe and explicit.

Canonical returns are decimals. MFE/MAE use the horizon OHLC path. SPY-relative return requires aligned SPY sessions. Stops and targets preserve unknown values. When both are touched in one daily bar, the result is conservatively stop-first and labeled ambiguous. R multiples remain null without a valid intended stop. Cost-adjusted results store the model version, total bps, parameters, source, and observation timestamp.

Actual fills, proposals without fills, blocked hypothetical setups, shadow hypothetical setups, observation-only rows, and generic hypothetical rows are separate `execution_type` populations. They must not be pooled without displaying each population and its sample size.

## Leakage controls

- Historical simulation calls the production `rule_based_v1` evaluator on an expanding prefix ending before the simulated entry session.
- Every opportunity stores strategy, score, feature, universe, regime, eligibility, blocker, and AI-gate versions.
- Universe membership must be supplied as-of each decision date. Current membership is not an acceptable historical substitute.
- Revised bars, corporate actions, delistings, and symbol changes require bundle provenance. Missing provenance is reported as unavailable or a limitation.
- Walk-forward folds are chronological, with a 20-session purge by default and a separate embargo. Labels never cross from training into test periods.
- Overlapping opportunities remain separate observations but require purging and cluster-aware uncertainty before inference.
- No threshold or strategy parameter is optimized in Phase 1. Sensitivity and ablation measure fragility; they do not select a better strategy.

## Validation and unsupported diagnostics

Reports always show sample sizes. The implementation provides score bands, Brier calibration, blocker/AI groups, cost sensitivity, ablation group comparisons, bootstrap intervals, probabilistic Sharpe, and walk-forward folds. Deflated Sharpe and PBO are emitted only when multiple independently tested configurations exist. Data-snooping correction is required whenever multiple hypotheses are compared. Unsupported statistics remain unavailable; they are never replaced with zero.

## Proposed cadence (design only; not deployed)

- Keep the existing scanner and all execution behavior unchanged.
- Capture immutable opportunity snapshots on each meaningful scanner decision, but do not recompute slow daily features every scan.
- Recompute daily trend/volatility/regime inputs once after a completed US session; reuse the versioned snapshot intraday.
- Mature 1/5/20-session outcomes once after the relevant session closes, with a bounded retry queue only for transient failures.
- Run point-in-time universe research before the session and one market-open freshness check. Preserve each membership change as an effective-dated event.
- Run the full validation report weekly or on a new immutable data cut, never inside the proposal/order path.

No launchd job, plist, production configuration, runtime pointer, or deployed cadence was changed.

## Gate

Phase 1 remains partial until a point-in-time bar/universe/corporate-action dataset yields sufficient completed OOS samples and all Critical/High research-integrity checks pass. Until then, score-based sizing, AI gating, Phase 2 activation, and Phase 3 risk expansion are unsupported.
