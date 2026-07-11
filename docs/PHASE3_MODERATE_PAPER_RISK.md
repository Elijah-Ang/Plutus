# Phase 3 moderate paper risk

Phase 3 is an active paper-only portfolio-risk layer. Every entry and add still
requires an authorized Telegram approval and final validation. Phase 0 durable
intents, reservations, reconciliation, partial fills, unknown-order retention,
and crash recovery remain the only execution path.

The validated `moderate_paper_risk_v1` profile uses 0.20% base stop risk and
0.10% add risk. Quantity is risk budget divided by stop distance. Trade score
does not change size. Regime multipliers are deterministic, and account drawdown
scales new risk to 75% at 2%, 50% at 4%, and zero at 6%. Kelly sizing, leverage,
shorting, live trading, and Phase 4 allocation are disabled.

Projected heat combines held-position stop risk with active reservations,
including submitting, submitted, partial-fill, cancel-pending, unknown, and
reconciliation-required intents. Missing held stops or ambiguous exposure blocks
new risk. Gross, symbol, cluster, buying-power, cash, liquidity, daily-loss, and
weekly-loss constraints are applied before proposal creation and again during
final validation through the existing RiskEngine.

Phase 2 sleeves are evaluated automatically from completed 20-session,
cost-adjusted out-of-sample outcomes. Positive expectancy, at least two regimes,
minimum sample size, and healthy reconciliation are required for `ACTIVE`.
Incomplete evidence is `THROTTLED`; negative evidence or unhealthy reconciliation
is `SUSPENDED`. Re-evaluation each cycle provides deterministic recovery. The
The executable `rule_based_v2` remains eligible, and risk is equal-weighted
across only the executable strategy. Shadow strategy states remain research-only.

Deployment requires schema `phase3_moderate_paper_risk_v1`, a verified SQLite
backup, an immutable paper release, an unambiguous healthy Alpaca paper endpoint,
zero critical durable-integrity counters, no open broker orders, no leverage, and
authoritative account loss inputs. Rollback is code-compatible because migration
is additive; exact database rollback requires stopped writers and the verified
pre-migration backup.
