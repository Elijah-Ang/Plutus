# Alpaca paper setup

Manually create or sign into Alpaca in your browser. Do not provide TradingAgent your Alpaca password. Select the paper-trading environment and generate paper API credentials.

Store only the paper key and secret in Keychain with `scripts/store_secret_keychain.sh`, or in a mode-600 `.env` fallback. Never use live credentials in v1. Run `./scripts/run_once.sh`; `401` usually means wrong environment or credentials, `403` indicates permissions, and timeouts indicate network/API availability. Confirm `paper=True` behavior and account mode in Alpaca before any proposal testing.
