import json
import uuid
import sys
from datetime import UTC, datetime, timedelta
from dotenv import load_dotenv
from app.storage import Storage
from app.telegram_bot import TelegramBot
from app.broker_alpaca import AlpacaBroker
from app.risk_engine import RiskEngine
from app.power import get_power_status
from app.utils import PROJECT_ROOT, load_config, format_proposal_message

def main():
    load_dotenv(PROJECT_ROOT / ".env")
    config = load_config()
    db_path = PROJECT_ROOT / config["storage"]["sqlite_path"]
    storage = Storage(db_path)
    storage.initialize()
    
    # 1. Initialize AlpacaBroker to fetch real data
    try:
        broker = AlpacaBroker(config)
    except Exception as e:
        print(f"Error initializing AlpacaBroker: {e}", file=sys.stderr)
        sys.exit(1)
        
    symbol = "SPY"
    print(f"Fetching latest price and bars for {symbol}...")
    try:
        trade = broker.get_latest_price(symbol)
        price = float(trade.price)
        price_at = trade.timestamp
        bars = broker.get_historical_bars(symbol, "1Day", 250)
    except Exception as e:
        print(f"Failed to fetch {symbol} data, trying QQQ: {e}")
        symbol = "QQQ"
        try:
            trade = broker.get_latest_price(symbol)
            price = float(trade.price)
            price_at = trade.timestamp
            bars = broker.get_historical_bars(symbol, "1Day", 250)
        except Exception as e2:
            print(f"Failed to fetch QQQ data: {e2}", file=sys.stderr)
            sys.exit(1)
            
    # 2. Build proposal and context
    run_id = storage.start_run("paper")
    proposal_id = str(uuid.uuid4())
    signal_id = str(uuid.uuid4())
    
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=10)
    
    proposal = {
        "id": proposal_id,
        "run_id": run_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": "buy",
        "action": "entry",
        "notional": 1.0,
        "latest_price": price,
        "price_at": price_at.isoformat() if hasattr(price_at, "isoformat") else str(price_at),
        "historical_bars": len(bars),
        "volume": float(bars.iloc[-1]["volume"]),
        "price_gap_pct": float((price / float(bars.iloc[-1]["close"]) - 1) * 100),
        "created_at": now.isoformat(),
        "expires_at": expiry.isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": f"Controlled real-symbol {symbol} paper execution test",
        "order_type": "market",
        "asset_class": "equity",
    }
    
    # Check if market is open
    market_open = broker.is_market_open()
    
    positions = broker.get_positions()
    orders = broker.get_open_orders()
    account = broker.get_account()
    today_orders = storage.fetch_all("SELECT id FROM orders WHERE substr(created_at,1,10)=?", (now.date().isoformat(),))
    
    context = {
        "power_connected": get_power_status().connected is True,
        "internet_available": True,
        "database_writable": storage.writable(),
        "broker_available": True,
        "telegram_available": True,
        "market_open": market_open,
        "kill_switch": (PROJECT_ROOT / "config" / "KILL_SWITCH").exists(),
        "open_positions": len(positions),
        "trades_today": len(today_orders),
        "duplicate_order": any(str(getattr(o, "symbol", "")).upper() == symbol.upper() for o in orders),
        "same_symbol_position": any(str(getattr(p, "symbol", "")).upper() == symbol.upper() for p in positions),
        "uses_margin": False,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "buying_power": float(getattr(account, "buying_power", 0) or 0),
        "approval_valid": False,
    }
    
    # 3. Evaluate via RiskEngine
    risk_engine = RiskEngine(config)
    decision = risk_engine.evaluate(proposal, context)
    if not decision.passed:
        print(f"Risk checks failed. Proposal blocked: {'; '.join(decision.reasons)}", file=sys.stderr)
        sys.exit(2)
        
    print(f"Risk checks passed. Saving {symbol} proposal {proposal_id} to database...")
    
    # Expire old pending proposals to avoid parser ambiguity
    storage.execute("UPDATE trade_proposals SET status='expired' WHERE status='pending'")
    
    # Write proposal to database
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (proposal_id, run_id, signal_id, symbol, "buy", 1.0, "pending", now.isoformat(), expiry.isoformat(), "rule_based_v1", json.dumps(proposal))
    )
    
    # Write mock AI review summarizing the paper test
    review = {
        "summary": f"PAPER EXECUTION TEST — Real-symbol {symbol} paper proposal test.",
        "risks": [
            "This is a controlled paper execution test.",
            "Mode is paper only. No real money will be used."
        ],
        "telegram_message": f"PAPER EXECUTION TEST — Real-symbol {symbol} paper proposal test. FAKE MONEY ONLY.",
        "caution_level": "low",
        "should_block_for_reasoning_only": False,
        "reasoning_notes": "Real-symbol paper execution test."
    }
    storage.execute(
        "INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)",
        (run_id, proposal_id, review["summary"], json.dumps(review["risks"]), review["caution_level"], json.dumps(review), now.isoformat())
    )
    
    # Save config snapshot
    storage.execute(
        "INSERT INTO config_snapshots(run_id,config_json,created_at) VALUES(?,?,?)",
        (run_id, json.dumps({"mode": "paper", "live_enabled": False}), now.isoformat())
    )
    
    # 4. Send message to Telegram
    try:
        bot = TelegramBot()
        msg_text = f"Proposal {proposal_id}\n\n" + format_proposal_message(proposal, config)
        bot.send_message(msg_text)
        print("Real-symbol paper proposal sent to Telegram successfully.")
        print(f"Proposal ID: {proposal_id}")
    except Exception as e:
        print(f"Error sending Telegram message: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
