#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
.venv/bin/python -c 'from app.telegram_bot import TelegramBot; TelegramBot().send_message("TradingAgent Telegram test successful. Paper-only mode remains active.")'
echo "Test message sent; token was not printed."
