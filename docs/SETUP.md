# Setup

1. Install Python 3.11 or newer and confirm with `python3 --version`.
2. Run `cd /Users/elijahang/Projects/TradingAgent && ./scripts/setup_venv.sh`.
3. Review `config/config.yaml`. Keep `mode: paper`, `live_enabled: false`, and `explicit_live_confirmation: false`.
4. Prefer Keychain: `./scripts/store_secret_keychain.sh ALPACA_API_KEY` (repeat for each supported secret). The runtime reads these `TradingAgent.NAME` Keychain entries directly. Alternatively, `cp .env.template .env`, restrict it with `chmod 600 .env`, and replace placeholders locally.
5. Put non-secret Telegram user/chat IDs in `.env`; never enter account passwords.
6. Run `.venv/bin/pytest`, then `./scripts/run_once.sh`. A blocked preflight is expected until every dependency is configured and the US market is open.

Crypto research is scheduled independently of US equity market hours. The default stage remains `crypto.mode=research_only`, with `crypto.paper_trading_enabled=false` and `crypto.proposals_enabled=false`, so it records snapshots, scores, blockers, provider coverage, Performance Lab rows, and counterfactuals only. `paper_watch` can record hypothetical crypto proposal candidates without creating `trade_proposals`, `proposal_batches`, Telegram approvals, approvals, orders, or fills. In this implementation, `paper_proposal` can only record a Stage 3 readiness report; it must not create crypto proposals automatically even when the evidence gate passes. Enabling Stage 3 proposals requires a separate explicit user-approved config-change task and separate commit. Market-closed trading preflight still blocks equity proposals and orders; crypto quiet hours suppress research/status notifications only, not database recording, Performance Lab shadow rows, or provider/data-coverage blockers. Stage 3 v1 proposals must not be sent during quiet hours. Crypto provider failures should record explicit blockers such as `crypto_provider_unavailable` or `crypto_alpaca_final_price_unavailable` and must not create orders.

Crypto Decimal sizing and portfolio-risk evaluation are also research-only.
They write immutable `crypto_risk_snapshots`, `crypto_sizing_decisions`, and
`crypto_risk_decisions`, all with `execution_authorized=0`. The authoritative
calculator uses current Alpaca pair increments and paper account/position/order
evidence plus durable SQLite exposure, loss, drawdown and reservation state.
The float-valued fields in `crypto_paper_watch_candidates` remain descriptive
research metadata and must never be treated as order authority. See
[CRYPTO_SIZING_AND_RISK.md](CRYPTO_SIZING_AND_RISK.md).
7. Export with `./scripts/export_excel.sh`.

Never paste secrets into source, Git, logs, screenshots, prompts, or exported workbooks. The active SQLite database must remain outside iCloud Drive. This project path is local; verify that your Documents folder is not being synchronized before production-like use.
