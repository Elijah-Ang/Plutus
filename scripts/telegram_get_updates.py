import argparse
import json

from app.telegram_bot import TelegramBot, redact_telegram_update

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect Telegram updates with privacy-safe output")
    parser.add_argument("--raw", action="store_true", help="Explicitly print raw Telegram IDs and text")
    args = parser.parse_args()
    for update in TelegramBot().get_updates():
        print(json.dumps(redact_telegram_update(update, include_raw=args.raw), sort_keys=True))
