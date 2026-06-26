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
from .data_providers.eodhd import EODHDProvider
from .dynamic_universe import DynamicUniverseEngine, OBSERVATION, PAPER_TRADABLE, RESEARCH_CANDIDATE
from .execution import Executor, ExecutionResult
from .internet import internet_available
from .market_data import normalize_bars
from .power import get_power_status
from .position_management import PositionManagementDecision, PositionManagementEngine
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


def _format_small_percent(value: float | int | None) -> str:
    if value is None:
        return "0.00%"
    numeric = float(value)
    if 0 < abs(numeric) < 0.01:
        return "<0.01%"
    return f"{numeric:.2f}%"


def _format_expiry_line(expires_at: str | datetime, now: datetime | None = None) -> str:
    expiry_dt = _parse_datetime(expires_at)
    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    minutes = max(0, int((expiry_dt - now_dt).total_seconds() // 60))
    return f"Expires: {_format_sgt_time(expiry_dt)} SGT" + (f" ({minutes} min left)" if minutes > 0 else "")


def _normalize_ranked_candidate_reason(reason: str | None, rank: int) -> str:
    normalized = str(reason or "").strip()
    strongest_boilerplate = {
        "selected because it was the strongest eligible candidate.",
        "selected as the strongest eligible candidate.",
        "strongest eligible candidate",
    }
    if rank == 1:
        if not normalized:
            return "Selected as the strongest eligible candidate."
        return normalized
    if not normalized or normalized.lower() in strongest_boilerplate:
        return f"Included as ranked eligible candidate #{rank} after risk-budget checks."
    return normalized


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

    def _exit_blocker_context(self, broker_orders: list[Any] | None = None) -> dict[str, Any]:
        open_sell_orders = []
        for order in broker_orders or []:
            side = str(_value(order, "side", "")).lower()
            status = str(_value(order, "status", "")).lower()
            if side == "sell" and status not in {"filled", "canceled", "cancelled", "expired", "rejected"}:
                open_sell_orders.append(order)
        if open_sell_orders:
            order = open_sell_orders[0]
            symbol = str(_value(order, "symbol", "")).upper()
            return {
                "active": True,
                "symbol": symbol,
                "reason": f"{symbol} SELL order open",
                "status": str(_value(order, "status", "open")),
                "source": "broker_open_order",
                "stale": False,
            }

        now_iso = iso_now()
        active_rows = self.storage.fetch_all(
            """
            SELECT id, symbol, status, created_at, expires_at, emergency_exit_triggered,
                   emergency_exit_score, emergency_exit_trigger_reason, exit_trigger_reason
            FROM trade_proposals
            WHERE side='sell'
              AND status IN ('pending','approved')
              AND expires_at>?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (now_iso,),
        )
        if active_rows:
            row = active_rows[0]
            symbol = str(row["symbol"]).upper()
            if row.get("emergency_exit_triggered") == 1:
                reason = f"{symbol} emergency exit review active"
            else:
                reason = f"{symbol} EXIT proposal pending"
            return {
                "active": True,
                "symbol": symbol,
                "reason": reason,
                "status": row["status"],
                "source": "trade_proposals",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "emergency_exit_score": row.get("emergency_exit_score"),
                "stale": False,
            }

        active_batch_rows = self.storage.fetch_all(
            """
            SELECT c.id, c.candidate_symbol, c.candidate_action, c.candidate_status, c.created_at, c.expires_at, b.id AS batch_id
            FROM proposal_batch_candidates c
            JOIN proposal_batches b ON b.id=c.batch_id
            WHERE c.candidate_status='pending'
              AND b.status IN ('pending','partially_approved')
              AND c.expires_at>?
              AND (
                lower(c.candidate_side)='sell'
                OR upper(c.candidate_action) IN ('SELL','EXIT')
              )
            ORDER BY c.created_at DESC
            LIMIT 1
            """,
            (now_iso,),
        )
        if active_batch_rows:
            row = active_batch_rows[0]
            symbol = str(row["candidate_symbol"]).upper()
            return {
                "active": True,
                "symbol": symbol,
                "reason": f"{symbol} EXIT batch candidate pending",
                "status": row["candidate_status"],
                "source": "proposal_batch_candidates",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "batch_id": row["batch_id"],
                "stale": False,
            }

        stale_rows = self.storage.fetch_all(
            """
            SELECT id, symbol, status, created_at, expires_at
            FROM trade_proposals
            WHERE side='sell'
              AND status IN ('pending','approved','submitted','filled','blocked','expired','rejected','superseded','stale_resolved')
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        if stale_rows:
            row = stale_rows[0]
            symbol = str(row["symbol"]).upper()
            return {
                "active": False,
                "symbol": symbol,
                "reason": f"stale {symbol} exit flag ignored",
                "status": row["status"],
                "source": "trade_proposals",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "stale": True,
            }

        return {"active": False, "symbol": None, "reason": None, "status": None, "source": None, "stale": False}

    def _sleep_mode_active(self) -> bool:
        try:
            return int(self.storage.get_control_state("sleep_mode_active", "0")) == 1
        except (TypeError, ValueError):
            return False

    def _dynamic_universe_engine(self) -> DynamicUniverseEngine | None:
        du_cfg = self.config.get("dynamic_universe", {}) or {}
        if not du_cfg.get("enabled", False) or self.config.get("mode") != "paper":
            return None
        provider_name = self.config.get("data_providers", {}).get("dynamic_universe_provider", du_cfg.get("provider", "eodhd"))
        provider = None
        if provider_name == "eodhd" and self.config.get("eodhd", {}).get("enabled", True):
            provider = EODHDProvider(self.config, self.storage, self.run_id)
        return DynamicUniverseEngine(self.config, self.storage, provider, self.run_id, self.broker)

    def _dynamic_universe_scan_symbols(self) -> tuple[list[str], list[str]]:
        engine = self._dynamic_universe_engine()
        if not engine:
            return [], []
        try:
            return engine.dynamic_scan_symbols()
        except Exception as exc:
            self.storage.audit(self.run_id, "dynamic_universe_scan_symbols_failed", {"error": type(exc).__name__})
            return [], []

    def _dynamic_universe_event_refresh_due(self) -> bool:
        if not self.config.get("dynamic_universe", {}).get("schedules", {}).get("event_triggered_refresh_enabled", True):
            return False
        cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        rows = self.storage.fetch_all(
            """
            SELECT 1 FROM fills WHERE filled_at>=?
            UNION ALL
            SELECT 1 FROM trade_proposals WHERE status='expired' AND expires_at>=?
            UNION ALL
            SELECT 1 FROM audit_events WHERE event_type LIKE 'emergency_exit%' AND created_at>=?
            LIMIT 1
            """,
            (cutoff, cutoff, cutoff),
        )
        return bool(rows)

    def _run_dynamic_universe_due(self) -> list[dict[str, Any]]:
        engine = self._dynamic_universe_engine()
        if not engine:
            return []
        run_types = ["daily_deep_research", "intraday_light_refresh", "post_market_review", "weekly_cleanup"]
        if self._dynamic_universe_event_refresh_due():
            run_types.append("event_triggered_refresh")
        try:
            return engine.run_due(run_types=run_types)
        except Exception as exc:
            self.storage.audit(self.run_id, "dynamic_universe_due_failed", {"error": type(exc).__name__})
            return []

    def _sleep_mode_blocks_approval(self, proposal: dict[str, Any]) -> bool:
        side = str(proposal.get("side") or proposal.get("candidate_side") or "").lower()
        action = str(proposal.get("action") or proposal.get("candidate_action") or "").lower()
        risk_reducing = side == "sell" or action in {"sell", "exit"}
        buy_or_add = side == "buy" or action in {"buy", "add", "entry"}
        return self._sleep_mode_active() and buy_or_add and not risk_reducing

    def _position_management_state(self, symbol: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all("SELECT * FROM position_management_state WHERE symbol=?", (symbol.upper(),))
        return rows[0] if rows else None

    def _initial_risk_seed_for_position(self, symbol: str) -> dict[str, Any]:
        existing_state = self._position_management_state(symbol)
        if existing_state and existing_state.get("initial_stop_price") is not None:
            return {
                "initial_stop_price": existing_state.get("initial_stop_price"),
                "initial_risk_per_share": existing_state.get("initial_risk_per_share"),
                "initial_risk_pct": existing_state.get("initial_risk_pct"),
                "initial_risk_dollars": existing_state.get("initial_risk_dollars"),
                "stop_model": existing_state.get("stop_model"),
                "stop_source": existing_state.get("stop_source"),
                "entry_price_for_r": existing_state.get("entry_price_for_r"),
                "risk_model_version": existing_state.get("risk_model_version"),
                "r_multiple_unavailable_reason": existing_state.get("r_multiple_unavailable_reason"),
            }
        rows = self.storage.fetch_all(
            "SELECT payload FROM trade_proposals WHERE symbol=? AND side='buy' AND status IN ('approved','submitted','filled') ORDER BY created_at ASC LIMIT 1",
            (symbol.upper(),),
        )
        if not rows:
            return {
                "initial_stop_price": None,
                "initial_risk_per_share": None,
                "initial_risk_pct": None,
                "initial_risk_dollars": None,
                "stop_model": None,
                "stop_source": None,
                "entry_price_for_r": None,
                "risk_model_version": None,
                "r_multiple_unavailable_reason": "r_multiple_unavailable_initial_stop_missing",
            }
        try:
            payload = json.loads(rows[0].get("payload") or "{}")
            stop = payload.get("initial_stop_price", payload.get("stop_price"))
            entry = payload.get("entry_price_for_r", payload.get("latest_price"))
            stop_val = float(stop) if stop is not None else None
            entry_val = float(entry) if entry is not None else None
            if stop_val is None:
                return {
                    "initial_stop_price": None,
                    "initial_risk_per_share": None,
                    "initial_risk_pct": None,
                    "initial_risk_dollars": None,
                    "stop_model": payload.get("stop_model", payload.get("stop_model_used")),
                    "stop_source": payload.get("stop_source", payload.get("stop_model_used")),
                    "entry_price_for_r": entry_val,
                    "risk_model_version": payload.get("risk_model_version"),
                    "r_multiple_unavailable_reason": payload.get("r_multiple_unavailable_reason", "r_multiple_unavailable_initial_stop_missing"),
                }
            if entry_val is not None and stop_val >= entry_val:
                return {
                    "initial_stop_price": None,
                    "initial_risk_per_share": None,
                    "initial_risk_pct": None,
                    "initial_risk_dollars": None,
                    "stop_model": payload.get("stop_model", payload.get("stop_model_used")),
                    "stop_source": payload.get("stop_source", payload.get("stop_model_used")),
                    "entry_price_for_r": entry_val,
                    "risk_model_version": payload.get("risk_model_version"),
                    "r_multiple_unavailable_reason": payload.get("r_multiple_unavailable_reason", "r_multiple_unavailable_initial_stop_invalid"),
                }
            return {
                "initial_stop_price": stop_val,
                "initial_risk_per_share": payload.get("initial_risk_per_share"),
                "initial_risk_pct": payload.get("initial_risk_pct", payload.get("stop_distance_pct")),
                "initial_risk_dollars": payload.get("initial_risk_dollars"),
                "stop_model": payload.get("stop_model", payload.get("stop_model_used")),
                "stop_source": payload.get("stop_source", payload.get("stop_model_used")),
                "entry_price_for_r": entry_val,
                "risk_model_version": payload.get("risk_model_version"),
                "r_multiple_unavailable_reason": payload.get("r_multiple_unavailable_reason"),
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return {
                "initial_stop_price": None,
                "initial_risk_per_share": None,
                "initial_risk_pct": None,
                "initial_risk_dollars": None,
                "stop_model": None,
                "stop_source": None,
                "entry_price_for_r": None,
                "risk_model_version": None,
                "r_multiple_unavailable_reason": "r_multiple_unavailable_initial_stop_missing",
            }

    def _initial_stop_for_position(self, symbol: str) -> float | None:
        seed = self._initial_risk_seed_for_position(symbol)
        stop = seed.get("initial_stop_price")
        try:
            return float(stop) if stop is not None else None
        except (TypeError, ValueError):
            return None

    def _record_position_management(self, decision: PositionManagementDecision, now: datetime, proposal_id: str | None = None) -> None:
        symbol = decision.symbol.upper()
        previous = self._position_management_state(symbol)
        risk_seed = self._initial_risk_seed_for_position(symbol)
        created_at = previous.get("created_at") if previous else now.isoformat()
        highest_seen_at = previous.get("highest_price_seen_at") if previous else None
        max_seen_at = previous.get("max_unrealized_profit_seen_at") if previous else None
        if not previous or decision.highest_price_since_entry != previous.get("highest_price_since_entry"):
            highest_seen_at = now.isoformat()
        if not previous or decision.max_unrealized_profit_pct != previous.get("max_unrealized_profit_pct"):
            max_seen_at = now.isoformat()

        profit_active = int(bool(previous.get("profit_protection_active") if previous else 0))
        cfg = self.config.get("position_management", {}).get("profit_protection", {})
        if (
            decision.max_unrealized_profit_pct is not None
            and decision.max_unrealized_profit_pct >= float(cfg.get("fallback_activate_at_profit_pct", 2.0))
        ):
            profit_active = 1
        profit_activated_at = previous.get("profit_protection_activated_at") if previous else None
        if profit_active and not profit_activated_at:
            profit_activated_at = now.isoformat()

        level_hits = {
            1: int(previous.get("take_profit_level_1_hit") or 0) if previous else 0,
            2: int(previous.get("take_profit_level_2_hit") or 0) if previous else 0,
            3: int(previous.get("take_profit_level_3_hit") or 0) if previous else 0,
        }

        self.storage.execute(
            """
            INSERT INTO position_management_state(
                id,symbol,broker_position_id,avg_entry_price,quantity,highest_price_since_entry,highest_price_seen_at,
                max_unrealized_profit_pct,max_unrealized_profit_seen_at,profit_protection_active,profit_protection_activated_at,
                take_profit_level_1_hit,take_profit_level_2_hit,take_profit_level_3_hit,trailing_stop_price,
                initial_stop_price,initial_risk_per_share,initial_risk_pct,initial_risk_dollars,stop_model,stop_source,
                entry_price_for_r,risk_model_version,r_multiple_unavailable_reason,last_decision_type,last_reason,updated_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                avg_entry_price=excluded.avg_entry_price,
                quantity=excluded.quantity,
                highest_price_since_entry=excluded.highest_price_since_entry,
                highest_price_seen_at=excluded.highest_price_seen_at,
                max_unrealized_profit_pct=excluded.max_unrealized_profit_pct,
                max_unrealized_profit_seen_at=excluded.max_unrealized_profit_seen_at,
                profit_protection_active=excluded.profit_protection_active,
                profit_protection_activated_at=excluded.profit_protection_activated_at,
                take_profit_level_1_hit=excluded.take_profit_level_1_hit,
                take_profit_level_2_hit=excluded.take_profit_level_2_hit,
                take_profit_level_3_hit=excluded.take_profit_level_3_hit,
                trailing_stop_price=excluded.trailing_stop_price,
                initial_stop_price=excluded.initial_stop_price,
                initial_risk_per_share=excluded.initial_risk_per_share,
                initial_risk_pct=excluded.initial_risk_pct,
                initial_risk_dollars=excluded.initial_risk_dollars,
                stop_model=excluded.stop_model,
                stop_source=excluded.stop_source,
                entry_price_for_r=excluded.entry_price_for_r,
                risk_model_version=excluded.risk_model_version,
                r_multiple_unavailable_reason=excluded.r_multiple_unavailable_reason,
                last_decision_type=excluded.last_decision_type,
                last_reason=excluded.last_reason,
                updated_at=excluded.updated_at
            """,
            (
                str(uuid.uuid4()), symbol, symbol, decision.avg_entry_price, decision.quantity,
                decision.highest_price_since_entry, highest_seen_at, decision.max_unrealized_profit_pct,
                max_seen_at, profit_active, profit_activated_at, level_hits[1], level_hits[2], level_hits[3],
                decision.trailing_stop_price,
                risk_seed.get("initial_stop_price"),
                risk_seed.get("initial_risk_per_share"),
                risk_seed.get("initial_risk_pct"),
                risk_seed.get("initial_risk_dollars"),
                risk_seed.get("stop_model"),
                risk_seed.get("stop_source"),
                risk_seed.get("entry_price_for_r"),
                risk_seed.get("risk_model_version"),
                risk_seed.get("r_multiple_unavailable_reason"),
                decision.decision_type, decision.reason, now.isoformat(), created_at,
            ),
        )
        self.storage.execute(
            """
            INSERT INTO position_management_decisions(
                id,run_id,symbol,decision_type,priority,action,reason,current_price,avg_entry_price,quantity,
                unrealized_profit_pct,highest_price_since_entry,max_unrealized_profit_pct,pullback_from_peak_pct,
                profit_giveback_ratio,current_r_multiple,trailing_stop_price,suggested_sell_fraction,
                suggested_add_notional,blocking_reasons,is_actionable,dip_trap_classification,created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, symbol, decision.decision_type, decision.priority, decision.action,
                decision.reason, decision.current_price, decision.avg_entry_price, decision.quantity,
                decision.unrealized_profit_pct, decision.highest_price_since_entry, decision.max_unrealized_profit_pct,
                decision.pullback_from_peak_pct, decision.profit_giveback_ratio, decision.current_r_multiple,
                decision.trailing_stop_price, decision.suggested_sell_fraction, decision.suggested_add_notional,
                "; ".join(decision.blocking_reasons), int(decision.is_actionable), decision.dip_trap_classification,
                now.isoformat(), json_dumps(dataclasses.asdict(decision)),
            ),
        )
        if decision.decision_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT"}:
            sell_fraction = decision.suggested_sell_fraction or 0.0
            estimated_shares = decision.quantity * sell_fraction
            estimated_notional = estimated_shares * decision.current_price
            self.storage.execute(
                """
                INSERT INTO profit_exit_events(
                    id,run_id,symbol,event_type,proposal_id,proposal_batch_id,sell_fraction,estimated_shares,
                    estimated_notional,current_gain_pct,peak_gain_pct,giveback_ratio,r_multiple,trailing_stop_price,status,created_at,resolved_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()), self.run_id, symbol, decision.decision_type, proposal_id, None,
                    sell_fraction, estimated_shares, estimated_notional, decision.unrealized_profit_pct,
                    decision.max_unrealized_profit_pct, decision.profit_giveback_ratio,
                    decision.current_r_multiple, decision.trailing_stop_price,
                    "proposal_created" if proposal_id else ("actionable" if decision.is_actionable else "tracked"),
                    now.isoformat(), None,
                ),
            )

    def _mark_position_management_proposal_handled(self, proposal_row: dict[str, Any], status: str) -> None:
        try:
            payload = json.loads(proposal_row.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        proposal = {**payload, **proposal_row}
        pm_type = proposal.get("position_management_decision_type")
        if not pm_type:
            return
        symbol = str(proposal.get("symbol", "")).upper()
        proposal_id = proposal.get("id")
        resolved_at = iso_now()
        if pm_type == "TAKE_PROFIT_PARTIAL":
            pm_decision = proposal.get("position_management_decision") or {}
            try:
                level = int(pm_decision.get("take_profit_level") or 0)
            except (TypeError, ValueError):
                level = 0
            if level in {1, 2, 3}:
                column = f"take_profit_level_{level}_hit"
                self.storage.execute(
                    f"UPDATE position_management_state SET {column}=1, updated_at=? WHERE symbol=?",
                    (resolved_at, symbol),
                )
        if pm_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT"}:
            self.storage.execute(
                "UPDATE profit_exit_events SET status=?, resolved_at=? WHERE proposal_id=?",
                (status, resolved_at, proposal_id),
            )

    def _parse_batch_approval_command(self, text: str) -> tuple[str, str] | None:
        normalized = text.strip()
        match = re.fullmatch(
            r"(?i)\s*(yes|approve|approved|no|reject|rejected)\s*,?\s*(all|[A-Z.]{1,10})\s*[.!]?\s*",
            normalized,
        )
        if not match:
            return None
        return match.group(1).lower(), match.group(2).upper().rstrip(".")

    def _approval_intent_from_text(self, text: str) -> tuple[str, str | None, str | None, tuple[str, str] | None]:
        batch_match = self._parse_batch_approval_command(text)
        if batch_match:
            action_word, target = batch_match
            action = "yes" if action_word in {"yes", "approve", "approved"} else "no"
            if target == "ALL":
                return f"{action}_all", None, None, batch_match
            return action, target, None, batch_match

        normalized = " ".join(text.lower().strip().split())
        approve_words = r"(?:yes|approve|approved)(?: please)?"
        reject_words = r"(?:no|reject|rejected)(?: thanks)?"
        if re.fullmatch(approve_words, normalized):
            return "yes", None, None, None
        if re.fullmatch(reject_words, normalized):
            return "no", None, None, None

        approve_match = re.fullmatch(approve_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9.-]+))?", normalized)
        reject_match = re.fullmatch(reject_words + r"(?: (buy|sell) ([a-z.]{1,10}))?(?: (?:proposal )?([a-z0-9.-]+))?", normalized)
        match = approve_match or reject_match
        if match:
            _side, symbol, proposal_id = match.groups()
            action = "yes" if approve_match else "no"
            if symbol:
                return action, symbol.upper(), None, None
            if proposal_id and re.fullmatch(r"[a-z.]{1,10}", proposal_id):
                return action, proposal_id.upper(), None, None
            return action, None, proposal_id, None
        return "unknown", None, None, None

    def _approval_route_context(self, text: str, reply_to_message_id: str | None) -> dict[str, Any]:
        intent, target_symbol, target_proposal_id, batch_match = self._approval_intent_from_text(text)
        active_batch_rows = self._fetch_batch_candidates(now_iso=iso_now(), active_only=True, pending_only=True)
        if reply_to_message_id is not None:
            reply_batch_rows = self._fetch_batch_candidates(
                now_iso=iso_now(),
                reply_to_message_id=reply_to_message_id,
                active_only=True,
                pending_only=True,
            )
            if reply_batch_rows:
                active_batch_rows = reply_batch_rows
        active_batch_ids = []
        active_symbols = []
        for row in active_batch_rows:
            batch_id = str(row["batch_id"])
            symbol = str(row["candidate_symbol"]).upper()
            if batch_id not in active_batch_ids:
                active_batch_ids.append(batch_id)
            if symbol not in active_symbols:
                active_symbols.append(symbol)
        return {
            "normalized_command": intent if target_symbol is None else f"{intent}_symbol",
            "approval_intent": intent,
            "target_symbol": target_symbol,
            "target_proposal_id": target_proposal_id,
            "batch_match": batch_match,
            "active_batch_count": len(active_batch_ids),
            "active_batch_ids": active_batch_ids,
            "active_batch_candidate_symbols": active_symbols,
            "active_single_proposal_count": len(self.storage.active_proposals()),
        }

    def _audit_telegram_approval_route(
        self,
        update_id: Any,
        message_id: Any,
        reply_to_message_id: str | None,
        context: dict[str, Any],
        route_chosen: str,
        route_outcome: str,
        fallback_reason: str | None = None,
        stopped_processing: bool = True,
    ) -> None:
        detail = {
            "update_id": update_id,
            "message_id": message_id,
            "reply_to_message_id": reply_to_message_id,
            "normalized_command": context.get("normalized_command"),
            "approval_intent": context.get("approval_intent"),
            "target_symbol": context.get("target_symbol"),
            "target_proposal_id": context.get("target_proposal_id"),
            "active_batch_count": context.get("active_batch_count"),
            "active_batch_ids": context.get("active_batch_ids"),
            "active_batch_candidate_symbols": context.get("active_batch_candidate_symbols"),
            "active_single_proposal_count": context.get("active_single_proposal_count"),
            "route_chosen": route_chosen,
            "route_outcome": route_outcome,
            "fallback_reason": fallback_reason,
            "stopped_processing": bool(stopped_processing),
        }
        self.storage.audit(self.run_id, "telegram_approval_route", detail)

    def _fetch_batch_candidates(
        self,
        *,
        now_iso: str,
        reply_to_message_id: str | None = None,
        active_only: bool = True,
        pending_only: bool = False,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if active_only:
            where.append("b.status IN ('pending','partially_approved')")
            where.append("b.expires_at>?")
            params.append(now_iso)
        if pending_only:
            where.append("c.candidate_status='pending'")
            where.append("p.status='pending'")
        if reply_to_message_id is not None:
            where.append("b.telegram_message_id=?")
            params.append(str(reply_to_message_id))
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        return self.storage.fetch_all(
            f"""
            SELECT
                c.*,
                b.status AS batch_status,
                b.expires_at AS batch_expires_at,
                b.telegram_message_id AS batch_message_id,
                b.expiry_notified AS batch_expiry_notified,
                p.status AS proposal_status,
                p.expires_at AS proposal_expires_at
            FROM proposal_batch_candidates c
            JOIN proposal_batches b ON b.id=c.batch_id
            JOIN trade_proposals p ON p.id=c.proposal_id
            {where_sql}
            ORDER BY b.created_at DESC, c.rank
            """,
            tuple(params),
        )

    def _batch_symbols_hint(self, rows: list[dict[str, Any]]) -> str:
        symbols = []
        for row in rows:
            symbol = str(row.get("candidate_symbol", "")).upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        if not symbols:
            return "yes all, no all"
        yes_parts = [f"yes {sym}" for sym in symbols]
        no_parts = [f"no {sym}" for sym in symbols]
        return ", ".join(yes_parts + ["yes all"] + no_parts + ["no all"])

    def _proposal_or_candidate_expired(self, proposal: dict[str, Any], candidate_row: dict[str, Any] | None = None) -> bool:
        now_dt = datetime.now(UTC)
        proposal_expiry = proposal.get("expires_at")
        if proposal_expiry and _parse_datetime(proposal_expiry) <= now_dt:
            return True
        if candidate_row is not None:
            candidate_expiry = candidate_row.get("expires_at")
            batch_expiry = candidate_row.get("batch_expires_at")
            if candidate_expiry and _parse_datetime(candidate_expiry) <= now_dt:
                return True
            if batch_expiry and _parse_datetime(batch_expiry) <= now_dt:
                return True
        return False

    def _mark_proposal_expiry_notified(self, proposal_id: str) -> None:
        self.storage.execute("UPDATE trade_proposals SET expiry_notified=1 WHERE id=?", (proposal_id,))

    def _mark_batch_expiry_notified(self, batch_id: str) -> None:
        self.storage.execute("UPDATE proposal_batches SET expiry_notified=1 WHERE id=?", (batch_id,))

    def _expire_pending_batches(self, notify: bool = False) -> None:
        now_iso = iso_now()
        expired_candidates = self.storage.fetch_all(
            """
            SELECT c.*, b.telegram_message_id AS batch_message_id, b.expires_at AS batch_expires_at, p.symbol, p.status AS proposal_status
            FROM proposal_batch_candidates c
            JOIN proposal_batches b ON b.id=c.batch_id
            JOIN trade_proposals p ON p.id=c.proposal_id
            WHERE c.candidate_status='pending'
              AND (c.expires_at<=? OR b.expires_at<=? OR p.status='expired')
            ORDER BY b.created_at, c.rank
            """,
            (now_iso, now_iso),
        )
        for row in expired_candidates:
            self.storage.execute(
                "UPDATE proposal_batch_candidates SET candidate_status='expired' WHERE id=? AND candidate_status='pending'",
                (row["id"],),
            )

        expired_batches = self.storage.fetch_all(
            """
            SELECT * FROM proposal_batches
            WHERE status IN ('pending','partially_approved')
              AND expires_at<=?
            ORDER BY created_at
            """,
            (now_iso,),
        )
        for row in expired_batches:
            self.storage.execute("UPDATE proposal_batches SET status='expired' WHERE id=?", (row["id"],))
            if notify and int(row.get("expiry_notified") or 0) == 0:
                batch_rows = self.storage.fetch_all(
                    "SELECT candidate_symbol FROM proposal_batch_candidates WHERE batch_id=? ORDER BY rank",
                    (row["id"],),
                )
                symbols = ", ".join(str(r["candidate_symbol"]).upper() for r in batch_rows)
                self.telegram.send_message(
                    f"⏳ Proposal batch expired\n\n"
                    f"The paper proposal batch for {symbols or 'pending candidates'} expired at {format_sgt(row['expires_at'])}.\n"
                    f"No order was placed from expired batch candidates."
                )
                self.storage.execute("UPDATE proposal_batches SET expiry_notified=1 WHERE id=?", (row["id"],))
                self.storage.audit(
                    self.run_id,
                    "proposal_batch_expiry_notified",
                    {"batch_id": row["id"], "expires_at": row["expires_at"], "symbols": symbols},
                )

    def _final_revalidate_position_management(self, proposal: dict[str, Any], refreshed_price: float | None = None) -> str | None:
        pm_type = proposal.get("position_management_decision_type")
        if not pm_type:
            return None
        symbol = str(proposal.get("symbol", "")).upper()
        side = str(proposal.get("side", "")).lower()
        if side == "sell":
            positions = self.broker.get_positions() if self.broker is not None else []
            pos = next((p for p in positions if str(_value(p, "symbol", "")).upper() == symbol), None)
            if pos is None:
                return "position no longer exists"
            held_qty = float(_value(pos, "qty", 0.0) or 0.0)
            sell_qty = float(proposal.get("qty") or 0.0)
            if sell_qty <= 0 or sell_qty > held_qty + 1e-9:
                return "sell quantity is invalid or exceeds held quantity"
            price = float(refreshed_price or proposal.get("latest_price") or 0.0)
            min_notional = float(self.config.get("position_management", {}).get("profit_taking", {}).get("minimum_notional_to_sell", 1.0))
            if sell_qty * price < min_notional:
                return "position-management sell is below minimum notional"
            open_orders = self.broker.get_open_orders() if self.broker is not None else []
            if any(str(_value(o, "symbol", "")).upper() == symbol for o in open_orders):
                return "conflicting open order exists for symbol"
        elif proposal.get("action") == "add":
            positions = self.broker.get_positions() if self.broker is not None else []
            pos = next((p for p in positions if str(_value(p, "symbol", "")).upper() == symbol), None)
            if pos is None:
                return "position no longer exists"
            avg_entry = float(_value(pos, "avg_entry_price", 0.0) or 0.0)
            price = float(refreshed_price or proposal.get("latest_price") or 0.0)
            if avg_entry <= 0 or price <= avg_entry:
                return "healthy-pullback add would average down or lacks valid entry price"
            if proposal.get("dip_trap_classification") != "healthy_pullback":
                return "pullback is no longer classified as healthy"
        return None

    def _exit_blocker_label_from_reason(self, no_action_reason: str) -> str:
        no_act = no_action_reason or ""
        lower = no_act.lower()
        match = re.search(r"new buy blocked because\s+(.+?)(?:;|$)", no_act, flags=re.IGNORECASE)
        if match:
            detail = match.group(1).strip()
            if detail == "an exit is pending":
                blocker = self._exit_blocker_context()
                if blocker.get("stale"):
                    return blocker.get("reason") or "stale pending-exit flag detected; needs cleanup"
                return blocker.get("reason") or "portfolio exit-first rule active"
            return detail[0].upper() + detail[1:] if detail else "portfolio exit-first rule active"
        if "block_new_buy_if_exit_pending" in lower or "exit is pending" in lower:
            blocker = self._exit_blocker_context()
            if blocker.get("stale"):
                return blocker.get("reason") or "stale pending-exit flag detected; needs cleanup"
            return blocker.get("reason") or "portfolio exit-first rule active"
        return "portfolio exit-first rule active"

    def _portfolio_context(self, proposal: dict[str, Any], approval_valid: bool = False) -> dict[str, Any]:
        state = self._authoritative_runtime_state(force=approval_valid)
        positions = state["positions"]
        orders = state["orders"]
        account = state["account"]
        symbol = proposal["symbol"]

        # Calculate exposure snapshot from current positions
        snapshot = self._get_exposure_snapshot(positions, account)
        equity = snapshot["portfolio_equity"]

        # Proposal notional
        proposal_notional = float(proposal.get("notional") or 0.0)
        proposal_notional_pct = (proposal_notional / equity) * 100 if equity > 0 else 0.0

        # Proposed total exposure %
        proposed_total_exposure_pct = snapshot["total_exposure_pct"] + proposal_notional_pct

        # Proposed symbol exposure %
        current_symbol_exposure = snapshot["single_exposures"].get(symbol.upper(), 0.0)
        proposed_symbol_exposure_pct = current_symbol_exposure + proposal_notional_pct

        # Cluster parameters
        c_name = self._get_symbol_cluster(symbol)
        proposed_cluster_positions_count = 0
        proposed_cluster_exposure_pct = 0.0
        if c_name:
            current_cluster_count = snapshot["cluster_counts"].get(c_name, 0)
            current_cluster_exposure = snapshot["cluster_exposures"].get(c_name, 0.0)
            has_symbol_pos = any(str(_value(p, "symbol", "")).upper() == symbol.upper() for p in positions)
            proposed_cluster_positions_count = current_cluster_count + (0 if has_symbol_pos else 1)
            proposed_cluster_exposure_pct = current_cluster_exposure + proposal_notional_pct

        exit_blocker = self._exit_blocker_context(orders)
        exit_pending = bool(exit_blocker.get("active"))

        # max_emergency_exit_score
        max_emergency_exit_score = 0.0
        for pos in positions:
            p_sym = str(_value(pos, "symbol", "")).upper()
            latest_mem = self.storage.fetch_all(
                "SELECT emergency_exit_score FROM market_memory WHERE symbol=? ORDER BY created_at DESC LIMIT 1",
                (p_sym,)
            )
            if latest_mem and latest_mem[0]["emergency_exit_score"] is not None:
                max_emergency_exit_score = max(max_emergency_exit_score, float(latest_mem[0]["emergency_exit_score"]))

        today_orders = self.storage.fetch_all("SELECT id FROM orders WHERE substr(created_at,1,10)=?", (datetime.now(UTC).date().isoformat(),))
        today_buy_orders = self.storage.fetch_all("SELECT id FROM orders WHERE side='buy' AND substr(created_at,1,10)=?", (datetime.now(UTC).date().isoformat(),))
        return {
            "power_connected": get_power_status().connected is True,
            "internet_available": state["internet_available"],
            "database_writable": state["database_writable"],
            "broker_available": state["broker_available"],
            "telegram_available": state["telegram_available"],
            "market_open": state["market_open"],
            "kill_switch": (PROJECT_ROOT / "config" / "KILL_SWITCH").exists(),
            "open_positions": len(positions), "trades_today": len(today_orders), "buy_trades_today": len(today_buy_orders),
            "duplicate_order": any(str(_value(o, "symbol", "")).upper() == symbol for o in orders),
            "same_symbol_position": any(str(_value(p, "symbol", "")).upper() == symbol for p in positions),
            "uses_margin": state["uses_margin"],
            "daily_loss": state["daily_loss"],
            "weekly_loss": state["weekly_loss"],
            "buying_power": float(_value(account, "buying_power", 0) or 0) if account is not None else 0.0,
            "approval_valid": approval_valid,

            # Exposure context fields
            "proposed_total_exposure_pct": proposed_total_exposure_pct,
            "proposed_symbol_exposure_pct": proposed_symbol_exposure_pct,
            "proposed_cluster_positions_count": proposed_cluster_positions_count,
            "proposed_cluster_exposure_pct": proposed_cluster_exposure_pct,
            "exit_pending": exit_pending,
            "exit_pending_symbol": exit_blocker.get("symbol"),
            "exit_pending_reason": exit_blocker.get("reason"),
            "exit_pending_status": exit_blocker.get("status"),
            "exit_pending_stale": bool(exit_blocker.get("stale")),
            "max_emergency_exit_score": max_emergency_exit_score,
        }

    def process_telegram(self) -> None:
        self.storage.expire_proposals()
        self._expire_pending_batches(notify=False)
        updates = self.telegram.get_updates(timeout=0)
        if not updates:
            self.notify_expired_proposals()
            self._expire_pending_batches(notify=True)
            return

        processed_update_ids = set()
        max_id = 0
        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                if update_id in processed_update_ids:
                    continue
                processed_update_ids.add(update_id)

            max_id = max(max_id, update.get("update_id", 0) if update_id is not None else 0)
            message = update.get("message") or {}

            # 0. Duplicate Update Prevention (Only in production, not with MockTelegramBot)
            is_mock_bot = getattr(self.telegram, "is_mock", False) or "Mock" in type(self.telegram).__name__
            if not is_mock_bot and update_id is not None:
                last_processed_id = int(self.storage.get_control_state("telegram_last_processed_update_id", "0"))
                if update_id <= last_processed_id:
                    continue
                self.storage.set_control_state("telegram_last_processed_update_id", str(update_id), "system", "telegram", f"processed_{update_id}", update_id, None, None)

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

            route_context = self._approval_route_context(
                text,
                str(reply_to_message_id) if reply_to_message_id is not None else None,
            ) if self.telegram.is_authorized(sender) else None

            batch_match = route_context.get("batch_match") if route_context else self._parse_batch_approval_command(text)
            if batch_match:
                handled = self._handle_batch_approval_command(
                    raw_text=text,
                    sender=str(sender),
                    action_word=batch_match[0],
                    target=batch_match[1],
                    reply_to_message_id=str(reply_to_message_id) if reply_to_message_id is not None else None,
                )
                if handled:
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            str(reply_to_message_id) if reply_to_message_id is not None else None,
                            route_context,
                            "batch",
                            "handled",
                            None,
                            True,
                        )
                    continue
                if route_context and int(route_context.get("active_batch_count") or 0) > 0:
                    self.telegram.send_message(
                        f"I found an active proposal batch, but I could not match your reply to a pending candidate. Try: {self._batch_symbols_hint(self._fetch_batch_candidates(now_iso=iso_now(), active_only=True, pending_only=True))}."
                    )
                    self._audit_telegram_approval_route(
                        update_id,
                        message.get("message_id"),
                        str(reply_to_message_id) if reply_to_message_id is not None else None,
                        route_context,
                        "batch",
                        "fallback",
                        "active_batch_unhandled",
                        True,
                    )
                    continue

            if (
                route_context
                and route_context.get("approval_intent") != "unknown"
                and (route_context.get("target_symbol") or route_context.get("target_proposal_id"))
                and int(route_context.get("active_batch_count") or 0) > 0
            ):
                self.telegram.send_message(
                    f"I found an active proposal batch, but I could not match your reply to a pending candidate. Try: {self._batch_symbols_hint(self._fetch_batch_candidates(now_iso=iso_now(), active_only=True, pending_only=True))}."
                )
                self._audit_telegram_approval_route(
                    update_id,
                    message.get("message_id"),
                    str(reply_to_message_id) if reply_to_message_id is not None else None,
                    route_context,
                    "batch",
                    "fallback",
                    "active_batch_non_batch_command",
                    True,
                )
                continue

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
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            str(reply_to_message_id),
                            route_context,
                            "single_reply_to",
                            "fallback",
                            "reply_to_target_not_found",
                            True,
                        )
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
                    self._mark_proposal_expiry_notified(str(proposal_row["id"]))
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            str(reply_to_message_id),
                            route_context,
                            "single_reply_to",
                            "expired",
                            "proposal_expired",
                            True,
                        )
                    self.telegram.send_message("⏳ This proposal has already expired. No order was placed.")
                    continue
                elif prop_status in ("approved", "rejected", "superseded"):
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            str(reply_to_message_id),
                            route_context,
                            "single_reply_to",
                            "already_handled",
                            "proposal_already_handled",
                            True,
                        )
                    self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                    continue

            # Check plain yes/no ambiguity
            normalized = " ".join(text.lower().strip().split())
            reject_words = r"(?:no|reject|rejected)(?: thanks)?"
            approve_words = r"(?:yes|approve|approved)(?: please)?"
            is_plain_reject = bool(re.fullmatch(reject_words, normalized))
            is_plain_approve = bool(re.fullmatch(approve_words, normalized))

            if reply_to_message_id is None and (is_plain_approve or is_plain_reject):
                active_batch_rows = self._fetch_batch_candidates(now_iso=iso_now(), active_only=True, pending_only=True)
                active_batch_ids = {str(r["batch_id"]) for r in active_batch_rows}
                if active_batch_rows:
                    if len(active_batch_ids) > 1:
                        self.telegram.send_message("Multiple proposal batches are pending. Please reply directly to the batch message or include the batch/proposal ID.")
                        fallback_reason = "multiple_active_batches"
                    elif len(active_batch_rows) > 1:
                        self.telegram.send_message(
                            f"Plain yes is ambiguous because more than one candidate is pending. Use {self._batch_symbols_hint(active_batch_rows)}."
                        )
                        fallback_reason = "plain_yes_multiple_batch_candidates"
                    else:
                        self.telegram.send_message(
                            f"Plain yes is ambiguous because a ranked batch is pending. Use {self._batch_symbols_hint(active_batch_rows)}."
                        )
                        fallback_reason = "plain_yes_single_batch_candidate"
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            None,
                            route_context,
                            "batch",
                            "fallback",
                            fallback_reason,
                            True,
                        )
                    continue
                if len(pending) > 1:
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            None,
                            route_context,
                            "single_pending",
                            "fallback",
                            "multiple_single_proposals",
                            True,
                        )
                    self.telegram.send_message("I found multiple pending proposals. Please reply directly to the proposal message, or include the symbol/proposal ID.")
                    continue
                elif len(pending) == 0:
                    time_limit = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
                    recent = self.storage.fetch_all(
                        "SELECT 1 FROM approvals WHERE approval_received_at >= ? AND sender_id=?",
                        (time_limit, sender)
                    )
                    if recent:
                        self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                        fallback_reason = "recent_approval_already_handled"
                    else:
                        self.telegram.send_message("I did not take any action because I could not match your reply to a single pending proposal. Please specify the proposal ID or symbol.")
                        fallback_reason = "no_pending_single_proposal"
                    if route_context:
                        self._audit_telegram_approval_route(
                            update_id,
                            message.get("message_id"),
                            None,
                            route_context,
                            "single_pending",
                            "fallback",
                            fallback_reason,
                            True,
                        )
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
                if parsed.reason == "proposal expired" and parsed.proposal_id:
                    self._mark_proposal_expiry_notified(str(parsed.proposal_id))
                msg = translate_reason(parsed.reason)
                self.telegram.send_message(msg)
                if route_context:
                    self._audit_telegram_approval_route(
                        update_id,
                        message.get("message_id"),
                        str(reply_to_message_id) if reply_to_message_id is not None else None,
                        route_context,
                        "single_parser",
                        "rejected",
                        parsed.reason,
                        True,
                    )

                # Update delay for non-accepted/expired/ambiguous updates
                ack_sent = iso_now()
                delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))
                continue

            row = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (parsed.proposal_id,))[0]
            prop_symbol = row.get("symbol", "")
            prop_side = row.get("side", "").lower()
            if parsed.action == "approve" and row.get("emergency_exit_triggered") != 1:
                proposal_for_sleep_check = {**json.loads(row.get("payload") or "{}"), **row}
                if self._sleep_mode_blocks_approval(proposal_for_sleep_check):
                    msg = "Sleep mode is ON, so I did not process this BUY/ADD approval. Send awake first, then approve again if the proposal is still valid."
                    self.telegram.send_message(msg)
                    ack_sent = iso_now()
                    delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                    self.storage.execute(
                        "UPDATE approvals SET status=?, acknowledgement_status='blocked', acknowledgement_sent_at=?, acknowledgement_delay_seconds=?, final_order_decision='blocked', final_block_reason=? WHERE id=?",
                        (f"sleep_blocked_{approval_id[:8]}", ack_sent, delay_sec, "sleep mode is active", approval_id),
                    )
                    continue

            if parsed.action == "reject":
                if row.get("emergency_exit_triggered") == 1:
                    self.storage.execute("UPDATE trade_proposals SET status='rejected', emergency_exit_final_decision='cancelled', emergency_exit_user_response='no' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                    self.telegram.send_message(f"❌ Received: NO for {prop_symbol} emergency paper sell proposal. Emergency exit cancelled.")
                    self.storage.audit(self.run_id, "emergency_exit_cancelled_by_user", {"symbol": prop_symbol, "proposal_id": parsed.proposal_id})
                else:
                    self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (parsed.proposal_id,))
                    self.telegram.send_message(f"❌ Received: NO for {prop_symbol} paper {prop_side} proposal. Proposal rejected. No order will be placed.")

                # Create shadow trade for the rejected proposal
                updated_rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (parsed.proposal_id,))
                if updated_rows:
                    self._mark_position_management_proposal_handled(updated_rows[0], "rejected")
                    self._create_shadow_trade_from_proposal(updated_rows[0], "rejected_by_user")

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
            if block_reason is None:
                block_reason = self._final_revalidate_position_management(proposal, refreshed_price_val)
            if block_reason:
                # Set up mock Executor result for blocked order
                result = ExecutionResult(False, "blocked", f"ta-{uuid.uuid4().hex[:24]}", reason=block_reason)
            else:
                # Get authoritative account & positions to compute fresh snapshot
                try:
                    state = self._authoritative_runtime_state(force=True)
                    positions_fresh = state["positions"]
                    account_fresh = state["account"]
                    snapshot_fresh = self._get_exposure_snapshot(positions_fresh, account_fresh)
                except Exception as e:
                    logger.warning("Failed to retrieve authoritative snapshot during revalidation: %s", e)
                    snapshot_fresh = None

                # Update proposal dict with fresh price data so risk engine evaluates using it
                if refreshed_price_val is not None:
                    proposal["latest_price"] = refreshed_price_val
                if refreshed_price_at is not None:
                    proposal["price_at"] = refreshed_price_at.isoformat()

                # Recalculate dynamic sizing if sizing enabled and is buy
                if snapshot_fresh and self.config.get("position_sizing", {}).get("enabled", True) and proposal.get("side") == "buy":
                    try:
                        bars_fresh = normalize_bars(self.broker.get_historical_bars(prop_symbol, "1Day", 250), prop_symbol)
                        volatility_regime = proposal.get("volatility_regime", "normal")
                        score = proposal.get("score", 70.0)
                        is_add = proposal.get("action") == "add" or bool(proposal.get("is_add", False))
                        size_dict = self._calculate_dynamic_size(prop_symbol, score, volatility_regime, refreshed_price_val, bars_fresh, snapshot_fresh, is_add=is_add)

                        proposal["notional"] = size_dict["final_notional"]
                        proposal["qty"] = size_dict["suggested_shares"]
                    except Exception as e:
                        logger.warning("Recalculate dynamic size failed during revalidation: %s", e)

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
                self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=?", (parsed.proposal_id,))
                self._mark_position_management_proposal_handled(proposal, "submitted")
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
                continue
            else:
                self.storage.execute("UPDATE approvals SET acknowledgement_status='blocked' WHERE id=?", (approval_id,))
                self.storage.execute("UPDATE trade_proposals SET status='blocked' WHERE id=?", (parsed.proposal_id,))
                if "refused to trade on stale data" in result.reason:
                    self.telegram.send_message(f"Approved, but no order was placed. {result.reason}")
                else:
                    self.telegram.send_message(f"⚠️ Approved, but no order was placed. Reason: {result.reason}.")
                continue
        self.notify_expired_proposals()
        self._expire_pending_batches(notify=True)
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

    def _update_batch_status(self, batch_id: str) -> None:
        rows = self.storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE batch_id=?", (batch_id,))
        statuses = {r["candidate_status"] for r in rows}
        if not rows:
            return
        if statuses == {"expired"}:
            status = "expired"
        elif statuses <= {"approved", "rejected", "expired", "blocked", "submitted"}:
            status = "completed"
        elif any(s in statuses for s in ("approved", "rejected", "blocked", "submitted")):
            status = "partially_approved"
        else:
            status = "pending"
        self.storage.execute("UPDATE proposal_batches SET status=? WHERE id=?", (status, batch_id))

    def _handle_batch_approval_command(
        self,
        raw_text: str,
        sender: str,
        action_word: str,
        target: str,
        reply_to_message_id: str | None,
    ) -> bool:
        if not self._ranked_batch_mode_enabled():
            return False
        if not self.telegram.is_authorized(sender):
            return True

        action = "approve" if action_word in {"yes", "approve", "approved"} else "reject"
        now_iso = iso_now()
        active_rows = self._fetch_batch_candidates(
            now_iso=now_iso,
            reply_to_message_id=reply_to_message_id,
            active_only=True,
            pending_only=True,
        )
        all_relevant_rows = self._fetch_batch_candidates(
            now_iso=now_iso,
            reply_to_message_id=reply_to_message_id,
            active_only=False,
            pending_only=False,
        )
        active_batch_ids = {str(r["batch_id"]) for r in active_rows}
        if not active_rows:
            if all_relevant_rows:
                target_rows = [r for r in all_relevant_rows if target == "ALL" or str(r["candidate_symbol"]).upper() == target]
                if target_rows and all(
                    str(r.get("candidate_status")) == "expired"
                    or str(r.get("proposal_status")) == "expired"
                    or _parse_datetime(r.get("expires_at") or r.get("proposal_expires_at")) <= datetime.now(UTC)
                    or _parse_datetime(r.get("batch_expires_at")) <= datetime.now(UTC)
                    for r in target_rows
                ):
                    for row in target_rows:
                        self._mark_proposal_expiry_notified(str(row["proposal_id"]))
                        self._mark_batch_expiry_notified(str(row["batch_id"]))
                    self.telegram.send_message("That candidate has expired, so I did not take action. I will not submit an order from an expired proposal.")
                    return True
                if target_rows:
                    self.telegram.send_message("I did not take any action because that batch candidate was already handled earlier.")
                    return True
            return False
        if reply_to_message_id is None and len(active_batch_ids) > 1:
            self.telegram.send_message("Multiple proposal batches are pending. Please reply directly to the batch message or include the batch/proposal ID.")
            return True

        target_upper = str(target).upper()
        if target_upper != "ALL":
            rows = [r for r in active_rows if str(r["candidate_symbol"]).upper() == target_upper]
        else:
            rows = list(active_rows)

        if not rows:
            symbol_rows = [r for r in all_relevant_rows if str(r["candidate_symbol"]).upper() == target_upper]
            if symbol_rows and all(
                str(r.get("candidate_status")) == "expired"
                or str(r.get("proposal_status")) == "expired"
                or _parse_datetime(r.get("expires_at") or r.get("proposal_expires_at")) <= datetime.now(UTC)
                or _parse_datetime(r.get("batch_expires_at")) <= datetime.now(UTC)
                for r in symbol_rows
            ):
                for row in symbol_rows:
                    self._mark_proposal_expiry_notified(str(row["proposal_id"]))
                    self._mark_batch_expiry_notified(str(row["batch_id"]))
                self.telegram.send_message("That candidate has expired, so I did not take action. I will not submit an order from an expired proposal.")
                return True
            if symbol_rows:
                self.telegram.send_message("I did not take any action because that batch candidate was already handled earlier.")
                return True
            self.telegram.send_message(
                f"I found an active proposal batch, but I could not match your reply to a pending candidate. Use one of: {self._batch_symbols_hint(active_rows)}."
            )
            return True
        if target_upper == "ALL" and action == "approve":
            if self.config.get("mode") != "paper" or self.config.get("proposal_mode", {}).get("allow_yes_all_for_paper") is not True:
                self.telegram.send_message("YES ALL is blocked because it is only allowed in paper ranked-batch mode.")
                return True
            self.telegram.send_message(
                f"✅ Received: YES ALL for {len(rows)} paper candidates. I will run final safety checks separately for each. No order will be placed for any candidate that fails final checks."
            )
        elif target_upper == "ALL" and action == "reject":
            self.telegram.send_message(f"❌ Received: NO ALL for {len(rows)} paper candidates. All pending batch candidates will be rejected.")

        for row in rows:
            batch_id = row["batch_id"]
            proposal_id = row["proposal_id"]
            symbol = row["candidate_symbol"]
            self.storage.execute(
                "INSERT INTO approval_batch_actions(id,run_id,batch_id,proposal_id,sender_id,raw_message,action,status,created_at,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), self.run_id, batch_id, proposal_id, sender, raw_text, action, "received", iso_now(), json_dumps({"target": target_upper})),
            )
            if action == "reject":
                self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (proposal_id,))
                self.storage.execute("UPDATE proposal_batch_candidates SET candidate_status='rejected' WHERE proposal_id=?", (proposal_id,))
                proposal_rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,))
                if proposal_rows:
                    self._mark_position_management_proposal_handled(proposal_rows[0], "rejected")
                    self._create_shadow_trade_from_proposal(proposal_rows[0], "rejected_by_user")
                if target_upper != "ALL":
                    self.telegram.send_message(f"❌ Received: NO for {symbol} paper candidate. Candidate rejected. No order will be placed.")
            else:
                if target_upper != "ALL":
                    action_label = str(row.get("candidate_action") or row.get("candidate_side") or "candidate").lower().replace("_", " ")
                    self.telegram.send_message(f"✅ Received: YES for {symbol} paper {action_label} candidate. I will run final safety checks now.")
                submitted, status, reason = self._approve_batch_candidate(proposal_id, sender, raw_text, row)
                candidate_status = "submitted" if submitted else ("pending" if status == "sleep_mode_active" else ("expired" if status == "expired" else "blocked"))
                self.storage.execute(
                    "UPDATE proposal_batch_candidates SET candidate_status=? WHERE proposal_id=?",
                    (candidate_status, proposal_id),
                )
                self.storage.execute(
                    "UPDATE approval_batch_actions SET status=?, detail=? WHERE proposal_id=? AND batch_id=?",
                    (status, json_dumps({"reason": reason}), proposal_id, batch_id),
                )
            self._update_batch_status(batch_id)
        return True

    def _approve_batch_candidate(self, proposal_id: str, sender: str, raw_text: str, batch_row: dict[str, Any]) -> tuple[bool, str, str | None]:
        rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=? AND status='pending'", (proposal_id,))
        if not rows:
            self.telegram.send_message("I did not take any action because this candidate was already handled earlier.")
            return False, "already_handled", "candidate already handled"

        row = rows[0]
        if self._proposal_or_candidate_expired(row, batch_row):
            self.storage.execute("UPDATE trade_proposals SET status='expired' WHERE id=? AND status='pending'", (proposal_id,))
            self.storage.execute("UPDATE proposal_batch_candidates SET candidate_status='expired' WHERE proposal_id=? AND candidate_status='pending'", (proposal_id,))
            self._mark_proposal_expiry_notified(proposal_id)
            if batch_row.get("batch_id"):
                self._mark_batch_expiry_notified(str(batch_row["batch_id"]))
            self._update_batch_status(str(batch_row.get("batch_id") or ""))
            self.telegram.send_message("That candidate has expired, so I did not take action. I will not submit an order from an expired proposal.")
            return False, "expired", "candidate expired"
        if self._sleep_mode_blocks_approval({**json.loads(row.get("payload") or "{}"), **row}):
            msg = "Sleep mode is ON, so I did not process this BUY/ADD approval. Send awake first, then approve again if the proposal is still valid."
            self.telegram.send_message(msg)
            return False, "sleep_mode_active", msg

        approval_id = str(uuid.uuid4())
        approval_received_at = iso_now()
        self.storage.execute(
            "INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at,reply_to_message_id,proposal_targeting_method,acknowledgement_status,approval_received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval_id, self.run_id, proposal_id, sender, raw_text, "approve", 1, "accepted", iso_now(),
                str(batch_row.get("telegram_message_id") or "") or None, "batch", "received", approval_received_at
            ),
        )

        if not self.storage.consume_approval(proposal_id, approval_id):
            self.telegram.send_message("I did not take any action because this candidate was already handled earlier.")
            return False, "already_handled", "candidate already handled"

        prop_symbol = row.get("symbol", "")
        prop_side = row.get("side", "").lower()
        proposal = {**json.loads(row.get("payload") or "{}"), **row, "status": "approved"}
        final_revalidation_started_at = iso_now()
        block_reason = None
        refreshed_price_val = None
        refreshed_price_at = None
        price_refreshed_at = None
        refreshed_price_age_seconds = None
        price_move_bps_since_proposal = None
        now_dt = datetime.now(UTC)

        if self.config.get("mode") != "paper" or self.config.get("live_enabled") is not False:
            block_reason = "not in paper mode / live enabled"
        elif (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            block_reason = "kill switch active"
        elif not self.storage.writable():
            block_reason = "database is not writable"

        telegram_cfg = self.config.get("telegram", {})
        refresh_required = telegram_cfg.get("approval_price_refresh_required", True)
        max_price_age = telegram_cfg.get("approval_max_price_age_seconds", 60)
        max_price_move_bps = telegram_cfg.get("approval_max_price_move_bps", 25)

        if block_reason is None and self.broker is not None:
            try:
                trade = self.broker.get_latest_price(prop_symbol)
                refreshed_price_val = float(_value(trade, "price", 0) or 0)
                refreshed_price_at = _dt(_value(trade, "timestamp", now_dt))
                if refreshed_price_at:
                    price_refreshed_at = refreshed_price_at.isoformat()
                    refreshed_price_age_seconds = (now_dt - refreshed_price_at).total_seconds()
            except Exception as e:
                logger.warning("Failed to refresh price for batch candidate %s: %s", prop_symbol, e)

        market_open = False
        if block_reason is None and self.broker is not None:
            try:
                market_open = self.broker.is_market_open()
            except Exception:
                market_open = False

        proposal_price = proposal.get("latest_price") or row.get("current_price")
        if block_reason is None:
            if self._proposal_or_candidate_expired(row, batch_row):
                block_reason = "Proposal expired"
            if refresh_required:
                if refreshed_price_val is None or refreshed_price_val <= 0:
                    block_reason = "Price refresh failed or price is unavailable"
                elif refreshed_price_age_seconds is None or refreshed_price_age_seconds > max_price_age or refreshed_price_age_seconds < -5:
                    block_reason = "The proposal price is no longer fresh, so the system refused to trade on stale data. A new proposal is required."
                elif not market_open:
                    block_reason = "Market is closed"
                elif proposal_price is not None and float(proposal_price) > 0:
                    price_move_bps_since_proposal = (abs(refreshed_price_val - float(proposal_price)) / float(proposal_price)) * 10000
                    if price_move_bps_since_proposal > max_price_move_bps:
                        block_reason = f"Price moved too much ({price_move_bps_since_proposal:.1f} bps > limit {max_price_move_bps} bps)"
            elif not market_open:
                block_reason = "Market is closed"
        if block_reason is None:
            block_reason = self._final_revalidate_position_management(proposal, refreshed_price_val)

        if block_reason:
            result = ExecutionResult(False, "blocked", f"ta-{uuid.uuid4().hex[:24]}", reason=block_reason)
        else:
            try:
                state = self._authoritative_runtime_state(force=True)
                snapshot_fresh = self._get_exposure_snapshot(state["positions"], state["account"])
                if refreshed_price_val is not None:
                    proposal["latest_price"] = refreshed_price_val
                if refreshed_price_at is not None:
                    proposal["price_at"] = refreshed_price_at.isoformat()
                if self.config.get("position_sizing", {}).get("enabled", True) and prop_side == "buy":
                    bars_fresh = normalize_bars(self.broker.get_historical_bars(prop_symbol, "1Day", 250), prop_symbol)
                    size_dict = self._calculate_dynamic_size(
                        prop_symbol,
                        float(proposal.get("score", 70.0) or 70.0),
                        proposal.get("volatility_regime", "normal"),
                        float(refreshed_price_val or proposal.get("latest_price") or 0.0),
                        bars_fresh,
                        snapshot_fresh,
                        is_add=proposal.get("action") == "add" or bool(proposal.get("is_add", False)),
                    )
                    proposal["notional"] = size_dict["final_notional"]
                    proposal["qty"] = size_dict["suggested_shares"]
                context = self._portfolio_context(proposal, approval_valid=True)
                result = Executor(self.broker, self._risk_engine(proposal_id, "final")).execute(proposal, context)
            except Exception as e:
                result = ExecutionResult(False, "blocked", f"ta-{uuid.uuid4().hex[:24]}", reason=str(e))

        final_revalidation_completed_at = iso_now()
        self.storage.execute(
            "UPDATE approvals SET final_revalidation_started_at=?, final_revalidation_completed_at=?, price_refreshed_at=?, refreshed_price=?, refreshed_price_age_seconds=?, price_move_bps_since_proposal=?, final_order_decision=?, final_block_reason=? WHERE id=?",
            (
                final_revalidation_started_at, final_revalidation_completed_at, price_refreshed_at,
                refreshed_price_val, refreshed_price_age_seconds, price_move_bps_since_proposal,
                "submitted" if result.submitted else "blocked", result.reason if not result.submitted else None,
                approval_id,
            ),
        )
        order_id = str(uuid.uuid4())
        self.storage.execute(
            "INSERT INTO orders(id,run_id,proposal_id,broker_order_id,client_order_id,symbol,side,notional,qty,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order_id, self.run_id, proposal_id, str(_value(result.broker_response, "id", "")) or None,
                result.client_order_id, proposal.get("symbol", prop_symbol), proposal.get("side", prop_side),
                proposal.get("notional"), proposal.get("qty"), result.status,
                json_dumps({"submitted": result.submitted, "reason": result.reason}), iso_now(), iso_now()
            ),
        )
        self.storage.link_executed_order_records(order_id)
        self.storage.upsert_actual_trade_outcome_for_order(order_id)
        if result.submitted:
            self.storage.execute("UPDATE approvals SET acknowledgement_status='submitted' WHERE id=?", (approval_id,))
            self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=?", (proposal_id,))
            self._mark_position_management_proposal_handled(proposal, "submitted")
            if prop_side == "buy":
                self.telegram.send_message(f"✅ Paper order submitted: Buy {prop_symbol} for ${float(proposal.get('notional') or 0.0):.0f}. Mode: paper only.")
            else:
                qty_val = proposal.get("qty")
                qty_str = f"{qty_val} shares" if qty_val is not None else f"${float(proposal.get('notional') or 0.0):.0f}"
                self.telegram.send_message(f"✅ Paper order submitted: Sell {prop_symbol} for {qty_str}. Mode: paper only.")
            return True, "submitted", None

        self.storage.execute("UPDATE approvals SET acknowledgement_status='blocked' WHERE id=?", (approval_id,))
        self.storage.execute("UPDATE trade_proposals SET status='blocked' WHERE id=?", (proposal_id,))
        self.telegram.send_message(f"⚠️ Approved, but no order was placed for {prop_symbol}. Reason: {result.reason}.")
        return False, "blocked", result.reason

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

            # Create shadow trade for the expired proposal
            self._mark_position_management_proposal_handled(row, "expired")
            self._create_shadow_trade_from_proposal(row, "expired: no response")

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

        try:
            account = self.broker.get_account()
        except Exception:
            account = None

        snapshot = self._get_exposure_snapshot(positions, account)

        # Insert portfolio exposure snapshot
        snapshot_id = str(uuid.uuid4())
        self.storage.execute(
            """INSERT INTO portfolio_exposure_snapshots(
                id, run_id, timestamp, total_exposure_pct, total_exposure_dollars,
                single_symbol_exposure_json, cluster_exposure_json
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                snapshot_id, self.run_id, now.isoformat(),
                snapshot["total_exposure_pct"], snapshot["total_exposure_dollars"],
                json.dumps(snapshot["single_exposures"]), json.dumps(snapshot["cluster_exposures"])
            )
        )

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
            dynamic_active, dynamic_observation = self._dynamic_universe_scan_symbols()
            active_watchlist = list(dict.fromkeys(active_watchlist + dynamic_active))
            obs_watchlist = list(dict.fromkeys(obs_watchlist + [s for s in dynamic_observation if s not in active_watchlist]))
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

                # Check pyramiding / add-to-winner eligibility
                is_add = False
                unrealized_gain_pct = 0.0
                add_block_reasons = []
                add_score_improvement = 0.0

                pyramiding_cfg = self.config.get("add_to_position", {})
                if pyramiding_cfg.get("enabled", True) and has_position and signal.action != "EXIT" and not has_order:
                    # Run evaluate_symbol pretending we don't have a position to see if buy setup exists
                    buy_setup_signal = evaluate_symbol(
                        symbol,
                        bars,
                        False, # has_position = False
                        has_order,
                        market_open,
                        strategy_config["maximum_volatility_20d"],
                        strategy_config["stop_drawdown_pct"],
                        position_drawdown_pct=0.0
                    )

                    if buy_setup_signal.action == "ENTRY" and buy_setup_signal.side == "buy":
                        # A buy setup is active! Let's check pyramiding constraints.

                        # 1. Profitability & Averaging Down
                        unrealized_gain_pct = 0.0
                        if avg_entry_price and avg_entry_price > 0:
                            unrealized_gain_pct = (price - avg_entry_price) / avg_entry_price * 100

                        only_if_profitable = pyramiding_cfg.get("only_if_profitable", True)
                        min_gain = float(pyramiding_cfg.get("min_unrealized_gain_pct", 0.5))
                        if only_if_profitable and unrealized_gain_pct < min_gain:
                            add_block_reasons.append(f"position not sufficiently profitable ({unrealized_gain_pct:.2f}% < {min_gain}%)")

                        if pyramiding_cfg.get("block_averaging_down", True) and unrealized_gain_pct < 0:
                            add_block_reasons.append("cannot average down")

                        # 2. Risk warnings & Emergency exit score
                        if pyramiding_cfg.get("block_if_exit_warning", True):
                            if emergency_exit_score is not None and emergency_exit_score > float(pyramiding_cfg.get("block_if_emergency_exit_score_above", 40)):
                                add_block_reasons.append(f"emergency exit score too high ({emergency_exit_score:.1f} > 40)")

                        # 3. Max additions caps
                        max_adds_day = int(pyramiding_cfg.get("max_adds_per_symbol_per_day", 1))
                        # Count adds today
                        adds_today_res = self.storage.fetch_all(
                            "SELECT COUNT(*) as cnt FROM trade_proposals WHERE symbol=? AND side='buy' AND json_extract(payload, '$.action')='add' AND status IN ('submitted', 'approved', 'filled') AND created_at >= ?",
                            (symbol, today_start)
                        )
                        adds_today_cnt = adds_today_res[0]["cnt"] if adds_today_res else 0
                        if adds_today_cnt >= max_adds_day:
                            add_block_reasons.append(f"max adds per day reached ({adds_today_cnt} >= {max_adds_day})")

                        max_adds_total = int(pyramiding_cfg.get("max_total_adds_per_symbol", 2))
                        # Count total adds for current position
                        latest_entry = self.storage.fetch_all(
                            "SELECT created_at FROM trade_proposals WHERE symbol=? AND side='buy' AND json_extract(payload, '$.action')='entry' AND status IN ('submitted', 'approved', 'filled') ORDER BY created_at DESC LIMIT 1",
                            (symbol,)
                        )
                        if latest_entry:
                            entry_time = latest_entry[0]["created_at"]
                            adds_total_res = self.storage.fetch_all(
                                "SELECT COUNT(*) as cnt FROM trade_proposals WHERE symbol=? AND side='buy' AND json_extract(payload, '$.action')='add' AND status IN ('submitted', 'approved', 'filled') AND created_at > ?",
                                (symbol, entry_time)
                            )
                            adds_total_cnt = adds_total_res[0]["cnt"] if adds_total_res else 0
                        else:
                            adds_total_cnt = 0
                        if adds_total_cnt >= max_adds_total:
                            add_block_reasons.append(f"max total adds reached ({adds_total_cnt} >= {max_adds_total})")

                        # Set signal action to ENTRY so we compute the score
                        signal = dataclasses.replace(buy_setup_signal, action="ENTRY")
                        is_add = True

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
                risk_budgeted_mode = self._ranked_batch_mode_enabled()
                if port_context.get("duplicate_order") or (not risk_budgeted_mode and port_context.get("trades_today", 0) >= self.config["risk"].get("max_trades_per_day", 1)):
                    safety_ok = False
                if not risk_budgeted_mode and signal.action == "ENTRY" and port_context.get("open_positions", 0) >= self.config["risk"].get("max_open_positions", 1):
                    safety_ok = False
                score_safety = 15.0 if safety_ok else 0.0

                age = (now - price_at).total_seconds() if price_at else float("inf")
                fresh_price = -5 <= age <= self.config["risk"].get("max_price_age_seconds", 120)
                enough_bars = len(bars) >= self.config["risk"].get("min_historical_bars", 50)
                score_data = 10.0 if (fresh_price and enough_bars) else (5.0 if fresh_price else 0.0)

                score = float(round(score_rule + score_asset + score_5m + score_session + score_vol + score_safety + score_data, 2))
                classification = self._classify_trade_score(score)

                # Check pyramiding constraints that require trade score
                if is_add:
                    min_trade_score = float(pyramiding_cfg.get("min_trade_score", 85))
                    if score < min_trade_score:
                        add_block_reasons.append(f"trade score below threshold ({score:.2f} < {min_trade_score})")

                    min_score_imp = float(pyramiding_cfg.get("min_score_improvement", 5))
                    add_score_improvement = 0.0
                    prev_buy_prop = self.storage.fetch_all(
                        "SELECT json_extract(payload, '$.score') as score FROM trade_proposals WHERE symbol=? AND side='buy' AND status IN ('submitted', 'approved', 'filled') ORDER BY created_at DESC LIMIT 1",
                        (symbol,)
                    )
                    if prev_buy_prop:
                        prev_score = float(prev_buy_prop[0]["score"])
                        add_score_improvement = score - prev_score
                        if add_score_improvement < min_score_imp:
                            add_block_reasons.append(f"insufficient score improvement ({add_score_improvement:.2f} < {min_score_imp})")

                # Calculate dynamic sizing
                final_notional = 5.0
                suggested_shares = 0.0
                stop_price = None
                stop_distance_pct = None
                stop_distance_dollars = None
                risk_budget = 0.0
                score_mult = 1.0
                vol_mult = 1.0
                stop_method = "default"
                risk_based_shares = 0.0
                score_adjusted_notional = 5.0
                vol_adjusted_notional = 5.0
                base_notional = 5.0

                if signal.action == "ENTRY" and signal.side == "buy":
                    size_dict = self._calculate_dynamic_size(symbol, score, volatility_regime, price, bars, snapshot, is_add=is_add)
                    final_notional = size_dict["final_notional"]
                    suggested_shares = size_dict["suggested_shares"]
                    stop_price = size_dict["stop_price"]
                    stop_distance_pct = size_dict["stop_distance_pct"]
                    stop_distance_dollars = size_dict["stop_distance_dollars"]
                    risk_budget = size_dict["risk_budget"]
                    score_mult = size_dict["score_multiplier"]
                    vol_mult = size_dict["volatility_multiplier"]
                    stop_method = size_dict["stop_model_used"]
                    risk_based_shares = size_dict["risk_based_shares"]
                    score_adjusted_notional = size_dict["score_adjusted_notional"]
                    vol_adjusted_notional = size_dict["vol_adjusted_notional"]
                    base_notional = size_dict["base_notional"]

                # Log add-on opportunity
                if is_add:
                    passed_add = 1 if len(add_block_reasons) == 0 else 0
                    self.storage.execute(
                        """INSERT INTO add_on_opportunities(
                            id, run_id, timestamp, symbol, current_qty, avg_entry_price, current_price,
                            unrealized_gain_pct, proposed_add_notional, proposed_add_shares, score,
                            score_improvement, passed, block_reasons
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), self.run_id, now.isoformat(), symbol, qty_held, avg_entry_price, price,
                            unrealized_gain_pct, final_notional, suggested_shares, score,
                            add_score_improvement, passed_add, "; ".join(add_block_reasons)
                        )
                    )

                    if not passed_add:
                        # If check failed, revert signal back to HOLD
                        signal = dataclasses.replace(signal, action="HOLD", reason="Pyramiding check failed: " + "; ".join(add_block_reasons))
                        no_action_reason = "Pyramiding check failed: " + "; ".join(add_block_reasons)

                position_management_decision = None
                position_management_sell_fraction = None
                position_management_sell_qty = None
                position_management_add_notional = None
                if has_position and self.config.get("position_management", {}).get("enabled", True):
                    previous_pm_state = self._position_management_state(symbol)
                    pm_engine = PositionManagementEngine(self.config).with_previous_state(previous_pm_state)
                    normal_exit_signal = signal.action == "EXIT" and signal.side == "sell" and emergency_exit_triggered != 1
                    position_management_decision = pm_engine.classify(
                        symbol=symbol,
                        current_price=price,
                        avg_entry_price=float(avg_entry_price or 0.0),
                        quantity=float(qty_held or 0.0),
                        bars=bars,
                        previous_state=previous_pm_state,
                        initial_stop_price=self._initial_stop_for_position(symbol),
                        trade_score=score,
                        score_improvement=add_score_improvement,
                        emergency_exit_score=emergency_exit_score,
                        normal_exit_signal=normal_exit_signal,
                        volatility_regime=volatility_regime,
                        has_open_order=has_order,
                        now=now,
                    )
                    self._record_position_management(position_management_decision, now)
                    if position_management_decision.is_actionable and emergency_exit_triggered != 1:
                        if position_management_decision.action == "sell":
                            position_management_sell_fraction = position_management_decision.suggested_sell_fraction or 1.0
                            position_management_sell_qty = min(float(qty_held or 0.0), float(qty_held or 0.0) * position_management_sell_fraction)
                            decision_reason = position_management_decision.reason
                            if position_management_decision.decision_type == "NORMAL_RISK_EXIT":
                                decision_reason = signal.reason
                            signal = dataclasses.replace(
                                signal,
                                action="EXIT",
                                side="sell",
                                reason=f"{position_management_decision.decision_type}: {decision_reason}",
                            )
                            exit_trigger_reason = decision_reason
                            score = max(score, float(self.config.get("ai", {}).get("ai_review_min_score", 65)))
                        elif position_management_decision.decision_type == "HEALTHY_PULLBACK_ADD":
                            position_management_add_notional = position_management_decision.suggested_add_notional
                            signal = dataclasses.replace(
                                signal,
                                action="ENTRY",
                                side="buy",
                                reason=position_management_decision.reason,
                            )
                            is_add = True
                            final_notional = float(position_management_add_notional or final_notional)
                            suggested_shares = final_notional / price if price > 0 else 0.0
                            score = max(score, float(self.config.get("position_management", {}).get("healthy_pullback_add", {}).get("minimum_trade_score", 85)))
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
                    # New sizing fields
                    "is_add": is_add,
                    "final_notional": final_notional,
                    "suggested_shares": suggested_shares,
                    "stop_price": stop_price,
                    "stop_distance_pct": stop_distance_pct,
                    "stop_distance_dollars": stop_distance_dollars,
                    "risk_budget": risk_budget,
                    "score_multiplier": score_mult,
                    "volatility_multiplier": vol_mult,
                    "stop_model_used": stop_method,
                    "risk_based_shares": risk_based_shares,
                    "score_adjusted_notional": score_adjusted_notional,
                    "vol_adjusted_notional": vol_adjusted_notional,
                    "base_notional": base_notional,
                    "position_management_decision": dataclasses.asdict(position_management_decision) if position_management_decision else None,
                    "position_management_decision_type": position_management_decision.decision_type if position_management_decision else None,
                    "position_management_sell_fraction": position_management_sell_fraction,
                    "position_management_sell_qty": position_management_sell_qty,
                    "position_management_add_notional": position_management_add_notional,
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

            # Filter BUY candidates. Risk-budgeted mode also keeps meaningful
            # pre-proposal risk blocks for measurement/ranking rows.
            buy_candidates = []
            batch_mode_enabled = self._ranked_batch_mode_enabled()
            for res in buy_candidates_all:
                ai_config = self.config.get("ai", {})
                symbol = res["symbol"]
                port_ctx = self._portfolio_context({
                    "symbol": symbol,
                    "side": "buy",
                    "action": "add" if res.get("is_add") else "entry",
                    "notional": res.get("final_notional", 5.0)
                })
                mock_prop = {
                    "symbol": symbol,
                    "side": "buy",
                    "action": "add" if res.get("is_add") else "entry",
                    "is_add": res.get("is_add", False),
                    "latest_price": res["price"],
                    "price_at": str(res["price_at"]),
                    "historical_bars": len(res["bars"]),
                    "volume": res["volume"],
                    "notional": res.get("final_notional", 5.0),
                    "created_at": now.isoformat(),
                    "expires_at": res["expiry"].isoformat(),
                    "strategy_version": res["signal"].strategy_version,
                    "reason": res["signal"].reason,
                }
                decision = self._risk_engine("mock_id", "proposal").evaluate(mock_prop, port_ctx)

                is_meaningful_buy = (
                    res["symbol"] in active_watchlist
                    and proposals_enabled
                    and res["score"] >= ai_config.get("ai_review_min_score", 65)
                    and res["dedupe_status"] == "allowed"
                )
                if is_meaningful_buy and decision.passed:
                    buy_candidates.append(res)
                elif batch_mode_enabled and is_meaningful_buy:
                    res["preproposal_block_reason"] = "; ".join(decision.reasons) or "pre-proposal risk check failed"
                    buy_candidates.append(res)

            buy_candidates = self._rank_candidates(buy_candidates, snapshot)

            risk_snapshot = self._record_risk_budget_snapshot(snapshot, account, now)
            ranked_budget_reasons: dict[str, str] = {}
            if batch_mode_enabled:
                allowed_buy_symbols, ranked_budget_reasons = self._apply_risk_budget_to_ranked_candidates(
                    buy_candidates, snapshot, account, now
                )
                suppressed_buy_symbols = {c["symbol"] for c in buy_candidates if c["symbol"] not in allowed_buy_symbols}
            else:
                risk_cfg = self.config.get("risk", {})
                portfolio_behavior_cfg = self.config.get("portfolio_behavior", {})
                max_new_buy_per_cycle = portfolio_behavior_cfg.get(
                    "max_new_buy_proposals_per_cycle",
                    risk_cfg.get("max_new_buy_proposals_per_cycle", 1),
                )
                max_pending_buy = portfolio_behavior_cfg.get(
                    "max_pending_buy_proposals",
                    risk_cfg.get("max_pending_buy_proposals", 1),
                )
                pending_in_db = self.storage.fetch_all("SELECT COUNT(*) as cnt FROM trade_proposals WHERE side='buy' AND status='pending'")[0]["cnt"]
                allowed_new_buys = max(0, min(max_new_buy_per_cycle, max_pending_buy - pending_in_db))
                allowed_buy_symbols = {c["symbol"] for c in buy_candidates[:allowed_new_buys]}
                suppressed_buy_symbols = {c["symbol"] for c in buy_candidates[allowed_new_buys:]}

            # Record rankings in candidate_rankings table
            for c in buy_candidates:
                reason_selected = None
                reason_not_selected = None
                if c["symbol"] in allowed_buy_symbols:
                    reason_selected = ranked_budget_reasons.get(c["symbol"]) or c.get("selection_reason") or "Top ranked candidate passing exposure/risk limits"
                else:
                    if batch_mode_enabled and c["symbol"] in suppressed_buy_symbols:
                        reason_not_selected = ranked_budget_reasons.get(c["symbol"]) or "blocked by risk budget"
                    elif c["symbol"] in suppressed_buy_symbols:
                        reason_not_selected = "suppressed due to simultaneous candidate limits"
                    else:
                        reason_not_selected = c.get("no_action_reason") or "Did not meet ranking criteria or limits"

                self.storage.execute(
                    """INSERT INTO candidate_rankings(
                        id, run_id, timestamp, symbol, true_score_rank, final_candidate_rank,
                        setup_quality_score, portfolio_fit_score, diversification_score, sizing_score,
                        reason_selected, reason_not_selected
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), self.run_id, now.isoformat(), c["symbol"],
                        c.get("true_score_rank"), c.get("final_candidate_rank"),
                        c.get("setup_quality_score"), c.get("portfolio_fit_score"),
                        c.get("diversification_score"), c.get("sizing_score"),
                        reason_selected, reason_not_selected
                    )
                )

            any_generated = False
            batch_proposals: list[dict[str, Any]] = []

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
                pm_decision_payload = res.get("position_management_decision")
                pm_decision_type = res.get("position_management_decision_type")
                pm_sell_fraction = res.get("position_management_sell_fraction")

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
                        no_action_reason = ranked_budget_reasons.get(symbol) if batch_mode_enabled else "suppressed due to simultaneous candidate limits"
                        candidate_suppression_reason = "blocked_by_risk_budget" if batch_mode_enabled else "suppressed_by_candidate_limit"
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
                            dedupe_reason = ranked_budget_reasons.get(symbol) if batch_mode_enabled else "suppressed due to simultaneous candidate limits"
                            no_action_reason = dedupe_reason
                            candidate_suppression_reason = "blocked_by_risk_budget" if batch_mode_enabled else "suppressed_by_candidate_limit"
                            self.storage.audit(self.run_id, "proposal_suppressed", {
                                "symbol": symbol, "reason": candidate_suppression_reason, "score": score
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

                            # Size adjustment calculation from res
                            notional = res.get("final_notional", 5.0)
                            qty_val = res.get("suggested_shares", 0.0) if (signal.action == "ENTRY" and signal.side == "buy") else (res.get("position_management_sell_qty") or qty_held)
                            if pm_decision_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT"}:
                                notional = float(qty_val or 0.0) * price

                            stop_price = res.get("stop_price")
                            stop_distance_pct = res.get("stop_distance_pct")
                            stop_distance_dollars = res.get("stop_distance_dollars")
                            stop_model_used = res.get("stop_model_used")
                            risk_budget = res.get("risk_budget")
                            score_multiplier = res.get("score_multiplier")
                            volatility_multiplier = res.get("volatility_multiplier")
                            if volatility_multiplier is not None:
                                paper_size_adjustment = volatility_multiplier

                            port_context = self._portfolio_context({
                                "symbol": symbol,
                                "side": "buy",
                                "action": "add" if res.get("is_add") else "entry",
                                "notional": notional
                            })

                            notional_adjustment_note = ""
                            if volatility_multiplier is not None and volatility_multiplier < 1.0:
                                pct = int(round((1.0 - volatility_multiplier) * 100))
                                notional_adjustment_note = f" (reduced by {pct}% due to volatility multiplier: {volatility_multiplier})"

                            proposal = {
                                "id": proposal_id,
                                "run_id": self.run_id,
                                "signal_id": signal_id,
                                "symbol": symbol,
                                "side": signal.side,
                                "action": "add" if res.get("is_add") else ("entry" if signal.action == "ENTRY" else "exit"),
                                "is_add": 1 if res.get("is_add") else 0,
                                "notional": notional,
                                "qty": qty_val,
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
                                # Sizing & risk details
                                "stop_price": stop_price,
                                "stop_distance_pct": stop_distance_pct,
                                "stop_distance_dollars": stop_distance_dollars,
                                "stop_model_used": stop_model_used,
                                "initial_stop_price": stop_price if stop_price is not None and price is not None and float(stop_price) < float(price) else None,
                                "initial_risk_per_share": (float(price) - float(stop_price)) if stop_price is not None and price is not None and float(stop_price) < float(price) else None,
                                "initial_risk_pct": stop_distance_pct if stop_price is not None and price is not None and float(stop_price) < float(price) else None,
                                "initial_risk_dollars": ((float(price) - float(stop_price)) * float(qty_val)) if stop_price is not None and price is not None and qty_val is not None and float(stop_price) < float(price) else None,
                                "stop_model": stop_model_used,
                                "stop_source": stop_model_used,
                                "entry_price_for_r": price,
                                "risk_model_version": "position_sizing_v1",
                                "r_multiple_unavailable_reason": (
                                    None
                                    if stop_price is not None and price is not None and float(stop_price) < float(price)
                                    else ("r_multiple_unavailable_initial_stop_missing" if stop_price is None else "r_multiple_unavailable_initial_stop_invalid")
                                ),
                                "risk_budget": risk_budget,
                                "score_multiplier": score_multiplier,
                                "volatility_multiplier": volatility_multiplier,
                                "proposed_total_exposure_pct": port_context.get("proposed_total_exposure_pct"),
                                "proposed_cluster_exposure_pct": port_context.get("proposed_cluster_exposure_pct"),
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
                                "position_management_decision_type": pm_decision_type,
                                "position_management_decision": pm_decision_payload,
                                "position_management_sell_fraction": pm_sell_fraction,
                                "dip_trap_classification": (pm_decision_payload or {}).get("dip_trap_classification") if pm_decision_payload else None,
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
                                decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, port_context)
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
                if pm_decision_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT"}:
                    self.storage.execute(
                        "UPDATE profit_exit_events SET proposal_id=?, status='proposal_created' WHERE run_id=? AND symbol=? AND event_type=? AND proposal_id IS NULL",
                        (proposal_id, self.run_id, symbol, pm_decision_type),
                    )

                if is_buy or (is_add and proposal_generated):
                    self.storage.execute(
                        """INSERT INTO position_sizing_decisions(
                            id, run_id, symbol, timestamp, portfolio_equity, risk_budget,
                            stop_distance_dollars, risk_based_shares, score_adjusted_notional,
                            vol_adjusted_notional, final_notional, suggested_shares,
                            base_notional, score_multiplier, volatility_multiplier, stop_model_used,
                            initial_stop_price, initial_risk_per_share, initial_risk_pct, initial_risk_dollars,
                            stop_source, entry_price_for_r, risk_model_version, r_multiple_unavailable_reason
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), self.run_id, symbol, now.isoformat(),
                            snapshot["portfolio_equity"], risk_budget, stop_distance_dollars,
                            risk_budget / stop_distance_dollars if stop_distance_dollars and stop_distance_dollars > 0 else 0.0,
                            res.get("score_adjusted_notional"), res.get("vol_adjusted_notional"),
                            proposal["notional"], qty_val,
                            res.get("base_notional"), score_multiplier, volatility_multiplier, stop_model_used,
                            proposal.get("initial_stop_price"), proposal.get("initial_risk_per_share"),
                            proposal.get("initial_risk_pct"), proposal.get("initial_risk_dollars"),
                            proposal.get("stop_source"), proposal.get("entry_price_for_r"),
                            proposal.get("risk_model_version"), proposal.get("r_multiple_unavailable_reason"),
                        )
                    )

                self.storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, proposal_id, review["summary"], json_dumps(review["risks"]), review["caution_level"], json_dumps(review), iso_now()))

                if batch_mode_enabled and proposal.get("status", "pending") == "pending" and emergency_exit_triggered != 1 and (is_buy or is_exit):
                    batch_proposals.append(proposal)
                else:
                    res_tg = self.telegram.send_message(format_proposal_message(proposal, self.config))
                    if res_tg and isinstance(res_tg, dict) and "message_id" in res_tg:
                        self.storage.execute("UPDATE trade_proposals SET telegram_message_id=? WHERE id=?", (str(res_tg["message_id"]), proposal_id))

            if batch_mode_enabled:
                self._send_ranked_batch_if_needed(batch_proposals, buy_candidates, risk_snapshot)

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

                self._run_performance_lab(profile_results, active_watchlist, positions, now, snapshot)

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
        dynamic_active, dynamic_observation = self._dynamic_universe_scan_symbols()
        active_watchlist.extend(dynamic_active)
        obs_watchlist.extend(s for s in dynamic_observation if s not in active_watchlist)

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

        try:
            current_positions = list(self.broker.get_positions())
        except Exception:
            current_positions = []
        cluster_holdings = self._cluster_holdings(current_positions)

        symbols_list = []
        score_threshold = self.config.get("ai", {}).get("ai_review_min_score", 65)
        for sym, s_rows in symbol_rows.items():
            first_row = s_rows[0]
            latest_row = s_rows[-1]
            p_first = first_row["price"]
            p_latest = latest_row["price"]
            change_30m = ((p_latest / p_first) - 1.0) * 100.0 if p_first > 0 else 0.0

            p_session_start = latest_row.get("session_start_price") or p_latest
            session_change = ((p_latest / p_session_start) - 1.0) * 100.0 if p_session_start > 0 else 0.0

            has_prop = any(bool(r.get("proposal_generated")) for r in s_rows)
            latest_score = latest_row.get("score") or 0.0
            latest_signal = latest_row.get("signal")
            authoritative = self._digest_authoritative_state(sym, window_start_iso, now.isoformat())
            if authoritative:
                status_info = {
                    "status": authoritative["status"],
                    "event": authoritative["event"],
                    "high_score": latest_score >= score_threshold,
                }
            elif has_prop:
                status_info = {"status": "Proposal pending approval", "event": "pending_approval", "high_score": latest_score >= score_threshold}
            else:
                status_info = self._digest_market_memory_status(sym, latest_row, set(obs_watchlist), cluster_holdings)

            symbols_list.append({
                "symbol": sym,
                "trade_score": latest_row["score"],
                "trade_classification": latest_row["classification"],
                "asset_score": latest_row.get("asset_score"),
                "price_change_30m": change_30m,
                "session_change": session_change,
                "status": status_info["status"],
                "_event": status_info.get("event"),
                "_high_score": status_info.get("high_score", False),
                "_cluster_name": status_info.get("cluster_name"),
                "_held_symbols": status_info.get("held_symbols", []),
                "_blocker": status_info.get("blocker"),
            })

        symbols_list.sort(key=lambda x: x["trade_score"] if x["trade_score"] is not None else -1, reverse=True)

        strongest = symbols_list[0]
        weakest = min(symbols_list, key=lambda x: x["trade_score"] if x["trade_score"] is not None else 1000)

        max_syms = digest_config.get("telegram_digest_max_symbols", 6)
        top_watched = symbols_list[:max_syms]

        proposals = self.storage.fetch_all(
            """
            SELECT COUNT(*) as cnt
            FROM trade_proposals
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) <= datetime(?)
            """,
            (window_start_iso, now.isoformat())
        )
        prop_cnt = proposals[0]["cnt"] if proposals else 0

        orders = self.storage.fetch_all(
            """
            SELECT COUNT(*) as cnt
            FROM orders
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) <= datetime(?)
            """,
            (window_start_iso, now.isoformat())
        )
        order_cnt = orders[0]["cnt"] if orders else 0

        fills = self.storage.fetch_all(
            """
            SELECT COUNT(DISTINCT order_id) as cnt
            FROM fills
            WHERE datetime(filled_at) >= datetime(?)
              AND datetime(filled_at) <= datetime(?)
            """,
            (window_start_iso, now.isoformat())
        )
        fill_cnt = fills[0]["cnt"] if fills else 0

        gpt_calls = sum(bool(r.get("gpt_called")) for r in rows)

        expired = self.storage.fetch_all(
            """
            SELECT COUNT(*) as cnt
            FROM trade_proposals
            WHERE status='expired'
              AND datetime(expires_at) >= datetime(?)
              AND datetime(expires_at) <= datetime(?)
            """,
            (window_start_iso, now.isoformat())
        )
        expired_cnt = expired[0]["cnt"] if expired else 0

        promotions = self.storage.fetch_all(
            "SELECT symbol, from_tier, to_tier, reason FROM symbol_promotion_decisions WHERE created_at>=? ORDER BY created_at DESC LIMIT 12",
            (window_start_iso,),
        )
        demotions = self.storage.fetch_all(
            "SELECT symbol, reason FROM symbol_demotion_decisions WHERE created_at>=? ORDER BY created_at DESC LIMIT 12",
            (window_start_iso,),
        )
        to_observation = sorted({r["symbol"] for r in promotions if r["to_tier"] == "observation"})
        to_tradable = sorted({r["symbol"] for r in promotions if r["to_tier"] == "paper_tradable"})
        to_research = sorted({r["symbol"] for r in promotions if r["to_tier"] == "research_candidate"})
        demoted = sorted({r["symbol"] for r in demotions})

        if prop_cnt == 0 and order_cnt == 0:
            universe_actions_str = "No dynamic proposals/orders created"
        else:
            universe_actions_str = f"Proposals {prop_cnt} | Orders {order_cnt} created"

        capabilities = self.storage.fetch_all(
            "SELECT endpoint_name, available, plan_limited, disabled_until, last_error_category FROM data_provider_capabilities"
        )
        health_events = self.storage.fetch_all(
            "SELECT status, checked_at FROM data_provider_health WHERE checked_at>=? ORDER BY checked_at DESC",
            (window_start_iso,),
        )

        completed_runs = self.storage.fetch_all(
            "SELECT research_type FROM universe_research_runs WHERE status='completed' AND ended_at>=? ORDER BY ended_at DESC LIMIT 5",
            (window_start_iso,),
        )

        cap_statuses = {}
        for r in capabilities:
            name = r["endpoint_name"]
            disabled = False
            if r.get("disabled_until"):
                try:
                    dt = datetime.fromisoformat(r["disabled_until"].replace("Z", "+00:00")).astimezone(UTC)
                    disabled = dt > now
                except Exception:
                    pass

            if int(r.get("plan_limited") or 0) == 1:
                cap_statuses[name] = "plan-limited"
            elif disabled:
                if r.get("last_error_category") == "rate_limited":
                    cap_statuses[name] = "rate-limited"
                else:
                    cap_statuses[name] = "cooldown"
            elif int(r.get("available") or 0) == 1:
                cap_statuses[name] = "ok"
            else:
                cap_statuses[name] = "unknown"

        had_historical_rate_limit = any(h["status"] == "rate_limited" for h in health_events)
        active_rate_limits = [name for name, status in cap_statuses.items() if status == "rate-limited"]
        active_cooldowns = [name for name, status in cap_statuses.items() if status == "cooldown"]
        active_plan_limits = [name for name, status in cap_statuses.items() if status == "plan-limited"]
        completed_subtasks = [str(r["research_type"]) for r in completed_runs if r.get("research_type")]

        provider_status_str = "EODHD: ok for current research subtasks"
        if not active_rate_limits and not active_cooldowns and not active_plan_limits:
            if had_historical_rate_limit:
                provider_status_str = "EODHD recovered from recent rate-limit"
            elif completed_subtasks:
                provider_status_str = f"EODHD ok for {completed_subtasks[0]}"
        else:
            core_endpoints = ["eod_bars", "intraday_bars", "realtime_quote", "screener", "technicals"]
            core_statuses = {ep: cap_statuses.get(ep, "ok") for ep in core_endpoints}
            core_issues = [ep for ep, stat in core_statuses.items() if stat not in ("ok", "unknown")]
            
            parts = []
            if not core_issues:
                parts.append("core ok")
            else:
                ep_names = {
                    "eod_bars": "eod",
                    "intraday_bars": "intraday",
                    "realtime_quote": "realtime",
                    "screener": "screener",
                    "technicals": "technicals"
                }
                for ep in core_endpoints:
                    stat = core_statuses[ep]
                    if stat == "plan-limited":
                        parts.append(f"{ep_names[ep]} plan-limited")
                    elif stat == "rate-limited":
                        parts.append(f"{ep_names[ep]} throttled briefly; using cached data")
                    elif stat == "cooldown":
                        parts.append(f"{ep_names[ep]} cooldown_active")
                    elif stat != "ok" and stat != "unknown":
                        parts.append(f"{ep_names[ep]} {stat}")

            news_status = cap_statuses.get("news", "ok")
            if news_status == "plan-limited":
                parts.append("news plan-limited")
            elif news_status in ("rate-limited", "cooldown"):
                parts.append("news optional cooldown")

            fund_status = cap_statuses.get("fundamentals", "ok")
            if fund_status == "plan-limited":
                parts.append("fundamentals plan-limited")
            elif fund_status in ("rate-limited", "cooldown"):
                parts.append("fundamentals cooldown")

            if parts:
                provider_status_str = f"EODHD: {'; '.join(parts)}"
            else:
                provider_status_str = "EODHD rate-limited"

        summary_str = self._build_digest_summary(strongest, symbols_list)
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
            "tier_snapshot": self._digest_tier_snapshot(symbols_list, window_start_iso, now.isoformat()),
            "weakest_symbol": weakest["symbol"],
            "weakest_score": weakest["trade_score"],
            "weakest_classification": weakest["trade_classification"],
            "actions": {
                "proposals": prop_cnt,
                "orders": order_cnt,
                "fills": fill_cnt,
                "gpt_calls": gpt_calls,
                "expired": expired_cnt
            },
            "exit_first_blocker": "; ".join(sorted({x.get("_blocker") for x in symbols_list if x.get("_blocker")} - {None})),
            "summary": summary_str,
            "universe_update": {
                "promoted_to_observation": to_observation,
                "promoted_to_paper_tradable": to_tradable,
                "promoted_to_research_candidate": to_research,
                "demoted_retired": demoted,
                "actions_created": universe_actions_str
            },
            "provider_status": provider_status_str,
        }

        from .utils import format_digest_message
        message_text = format_digest_message(digest_data, self.config)

        try:
            self.telegram.send_message(message_text)
            status = "sent"
        except Exception as e:
            status = "error"
            self.storage.record_check(self.run_id, "digest_send", False, str(e), stage="digest")

        symbols_str = ", ".join(f"{x['symbol']}:{x['status']}" for x in top_watched)
        self.storage.execute(
            "INSERT INTO telegram_digests(run_id,window_start,window_end,sent_at,symbols,summary_text,status) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, window_start_iso, now.isoformat(), now.isoformat(), symbols_str, summary_str, status)
        )
        self.storage.audit(self.run_id, "digest_processed", {"status": status, "window_start": window_start_iso, "window_end": now.isoformat()})

    def _digest_tier_snapshot(self, symbols_list: list[dict[str, Any]], window_start_iso: str, window_end_iso: str) -> dict[str, Any]:
        status_by_symbol = {str(item.get("symbol", "")).upper(): item for item in symbols_list}
        position_symbols = set()
        try:
            position_symbols = {str(_value(p, "symbol", "")).upper() for p in self.broker.get_positions()}
        except Exception:
            rows = self.storage.fetch_all("SELECT symbol FROM positions WHERE created_at=(SELECT MAX(created_at) FROM positions)")
            position_symbols = {str(r.get("symbol", "")).upper() for r in rows}
        universe = self.storage.fetch_all(
            """
            SELECT symbol,tier,source,universe_lane,score,data_confidence,last_promoted_at,created_at,updated_at
            FROM universe_symbols
            WHERE tier IN ('paper_tradable','observation','research_candidate')
            ORDER BY tier, score DESC, symbol
            """
        )
        latest_reviews = {
            r["symbol"]: r
            for r in self.storage.fetch_all(
                """
                SELECT r.*
                FROM dynamic_universe_stage_reviews r
                INNER JOIN (
                    SELECT symbol, MAX(created_at) AS created_at
                    FROM dynamic_universe_stage_reviews
                    GROUP BY symbol
                ) latest ON latest.symbol=r.symbol AND latest.created_at=r.created_at
                """
            )
        }

        def proposal_status(symbol: str, tier: str, source: str) -> tuple[str, str]:
            status_item = status_by_symbol.get(symbol, {})
            status = str(status_item.get("status") or "")
            if tier != PAPER_TRADABLE:
                if tier == OBSERVATION:
                    return "no", "needs paper-tradable promotion"
                return "no", "needs observation promotion first"
            if "cluster limit" in status.lower():
                cleaned = status.replace("Watch — ", "").replace("Status: ", "")
                if "broad-market cluster limit reached" in cleaned.lower():
                    import re
                    syms = sorted({s for s in re.findall(r'\b[A-Z]{3,4}\b', cleaned) if s != symbol})
                    if syms:
                        return "blocked", f"broad-market cluster limit due {'/'.join(syms)}"
                    return "blocked", "broad-market cluster limit"
                return "blocked", cleaned
            if status:
                cleaned = status.replace("Watch — ", "").replace("Watch only — ", "").replace("Status: ", "")
                if cleaned.lower() == "no entry signal" or cleaned.lower() == "no entry/exit signal":
                    return "blocked", "no ENTRY signal"
                return "blocked", cleaned
            if source == "existing_static_watchlist":
                return "blocked", "no ENTRY signal"
            return "blocked", "requires setup, RiskEngine, Telegram approval, and final validation"

        rows_by_tier = {"static_paper_tradable": [], "dynamic_paper_tradable": [], "observation": [], "research_candidate": []}
        for row in universe:
            symbol = str(row["symbol"]).upper()
            tier = str(row["tier"])
            source = str(row.get("source") or "")
            allowed, block = proposal_status(symbol, tier, source)
            review = latest_reviews.get(symbol, {})
            status_item = status_by_symbol.get(symbol, {})
            universe_score = row.get("score")

            if tier == PAPER_TRADABLE:
                trade_score = status_item.get("trade_score")
                if trade_score is not None:
                    score_val = trade_score
                    score_label = "Trade score"
                else:
                    score_val = universe_score
                    score_label = "Fallback score"
            elif tier == OBSERVATION:
                score_val = universe_score
                score_label = "Research score"
            elif tier == RESEARCH_CANDIDATE:
                score_val = universe_score
                score_label = "Research score"
            else:
                score_val = universe_score
                score_label = "Score"

            item = {
                "symbol": symbol,
                "tier": tier,
                "source": source,
                "score": row.get("score"),
                "score_val": score_val,
                "score_label": score_label,
                "data_confidence": row.get("data_confidence"),
                "tradable": tier == PAPER_TRADABLE,
                "held": symbol in position_symbols,
                "proposal_allowed": allowed,
                "proposal_block_reason": block,
                "stage_reason": review.get("reason") or ("static core paper-tradable" if source == "existing_static_watchlist" else "needs next stage promotion"),
                "next_check": review.get("next_promotion_review_at") or "next scanner refresh",
                "decision": review.get("decision"),
            }
            if tier == PAPER_TRADABLE and source == "existing_static_watchlist":
                rows_by_tier["static_paper_tradable"].append(item)
            elif tier == PAPER_TRADABLE:
                rows_by_tier["dynamic_paper_tradable"].append(item)
            elif tier == OBSERVATION:
                rows_by_tier["observation"].append(item)
            elif tier == RESEARCH_CANDIDATE:
                rows_by_tier["research_candidate"].append(item)

        for key in rows_by_tier:
            rows_by_tier[key].sort(key=lambda x: (-(x["score_val"] if x["score_val"] is not None else -1.0), x["symbol"]))

        return rows_by_tier

    def run_cycle(self, run_dynamic_universe: bool = True) -> None:
        self._update_forward_outcomes()
        BrokerReconciler(self.broker, self.storage, self.run_id, self.telegram).reconcile()
        # Reconciliation has refreshed account/position state; force the next
        # proposal/final context to retrieve an authoritative fresh snapshot.
        self._context_cache = None
        self.storage.expire_proposals()
        self.notify_expired_proposals()
        self._expire_pending_batches(notify=False)
        if run_dynamic_universe:
            self._run_dynamic_universe_due()
        if self.config.get("telegram", {}).get("market_scan_processes_telegram_updates", True):
            self.process_telegram()
        if not (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            self.scan()
        self.check_and_send_digest()

    def run_dynamic_universe_research_only(self) -> list[dict[str, Any]]:
        return self._run_dynamic_universe_due()

    def notify_premarket_dynamic_universe_status(self, results: list[dict[str, Any]], trading_skipped_reason: str) -> None:
        if not results or not self.config.get("telegram", {}).get("dynamic_universe_premarket_updates_enabled", True):
            return
        completed = [r for r in results if r.get("status") == "completed"]
        skipped = [r for r in results if r.get("status") == "skipped"]
        if completed:
            promoted = sorted({sym for r in completed for sym in r.get("promoted", [])})
            run_ids = [str(r.get("run_id")) for r in completed if r.get("run_id")]
            placeholders = ",".join("?" for _ in run_ids)
            briefs = []
            if run_ids:
                briefs = self.storage.fetch_all(
                    f"""
                    SELECT symbol,research_score,main_positive_reasons
                    FROM research_candidate_briefs
                    WHERE run_id IN ({placeholders})
                    ORDER BY research_score DESC, symbol
                    LIMIT 5
                    """,
                    tuple(run_ids),
                )
            candidate_count = len(promoted)
            brief_count = sum(int(r.get("candidate_briefs") or 0) for r in completed)
            observation_count = len({sym for r in completed for sym in r.get("promoted", []) if self.storage.fetch_all("SELECT tier FROM universe_symbols WHERE symbol=? AND tier='observation'", (sym,))})
            paper_count = len({sym for r in completed for sym in r.get("promoted", []) if self.storage.fetch_all("SELECT tier FROM universe_symbols WHERE symbol=? AND tier='paper_tradable'", (sym,))})
            top = ", ".join(
                f"{row['symbol']} {float(row['research_score'] or 0):.0f} {str(row.get('main_positive_reasons') or 'score').split(',')[0]}"
                for row in briefs
            )
            provider_rows = self.storage.fetch_all(
                "SELECT SUM(CASE WHEN available=1 THEN 1 ELSE 0 END) available_count, SUM(CASE WHEN disabled_until IS NOT NULL THEN 1 ELSE 0 END) cooldown_count FROM data_provider_capabilities"
            )
            provider = provider_rows[0] if provider_rows else {}
            provider_line = f"Provider: {int(provider.get('available_count') or 0)} endpoints available, {int(provider.get('cooldown_count') or 0)} on cooldown."
            lines = [
                "Dynamic Universe pre-market universe scan completed. Trading remains blocked until market open.",
                f"Research candidates: {candidate_count} | Briefs: {brief_count} | Observation: {observation_count} | Paper-tradable: {paper_count}",
            ]
            if top:
                lines.append(f"Top: {top}.")
            lines.extend([provider_line, "Next: market-open refresh/promotion checks.", "No trade proposals/orders created."])
            text = "\n".join(lines)
        elif skipped:
            reason = skipped[0].get("reason") or "research skipped"
            text = f"Dynamic Universe pre-market universe scan skipped: {reason}. Trading remains blocked until market open.\nNo trade proposals/orders created."
        else:
            text = f"Dynamic Universe pre-market universe scan checked. Trading remains blocked until market open: {trading_skipped_reason}.\nNo trade proposals/orders created."
        try:
            self.telegram.send_message(text)
            self.storage.audit(self.run_id, "dynamic_universe_premarket_update_sent", {"status": "sent", "trading_skipped_reason": trading_skipped_reason})
        except Exception as exc:
            self.storage.audit(self.run_id, "dynamic_universe_premarket_update_failed", {"error": type(exc).__name__, "trading_skipped_reason": trading_skipped_reason})

    def _get_symbol_cluster(self, symbol: str) -> str | None:
        clusters = self.config.get("portfolio_optimizer", {}).get("clusters", {})
        if not clusters:
            clusters = {
                "us_broad_market": ["SPY", "DIA", "IWM"],
                "us_growth_tech": ["QQQ", "XLK"],
                "defensive_healthcare": ["XLV"],
                "financials": ["XLF"],
                "energy": ["XLE"],
            }
        for c_name, c_symbols in clusters.items():
            if symbol.upper() in [s.upper() for s in c_symbols]:
                return c_name
        try:
            rows = self.storage.fetch_all(
                "SELECT cluster FROM universe_symbols WHERE symbol=? AND cluster IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (symbol.upper(),),
            )
            if rows and rows[0]["cluster"] and rows[0]["cluster"] != "unknown_cluster":
                return rows[0]["cluster"]
        except Exception:
            pass
        return None

    def _get_exposure_snapshot(self, positions: list[Any], account: Any) -> dict[str, Any]:
        equity = 10000.0
        if account is not None:
            equity = float(_value(account, "equity", 10000.0) or 10000.0)
            if equity <= 0:
                equity = 10000.0

        total_val = 0.0
        single_exposures = {}
        cluster_values = {}
        cluster_counts = {}
        cluster_symbols: dict[str, list[str]] = {}

        clusters = self.config.get("portfolio_optimizer", {}).get("clusters", {})
        if not clusters:
            clusters = {
                "us_broad_market": ["SPY", "DIA", "IWM"],
                "us_growth_tech": ["QQQ", "XLK"],
                "defensive_healthcare": ["XLV"],
                "financials": ["XLF"],
                "energy": ["XLE"],
            }
        for c in clusters:
            cluster_values[c] = 0.0
            cluster_counts[c] = 0
            cluster_symbols[c] = []

        for pos in positions:
            sym = str(_value(pos, "symbol", "")).upper()
            qty = float(_value(pos, "qty", 0.0) or 0.0)
            price = float(_value(pos, "current_price", 0.0) or _value(pos, "avg_entry_price", 0.0) or 0.0)
            val = float(_value(pos, "market_value", 0.0) or (qty * price))
            total_val += val
            single_exposures[sym] = (val / equity) * 100

            c_name = self._get_symbol_cluster(sym)
            if c_name:
                cluster_values[c_name] += val
                cluster_counts[c_name] += 1
                if sym not in cluster_symbols[c_name]:
                    cluster_symbols[c_name].append(sym)

        cluster_exposures = {}
        for c in cluster_values:
            cluster_exposures[c] = (cluster_values[c] / equity) * 100

        return {
            "portfolio_equity": equity,
            "total_exposure_dollars": total_val,
            "total_exposure_pct": (total_val / equity) * 100,
            "single_exposures": single_exposures,
            "cluster_exposures": cluster_exposures,
            "cluster_counts": cluster_counts,
            "cluster_symbols": cluster_symbols,
        }

    def _calculate_dynamic_size(self, symbol: str, score: float, volatility_regime: str, price: float, bars: pd.DataFrame, snapshot: dict[str, Any], is_add: bool = False) -> dict[str, Any]:
        sizing_cfg = self.config.get("position_sizing", {})
        if not sizing_cfg.get("enabled", True):
            base_notional = float(self.config.get("risk", {}).get("max_trade_notional_paper", 5.0))
            vol_mult = 1.0
            if volatility_regime == "elevated":
                vol_mult = 0.5
                base_notional = base_notional * 0.5
            elif volatility_regime in ("high", "extreme"):
                vol_mult = 0.0
                base_notional = 0.0
            elif volatility_regime == "too quiet":
                vol_mult = 0.75
                base_notional = base_notional * 0.75
            return {
                "final_notional": base_notional,
                "suggested_shares": base_notional / price if price > 0 else 0.0,
                "stop_price": price * 0.92,
                "stop_distance_pct": 8.0,
                "stop_distance_dollars": price * 0.08,
                "risk_budget": 0.0,
                "score_multiplier": 1.0,
                "volatility_multiplier": vol_mult,
                "stop_model_used": "fixed_8pct_fallback",
                "risk_based_shares": 0.0,
                "score_adjusted_notional": base_notional,
                "vol_adjusted_notional": base_notional,
                "base_notional": base_notional
            }

        equity = snapshot["portfolio_equity"]
        risk_per_trade_pct = float(sizing_cfg.get("risk_per_trade_pct", 0.05))
        risk_budget = equity * risk_per_trade_pct / 100

        stop_model = sizing_cfg.get("stop_model", {})
        atr_multiple = float(stop_model.get("atr_multiple", 2.0))
        max_stop_pct = float(stop_model.get("max_stop_pct", 8.0))
        min_stop_pct = float(stop_model.get("min_stop_pct", 1.0))

        vol_20 = None
        if not bars.empty and "volatility_20" in bars.columns:
            vol_20 = bars["volatility_20"].iloc[-1]
            if pd.isna(vol_20):
                vol_20 = None

        atr_value = 0.0
        if "high" in bars.columns and "low" in bars.columns and "close" in bars.columns:
            high = bars["high"].astype(float)
            low = bars["low"].astype(float)
            close = bars["close"].astype(float)
            close_prev = close.shift(1)
            tr = pd.concat([high - low, (high - close_prev).abs(), (low - close_prev).abs()], axis=1).max(axis=1)
            atr_series = tr.rolling(20).mean()
            if not atr_series.empty and not pd.isna(atr_series.iloc[-1]):
                atr_value = float(atr_series.iloc[-1])

        if atr_value <= 0:
            if vol_20 is not None:
                vol_daily = vol_20 / math.sqrt(252)
                atr_value = price * vol_daily
            else:
                atr_value = 0.0

        atr_stop_distance = atr_value * atr_multiple

        technical_stop_distance = 0.0
        if not bars.empty:
            close_s = bars["close"].astype(float) if "close" in bars.columns else None
            ma50 = close_s.rolling(50).mean().iloc[-1] if (close_s is not None and len(bars) >= 50) else None
            recent_low = bars["low"].iloc[-20:].min() if ("low" in bars.columns and len(bars) >= 20) else None

            tech_level = None
            if ma50 is not None and not pd.isna(ma50) and recent_low is not None and not pd.isna(recent_low):
                tech_level = min(ma50, recent_low)
            elif ma50 is not None and not pd.isna(ma50):
                tech_level = ma50
            elif recent_low is not None and not pd.isna(recent_low):
                tech_level = recent_low

            if tech_level is not None and tech_level < price:
                technical_stop_distance = price - tech_level

        stop_distance_dollars = max(atr_stop_distance, technical_stop_distance)
        stop_method = "max_of_atr_or_technical"

        if stop_distance_dollars <= 0:
            stop_distance_dollars = price * max_stop_pct / 100
            stop_method = "fallback_max_stop"

        stop_distance_pct = (stop_distance_dollars / price) * 100

        if stop_distance_pct > max_stop_pct:
            stop_distance_pct = max_stop_pct
            stop_distance_dollars = price * max_stop_pct / 100
        elif stop_distance_pct < min_stop_pct:
            stop_distance_pct = min_stop_pct
            stop_distance_dollars = price * min_stop_pct / 100

        stop_price = price - stop_distance_dollars

        risk_based_shares = risk_budget / stop_distance_dollars
        risk_based_notional = risk_based_shares * price

        score_mult = 1.0
        score_mult_map = sizing_cfg.get("score_multiplier", {})
        if score >= 95:
            score_mult = float(score_mult_map.get("95_100", 2.0))
        elif score >= 85:
            score_mult = float(score_mult_map.get("85_94", 1.5))
        elif score >= 75:
            score_mult = float(score_mult_map.get("75_84", 1.0))
        elif score >= 65:
            score_mult = float(score_mult_map.get("65_74", 0.5))

        vol_mult = 1.0
        vol_mult_map = sizing_cfg.get("volatility_multiplier", {})
        if volatility_regime == "too quiet":
            vol_mult = float(vol_mult_map.get("too_quiet", 0.75))
        elif volatility_regime == "normal":
            vol_mult = float(vol_mult_map.get("normal", 1.0))
        elif volatility_regime == "elevated":
            vol_mult = float(vol_mult_map.get("elevated", 0.5))
        elif volatility_regime == "high":
            vol_mult = float(vol_mult_map.get("high", 0.0))
        elif volatility_regime == "extreme":
            vol_mult = float(vol_mult_map.get("extreme", 0.0))

        base_paper_notional = float(sizing_cfg.get("base_paper_notional", 10.0))
        score_adjusted_notional = base_paper_notional * score_mult
        vol_adjusted_notional = score_adjusted_notional * vol_mult

        final_notional = min(risk_based_notional, vol_adjusted_notional)

        min_notional = float(sizing_cfg.get("min_paper_notional", 5.0))
        max_notional_cap = float(sizing_cfg.get("max_initial_paper_notional", 50.0))
        if is_add:
            max_notional_cap = float(sizing_cfg.get("max_add_paper_notional", 25.0))

        if final_notional < min_notional:
            final_notional = min_notional
        if final_notional > max_notional_cap:
            final_notional = max_notional_cap

        if vol_mult == 0.0:
            final_notional = 0.0

        max_single_exposure_pct = float(self.config.get("portfolio_behavior", {}).get("max_single_symbol_exposure_pct", 2.5))
        current_symbol_value = snapshot["single_exposures"].get(symbol.upper(), 0.0) / 100 * equity
        allowed_additional_single = max(0.0, (equity * max_single_exposure_pct / 100) - current_symbol_value)
        final_notional = min(final_notional, allowed_additional_single)

        max_total_exposure_pct = float(self.config.get("portfolio_behavior", {}).get("max_total_portfolio_exposure_pct", 6.0))
        current_total_value = snapshot["total_exposure_dollars"]
        allowed_additional_total = max(0.0, (equity * max_total_exposure_pct / 100) - current_total_value)
        final_notional = min(final_notional, allowed_additional_total)

        c_name = self._get_symbol_cluster(symbol)
        if c_name:
            max_cluster_exposure_pct = float(self.config.get("portfolio_optimizer", {}).get("max_same_cluster_exposure_pct", 5.0))
            current_cluster_value = snapshot["cluster_exposures"].get(c_name, 0.0) / 100 * equity
            allowed_additional_cluster = max(0.0, (equity * max_cluster_exposure_pct / 100) - current_cluster_value)
            final_notional = min(final_notional, allowed_additional_cluster)

        final_notional = max(0.0, final_notional)
        suggested_shares = final_notional / price if price > 0 else 0.0

        return {
            "final_notional": final_notional,
            "suggested_shares": suggested_shares,
            "stop_price": stop_price,
            "stop_distance_pct": stop_distance_pct,
            "stop_distance_dollars": stop_distance_dollars,
            "risk_budget": risk_budget,
            "score_multiplier": score_mult,
            "volatility_multiplier": vol_mult,
            "stop_model_used": stop_method,
            "risk_based_shares": risk_based_shares,
            "score_adjusted_notional": score_adjusted_notional,
            "vol_adjusted_notional": vol_adjusted_notional,
            "base_notional": base_paper_notional
        }

    def _rank_candidates(self, buy_candidates: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        ranked = []
        for c in buy_candidates:
            symbol = c["symbol"]
            score = c["score"]
            notional = c.get("final_notional", 5.0)

            setup_quality = score

            c_name = self._get_symbol_cluster(symbol)
            cluster_exp = snapshot["cluster_exposures"].get(c_name or "", 0.0)
            portfolio_fit = 100.0 - (cluster_exp * 10.0)

            diversification = 100.0
            if c_name:
                if snapshot["cluster_counts"].get(c_name, 0) > 0:
                    diversification -= 20.0
                else:
                    diversification += 10.0

            sizing_score = min(100.0, notional * 2.0)

            ranking_score = setup_quality * 0.4 + portfolio_fit * 0.3 + diversification * 0.2 + sizing_score * 0.1

            if c.get("is_observation"):
                ranking_score -= 50.0
            if c.get("dedupe_status") == "suppressed" or c.get("cooldown_applied") == 1:
                ranking_score -= 50.0

            ranked.append({
                **c,
                "setup_quality_score": setup_quality,
                "portfolio_fit_score": portfolio_fit,
                "diversification_score": diversification,
                "sizing_score": sizing_score,
                "ranking_score": ranking_score
            })

        ranked.sort(key=lambda x: (-x["ranking_score"], x["symbol"]))

        for idx, c in enumerate(ranked):
            c["final_candidate_rank"] = idx + 1

        return ranked

    def _ranked_batch_mode_enabled(self) -> bool:
        return (
            self.config.get("portfolio_execution_mode") == "risk_budgeted"
            and self.config.get("proposal_mode", {}).get("type") == "ranked_batch"
        )

    def _digest_display_cluster_name(self, cluster_name: str | None) -> str:
        labels = {
            "us_broad_market": "broad-market",
            "us_growth_tech": "growth-tech",
            "defensive_healthcare": "defensive-healthcare",
            "financials": "financials",
            "energy": "energy",
        }
        if not cluster_name:
            return "same-cluster"
        return labels.get(cluster_name, cluster_name.replace("_", " "))

    def _cluster_holdings(self, positions: list[Any]) -> dict[str, list[str]]:
        holdings: dict[str, list[str]] = {}
        for pos in positions:
            symbol = str(_value(pos, "symbol", "")).upper()
            cluster_name = self._get_symbol_cluster(symbol)
            if not cluster_name:
                continue
            holdings.setdefault(cluster_name, [])
            if symbol not in holdings[cluster_name]:
                holdings[cluster_name].append(symbol)
        return holdings

    def _digest_authoritative_state(self, symbol: str, window_start_iso: str, window_end_iso: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all(
            """
            SELECT tp.id AS proposal_id, tp.status AS proposal_status, tp.side, tp.created_at AS proposal_created_at, tp.expires_at,
                   pbc.id AS candidate_id, pbc.candidate_status, pb.id AS batch_id, pb.status AS batch_status,
                   o.id AS order_id, o.status AS order_status, o.created_at AS order_created_at, o.updated_at AS order_updated_at,
                   f.id AS fill_id, f.qty AS fill_qty, f.price AS fill_price, f.filled_at
            FROM trade_proposals tp
            LEFT JOIN proposal_batch_candidates pbc ON pbc.proposal_id=tp.id
            LEFT JOIN proposal_batches pb ON pb.id=pbc.batch_id
            LEFT JOIN orders o ON o.proposal_id=tp.id
            LEFT JOIN fills f ON f.order_id=o.id
            WHERE tp.symbol=? AND tp.created_at <= ?
            ORDER BY COALESCE(f.filled_at, o.updated_at, o.created_at, tp.expires_at, tp.created_at) DESC
            LIMIT 10
            """,
            (symbol, window_end_iso),
        )
        if not rows:
            return None

        def in_window(value: Any) -> bool:
            if not value:
                return False
            dt = _parse_datetime(value)
            if dt is None:
                return False
            return window_start_iso <= dt.isoformat() <= window_end_iso

        for row in rows:
            order_status = str(row.get("order_status") or "").lower()
            proposal_status = str(row.get("proposal_status") or "").lower()
            candidate_status = str(row.get("candidate_status") or "").lower()

            if row.get("fill_id") and in_window(row.get("filled_at")):
                if order_status == "filled":
                    return {"status": f"Approved and filled — {symbol} paper buy filled", "event": "filled", **row}
                if order_status == "partially_filled":
                    return {"status": f"Approved — {symbol} order partially filled", "event": "partially_filled", **row}

            if row.get("order_id") and (
                in_window(row.get("order_created_at")) or in_window(row.get("order_updated_at")) or in_window(row.get("proposal_created_at"))
            ):
                if order_status in {"submitted", "new", "accepted", "pending_new"} or candidate_status == "submitted" or proposal_status == "submitted":
                    return {"status": "Approved — order submitted, awaiting fill", "event": "submitted", **row}
                if order_status == "rejected":
                    return {"status": "Order rejected — no fill", "event": "rejected", **row}
                if order_status in {"canceled", "cancelled"}:
                    return {"status": "Order canceled — no fill", "event": "canceled", **row}
                if order_status == "blocked":
                    return {"status": "Proposal blocked by final validation", "event": "blocked", **row}

            if proposal_status == "pending" or candidate_status == "pending":
                if in_window(row.get("proposal_created_at")) or in_window(row.get("expires_at")):
                    return {"status": "Proposal pending approval", "event": "pending_approval", **row}
            if proposal_status == "expired" or candidate_status == "expired":
                if in_window(row.get("expires_at")) or in_window(row.get("proposal_created_at")):
                    return {"status": "Proposal expired — no order", "event": "expired", **row}
            if proposal_status == "rejected" or candidate_status == "rejected":
                if in_window(row.get("proposal_created_at")) or in_window(row.get("expires_at")):
                    return {"status": "Proposal rejected — no order", "event": "rejected", **row}
            if proposal_status == "blocked" or candidate_status == "blocked":
                if in_window(row.get("proposal_created_at")) or in_window(row.get("order_updated_at")):
                    return {"status": "Proposal blocked by final validation", "event": "blocked", **row}

        return None

    def _digest_market_memory_status(
        self,
        symbol: str,
        latest_row: dict[str, Any],
        obs_watchlist: set[str],
        cluster_holdings: dict[str, list[str]],
    ) -> dict[str, Any]:
        latest_score = latest_row.get("score") or 0.0
        latest_signal = latest_row.get("signal")
        no_action_reason = latest_row.get("no_action_reason") or ""
        no_act = no_action_reason.lower()
        score_threshold = self.config.get("ai", {}).get("ai_review_min_score", 65)
        cluster_name = self._get_symbol_cluster(symbol)
        held_symbols = [s for s in cluster_holdings.get(cluster_name or "", []) if s != symbol]
        cluster_display = self._digest_display_cluster_name(cluster_name)

        if symbol in obs_watchlist:
            return {
                "status": "Observation only — no proposal allowed",
                "event": "observation_only",
                "high_score": latest_score >= score_threshold,
            }
        if latest_score < score_threshold:
            return {"status": "No proposal — score below threshold", "event": "below_threshold", "high_score": False}
        if latest_signal not in {"ENTRY", "EXIT"}:
            return {"status": "Watch — no ENTRY signal", "event": "no_entry", "high_score": True}
        if "sleep" in no_act:
            return {"status": "Watch — BUY suppressed by sleep mode", "event": "sleep", "high_score": True}
        if "cooldown" in no_act or "dedupe" in no_act:
            return {"status": "Watch — cooldown active", "event": "cooldown", "high_score": True}
        if "gpt review" in no_act or "deferred due to ai" in no_act:
            return {"status": "Watch — GPT review unavailable", "event": "gpt_unavailable", "high_score": True}
        if "total portfolio exposure" in no_act or "portfolio_total_exposure" in no_act:
            return {"status": "Watch — total portfolio exposure cap reached", "event": "exposure_cap", "high_score": True}
        if "single symbol exposure" in no_act or "portfolio_single_symbol_exposure" in no_act:
            return {"status": "Watch — single-symbol exposure cap reached", "event": "single_symbol_cap", "high_score": True}
        if "cluster positions limit" in no_act or "portfolio_cluster_positions_limit" in no_act:
            if held_symbols:
                status = f"Watch — {cluster_display} cluster limit reached: existing {' and '.join(held_symbols)} positions"
            else:
                status = f"Watch — {cluster_display} cluster limit reached"
            return {
                "status": status,
                "event": "cluster_limit",
                "cluster_name": cluster_display,
                "held_symbols": held_symbols,
                "high_score": True,
            }
        if "cluster exposure limit" in no_act or "portfolio_cluster_exposure_limit" in no_act:
            return {
                "status": f"Watch — {cluster_display} cluster exposure cap reached",
                "event": "cluster_exposure",
                "cluster_name": cluster_display,
                "held_symbols": held_symbols,
                "high_score": True,
            }
        if (
            "exit is pending" in no_act
            or "exit proposal pending" in no_act
            or "new buy blocked because" in no_act and "exit" in no_act
            or "block_new_buy_if_exit_pending" in no_act
        ):
            blocker_label = self._exit_blocker_label_from_reason(no_action_reason)
            return {"status": f"Watch — New buy blocked — {blocker_label}", "event": "exit_blocked", "high_score": True, "blocker": blocker_label}
        if "emergency exit score is" in no_act or "block_new_buy_if_emergency_exit_score_above" in no_act:
            return {"status": "Watch — new buy blocked due to emergency exit risk", "event": "emergency_risk", "high_score": True}
        if "pyramiding check failed" in no_act or "add_on_check_failed" in no_act or "position not sufficiently profitable" in no_act or "cannot average down" in no_act:
            return {"status": "Watch — pyramiding check failed", "event": "pyramiding", "high_score": True}
        if "no entry/exit signal" in no_act:
            return {"status": "Watch — no ENTRY signal", "event": "no_entry", "high_score": True}
        if "blocked by risk checks" in no_act:
            return {"status": "Watch — blocked by risk checks", "event": "risk_blocked", "high_score": True}
        return {"status": "Watch — no proposal", "event": "no_proposal", "high_score": True}

    def _build_digest_summary(self, strongest: dict[str, Any], symbols_list: list[dict[str, Any]]) -> str:
        filled_syms = [x["symbol"] for x in symbols_list if x.get("_event") == "filled"]
        submitted_syms = [x["symbol"] for x in symbols_list if x.get("_event") == "submitted"]
        expired_syms = [x["symbol"] for x in symbols_list if x.get("_event") == "expired"]
        pending_syms = [x["symbol"] for x in symbols_list if x.get("_event") == "pending_approval"]

        parts: list[str] = []
        if filled_syms:
            parts.append(f"{', '.join(sorted(filled_syms))} was approved and filled during this window.")
        elif submitted_syms:
            parts.append(f"{', '.join(sorted(submitted_syms))} was approved and submitted during this window.")
        elif pending_syms:
            parts.append(f"Pending approval: {', '.join(sorted(pending_syms))}.")

        strongest_event = strongest.get("_event")
        if strongest_event == "cluster_limit":
            held_symbols = strongest.get("_held_symbols") or []
            cluster_name = strongest.get("_cluster_name") or "same-cluster"
            if held_symbols:
                parts.append(
                    f"{strongest['symbol']} scored highest, but it was blocked by the {cluster_name} cluster limit because {' and '.join(held_symbols)} are already held."
                )
            else:
                parts.append(f"{strongest['symbol']} scored highest, but it was blocked by the {cluster_name} cluster limit.")
        elif strongest_event == "cluster_exposure":
            parts.append(f"{strongest['symbol']} scored highest, but it was blocked because cluster exposure would exceed the configured limit.")
        elif strongest_event == "exposure_cap":
            parts.append(f"{strongest['symbol']} scored highest, but it was blocked because total portfolio exposure would exceed the configured limit.")
        elif strongest_event == "observation_only":
            parts.append(f"{strongest['symbol']} crossed score threshold but is observation-only, so no proposal is allowed.")
        elif strongest_event == "no_entry":
            parts.append(f"{strongest['symbol']} crossed score threshold but had no ENTRY signal.")
        elif strongest_event == "pending_approval" and not parts:
            parts.append(f"{strongest['symbol']} has an active proposal pending approval.")

        if expired_syms:
            parts.append(f"Expired with no order: {', '.join(sorted(expired_syms))}.")

        if not parts:
            high_score_watch = [x["symbol"] for x in symbols_list if x.get("_high_score")]
            if high_score_watch:
                strongest_name = strongest["symbol"]
                others = [s for s in sorted(high_score_watch) if s != strongest_name]
                if others:
                    parts.append(f"{strongest_name} scored highest, while {', '.join(others)} also crossed the score threshold.")
                else:
                    parts.append(f"{strongest_name} crossed the score threshold.")
            else:
                parts.append("No setup crossed the score threshold.")
        return " ".join(parts)

    def _dynamic_universe_update_since(self, window_start_iso: str) -> str | None:
        promotions = self.storage.fetch_all(
            """
            SELECT symbol, from_tier, to_tier, reason, payload, created_at
            FROM symbol_promotion_decisions
            WHERE created_at>=?
            ORDER BY created_at DESC
            LIMIT 12
            """,
            (window_start_iso,),
        )
        demotions = self.storage.fetch_all(
            """
            SELECT symbol, reason
            FROM symbol_demotion_decisions
            WHERE created_at>=?
            ORDER BY created_at DESC
            LIMIT 12
            """,
            (window_start_iso,),
        )
        health = self.storage.fetch_all(
            """
            SELECT provider, status, error
            FROM data_provider_health
            WHERE checked_at>=? AND status!='ok'
            ORDER BY checked_at DESC
            LIMIT 3
            """,
            (window_start_iso,),
        )
        capabilities = self.storage.fetch_all(
            """
            SELECT endpoint_name, available, plan_limited
            FROM data_provider_capabilities
            WHERE updated_at>=?
            ORDER BY endpoint_name
            """,
            (window_start_iso,),
        )
        schedule_rows = self.storage.fetch_all(
            """
            SELECT schedule_name, last_started_at, last_completed_at, last_success_at, last_skipped_at,
                   last_skip_reason, missed_count, catchup_status, provider_health_status,
                   internet_status, power_status, data_freshness_status, promotion_allowed, updated_at
            FROM dynamic_universe_schedule_state
            WHERE updated_at>=?
            ORDER BY updated_at DESC
            LIMIT 5
            """,
            (window_start_iso,),
        )
        completed_runs = self.storage.fetch_all(
            """
            SELECT research_type, symbols_promoted, detail, ended_at
            FROM universe_research_runs
            WHERE status='completed' AND ended_at>=?
            ORDER BY ended_at DESC
            LIMIT 5
            """,
            (window_start_iso,),
        )
        stale_rows = self.storage.fetch_all(
            """
            SELECT event_type, detail, created_at
            FROM dynamic_universe_audit
            WHERE created_at>=?
              AND event_type IN (
                'dynamic_universe_stale_data_guard',
                'dynamic_universe_promotions_blocked_stale_research',
                'dynamic_universe_demotions_blocked_provider_unavailable'
              )
            ORDER BY created_at DESC
            LIMIT 3
            """,
            (window_start_iso,),
        )
        proposal_rows = self.storage.fetch_all(
            """
            SELECT
                (SELECT COUNT(*) FROM trade_proposals WHERE datetime(created_at)>=datetime(?)) AS proposals,
                (SELECT COUNT(*) FROM orders WHERE datetime(created_at)>=datetime(?)) AS orders
            """,
            (window_start_iso, window_start_iso),
        )
        if not promotions and not demotions and not health and not schedule_rows and not stale_rows and not capabilities and not completed_runs:
            return None
        to_observation = sorted({r["symbol"] for r in promotions if r["to_tier"] == "observation"})
        to_tradable = sorted({r["symbol"] for r in promotions if r["to_tier"] == "paper_tradable"})
        to_research = sorted({r["symbol"] for r in promotions if r["to_tier"] == "research_candidate"})
        demoted = sorted({r["symbol"] for r in demotions})
        parts = ["Universe update:"]
        if to_research:
            parts.append(f"Research candidates: {', '.join(to_research)}.")
        if to_observation:
            parts.append(f"Promoted to observation: {', '.join(to_observation)}.")
        if to_tradable:
            parts.append(f"Promoted to paper-tradable: {', '.join(to_tradable)}.")
        if demoted:
            parts.append(f"Demoted: {', '.join(demoted)}.")
        if health:
            statuses = ", ".join(sorted({f"{r['provider']} {r['status']}" for r in health}))
            parts.append(f"Provider health: {statuses}.")
        if capabilities:
            available = sorted({r["endpoint_name"] for r in capabilities if int(r.get("available") or 0) == 1})
            limited = sorted({r["endpoint_name"] for r in capabilities if int(r.get("plan_limited") or 0) == 1})
            if limited:
                using = ", ".join(available) if available else "available endpoints"
                unavailable = ", ".join(limited)
                parts.append(f"Dynamic universe provider access is partial. Using {using}; plan-limited: {unavailable}.")
        completed_names = sorted({str(r["research_type"]) for r in completed_runs if r.get("research_type")})
        if completed_names:
            readable = ", ".join(name.replace("_", " ") for name in completed_names)
            parts.append(f"Research subtasks completed: {readable}.")
        current_skips = []
        if schedule_rows:
            for latest in schedule_rows:
                last_skip = latest.get("last_skipped_at")
                last_success = latest.get("last_success_at")
                skip_current = bool(latest.get("last_skip_reason") and last_skip)
                if skip_current and last_success:
                    try:
                        skip_current = datetime.fromisoformat(str(last_skip).replace("Z", "+00:00")) > datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                    except Exception:
                        skip_current = True
                if skip_current:
                    current_skips.append(latest)
            if current_skips and not completed_runs and not promotions:
                latest = current_skips[0]
                missed = int(latest.get("missed_count") or 0)
                suffix = f" Missed count: {missed}." if missed else ""
                parts.append(f"Dynamic Universe research skipped: {latest['last_skip_reason']}.{suffix}")
            else:
                for latest in current_skips:
                    reason = self._digest_skip_reason_label(str(latest.get("last_skip_reason") or "unknown"))
                    parts.append(f"{str(latest['schedule_name']).replace('_', ' ').capitalize()} skipped: {reason}; existing research state was still used.")
                catchups = [r for r in schedule_rows if r.get("catchup_status") == "completed"]
                if catchups and not current_skips:
                    parts.append(f"Research catch-up completed: {str(catchups[0]['schedule_name']).replace('_', ' ')}.")
        if to_observation and completed_runs:
            parts.append("Observation promotions used deterministic candidate state from the latest completed research subtask.")
        stale_guard_rows = [r for r in stale_rows if r.get("event_type") in {"dynamic_universe_stale_data_guard", "dynamic_universe_promotions_blocked_stale_research"}]
        demotion_guard_rows = [r for r in stale_rows if r.get("event_type") == "dynamic_universe_demotions_blocked_provider_unavailable"]
        if stale_guard_rows:
            parts.append("Stale research guard active: BUY/ADD eligibility and unsafe paper-tradable promotion blocked until fresh refresh; observation-only tracking and SELL/EXIT monitoring may continue.")
        if demotion_guard_rows:
            parts.append("Provider guard active: demotions based only on unavailable provider data are paused.")
        if proposal_rows:
            counts = proposal_rows[0]
            if int(counts.get("proposals") or 0) == 0 and int(counts.get("orders") or 0) == 0:
                parts.append("No dynamic proposals/orders created.")
        return " ".join(parts)

    def _digest_skip_reason_label(self, reason: str) -> str:
        if reason == "missing_api_key":
            return "provider key missing"
        if reason in {"rate_limited", "max_calls_per_run_exceeded"}:
            return "provider rate-limited"
        if reason in {"capability_disabled", "cooldown_active"}:
            return "provider cooldown active"
        if reason == "no_internet":
            return "internet unavailable"
        return reason.replace("_", " ")

    def _risk_budget_cfg(self) -> dict[str, Any]:
        rb = self.config.get("risk_budget", {})
        pb = self.config.get("portfolio_behavior", {})
        sizing = self.config.get("position_sizing", {})
        optimizer = self.config.get("portfolio_optimizer", {})
        return {
            "risk_per_trade_pct": float(rb.get("risk_per_trade_pct", sizing.get("risk_per_trade_pct", 0.05))),
            "max_open_risk_pct": float(rb.get("max_open_risk_pct", 0.30)),
            "max_daily_realized_loss_pct": float(rb.get("max_daily_realized_loss_pct", 0.25)),
            "max_total_portfolio_exposure_pct": float(rb.get("max_total_portfolio_exposure_pct", pb.get("max_total_portfolio_exposure_pct", 6.0))),
            "max_single_symbol_exposure_pct": float(rb.get("max_single_symbol_exposure_pct", pb.get("max_single_symbol_exposure_pct", 2.5))),
            "max_cluster_exposure_pct": float(rb.get("max_cluster_exposure_pct", optimizer.get("max_same_cluster_exposure_pct", pb.get("max_correlated_us_equity_exposure_pct", 5.0)))),
            "min_notional": float(sizing.get("min_paper_notional", 5.0)),
        }

    def _buying_power(self, account: Any) -> float:
        return float(_value(account, "buying_power", _value(account, "cash", 0.0)) or 0.0)

    def _record_risk_budget_snapshot(self, snapshot: dict[str, Any], account: Any, now: datetime) -> dict[str, Any]:
        cfg = self._risk_budget_cfg()
        buying_power = self._buying_power(account)
        row = {
            "total_exposure_pct": float(snapshot.get("total_exposure_pct", 0.0) or 0.0),
            "open_risk_pct": 0.0,
            "daily_realized_loss_pct": 0.0,
            "max_open_risk_pct": cfg["max_open_risk_pct"],
            "buying_power": buying_power,
        }
        self.storage.execute(
            "INSERT INTO risk_budget_snapshots(id,run_id,timestamp,total_exposure_pct,open_risk_pct,daily_realized_loss_pct,max_open_risk_pct,buying_power,payload) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), self.run_id, now.isoformat(), row["total_exposure_pct"], row["open_risk_pct"],
                row["daily_realized_loss_pct"], row["max_open_risk_pct"], buying_power, json_dumps(row)
            ),
        )
        return row

    def _apply_risk_budget_to_ranked_candidates(
        self,
        ranked_candidates: list[dict[str, Any]],
        snapshot: dict[str, Any],
        account: Any,
        now: datetime,
    ) -> tuple[set[str], dict[str, str]]:
        if not ranked_candidates:
            return set(), {}

        cfg = self._risk_budget_cfg()
        equity = float(snapshot.get("portfolio_equity", 10000.0) or 10000.0)
        if equity <= 0:
            equity = 10000.0
        buying_power_remaining = self._buying_power(account)
        total_exposure_after = float(snapshot.get("total_exposure_pct", 0.0) or 0.0)
        single_after = dict(snapshot.get("single_exposures", {}) or {})
        cluster_after = dict(snapshot.get("cluster_exposures", {}) or {})
        open_risk_after = 0.0
        allowed: set[str] = set()
        reasons: dict[str, str] = {}

        for candidate in ranked_candidates:
            symbol = str(candidate["symbol"]).upper()
            rank = int(candidate.get("final_candidate_rank") or 0)
            raw_notional = float(candidate.get("final_notional", 0.0) or 0.0)
            price = float(candidate.get("price", candidate.get("latest_price", 0.0)) or 0.0)
            stop_distance_pct = float(candidate.get("stop_distance_pct", 8.0) or 8.0)
            if stop_distance_pct <= 0:
                stop_distance_pct = 8.0

            cap_reason = None
            reduction_reason = None
            final_notional = max(0.0, raw_notional)

            current_symbol_pct = float(single_after.get(symbol, 0.0) or 0.0)
            cluster_name = self._get_symbol_cluster(symbol)
            current_cluster_pct = float(cluster_after.get(cluster_name or "", 0.0) or 0.0)

            def pct_to_notional(remaining_pct: float) -> float:
                return max(0.0, equity * remaining_pct / 100)

            limits = [
                (pct_to_notional(cfg["max_total_portfolio_exposure_pct"] - total_exposure_after), "portfolio exposure budget"),
                (pct_to_notional(cfg["max_single_symbol_exposure_pct"] - current_symbol_pct), "single-symbol exposure budget"),
                (pct_to_notional(cfg["max_cluster_exposure_pct"] - current_cluster_pct), "cluster exposure budget"),
                (buying_power_remaining, "paper buying power"),
            ]
            per_trade_risk_cap_notional = pct_to_notional(cfg["risk_per_trade_pct"]) / (stop_distance_pct / 100)
            limits.append((per_trade_risk_cap_notional, "per-trade risk budget"))
            remaining_open_risk_pct = cfg["max_open_risk_pct"] - open_risk_after
            risk_cap_notional = pct_to_notional(remaining_open_risk_pct) / (stop_distance_pct / 100)
            limits.append((risk_cap_notional, "open risk budget"))

            for limit_value, reason in limits:
                if final_notional > limit_value:
                    final_notional = max(0.0, limit_value)
                    reduction_reason = reason

            risk_pct = (final_notional * (stop_distance_pct / 100) / equity) * 100 if equity else 0.0
            exposure_pct = (final_notional / equity) * 100 if equity else 0.0
            total_after_candidate = total_exposure_after + exposure_pct
            single_after_candidate = current_symbol_pct + exposure_pct
            cluster_after_candidate = current_cluster_pct + exposure_pct
            open_risk_after_candidate = open_risk_after + risk_pct

            passed = True
            if candidate.get("preproposal_block_reason"):
                passed = False
                cap_reason = f"not actionable - pre-proposal risk check failed: {candidate['preproposal_block_reason']}"
            elif raw_notional <= 0 or price <= 0:
                passed = False
                cap_reason = "not actionable - no valid size or price"
            elif final_notional < cfg["min_notional"]:
                passed = False
                cap_reason = "not actionable - insufficient risk budget after higher-ranked candidates"
            elif risk_pct > cfg["risk_per_trade_pct"]:
                passed = False
                cap_reason = "not actionable - per-trade risk budget exceeded"
            elif total_after_candidate > cfg["max_total_portfolio_exposure_pct"] + 1e-9:
                passed = False
                cap_reason = "not actionable - portfolio exposure budget exceeded"
            elif single_after_candidate > cfg["max_single_symbol_exposure_pct"] + 1e-9:
                passed = False
                cap_reason = "not actionable - single-symbol exposure budget exceeded"
            elif cluster_after_candidate > cfg["max_cluster_exposure_pct"] + 1e-9:
                passed = False
                cap_reason = "not actionable - cluster exposure budget exceeded"
            elif final_notional > buying_power_remaining:
                passed = False
                cap_reason = "not actionable - insufficient paper buying power"

            if passed:
                allowed.add(symbol)
                reasons[symbol] = "passes ranked risk budget and exposure checks"
                candidate["final_notional"] = final_notional
                candidate["suggested_shares"] = final_notional / price if price > 0 else 0.0
                candidate["risk_budget_block_reason"] = None
                total_exposure_after = total_after_candidate
                single_after[symbol] = single_after_candidate
                if cluster_name:
                    cluster_after[cluster_name] = cluster_after_candidate
                open_risk_after = open_risk_after_candidate
                buying_power_remaining -= final_notional
            else:
                reasons[symbol] = cap_reason or reduction_reason or "not actionable - risk budget blocked"
                candidate["risk_budget_block_reason"] = reasons[symbol]

            self.storage.execute(
                "INSERT INTO candidate_batch_allocations(id,run_id,batch_id,proposal_id,symbol,rank,raw_suggested_notional,adjusted_suggested_notional,risk_budget_adjusted_notional,final_suggested_notional,final_suggested_shares,cap_reason,reduction_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), self.run_id, None, None, symbol, rank, raw_notional, raw_notional,
                    final_notional, final_notional, final_notional / price if price > 0 else 0.0,
                    cap_reason, reduction_reason, now.isoformat()
                ),
            )
            self.storage.execute(
                "INSERT INTO candidate_risk_budget_decisions(id,run_id,batch_id,candidate_id,proposal_id,order_id,broker_order_id,fill_id,symbol,timestamp,risk_per_trade_pct,open_risk_after_pct,max_open_risk_pct,total_exposure_after_pct,single_symbol_exposure_after_pct,cluster_exposure_after_pct,buying_power,passed,block_reason,cluster_name,cluster_held_symbols,cluster_positions_count_after,max_cluster_positions,max_cluster_exposure_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), self.run_id, None, None, None, None, None, None, symbol, now.isoformat(), cfg["risk_per_trade_pct"],
                    open_risk_after_candidate, cfg["max_open_risk_pct"], total_after_candidate,
                    single_after_candidate, cluster_after_candidate, buying_power_remaining, int(passed),
                    None if passed else reasons[symbol],
                    cluster_name,
                    json_dumps(sorted(snapshot.get("cluster_symbols", {}).get(cluster_name or "", []))) if cluster_name else None,
                    int(snapshot.get("cluster_counts", {}).get(cluster_name or "", 0)) + (1 if cluster_name else 0),
                    int(self.config.get("portfolio_optimizer", {}).get("max_same_cluster_positions", 2)),
                    float(cfg["max_cluster_exposure_pct"]),
                ),
            )
            self.storage.execute(
                "INSERT INTO ranked_opportunity_sets(id,run_id,batch_id,timestamp,symbol,rank,actionable,reason,score,suggested_notional,suggested_shares,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), self.run_id, None, now.isoformat(), symbol, rank, int(passed),
                    reasons[symbol], candidate.get("score"), final_notional,
                    final_notional / price if price > 0 else 0.0, json_dumps(candidate)
                ),
            )

        return allowed, reasons

    def _format_ranked_batch_message(
        self,
        proposals: list[dict[str, Any]],
        tracked_candidates: list[dict[str, Any]],
        risk_snapshot: dict[str, Any],
    ) -> str:
        now_dt = datetime.now(UTC)
        created_candidates = [p.get("created_at") for p in proposals if p.get("created_at")]
        created_dt = _parse_datetime(created_candidates[0]) if created_candidates else now_dt
        expiries = [_parse_datetime(p["expires_at"]) for p in proposals if p.get("expires_at")]
        batch_expiry = min(expiries) if expiries else None
        lines = [
            "📊 Paper position and trade opportunity set",
            f"Created: {_format_sgt_time(created_dt)} SGT",
            _format_expiry_line(batch_expiry, now_dt) if batch_expiry else "Expires: not set",
            "No reply before expiry = no order.",
            "Replies after expiry will be rejected.",
            "Portfolio room:",
            f"- Total exposure after proposed trades: {_format_small_percent(risk_snapshot.get('total_exposure_pct', 0.0))} / {self._risk_budget_cfg()['max_total_portfolio_exposure_pct']:.1f}%",
            f"- Open risk after proposed trades: {_format_small_percent(risk_snapshot.get('open_risk_pct', 0.0))} / {self._risk_budget_cfg()['max_open_risk_pct']:.2f}%",
            f"- Available paper buying power: ${risk_snapshot.get('buying_power', 0.0):,.2f}",
            "Actionable:",
        ]
        for idx, proposal in enumerate(proposals, start=1):
            pm_type = proposal.get("position_management_decision_type")
            action_word = "Add" if proposal.get("action") == "add" else ("Sell" if proposal.get("side") == "sell" else "Buy")
            if pm_type == "TAKE_PROFIT_PARTIAL":
                action_word = "TAKE PROFIT"
            elif pm_type == "PROFIT_PROTECT_EXIT":
                action_word = "PROFIT PROTECT"
            elif pm_type == "TRAILING_STOP_EXIT":
                action_word = "TRAILING STOP"
            elif pm_type == "HEALTHY_PULLBACK_ADD":
                action_word = "ADD"
            qty = float(proposal.get("qty") or 0.0)
            pm = proposal.get("position_management_decision") or {}
            candidate_expiry = proposal.get("candidate_expires_at") or proposal.get("expires_at")
            lines.extend([
                f"{idx}. {action_word} {proposal['symbol']} - ${float(proposal.get('notional') or 0.0):.2f} / approx. {qty:.6f} shares",
                f"   Score: {float(proposal.get('score') or 0.0):.0f}",
                "   Risk: normal",
                "   Portfolio fit: passes risk budget",
                f"   Reason: {_normalize_ranked_candidate_reason(proposal.get('selection_reason') or proposal.get('reason'), idx)}",
            ])
            if batch_expiry and candidate_expiry and _parse_datetime(candidate_expiry) != batch_expiry:
                lines.append(f"   Candidate expiry: {_format_expiry_line(candidate_expiry, now_dt).replace('Expires: ', '')}")
            if pm_type:
                if pm_type == "HEALTHY_PULLBACK_ADD":
                    lines.append("   Note: add-to-winner, not averaging down")
                if pm.get("unrealized_profit_pct") is not None:
                    lines.append(f"   Current gain: {float(pm['unrealized_profit_pct']):+.2f}%")
                if pm.get("max_unrealized_profit_pct") is not None:
                    lines.append(f"   Peak gain: {float(pm['max_unrealized_profit_pct']):+.2f}%")
                if pm.get("profit_giveback_ratio") is not None:
                    lines.append(f"   Profit giveback: {float(pm['profit_giveback_ratio']) * 100:.1f}%")
        if not proposals:
            lines.append("None")

        tracked = [c for c in tracked_candidates if str(c.get("symbol", "")).upper() not in {str(p.get("symbol", "")).upper() for p in proposals}]
        if tracked:
            lines.append("Not actionable but tracked:")
            for idx, candidate in enumerate(tracked, start=len(proposals) + 1):
                lines.append(f"{idx}. {candidate['symbol']} - blocked")
                lines.append(f"   Reason: {candidate.get('risk_budget_block_reason') or candidate.get('no_action_reason') or 'not actionable but recorded'}")

        symbols = [str(p["symbol"]).upper() for p in proposals]
        lines.extend(["Reply:"])
        for sym in symbols:
            lines.append(f"yes {sym} = approve {sym} only")
        if symbols:
            lines.append("yes all = approve all actionable candidates after final checks")
        for sym in symbols:
            lines.append(f"no {sym} = reject {sym}")
        if symbols:
            lines.append("no all = reject all actionable candidates")
            lines.append("Plain yes is ambiguous when more than one candidate is pending.")
        return "\n".join(lines)

    def _send_ranked_batch_if_needed(
        self,
        proposals: list[dict[str, Any]],
        tracked_candidates: list[dict[str, Any]],
        risk_snapshot: dict[str, Any],
    ) -> None:
        if not proposals:
            return
        batch_id = str(uuid.uuid4())
        batch_expiry_dt = min(_parse_datetime(p["expires_at"]) for p in proposals if p.get("expires_at"))
        expires_at = batch_expiry_dt.isoformat()
        self.storage.execute(
            "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload,expiry_notified) VALUES(?,?,?,?,?,?,?,?)",
            (batch_id, self.run_id, None, "pending", iso_now(), expires_at, json_dumps({"proposal_ids": [p["id"] for p in proposals]}), 0),
        )
        for idx, proposal in enumerate(proposals, start=1):
            candidate_id = str(uuid.uuid4())
            proposal["candidate_expires_at"] = proposal.get("expires_at")
            proposal["selection_reason"] = _normalize_ranked_candidate_reason(proposal.get("selection_reason") or proposal.get("reason"), idx)
            proposal["expires_at"] = expires_at
            payload = json.loads(proposal.get("payload") or "{}") if isinstance(proposal.get("payload"), str) else {}
            if payload:
                payload["candidate_expires_at"] = proposal["candidate_expires_at"]
                payload["expires_at"] = expires_at
            candidate_action = proposal.get("position_management_decision_type") or proposal.get("action") or proposal["side"]
            self.storage.execute(
                "UPDATE trade_proposals SET expires_at=?, payload=? WHERE id=?",
                (expires_at, json_dumps({**payload, **proposal}), proposal["id"]),
            )
            self.storage.execute(
                "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,rank,reason,created_at,expires_at,payload,expiry_notified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id, batch_id, proposal["id"], None, proposal["symbol"], proposal["side"],
                    candidate_action, "pending", idx,
                    proposal.get("selection_reason") or proposal.get("reason"), iso_now(), expires_at,
                    json_dumps(proposal), 0
                ),
            )
            self.storage.link_batch_candidate_records(proposal["id"], batch_id, candidate_id)
        message = self._format_ranked_batch_message(proposals, tracked_candidates, risk_snapshot)
        res_tg = self.telegram.send_message(message)
        if res_tg and isinstance(res_tg, dict) and "message_id" in res_tg:
            msg_id = str(res_tg["message_id"])
            self.storage.execute("UPDATE proposal_batches SET telegram_message_id=? WHERE id=?", (msg_id, batch_id))
            self.storage.execute("UPDATE proposal_batch_candidates SET telegram_message_id=? WHERE batch_id=?", (msg_id, batch_id))
            self.storage.execute(
                f"UPDATE trade_proposals SET telegram_message_id=? WHERE id IN ({','.join(['?'] * len(proposals))})",
                (msg_id, *[p["id"] for p in proposals]),
            )

    def _run_performance_lab(self, profile_results: list[dict[str, Any]], active_watchlist: list[str], positions: list[Any], now: datetime, snapshot: dict[str, Any]) -> None:
        qualified_setups_cnt = 0
        shadow_trades_cnt = 0
        actual_trades_cnt = 0

        for res in profile_results:
            score = res["score"]
            symbol = res["symbol"]
            signal = res["signal"]

            is_qualified = (score >= 65 or signal.action in ("ENTRY", "EXIT"))
            if not is_qualified:
                continue

            qualified_setups_cnt += 1
            setup_id = str(uuid.uuid4())
            is_active = 1 if symbol in active_watchlist else 0

            self.storage.execute(
                """INSERT INTO trade_setups(
                    id, run_id, symbol, timestamp, side, action, setup_key, is_active,
                    price, score, asset_score, volatility_regime, trend_state, gpt_status,
                    proposal_eligible, proposal_sent, block_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    setup_id, self.run_id, symbol, now.isoformat(), signal.side, signal.action, res.get("setup_key"), is_active,
                    res["price"], score, res["asset_score"], res["volatility_regime"], signal.reason,
                    "Completed" if res.get("gpt_called") else "Not called",
                    1 if res.get("proposal_allowed") else 0,
                    1 if res.get("proposal_generated") else 0,
                    res.get("no_action_reason")
                )
            )
            tier_rows = self.storage.fetch_all("SELECT tier FROM universe_symbols WHERE symbol=? LIMIT 1", (symbol,))
            if tier_rows:
                self.storage.execute(
                    "INSERT INTO dynamic_universe_performance(id,run_id,symbol,tier,metric,value,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        self.run_id,
                        symbol,
                        tier_rows[0]["tier"],
                        "scan_score",
                        score,
                        now.isoformat(),
                        json_dumps(
                            {
                                "setup_id": setup_id,
                                "action": signal.action,
                                "proposal_eligible": 1 if res.get("proposal_allowed") else 0,
                                "proposal_sent": 1 if res.get("proposal_generated") else 0,
                            }
                        ),
                    ),
                )

            is_tradable_buy = (score >= 65 and signal.side == "buy" and signal.action == "ENTRY")
            if is_tradable_buy:
                actual_proposal = self.storage.fetch_all(
                    "SELECT id, status FROM trade_proposals WHERE run_id=? AND symbol=? AND side='buy' AND status IN ('submitted', 'approved', 'filled')",
                    (self.run_id, symbol)
                )
                if not actual_proposal:
                    shadow_trades_cnt += 1
                    shadow_id = str(uuid.uuid4())

                    port_state = {
                        "portfolio_equity": snapshot["portfolio_equity"],
                        "total_exposure_pct": snapshot["total_exposure_pct"],
                        "single_exposure_pct": snapshot["single_exposures"].get(symbol, 0.0),
                        "cluster_exposures": snapshot["cluster_exposures"]
                    }

                    self.storage.execute(
                        """INSERT INTO shadow_trades(
                            id, run_id, setup_id, symbol, side, would_have_entry_price, would_have_entry_time,
                            would_have_notional, would_have_shares, would_have_stop_price, would_have_stop_distance_pct,
                            reason_not_executed, score, volatility_regime, gpt_confidence, gpt_caution, setup_key,
                            portfolio_state_json, sleep_mode_active, cooldown_state, selected_actual_trade_this_cycle
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            shadow_id, self.run_id, setup_id, symbol, "buy", res["price"], now.isoformat(),
                            res.get("final_notional", 5.0), res.get("suggested_shares", 0.0), res.get("stop_price"), res.get("stop_distance_pct"),
                            res.get("no_action_reason") or "suppressed", score, res["volatility_regime"],
                            res.get("gpt_confidence"), res.get("gpt_caution"), res.get("setup_key"),
                            json.dumps(port_state), res.get("sleep_mode_active", 0),
                            res.get("dedupe_status"), 0
                        )
                    )

                    self.storage.execute(
                        """INSERT INTO trade_outcomes(
                            id, trade_id, actual_or_shadow, symbol, entry_time, entry_price, outcome_status,
                            stop_hit, target_reached, add_on_improved, beat_shadow_alternatives, updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), shadow_id, "shadow", symbol, now.isoformat(), res["price"], "pending",
                            0, 0, None, None, now.isoformat()
                        )
                    )
                else:
                    actual_trades_cnt += 1

        self.storage.execute(
            "INSERT INTO performance_lab_summaries(id, run_id, timestamp, total_qualified_setups, total_shadow_trades, total_actual_trades) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, now.isoformat(), qualified_setups_cnt, shadow_trades_cnt, actual_trades_cnt)
        )

    def _update_forward_outcomes(self) -> None:
        now = datetime.now(UTC)
        pending = self.storage.fetch_all("SELECT * FROM trade_outcomes WHERE outcome_status IN ('pending','pending_forward_returns')")
        if not pending:
            return

        logger.info("Updating %d pending trade outcomes...", len(pending))
        for out in pending:
            out_id = out["id"]
            trade_id = out["trade_id"]
            actual_or_shadow = out["actual_or_shadow"]
            symbol = out["symbol"]
            entry_price = float(out["entry_price"])
            entry_time_str = out["entry_time"]

            try:
                entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00")).replace(tzinfo=UTC)
            except Exception:
                continue

            stop_price = None
            if actual_or_shadow == "shadow":
                shadow_row = self.storage.fetch_all("SELECT would_have_stop_price FROM shadow_trades WHERE id=?", (trade_id,))
                if shadow_row and shadow_row[0]["would_have_stop_price"] is not None:
                    stop_price = float(shadow_row[0]["would_have_stop_price"])
            else:
                order_row = self.storage.fetch_all("SELECT proposal_id FROM orders WHERE id=?", (trade_id,))
                if order_row:
                    prop_id = order_row[0]["proposal_id"]
                    prop_row = self.storage.fetch_all("SELECT payload FROM trade_proposals WHERE id=?", (prop_id,))
                    if prop_row:
                        try:
                            payload = json.loads(prop_row[0]["payload"])
                            stop_price = payload.get("stop_price")
                        except Exception:
                            pass
            if not stop_price or stop_price >= entry_price:
                stop_price = entry_price * 0.92

            target_price = entry_price + 2.0 * (entry_price - stop_price)

            if self.broker is None:
                continue
            try:
                bars = normalize_bars(self.broker.get_historical_bars(symbol, "1Day", 250), symbol)
            except Exception as e:
                logger.warning("Failed to fetch historical bars for %s during outcome update: %s", symbol, e)
                continue

            if bars.empty:
                continue

            future_bars = []
            for idx, row in bars.iterrows():
                bar_dt = idx
                if hasattr(bar_dt, "to_pydatetime"):
                    bar_dt = bar_dt.to_pydatetime()
                if bar_dt.tzinfo is None:
                    bar_dt = bar_dt.replace(tzinfo=UTC)
                else:
                    bar_dt = bar_dt.astimezone(UTC)
                if bar_dt > entry_dt:
                    future_bars.append((bar_dt, row))

            if not future_bars:
                continue

            ret_1d = None
            ret_5d = None
            ret_20d = None

            if len(future_bars) >= 1:
                ret_1d = float(future_bars[0][1]["close"] / entry_price - 1.0) * 100
            if len(future_bars) >= 5:
                ret_5d = float(future_bars[4][1]["close"] / entry_price - 1.0) * 100
            if len(future_bars) >= 20:
                ret_20d = float(future_bars[19][1]["close"] / entry_price - 1.0) * 100

            max_high = max(float(row["high"]) for _, row in future_bars[:20])
            min_low = min(float(row["low"]) for _, row in future_bars[:20])
            mfe = (max_high - entry_price) / entry_price * 100
            mae = (min_low - entry_price) / entry_price * 100

            stop_hit = 1 if min_low <= stop_price else 0
            target_reached = 1 if max_high >= target_price else 0

            outcome_status = "complete" if (len(future_bars) >= 20 or stop_hit == 1) else "pending"

            self.storage.execute(
                """UPDATE trade_outcomes SET
                    forward_return_1d=?, forward_return_5d=?, forward_return_20d=?,
                    max_favorable_excursion=?, max_adverse_excursion=?, stop_hit=?,
                    target_reached=?, outcome_status=?, updated_at=?
                   WHERE id=?""",
                (
                    ret_1d, ret_5d, ret_20d, mfe, mae, stop_hit, target_reached,
                    outcome_status, now.isoformat(), out_id
                )
            )

            if outcome_status == "complete" and actual_or_shadow == "actual" and ret_20d is not None:
                shadows = self.storage.fetch_all(
                    "SELECT forward_return_20d FROM trade_outcomes WHERE actual_or_shadow='shadow' AND symbol=? AND outcome_status='complete'",
                    (symbol,)
                )
                if shadows:
                    max_shadow = max([float(s["forward_return_20d"]) for s in shadows if s["forward_return_20d"] is not None] + [-999.0])
                    beat = 1 if ret_20d > max_shadow else 0
                    self.storage.execute("UPDATE trade_outcomes SET beat_shadow_alternatives=? WHERE id=?", (beat, out_id))

    def _create_shadow_trade_from_proposal(self, prop_row: dict[str, Any], reason: str) -> None:
        exists = self.storage.fetch_all("SELECT 1 FROM shadow_trades WHERE id=?", (prop_row["id"],))
        if exists:
            return

        now = datetime.now(UTC)
        try:
            payload = json.loads(prop_row.get("payload") or "{}")
        except Exception:
            payload = {}

        shadow_id = prop_row["id"]
        self.storage.execute(
            """INSERT INTO shadow_trades(
                id, run_id, setup_id, symbol, side, would_have_entry_price, would_have_entry_time,
                would_have_notional, would_have_shares, would_have_stop_price, would_have_stop_distance_pct,
                reason_not_executed, score, volatility_regime, gpt_confidence, gpt_caution, setup_key,
                portfolio_state_json, sleep_mode_active, cooldown_state, selected_actual_trade_this_cycle
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                shadow_id, prop_row["run_id"], prop_row.get("signal_id"), prop_row["symbol"], prop_row["side"],
                prop_row.get("price") or payload.get("latest_price") or prop_row.get("current_price"), prop_row["created_at"],
                prop_row.get("notional"), payload.get("suggested_shares", 0.0), payload.get("stop_price"), payload.get("stop_distance_pct"),
                reason, prop_row.get("score"), payload.get("volatility_regime"),
                prop_row.get("ai_confidence"), prop_row.get("ai_caution"), prop_row.get("setup_key"),
                json.dumps({}), prop_row.get("sleep_mode_active", 0),
                prop_row.get("cooldown_reason"), 0
            )
        )

        self.storage.execute(
            """INSERT INTO trade_outcomes(
                id, trade_id, actual_or_shadow, symbol, entry_time, entry_price, outcome_status,
                stop_hit, target_reached, add_on_improved, beat_shadow_alternatives, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), shadow_id, "shadow", prop_row["symbol"], prop_row["created_at"],
                prop_row.get("price") or payload.get("latest_price") or prop_row.get("current_price"), "pending",
                0, 0, None, None, now.isoformat()
            )
        )
