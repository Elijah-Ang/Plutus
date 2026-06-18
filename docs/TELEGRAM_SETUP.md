# Telegram setup

1. In Telegram, manually talk to BotFather, create a bot, and copy its token. Do not share your Telegram password.
2. Store the token in Keychain or the gitignored `.env` as `TELEGRAM_BOT_TOKEN`.
3. Send your bot a message, then run `.venv/bin/python scripts/telegram_get_updates.py`. Copy only the numeric user/chat IDs into `.env`.
4. Test with `.venv/bin/python scripts/telegram_test.py`.

Accepted approvals include `yes`, `approve`, `yes please`, and `yes buy qqq`; rejections include `no`, `reject`, and `no thanks`. Plain approval works only for exactly one pending, unexpired proposal. Mismatched users, symbols, actions, ambiguous replies, reused approvals, and expired proposals are rejected. `/killswitch` must be handled locally; Telegram can never enable live trading.
