# Phase 4 adaptive paper allocation

`adaptive_paper_allocator_v1` is active only in paper mode. It consumes only
completed out-of-sample evidence explicitly classified as executable portfolio
returns or actual paper-trade returns; signal and shadow outcomes remain
separate diagnostics. Qualified evidence uses adaptive allocation. Healthy
immature executable strategies are persisted as `EXPLORATION`
with an explicit `insufficient` evidence class and may receive bounded paper
exploration; negative, deteriorating, stale, or unsafe evidence is persisted as
`SUSPENDED` and receives zero new risk. Mature but uncertain positive evidence is
`THROTTLED` and receives zero adaptive risk. These states are versioned and
fingerprinted; insufficient evidence is never represented as negative evidence.

Each estimate stores beta-prior calibrated positive probability, normal-prior
mean shrinkage toward zero, a conservative lower confidence estimate, uncertainty,
cost completeness, regime coverage, evidence fingerprint, and recent deterioration.
Strategies promote to `ACTIVE`, throttle for incomplete evidence, suspend for
negative conservative evidence or unhealthy integrity, and recover deterministically.

Allocation uses a covariance matrix shrunk 50% toward its diagonal. Insufficient
paired observations use a conservative 0.50 correlation fallback. Marginal and
component risk, expected shortfall, overlap, uncertainty, costs, data quality,
regime, and drawdown are persisted. Stress scenarios include SPY -3%/-5%, sector
-7%, doubled volatility, two-ATR gaps, correlations to one, and largest strategy
-15%.

Kelly, covariance, calibration, and shrinkage remain persisted research
diagnostics. They do not size operational paper proposals. Qualified
executable strategies use deterministic equal-risk allocation, capped at 35%
per strategy and 75% total allocation; operational Kelly is disabled until
synchronized strategy-level return and covariance evidence is explicitly
introduced. Exploration is capped at 0.25% total stop-risk heat, 0.05% stop
risk per immature strategy, 0.10% maximum per strategy, and 7.5% gross
exposure. Research-only and shadow strategies retain evidence and state
transitions but never receive executable allocation. If no safe adaptive or
exploration policy is eligible, the allocator records `PRESERVE_CASH` with
zero new strategy risk.

Phase 3 stop-risk sizing, heat, gross, symbol, cluster, liquidity, loss, drawdown,
no-leverage, durable intent/reservation, final validation, and Telegram approval
limits remain authoritative and cannot be raised by Phase 4. Full Kelly, score
sizing, LLM decisions, live trading, and Phase 5 behavior are forbidden.
