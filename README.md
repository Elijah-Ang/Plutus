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
