from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ai_review import AIReviewer, deterministic_review
from .capabilities import AUTO_EXECUTION_SUPPORTED

logger = logging.getLogger("trading_agent")

from .approval_parser import parse_approval
from .execution import Executor, ExecutionResult
from .internet import internet_available
from .market_data import normalize_bars
from .power import get_power_status
from .risk_engine import RiskCheck, RiskEngine, _dt
from .reconciliation import BrokerReconciler
from .strategy_rule_based import evaluate_symbol
from .telegram_bot import TelegramBot
from .utils import PROJECT_ROOT, iso_now, json_dumps, format_proposal_message, translate_reason, format_sgt


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if not isinstance(obj, dict) else obj.get(name, default)


class TradingService:
    """One bounded launchd cycle. AI never receives a broker or execution object."""

    def __init__(self, config: dict[str, Any], storage: Any, broker: Any, run_id: str) -> None:
        self.config, self.storage, self.broker, self.run_id = config, storage, broker, run_id
        telegram = TelegramBot()
        self.telegram = telegram
        self.ai = AIReviewer(config.get("ai", {}))
        self._context_cache: tuple[float, dict[str, Any]] | None = None
        self._auto_block_audited = False

    def _risk_engine(self, proposal_id: str, stage: str) -> RiskEngine:
        return RiskEngine(self.config, lambda c: self.storage.record_check(self.run_id, c.name, c.passed, c.reason, proposal_id, stage))

    def _authoritative_runtime_state(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._context_cache and now - self._context_cache[0] <= 15:
            return self._context_cache[1]

        telegram_health = getattr(self.telegram, "is_available", None)
        state: dict[str, Any] = {
            "internet_available": internet_available(),
            "database_writable": self.storage.writable(),
            "telegram_available": bool(telegram_health(force=force)) if callable(telegram_health) else False,
            "broker_available": False,
            "market_open": False,
            "account": None,
            "positions": [],
            "orders": [],
            "daily_loss": None,
            "weekly_loss": None,
            "uses_margin": None,
        }
        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            orders = self.broker.get_open_orders()
            get_clock = getattr(self.broker, "get_clock", None)
            clock = get_clock() if callable(get_clock) else None
            market_open = bool(clock.is_open) if clock is not None else bool(self.broker.is_market_open())
            state.update(
                account=account,
                positions=positions,
                orders=orders,
                broker_available=True,
                market_open=market_open,
            )

            try:
                losses = self.broker.get_loss_metrics()
                state["daily_loss"] = losses.get("daily_loss")
                state["weekly_loss"] = losses.get("weekly_loss")
            except Exception:
                # Daily equity comparison is still authoritative when present.
                equity = _value(account, "equity")
                last_equity = _value(account, "last_equity")
                if equity is not None and last_equity is not None:
                    state["daily_loss"] = max(0.0, float(last_equity) - float(equity))

            cash = _value(account, "cash")
            equity = _value(account, "equity")
            long_value = _value(account, "long_market_value")
            short_value = _value(account, "short_market_value")
            if all(value is not None for value in (cash, equity, long_value, short_value)):
                state["uses_margin"] = (
                    float(cash) < 0
                    or float(short_value) < 0
                    or float(long_value) > float(equity) + 0.01
                )
        except Exception:
            # Unknown broker/account state stays unknown and blocks risk checks.
            pass

        self._context_cache = (now, state)
        return state

    def _portfolio_context(self, proposal: dict[str, Any], approval_valid: bool = False) -> dict[str, Any]:
        state = self._authoritative_runtime_state(force=approval_valid)
        positions = state["positions"]
        orders = state["orders"]
        account = state["account"]
        symbol = proposal["symbol"]
        today_orders = self.storage.fetch_all("SELECT id FROM orders WHERE substr(created_at,1,10)=?", (datetime.now(UTC).date().isoformat(),))
        return {
            "power_connected": get_power_status().connected is True,
            "internet_available": state["internet_available"],
            "database_writable": state["database_writable"],
            "broker_available": state["broker_available"],
            "telegram_available": state["telegram_available"],
            "market_open": state["market_open"],
            "kill_switch": (PROJECT_ROOT / "config" / "KILL_SWITCH").exists(),
            "open_positions": len(positions), "trades_today": len(today_orders),
            "duplicate_order": any(str(_value(o, "symbol", "")).upper() == symbol for o in orders),
            "same_symbol_position": any(str(_value(p, "symbol", "")).upper() == symbol for p in positions),
            "uses_margin": state["uses_margin"],
            "daily_loss": state["daily_loss"],
            "weekly_loss": state["weekly_loss"],
            "buying_power": float(_value(account, "buying_power", 0) or 0) if account is not None else 0.0,
            "approval_valid": approval_valid,
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

            reply_to = message.get("reply_to_message") or {}
            reply_to_message_id = reply_to.get("message_id")
            
            # Determine targeting method
            targeting_method = None
            if reply_to_message_id is not None:
                targeting_method = "reply_to"
            else:
                normalized = " ".join(text.lower().strip().split())
                reject_words = r"(?:no|reject|rejected)(?: thanks)?"
                approve_words = r"(?:yes|approve|approved)(?: please)?"
                is_plain_reject = bool(re.fullmatch(reject_words, normalized))
                is_plain_approve = bool(re.fullmatch(approve_words, normalized))
                if is_plain_approve or is_plain_reject:
                    targeting_method = "single_pending"
                else:
                    reject_match = re.fullmatch(reject_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9-]+))?", normalized)
                    approve_match = re.fullmatch(approve_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9-]+))?", normalized)
                    match_obj = approve_match or reject_match
                    if match_obj:
                        side, symbol, proposal_id = match_obj.groups()
                        if proposal_id:
                            targeting_method = "proposal_id"
                        elif symbol:
                            targeting_method = "symbol"
                        else:
                            targeting_method = "single_pending"
                            
            pending = self.storage.active_proposals()
            
            # Check reply-to targeting validations (wrong/expired/handled proposals)
            if reply_to_message_id is not None:
                proposal_rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE telegram_message_id=?", (str(reply_to_message_id),))
                if not proposal_rows:
                    self.telegram.send_message("I did not take any action because I could not match your reply to a single pending proposal. Please specify the proposal ID or symbol.")
                    continue
                    
                proposal_row = proposal_rows[0]
                prop_status = proposal_row.get("status")
                prop_symbol = proposal_row.get("symbol", "")
                prop_side = proposal_row.get("side", "").upper()
                
                # Check text matches yes/no action
                normalized = " ".join(text.lower().strip().split())
                reject_words = r"(?:no|reject|rejected)(?: thanks)?"
                approve_words = r"(?:yes|approve|approved)(?: please)?"
                is_approve = bool(re.fullmatch(approve_words, normalized)) or bool(re.fullmatch(approve_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9-]+))?", normalized))
                is_reject = bool(re.fullmatch(reject_words, normalized)) or bool(re.fullmatch(reject_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9-]+))?", normalized))
                
                if not is_approve and not is_reject:
                    self.telegram.send_message("I did not take any action because I could not tell whether you meant yes or no. Please reply yes to approve or no to reject.")
                    continue
                    
                if prop_status == "expired":
                    self.telegram.send_message("⏳ This proposal has already expired. No order was placed.")
                    continue
                elif prop_status in ("approved", "rejected", "superseded"):
                    self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                    continue
            
            # Check plain yes/no ambiguity
            normalized = " ".join(text.lower().strip().split())
            reject_words = r"(?:no|reject|rejected)(?: thanks)?"
            approve_words = r"(?:yes|approve|approved)(?: please)?"
            is_plain_reject = bool(re.fullmatch(reject_words, normalized))
            is_plain_approve = bool(re.fullmatch(approve_words, normalized))
            
            if reply_to_message_id is None and (is_plain_approve or is_plain_reject):
                if len(pending) > 1:
                    self.telegram.send_message("I found multiple pending proposals. Please reply directly to the proposal message, or include the symbol/proposal ID.")
                    continue
                elif len(pending) == 0:
                    self.telegram.send_message("I did not take any action because I could not match your reply to a single pending proposal. Please specify the proposal ID or symbol.")
                    continue

            parsed = parse_approval(
                text,
                sender,
                self.telegram.allowed_user_id or "",
                pending,
                reply_to_message_id=reply_to_message_id
            )
            
            # If not authorized, ignore
            if parsed.reason == "unauthorized sender":
                continue
                
            approval_id = str(uuid.uuid4())
            ack_status = "rejected" if parsed.action == "reject" else "received"
            approval_received_at = iso_now()
            
            self.storage.execute(
                "INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at,reply_to_message_id,proposal_targeting_method,acknowledgement_status,approval_received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (approval_id, self.run_id, parsed.proposal_id, sender, text, parsed.action, int(self.telegram.is_authorized(sender)), "accepted" if parsed.accepted else "rejected", iso_now(), str(reply_to_message_id) if reply_to_message_id is not None else None, targeting_method, ack_status, approval_received_at),
            )
            
            if not parsed.accepted or not parsed.proposal_id:
                msg = translate_reason(parsed.reason)
                self.telegram.send_message(msg)
                
                # Update delay for non-accepted/expired/ambiguous updates
                ack_sent = iso_now()
                delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))
                continue
                
            row = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (parsed.proposal_id,))[0]
            prop_symbol = row.get("symbol", "")
            prop_side = row.get("side", "").lower()
            
            if parsed.action == "reject":
                self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                self.telegram.send_message(f"❌ Received: NO for {prop_symbol} paper {prop_side} proposal. Proposal rejected. No order will be placed.")
                ack_sent = iso_now()
                delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))
                continue
                
            # Send immediate acknowledgement message for YES
            self.telegram.send_message(f"✅ Received: YES for {prop_symbol} paper {prop_side} proposal. I will now run the final safety check. No order will be placed unless the final check passes.")
            ack_sent = iso_now()
            delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
            self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))
            
            if not self.storage.consume_approval(parsed.proposal_id, approval_id):
                self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                continue
                
            final_revalidation_started_at = iso_now()
            
            # Retrieve parameters from config
            telegram_cfg = self.config.get("telegram", {})
            refresh_required = telegram_cfg.get("approval_price_refresh_required", True)
            max_price_age = telegram_cfg.get("approval_max_price_age_seconds", 60)
            max_price_move_bps = telegram_cfg.get("approval_max_price_move_bps", 25)
            
            refreshed_price_val = None
            refreshed_price_at = None
            price_refreshed_at = None
            refreshed_price_age_seconds = None
            price_move_bps_since_proposal = None
            block_reason = None
            
            now_dt = datetime.now(UTC)
            
            # Fetch latest price
            if self.broker is not None:
                try:
                    trade = self.broker.get_latest_price(prop_symbol)
                    refreshed_price_val = float(_value(trade, "price", 0) or 0)
                    refreshed_price_at = _dt(_value(trade, "timestamp", now_dt))
                    if refreshed_price_at:
                        price_refreshed_at = refreshed_price_at.isoformat()
                        refreshed_price_age_seconds = (now_dt - refreshed_price_at).total_seconds()
                except Exception as e:
                    logger.warning("Failed to refresh price for symbol %s: %s", prop_symbol, e)
            
            # Check market open status
            market_open = False
            if self.broker is not None:
                try:
                    market_open = self.broker.is_market_open()
                except Exception:
                    market_open = False
            
            # Get the proposal price
            proposal_price = None
            try:
                proposal_payload = json.loads(row.get("payload") or "{}")
                proposal_price = proposal_payload.get("latest_price")
            except Exception:
                pass
            if proposal_price is None:
                proposal_price = row.get("price")
                
            # Perform revalidation checks
            if refresh_required:
                if refreshed_price_val is None or refreshed_price_val <= 0:
                    block_reason = "Price refresh failed or price is unavailable"
                elif refreshed_price_age_seconds is None or refreshed_price_age_seconds > max_price_age or refreshed_price_age_seconds < -5:
                    block_reason = "The proposal price is no longer fresh, so the system refused to trade on stale data. A new proposal is required."
                elif not market_open:
                    block_reason = "Market is closed"
                elif proposal_price is not None and proposal_price > 0:
                    price_move_bps_since_proposal = (abs(refreshed_price_val - proposal_price) / proposal_price) * 10000
                    # Apply price movement limit
                    if price_move_bps_since_proposal > max_price_move_bps:
                        block_reason = f"Price moved too much ({price_move_bps_since_proposal:.1f} bps > limit {max_price_move_bps} bps)"
            else:
                if not market_open:
                    block_reason = "Market is closed"
            
            proposal = {**json.loads(row.get("payload") or "{}"), **row, "status": "approved"}
            if block_reason:
                # Set up mock Executor result for blocked order
                result = ExecutionResult(False, "blocked", f"ta-{uuid.uuid4().hex[:24]}", reason=block_reason)
            else:
                # Update proposal dict with fresh price data so risk engine evaluates using it
                if refreshed_price_val is not None:
                    proposal["latest_price"] = refreshed_price_val
                if refreshed_price_at is not None:
                    proposal["price_at"] = refreshed_price_at.isoformat()
                    
                context = self._portfolio_context(proposal, approval_valid=True)
                
                # Execute
                result = Executor(self.broker, self._risk_engine(parsed.proposal_id, "final")).execute(proposal, context)
                
            final_revalidation_completed_at = iso_now()
            
            # Record order decision
            final_order_decision = "submitted" if result.submitted else "blocked"
            final_block_reason = result.reason if not result.submitted else None
            
            self.storage.execute(
                "UPDATE approvals SET final_revalidation_started_at=?, final_revalidation_completed_at=?, price_refreshed_at=?, refreshed_price=?, refreshed_price_age_seconds=?, price_move_bps_since_proposal=?, final_order_decision=?, final_block_reason=? WHERE id=?",
                (
                    final_revalidation_started_at,
                    final_revalidation_completed_at,
                    price_refreshed_at,
                    refreshed_price_val,
                    refreshed_price_age_seconds,
                    price_move_bps_since_proposal,
                    final_order_decision,
                    final_block_reason,
                    approval_id
                )
            )
            
            self.storage.execute(
                "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, parsed.proposal_id, str(_value(result.broker_response, "id", "")) or None, result.client_order_id, proposal.get("symbol", prop_symbol), proposal.get("side", prop_side), proposal.get("notional"), proposal.get("qty"), result.status, json_dumps({"submitted": result.submitted, "reason": result.reason}), iso_now(), iso_now()),
            )
            
            if result.submitted:
                self.storage.execute("UPDATE approvals SET acknowledgement_status='submitted' WHERE id=?", (approval_id,))
                notional_val = proposal.get("notional", 5)
                qty_val = proposal.get("qty")
                if prop_side == "buy":
                    self.telegram.send_message(f"✅ Paper order submitted: Buy {prop_symbol} for ${notional_val:.0f}. Mode: paper only.")
                else:
                    qty_str = f"{qty_val} shares" if qty_val is not None else f"${notional_val:.0f}"
                    self.telegram.send_message(f"✅ Paper order submitted: Sell {prop_symbol} for {qty_str}. Mode: paper only.")
                    
                if prop_side == "buy":
                    other_buys = self.storage.fetch_all("SELECT id FROM trade_proposals WHERE side='buy' AND status='pending' AND id != ?", (parsed.proposal_id,))
                    if other_buys:
                        self.storage.execute("UPDATE trade_proposals SET status='superseded' WHERE side='buy' AND status='pending' AND id != ?", (parsed.proposal_id,))
                        self.telegram.send_message("Other pending BUY proposals were cancelled because one paper position/trade is already active.")
            else:
                self.storage.execute("UPDATE approvals SET acknowledgement_status='blocked' WHERE id=?", (approval_id,))
                if "refused to trade on stale data" in result.reason:
                    self.telegram.send_message(f"Approved, but no order was placed. {result.reason}")
                else:
                    self.telegram.send_message(f"⚠️ Approved, but no order was placed. Reason: {result.reason}.")
        if max_id > 0:
            self.telegram.get_updates(offset=max_id + 1, timeout=0)

    def _should_auto_execute(self, proposal: dict[str, Any]) -> bool:
        # Quarantined: YAML cannot enable this unsupported capability.
        requested = self.config.get("auto_execution_enabled", False) or self.config.get("auto_execution_mode") != "manual_only"
        if requested and not self._auto_block_audited:
            self.storage.audit(self.run_id, "auto_execution_blocked", {"reason": "unsupported capability"})
            self._auto_block_audited = True
        assert AUTO_EXECUTION_SUPPORTED is False
        return False

    def notify_expired_proposals(self) -> None:
        # Find all expired proposals that haven't been notified yet
        expired_rows = self.storage.fetch_all(
            "SELECT * FROM trade_proposals WHERE status='expired' AND (expiry_notified=0 OR expiry_notified IS NULL)"
        )
        for row in expired_rows:
            proposal_id = row["id"]
            symbol = row["symbol"]
            expires_at = row["expires_at"]
            expires_fmt = format_sgt(expires_at)
            
            msg = (
                f"⏳ Proposal expired\n\n"
                f"The {symbol} paper trade proposal expired at {expires_fmt}.\n"
                f"No order was placed.\n"
                f"Reason: no yes/no reply before expiry."
            )
            self.telegram.send_message(msg)
            
            self.storage.execute(
                "UPDATE trade_proposals SET expiry_notified=1 WHERE id=?",
                (proposal_id,)
            )
            self.storage.audit(
                self.run_id,
                "proposal_expiry_notified",
                {"proposal_id": proposal_id, "symbol": symbol, "expires_at": expires_at}
            )

    def _calculate_asset_selection_score(self, symbol: str, bars: Any, price_at: Any, signal: Any, now: Any, spy_ret_20d: float | None = None) -> float:
        import pandas as pd
        # 1. Liquidity/spread quality (max 20)
        score_liq = 10.0
        if isinstance(bars, pd.DataFrame) and not bars.empty and "volume" in bars.columns:
            avg_vol = float(bars["volume"].tail(20).mean())
            if avg_vol >= 1000000:
                score_liq = 20.0
            elif avg_vol >= 500000:
                score_liq = 15.0
            elif avg_vol >= 100000:
                score_liq = 10.0
            else:
                score_liq = 5.0
                
        # 2. Trend strength (max 20)
        score_trend = 10.0
        if isinstance(bars, pd.DataFrame) and not bars.empty and len(bars) >= 50 and "close" in bars.columns:
            close = float(bars["close"].iloc[-1])
            ma_50 = float(bars["close"].tail(50).mean())
            ma_200 = float(bars["close"].tail(200).mean()) if len(bars) >= 200 else None
            if ma_200 is not None:
                if close > ma_50 and ma_50 > ma_200:
                    score_trend = 20.0
                elif close > ma_50:
                    score_trend = 15.0
                elif close > ma_200:
                    score_trend = 10.0
                else:
                    score_trend = 5.0
            else:
                score_trend = 15.0 if close > ma_50 else 5.0
                
        # 3. Volatility sanity (max 20)
        score_vol = 10.0
        vol_20 = signal.indicators.get("volatility_20")
        if vol_20 is not None and isinstance(vol_20, (int, float)) and vol_20 > 0:
            if 0.05 <= vol_20 <= 0.35:
                score_vol = 20.0
            elif 0.02 <= vol_20 <= 0.50:
                score_vol = 12.0
            else:
                score_vol = 5.0
                
        # 4. Relative strength vs SPY (max 15)
        score_rel = 10.0
        if isinstance(bars, pd.DataFrame) and not bars.empty and len(bars) >= 20 and "close" in bars.columns:
            ret_20d = float(bars["close"].iloc[-1] / bars["close"].iloc[-20]) - 1.0
            if spy_ret_20d is not None:
                if ret_20d > spy_ret_20d:
                    score_rel = 15.0
                elif ret_20d == spy_ret_20d:
                    score_rel = 10.0
                else:
                    score_rel = 5.0
                    
        # 5. Signal confirmation (max 15)
        score_sig = 5.0
        if signal.action in {"ENTRY", "EXIT"}:
            score_sig = 15.0
        elif signal.side in {"buy", "sell"}:
            score_sig = 10.0
            
        # 6. Data quality/confidence (max 10)
        age = (now - price_at).total_seconds() if price_at else float("inf")
        fresh_price = -5 <= age <= 120
        enough_bars = isinstance(bars, pd.DataFrame) and len(bars) >= 50
        if fresh_price and enough_bars:
            score_data = 10.0
        elif fresh_price:
            score_data = 5.0
        else:
            score_data = 2.0
            
        total = score_liq + score_trend + score_vol + score_rel + score_sig + score_data
        return float(round(total, 2))

    def _classify_asset_score(self, score: float) -> str:
        if score >= 80:
            return "Strong approved-universe candidate"
        if score >= 65:
            return "Moderate approved-universe candidate"
        if score >= 50:
            return "Watch only"
        return "Do not prioritize"

    def _classify_trade_score(self, score: float) -> str:
        if score >= 90:
            return "Very strong paper setup"
        if score >= 80:
            return "Strong paper setup"
        if score >= 65:
            return "Moderate paper setup"
        if score >= 50:
            return "Weak setup, watch only"
        return "No action suggested"

    def _calculate_expiry_minutes(self, symbol: str, signal: Any, vol_20: float | None, score: float, price_at: datetime, now: datetime) -> int:
        default_exp = self.config.get("proposal_expiry_default_minutes", 15)
        high_vol_thresh = self.config.get("proposal_expiry_high_volatility_threshold", 0.20)
        low_vol_thresh = self.config.get("proposal_expiry_low_volatility_threshold", 0.12)
        
        is_exit = signal.action == "EXIT" or signal.side == "sell"
        
        # Base expiry depending on Volatility and Action type
        if vol_20 is None or not isinstance(vol_20, (int, float)) or vol_20 <= 0:
            expiry_minutes = 10 if is_exit else default_exp
        elif vol_20 >= high_vol_thresh:
            expiry_minutes = 5
        elif vol_20 <= low_vol_thresh:
            expiry_minutes = 10 if is_exit else 20
        else:
            expiry_minutes = 10 if is_exit else default_exp

        # Dependency on setup confidence
        if score < 65:  # Weak setup
            expiry_minutes -= 2
        elif score >= 90:  # Very strong setup
            expiry_minutes += 2

        # Dependency on data freshness
        age = (now - price_at).total_seconds() if price_at else float("inf")
        if age > 60:
            expiry_minutes -= 3

        # Dependency on market session state (if close is within 20 mins)
        try:
            clock = self.broker.get_clock()
            if clock and clock.is_open:
                time_until_close = (clock.next_close - clock.timestamp).total_seconds() / 60
                if time_until_close < expiry_minutes:
                    expiry_minutes = max(5, int(time_until_close))
        except Exception:
            # Unknown close time must shorten, never extend, a proposal window.
            expiry_minutes = min(expiry_minutes, 5)

        # Hard boundaries
        return max(5, min(20, expiry_minutes))


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
        
        profiles = self.config.get("market_profiles", {})
        if not profiles:
            # Fallback if config has no profiles
            profiles = {
                "default": {
                    "status": "active",
                    "broker": "alpaca",
                    "watchlist": self.config.get("watchlist", []),
                    "observation_watchlist": [],
                    "proposals_enabled": True,
                    "execution_enabled": True
                }
            }

        for profile_key, profile in profiles.items():
            status = profile.get("status", "disabled")
            if status == "disabled":
                continue
                
            broker_name = profile.get("broker")
            if broker_name != "alpaca":
                # Report data_source_missing safely and skip
                self.storage.audit(
                    self.run_id,
                    "data_source_missing",
                    {"profile": profile_key, "broker": broker_name}
                )
                continue
                
            active_watchlist = profile.get("watchlist", [])
            obs_watchlist = profile.get("observation_watchlist", [])
            proposals_enabled = profile.get("proposals_enabled", False)
            
            all_symbols = list(dict.fromkeys(active_watchlist + obs_watchlist))
            
            spy_ret_20d = None
            if "SPY" in all_symbols or any(p.get("watchlist") and "SPY" in p.get("watchlist") for p in profiles.values()):
                try:
                    spy_bars = normalize_bars(self.broker.get_historical_bars("SPY", "1Day", 50), "SPY")
                    if not spy_bars.empty and len(spy_bars) >= 20:
                        spy_ret_20d = float(spy_bars["close"].iloc[-1] / spy_bars["close"].iloc[-20]) - 1.0
                except Exception:
                    pass
            
            profile_results = []
            
            for symbol in all_symbols:
                try:
                    trade = self.broker.get_latest_price(symbol)
                    price = float(_value(trade, "price", 0) or 0)
                    price_at = _value(trade, "timestamp", now)
                except Exception:
                    price = 0.0
                    price_at = now
                    
                bars = normalize_bars(self.broker.get_historical_bars(symbol, "1Day", 250), symbol)
                volume = float(bars.iloc[-1]["volume"]) if not bars.empty else 0.0
                
                self.storage.execute(
                    "INSERT INTO market_snapshots(run_id,symbol,price,price_at,volume,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                    (self.run_id, symbol, price, price_at.isoformat() if hasattr(price_at, "isoformat") else str(price_at), volume, json_dumps({"price": price, "volume": volume}), now.isoformat())
                )
                
                has_position = any(str(_value(p, "symbol", "")).upper() == symbol for p in positions)
                has_order = any(str(_value(o, "symbol", "")).upper() == symbol for o in orders)
                signal = evaluate_symbol(symbol, bars, has_position, has_order, market_open, strategy_config["maximum_volatility_20d"], strategy_config["stop_drawdown_pct"])
                signal_id = str(uuid.uuid4())
                
                vol_20 = signal.indicators.get("volatility_20")
                asset_score = self._calculate_asset_selection_score(symbol, bars, price_at, signal, now, spy_ret_20d)
                asset_classification = self._classify_asset_score(asset_score)
                
                prev_row = self.storage.fetch_all("SELECT price, score, signal FROM market_memory WHERE symbol=? ORDER BY created_at DESC LIMIT 1", (symbol,))
                prev_price = float(prev_row[0]["price"]) if prev_row else price
                session_row = self.storage.fetch_all("SELECT price FROM market_memory WHERE symbol=? AND created_at>=? ORDER BY created_at ASC LIMIT 1", (symbol, today_start))
                session_start_price = float(session_row[0]["price"]) if session_row else price
                price_change = price - prev_price
                price_change_pct = (price / prev_price - 1) * 100 if prev_price > 0 else 0.0
                session_change = price - session_start_price
                session_change_pct = (price / session_start_price - 1) * 100 if session_start_price > 0 else 0.0
                
                # Part B scoring weighting:
                # 1. Strategy signal strength (max 25)
                score_rule = 25.0 if signal.action in {"ENTRY", "EXIT"} else 0.0
                
                # 2. Asset selection/rank (max 15)
                score_asset = 15.0 if asset_score >= 80 else (12.0 if asset_score >= 65 else (8.0 if asset_score >= 50 else 3.0))
                
                # 3. Recent 10-minute movement (max 10)
                score_5m = 5.0
                if prev_row:
                    if signal.side == "buy":
                        score_5m = 10.0 if price > prev_price else (5.0 if price == prev_price else 0.0)
                    elif signal.side == "sell":
                        score_5m = 10.0 if price < prev_price else (5.0 if price == prev_price else 0.0)
                    else:
                        score_5m = 5.0 if price == prev_price else (10.0 if price > prev_price else 0.0)
                
                # 4. Session trend (max 10)
                score_session = 5.0
                if session_row:
                    if signal.side == "buy":
                        score_session = 10.0 if price > session_start_price else (5.0 if price == session_start_price else 0.0)
                    elif signal.side == "sell":
                        score_session = 10.0 if price < session_start_price else (5.0 if price == session_start_price else 0.0)
                    else:
                        score_session = 5.0 if price == session_start_price else (10.0 if price > session_start_price else 0.0)
                
                # 5. Volatility sanity (max 15)
                volatility_regime = "unknown"
                volatility_gate_result = "fail-safe HOLD"
                if vol_20 is None:
                    score_vol = 0.0
                    volatility_regime = "missing"
                    volatility_gate_result = "fail-safe HOLD"
                elif vol_20 > 0.45:
                    score_vol = 0.0
                    volatility_regime = "extreme"
                    volatility_gate_result = "blocked"
                elif vol_20 > 0.35:
                    score_vol = 5.0
                    volatility_regime = "high"
                    volatility_gate_result = "watch only"
                elif vol_20 >= 0.25:
                    score_vol = 10.0
                    volatility_regime = "elevated"
                    volatility_gate_result = "eligible"
                elif vol_20 >= 0.08:
                    score_vol = 15.0
                    volatility_regime = "normal"
                    volatility_gate_result = "eligible"
                elif vol_20 >= 0.0:
                    score_vol = 8.0
                    volatility_regime = "too quiet"
                    volatility_gate_result = "eligible"
                else:
                    score_vol = 0.0
                    volatility_regime = "unknown"
                    volatility_gate_result = "fail-safe HOLD"
                
                # 6. Risk safety (max 15)
                port_context = self._portfolio_context({"symbol": symbol, "side": signal.side or "buy", "action": "entry"})
                safety_ok = True
                if port_context.get("duplicate_order") or port_context.get("trades_today", 0) >= self.config["risk"].get("max_trades_per_day", 1):
                    safety_ok = False
                if signal.action == "ENTRY" and port_context.get("open_positions", 0) >= self.config["risk"].get("max_open_positions", 1):
                    safety_ok = False
                score_safety = 15.0 if safety_ok else 0.0
                
                # 7. Data quality/freshness (max 10)
                age = (now - price_at).total_seconds() if price_at else float("inf")
                fresh_price = -5 <= age <= self.config["risk"].get("max_price_age_seconds", 120)
                enough_bars = len(bars) >= self.config["risk"].get("min_historical_bars", 50)
                score_data = 10.0 if (fresh_price and enough_bars) else (5.0 if fresh_price else 0.0)
                
                # Calculate final trade decision score
                score = float(round(score_rule + score_asset + score_5m + score_session + score_vol + score_safety + score_data, 2))
                classification = self._classify_trade_score(score)
                
                # System confidence
                system_confidence = "No action suggested"
                if score >= 90:
                    system_confidence = "Very strong"
                elif score >= 80:
                    system_confidence = "Strong"
                elif score >= 65:
                    system_confidence = "Moderate"
                elif score >= 50:
                    system_confidence = "Weak"
                
                # Dynamic expiry
                expiry_minutes = self._calculate_expiry_minutes(symbol, signal, vol_20, score, price_at, now)
                
                high_vol_thresh = self.config.get("proposal_expiry_high_volatility_threshold", 0.20)
                low_vol_thresh = self.config.get("proposal_expiry_low_volatility_threshold", 0.12)
                if vol_20 is None or not isinstance(vol_20, (int, float)) or vol_20 <= 0:
                    volatility_class = "normal"
                elif vol_20 >= high_vol_thresh:
                    volatility_class = "high"
                elif vol_20 <= low_vol_thresh:
                    volatility_class = "low"
                else:
                    volatility_class = "normal"
                
                expiry = now + timedelta(minutes=expiry_minutes)
                
                self.storage.execute("INSERT INTO signals(id,run_id,symbol,side,action,strategy_version,reason,confidence,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (signal_id, self.run_id, symbol, signal.side, signal.action, signal.strategy_version, signal.reason, signal.confidence, now.isoformat(), expiry.isoformat(), json_dumps(signal.indicators)))
                self.storage.execute("INSERT INTO indicators(run_id,symbol,values_json,created_at) VALUES(?,?,?,?)", (self.run_id, symbol, json_dumps(signal.indicators), now.isoformat()))
                
                profile_results.append({
                    "symbol": symbol,
                    "price": price,
                    "price_at": price_at,
                    "bars": bars,
                    "volume": volume,
                    "has_position": has_position,
                    "has_order": has_order,
                    "signal": signal,
                    "signal_id": signal_id,
                    "vol_20": vol_20,
                    "expiry_minutes": expiry_minutes,
                    "volatility_class": volatility_class,
                    "expiry": expiry,
                    "prev_price": prev_price,
                    "price_change": price_change,
                    "price_change_pct": price_change_pct,
                    "session_start_price": session_start_price,
                    "session_change": session_change,
                    "session_change_pct": session_change_pct,
                    "score": score,
                    "classification": classification,
                    "system_confidence": system_confidence,
                    "asset_score": asset_score,
                    "asset_classification": asset_classification,
                    "score_vol": score_vol,
                    "volatility_regime": volatility_regime,
                    "volatility_gate_result": volatility_gate_result,
                })

            # Populate symbol rank and total active symbols first
            for idx, res in enumerate(profile_results):
                res["symbol_rank"] = idx + 1
                res["total_active_symbols"] = len(active_watchlist)

            risk_cfg = self.config.get("risk", {})
            max_new_buy_per_cycle = risk_cfg.get("max_new_buy_proposals_per_cycle", 1)
            max_pending_buy = risk_cfg.get("max_pending_buy_proposals", 1)
            
            # Determine which BUY candidates are eligible before we process/filter them
            buy_candidates = []
            for res in profile_results:
                symbol = res["symbol"]
                signal = res["signal"]
                score = res["score"]
                
                # Check deduplication first to see if it would be allowed
                tmp_dedupe_status = "allowed"
                tmp_dedupe_reason = "allowed"
                pending_proposals = self.storage.fetch_all(
                    "SELECT * FROM trade_proposals WHERE symbol=? AND side=? AND status='pending'",
                    (symbol, signal.side)
                )
                if pending_proposals:
                    tmp_dedupe_status = "suppressed"
                    tmp_dedupe_reason = "pending_proposal"
                else:
                    last_prop_rows = self.storage.fetch_all(
                        "SELECT * FROM trade_proposals WHERE symbol=? AND side=? ORDER BY created_at DESC LIMIT 1",
                        (symbol, signal.side)
                    )
                    if last_prop_rows:
                        last_prop = last_prop_rows[0]
                        last_created_at = datetime.fromisoformat(last_prop["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
                        elapsed_mins = (now - last_created_at).total_seconds() / 60
                        try:
                            payload_dict = json.loads(last_prop["payload"])
                            last_score = float(payload_dict.get("score", 0))
                        except Exception:
                            last_score = 0.0
                            
                        is_exit = (signal.action == "EXIT" or signal.side == "sell")
                        if elapsed_mins < 60:
                            if is_exit and res["has_position"]:
                                pass
                            elif score >= last_score + 10:
                                pass
                            else:
                                tmp_dedupe_status = "suppressed"
                                tmp_dedupe_reason = "cooldown"
                                
                res["tmp_dedupe_status"] = tmp_dedupe_status
                res["tmp_dedupe_reason"] = tmp_dedupe_reason
                
                ai_config = self.config.get("ai", {})
                is_eligible_buy = (
                    symbol in active_watchlist 
                    and proposals_enabled 
                    and signal.action == "ENTRY" 
                    and signal.side == "buy" 
                    and score >= ai_config.get("ai_review_min_score", 65)
                    and tmp_dedupe_status == "allowed"
                )
                if is_eligible_buy:
                    buy_candidates.append(res)
            
            def get_vol_regime_rank(regime):
                order = ["normal", "too quiet", "elevated", "high", "extreme"]
                try:
                    return order.index(regime)
                except ValueError:
                    return len(order)
                    
            def buy_sort_key(candidate):
                return (
                    -candidate["score"],
                    -candidate["asset_score"],
                    candidate["symbol_rank"],
                    get_vol_regime_rank(candidate["volatility_regime"]),
                    -candidate["price_change_pct"],
                    -candidate["session_change_pct"],
                    candidate["symbol"]
                )
                
            buy_candidates.sort(key=buy_sort_key)
            
            pending_in_db = self.storage.fetch_all("SELECT COUNT(*) as cnt FROM trade_proposals WHERE side='buy' AND status='pending'")[0]["cnt"]
            allowed_new_buys = max(0, min(max_new_buy_per_cycle, max_pending_buy - pending_in_db))
            
            allowed_buy_symbols = {c["symbol"] for c in buy_candidates[:allowed_new_buys]}
            suppressed_buy_symbols = {c["symbol"] for c in buy_candidates[allowed_new_buys:]}

            any_generated = False
            
            for idx, res in enumerate(profile_results):
                rank = res["symbol_rank"]
                symbol = res["symbol"]
                price = res["price"]
                price_at = res["price_at"]
                bars = res["bars"]
                volume = res["volume"]
                signal = res["signal"]
                signal_id = res["signal_id"]
                vol_20 = res["vol_20"]
                expiry_minutes = res["expiry_minutes"]
                volatility_class = res["volatility_class"]
                expiry = res["expiry"]
                prev_price = res["prev_price"]
                price_change = res["price_change"]
                price_change_pct = res["price_change_pct"]
                session_start_price = res["session_start_price"]
                session_change = res["session_change"]
                session_change_pct = res["session_change_pct"]
                score = res["score"]
                classification = res["classification"]
                system_confidence = res["system_confidence"]
                asset_score = res["asset_score"]
                asset_classification = res["asset_classification"]
                has_position = res["has_position"]
                
                score_vol = res.get("score_vol", 0.0)
                volatility_regime = res.get("volatility_regime", "unknown")
                volatility_gate_result = res.get("volatility_gate_result", "fail-safe HOLD")

                ai_config = self.config.get("ai", {})
                is_buy = (signal.action == "ENTRY" and signal.side == "buy")
                
                proposal_allowed = (symbol in active_watchlist and proposals_enabled and signal.action in {"ENTRY", "EXIT"} and score >= ai_config.get("ai_review_min_score", 65))
                gpt_called = False
                proposal_generated = False
                no_action_reason = ""
                proposal_id = None
                decision = None
                proposal = None
                review = None
                
                dedupe_status = "skipped"
                dedupe_reason = "not eligible for proposal"
                paper_size_adjustment = 1.0
                candidate_suppression_reason = None
                deferred_ai_review_reason = None
                
                if not proposal_allowed:
                    if is_buy and symbol in suppressed_buy_symbols:
                        no_action_reason = "suppressed due to simultaneous candidate limits"
                        candidate_suppression_reason = "suppressed_by_candidate_limit"
                    elif symbol not in active_watchlist:
                        no_action_reason = "symbol not in active watchlist"
                    elif not proposals_enabled:
                        no_action_reason = "proposals disabled for profile"
                    elif signal.action not in {"ENTRY", "EXIT"}:
                        no_action_reason = f"no entry/exit signal ({signal.reason})"
                    else:
                        no_action_reason = f"trade score below threshold ({score} < {ai_config.get('ai_review_min_score', 65)})"
                else:
                    # State-based proposal deduplication
                    dedupe_status = "allowed"
                    dedupe_reason = "passed deduplication check"
                    
                    # Check 1: Pending similar proposal
                    pending_proposals = self.storage.fetch_all(
                        "SELECT * FROM trade_proposals WHERE symbol=? AND side=? AND status='pending'",
                        (symbol, signal.side)
                    )
                    if pending_proposals:
                        dedupe_status = "suppressed"
                        dedupe_reason = "active/pending similar proposal exists"
                    else:
                        # Check 2: Cooldown within 60 minutes
                        last_prop_rows = self.storage.fetch_all(
                            "SELECT * FROM trade_proposals WHERE symbol=? AND side=? ORDER BY created_at DESC LIMIT 1",
                            (symbol, signal.side)
                        )
                        if last_prop_rows:
                            last_prop = last_prop_rows[0]
                            last_created_at = datetime.fromisoformat(last_prop["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
                            elapsed_mins = (now - last_created_at).total_seconds() / 60
                            
                            try:
                                payload_dict = json.loads(last_prop["payload"])
                                last_score = float(payload_dict.get("score", 0))
                            except Exception:
                                last_score = 0.0
                                
                            is_exit = (signal.action == "EXIT" or signal.side == "sell")
                            if elapsed_mins < 60:
                                if is_exit and has_position:
                                    dedupe_status = "allowed"
                                    dedupe_reason = f"exit/reduce-risk action allowed (elapsed: {elapsed_mins:.1f}m)"
                                elif score >= last_score + 10:
                                    dedupe_status = "allowed"
                                    dedupe_reason = f"meaningful score improvement (score: {score:.1f} vs previous: {last_score:.1f}, elapsed: {elapsed_mins:.1f}m)"
                                else:
                                    dedupe_status = "suppressed"
                                    dedupe_reason = f"duplicate proposal cooldown (elapsed: {elapsed_mins:.1f}m, score delta: {score - last_score:.1f})"
                    
                    if dedupe_status == "suppressed":
                        proposal_allowed = False
                        no_action_reason = f"suppressed by dedupe: {dedupe_reason}"
                        self.storage.audit(self.run_id, "proposal_deduplicated", {
                            "symbol": symbol, "side": signal.side, "status": "suppressed", "reason": dedupe_reason
                        })
                    else:
                        # Check candidate limit suppression
                        if is_buy and symbol in suppressed_buy_symbols:
                            proposal_allowed = False
                            dedupe_status = "suppressed"
                            dedupe_reason = "suppressed due to simultaneous candidate limits"
                            no_action_reason = "suppressed due to simultaneous candidate limits"
                            candidate_suppression_reason = "suppressed_by_candidate_limit"
                            self.storage.audit(self.run_id, "proposal_suppressed", {
                                "symbol": symbol, "reason": "suppressed_by_candidate_limit", "score": score
                            })
                        else:
                            calls_today = len(self.storage.fetch_all("SELECT id FROM ai_reviews WHERE created_at >= ?", (today_start,)))
                            last_call = self.storage.fetch_all("SELECT created_at FROM ai_reviews WHERE proposal_id IN (SELECT id FROM trade_proposals WHERE symbol=?) ORDER BY created_at DESC LIMIT 1", (symbol,))
                            time_since = (now - datetime.fromisoformat(last_call[0]["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)).total_seconds() / 60 if last_call else float("inf")
                            if (ai_config.get("ai_review_on_every_run", False) or (calls_today < ai_config.get("ai_daily_call_limit", 10) and self.ai.calls_made < ai_config.get("ai_max_calls_per_run", 2) and time_since >= ai_config.get("ai_review_min_interval_minutes", 30))):
                                gpt_called = True
                            
                            proposal_id = str(uuid.uuid4())
                                         # Ranks and selection reasons
                            eligible_rank = None
                            selection_reason = None
                            if is_buy:
                                try:
                                    eligible_rank = [c["symbol"] for c in buy_candidates].index(symbol) + 1
                                except ValueError:
                                    eligible_rank = None
                                    
                                higher_rank_suppressed_cooldown = False
                                higher_rank_suppressed_pending = False
                                for r in profile_results:
                                    if r["symbol_rank"] < rank:
                                        r_sig = r["signal"]
                                        if r_sig.action == "ENTRY" and r_sig.side == "buy":
                                            if r.get("tmp_dedupe_status") == "suppressed":
                                                if r.get("tmp_dedupe_reason") == "cooldown":
                                                    higher_rank_suppressed_cooldown = True
                                                elif r.get("tmp_dedupe_reason") == "pending_proposal":
                                                    higher_rank_suppressed_pending = True
                                                    
                                if higher_rank_suppressed_cooldown:
                                    selection_reason = "Selected because higher-ranked candidates were recently proposed and are still in cooldown."
                                elif higher_rank_suppressed_pending:
                                    selection_reason = "Selected because it was the best candidate that passed cooldown and pending-proposal checks."
                                else:
                                    selection_reason = "Selected because it was the strongest eligible candidate."

                            # Size adjustment calculation
                            base_notional = float(self.config["risk"].get("max_trade_notional_paper" if self.config.get("mode") == "paper" else "max_trade_notional_live", 5))
                            notional = base_notional
                            notional_adjustment_note = ""
                            if signal.action == "ENTRY" and signal.side == "buy":
                                if vol_20 is not None and 0.25 <= vol_20 <= 0.35:
                                    notional = base_notional * 0.5
                                    paper_size_adjustment = 0.5
                                    notional_adjustment_note = " (reduced by 50% due to elevated volatility)"
                                    
                            proposal = {
                                "id": proposal_id,
                                "run_id": self.run_id,
                                "signal_id": signal_id,
                                "symbol": symbol,
                                "side": signal.side,
                                "action": "entry" if signal.action == "ENTRY" else "exit",
                                "notional": notional,
                                "notional_adjustment_note": notional_adjustment_note,
                                "latest_price": price,
                                "price_at": str(price_at),
                                "historical_bars": len(bars),
                                "volume": volume,
                                "price_gap_pct": float((price / float(bars.iloc[-1]["close"]) - 1) * 100) if not bars.empty and float(bars.iloc[-1]["close"]) > 0 else 0.0,
                                "created_at": now.isoformat(),
                                "expires_at": expiry.isoformat(),
                                "strategy_version": signal.strategy_version,
                                "reason": signal.reason,
                                "order_type": "market",
                                "asset_class": "equity",
                                "indicators": signal.indicators,
                                "score": score,
                                "classification": classification,
                                "system_confidence": system_confidence,
                                "expiry_minutes": expiry_minutes,
                                "volatility_class": volatility_class,
                                "asset_score": asset_score,
                                "asset_classification": asset_classification,
                                "symbol_rank": rank,
                                "total_active_symbols": len(active_watchlist),
                                "price_change_pct": price_change_pct,
                                "session_change_pct": session_change_pct,
                                "gpt_called": gpt_called,
                                "proposal_market_rank": rank,
                                "proposal_eligible_rank": eligible_rank,
                                "selection_reason": selection_reason
                            }
                            
                            self._should_auto_execute(proposal)
                            decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, self._portfolio_context(proposal))
                            if not decision.passed:
                                no_action_reason = f"blocked by risk checks: {'; '.join(decision.reasons)}"
                                proposal_allowed = False
                            else:
                                require_gpt = self.config.get("risk", {}).get("require_gpt_review_for_buy_proposals", True)
                                
                                try:
                                    if gpt_called:
                                        review = self.ai.review(proposal)
                                        if "Deterministic fallback" in review.get("reasoning_notes", ""):
                                            gpt_called = False
                                    else:
                                        review = deterministic_review(proposal, warning="AI review throttled to avoid spam")
                                except Exception as e:
                                    logger.error("GPT review failed: %s", e)
                                    gpt_called = False
                                    review = deterministic_review(proposal, warning="AI review failed: " + str(e))
                                    
                                proposal["gpt_called"] = gpt_called
                                proposal["review"] = review
                                
                                if is_buy and require_gpt and not gpt_called:
                                    proposal_generated = False
                                    proposal_allowed = False
                                    no_action_reason = "deferred due to AI review throttling/unavailability"
                                    deferred_ai_review_reason = "deferred_ai_review_unavailable"
                                    self.storage.audit(self.run_id, "proposal_deferred", {
                                        "symbol": symbol, "reason": "deferred_ai_review_unavailable", "score": score
                                    })
                                else:
                                    proposal_generated = True
                                    no_action_reason = "proposal generated"
                                    any_generated = True
                                    if review:
                                        proposal["ai_review_status"] = "Completed" if gpt_called else "Not available"
                                        proposal["ai_confidence"] = review.get("gpt_confidence", "Not called")
                                        proposal["ai_caution"] = review.get("gpt_caution", "Low")

                if proposal_allowed:
                    if review is None:
                        review = self.ai.review(proposal) if gpt_called else deterministic_review(proposal, warning="AI review throttled to avoid spam")
                        proposal["review"] = review
                    if review:
                        proposal["ai_review_status"] = "Completed" if gpt_called else "Not available"
                        proposal["ai_confidence"] = review.get("gpt_confidence", "Not called")
                        proposal["ai_caution"] = review.get("gpt_caution", "Low")

                g_conf = review.get("gpt_confidence", "Not called") if (gpt_called and review) else "Not called"
                g_caut = review.get("gpt_caution", "Low") if (gpt_called and review) else "N/A"
                m_risk = review.get("main_risk", "No AI risk evaluation was performed.") if (gpt_called and review) else "N/A"
                exp_sgt = format_sgt(expiry)

                self.storage.execute(
                    "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification,symbol_rank,proposal_generated,no_action_reason,asset_selection_score,trade_decision_score,system_confidence,gpt_confidence,gpt_caution,expiry_minutes,expires_at_sgt,main_risk,volatility_regime,volatility_score_contribution,volatility_gate_result,dedupe_status,dedupe_reason,paper_size_adjustment,candidate_suppression_reason,deferred_ai_review_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, profile_key, symbol, price, prev_price, price_change, price_change_pct, session_start_price, session_change, vol_20 or 0.0, signal.action, score, classification, signal.reason, int(proposal_allowed), int(gpt_called), now.isoformat(), asset_score, asset_classification, rank, int(proposal_generated), no_action_reason, asset_score, score, system_confidence, g_conf, g_caut, expiry_minutes, exp_sgt, m_risk, volatility_regime, score_vol, volatility_gate_result, dedupe_status, dedupe_reason, paper_size_adjustment, candidate_suppression_reason, deferred_ai_review_reason)
                )
                
                logger.info(
                    "Symbol: %s | Profile: %s | Asset Score: %.2f (%s) | Trade Score: %.2f (%s) | Rank: #%d | Prev Change: %.2f%% | Session Change: %.2f | Proposal Allowed: %s | GPT Called: %s | Proposal Generated: %s | No-Action Reason: %s",
                    symbol, profile_key, asset_score, asset_classification, score, classification, rank, price_change_pct, session_change, proposal_allowed, gpt_called, proposal_generated, no_action_reason or "N/A"
                )
                
                if not proposal_generated:
                    if proposal_allowed and decision and not decision.passed:
                        self.storage.audit(self.run_id, "proposal_blocked", {"symbol": symbol, "reasons": decision.reasons})
                    continue
                
                # Manual approval is the only supported path. Auto-execution
                # cannot synthesize an approval or approved proposal state.
                self.storage.execute(
                    "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,proposal_market_rank,proposal_eligible_rank,selection_reason,ai_review_status,ai_confidence,ai_caution) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal_id,
                        self.run_id,
                        signal_id,
                        symbol,
                        signal.side,
                        proposal["notional"],
                        "pending",
                        now.isoformat(),
                        expiry.isoformat(),
                        signal.strategy_version,
                        json_dumps(proposal),
                        rank,
                        eligible_rank,
                        selection_reason,
                        proposal.get("ai_review_status"),
                        proposal.get("ai_confidence"),
                        proposal.get("ai_caution")
                    )
                )
                self.storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, proposal_id, review["summary"], json_dumps(review["risks"]), review["caution_level"], json_dumps(review), iso_now()))
                
                res_tg = self.telegram.send_message(format_proposal_message(proposal, self.config))
                if res_tg and isinstance(res_tg, dict) and "message_id" in res_tg:
                    self.storage.execute("UPDATE trade_proposals SET telegram_message_id=? WHERE id=?", (str(res_tg["message_id"]), proposal_id))

            if profile_results:
                best_watch_res = profile_results[0]
                active_results = [r for r in profile_results if r["symbol"] in active_watchlist]
                best_trade_res = max(active_results, key=lambda x: x["score"]) if active_results else (max(profile_results, key=lambda x: x["score"]) if profile_results else None)
                
                logger.info("=== Profile '%s' Scan Summary ===", profile_key)
                logger.info("Best symbol to watch: %s (Asset Score: %.2f)", best_watch_res["symbol"], best_watch_res["asset_score"])
                if best_trade_res:
                    logger.info("Best symbol for trade consideration: %s (Trade Score: %.2f)", best_trade_res["symbol"], best_trade_res["score"])
                else:
                    logger.info("Best symbol for trade consideration: None")
                
                if any_generated:
                    logger.info("Why no proposal was generated: N/A (Proposal was generated)")
                else:
                    reasons_summary = ", ".join(f"{r['symbol']}: {r.get('no_action_reason') or 'N/A'}" for r in profile_results)
                    logger.info("Why no proposal was generated: %s", reasons_summary)

    def check_and_send_digest(self) -> None:
        digest_config = self.config.get("digest", {})
        if not digest_config.get("telegram_digest_enabled", True):
            return

        now = datetime.now(UTC)
        interval_minutes = digest_config.get("telegram_digest_interval_minutes", 30)

        try:
            market_open = self.broker.is_market_open()
        except Exception:
            market_open = False

        if not market_open and not digest_config.get("telegram_digest_send_when_market_closed", False):
            return

        # 1. Throttling
        last_sent = self.storage.fetch_all(
            "SELECT sent_at FROM telegram_digests WHERE status='sent' ORDER BY sent_at DESC LIMIT 1"
        )
        if last_sent:
            last_sent_dt = datetime.fromisoformat(last_sent[0]["sent_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
            elapsed_mins = (now - last_sent_dt).total_seconds() / 60
            if elapsed_mins < (interval_minutes - 2):
                return

        # 2. Minimum cycles
        window_start = now - timedelta(minutes=interval_minutes)
        window_start_iso = window_start.isoformat()
        
        cycles = self.storage.fetch_all(
            "SELECT COUNT(DISTINCT run_id) as cnt FROM market_memory WHERE created_at >= ?",
            (window_start_iso,)
        )
        cycle_count = cycles[0]["cnt"] if cycles else 0
        min_cycles = digest_config.get("telegram_digest_min_cycles_required", 2)
        if cycle_count < min_cycles:
            return

        # 3. Retrieve rows
        include_obs = digest_config.get("telegram_digest_include_observation_symbols", True)
        profiles = self.config.get("market_profiles", {})
        active_watchlist = []
        obs_watchlist = []
        for p in profiles.values():
            if p.get("status") == "active":
                active_watchlist.extend(p.get("watchlist", []))
                obs_watchlist.extend(p.get("observation_watchlist", []))
                
        allowed_symbols = set(active_watchlist)
        if include_obs:
            allowed_symbols.update(obs_watchlist)

        rows = self.storage.fetch_all(
            "SELECT * FROM market_memory WHERE created_at >= ? ORDER BY created_at ASC",
            (window_start_iso,)
        )
        if not rows:
            return

        import collections
        symbol_rows = collections.defaultdict(list)
        for row in rows:
            sym = row["symbol"]
            if allowed_symbols and sym not in allowed_symbols:
                continue
            symbol_rows[sym].append(row)

        if not symbol_rows:
            return

        symbols_list = []
        for sym, s_rows in symbol_rows.items():
            first_row = s_rows[0]
            latest_row = s_rows[-1]
            p_first = first_row["price"]
            p_latest = latest_row["price"]
            change_30m = ((p_latest / p_first) - 1.0) * 100.0 if p_first > 0 else 0.0
            
            p_session_start = latest_row.get("session_start_price") or p_latest
            session_change = ((p_latest / p_session_start) - 1.0) * 100.0 if p_session_start > 0 else 0.0
            
            has_prop = any(bool(r.get("proposal_generated")) for r in s_rows)
            status_str = "Watch"
            if has_prop:
                status_str = "Proposal generated, pending approval"
            elif latest_row.get("signal") in {"ENTRY", "EXIT"}:
                status_str = "Watch, no proposal"
                
            symbols_list.append({
                "symbol": sym,
                "trade_score": latest_row["score"],
                "trade_classification": latest_row["classification"],
                "asset_score": latest_row.get("asset_score"),
                "price_change_30m": change_30m,
                "session_change": session_change,
                "status": status_str
            })

        symbols_list.sort(key=lambda x: x["trade_score"] if x["trade_score"] is not None else -1, reverse=True)

        strongest = symbols_list[0]
        weakest = min(symbols_list, key=lambda x: x["trade_score"] if x["trade_score"] is not None else 1000)

        max_syms = digest_config.get("telegram_digest_max_symbols", 6)
        top_watched = symbols_list[:max_syms]

        proposals = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE created_at >= ?",
            (window_start_iso,)
        )
        prop_cnt = proposals[0]["cnt"] if proposals else 0
        
        orders = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM orders WHERE created_at >= ?",
            (window_start_iso,)
        )
        order_cnt = orders[0]["cnt"] if orders else 0
        
        gpt_calls = sum(bool(r.get("gpt_called")) for r in rows)
        
        expired = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE status='expired' AND expires_at >= ? AND expires_at <= ?",
            (window_start_iso, now.isoformat())
        )
        expired_cnt = expired[0]["cnt"] if expired else 0

        if prop_cnt > 0:
            pending_rows = self.storage.fetch_all(
                "SELECT symbol, side FROM trade_proposals WHERE created_at >= ? AND status='pending'",
                (window_start_iso,)
            )
            if pending_rows:
                syms_p = ", ".join(f"{r['side'].upper()} {r['symbol']}" for r in pending_rows)
                summary_str = f"{strongest['symbol']} is strongest, pending proposal for {syms_p}."
            else:
                summary_str = f"Setup triggered action for {strongest['symbol']} during the window."
        else:
            summary_str = f"{strongest['symbol']} is strongest, but no setup crossed the proposal threshold."

        # Mention deferred candidates due to AI review unavailability
        deferred_rows = self.storage.fetch_all(
            "SELECT DISTINCT symbol FROM market_memory WHERE created_at >= ? AND deferred_ai_review_reason='deferred_ai_review_unavailable'",
            (window_start_iso,)
        )
        if deferred_rows:
            deferred_syms = ", ".join(r["symbol"] for r in deferred_rows)
            summary_str += f" Candidates deferred due to AI review throttling: {deferred_syms}."

        digest_data = {
            "market_open_status": "Open" if market_open else "Closed",
            "window_start": window_start,
            "window_end": now,
            "symbols_list": top_watched,
            "weakest_symbol": weakest["symbol"],
            "weakest_score": weakest["trade_score"],
            "weakest_classification": weakest["trade_classification"],
            "actions": {
                "proposals": prop_cnt,
                "orders": order_cnt,
                "gpt_calls": gpt_calls,
                "expired": expired_cnt
            },
            "summary": summary_str
        }

        from .utils import format_digest_message
        message_text = format_digest_message(digest_data, self.config)
        
        try:
            self.telegram.send_message(message_text)
            status = "sent"
        except Exception as e:
            status = "error"
            self.storage.record_check(self.run_id, "digest_send", False, str(e), stage="digest")
            
        symbols_str = ", ".join(x["symbol"] for x in top_watched)
        self.storage.execute(
            "INSERT INTO telegram_digests(run_id,window_start,window_end,sent_at,symbols,summary_text,status) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, window_start_iso, now.isoformat(), now.isoformat(), symbols_str, summary_str, status)
        )
        self.storage.audit(self.run_id, "digest_processed", {"status": status, "window_start": window_start_iso, "window_end": now.isoformat()})

    def run_cycle(self) -> None:
        BrokerReconciler(self.broker, self.storage, self.run_id).reconcile()
        # Reconciliation has refreshed account/position state; force the next
        # proposal/final context to retrieve an authoritative fresh snapshot.
        self._context_cache = None
        self.notify_expired_proposals()
        if self.config.get("telegram", {}).get("market_scan_processes_telegram_updates", True):
            self.process_telegram()
        if not (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            self.scan()
        self.check_and_send_digest()
