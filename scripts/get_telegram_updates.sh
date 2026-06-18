#!/bin/bash
set -euo pipefail

# Get token from Keychain
export TELEGRAM_BOT_TOKEN=$(/usr/bin/security find-generic-password -a "$USER" -s "TradingAgent.TELEGRAM_BOT_TOKEN" -w)
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "Error: TELEGRAM_BOT_TOKEN not found in Keychain" >&2
  exit 1
fi

python3 -c "
import os, urllib.request, json
token = os.environ['TELEGRAM_BOT_TOKEN']
url = f'https://api.telegram.org/bot{token}/getUpdates'
req = urllib.request.Request(url, method='POST')
try:
    with urllib.request.urlopen(req) as response:
        res = json.loads(response.read().decode('utf-8'))
        if not res.get('ok'):
            print('API Error:', res)
            exit(1)
        for update in res.get('result', []):
            msg = update.get('message', {})
            sender = msg.get('from', {})
            chat = msg.get('chat', {})
            print(json.dumps({
                'update_id': update.get('update_id'),
                'chat_id': chat.get('id'),
                'user_id': sender.get('id'),
                'username': sender.get('username'),
                'text': msg.get('text')
            }))
except Exception as e:
    print('Error executing request:', str(e))
"
