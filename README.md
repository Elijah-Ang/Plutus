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
cd /Users/elijahang/Documents/Trading/TradingAgent
./scripts/setup_venv.sh
.venv/bin/pytest
./scripts/run_once.sh
./scripts/export_excel.sh
```

The first run will safely fail preflight until local credentials are configured. Do not install launchd until the manual run and tests are satisfactory. See [SETUP.md](docs/SETUP.md), [SAFETY_RULES.md](docs/SAFETY_RULES.md), and [OPERATIONS_MANUAL.md](docs/OPERATIONS_MANUAL.md).

## Developer safeguard checklist
Whenever making meaningful changes to configs, scripts, or core application modules, you must update the master [docs/SYSTEM_OVERVIEW.md](file:///Users/elijahang/Desktop/TradingAgent/docs/SYSTEM_OVERVIEW.md) to reflect the new state. A unit test enforces the existence and structure of this document.

## GitHub Workflow & Telemetry scoring

- **GitHub Remote**: Plutus points to `git@github.com:Elijah-Ang/Plutus.git`. Run `scripts/safe_commit_push.sh` to safely run tests, scan for secrets, and commit changes.
- **5-Minute Telemetry**: Scheduled launchd runs observation cycles every 5 minutes to calculate deterministic scores (0-100), log snapshots to `market_memory`, and compare short-term trends without spamming Telegram or calling GPT.
- **GPT Throttling**: GPT calls are limited by daily caps (10/day), score thresholds (>= 65), and minimum time intervals (30 minutes).


