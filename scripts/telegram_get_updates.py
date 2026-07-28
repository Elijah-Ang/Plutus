import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.telegram_bot import TelegramBot, redact_telegram_update  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Inspect Telegram updates with privacy-safe output"
    )
    parser.add_argument(
        "--raw", action="store_true", help="Explicitly print raw Telegram IDs and text"
    )
    args = parser.parse_args()
    for update in TelegramBot().get_updates():
        print(
            json.dumps(
                redact_telegram_update(update, include_raw=args.raw), sort_keys=True
            )
        )
