#!/bin/zsh
set -euo pipefail
if [[ $# -ne 1 ]]; then echo "Usage: $0 ALPACA_API_KEY|ALPACA_SECRET_KEY|TELEGRAM_BOT_TOKEN|OPENAI_API_KEY"; exit 2; fi
case "$1" in ALPACA_API_KEY|ALPACA_SECRET_KEY|TELEGRAM_BOT_TOKEN|OPENAI_API_KEY) ;; *) echo "Unsupported secret name"; exit 2;; esac
read -s "VALUE?Enter value securely (input hidden): "
echo
security add-generic-password -U -a "$USER" -s "TradingAgent.$1" -w "$VALUE"
unset VALUE
echo "Stored in macOS Keychain; value was not printed."
