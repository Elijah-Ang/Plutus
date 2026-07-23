import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.telegram_bot import TelegramBot  # noqa: E402

if __name__ == "__main__":
    TelegramBot().send_test_message()
    print("Test message sent; token was not printed.")
