# TradingAgent

TradingAgent is a local, supervised trading assistant designed for education and Alpaca paper trading. It checks Mac power, connectivity, configuration, market state, deterministic risk limits, proposal expiry, Telegram authorization, and final order state. SQLite stores the audit trail and Excel exports provide a reviewable report.

It does **not** provide financial advice, let an AI decide or place orders, enable live trading by default, move cash, automate account login, or retry an uncertain order. A configured cycle processes Telegram updates, scans the watchlist, creates risk-cleared proposals, and can submit an approved Alpaca paper order only after final revalidation.

## Safety baseline

- `mode: paper`, `live_enabled: false`, and `explicit_live_confirmation: false`
- Secrets only in macOS Keychain or a gitignored `.env`
- Unknown power, ambiguous approval, stale data, failed risk checks, or uncertain execution blocks action
- ML is shadow-only; AI only explains structured, redacted proposal data
- `config/KILL_SWITCH` blocks operation

## Quick start

```zsh
cd /Users/elijahang/Projects/TradingAgent
./scripts/setup_venv.sh
.venv/bin/pytest
./scripts/run_once.sh
./scripts/export_excel.sh
```

The first run will safely fail preflight until local credentials are configured. Do not install launchd until the manual run and tests are satisfactory. See [SETUP.md](docs/SETUP.md), [SAFETY_RULES.md](docs/SAFETY_RULES.md), and [OPERATIONS_MANUAL.md](docs/OPERATIONS_MANUAL.md).

## Developer safeguard checklist
Whenever making meaningful changes to configs, scripts, or core application modules, update [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) to reflect the new state. A unit test enforces the existence and structure of this document.

## GitHub Workflow & Telemetry scoring

- **GitHub Remote**: Plutus points to `git@github.com:Elijah-Ang/Plutus.git`. Run `scripts/safe_commit_push.sh` to safely run tests, scan for secrets, and commit changes.
- **10-Minute Telemetry**: Scheduled launchd runs observation cycles every 10 minutes to calculate deterministic scores (0-100), log snapshots to `market_memory`, and compare short-term trends without spamming Telegram or calling GPT.
- **Performance Lab**: Every meaningful setup can be recorded as an actual or shadow opportunity, with forward outcome tracking for actual-vs-shadow review.
- **Risk-Budgeted Ranked Proposals**: BUY/ADD opportunities are ranked as a batch, sized by stop distance and portfolio risk, and constrained by open risk, exposure, cluster, and paper buying power instead of fixed proposal-count caps.
- **Position Management Engine**: Existing paper positions are classified each cycle for profit-taking, profit protection, trailing stops, and healthy-pullback adds. Higher-priority risk/profit-protection exits override ADD logic.
- **Dynamic Universe Research Engine**: EODHD-backed discovery researches broad symbol pools, promotes symbols through raw/research/observation/paper-tradable tiers, and demotes stale candidates. EODHD is research-only; Alpaca remains the paper broker and execution truth.
- **Staged Crypto Lane**: BTC/USD and ETH/USD stay `research_only` by default. Each research cycle now binds current Alpaca Assets API precision/tradability to a verified paper-account capability snapshot, while the generic equity adapter rejects crypto before broker I/O. `paper_watch` can record hypothetical candidates with stop/target/risk metadata but no proposals or orders. In this implementation, Stage 3 is readiness-report-only; enabling `paper_proposal` requires a separate user-approved config-change task and commit. EODHD remains research context only, never a final trading source. See [CRYPTO_DATA_AND_BROKER_CAPABILITY.md](docs/CRYPTO_DATA_AND_BROKER_CAPABILITY.md).
- **Dynamic Universe Resilience**: Missed/offline/provider-blocked research cycles are recorded and caught up safely later. Stale dynamic research blocks new dynamic BUY/ADD eligibility and promotions, while provider outages block false demotions.
- **Provider Capability Handling**: EODHD endpoint availability is tracked per endpoint. Plan-limited endpoints cool down instead of being called every cycle. The current scoring path is tailored to the EOD+Intraday plan: EOD bars, screener, realtime quote, and intraday bars are core; fundamentals are non-gating, news is an optional catalyst bonus, and local technical calculations are preferred over per-symbol technical API calls.
- **Research Candidate Semantics**: A research candidate has passed the pre-market universe scan, filtering, EODHD/local quantitative scoring, and deterministic candidate brief generation. It is worth tracking and explaining, but it is not tradable and cannot create Telegram trade proposals or orders.
- **Adaptive Paper Sizing**: BUY/ADD proposals use Phase 3 hard limits, Phase 4 strategy sleeves, Adaptive Conviction, and Adaptive Sizing. Candidate, sleeve, global-budget, allocation, reservation, and reconciliation risk is canonical stop-risk dollars converted from explicitly tagged units using fresh authoritative equity. Final quantity, notional, and stop risk can only stay the same, reduce, or block at approval.
- **Operational Strategy States**: `PROBE`, `EXPLORATION`, `THROTTLED`, and `ACTIVE` are bounded paper-only states. Multi-strategy allocation ranks candidates globally but prevents one strategy from consuming another strategy's sleeve.
- **Winner and Trend Management**: Winner expansion is lifecycle-bound, never averages down, and requires a filled entry with Initial-R provenance. Trend-sensitive management rebuilds every dependent flag from its final mode; `EXIT_REQUIRED` has no ADD authority and requests a full risk-reducing exit.
- **Exit-First Rotation**: A manually approved, lifecycle-bound rotation submits exits first, waits for actual fill and reconciliation, then independently revalidates exactly one contingent entry. Expected proceeds are never reserved before the exit fill.
- **Durable Execution and Replay**: Atomic reservations and idempotent intents remain authoritative. Ambiguous broker outcomes reconcile by broker/client identity without blind resubmission. Deterministic policy, allocation, covariance, ranking, and replay paths require an explicit evaluation timestamp.
- **Configuration Source of Truth**: Runtime reads and validates `config/config.yaml`. `config/strategies.yaml` is archived/unused; there is no active `config/risk_limits.yaml`. See [CONFIGURATION_AND_SIZING.md](docs/CONFIGURATION_AND_SIZING.md).
- **Portfolio Controls**: Paper BUY proposals are constrained by per-trade risk, open portfolio risk, single-symbol exposure, total exposure, correlated ETF cluster exposure, and available paper buying power.
- **Add-to-Winner Rules**: Add-on BUY proposals are allowed only for profitable positions with stronger setups; averaging down is blocked.
- **GPT Throttling**: GPT calls are limited by daily caps (10/day), score thresholds (>= 65), and minimum time intervals (30 minutes).

