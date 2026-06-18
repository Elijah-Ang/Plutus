from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ai_review import AIReviewer
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
        for symbol in self.config.get("watchlist", []):
            bars = normalize_bars(self.broker.get_historical_bars(symbol, "1Day", 250), symbol)
            has_position = any(str(_value(p, "symbol", "")).upper() == symbol for p in positions)
            has_order = any(str(_value(o, "symbol", "")).upper() == symbol for o in orders)
            signal = evaluate_symbol(symbol, bars, has_position, has_order, market_open, strategy_config["maximum_volatility_20d"], strategy_config["stop_drawdown_pct"])
            signal_id = str(uuid.uuid4())
            now = datetime.now(UTC)
            expiry = now + timedelta(minutes=self.config["risk"]["signal_expiry_minutes"])
            self.storage.execute("INSERT INTO signals(id,run_id,symbol,side,action,strategy_version,reason,confidence,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (signal_id, self.run_id, symbol, signal.side, signal.action, signal.strategy_version, signal.reason, signal.confidence, now.isoformat(), expiry.isoformat(), json_dumps(signal.indicators)))
            if signal.action == "HOLD":
                continue
            trade = self.broker.get_latest_price(symbol)
            price = float(_value(trade, "price", 0) or 0)
            price_at = _value(trade, "timestamp", now)
            proposal_id = str(uuid.uuid4())
            proposal = {
                "id": proposal_id, "run_id": self.run_id, "signal_id": signal_id, "symbol": symbol,
                "side": signal.side, "action": "entry" if signal.action == "ENTRY" else "exit", "notional": float(self.config["risk"]["max_trade_notional_paper"]),
                "latest_price": price, "price_at": str(price_at), "historical_bars": len(bars),
                "volume": float(bars.iloc[-1]["volume"]), "price_gap_pct": float((price / float(bars.iloc[-1]["close"]) - 1) * 100),
                "created_at": now.isoformat(), "expires_at": expiry.isoformat(), "strategy_version": signal.strategy_version,
                "reason": signal.reason, "order_type": "market", "asset_class": "equity", "indicators": signal.indicators,
            }
            decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, self._portfolio_context(proposal))
            if not decision.passed:
                self.storage.audit(self.run_id, "proposal_blocked", {"symbol": symbol, "reasons": decision.reasons})
                continue
            self.storage.execute("INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proposal_id, self.run_id, signal_id, symbol, signal.side, proposal["notional"], "pending", now.isoformat(), expiry.isoformat(), signal.strategy_version, json_dumps(proposal)))
            review = self.ai.review(proposal)
            self.storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, proposal_id, review["summary"], json_dumps(review["risks"]), review["caution_level"], json_dumps(review), iso_now()))
            
            # Natural language proposal message
            message_text = f"Proposal {proposal_id}\n\n" + format_proposal_message(proposal, self.config)
            self.telegram.send_message(message_text)

    def run_cycle(self) -> None:
        self.process_telegram()
        if not (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            self.scan()
