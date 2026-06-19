from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ai_review import AIReviewer, deterministic_review
from .approval_parser import parse_approval
from .execution import Executor
from .market_data import normalize_bars
from .power import get_power_status
from .risk_engine import RiskCheck, RiskEngine
from .strategy_rule_based import evaluate_symbol
from .telegram_bot import TelegramBot
from .utils import PROJECT_ROOT, iso_now, json_dumps, format_proposal_message, translate_reason


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if not isinstance(obj, dict) else obj.get(name, default)


class TradingService:
    """One bounded launchd cycle. AI never receives a broker or execution object."""

    def __init__(self, config: dict[str, Any], storage: Any, broker: Any, run_id: str) -> None:
        self.config, self.storage, self.broker, self.run_id = config, storage, broker, run_id
        telegram = TelegramBot()
        self.telegram = telegram
        self.ai = AIReviewer(config.get("ai", {}))

    def _risk_engine(self, proposal_id: str, stage: str) -> RiskEngine:
        return RiskEngine(self.config, lambda c: self.storage.record_check(self.run_id, c.name, c.passed, c.reason, proposal_id, stage))

    def _portfolio_context(self, proposal: dict[str, Any], approval_valid: bool = False) -> dict[str, Any]:
        positions = self.broker.get_positions()
        orders = self.broker.get_open_orders()
        account = self.broker.get_account()
        symbol = proposal["symbol"]
        today_orders = self.storage.fetch_all("SELECT id FROM orders WHERE substr(created_at,1,10)=?", (datetime.now(UTC).date().isoformat(),))
        return {
            "power_connected": get_power_status().connected is True,
            "internet_available": True, "database_writable": self.storage.writable(), "broker_available": True,
            "telegram_available": True, "market_open": self.broker.is_market_open(),
            "kill_switch": (PROJECT_ROOT / "config" / "KILL_SWITCH").exists(),
            "open_positions": len(positions), "trades_today": len(today_orders),
            "duplicate_order": any(str(_value(o, "symbol", "")).upper() == symbol for o in orders),
            "same_symbol_position": any(str(_value(p, "symbol", "")).upper() == symbol for p in positions),
            "uses_margin": False, "daily_loss": 0, "weekly_loss": 0,
            "buying_power": float(_value(account, "buying_power", 0) or 0), "approval_valid": approval_valid,
        }

    def process_telegram(self) -> None:
        updates = self.telegram.get_updates(timeout=0)
        if not updates:
            return
        max_id = 0
        for update in updates:
            max_id = max(max_id, update.get("update_id", 0))
            message = update.get("message") or {}
            text = str(message.get("text", "")).strip()
            sender = str((message.get("from") or {}).get("id", ""))
            if not text:
                continue
            if text.startswith("/"):
                response = self.telegram.handle_command(text, sender)
                self.storage.audit(self.run_id, "telegram_command", {"command": text.split()[0], "authorized": self.telegram.is_authorized(sender)})
                self.telegram.send_message(response, str((message.get("chat") or {}).get("id", self.telegram.chat_id)))
                continue
            
            # Live safety check before processing approvals
            if self.config.get("mode") == "live" and not self.config.get("live_enabled"):
                self.telegram.send_message("Blocked for safety: live trading is disabled.")
                continue

            pending = self.storage.active_proposals()
            parsed = parse_approval(text, sender, self.telegram.allowed_user_id or "", pending)
            approval_id = str(uuid.uuid4())
            self.storage.execute(
                "INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (approval_id, self.run_id, parsed.proposal_id, sender, text, parsed.action, int(self.telegram.is_authorized(sender)), "accepted" if parsed.accepted else "rejected", iso_now()),
            )
            if not parsed.accepted or not parsed.proposal_id:
                msg = translate_reason(parsed.reason)
                self.telegram.send_message(msg)
                continue
            if parsed.action == "reject":
                self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                self.telegram.send_message(f"Rejected. No order was placed for proposal {parsed.proposal_id[:8]}.")
                continue
            row = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (parsed.proposal_id,))[0]
            if not self.storage.consume_approval(parsed.proposal_id, approval_id):
                self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                continue
            proposal = {**json.loads(row.get("payload") or "{}"), **row, "status": "approved"}
            context = self._portfolio_context(proposal, approval_valid=True)
            result = Executor(self.broker, self._risk_engine(parsed.proposal_id, "final")).execute(proposal, context)
            self.storage.execute(
                "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, parsed.proposal_id, str(_value(result.broker_response, "id", "")) or None, result.client_order_id, proposal["symbol"], proposal["side"], proposal.get("notional"), proposal.get("qty"), result.status, json_dumps({"submitted": result.submitted, "reason": result.reason}), iso_now(), iso_now()),
            )
            
            # User-friendly order status response
            if result.status.lower() in {"filled", "filled_fully", "orderstatus.filled"}:
                self.telegram.send_message(f"Filled. The paper order for {proposal['symbol']} was completed successfully.")
            elif result.submitted:
                self.telegram.send_message(f"Approved. A paper order was submitted for {proposal['symbol']}. Current status: pending.")
            else:
                self.telegram.send_message(f"Approved, but submission failed or was blocked: {result.reason}")
        if max_id > 0:
            self.telegram.get_updates(offset=max_id + 1, timeout=0)

    def scan(self) -> None:
        if self.config.get("mode") == "live" and not self.config.get("live_enabled"):
            self.telegram.send_message("Blocked for safety: live trading is disabled.")
            return

        positions = self.broker.get_positions()
        orders = self.broker.get_open_orders()
        market_open = self.broker.is_market_open()
        strategy_config = __import__("yaml").safe_load((PROJECT_ROOT / "config" / "strategies.yaml").read_text())["rule_based_v1"]
        
        now = datetime.now(UTC)
        today_start = now.date().isoformat() + "T00:00:00"
        
        for symbol in self.config.get("watchlist", []):
            try:
                trade = self.broker.get_latest_price(symbol)
                price = float(_value(trade, "price", 0) or 0)
                price_at = _value(trade, "timestamp", now)
            except Exception:
                price = 0.0
                price_at = now
                
            bars = normalize_bars(self.broker.get_historical_bars(symbol, "1Day", 250), symbol)
            volume = float(bars.iloc[-1]["volume"]) if not bars.empty else 0.0
            
            # Write market snapshot
            self.storage.execute(
                "INSERT INTO market_snapshots(run_id,symbol,price,price_at,volume,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (self.run_id, symbol, price, price_at.isoformat() if hasattr(price_at, "isoformat") else str(price_at), volume, json_dumps({"price": price, "volume": volume}), now.isoformat())
            )
            
            has_position = any(str(_value(p, "symbol", "")).upper() == symbol for p in positions)
            has_order = any(str(_value(o, "symbol", "")).upper() == symbol for o in orders)
            signal = evaluate_symbol(symbol, bars, has_position, has_order, market_open, strategy_config["maximum_volatility_20d"], strategy_config["stop_drawdown_pct"])
            signal_id = str(uuid.uuid4())
            expiry = now + timedelta(minutes=self.config["risk"]["signal_expiry_minutes"])
            self.storage.execute("INSERT INTO signals(id,run_id,symbol,side,action,strategy_version,reason,confidence,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (signal_id, self.run_id, symbol, signal.side, signal.action, signal.strategy_version, signal.reason, signal.confidence, now.isoformat(), expiry.isoformat(), json_dumps(signal.indicators)))
            
            # Write indicators
            self.storage.execute(
                "INSERT INTO indicators(run_id,symbol,values_json,created_at) VALUES(?,?,?,?)",
                (self.run_id, symbol, json_dumps(signal.indicators), now.isoformat())
            )
            
            # Fetch previous snapshot & session start
            prev_row = self.storage.fetch_all(
                "SELECT price, score, signal FROM market_memory WHERE symbol=? ORDER BY created_at DESC LIMIT 1",
                (symbol,)
            )
            prev_price = float(prev_row[0]["price"]) if prev_row else price
            
            session_row = self.storage.fetch_all(
                "SELECT price FROM market_memory WHERE symbol=? AND created_at>=? ORDER BY created_at ASC LIMIT 1",
                (symbol, today_start)
            )
            session_start_price = float(session_row[0]["price"]) if session_row else price
            
            price_change = price - prev_price
            price_change_pct = (price / prev_price - 1) * 100 if prev_price > 0 else 0.0
            session_change = price - session_start_price
            
            # Compute score
            # 1. Rule signal strength (30)
            score_rule = 30.0 if signal.action in {"ENTRY", "EXIT"} else 0.0

            # 2. Short-term 5-minute change (15)
            score_5m = 7.5
            if prev_row:
                if signal.side == "buy":
                    score_5m = 15.0 if price > prev_price else (7.5 if price == prev_price else 0.0)
                elif signal.side == "sell":
                    score_5m = 15.0 if price < prev_price else (7.5 if price == prev_price else 0.0)
                else:
                    score_5m = 7.5 if price == prev_price else (15.0 if price > prev_price else 0.0)

            # 3. Session/day trend (15)
            score_session = 7.5
            if session_row:
                if signal.side == "buy":
                    score_session = 15.0 if price > session_start_price else (7.5 if price == session_start_price else 0.0)
                elif signal.side == "sell":
                    score_session = 15.0 if price < session_start_price else (7.5 if price == session_start_price else 0.0)
                else:
                    score_session = 7.5 if price == session_start_price else (15.0 if price > session_start_price else 0.0)

            # 4. Volatility/risk (15)
            vol_20 = signal.indicators.get("volatility_20")
            max_vol = strategy_config.get("maximum_volatility_20d", 0.05)
            score_vol = 15.0 if (vol_20 is not None and vol_20 <= max_vol) else 0.0

            # 5. Portfolio safety (15)
            port_context = self._portfolio_context({"symbol": symbol, "side": signal.side or "buy", "action": "entry"})
            safety_ok = True
            if port_context.get("duplicate_order"):
                safety_ok = False
            if port_context.get("trades_today", 0) >= self.config["risk"].get("max_trades_per_day", 1):
                safety_ok = False
            if signal.action == "ENTRY" and port_context.get("open_positions", 0) >= self.config["risk"].get("max_open_positions", 1):
                safety_ok = False
            score_safety = 15.0 if safety_ok else 0.0

            # 6. Data quality / confidence (10)
            age = (now - price_at).total_seconds() if price_at else float("inf")
            fresh_price = 0 <= age <= self.config["risk"].get("max_price_age_seconds", 120)
            enough_bars = len(bars) >= self.config["risk"].get("min_historical_bars", 50)
            score_data = 10.0 if (fresh_price and enough_bars) else 0.0

            score = float(round(score_rule + score_5m + score_session + score_vol + score_safety + score_data, 2))
            
            # Classification
            if score >= 80:
                classification = "Strong paper candidate"
            elif score >= 65:
                classification = "Moderate paper candidate"
            elif score >= 50:
                classification = "Weak / watch only"
            else:
                classification = "Do not approve / wait"
                
            ai_config = self.config.get("ai", {})
            proposal_allowed = (signal.action in {"ENTRY", "EXIT"} and score >= ai_config.get("ai_review_min_score", 65))
            
            gpt_called = False
            if proposal_allowed:
                # Count calls today
                calls_today = len(self.storage.fetch_all(
                    "SELECT id FROM ai_reviews WHERE created_at >= ?", (today_start,)
                ))
                # Count calls in current run
                calls_current_run = self.ai.calls_made
                
                # Check interval since last GPT call
                last_call = self.storage.fetch_all(
                    "SELECT created_at FROM ai_reviews WHERE proposal_id IN (SELECT id FROM trade_proposals WHERE symbol=?) ORDER BY created_at DESC LIMIT 1",
                    (symbol,)
                )
                if last_call:
                    last_call_time = datetime.fromisoformat(last_call[0]["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
                    time_since = (now - last_call_time).total_seconds() / 60
                else:
                    time_since = float("inf")
                
                # Throttling logic
                max_calls_run = ai_config.get("ai_max_calls_per_run", 2)
                daily_limit = ai_config.get("ai_daily_call_limit", 10)
                min_interval = ai_config.get("ai_review_min_interval_minutes", 30)
                
                if (ai_config.get("ai_review_on_every_run", False) or
                    (calls_today < daily_limit and calls_current_run < max_calls_run and time_since >= min_interval)):
                    gpt_called = True
            
            # Log to market_memory
            self.storage.execute(
                "INSERT INTO market_memory(run_id,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id, symbol, price, prev_price, price_change, price_change_pct,
                    session_start_price, session_change, vol_20 or 0.0, signal.action, score,
                    classification, signal.reason, int(proposal_allowed), int(gpt_called), now.isoformat()
                )
            )
            
            if not proposal_allowed:
                continue
                
            proposal_id = str(uuid.uuid4())
            proposal = {
                "id": proposal_id, "run_id": self.run_id, "signal_id": signal_id, "symbol": symbol,
                "side": signal.side, "action": "entry" if signal.action == "ENTRY" else "exit", "notional": float(self.config["risk"]["max_trade_notional_paper"]),
                "latest_price": price, "price_at": str(price_at), "historical_bars": len(bars),
                "volume": volume, "price_gap_pct": float((price / float(bars.iloc[-1]["close"]) - 1) * 100) if not bars.empty and float(bars.iloc[-1]["close"]) > 0 else 0.0,
                "created_at": now.isoformat(), "expires_at": expiry.isoformat(), "strategy_version": signal.strategy_version,
                "reason": signal.reason, "order_type": "market", "asset_class": "equity", "indicators": signal.indicators,
                "score": score, "classification": classification,
            }
            decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, self._portfolio_context(proposal))
            if not decision.passed:
                self.storage.audit(self.run_id, "proposal_blocked", {"symbol": symbol, "reasons": decision.reasons})
                continue
            self.storage.execute("INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proposal_id, self.run_id, signal_id, symbol, signal.side, proposal["notional"], "pending", now.isoformat(), expiry.isoformat(), signal.strategy_version, json_dumps(proposal)))
            
            if gpt_called:
                review = self.ai.review(proposal)
            else:
                review = deterministic_review(proposal, warning="AI review throttled to avoid spam")
                
            self.storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, proposal_id, review["summary"], json_dumps(review["risks"]), review["caution_level"], json_dumps(review), iso_now()))
            
            # Natural language proposal message
            message_text = f"Proposal {proposal_id}\n\n" + format_proposal_message(proposal, self.config)
            self.telegram.send_message(message_text)

    def run_cycle(self) -> None:
        self.process_telegram()
        if not (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            self.scan()