## Current hard safety capabilities

- Live trading is not supported by this build and cannot be enabled through YAML or Telegram.
- Auto-execution is not supported; every normal order requires an authorized Telegram approval.
- Ranked batches support `yes SYMBOL`, `no SYMBOL`, `yes all`, and `no all`; `yes all` is paper-only and still final-revalidates each candidate separately.
- Dynamic symbols start non-executable. Observation symbols are shadow-only, unsupported asset classes remain research-only, and paper-tradable promotion still routes through the existing risk budget, Telegram approval, and final revalidation.
- Missing EODHD keys, no internet, provider downtime, and low battery skip Dynamic Universe research safely. Existing static scanning can continue when normal scanner preflight passes.
- Dynamic universe intake is split into lanes: `alpaca_compatible_us`, `global_research_only`, and `excluded_or_low_quality`. Only the Alpaca-compatible US lane can ever enter the paper-trading scanner, and only after recorded promotion evidence. Global and low-quality symbols remain research/reporting-only.
- Partial EODHD plans are supported. Missing fundamentals or news does not block research candidates when price/liquidity data is usable. Missing critical EOD price/liquidity data still blocks promotion.
- Pre-market Dynamic Universe research can run before US market open. The market-open gate remains required for trading/proposal/order paths, so research-only runs can complete with trading explicitly blocked.
- The accurate operator wording is "pre-market universe scan" unless candidate-level research briefs were generated. Full analyst-style research would require thesis, sector/news/catalyst context, risk narrative, comparisons, and promotion requirements beyond raw scoring.
- Final risk validation uses current broker/account loss and margin state plus live internet, Telegram, broker, and database checks. Unknown state blocks execution.
- Each cycle reconciles existing local orders/fills from Alpaca using broker/client order IDs without resubmitting anything.
- Take-profit milestones advance only from unique broker fill records linked to the active position lifecycle, proposal, and order intent; rejection, expiry, submission, cancellation, and zero-fill reconciliation do not consume a level.
- Submitting a BUY supersedes only an equivalent or mutually exclusive BUY. Unrelated ranked-batch, `YES ALL`, position, and strategy-sleeve candidates remain pending and are independently revalidated.
- SELL revalidation is directional: a further adverse downward move increases exit urgency but does not by itself invalidate a risk-reducing exit. Fresh quote, held quantity, conflicting orders, market state, expiry, and no-shorting checks remain mandatory.
- `scripts/check_release_eligibility.py` reports local commit/config/schema/migration/paper/test evidence and only reports GitHub CI passed when a successful workflow run is linked to the exact commit.

### Release source authority and rollback

Release verification has two intentionally separate inventories. `tracked-source-inventory.json`
is derived from the exact GitHub commit tree and records every tracked path, Git blob SHA, and
content SHA-256. Its digest and Git tree SHA are bound into the release authority; changing a
tracked file cannot be legitimized by regenerating local artifact hashes. The separate
`release-file-inventory.sha256` covers generated artifact evidence such as the virtual environment,
test results, dependency inventory, and manifest.

Ordinary deployment accepts only the exact current GitHub `main` commit:

```sh
scripts/deploy_release.sh --mode forward /Users/elijahang/TradingAgentReleases/<release-id>
```

A rollback must name a previously built immutable release whose annotated
`immutable-release-*` tag and immutable GitHub Release attestation bind the original commit,
Git tree, tracked-source inventory digest, CI run, configuration, schema, and formula versions:

```sh
scripts/deploy_release.sh --mode rollback /Users/elijahang/TradingAgentReleases/<approved-release-id>
```

An arbitrary ancestor, lightweight tag, replaced/duplicated attestation asset, or locally
regenerated inventory is not rollback authority.
- Excel reports redact Telegram text, sender IDs, and sensitive payload fields by default.
- The launchd lock records PID/time and only recovers a dead, sufficiently old lock.
