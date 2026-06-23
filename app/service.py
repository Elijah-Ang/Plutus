from __future__ import annotations

import json
import logging
import math
import re
import time
import dataclasses
import uuid
import pandas as pd
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .ai_review import AIReviewer, deterministic_review
from .capabilities import AUTO_EXECUTION_SUPPORTED

logger = logging.getLogger("trading_agent")

SGT = ZoneInfo("Asia/Singapore")

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


def _parse_datetime(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _format_sgt_time(value: datetime) -> str:
    sgt_dt = value.astimezone(SGT)
    hour = sgt_dt.hour % 12 or 12
    return f"{hour}:{sgt_dt:%M %p}"


def _format_sleep_window(start_time: str | datetime, end_time: str | datetime) -> str:
    start_sgt = _parse_datetime(start_time).astimezone(SGT)
    end_sgt = _parse_datetime(end_time).astimezone(SGT)
    start_date = start_sgt.strftime("%b %d, %Y")
    end_date = end_sgt.strftime("%b %d, %Y")
    if start_sgt.date() == end_sgt.date():
        return f"{start_date}, {_format_sgt_time(start_sgt)}–{_format_sgt_time(end_sgt)} SGT"
    return f"{start_date}, {_format_sgt_time(start_sgt)}–{end_date}, {_format_sgt_time(end_sgt)} SGT"


def _format_sleep_duration(start_time: str | datetime, end_time: str | datetime) -> str:
    seconds = max(0, int((_parse_datetime(end_time) - _parse_datetime(start_time)).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hr" + ("" if hours == 1 else "s"))
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} sec")
    return " ".join(parts)


class TradingService:
    """One bounded launchd cycle. AI never receives a broker or execution object."""

    def __init__(self, config: dict[str, Any], storage: Any, broker: Any, run_id: str) -> None:
        self.config, self.storage, self.broker, self.run_id = config, storage, broker, run_id
        telegram = TelegramBot()
        self.telegram = telegram
        self.ai = AIReviewer(config.get("ai", {}))
        self._context_cache: tuple[float, dict[str, Any]] | None = None
        self._auto_block_audited = False
        self.listener_started_at = time.time()

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

            # 1. Sleep / Wake Command Parsing (Prioritized Control Commands)
            cleaned = text.strip().lower()
            cleaned = re.sub(r"[.!?,]+$", "", cleaned).strip()
            normalized_cmd = " ".join(cleaned.split())

            is_sleep_on = normalized_cmd in (
                "/sleep", "sleep", "sleep mode on", "i'm going to sleep", "im going to sleep", "going to sleep"
            )
            is_sleep_off = normalized_cmd in (
                "/awake", "awake", "i'm awake", "im awake", "sleep mode off", "wake up"
            )
            
            if is_sleep_on or is_sleep_off:
                if not self.telegram.is_authorized(sender):
                    self.storage.audit(self.run_id, "sleep_mode_command_ignored_unauthorized", {
                        "sender_id": sender,
                        "raw_command": text
                    })
                    continue
                    
                message_date = message.get("date")
                if message_date is not None:
                    if message_date < time.time() - 86400:
                        self.storage.audit(self.run_id, "sleep_mode_command_ignored_old", {
                            "message_date": message_date,
                            "raw_command": text
                        })
                        self.telegram.send_message(
                            "⚠️ Ignored old sleep/wake command from more than 24 hours ago. Please send it again.",
                            str((message.get("chat") or {}).get("id", self.telegram.chat_id))
                        )
                        continue
                
                update_id = update.get("update_id")
                message_id = message.get("message_id")
                
                if is_sleep_on:
                    was_active = int(self.storage.get_control_state("sleep_mode_active", "0")) == 1
                    if was_active:
                        self.telegram.send_message(
                            "🌙 Sleep mode is already ON.",
                            str((message.get("chat") or {}).get("id", self.telegram.chat_id))
                        )
                    else:
                        self.storage.set_control_state("sleep_mode_active", "1", sender, "telegram", text, update_id, message_id, message_date)
                        self.storage.set_control_state("sleep_mode_last_command", "sleep", sender, "telegram", text, update_id, message_id, message_date)
                        
                        start_time_iso = datetime.fromtimestamp(message_date, UTC).isoformat() if message_date else iso_now()
                        self.storage.set_control_state("sleep_mode_started_at", start_time_iso, sender, "telegram", text, update_id, message_id, message_date)
                        self.storage.set_control_state("sleep_mode_last_command_sent_at", start_time_iso, sender, "telegram", text, update_id, message_id, message_date)
                        self.storage.set_control_state("sleep_mode_last_command_processed_at", iso_now(), sender, "telegram", text, update_id, message_id, message_date)
                        
                        self.storage.audit(self.run_id, "sleep_mode_enabled", {"raw_command": text})
                        
                        self.telegram.send_message(
                            "🌙 Sleep mode ON. I will keep scanning and logging, suppress normal BUY proposals, and only alert/act on serious paper-exit risk according to the configured emergency-exit rules. No live trading is enabled.",
                            str((message.get("chat") or {}).get("id", self.telegram.chat_id))
                        )
                else:
                    was_active = int(self.storage.get_control_state("sleep_mode_active", "0")) == 1
                    if not was_active:
                        self.telegram.send_message(
                            "☀️ Sleep mode is already OFF.",
                            str((message.get("chat") or {}).get("id", self.telegram.chat_id))
                        )
                    else:
                        start_time_iso = self.storage.get_control_state("sleep_mode_started_at", iso_now())
                        self.storage.set_control_state("sleep_mode_active", "0", sender, "telegram", text, update_id, message_id, message_date)
                        self.storage.set_control_state("sleep_mode_last_command", "awake", sender, "telegram", text, update_id, message_id, message_date)
                        
                        end_time_iso = iso_now()
                        self.storage.set_control_state("sleep_mode_ended_at", end_time_iso, sender, "telegram", text, update_id, message_id, message_date)
                        self.storage.set_control_state("sleep_mode_last_command_sent_at", datetime.fromtimestamp(message_date, UTC).isoformat() if message_date else end_time_iso, sender, "telegram", text, update_id, message_id, message_date)
                        self.storage.set_control_state("sleep_mode_last_command_processed_at", end_time_iso, sender, "telegram", text, update_id, message_id, message_date)
                        
                        self.storage.audit(self.run_id, "sleep_mode_disabled", {"raw_command": text})
                        
                        self.telegram.send_message(
                            "☀️ Sleep mode OFF. Normal paper proposal alerts are enabled again. No orders were placed unless explicitly approved or emergency paper-exit rules were triggered.",
                            str((message.get("chat") or {}).get("id", self.telegram.chat_id))
                        )
                        self.send_wake_summary(start_time_iso, end_time_iso)
                continue

            # 2. Telegram Bot Utility Commands
            if text.startswith("/"):
                response = self.telegram.handle_command(text, sender)
                self.storage.audit(self.run_id, "telegram_command", {"command": text.split()[0], "authorized": self.telegram.is_authorized(sender)})
                self.telegram.send_message(response, str((message.get("chat") or {}).get("id", self.telegram.chat_id)))
                continue

            # 3. Phase 0: Protect against old queued Telegram approvals (Only for approvals/rejections)
            message_date = message.get("date")
            if message_date is not None:
                is_stale = (message_date < self.listener_started_at) or (message_date < time.time() - 120)
                if is_stale:
                    self.storage.audit(self.run_id, "listener_bootstrap_update_ignored", {
                        "message_id": message.get("message_id"),
                        "message_date": message_date,
                        "listener_started_at": self.listener_started_at,
                        "text": message.get("text")
                    })
                    text_lower = str(message.get("text", "")).strip().lower()
                    if text_lower in ("yes", "no") or any(w in text_lower for w in ("yes", "no", "approve", "reject")):
                        self.telegram.send_message(
                            "I ignored an old approval message from before the fast listener started. Please reply again to the current proposal if it is still pending.",
                            str((message.get("chat") or {}).get("id", self.telegram.chat_id))
                        )
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
                getattr(self.telegram, "allowed_user_id", "") or "",
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
                if row.get("emergency_exit_triggered") == 1:
                    self.storage.execute("UPDATE trade_proposals SET status='rejected', emergency_exit_final_decision='cancelled', emergency_exit_user_response='no' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                    self.telegram.send_message(f"❌ Received: NO for {prop_symbol} emergency paper sell proposal. Emergency exit cancelled.")
                    self.storage.audit(self.run_id, "emergency_exit_cancelled_by_user", {"symbol": prop_symbol, "proposal_id": parsed.proposal_id})
                else:
                    self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                    self.telegram.send_message(f"❌ Received: NO for {prop_symbol} paper {prop_side} proposal. Proposal rejected. No order will be placed.")
                ack_sent = iso_now()
                delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))
                continue
                
            if row.get("emergency_exit_triggered") == 1:
                self.telegram.send_message(f"✅ Received: YES for {prop_symbol} emergency paper sell proposal. I will now run the final safety check.")
                ack_sent = iso_now()
                delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))
                
                if not self.storage.consume_approval(parsed.proposal_id, approval_id):
                    self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                    continue
                
                self.storage.audit(self.run_id, "emergency_exit_approved_by_user", {"symbol": prop_symbol, "proposal_id": parsed.proposal_id})
                
                proposal = {**json.loads(row.get("payload") or "{}"), **row}
                success, err_reason = self.revalidate_and_execute_emergency_exit(proposal)
                if success:
                    self.storage.execute("UPDATE trade_proposals SET status='approved', emergency_exit_final_decision='submitted', emergency_exit_user_response='yes' WHERE id=?", (parsed.proposal_id,))
                    self.telegram.send_message(f"✅ Paper order submitted: Sell {prop_symbol} for {proposal.get('qty', 0)} shares. Mode: paper only.")
                    self.storage.audit(self.run_id, "emergency_exit_submitted", {"symbol": prop_symbol, "score": row.get("emergency_exit_score")})
                else:
                    self.storage.execute("UPDATE trade_proposals SET status='blocked', emergency_exit_block_reason=?, emergency_exit_user_response='yes' WHERE id=?", (err_reason, parsed.proposal_id))
                    self.telegram.send_message(f"⚠️ Emergency exit was blocked. Reason: {err_reason}. No order was placed.")
                    self.storage.audit(self.run_id, "emergency_exit_blocked", {"symbol": prop_symbol, "reason": err_reason})
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

        # Check for timed out emergency exit proposals
        now_str = iso_now()
        timed_out = self.storage.fetch_all(
            "SELECT * FROM trade_proposals WHERE status='pending' AND emergency_exit_triggered=1 AND emergency_exit_auto_execute_due_at <= ?",
            (now_str,)
        )
        for row in timed_out:
            proposal_id = row["id"]
            symbol = row["symbol"]
            qty = row["qty"]
            total_score = row["emergency_exit_score"]
            proposal = {**json.loads(row["payload"] or "{}"), **row}
            
            # Consume first to prevent race condition
            self.storage.execute("UPDATE trade_proposals SET status='approved', emergency_exit_auto_execute_attempted_at=? WHERE id=?", (iso_now(), proposal_id))
            
            self.storage.audit(self.run_id, "emergency_exit_auto_timeout_reached", {"symbol": symbol, "proposal_id": proposal_id})
            
            success, err_reason = self.revalidate_and_execute_emergency_exit(proposal)
            if success:
                self.storage.execute("UPDATE trade_proposals SET emergency_exit_final_decision='submitted' WHERE id=?", (proposal_id,))
                self.telegram.send_message(f"✅ Paper order submitted: Sell {symbol} for {qty} shares. Mode: paper only.")
                self.storage.audit(self.run_id, "emergency_exit_submitted", {"symbol": symbol, "score": total_score})
            else:
                self.storage.execute("UPDATE trade_proposals SET status='blocked', emergency_exit_block_reason=? WHERE id=?", (err_reason, proposal_id))
                self.telegram.send_message(f"⚠️ Emergency exit was blocked. Reason: {err_reason}. No order was placed.")
                self.storage.audit(self.run_id, "emergency_exit_blocked", {"symbol": symbol, "reason": err_reason})

    def _should_auto_execute(self, proposal: dict[str, Any]) -> bool:
        # Quarantined: YAML cannot enable this unsupported capability.
        requested = self.config.get("auto_execution_enabled", False) or self.config.get("auto_execution_mode") != "manual_only"
        if requested and not self._auto_block_audited:
            self.storage.audit(self.run_id, "auto_execution_blocked", {"reason": "unsupported capability"})
            self._auto_block_audited = True
        assert AUTO_EXECUTION_SUPPORTED is False
        return False

    def calculate_emergency_exit_risk_score(
        self,
        symbol: str,
        position_drawdown_pct: float,
        average_entry_price: float,
        current_price: float,
        indicators: dict[str, Any],
        bars: Any
    ) -> tuple[float, dict[str, Any], bool, str]:
        if not average_entry_price or average_entry_price <= 0:
            dd_val = -1.0
            drawdown_points = 35
            adverse_points = 15
            adverse_move_atr = 0.0
        else:
            dd_val = position_drawdown_pct
            if dd_val > -0.04:
                drawdown_points = 0
            elif dd_val > -0.06:
                drawdown_points = 10
            elif dd_val > -0.08:
                drawdown_points = 20
            elif dd_val > -0.10:
                drawdown_points = 28
            else:
                drawdown_points = 35

        from app.features import build_features
        features_df = build_features(bars)
        row = features_df.iloc[-1]
        prev_row = features_df.iloc[-2] if len(features_df) >= 2 else None
        
        close_val = float(row["close"])
        ma_50_val = float(row["ma_50"]) if "ma_50" in row and not pd.isna(row["ma_50"]) else None
        ma_200_val = float(row["ma_200"]) if "ma_200" in row and not pd.isna(row["ma_200"]) else None
        
        ma_50_current = ma_50_val
        ma_50_prev = float(prev_row["ma_50"]) if (prev_row is not None and "ma_50" in prev_row and not pd.isna(prev_row["ma_50"])) else None
        ma_50_falling = (ma_50_current < ma_50_prev) if (ma_50_current is not None and ma_50_prev is not None) else False
        
        trend_points = 0
        if ma_200_val is not None and close_val < ma_200_val:
            trend_points = 20
        elif ma_50_val is not None and close_val < ma_50_val:
            if ma_50_falling:
                trend_points = 16
            else:
                trend_points = 12

        atr_value = None
        if "high" in bars.columns and "low" in bars.columns and "close" in bars.columns:
            high = bars["high"].astype(float)
            low = bars["low"].astype(float)
            close = bars["close"].astype(float)
            close_prev = close.shift(1)
            tr = pd.concat([high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1).max(axis=1)
            atr_series = tr.rolling(20).mean()
            if not atr_series.empty and not pd.isna(atr_series.iloc[-1]):
                atr_value = float(atr_series.iloc[-1])
        
        is_atr_proxy = False
        vol_20 = indicators.get("volatility_20")
        if atr_value is None:
            is_atr_proxy = True
            if vol_20 is not None:
                vol_daily = vol_20 / math.sqrt(252)
                atr_value = current_price * vol_daily
            else:
                atr_value = 0.0

        if average_entry_price and average_entry_price > 0:
            adverse_move = average_entry_price - current_price
            adverse_move_atr = adverse_move / atr_value if atr_value > 0 else 0.0
            
            if adverse_move_atr >= 1.50:
                adverse_points = 15
            elif adverse_move_atr >= 1.00:
                adverse_points = 10
            elif adverse_move_atr >= 0.75:
                adverse_points = 5
            else:
                adverse_points = 0
        else:
            adverse_points = 15
            adverse_move_atr = 0.0

        vol_points = 0
        if vol_20 is None:
            vol_points = 0
        elif vol_20 > 0.45:
            vol_points = 10
        elif vol_20 > 0.35:
            vol_points = 7
        elif vol_20 >= 0.25:
            vol_points = 4
        else:
            vol_points = 0

        minutes_to_close = None
        if self.broker is not None:
            try:
                clock = self.broker.get_clock()
                if clock and clock.is_open:
                    minutes_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
            except Exception:
                pass
        
        if minutes_to_close is None or minutes_to_close > 90:
            near_close_points = 0
        elif 30 <= minutes_to_close <= 90:
            near_close_points = 5
        else:
            near_close_points = 10

        quality_points = 0
        price_age = float("inf")
        
        # Get price_at from parameters
        price_at_dt = None
        for r in self.storage.fetch_all("SELECT price_at FROM market_snapshots WHERE symbol=? ORDER BY created_at DESC LIMIT 1", (symbol,)):
            try:
                price_at_dt = datetime.fromisoformat(r["price_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
            except Exception:
                pass
        if price_at_dt:
            price_age = (datetime.now(UTC) - price_at_dt).total_seconds()
        
        if price_age <= 30:
            quality_points += 5
            
        quality_points += 3
        
        open_orders = []
        if self.broker is not None:
            try:
                open_orders = self.broker.get_open_orders()
            except Exception:
                pass
        conflicting = any(str(_value(o, "symbol", "")).upper() == symbol.upper() and str(_value(o, "side", "")).lower() == "sell" for o in open_orders)
        if not conflicting:
            quality_points += 2
            
        total_score = drawdown_points + trend_points + adverse_points + vol_points + near_close_points + quality_points
        
        breakdown = {
            "drawdown_points": drawdown_points,
            "trend_points": trend_points,
            "adverse_points": adverse_points,
            "vol_points": vol_points,
            "near_close_points": near_close_points,
            "quality_points": quality_points,
            "atr_value": atr_value,
            "is_atr_proxy": is_atr_proxy,
            "adverse_move_atr": adverse_move_atr,
            "minutes_to_close": minutes_to_close,
            "price_age_seconds": price_age
        }
        
        hard_trigger_1 = (dd_val <= -0.08) and (ma_50_val is not None and close_val < ma_50_val)
        hard_trigger_2 = (dd_val <= -0.10)
        hard_trigger_3 = (ma_200_val is not None and close_val < ma_200_val) and (dd_val < 0)
        hard_trigger_4 = (adverse_move_atr >= 1.50) and (dd_val <= -0.06)
        
        hard_trigger_matched = any([hard_trigger_1, hard_trigger_2, hard_trigger_3, hard_trigger_4])
        hard_trigger_reason = ""
        if hard_trigger_matched:
            reasons = []
            if hard_trigger_1: reasons.append("drawdown <= -8% and below MA50")
            if hard_trigger_2: reasons.append("drawdown <= -10%")
            if hard_trigger_3: reasons.append("below MA200 and position losing")
            if hard_trigger_4: reasons.append("adverse move >= 1.5 ATR and drawdown <= -6%")
            hard_trigger_reason = "; ".join(reasons)
            
        return float(total_score), breakdown, hard_trigger_matched, hard_trigger_reason

    def get_gpt_exit_explanation(self, proposal: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.ai.review, proposal)
            try:
                review = future.result(timeout=timeout)
                return {
                    "status": "Completed",
                    "confidence": review.get("gpt_confidence", "High"),
                    "caution": review.get("gpt_caution", "Low"),
                    "main_risk": review.get("main_risk", "N/A"),
                    "telegram_message": review.get("telegram_message")
                }
            except Exception as e:
                logger.warning("GPT exit review timed out or failed: %s", e)
                return {
                    "status": "Not available; using rule-based emergency exit reason",
                    "confidence": "Not called",
                    "caution": "Low",
                    "main_risk": "N/A",
                    "telegram_message": None
                }

    def revalidate_and_execute_emergency_exit(self, proposal: dict[str, Any]) -> tuple[bool, str]:
        if self.config.get("mode") != "paper" or self.config.get("live_enabled") is not False:
            return False, "not in paper mode / live enabled"
        
        emergency_cfg = self.config.get("emergency_exit", {})
        if not emergency_cfg.get("enabled", True):
            return False, "emergency exit disabled in configuration"
            
        if not self.storage.writable():
            return False, "database is not writable"
            
        if (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            return False, "kill switch active"
            
        symbol = proposal["symbol"]
        
        existing_orders = self.storage.fetch_all(
            "SELECT id FROM orders WHERE symbol=? AND side='sell' AND status IN ('submitted', 'filled')",
            (symbol,)
        )
        if existing_orders:
            return False, "duplicate emergency exit order already submitted"

        if self.broker is None:
            return False, "broker client unavailable"
            
        positions = self.broker.get_positions()
        pos_obj = next((p for p in positions if str(_value(p, "symbol", "")).upper() == symbol.upper()), None)
        if not pos_obj:
            return False, f"no active broker position found for symbol {symbol}"
            
        qty_held = float(_value(pos_obj, "qty") or 0.0)
        if qty_held <= 0:
            return False, f"broker position quantity for {symbol} is 0"
            
        open_orders = self.broker.get_open_orders()
        conflicting = any(str(_value(o, "symbol", "")).upper() == symbol.upper() and str(_value(o, "side", "")).lower() == "sell" for o in open_orders)
        if conflicting:
            return False, "conflicting open sell order exists"
            
        if not self.broker.is_market_open():
            return False, "market is closed"
            
        trade = self.broker.get_latest_price(symbol)
        refreshed_price = float(_value(trade, "price", 0) or 0)
        refreshed_at = _dt(_value(trade, "timestamp", datetime.now(UTC)))
        if not refreshed_price or refreshed_price <= 0:
            return False, "failed to fetch refreshed price"
            
        price_age = (datetime.now(UTC) - refreshed_at).total_seconds()
        if price_age > 60:
            return False, f"refreshed price is stale (age: {price_age:.1f}s > 60s)"
            
        proposal_price = proposal.get("latest_price")
        if proposal_price is not None and proposal_price > 0:
            move_bps = (abs(refreshed_price - proposal_price) / proposal_price) * 10000
            max_move_bps = self.config.get("telegram", {}).get("approval_max_price_move_bps", 25)
            if move_bps > max_move_bps:
                return False, f"price moved too much since emergency decision ({move_bps:.1f} bps > limit {max_move_bps} bps)"
                
        client_order_id = f"ta-emergency-{uuid.uuid4().hex[:16]}"
        try:
            order_args = {"qty": qty_held}
            response = self.broker.submit_order(
                symbol, "sell", order_args, "market", None, client_order_id
            )
            self.storage.execute(
                "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, proposal["id"], str(_value(response, "id", "")) or None, client_order_id, symbol, "sell", None, qty_held, "submitted", json_dumps({"submitted": True}), iso_now(), iso_now()),
            )
            return True, "submitted"
        except Exception as e:
            return False, f"broker execution failed: {type(e).__name__} ({str(e)})"

    def send_wake_summary(self, start_time: str, end_time: str) -> None:
        suppressed_buys = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM market_memory WHERE candidate_suppression_reason='suppressed_by_sleep_mode' AND created_at BETWEEN ? AND ?",
            (start_time, end_time)
        )[0]["cnt"]
        
        sell_alerts = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE side='sell' AND emergency_exit_triggered=0 AND created_at BETWEEN ? AND ?",
            (start_time, end_time)
        )[0]["cnt"]
        
        emerg_triggered = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE emergency_exit_triggered=1 AND created_at BETWEEN ? AND ?",
            (start_time, end_time)
        )[0]["cnt"]
        
        emerg_submitted = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE emergency_exit_triggered=1 AND emergency_exit_final_decision='submitted' AND created_at BETWEEN ? AND ?",
            (start_time, end_time)
        )[0]["cnt"]
        emerg_blocked = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE emergency_exit_triggered=1 AND status='blocked' AND created_at BETWEEN ? AND ?",
            (start_time, end_time)
        )[0]["cnt"]
        
        orders_placed = self.storage.fetch_all(
            "SELECT COUNT(*) as cnt FROM orders WHERE created_at BETWEEN ? AND ?",
            (start_time, end_time)
        )[0]["cnt"]
        
        positions_cnt = 0
        open_orders_cnt = 0
        if self.broker is not None:
            try:
                positions_cnt = len(self.broker.get_positions())
                open_orders_cnt = len(self.broker.get_open_orders())
            except Exception:
                pass
                
        window_str = _format_sleep_window(start_time, end_time)
        duration_str = _format_sleep_duration(start_time, end_time)
        has_events = (suppressed_buys > 0 or sell_alerts > 0 or emerg_triggered > 0 or orders_placed > 0)
        
        if not has_events:
            summary_msg = (
                f"☀️ Sleep mode OFF — Overnight summary\n\n"
                f"Window: {window_str}\n"
                f"Duration: {duration_str}\n"
                f"No emergency exits, no orders, no action needed."
            )
        else:
            action_needed = "review Excel report / Telegram alert." if (emerg_triggered > 0 or orders_placed > 0) else "none."
            summary_msg = (
                f"☀️ Sleep mode OFF — Overnight summary\n\n"
                f"Window: {window_str}\n"
                f"Duration: {duration_str}\n"
                f"Suppressed BUY candidates: {suppressed_buys}\n"
                f"Emergency exits: {emerg_triggered} triggered, {emerg_submitted} submitted, {emerg_blocked} blocked\n"
                f"Orders placed: {orders_placed} paper sell\n"
                f"Current positions: {positions_cnt}\n"
                f"Current open orders: {open_orders_cnt}\n"
                f"Action needed: {action_needed}"
            )
        self.telegram.send_message(summary_msg)
        self.storage.audit(self.run_id, "wake_summary_sent", {
            "start_time": start_time,
            "end_time": end_time,
            "suppressed_buys": suppressed_buys,
            "emerg_triggered": emerg_triggered,
            "emerg_submitted": emerg_submitted,
            "emerg_blocked": emerg_blocked,
            "orders_placed": orders_placed,
            "positions_cnt": positions_cnt,
            "open_orders_cnt": open_orders_cnt
        })

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

    def _compute_setup_key(self, symbol: str, side: str | None, action: str, indicators: dict[str, Any], score: float) -> str:
        close = float(indicators.get("close") or 0.0)
        ma_50 = float(indicators.get("ma_50") or 0.0)
        ma_200 = float(indicators.get("ma_200") or 0.0)
        above_50 = "above_50" if close > ma_50 else "below_50"
        above_200 = "above_200" if (not ma_200 or close > ma_200) else "below_200"
        
        vol_20 = indicators.get("volatility_20")
        if vol_20 is None:
            vol_regime = "missing"
        elif vol_20 > 0.45:
            vol_regime = "extreme"
        elif vol_20 > 0.35:
            vol_regime = "high"
        elif vol_20 >= 0.25:
            vol_regime = "elevated"
        elif vol_20 >= 0.08:
            vol_regime = "normal"
        elif vol_20 >= 0.0:
            vol_regime = "too_quiet"
        else:
            vol_regime = "unknown"
            
        score_band = f"score_{int(score // 10) * 10}"
        side_str = str(side or "").lower()
        action_str = str(action or "").upper()
        
        return f"{symbol}:{side_str}:{action_str}:{above_50}:{above_200}:{vol_regime}:{score_band}"

    def scan(self) -> None:
        if self.config.get("mode") == "live" and not self.config.get("live_enabled"):
            self.telegram.send_message("Blocked for safety: live trading is disabled.")
            return

        sleep_mode_active = int(self.storage.get_control_state("sleep_mode_active", "0")) == 1
        sleep_mode_started_at = self.storage.get_control_state("sleep_mode_started_at")
        sleep_mode_ended_at = self.storage.get_control_state("sleep_mode_ended_at")
        sleep_mode_reason = "sleep mode is active" if sleep_mode_active else None

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
                
            active_watchlist = [str(s).upper() for s in profile.get("watchlist", [])]
            obs_watchlist = [str(s).upper() for s in profile.get("observation_watchlist", [])]
            proposals_enabled = profile.get("proposals_enabled", True)
            pos_symbols = [str(_value(p, "symbol", "")).upper() for p in positions if _value(p, "symbol")]
            all_symbols = list(dict.fromkeys(active_watchlist + obs_watchlist + pos_symbols))
            
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
                
                pos_obj = next((p for p in positions if str(_value(p, "symbol", "")).upper() == symbol), None)
                has_position = pos_obj is not None
                has_order = any(str(_value(o, "symbol", "")).upper() == symbol for o in orders)
                
                position_drawdown_pct = 0.0
                qty_held = None
                avg_entry_price = None
                latest_position_price = None
                
                if has_position:
                    try:
                        qty_held = float(_value(pos_obj, "qty") or 0.0)
                        avg_entry_price = float(_value(pos_obj, "avg_entry_price") or 0.0)
                        latest_position_price = float(_value(pos_obj, "current_price") or 0.0)
                        
                        # Fallback for average entry price
                        if not avg_entry_price or avg_entry_price <= 0:
                            last_fill = self.storage.fetch_all(
                                "SELECT price FROM fills WHERE order_id IN (SELECT id FROM orders WHERE symbol=? AND side='buy') ORDER BY filled_at DESC LIMIT 1",
                                (symbol,)
                            )
                            if last_fill:
                                avg_entry_price = float(last_fill[0]["price"])
                                logger.info("Using fallback average entry price from fills for %s: %f", symbol, avg_entry_price)
                        
                        if not latest_position_price or latest_position_price <= 0:
                            latest_position_price = price
                            
                        if avg_entry_price and avg_entry_price > 0 and latest_position_price and latest_position_price > 0:
                            position_drawdown_pct = (latest_position_price - avg_entry_price) / avg_entry_price
                        else:
                            position_drawdown_pct = 0.0
                            logger.warning("position_drawdown_unavailable for %s (avg_entry_price=%s, current_price=%s)", symbol, avg_entry_price, latest_position_price)
                            self.storage.audit(self.run_id, "position_drawdown_unavailable", {
                                "symbol": symbol,
                                "avg_entry_price": avg_entry_price,
                                "latest_price": latest_position_price
                            })
                    except Exception as e:
                        position_drawdown_pct = 0.0
                        logger.error("Failed to calculate position drawdown for %s: %s", symbol, e)
                        self.storage.audit(self.run_id, "position_drawdown_unavailable", {
                            "symbol": symbol,
                            "error": str(e)
                        })
                
                signal = evaluate_symbol(
                    symbol,
                    bars,
                    has_position,
                    has_order,
                    market_open,
                    strategy_config["maximum_volatility_20d"],
                    strategy_config["stop_drawdown_pct"],
                    position_drawdown_pct=position_drawdown_pct
                )
                
                # Make sure exits only happen with real position
                if signal.action == "EXIT" and not has_position:
                    signal = evaluate_symbol(
                        symbol,
                        bars,
                        False,
                        has_order,
                        market_open,
                        strategy_config["maximum_volatility_20d"],
                        strategy_config["stop_drawdown_pct"],
                        position_drawdown_pct=0.0
                    )
                
                # Calculate emergency exit risk score and check hard triggers
                emergency_exit_score = None
                emergency_exit_triggered = 0
                emergency_exit_trigger_reason = None
                emergency_exit_hard_trigger = None
                emergency_exit_mode = None
                emergency_exit_wait_seconds = None
                emergency_exit_auto_execute_due_at = None
                emergency_exit_final_decision = None
                emergency_exit_block_reason = None
                atr_value = None
                adverse_move_atr = None
                minutes_to_close = None
                
                if has_position:
                    total_score, breakdown, hard_trigger_matched, hard_trigger_reason = self.calculate_emergency_exit_risk_score(
                        symbol,
                        position_drawdown_pct,
                        avg_entry_price,
                        price,
                        signal.indicators or {},
                        bars
                    )
                    emergency_exit_score = total_score
                    atr_value = breakdown.get("atr_value")
                    adverse_move_atr = breakdown.get("adverse_move_atr")
                    minutes_to_close = breakdown.get("minutes_to_close")
                    
                    if total_score >= 85 and hard_trigger_matched:
                        existing_proposals = self.storage.fetch_all(
                            "SELECT id FROM trade_proposals WHERE symbol=? AND side='sell' AND emergency_exit_triggered=1 AND status IN ('pending', 'approved', 'submitted', 'filled', 'blocked')",
                            (symbol,)
                        )
                        if not existing_proposals:
                            if not avg_entry_price or avg_entry_price <= 0:
                                emergency_exit_triggered = 1
                                emergency_exit_trigger_reason = "drawdown could not be reliably calculated"
                                emergency_exit_hard_trigger = hard_trigger_reason
                                emergency_exit_mode = "blocked"
                                emergency_exit_block_reason = "emergency_drawdown_unavailable"
                                emergency_exit_final_decision = "blocked"
                                
                                self.telegram.send_message(
                                    f"Emergency exit was blocked because position drawdown could not be reliably calculated. No order was placed."
                                )
                                self.storage.audit(self.run_id, "emergency_exit_blocked_drawdown_unavailable", {
                                    "symbol": symbol,
                                    "score": total_score,
                                    "reason": "missing entry price"
                                })
                                signal = dataclasses.replace(signal, action="EXIT", side="sell", reason="Emergency exit blocked: drawdown could not be reliably calculated")
                            else:
                                emergency_exit_triggered = 1
                                emergency_exit_trigger_reason = hard_trigger_reason
                                emergency_exit_hard_trigger = hard_trigger_reason
                                
                                if total_score >= 95 or position_drawdown_pct <= -0.12:
                                    emergency_exit_mode = "extreme"
                                    emergency_exit_wait_seconds = 0
                                elif sleep_mode_active:
                                    emergency_exit_mode = "sleep"
                                    emergency_exit_wait_seconds = 15
                                else:
                                    emergency_exit_mode = "normal"
                                    emergency_exit_wait_seconds = 60
                                    
                                due_at = now + timedelta(seconds=emergency_exit_wait_seconds)
                                emergency_exit_auto_execute_due_at = due_at.isoformat()
                                
                                signal = dataclasses.replace(signal, action="EXIT", side="sell", reason=f"Emergency exit triggered: {hard_trigger_reason}")
                
                exit_trigger_reason = None
                if signal.action == "EXIT" and has_position:
                    if emergency_exit_triggered == 1:
                        exit_trigger_reason = hard_trigger_reason
                    else:
                        above_50 = True
                        if not bars.empty:
                            from app.features import build_features
                            features_df = build_features(bars)
                            if not features_df.empty and "ma_50" in features_df.columns:
                                row = features_df.iloc[-1]
                                above_50 = row["close"] > row["ma_50"]
                            else:
                                above_50 = True
                        drawdown_triggered = (position_drawdown_pct <= -abs(strategy_config["stop_drawdown_pct"]))
                        ma_triggered = not above_50
                        if ma_triggered and drawdown_triggered:
                            exit_trigger_reason = "both close below 50-day MA and drawdown stop reached"
                        elif ma_triggered:
                            exit_trigger_reason = "close below 50-day MA"
                        elif drawdown_triggered:
                            exit_trigger_reason = "drawdown stop reached"
                        else:
                            exit_trigger_reason = "other exit criteria"
                
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
                
                score_rule = 25.0 if signal.action in {"ENTRY", "EXIT"} else 0.0
                score_asset = 15.0 if asset_score >= 80 else (12.0 if asset_score >= 65 else (8.0 if asset_score >= 50 else 3.0))
                
                score_5m = 5.0
                if prev_row:
                    if signal.side == "buy":
                        score_5m = 10.0 if price > prev_price else (5.0 if price == prev_price else 0.0)
                    elif signal.side == "sell":
                        score_5m = 10.0 if price < prev_price else (5.0 if price == prev_price else 0.0)
                    else:
                        score_5m = 5.0 if price == prev_price else (10.0 if price > prev_price else 0.0)
                
                score_session = 5.0
                if session_row:
                    if signal.side == "buy":
                        score_session = 10.0 if price > session_start_price else (5.0 if price == session_start_price else 0.0)
                    elif signal.side == "sell":
                        score_session = 10.0 if price < session_start_price else (5.0 if price == session_start_price else 0.0)
                    else:
                        score_session = 5.0 if price == session_start_price else (10.0 if price > session_start_price else 0.0)
                
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
                
                port_context = self._portfolio_context({"symbol": symbol, "side": signal.side or "buy", "action": "entry"})
                safety_ok = True
                if port_context.get("duplicate_order") or port_context.get("trades_today", 0) >= self.config["risk"].get("max_trades_per_day", 1):
                    safety_ok = False
                if signal.action == "ENTRY" and port_context.get("open_positions", 0) >= self.config["risk"].get("max_open_positions", 1):
                    safety_ok = False
                score_safety = 15.0 if safety_ok else 0.0
                
                age = (now - price_at).total_seconds() if price_at else float("inf")
                fresh_price = -5 <= age <= self.config["risk"].get("max_price_age_seconds", 120)
                enough_bars = len(bars) >= self.config["risk"].get("min_historical_bars", 50)
                score_data = 10.0 if (fresh_price and enough_bars) else (5.0 if fresh_price else 0.0)
                
                score = float(round(score_rule + score_asset + score_5m + score_session + score_vol + score_safety + score_data, 2))
                classification = self._classify_trade_score(score)
                
                system_confidence = "No action suggested"
                if score >= 90:
                    system_confidence = "Very strong"
                elif score >= 80:
                    system_confidence = "Strong"
                elif score >= 65:
                    system_confidence = "Moderate"
                elif score >= 50:
                    system_confidence = "Weak"
                
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
                    "position_drawdown_pct": position_drawdown_pct,
                    "average_entry_price": avg_entry_price,
                    "latest_position_price": latest_position_price,
                    "qty": qty_held,
                    "exit_trigger_reason": exit_trigger_reason,
                    "emergency_exit_score": emergency_exit_score,
                    "emergency_exit_triggered": emergency_exit_triggered,
                    "emergency_exit_trigger_reason": emergency_exit_trigger_reason,
                    "emergency_exit_hard_trigger": emergency_exit_hard_trigger,
                    "emergency_exit_mode": emergency_exit_mode,
                    "emergency_exit_wait_seconds": emergency_exit_wait_seconds,
                    "emergency_exit_auto_execute_due_at": emergency_exit_auto_execute_due_at,
                    "emergency_exit_final_decision": emergency_exit_final_decision,
                    "emergency_exit_block_reason": emergency_exit_block_reason,
                    "atr_value": atr_value,
                    "adverse_move_atr": adverse_move_atr,
                    "minutes_to_close": minutes_to_close,
                })

            # Populate watchlist order first
            for idx, res in enumerate(profile_results):
                res["watchlist_order"] = idx + 1
                res["total_active_symbols"] = len(active_watchlist)

            # Compute true_score_rank among active watchlist candidates
            active_results = [r for r in profile_results if r["symbol"] in active_watchlist]
            def get_vol_regime_rank(regime):
                order = ["normal", "too quiet", "elevated", "high", "extreme"]
                try:
                    return order.index(regime)
                except ValueError:
                    return len(order)
            
            def score_sort_key(candidate):
                return (
                    -candidate["score"],
                    -candidate["asset_score"],
                    candidate["watchlist_order"],
                    get_vol_regime_rank(candidate["volatility_regime"]),
                    -candidate["price_change_pct"],
                    -candidate["session_change_pct"],
                    candidate["symbol"]
                )
            
            active_results.sort(key=score_sort_key)
            for rank_idx, res in enumerate(active_results):
                res["true_score_rank"] = rank_idx + 1
            
            # For non-active watchlist candidates, true_score_rank is None
            for res in profile_results:
                if res["symbol"] not in active_watchlist:
                    res["true_score_rank"] = None

            # Split exits vs buys
            exit_candidates = [r for r in profile_results if r["signal"].action == "EXIT" and r["has_position"]]
            buy_candidates_all = [r for r in profile_results if r["signal"].action == "ENTRY" and r["signal"].side == "buy"]
            
            exit_candidates_exist = len(exit_candidates) > 0
            
            # Setup key and dedupe status pre-evaluation
            for res in profile_results:
                symbol = res["symbol"]
                signal = res["signal"]
                score = res["score"]
                has_position = res["has_position"]
                volatility_regime = res["volatility_regime"]
                
                # Check 1: Pending proposal blocks duplicate
                pending_proposals = self.storage.fetch_all(
                    "SELECT * FROM trade_proposals WHERE symbol=? AND side=? AND status='pending'",
                    (symbol, signal.side)
                )
                
                # Compute setup key
                setup_key = self._compute_setup_key(symbol, signal.side, signal.action, signal.indicators, score)
                res["setup_key"] = setup_key
                
                cooldown_applied = 0
                cooldown_remaining_minutes = 0.0
                cooldown_reason = None
                revival_reason = None
                last_proposal_status = None
                last_proposal_score = 0.0
                score_delta = 0.0
                volatility_regime_change = None
                
                if pending_proposals:
                    cooldown_applied = 1
                    cooldown_reason = "pending_proposal_exists"
                    last_proposal_status = "pending"
                    dedupe_status = "suppressed"
                    dedupe_reason = "active/pending similar proposal exists"
                else:
                    # Approved/submitted BUY blocks competing BUYs
                    if signal.side == "buy":
                        competing = self.storage.fetch_all(
                            "SELECT * FROM trade_proposals WHERE symbol=? AND side='buy' AND status IN ('approved', 'submitted') AND created_at >= ?",
                            (symbol, (now - timedelta(minutes=5)).isoformat())
                        )
                        if competing:
                            cooldown_applied = 1
                            cooldown_reason = "competing approved/submitted buy proposal exists"
                            last_proposal_status = competing[0]["status"]
                            dedupe_status = "suppressed"
                            dedupe_reason = "competing approved/submitted buy proposal exists"
                    
                    if not cooldown_applied:
                        # Check setup key cooldown
                        last_prop_rows = self.storage.fetch_all(
                            "SELECT status, created_at, payload FROM trade_proposals WHERE setup_key=? ORDER BY created_at DESC LIMIT 1",
                            (setup_key,)
                        )
                        if last_prop_rows:
                            last_prop = last_prop_rows[0]
                            last_proposal_status = last_prop["status"]
                            last_created_at = datetime.fromisoformat(last_prop["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
                            elapsed_mins = (now - last_created_at).total_seconds() / 60
                            
                            try:
                                payload_dict = json.loads(last_prop["payload"])
                                last_score = float(payload_dict.get("score", 0))
                                last_vol_regime = payload_dict.get("volatility_regime", "normal")
                            except Exception:
                                last_score = float(last_prop.get("score") or 0.0)
                                last_vol_regime = "normal"
                                
                            last_proposal_score = last_score
                            score_delta = score - last_score
                            
                            cooldown_duration = 60.0
                            if last_proposal_status == "rejected":
                                cooldown_duration = 120.0
                                
                            if elapsed_mins < cooldown_duration:
                                cooldown_applied = 1
                                cooldown_remaining_minutes = max(0.0, cooldown_duration - elapsed_mins)
                                cooldown_reason = f"setup cooldown active (status: {last_proposal_status})"
                                dedupe_status = "suppressed"
                                dedupe_reason = f"duplicate proposal cooldown (elapsed: {elapsed_mins:.1f}m)"
                                
                                # Revival checks
                                is_exit_action = (signal.action == "EXIT" and has_position)
                                if is_exit_action:
                                    cooldown_applied = 0
                                    dedupe_status = "allowed"
                                    dedupe_reason = "exit/reduce-risk action bypasses cooldown"
                                    revival_reason = "exit/reduce-risk action bypasses cooldown"
                                elif score_delta >= 10.0:
                                    cooldown_applied = 0
                                    dedupe_status = "allowed"
                                    dedupe_reason = f"meaningful score improvement (score: {score:.1f} vs previous: {last_score:.1f})"
                                    revival_reason = f"meaningful score improvement (score: {score:.1f} vs previous: {last_score:.1f})"
                                elif last_vol_regime != volatility_regime:
                                    volatility_regime_change = f"{last_vol_regime}->{volatility_regime}"
                                    vol_improved = False
                                    if last_vol_regime == "extreme" and volatility_regime in ("high", "elevated", "normal"):
                                        vol_improved = True
                                    elif last_vol_regime == "high" and volatility_regime in ("elevated", "normal"):
                                        vol_improved = True
                                    elif last_vol_regime == "elevated" and volatility_regime == "normal":
                                        vol_improved = True
                                        
                                    if vol_improved:
                                        cooldown_applied = 0
                                        dedupe_status = "allowed"
                                        dedupe_reason = f"volatility regime improved from {last_vol_regime} to {volatility_regime}"
                                        revival_reason = f"volatility regime improved from {last_vol_regime} to {volatility_regime}"
                            else:
                                dedupe_status = "allowed"
                                dedupe_reason = "cooldown expired"
                        else:
                            dedupe_status = "allowed"
                            dedupe_reason = "no previous setup proposal found"
                            
                res["cooldown_applied"] = cooldown_applied
                res["cooldown_remaining_minutes"] = cooldown_remaining_minutes
                res["cooldown_reason"] = cooldown_reason
                res["revival_reason"] = revival_reason
                res["last_proposal_status"] = last_proposal_status
                res["last_proposal_score"] = last_proposal_score
                res["score_delta"] = score_delta
                res["volatility_regime_change"] = volatility_regime_change
                res["dedupe_status"] = dedupe_status
                res["dedupe_reason"] = dedupe_reason

            # Filter eligible BUY candidates (only those allowed by cooldown)
            buy_candidates = []
            for res in buy_candidates_all:
                ai_config = self.config.get("ai", {})
                is_eligible_buy = (
                    res["symbol"] in active_watchlist
                    and proposals_enabled
                    and res["score"] >= ai_config.get("ai_review_min_score", 65)
                    and res["dedupe_status"] == "allowed"
                )
                if is_eligible_buy:
                    buy_candidates.append(res)
                    
            buy_candidates.sort(key=score_sort_key)
            
            risk_cfg = self.config.get("risk", {})
            max_new_buy_per_cycle = risk_cfg.get("max_new_buy_proposals_per_cycle", 1)
            max_pending_buy = risk_cfg.get("max_pending_buy_proposals", 1)
            pending_in_db = self.storage.fetch_all("SELECT COUNT(*) as cnt FROM trade_proposals WHERE side='buy' AND status='pending'")[0]["cnt"]
            allowed_new_buys = max(0, min(max_new_buy_per_cycle, max_pending_buy - pending_in_db))
            
            allowed_buy_symbols = {c["symbol"] for c in buy_candidates[:allowed_new_buys]}
            suppressed_buy_symbols = {c["symbol"] for c in buy_candidates[allowed_new_buys:]}
            
            any_generated = False
            
            # Sort profile_results: EXITS first, then BUYS, then HOLDS
            def scan_processing_order(r):
                action = r["signal"].action
                if action == "EXIT":
                    return 0
                elif action == "ENTRY":
                    return 1
                else:
                    return 2
            profile_results.sort(key=scan_processing_order)
            
            # Proposal generation loop
            for idx, res in enumerate(profile_results):
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
                
                # New fields from Phase 3 & 6
                position_drawdown_pct = res.get("position_drawdown_pct", 0.0)
                average_entry_price = res.get("average_entry_price")
                latest_position_price = res.get("latest_position_price")
                qty_held = res.get("qty")
                exit_trigger_reason = res.get("exit_trigger_reason")
                setup_key = res.get("setup_key")
                cooldown_applied = res.get("cooldown_applied", 0)
                cooldown_remaining_minutes = res.get("cooldown_remaining_minutes", 0.0)
                cooldown_reason = res.get("cooldown_reason")
                revival_reason = res.get("revival_reason")
                last_proposal_status = res.get("last_proposal_status")
                last_proposal_score = res.get("last_proposal_score", 0.0)
                score_delta = res.get("score_delta", 0.0)
                volatility_regime_change = res.get("volatility_regime_change")
                true_score_rank = res.get("true_score_rank")
                watchlist_order = res.get("watchlist_order")
                
                emergency_exit_score = res.get("emergency_exit_score")
                emergency_exit_triggered = res.get("emergency_exit_triggered", 0)
                emergency_exit_trigger_reason = res.get("emergency_exit_trigger_reason")
                emergency_exit_hard_trigger = res.get("emergency_exit_hard_trigger")
                emergency_exit_mode = res.get("emergency_exit_mode")
                emergency_exit_wait_seconds = res.get("emergency_exit_wait_seconds")
                emergency_exit_auto_execute_due_at = res.get("emergency_exit_auto_execute_due_at")
                emergency_exit_final_decision = res.get("emergency_exit_final_decision")
                emergency_exit_block_reason = res.get("emergency_exit_block_reason")
                atr_value = res.get("atr_value")
                adverse_move_atr = res.get("adverse_move_atr")
                minutes_to_close = res.get("minutes_to_close")
                
                ai_config = self.config.get("ai", {})
                is_buy = (signal.action == "ENTRY" and signal.side == "buy")
                is_exit = (signal.action == "EXIT" and signal.side == "sell")
                
                suppressed_by_sleep_mode = 0
                sleep_mode_suppressed_candidate = 0
                
                # Check sleep mode suppression for buys
                if is_buy and sleep_mode_active:
                    proposal_allowed = False
                    no_action_reason = "suppressed by sleep mode"
                    candidate_suppression_reason = "suppressed_by_sleep_mode"
                    suppressed_by_sleep_mode = 1
                    sleep_mode_suppressed_candidate = 1
                elif emergency_exit_triggered == 1:
                    proposal_allowed = True
                    is_buy = False
                    is_exit = True
                else:
                    proposal_allowed = (symbol in active_watchlist and proposals_enabled and signal.action in {"ENTRY", "EXIT"} and score >= ai_config.get("ai_review_min_score", 65))
                
                gpt_called = False
                proposal_generated = False
                no_action_reason = "" if not (is_buy and sleep_mode_active) else "suppressed by sleep mode"
                proposal_id = None
                decision = None
                proposal = None
                review = None
                dedupe_status = res.get("dedupe_status", "skipped")
                dedupe_reason = res.get("dedupe_reason", "not eligible for proposal")
                paper_size_adjustment = 1.0
                candidate_suppression_reason = None if not (is_buy and sleep_mode_active) else "suppressed_by_sleep_mode"
                deferred_ai_review_reason = None
                
                exit_priority_applied = 1 if exit_candidates_exist else 0
                gpt_exit_explanation_status = None
                gpt_exit_confidence = None
                gpt_exit_caution = None
                final_proposal_message_category = "buy" if is_buy else ("exit" if is_exit else "suppressed")
                
                # Check exit prioritization suppression for buys
                if is_buy and exit_candidates_exist and not sleep_mode_active:
                    proposal_allowed = False
                    dedupe_status = "suppressed"
                    dedupe_reason = "suppressed due to exit priority"
                    no_action_reason = "suppressed due to exit priority"
                    candidate_suppression_reason = "suppressed_due_to_exit_priority"
                    self.storage.audit(self.run_id, "proposal_suppressed", {
                        "symbol": symbol, "reason": "suppressed_due_to_exit_priority", "score": score
                    })
                
                if not proposal_allowed:
                    if is_buy and suppressed_by_sleep_mode == 1:
                        pass
                    elif is_buy and candidate_suppression_reason == "suppressed_due_to_exit_priority":
                        pass # Reason already set
                    elif is_buy and symbol in suppressed_buy_symbols:
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
                    # Check deduplication suppression
                    if res["dedupe_status"] == "suppressed" and not (is_exit and has_position) and not emergency_exit_triggered: # exits bypass buy cooldown/dedupe
                        proposal_allowed = False
                        no_action_reason = f"suppressed by dedupe: {dedupe_reason}"
                        self.storage.audit(self.run_id, "proposal_deduplicated", {
                            "symbol": symbol, "side": signal.side, "status": "suppressed", "reason": dedupe_reason
                        })
                    else:
                        # Check candidate limit suppression for buys
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
                                    if r["symbol"] in active_watchlist and r.get("true_score_rank") is not None and true_score_rank is not None and r["true_score_rank"] < true_score_rank:
                                        r_sig = r["signal"]
                                        if r_sig.action == "ENTRY" and r_sig.side == "buy":
                                            if r.get("cooldown_applied") == 1:
                                                if "pending" in str(r.get("cooldown_reason")):
                                                    higher_rank_suppressed_pending = True
                                                else:
                                                    higher_rank_suppressed_cooldown = True
                                                    
                                if higher_rank_suppressed_cooldown:
                                    selection_reason = "Selected because higher-scoring candidates were recently proposed and are still cooling down."
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
                                "qty": qty_held,
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
                                "symbol_rank": watchlist_order,
                                "total_active_symbols": len(active_watchlist),
                                "price_change_pct": price_change_pct,
                                "session_change_pct": session_change_pct,
                                "gpt_called": gpt_called,
                                "proposal_market_rank": watchlist_order,
                                "proposal_eligible_rank": eligible_rank,
                                "selection_reason": selection_reason,
                                "true_score_rank": true_score_rank,
                                "watchlist_order": watchlist_order,
                                "position_drawdown_pct": position_drawdown_pct,
                                "average_entry_price": average_entry_price,
                                "latest_position_price": latest_position_price,
                                "exit_trigger_reason": exit_trigger_reason,
                                "setup_key": setup_key,
                                "revival_reason": revival_reason,
                                "exit_priority_applied": exit_priority_applied,
                                # Emergency fields
                                "emergency_exit_score": emergency_exit_score,
                                "emergency_exit_triggered": emergency_exit_triggered,
                                "emergency_exit_trigger_reason": emergency_exit_trigger_reason,
                                "emergency_exit_hard_trigger": emergency_exit_hard_trigger,
                                "emergency_exit_mode": emergency_exit_mode,
                                "emergency_exit_wait_seconds": emergency_exit_wait_seconds,
                                "emergency_exit_auto_execute_due_at": emergency_exit_auto_execute_due_at,
                                "emergency_exit_final_decision": emergency_exit_final_decision,
                                "emergency_exit_block_reason": emergency_exit_block_reason,
                                "atr_value": atr_value,
                                "adverse_move_atr": adverse_move_atr,
                                "minutes_to_close": minutes_to_close,
                                "sleep_mode_active": 1 if sleep_mode_active else 0,
                                "suppressed_by_sleep_mode": 1 if suppressed_by_sleep_mode else 0,
                                "sleep_mode_reason": sleep_mode_reason,
                                "sleep_mode_suppressed_candidate": 1 if sleep_mode_suppressed_candidate else 0,
                                "sleep_mode_started_at": sleep_mode_started_at,
                                "sleep_mode_ended_at": sleep_mode_ended_at,
                            }
                            
                            if emergency_exit_triggered == 1:
                                # Emergency exits bypass standard risk engine proposal evaluations
                                pass
                            else:
                                self._should_auto_execute(proposal)
                                decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, self._portfolio_context(proposal))
                                if not decision.passed:
                                    no_action_reason = f"blocked by risk checks: {'; '.join(decision.reasons)}"
                                    proposal_allowed = False
                                    
                            if proposal_allowed:
                                if is_buy:
                                    require_gpt = self.config.get("risk", {}).get("require_gpt_review_for_buy_proposals", True)
                                    calls_today = len(self.storage.fetch_all("SELECT id FROM ai_reviews WHERE created_at >= ?", (today_start,)))
                                    last_call = self.storage.fetch_all("SELECT created_at FROM ai_reviews WHERE proposal_id IN (SELECT id FROM trade_proposals WHERE symbol=?) ORDER BY created_at DESC LIMIT 1", (symbol,))
                                    time_since = (now - datetime.fromisoformat(last_call[0]["created_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)).total_seconds() / 60 if last_call else float("inf")
                                    if (ai_config.get("ai_review_on_every_run", False) or (calls_today < ai_config.get("ai_daily_call_limit", 10) and self.ai.calls_made < ai_config.get("ai_max_calls_per_run", 2) and time_since >= ai_config.get("ai_review_min_interval_minutes", 30))):
                                        gpt_called = True
                                        
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
                                    
                                    if require_gpt and not gpt_called:
                                        proposal_generated = False
                                        proposal_allowed = False
                                        no_action_reason = "deferred due to AI review throttling/unavailability"
                                        deferred_ai_review_reason = "deferred_ai_review_unavailable"
                                        final_proposal_message_category = "deferred"
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
                                elif is_exit:
                                    if emergency_exit_triggered == 1:
                                        # Special emergency exit review & immediate execution check
                                        gpt_explanation = self.get_gpt_exit_explanation(proposal)
                                        gpt_exit_explanation_status = gpt_explanation["status"]
                                        gpt_exit_confidence = gpt_explanation["confidence"]
                                        gpt_exit_caution = gpt_explanation["caution"]
                                        gpt_called = (gpt_explanation["status"] == "Completed")
                                        
                                        proposal["gpt_called"] = gpt_called
                                        proposal["gpt_exit_explanation_status"] = gpt_exit_explanation_status
                                        proposal["gpt_exit_confidence"] = gpt_exit_confidence
                                        proposal["gpt_exit_caution"] = gpt_exit_caution
                                        
                                        review = {
                                            "summary": gpt_explanation.get("telegram_message") or f"Emergency exit triggered: {emergency_exit_trigger_reason}",
                                            "risks": [emergency_exit_trigger_reason],
                                            "caution_level": gpt_exit_caution,
                                            "gpt_confidence": gpt_exit_confidence,
                                            "gpt_caution": gpt_exit_caution,
                                            "main_risk": gpt_explanation.get("main_risk") or "N/A"
                                        }
                                        proposal["review"] = review
                                        proposal_generated = True
                                        no_action_reason = "proposal generated"
                                        any_generated = True
                                        
                                        if emergency_exit_mode == "blocked":
                                            proposal["status"] = "blocked"
                                            proposal["emergency_exit_block_reason"] = "emergency_drawdown_unavailable"
                                            proposal["emergency_exit_final_decision"] = "blocked"
                                        elif emergency_exit_mode == "extreme":
                                            success, err_reason = self.revalidate_and_execute_emergency_exit(proposal)
                                            if success:
                                                emergency_exit_final_decision = "submitted"
                                                proposal["status"] = "approved"
                                                proposal["emergency_exit_final_decision"] = "submitted"
                                                self.telegram.send_message(
                                                    f"🚨 [EXTREME EMERGENCY EXIT] Immediate paper market order submitted for {symbol} ({qty_held} shares) due to high risk score {emergency_exit_score:.1f}. Reason: {emergency_exit_trigger_reason}."
                                                )
                                                self.storage.audit(self.run_id, "emergency_exit_submitted", {"symbol": symbol, "score": emergency_exit_score})
                                            else:
                                                emergency_exit_block_reason = err_reason
                                                proposal["status"] = "blocked"
                                                proposal["emergency_exit_block_reason"] = err_reason
                                                self.telegram.send_message(
                                                    f"🚨 [EXTREME EMERGENCY EXIT] Triggered for {symbol} but execution was blocked: {err_reason}."
                                                )
                                                self.storage.audit(self.run_id, "emergency_exit_blocked", {"symbol": symbol, "reason": err_reason})
                                        elif emergency_exit_mode == "sleep":
                                            self.telegram.send_message(
                                                f"🚨 [SLEEP MODE EMERGENCY EXIT] Triggered for {symbol} ({qty_held} shares). Risk score: {emergency_exit_score:.1f}. Reason: {emergency_exit_trigger_reason}. "
                                                f"Auto-executing in 15 seconds unless cancelled."
                                            )
                                        else:
                                            self.telegram.send_message(
                                                f"🚨 [EMERGENCY EXIT ALERT] Triggered for {symbol} ({qty_held} shares). Risk score: {emergency_exit_score:.1f}. Reason: {emergency_exit_trigger_reason}.\n\n"
                                                f"I will auto-execute in 60 seconds. Reply YES to execute immediately, or NO to cancel."
                                            )
                                    else:
                                        # Normal exit GPT explanation check
                                        use_gpt_for_exits = self.config.get("risk", {}).get("use_gpt_for_exit_explanations", True)
                                        gpt_timeout = self.config.get("risk", {}).get("exit_gpt_max_wait_seconds", 3)
                                        
                                        if use_gpt_for_exits:
                                            import concurrent.futures
                                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                                                future = executor.submit(self.ai.review, proposal)
                                                try:
                                                    review = future.result(timeout=gpt_timeout)
                                                    gpt_called = True
                                                    gpt_exit_explanation_status = "Completed"
                                                except Exception as e:
                                                    logger.warning("GPT exit explanation failed or timed out: %s", e)
                                                    gpt_called = False
                                                    gpt_exit_explanation_status = "Not available; using rule-based exit reason"
                                                    review = deterministic_review(proposal, warning="AI review unavailable")
                                        else:
                                            gpt_called = False
                                            gpt_exit_explanation_status = "Not available; using rule-based exit reason"
                                            review = deterministic_review(proposal, warning="AI review disabled for exits")
                                            
                                        proposal["gpt_called"] = gpt_called
                                        proposal["review"] = review
                                        proposal["gpt_exit_explanation_status"] = gpt_exit_explanation_status
                                        if review:
                                            proposal["gpt_exit_confidence"] = review.get("gpt_confidence", "Not called")
                                            proposal["gpt_exit_caution"] = review.get("gpt_caution", "Low")
                                            
                                        proposal_generated = True
                                        no_action_reason = "proposal generated"
                                        any_generated = True
                            
                if proposal_allowed and is_buy:
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
                
                # Check category for final_proposal_message_category
                if not proposal_generated:
                    final_proposal_message_category = "suppressed"
                elif is_buy:
                    final_proposal_message_category = "buy"
                elif is_exit:
                    final_proposal_message_category = "exit"
                else:
                    final_proposal_message_category = "suppressed"
                
                self.storage.execute(
                    "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification,symbol_rank,proposal_generated,no_action_reason,asset_selection_score,trade_decision_score,system_confidence,gpt_confidence,gpt_caution,expiry_minutes,expires_at_sgt,main_risk,volatility_regime,volatility_score_contribution,volatility_gate_result,dedupe_status,dedupe_reason,paper_size_adjustment,candidate_suppression_reason,deferred_ai_review_reason,true_score_rank,watchlist_order,setup_key,cooldown_applied,cooldown_remaining_minutes,cooldown_reason,revival_reason,last_proposal_status,score_delta,volatility_regime_change,exit_priority_applied,exit_trigger_reason,position_drawdown_pct,average_entry_price,latest_position_price,gpt_exit_explanation_status,gpt_exit_confidence,gpt_exit_caution,final_proposal_message_category,emergency_exit_score,emergency_exit_triggered,emergency_exit_trigger_reason,emergency_exit_hard_trigger,emergency_exit_mode,emergency_exit_wait_seconds,emergency_exit_user_response,emergency_exit_auto_execute_due_at,emergency_exit_auto_execute_attempted_at,emergency_exit_final_decision,emergency_exit_block_reason,current_price,atr_value,adverse_move_atr,minutes_to_close,sleep_mode_active,suppressed_by_sleep_mode,sleep_mode_reason,sleep_mode_suppressed_candidate,sleep_mode_started_at,sleep_mode_ended_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, profile_key, symbol, price, prev_price, price_change, price_change_pct, session_start_price, session_change, vol_20 or 0.0, signal.action, score, classification, signal.reason, int(proposal_allowed), int(gpt_called), now.isoformat(), asset_score, asset_classification, watchlist_order, int(proposal_generated), no_action_reason, asset_score, score, system_confidence, g_conf, g_caut, expiry_minutes, exp_sgt, m_risk, volatility_regime, score_vol, volatility_gate_result, dedupe_status, dedupe_reason, paper_size_adjustment, candidate_suppression_reason, deferred_ai_review_reason, true_score_rank, watchlist_order, setup_key, int(cooldown_applied), cooldown_remaining_minutes, cooldown_reason, revival_reason, last_proposal_status, score_delta, volatility_regime_change, int(exit_priority_applied), exit_trigger_reason, position_drawdown_pct, average_entry_price, latest_position_price, gpt_exit_explanation_status, gpt_exit_confidence, gpt_exit_caution, final_proposal_message_category, emergency_exit_score, emergency_exit_triggered, emergency_exit_trigger_reason, emergency_exit_hard_trigger, emergency_exit_mode, emergency_exit_wait_seconds, None, emergency_exit_auto_execute_due_at, None, emergency_exit_final_decision, emergency_exit_block_reason, price, atr_value, adverse_move_atr, minutes_to_close, 1 if sleep_mode_active else 0, suppressed_by_sleep_mode, sleep_mode_reason, sleep_mode_suppressed_candidate, sleep_mode_started_at, sleep_mode_ended_at)
                )
                
                logger.info(
                    "Symbol: %s | Profile: %s | Asset Score: %.2f (%s) | Trade Score: %.2f (%s) | Watchlist Order: #%d | True Score Rank: %s | Prev Change: %.2f%% | Session Change: %.2f | Proposal Allowed: %s | GPT Called: %s | Proposal Generated: %s | No-Action Reason: %s",
                    symbol, profile_key, asset_score, asset_classification, score, classification, watchlist_order, true_score_rank, price_change_pct, session_change, proposal_allowed, gpt_called, proposal_generated, no_action_reason or "N/A"
                )
                
                if not proposal_generated:
                    if proposal_allowed and decision and not decision.passed:
                        self.storage.audit(self.run_id, "proposal_blocked", {"symbol": symbol, "reasons": decision.reasons})
                    continue
                    
                self.storage.execute(
                    "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,proposal_market_rank,proposal_eligible_rank,selection_reason,ai_review_status,ai_confidence,ai_caution,true_score_rank,watchlist_order,setup_key,cooldown_applied,cooldown_remaining_minutes,cooldown_reason,revival_reason,last_proposal_status,score_delta,volatility_regime_change,exit_priority_applied,exit_trigger_reason,position_drawdown_pct,average_entry_price,latest_position_price,gpt_exit_explanation_status,gpt_exit_confidence,gpt_exit_caution,final_proposal_message_category,emergency_exit_score,emergency_exit_triggered,emergency_exit_trigger_reason,emergency_exit_hard_trigger,emergency_exit_mode,emergency_exit_wait_seconds,emergency_exit_user_response,emergency_exit_auto_execute_due_at,emergency_exit_auto_execute_attempted_at,emergency_exit_final_decision,emergency_exit_block_reason,current_price,atr_value,adverse_move_atr,minutes_to_close,sleep_mode_active,suppressed_by_sleep_mode,sleep_mode_reason,sleep_mode_suppressed_candidate,sleep_mode_started_at,sleep_mode_ended_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal_id,
                        self.run_id,
                        signal_id,
                        symbol,
                        signal.side,
                        proposal["notional"],
                        proposal.get("status", "pending"),
                        now.isoformat(),
                        expiry.isoformat(),
                        signal.strategy_version,
                        json_dumps(proposal),
                        watchlist_order,
                        eligible_rank,
                        selection_reason,
                        proposal.get("ai_review_status") if is_buy else None,
                        proposal.get("ai_confidence") if is_buy else None,
                        proposal.get("ai_caution") if is_buy else None,
                        true_score_rank,
                        watchlist_order,
                        setup_key,
                        int(cooldown_applied),
                        cooldown_remaining_minutes,
                        cooldown_reason,
                        revival_reason,
                        last_proposal_status,
                        score_delta,
                        volatility_regime_change,
                        int(exit_priority_applied),
                        exit_trigger_reason,
                        position_drawdown_pct,
                        average_entry_price,
                        latest_position_price,
                        gpt_exit_explanation_status,
                        gpt_exit_confidence,
                        gpt_exit_caution,
                        final_proposal_message_category,
                        emergency_exit_score,
                        emergency_exit_triggered,
                        emergency_exit_trigger_reason,
                        emergency_exit_hard_trigger,
                        emergency_exit_mode,
                        emergency_exit_wait_seconds,
                        proposal.get("emergency_exit_user_response"),
                        emergency_exit_auto_execute_due_at,
                        proposal.get("emergency_exit_auto_execute_attempted_at"),
                        emergency_exit_final_decision,
                        emergency_exit_block_reason,
                        price,
                        atr_value,
                        adverse_move_atr,
                        minutes_to_close,
                        1 if sleep_mode_active else 0,
                        suppressed_by_sleep_mode,
                        sleep_mode_reason,
                        sleep_mode_suppressed_candidate,
                        sleep_mode_started_at,
                        sleep_mode_ended_at
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
