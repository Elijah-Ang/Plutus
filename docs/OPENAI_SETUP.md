# OpenAI setup

Create an API key manually in your OpenAI account and store it in Keychain or the gitignored `.env`; never provide an account password. Configure the model and reasoning effort in `config/config.yaml` and monitor API costs.

The Responses API receives only proposal symbol, side, mode, small proposed notional, indicators, risk summary, strategy reason, optional shadow opinion, expiry, and warnings. It never receives broker keys, Telegram tokens, private account IDs, or full balances. AI produces an explanatory JSON summary only. Invalid/unavailable AI output falls back deterministically in paper mode. Python risk rules retain control.
