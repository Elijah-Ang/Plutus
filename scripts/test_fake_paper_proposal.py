import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.storage import Storage  # noqa: E402
from app.telegram_bot import TelegramBot  # noqa: E402
from app.utils import PROJECT_ROOT, format_proposal_message, load_config  # noqa: E402


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config()
    db_path = PROJECT_ROOT / config["storage"]["sqlite_path"]
    storage = Storage(db_path)
    storage.initialize()

    run_id = storage.start_run("paper")
    proposal_id = str(uuid.uuid4())
    signal_id = str(uuid.uuid4())

    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=10)

    proposal = {
        "id": proposal_id,
        "run_id": run_id,
        "signal_id": signal_id,
        "symbol": "TEST",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "latest_price": 100.0,
        "price_at": now.isoformat(),
        "historical_bars": 100,
        "volume": 5000,
        "price_gap_pct": 0.0,
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "Telegram approval flow test only",
        "order_type": "market",
        "asset_class": "equity",
    }

    # Write proposal to database
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            proposal_id,
            run_id,
            signal_id,
            "TEST",
            "buy",
            5.0,
            "pending",
            now.isoformat(),
            expiry.isoformat(),
            "rule_based_v1",
            json.dumps(proposal),
        ),
    )

    # Write mock AI review
    review = {
        "summary": "FAKE PAPER TEST PROPOSAL — no real/live order will be placed unless explicitly approved through the test flow.",
        "risks": [
            "This is a fake test proposal for approval flow testing.",
            "Symbol TEST is mocked and will not submit a real order to Alpaca.",
            "Mode is paper only.",
        ],
        "telegram_message": "FAKE PAPER TEST PROPOSAL — no real/live order will be placed unless explicitly approved through the test flow.",
        "caution_level": "low",
        "should_block_for_reasoning_only": False,
        "reasoning_notes": "Fake test review.",
    }
    storage.execute(
        "INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            run_id,
            proposal_id,
            review["summary"],
            json.dumps(review["risks"]),
            review["caution_level"],
            json.dumps(review),
            now.isoformat(),
        ),
    )

    # Save config snapshot
    storage.execute(
        "INSERT INTO config_snapshots(run_id,config_json,created_at) VALUES(?,?,?)",
        (run_id, json.dumps({"mode": "paper", "live_enabled": False}), now.isoformat()),
    )

    # Send message to Telegram
    try:
        bot = TelegramBot()
        msg_text = f"Proposal {proposal_id}\n\n" + format_proposal_message(
            proposal, config, is_fake_test=True
        )
        bot.send_message(msg_text)
        print("Fake proposal sent to Telegram successfully.")
        print(f"Proposal ID: {proposal_id}")
    except Exception as e:
        print(f"Error sending Telegram message: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
