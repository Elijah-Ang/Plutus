# Alpaca paper setup

Manually create or sign into Alpaca in your browser. Do not provide TradingAgent your Alpaca password. Select the paper-trading environment and generate paper API credentials.

Store only the paper key and secret in Keychain with `scripts/store_secret_keychain.sh`, or in a mode-600 `.env` fallback. Never use live credentials in v1. Run `./scripts/run_once.sh`; `401` usually means wrong environment or credentials, `403` indicates permissions, and timeouts indicate network/API availability. Confirm `paper=True` behavior and account mode in Alpaca before any proposal testing.

The equity real-time feed must be selected explicitly with
`alpaca.equity_realtime_data_feed`. Use `iex` for a Basic-data account. IEX is a
single-exchange feed and can produce a quote that fails the configured spread guard;
that is a safe no-proposal/no-order result. Select `sip` only after Alpaca confirms
that the account is entitled to current real-time SIP data. Never change the feed or
weaken the spread threshold merely to force an approval through. After any feed
change, build and deploy a separately reviewed immutable release; old proposal and
approval authority remains invalid.
