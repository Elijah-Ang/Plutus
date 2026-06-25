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
- **Dynamic Universe Resilience**: Missed/offline/provider-blocked research cycles are recorded and caught up safely later. Stale dynamic research blocks new dynamic BUY/ADD eligibility and promotions, while provider outages block false demotions.
- **Provider Capability Handling**: EODHD endpoint availability is tracked per endpoint. Plan-limited endpoints cool down instead of being called every cycle, and research scoring can use partial EOD/quote/news coverage with explicit data confidence.
- **Dynamic Paper Sizing**: BUY proposals use deterministic score, volatility, stop distance, and portfolio exposure caps instead of a fixed `$5` amount.
- **Portfolio Controls**: Paper BUY proposals are constrained by per-trade risk, open portfolio risk, single-symbol exposure, total exposure, correlated ETF cluster exposure, and available paper buying power.
- **Add-to-Winner Rules**: Add-on BUY proposals are allowed only for profitable positions with stronger setups; averaging down is blocked.
- **GPT Throttling**: GPT calls are limited by daily caps (10/day), score thresholds (>= 65), and minimum time intervals (30 minutes).

## Current hard safety capabilities

- Live trading is not supported by this build and cannot be enabled through YAML or Telegram.
- Auto-execution is not supported; every normal order requires an authorized Telegram approval.
- Ranked batches support `yes SYMBOL`, `no SYMBOL`, `yes all`, and `no all`; `yes all` is paper-only and still final-revalidates each candidate separately.
- Dynamic symbols start non-executable. Observation symbols are shadow-only, unsupported asset classes remain research-only, and paper-tradable promotion still routes through the existing risk budget, Telegram approval, and final revalidation.
- Missing EODHD keys, no internet, provider downtime, and low battery skip Dynamic Universe research safely. Existing static scanning can continue when normal scanner preflight passes.
- Partial EODHD plans are supported. Missing intraday, fundamentals, technicals, or screener access reduces provider capability and confidence but does not automatically block research candidates when EOD/quote/news data is sufficient.
- Final risk validation uses current broker/account loss and margin state plus live internet, Telegram, broker, and database checks. Unknown state blocks execution.
- Each cycle reconciles existing local orders/fills from Alpaca using broker/client order IDs without resubmitting anything.
- Excel reports redact Telegram text, sender IDs, and sensitive payload fields by default.
- The launchd lock records PID/time and only recovers a dead, sufficiently old lock.
