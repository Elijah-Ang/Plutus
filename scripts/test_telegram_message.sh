#!/bin/bash
set -euo pipefail

# Get token from Keychain
TOKEN=$(/usr/bin/security find-generic-password -a "$USER" -s "TradingAgent.TELEGRAM_BOT_TOKEN" -w)
if [ -z "$TOKEN" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN not found in Keychain" >&2
  exit 1
fi

# Load chat ID from .env
if [ -f .env ]; then
  TELEGRAM_CHAT_ID=$(grep -E "^TELEGRAM_CHAT_ID=" .env | cut -d'=' -f2)
fi

if [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "Error: TELEGRAM_CHAT_ID not found in .env" >&2
  exit 1
fi

MESSAGE="TradingAgent Telegram test successful. Paper-only mode remains active."

# Send message using curl
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
     -H "Content-Type: application/json" \
     -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"${MESSAGE}\"}" > /dev/null

echo "Test message sent successfully."
