# Setup

1. Install Python 3.11 or newer and confirm with `python3 --version`.
2. Run `cd /Users/elijahang/Projects/TradingAgent && ./scripts/setup_venv.sh`.
3. Review `config/config.yaml`. Keep `mode: paper`, `live_enabled: false`, and `explicit_live_confirmation: false`.
4. Prefer Keychain: `./scripts/store_secret_keychain.sh ALPACA_API_KEY` (repeat for each supported secret). The runtime reads these `TradingAgent.NAME` Keychain entries directly. Alternatively, `cp .env.template .env`, restrict it with `chmod 600 .env`, and replace placeholders locally.
5. Put non-secret Telegram user/chat IDs in `.env`; never enter account passwords.
6. Run `.venv/bin/pytest`, then `./scripts/run_once.sh`. A blocked preflight is expected until every dependency is configured and the US market is open.

Crypto research is scheduled independently of US equity market hours. The default stage remains `crypto.mode=research_only`, with `crypto.paper_trading_enabled=false` and `crypto.proposals_enabled=false`, so it records snapshots, scores, blockers, provider coverage, Performance Lab rows, and counterfactuals only. `paper_watch` can record hypothetical crypto proposal candidates without creating `trade_proposals`, Telegram approvals, approvals, or broker orders. `paper_proposal` is evidence-gated and still requires manual Telegram approval plus final validation before any Alpaca paper order path can run. Market-closed trading preflight still blocks equity proposals and orders; crypto quiet hours suppress non-urgent Telegram status only, not database recording, Performance Lab shadow rows, or provider/data-coverage blockers. Crypto provider failures should record explicit blockers such as `crypto_provider_unavailable` or `crypto_alpaca_final_price_unavailable` and must not create orders.
7. Export with `./scripts/export_excel.sh`.

Never paste secrets into source, Git, logs, screenshots, prompts, or exported workbooks. The active SQLite database must remain outside iCloud Drive. This project path is local; verify that your Documents folder is not being synchronized before production-like use.
