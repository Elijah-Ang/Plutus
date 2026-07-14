from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
import dataclasses
import uuid
import pandas as pd
from datetime import UTC, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .ai_review import AIReviewer, deterministic_review
from .adaptive_conviction import AdaptiveConvictionEngine
from .adaptive_sizing import AdaptiveSizingEngine
from .capabilities import AUTO_EXECUTION_SUPPORTED, require_protective_paper_exit_support
from .crypto_research import CryptoResearchEngine, crypto_quiet_hours_active

logger = logging.getLogger("trading_agent")

SGT = ZoneInfo("Asia/Singapore")

from .approval_parser import parse_approval
from .approval_workflow import (
    ApprovalWorkflowConflict,
    ApprovalWorkflowState,
    ApprovalWorkflowStore,
)
from .data_providers.eodhd import EODHDProvider
from .dynamic_universe import DynamicUniverseEngine, OBSERVATION, PAPER_TRADABLE, RESEARCH_CANDIDATE
from .execution import Executor, ExecutionResult
from .execution import DurableExecutionStore
from .health import HealthMonitor, record_heartbeat
from .internet import internet_available
from .lot_ledger import LotLedger
from .loss_controls import LOSS_METRICS_VERSION, build_loss_metrics
from .market_data import normalize_bars
from .power import get_power_status
from .position_management import PositionManagementDecision, PositionManagementEngine
from .position_risk import PositionRiskInput
from .position_lifecycle import PositionLifecycleManager
from .risk_engine import RiskCheck, RiskEngine, _dt
from .risk_snapshot import RiskSnapshotBuilder
from .trend_management import TrendManagementEngine, TrendManagementInput
from .winner_expansion import (
    MilestoneIdentity,
    WinnerExpansionEngine,
    WinnerExpansionInput,
    WinnerExpansionStore,
)
from .position_sizing import effective_notional_policy, notional_from_stop_risk
from .position_sizing import validate_stop_evidence
from .order_state import logical_action_key, stable_client_order_id
from .formula_versions import (
    ACCOUNTING_VERSION,
    EVIDENCE_VERSION,
    PHASE3_DECISION_VERSION,
    RISK_DECISION_VERSION,
    SIZING_POLICY_VERSION,
    STOP_POLICY_VERSION,
)
from .quotes import (
    bounded_marketable_limit,
    implementation_shortfall_bps,
    validate_quote_payload,
    validated_quote,
)
from .reconciliation import BrokerReconciler
from .runtime_guards import WallClockTimeout, wall_clock_timeout
from .strategy_rule_based import evaluate_symbol
from .strategy_rule_based import STRATEGY_VERSION
from .telegram_bot import TelegramBot
from .utils import PROJECT_ROOT, iso_now, json_dumps, format_proposal_message, translate_reason, format_sgt


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if not isinstance(obj, dict) else obj.get(name, default)


def _hydrate_proposal_row(row: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Hydrate a proposal without letting nullable additive columns erase payload provenance."""
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    authoritative = {key: value for key, value in row.items() if value is not None}
    return {**payload, **authoritative, **overrides}


def _parse_datetime(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _validated_authoritative_stop(
    position_state: dict[str, Any],
    winner_config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[float, bool]:
    authoritative_stop = position_state.get("authoritative_protective_stop")
    stop_as_of_value = position_state.get("protective_stop_as_of")
    if bool(winner_config.get("require_authoritative_current_stop", True)):
        if authoritative_stop is None or float(authoritative_stop) <= 0:
            raise ValueError("authoritative protective stop is absent")
        if not stop_as_of_value:
            raise ValueError("authoritative protective stop timestamp is absent")
        try:
            stop_age = ((now or datetime.now(UTC)) - _parse_datetime(str(stop_as_of_value))).total_seconds()
        except (TypeError, ValueError) as exc:
            raise ValueError("authoritative protective stop timestamp is invalid") from exc
        freshness_seconds = float(winner_config.get("stop_freshness_seconds", 300.0))
        if stop_age < -60.0 or stop_age > freshness_seconds:
            raise ValueError("authoritative protective stop is stale")
        if not position_state.get("protective_stop_source") or not position_state.get("protective_stop_formula_version"):
            raise ValueError("authoritative protective stop provenance is incomplete")
        if int(position_state.get("protective_stop_sequence") or 0) < 1:
            raise ValueError("authoritative protective stop sequence is absent")
        return float(authoritative_stop), True
    stops = [
        float(value)
        for value in (
            position_state.get("initial_stop_price"),
            position_state.get("trailing_stop_price"),
            authoritative_stop,
        )
        if value is not None and float(value) > 0
    ]
    if not stops:
        raise ValueError("protective stop is absent")
    return max(stops), bool(stop_as_of_value)


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


def _proposal_action_for_signal(signal_action: str | None, signal_side: str | None, is_add: bool) -> str:
    """Encode the final signal action without letting stale add metadata win."""
    if str(signal_action or "").upper() == "EXIT" and str(signal_side or "").lower() == "sell":
        return "exit"
    return "add" if is_add else "entry"


MARKET_PHASE_PRE = "pre_market"
MARKET_PHASE_REGULAR = "regular_market"
MARKET_PHASE_REGULAR_CATCH_UP = "regular_market_catch_up"
MARKET_PHASE_POST = "post_market"
MARKET_PHASE_WEEKEND = "market_closed_weekend"
MARKET_PHASE_HOLIDAY = "market_closed_holiday"
MARKET_PHASE_NON_TRADING = "market_closed_non_trading_day"
MARKET_PHASE_CATCH_UP = "catch_up"
MARKET_PHASE_UNKNOWN_CLOSED = "unknown_market_closed"
MARKET_CLOSED_STATUS_PHASES = {
    MARKET_PHASE_PRE,
    MARKET_PHASE_POST,
    MARKET_PHASE_WEEKEND,
    MARKET_PHASE_HOLIDAY,
    MARKET_PHASE_NON_TRADING,
    MARKET_PHASE_CATCH_UP,
    MARKET_PHASE_UNKNOWN_CLOSED,
}


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
        self._phase1_bar_cache: dict[str, Any] = {}
        self._phase4_allocation_cache: dict[str, Any] | None = None
        self._strategy_policy_map: dict[str, Any] | None = None
        self._strategy_registry_snapshot_id: str | None = None
        self._current_cycle_exit_blocker: dict[str, Any] | None = None
        self._auto_block_audited = False
        self.listener_started_at = time.time()
        if self.storage is not None:
            self._recover_local_workflows()

    def _recover_local_workflows(self) -> dict[str, int]:
        """Idempotently surface unfinished local work without submitting orders."""
        recovery = DurableExecutionStore(self.storage).recovery_sweep()

        def load_local_proposal(proposal_id: str) -> dict[str, Any] | None:
            rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,))
            if not rows:
                return None
            row = rows[0]
            try:
                payload = json.loads(row.get("payload") or "{}")
            except (TypeError, ValueError):
                payload = {}
            return _hydrate_proposal_row(
                row, proposal_id=proposal_id, source_id=proposal_id, trading_mode="paper"
            )

        def recover_validation(workflow: dict[str, Any], proposal: dict[str, Any] | None):
            if proposal is None:
                return "manual_review", None, "proposal record unavailable during restart recovery"
            expires = _dt(proposal.get("expires_at"))
            if str(proposal.get("status")) in {"expired", "rejected", "superseded"} or (expires and expires <= datetime.now(UTC)):
                return "blocked", None, "proposal expired or became ineligible before recovery"
            if proposal.get("relationship_type") in {"rotation_exit", "rotation_entry"}:
                # The rotation coordinator owns fresh group/dependency
                # revalidation and resumes the already-consumed derived
                # approval.  Generic recovery must neither submit it nor turn
                # the grouped approval into an unrecoverable manual-review row.
                return "retry", None, "rotation coordinator owns grouped recovery"
            # A crash before final validation lacks a fresh broker/account proof.
            # Surface it explicitly instead of silently reviving or stranding it.
            return "manual_review", None, "fresh final broker validation cannot be reconstructed automatically"

        def recover_action_authority(
            workflow: dict[str, Any], proposal: dict[str, Any] | None
        ) -> tuple[str, dict[str, Any] | None, str | None]:
            if proposal is None:
                return "blocked", None, "proposal record unavailable during recovery action"
            relationship = str(proposal.get("relationship_type") or "")
            if relationship not in {"rotation_exit", "rotation_entry"}:
                return "approved", proposal, "ordinary workflow retains its existing recovery authority"
            from .rotation_coordinator import RotationCoordinator, RotationState, TERMINAL_STATES

            group_id = str(
                proposal.get("rotation_group_id")
                or proposal.get("relationship_group_id")
                or ""
            )
            coordinator = RotationCoordinator(
                self.storage, config_hash=self.config.get("effective_config_hash")
            )
            try:
                group = coordinator.get_group(group_id)
            except KeyError:
                return "blocked", proposal, "rotation group disappeared before recovery action"
            now = datetime.now(UTC)
            if RotationState(group["state"]) in TERMINAL_STATES:
                return "blocked", proposal, "rotation group is terminal"
            if _parse_datetime(group["expires_at"]) <= now or self._proposal_or_candidate_expired(proposal):
                return "blocked", proposal, "rotation group or proposal expired before recovery action"
            if relationship == "rotation_entry":
                if RotationState(group["state"]) not in {
                    RotationState.ENTRY_REVALIDATING, RotationState.ENTRY_RESERVED,
                } or not coordinator.approval_is_current(group_id):
                    return "blocked", proposal, "rotation entry dependency or grouped approval is no longer current"
            else:
                if RotationState(group["state"]) not in {
                    RotationState.APPROVED_EXIT_PENDING,
                    RotationState.EXIT_SUBMITTED,
                    RotationState.EXIT_PARTIALLY_FILLED,
                }:
                    return "blocked", proposal, "rotation exit group is no longer eligible for first submission"
                approvals = self.storage.fetch_all(
                    """SELECT approval_id,status FROM rotation_group_approvals
                       WHERE group_id=? ORDER BY created_at DESC LIMIT 1""",
                    (group_id,),
                )
                if (
                    not approvals
                    or approvals[0].get("approval_id") != group.get("approval_id")
                    or approvals[0].get("status") not in {"active", "exit_submitted"}
                ):
                    return "blocked", proposal, "rotation exit grouped approval is no longer current"
            if self.broker is None:
                return "retry", proposal, "rotation recovery is deferred until the broker client is available"
            return "approved", proposal, "current rotation dependency and expiry revalidated"

        def recover_submission(workflow: dict[str, Any], intent: dict[str, Any]) -> str:
            proposal = load_local_proposal(workflow["proposal_id"])
            if proposal is None:
                return "terminal"
            authority, proposal, _reason = recover_action_authority(workflow, proposal)
            if authority != "approved" or proposal is None:
                current_intent = DurableExecutionStore(self.storage).get_intent(str(intent["id"]))
                if str(current_intent.get("state")) in {"created", "reserved"}:
                    from .order_state import OrderState

                    DurableExecutionStore(self.storage).transition(
                        str(intent["id"]), OrderState.EXPIRED,
                        event_type="rotation_recovery_authority_expired",
                        safe_summary="rotation dependency failed the immediate pre-submission check",
                        expected_state=OrderState(str(current_intent["state"])),
                    )
                return "terminal"
            if self.broker is None:
                return "unknown"
            executable = {
                **proposal,
                "status": "approved",
                "symbol": intent["symbol"],
                "side": intent["side"],
                "action": intent["intended_action"],
                "qty": float(intent["requested_quantity"]),
                "notional": intent.get("requested_notional"),
                "latest_price": float(intent["reference_price"]),
                "stop_price": intent.get("intended_stop_price"),
                "trading_mode": "paper",
            }
            context = self._portfolio_context(executable, approval_valid=True)
            result = Executor(self.broker, self._risk_engine(intent.get("proposal_id"), "recovery_final"), self.storage, self.run_id).execute(
                executable,
                context,
                source_type=str(intent.get("source_type") or "telegram"),
                approval_id=str(workflow["approval_id"]),
            )
            if result.status == "unknown":
                return "unknown"
            return "submitted" if result.submitted else "terminal"

        def recover_lookup(_workflow: dict[str, Any], intent: dict[str, Any] | None) -> str:
            if self.broker is None or intent is None:
                return "unknown"
            BrokerReconciler(self.broker, self.storage, self.run_id).reconcile()
            current = DurableExecutionStore(self.storage).get_intent(intent["id"])
            if current["state"] in {"submitted", "partially_filled", "cancel_pending"}:
                return "submitted"
            if current["state"] in {"filled", "cancelled", "rejected", "expired"}:
                return "terminal"
            return "unknown"

        local_recovery = ApprovalWorkflowStore(self.storage).recover(
            owner_token=f"service:{self.run_id}:{uuid.uuid4()}",
            proposal_loader=load_local_proposal,
            run_id=self.run_id,
            validator=recover_validation,
            action_validator=recover_action_authority,
            submitter=recover_submission,
            lookup_reconciler=recover_lookup,
            max_items=100,
        )
        consumed_without_intent = self.storage.fetch_all(
            """SELECT a.id,a.proposal_id FROM approvals a
               LEFT JOIN order_intents i ON i.approval_id=a.id
               LEFT JOIN approval_workflows w ON w.approval_id=a.id
               WHERE a.consumed_at IS NOT NULL AND i.id IS NULL AND w.id IS NULL"""
        )
        now = iso_now()
        for row in consumed_without_intent:
            self.storage.execute(
                """INSERT INTO approval_workflows(
                       id,approval_id,proposal_id,state,created_at,updated_at,manual_review_reason)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(approval_id) DO UPDATE SET
                   state=CASE WHEN approval_workflows.intent_id IS NULL THEN 'manual_review' ELSE approval_workflows.state END,
                   updated_at=excluded.updated_at,
                   manual_review_reason=CASE WHEN approval_workflows.intent_id IS NULL THEN excluded.manual_review_reason ELSE approval_workflows.manual_review_reason END,
                   version=approval_workflows.version+1""",
                (str(uuid.uuid4()), row["id"], row["proposal_id"], "manual_review", now, now, "consumed approval has no durable order intent"),
            )
        received_updates = int(self.storage.fetch_all("SELECT COUNT(*) n FROM telegram_updates WHERE processing_state='received'")[0]["n"])
        incomplete_approval_workflows = int(
            self.storage.fetch_all(
                """SELECT COUNT(*) n FROM approval_workflows
                   WHERE intent_id IS NULL AND state NOT IN ('blocked','terminal','manual_review')"""
            )[0]["n"]
        )
        detail = {
            "received_unprocessed_updates": received_updates,
            "incomplete_approval_workflows": incomplete_approval_workflows,
            "approvals_without_intents": recovery.approvals_without_intents,
            "intents_awaiting_submission": recovery.intents_awaiting_submission,
            "intents_awaiting_reconciliation": recovery.intents_awaiting_reconciliation,
            "stale_submitted": recovery.stale_submitted,
            "terminal_with_reservations": recovery.terminal_with_reservations,
            "approval_intents_created": local_recovery.intent_created,
            "approval_existing_intents_linked": local_recovery.existing_intent_linked,
            "approval_external_ambiguity": local_recovery.external_ambiguity,
            "approval_recovery_retryable_failures": local_recovery.failed_retryable,
        }
        state = "degraded" if any(detail.values()) else "healthy"
        self.storage.execute(
            """INSERT INTO health_heartbeats(component,state,attempted_at,completed_at,successful_at,detail,updated_at)
               VALUES('recovery',?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET
               state=excluded.state,attempted_at=excluded.attempted_at,completed_at=excluded.completed_at,
               successful_at=excluded.successful_at,detail=excluded.detail,updated_at=excluded.updated_at""",
            (state, now, now, now if state == "healthy" else None, json_dumps(detail), now),
        )
        if any(detail.values()):
            self.storage.audit(self.run_id, "execution_recovery_work_detected", detail)
        return detail

    def _risk_engine(self, proposal_id: str, stage: str) -> RiskEngine:
        risk_config = self.config
        if self.config.get("strategy_execution_registry") and (
            self._strategy_registry_snapshot_id is not None
            or self._strategy_policy_map is not None
            or stage == "final"
        ):
            snapshot_id = self._ensure_strategy_registry_snapshot()
            authorized_rows = self.storage.fetch_all(
                """SELECT strategy_version FROM strategy_registry_decisions
                   WHERE snapshot_id=? AND authorized=1 ORDER BY strategy_version""",
                (snapshot_id,),
            ) if snapshot_id else []
            # A per-call config view prevents the legacy compatibility list
            # from granting production execution authority.  An empty current
            # registry result is intentionally fail closed.
            risk_config = {
                **self.config,
                "runtime_authorized_strategy_versions": [
                    str(row["strategy_version"]) for row in authorized_rows
                ],
                "runtime_strategy_registry_snapshot_id": snapshot_id,
            }
        return RiskEngine(
            risk_config,
            lambda c: self.storage.record_check(
                self.run_id, c.name, c.passed, c.reason, proposal_id, stage,
                config_hash=self.config.get("effective_config_hash"),
            ),
        )

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
            "loss_metrics": None,
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
                losses = dict(losses or {})
                state["loss_metrics"] = losses
            except Exception:
                # Daily equity comparison is still authoritative when present.
                equity = _value(account, "equity")
                last_equity = _value(account, "last_equity")
                if equity is not None and last_equity is not None:
                    state["loss_metrics"] = {
                        "daily_loss_dollars": max(0.0, float(last_equity) - float(equity)),
                        "weekly_loss_dollars": None,
                        "reference_equity": float(last_equity),
                        "daily_loss_confidence": "verified",
                        "weekly_loss_confidence": "unavailable",
                        "provenance": "alpaca_account_snapshot_fallback",
                        "metrics_version": LOSS_METRICS_VERSION,
                    }

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

    def _exit_blocker_context(
        self,
        broker_orders: list[Any] | None = None,
        *,
        positions: list[Any] | None = None,
        current_cycle_exits: list[dict[str, Any]] | None = None,
        validation_at: datetime | None = None,
        exclude_reconciled_rotation_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one current, provenance-bearing exit-first blocker.

        Historical market-memory flags and terminal proposal rows are never
        blockers. Every persistent source is checked against its own lifecycle
        and expiry, and current-cycle decisions are accepted only when the
        cycle still has the corresponding position.
        """
        now = validation_at or datetime.now(UTC)
        now_iso = now.isoformat()
        stale_sources: list[dict[str, Any]] = []
        excluded_client_order_ids: set[str] = set()
        excluded_broker_order_ids: set[str] = set()
        if exclude_reconciled_rotation_group_id:
            excluded_rows = self.storage.fetch_all(
                """SELECT client_order_id,broker_order_id FROM order_intents
                   WHERE relationship_group_id=? AND relationship_type='rotation_exit'""",
                (exclude_reconciled_rotation_group_id,),
            )
            excluded_client_order_ids = {
                str(row.get("client_order_id")) for row in excluded_rows if row.get("client_order_id")
            }
            excluded_broker_order_ids = {
                str(row.get("broker_order_id")) for row in excluded_rows if row.get("broker_order_id")
            }

        positions_known = positions is not None
        if positions is None:
            try:
                positions = list(self.broker.get_positions()) if self.broker is not None else []
                positions_known = True
            except Exception:
                positions = []
                positions_known = False
        position_symbols = {
            str(_value(position, "symbol", "")).upper()
            for position in positions or []
            if abs(float(_value(position, "qty", 0.0) or 0.0)) > 1e-12
        }

        def has_position(symbol: str) -> bool:
            return not positions_known or symbol.upper() in position_symbols

        def stale(source: dict[str, Any], reason: str) -> None:
            display_reason = (
                f"stale {source.get('symbol') or ''} exit flag ignored"
                if source.get("source_type") == "historical_sell_proposal"
                else f"stale {source.get('symbol') or ''} exit blocker ignored: {reason}"
            ).strip()
            source = {
                "active": False,
                "stale": True,
                "stale_reason": reason,
                "latest_validation_at": now_iso,
                "reason": display_reason,
                **source,
            }
            stale_sources.append(source)
            self.storage.audit(self.run_id, "exit_blocker_ignored_stale", self._exit_blocker_audit_detail(source, "stale", reason))

        def active(source: dict[str, Any]) -> dict[str, Any]:
            source = {
                "active": True,
                "stale": False,
                "latest_validation_at": now_iso,
                "source": source.get("source_type"),
                **source,
            }
            self.storage.audit(self.run_id, "exit_blocker_validated", self._exit_blocker_audit_detail(source, "active", source.get("validation_reason")))
            return source

        # Broker state is the strongest current source. A sell order remains
        # blocking until reconciliation proves it terminal; elapsed wall-clock
        # time is intentionally irrelevant here.
        for order in broker_orders or []:
            side = str(_value(order, "side", "")).lower()
            status = str(_value(order, "status", "open"))
            if side != "sell" or status.lower() in {"filled", "canceled", "cancelled", "expired", "rejected"}:
                continue
            if (
                str(_value(order, "client_order_id", "")) in excluded_client_order_ids
                or str(_value(order, "id", "")) in excluded_broker_order_ids
            ):
                continue
            symbol = str(_value(order, "symbol", "")).upper()
            return active({
                "source_type": "broker_open_order",
                "source_id": str(_value(order, "id", "") or _value(order, "client_order_id", "") or symbol),
                "symbol": symbol,
                "status": status,
                "created_at": _value(order, "created_at"),
                "expires_at": None,
                "reason": f"{symbol} SELL order open",
                "validation_reason": "broker reports a non-terminal sell order",
            })

        active_intent_states = (
            "created", "reserved", "submitting", "submitted", "partially_filled",
            "cancel_pending", "unknown", "reconciliation_required",
        )
        placeholders = ",".join("?" for _ in active_intent_states)
        intent_rows = self.storage.fetch_all(
            f"""
            SELECT i.*, r.id AS reservation_id, r.state AS reservation_state
            FROM order_intents i
            LEFT JOIN risk_reservations r ON r.intent_id=i.id AND r.state='active'
            WHERE lower(i.side)='sell' AND i.state IN ({placeholders})
              AND (? IS NULL OR COALESCE(i.relationship_group_id,'')<>?)
            ORDER BY datetime(i.updated_at) DESC, datetime(i.created_at) DESC
            LIMIT 10
            """,
            (*active_intent_states, exclude_reconciled_rotation_group_id, exclude_reconciled_rotation_group_id),
        )
        if intent_rows:
            row = intent_rows[0]
            symbol = str(row.get("symbol") or "").upper()
            source_type = "active_sell_reservation" if row.get("reservation_id") else "sell_order_intent"
            source_id = str(row.get("reservation_id") or row.get("id"))
            status = str(row.get("reservation_state") or row.get("state"))
            return active({
                "source_type": source_type,
                "source_id": source_id,
                "symbol": symbol,
                "status": status,
                "created_at": row.get("created_at"),
                "expires_at": None,
                "position_lifecycle_id": row.get("position_lifecycle_id"),
                "order_intent_id": row.get("id"),
                "reason": f"{symbol} sell order intent {status}",
                "validation_reason": "sell intent or reservation is unresolved",
            })

        # A pending/approved sell proposal is meaningful only while its
        # position lifecycle is still represented by a current broker holding.
        proposal_rows = self.storage.fetch_all(
            """
            SELECT trade_proposals.id, trade_proposals.symbol, trade_proposals.status, trade_proposals.created_at, trade_proposals.expires_at, trade_proposals.emergency_exit_triggered,
                   emergency_exit_score, emergency_exit_trigger_reason, exit_trigger_reason, trade_proposals.payload,
                   o.status AS linked_order_status, i.state AS linked_intent_state
            FROM trade_proposals
            LEFT JOIN orders o ON o.proposal_id=trade_proposals.id
            LEFT JOIN order_intents i ON i.proposal_id=trade_proposals.id
            WHERE lower(trade_proposals.side)='sell' AND trade_proposals.status IN ('pending','approved')
              AND (expires_at IS NULL OR expires_at>?)
              AND (? IS NULL OR COALESCE(trade_proposals.rotation_group_id,'')<>?)
            ORDER BY datetime(trade_proposals.created_at) DESC
            """,
            (now_iso, exclude_reconciled_rotation_group_id, exclude_reconciled_rotation_group_id),
        )
        for row in proposal_rows:
            symbol = str(row.get("symbol") or "").upper()
            lifecycle_id = PositionLifecycleManager(self.storage).active_id(symbol)
            source = {
                "source_type": "active_sell_proposal",
                "source_id": str(row["id"]),
                "symbol": symbol,
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "emergency_exit_triggered": row.get("emergency_exit_triggered"),
                "position_lifecycle_id": lifecycle_id,
            }
            linked_order_status = str(row.get("linked_order_status") or "").lower()
            linked_intent_state = str(row.get("linked_intent_state") or "").lower()
            if linked_order_status in {"filled", "canceled", "cancelled", "expired", "rejected"} or linked_intent_state in {"filled", "cancelled", "rejected", "expired"}:
                stale(source, f"linked sell order or intent is {linked_order_status or linked_intent_state}")
                continue
            if not has_position(symbol):
                stale(source, "position no longer exists")
                continue
            reason = f"{symbol} EXIT proposal {row.get('status')}"
            if int(row.get("emergency_exit_triggered") or 0) == 1:
                reason = f"{symbol} emergency exit review active"
            return active({**source, "reason": reason, "validation_reason": "proposal is active, unexpired, and position exists"})

        # Batch candidates are independently checked against both candidate and
        # batch expiry, plus the linked proposal when that row exists.
        batch_rows = self.storage.fetch_all(
            """
            SELECT c.id, c.proposal_id, c.candidate_symbol, c.candidate_action, c.candidate_side,
                   c.candidate_status, c.created_at, c.expires_at, b.id AS batch_id,
                   b.status AS batch_status, b.expires_at AS batch_expires_at,
                   p.status AS proposal_status, o.status AS linked_order_status,
                   i.state AS linked_intent_state
            FROM proposal_batch_candidates c
            JOIN proposal_batches b ON b.id=c.batch_id
            LEFT JOIN trade_proposals p ON p.id=c.proposal_id
            LEFT JOIN orders o ON o.proposal_id=p.id
            LEFT JOIN order_intents i ON i.proposal_id=p.id
            WHERE c.candidate_status='pending'
              AND b.status IN ('pending','partially_approved')
              AND c.expires_at>?
              AND b.expires_at>?
              AND (lower(c.candidate_side)='sell' OR upper(c.candidate_action) IN ('SELL','EXIT'))
              AND (p.id IS NULL OR p.status IN ('pending','approved'))
            ORDER BY datetime(c.created_at) DESC
            """,
            (now_iso, now_iso),
        )
        for row in batch_rows:
            symbol = str(row.get("candidate_symbol") or "").upper()
            lifecycle_id = PositionLifecycleManager(self.storage).active_id(symbol)
            source = {
                "source_type": "active_sell_batch_candidate",
                "source_id": str(row["id"]),
                "symbol": symbol,
                "status": row.get("candidate_status"),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "batch_id": row.get("batch_id"),
                "batch_status": row.get("batch_status"),
                "batch_expires_at": row.get("batch_expires_at"),
                "proposal_id": row.get("proposal_id"),
                "position_lifecycle_id": lifecycle_id,
            }
            linked_order_status = str(row.get("linked_order_status") or "").lower()
            linked_intent_state = str(row.get("linked_intent_state") or "").lower()
            if linked_order_status in {"filled", "canceled", "cancelled", "expired", "rejected"} or linked_intent_state in {"filled", "cancelled", "rejected", "expired"}:
                stale(source, f"linked sell order or intent is {linked_order_status or linked_intent_state}")
                continue
            if not has_position(symbol):
                stale(source, "position no longer exists")
                continue
            return active({
                **source,
                "reason": f"{symbol} EXIT batch candidate {row.get('candidate_status')}",
                "validation_reason": "batch candidate and batch are pending, unexpired, and position exists",
            })

        historical_rows = self.storage.fetch_all(
            """
            SELECT id, symbol, status, created_at, expires_at
            FROM trade_proposals
            WHERE lower(side)='sell'
              AND status IN ('pending','approved','submitted','filled','blocked','expired','rejected','superseded','stale_resolved')
            ORDER BY datetime(created_at) DESC
            LIMIT 1
            """
        )
        if historical_rows:
            row = historical_rows[0]
            source = {
                "source_type": "historical_sell_proposal",
                "source_id": str(row["id"]),
                "symbol": str(row.get("symbol") or "").upper(),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
            }
            stale(source, "source is terminal, expired, superseded, or no longer reproduced")

        # Only a decision produced by this scan is allowed to act as a
        # proposal-less blocker. Position-management decisions from prior runs
        # are historical evidence, not current intent.
        for result in current_cycle_exits or []:
            signal = result.get("signal")
            if getattr(signal, "action", None) != "EXIT" or getattr(signal, "side", None) != "sell":
                continue
            symbol = str(result.get("symbol") or "").upper()
            if not result.get("has_position") or not has_position(symbol):
                continue
            decision = result.get("position_management_decision") or {}
            decision_type = decision.get("decision_type") if isinstance(decision, dict) else None
            source_type = "current_position_management_decision" if decision.get("is_actionable") and decision.get("action") == "sell" else "current_cycle_exit_signal"
            source_id = result.get("position_management_decision_id") if source_type == "current_position_management_decision" else result.get("signal_id")
            status = decision_type or "actionable"
            reason = (
                f"fresh {symbol} {decision_type} decision has priority"
                if source_type == "current_position_management_decision"
                else f"fresh {symbol} EXIT signal has priority"
            )
            return active({
                "source_type": source_type,
                "source_id": str(source_id or symbol),
                "symbol": symbol,
                "status": status,
                "created_at": result.get("cycle_created_at") or now_iso,
                "expires_at": result.get("expiry").isoformat() if isinstance(result.get("expiry"), datetime) else result.get("expiry"),
                "position_lifecycle_id": result.get("position_lifecycle_id"),
                "reason": reason,
                "validation_reason": "fresh current-cycle exit signal reproduced from current position and market data",
            })

        if stale_sources:
            return stale_sources[0]
        return {
            "active": False, "symbol": None, "reason": None, "status": None,
            "source": None, "source_type": None, "source_id": None,
            "created_at": None, "expires_at": None, "latest_validation_at": now_iso,
            "stale": False,
        }

    def _exit_blocker_audit_detail(self, blocker: dict[str, Any], classification: str, reason: str | None) -> dict[str, Any]:
        return {
            "source_type": blocker.get("source_type") or blocker.get("source"),
            "source_id": blocker.get("source_id"),
            "symbol": blocker.get("symbol"),
            "status": blocker.get("status"),
            "created_at": blocker.get("created_at"),
            "expires_at": blocker.get("expires_at"),
            "position_lifecycle_id": blocker.get("position_lifecycle_id"),
            "latest_validation_at": blocker.get("latest_validation_at"),
            "classification": classification,
            "reason": reason or blocker.get("reason"),
        }

    def _exit_blocker_display_reason(self, blocker: dict[str, Any]) -> str:
        """Stable digest wording containing the current source and status."""
        if not blocker or not blocker.get("active"):
            return (blocker.get("reason") if blocker else None) or "no current exit blocker"
        symbol = str(blocker.get("symbol") or "EXIT").upper()
        source_type = blocker.get("source_type") or blocker.get("source")
        status = str(blocker.get("status") or "active")
        expiry = blocker.get("expires_at")
        until = ""
        if expiry:
            try:
                until = f" until {_format_sgt_time(_parse_datetime(expiry))} SGT"
            except Exception:
                pass
        if source_type == "broker_open_order":
            return f"{symbol} sell order remains open"
        if source_type == "active_sell_proposal":
            if int(blocker.get("emergency_exit_triggered") or 0) == 1:
                return f"{symbol} emergency exit review active ({status}){until}"
            return f"{symbol} exit proposal {status}{until}"
        if source_type == "active_sell_batch_candidate":
            return f"{symbol} sell batch candidate {status}{until}"
        if source_type == "sell_order_intent":
            return f"{symbol} sell order intent {status}"
        if source_type == "active_sell_reservation":
            return f"{symbol} sell reservation {status}"
        return blocker.get("reason") or f"fresh {symbol} EXIT decision has priority"

    def _digest_exit_blocker_context(
        self,
        broker_orders: list[Any] | None,
        positions: list[Any] | None,
        validation_at: datetime,
    ) -> dict[str, Any]:
        """Revalidate persistent sources and retain only this scan's fresh decision."""
        persistent = self._exit_blocker_context(
            broker_orders,
            positions=positions,
            validation_at=validation_at,
        )
        if persistent.get("active"):
            return persistent

        cached = dict(self._current_cycle_exit_blocker or {})
        if not cached.get("active") or cached.get("run_id") != self.run_id:
            return persistent
        if cached.get("source_type") not in {
            "current_position_management_decision", "current_cycle_exit_signal",
        }:
            return persistent

        symbol = str(cached.get("symbol") or "").upper()
        if positions is not None:
            position_symbols = {
                str(_value(position, "symbol", "")).upper()
                for position in positions
                if abs(float(_value(position, "qty", 0.0) or 0.0)) > 1e-12
            }
            if symbol not in position_symbols:
                detail = self._exit_blocker_audit_detail(
                    {**cached, "latest_validation_at": validation_at.isoformat()},
                    "stale",
                    "position no longer exists at digest validation",
                )
                self.storage.audit(self.run_id, "exit_blocker_ignored_stale", detail)
                return persistent

        expires_at = cached.get("expires_at")
        if expires_at:
            try:
                if _parse_datetime(expires_at) <= validation_at:
                    detail = self._exit_blocker_audit_detail(
                        {**cached, "latest_validation_at": validation_at.isoformat()},
                        "stale",
                        "current-cycle decision expired before digest validation",
                    )
                    self.storage.audit(self.run_id, "exit_blocker_ignored_stale", detail)
                    return persistent
            except (TypeError, ValueError):
                return persistent

        validated = {
            **cached,
            "latest_validation_at": validation_at.isoformat(),
            "validation_reason": "same-cycle decision still has its broker position and remains unexpired",
        }
        self.storage.audit(
            self.run_id,
            "exit_blocker_validated",
            self._exit_blocker_audit_detail(
                validated,
                "active",
                validated["validation_reason"],
            ),
        )
        return validated

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

    def _runtime_orchestration_cfg(self) -> dict[str, Any]:
        return self.config.get("runtime_orchestration") or self.config.get("dynamic_universe", {}).get("runtime_orchestration", {})

    def _run_dynamic_universe_due(
        self,
        run_types: list[str] | None = None,
        skip_run_types: list[str] | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[dict[str, Any]]:
        engine = self._dynamic_universe_engine()
        if not engine:
            return []
        if getattr(engine, "provider", None) is not None:
            engine.provider.set_run_deadline(deadline_monotonic)
        run_types = run_types or ["daily_deep_research", "intraday_light_refresh", "post_market_review", "weekly_cleanup"]
        if self._dynamic_universe_event_refresh_due():
            run_types.append("event_triggered_refresh")
        if skip_run_types:
            skip_set = set(skip_run_types)
            run_types = [run_type for run_type in run_types if run_type not in skip_set]
        try:
            return engine.run_due(run_types=run_types)
        except WallClockTimeout:
            raise
        except Exception as exc:
            self.storage.audit(self.run_id, "dynamic_universe_due_failed", {"error": type(exc).__name__})
            return []

    def cleanup_stale_research_runs(self, timeout_seconds: int | None = None, reason: str = "stale_running_timeout", run_id: str | None = None) -> int:
        cfg = self._runtime_orchestration_cfg()
        timeout = int(cfg.get("stale_research_timeout_seconds", 900) if timeout_seconds is None else timeout_seconds)
        cutoff = (datetime.now(UTC) - timedelta(seconds=timeout)).isoformat()
        if run_id:
            rows = self.storage.fetch_all(
                "SELECT * FROM universe_research_runs WHERE status='running' AND run_id=? AND datetime(started_at) <= datetime(?)",
                (run_id, cutoff),
            )
        else:
            rows = self.storage.fetch_all(
                "SELECT * FROM universe_research_runs WHERE status='running' AND datetime(started_at) <= datetime(?)",
                (cutoff,),
            )
        for row in rows:
            detail = {}
            try:
                detail = json.loads(row.get("detail") or "{}")
            except Exception:
                detail = {}
            detail.update({"reason": reason, "timeout_seconds": timeout})
            self.storage.execute(
                "UPDATE universe_research_runs SET status=?, ended_at=?, detail=? WHERE id=?",
                ("timeout", iso_now(), json_dumps(detail), row["id"]),
            )
            self.storage.audit(
                row.get("run_id") or self.run_id,
                "research_timed_out",
                {
                    "research_run_id": row["id"],
                    "research_type": row.get("research_type"),
                    "started_at": row.get("started_at"),
                    "reason": reason,
                    "timeout_seconds": timeout,
                },
            )
        return len(rows)

    def run_dynamic_universe_research_only(
        self,
        timeout_seconds: int | None = None,
        run_types: list[str] | None = None,
        skip_run_types: list[str] | None = None,
        label: str = "dynamic_universe_research",
    ) -> list[dict[str, Any]]:
        cfg = self._runtime_orchestration_cfg()
        timeout = int(timeout_seconds or cfg.get("research_wall_clock_timeout_seconds", 240))
        self.cleanup_stale_research_runs()
        detail = {"timeout_seconds": timeout, "run_types": run_types, "skip_run_types": skip_run_types}
        self.storage.audit(self.run_id, "research_started", detail)
        try:
            deadline = time.monotonic() + timeout
            with wall_clock_timeout(timeout, label):
                results = self._run_dynamic_universe_due(
                    run_types=run_types,
                    skip_run_types=skip_run_types,
                    deadline_monotonic=deadline,
                )
        except WallClockTimeout:
            timed_out = self.cleanup_stale_research_runs(timeout_seconds=0, reason="research_wall_clock_timeout", run_id=self.run_id)
            self.storage.audit(
                self.run_id,
                "research_timed_out",
                {**detail, "reason": "research_wall_clock_timeout", "timed_out_rows": timed_out},
            )
            return [{"status": "timeout", "run_type": "dynamic_universe", "reason": "research_wall_clock_timeout"}]
        statuses = sorted({str(r.get("status") or "unknown") for r in results}) if results else ["not_due"]
        self.storage.audit(self.run_id, "research_completed", {**detail, "statuses": statuses, "results": len(results)})
        return results

    def _sleep_mode_blocks_approval(self, proposal: dict[str, Any]) -> bool:
        side = str(proposal.get("side") or proposal.get("candidate_side") or "").lower()
        action = str(proposal.get("action") or proposal.get("candidate_action") or "").lower()
        risk_reducing = side == "sell" or action in {"sell", "exit"}
        buy_or_add = side == "buy" or action in {"buy", "add", "entry"}
        return self._sleep_mode_active() and buy_or_add and not risk_reducing

    def _position_management_state(self, symbol: str) -> dict[str, Any] | None:
        lifecycle_id = PositionLifecycleManager(self.storage).active_id(symbol)
        if lifecycle_id:
            rows = self.storage.fetch_all(
                "SELECT * FROM position_management_state WHERE symbol=? AND position_lifecycle_id=?",
                (symbol.upper(), lifecycle_id),
            )
        else:
            rows = self.storage.fetch_all("SELECT * FROM position_management_state WHERE symbol=?", (symbol.upper(),))
        return rows[0] if rows else None

    def _initial_risk_seed_for_position(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        lifecycle_id = PositionLifecycleManager(self.storage).active_id(symbol)

        def unavailable(reason: str) -> dict[str, Any]:
            return {
                "position_lifecycle_id": lifecycle_id,
                "entry_fill_id": None,
                "entry_order_intent_id": None,
                "initial_stop_price": None,
                "initial_risk_per_share": None,
                "initial_risk_pct": None,
                "initial_risk_dollars": None,
                "stop_model": None,
                "stop_source": None,
                "entry_price_for_r": None,
                "risk_model_version": "initial_r_lifecycle_provenance_v1",
                "initial_risk_reconstruction_source": None,
                "initial_risk_formula_version": "initial_r_lifecycle_provenance_v1",
                "initial_risk_evidence_version": EVIDENCE_VERSION,
                "r_multiple_unavailable_reason": reason,
            }

        if not lifecycle_id:
            return unavailable("r_multiple_unavailable_active_position_lifecycle_missing")
        lifecycle_rows = self.storage.fetch_all(
            "SELECT * FROM position_lifecycles WHERE id=? AND symbol=? AND state='active'",
            (lifecycle_id, symbol),
        )
        if len(lifecycle_rows) != 1:
            return unavailable("r_multiple_unavailable_active_position_lifecycle_invalid")
        fill_rows = self.storage.fetch_all(
            """SELECT f.id AS entry_fill_id,
                      i.id AS entry_order_intent_id,i.proposal_id,i.intended_stop_price,
                      i.average_fill_price,i.state,p.payload
               FROM order_intents i
               LEFT JOIN broker_fill_events f ON f.id=(
                   SELECT first_fill.id FROM broker_fill_events first_fill
                   WHERE first_fill.intent_id=i.id AND first_fill.delta_quantity>0
                   ORDER BY first_fill.occurred_at,first_fill.id LIMIT 1
               )
               LEFT JOIN trade_proposals p ON p.id=i.proposal_id
               WHERE i.position_lifecycle_id=? AND i.symbol=? AND i.side='buy'
                 AND i.intended_action='entry' AND i.filled_quantity>0
               ORDER BY i.created_at,i.id LIMIT 1""",
            (lifecycle_id, symbol),
        )
        if not fill_rows:
            return unavailable("r_multiple_unavailable_current_lifecycle_entry_fill_missing")
        fill = fill_rows[0]
        try:
            entry_price = float(fill.get("average_fill_price") or 0.0)
        except (TypeError, ValueError):
            entry_price = 0.0
        if not math.isfinite(entry_price) or entry_price <= 0:
            return unavailable("r_multiple_unavailable_current_lifecycle_entry_price_invalid")
        try:
            payload = json.loads(fill.get("payload") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        stop = fill.get("intended_stop_price")
        source = "linked_order_intent"
        if stop is None:
            stop = payload.get("initial_stop_price", payload.get("stop_price"))
            source = "linked_filled_proposal"
        existing = self._position_management_state(symbol)
        if stop is None and existing and existing.get("position_lifecycle_id") == lifecycle_id:
            stop = existing.get("initial_stop_price")
            source = "lifecycle_initial_protective_stop"
        try:
            stop_price = float(stop) if stop is not None else None
        except (TypeError, ValueError):
            stop_price = None
        if stop_price is None:
            return unavailable("r_multiple_unavailable_current_lifecycle_initial_stop_missing")
        if not math.isfinite(stop_price) or stop_price <= 0 or stop_price >= entry_price:
            return unavailable("r_multiple_unavailable_current_lifecycle_initial_stop_invalid")
        risk_per_share = entry_price - stop_price
        opening_quantity = float(lifecycle_rows[0].get("opening_quantity") or 0.0)
        return {
            "position_lifecycle_id": lifecycle_id,
            "entry_fill_id": fill.get("entry_fill_id"),
            "entry_order_intent_id": fill.get("entry_order_intent_id"),
            "initial_stop_price": stop_price,
            "initial_risk_per_share": risk_per_share,
            "initial_risk_pct": risk_per_share / entry_price * 100.0,
            "initial_risk_dollars": risk_per_share * opening_quantity if opening_quantity > 0 else None,
            "stop_model": payload.get("stop_model", payload.get("stop_model_used")),
            "stop_source": payload.get("stop_source", source),
            "entry_price_for_r": entry_price,
            "risk_model_version": "initial_r_lifecycle_provenance_v1",
            "initial_risk_reconstruction_source": source,
            "initial_risk_formula_version": "initial_r_lifecycle_provenance_v1",
            "initial_risk_evidence_version": EVIDENCE_VERSION,
            "r_multiple_unavailable_reason": None,
        }

    def _initial_stop_for_position(self, symbol: str) -> float | None:
        seed = self._initial_risk_seed_for_position(symbol)
        stop = seed.get("initial_stop_price")
        try:
            return float(stop) if stop is not None else None
        except (TypeError, ValueError):
            return None

    def _record_position_management(self, decision: PositionManagementDecision, now: datetime, proposal_id: str | None = None) -> str:
        symbol = decision.symbol.upper()
        lifecycle_id = PositionLifecycleManager(self.storage).active_id(symbol)
        decision_id = str(uuid.uuid4())
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
                id,symbol,position_lifecycle_id,broker_position_id,avg_entry_price,quantity,highest_price_since_entry,highest_price_seen_at,
                max_unrealized_profit_pct,max_unrealized_profit_seen_at,profit_protection_active,profit_protection_activated_at,
                take_profit_level_1_hit,take_profit_level_2_hit,take_profit_level_3_hit,trailing_stop_price,
                initial_stop_price,initial_risk_per_share,initial_risk_pct,initial_risk_dollars,stop_model,stop_source,
                entry_price_for_r,risk_model_version,r_multiple_unavailable_reason,last_decision_type,last_reason,updated_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                position_lifecycle_id=excluded.position_lifecycle_id,
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
                str(uuid.uuid4()), symbol, lifecycle_id, symbol, decision.avg_entry_price, decision.quantity,
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
            """UPDATE position_management_state SET entry_fill_id=?,entry_order_intent_id=?,
                 initial_risk_reconstruction_source=?,initial_risk_formula_version=?,
                 initial_risk_evidence_version=? WHERE symbol=? AND position_lifecycle_id=?""",
            (
                risk_seed.get("entry_fill_id"),
                risk_seed.get("entry_order_intent_id"),
                risk_seed.get("initial_risk_reconstruction_source"),
                risk_seed.get("initial_risk_formula_version"),
                risk_seed.get("initial_risk_evidence_version"),
                symbol,
                lifecycle_id,
            ),
        )
        self.storage.execute(
            """
            INSERT INTO position_management_decisions(
                id,run_id,symbol,position_lifecycle_id,decision_type,priority,action,reason,current_price,avg_entry_price,quantity,
                unrealized_profit_pct,highest_price_since_entry,max_unrealized_profit_pct,pullback_from_peak_pct,
                drawdown_from_entry_pct,drawdown_from_peak_pct,profit_giveback_ratio,current_r_multiple,
                trailing_stop_price,suggested_sell_fraction,suggested_add_notional,blocking_reasons,is_actionable,
                dip_trap_classification,position_age_days,position_age_cycles,exit_review_needed,created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id, self.run_id, symbol, lifecycle_id, decision.decision_type, decision.priority, decision.action,
                decision.reason, decision.current_price, decision.avg_entry_price, decision.quantity,
                decision.unrealized_profit_pct, decision.highest_price_since_entry, decision.max_unrealized_profit_pct,
                decision.pullback_from_peak_pct, decision.drawdown_from_entry_pct, decision.drawdown_from_peak_pct,
                decision.profit_giveback_ratio, decision.current_r_multiple,
                decision.trailing_stop_price, decision.suggested_sell_fraction, decision.suggested_add_notional,
                "; ".join(decision.blocking_reasons), int(decision.is_actionable), decision.dip_trap_classification,
                decision.position_age_days, decision.position_age_cycles, int(decision.exit_review_needed),
                now.isoformat(), json_dumps(dataclasses.asdict(decision)),
            ),
        )
        self.storage.execute(
            """
            INSERT INTO exit_review_events(
                id,run_id,symbol,review_type,status,reason,drawdown_from_entry_pct,drawdown_from_peak_pct,
                unrealized_pl_pct,peak_price_since_entry,peak_unrealized_pct,trailing_stop_price,time_stop_status,
                position_age_days,position_age_cycles,created_at,payload
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()), self.run_id, symbol, decision.decision_type,
                "exit_candidate" if decision.action == "sell" and decision.is_actionable else ("exit_review_needed" if decision.exit_review_needed else "watch"),
                decision.reason, decision.drawdown_from_entry_pct, decision.drawdown_from_peak_pct,
                decision.unrealized_profit_pct, decision.highest_price_since_entry, decision.max_unrealized_profit_pct,
                decision.trailing_stop_price, "triggered" if decision.decision_type == "TIME_STOP_EXIT" else "not_triggered",
                decision.position_age_days, decision.position_age_cycles, now.isoformat(), json_dumps(dataclasses.asdict(decision)),
            ),
        )
        if decision.decision_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT", "TIME_STOP_EXIT"}:
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
        return decision_id

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
        # TAKE_PROFIT_PARTIAL progress is intentionally not changed here.
        # Proposal status is not fill status; DurableExecutionStore.record_fill
        # is the sole milestone advancement path.
        if pm_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT", "TIME_STOP_EXIT"}:
            self.storage.execute(
                "UPDATE profit_exit_events SET status=?, resolved_at=? WHERE proposal_id=?",
                (status, resolved_at, proposal_id),
            )

    @staticmethod
    def _buy_proposals_are_equivalent(current: dict[str, Any], other: dict[str, Any]) -> bool:
        current_id = str(current.get("id") or current.get("proposal_id") or "")
        other_id = str(other.get("id") or other.get("proposal_id") or "")
        if not current_id or not other_id or current_id == other_id:
            return False
        if str(current.get("replaces_proposal_id") or "") == other_id:
            return True
        for key in ("logical_action_key", "pyramiding_milestone_id", "pyramiding_milestone_key"):
            left, right = str(current.get(key) or ""), str(other.get(key) or "")
            if left and left == right:
                return True
        current_symbol = str(current.get("symbol") or "").upper()
        other_symbol = str(other.get("symbol") or "").upper()
        current_action = str(current.get("action") or "entry").lower()
        other_action = str(other.get("action") or "entry").lower()
        current_lifecycle = str(current.get("position_lifecycle_id") or "")
        other_lifecycle = str(other.get("position_lifecycle_id") or "")
        if (
            current_symbol
            and current_symbol == other_symbol
            and current_action == other_action
            and current_lifecycle
            and current_lifecycle == other_lifecycle
        ):
            return True
        current_setup = str(current.get("setup_key") or "")
        other_setup = str(other.get("setup_key") or "")
        if (
            current_setup
            and current_setup == other_setup
            and current_symbol == other_symbol
            and current_action == other_action
            and str(current.get("strategy_version") or "") == str(other.get("strategy_version") or "")
        ):
            return True
        current_rotation = str(current.get("rotation_group_id") or current.get("relationship_group_id") or "")
        other_rotation = str(other.get("rotation_group_id") or other.get("relationship_group_id") or "")
        return bool(
            current_rotation
            and current_rotation == other_rotation
            and str(current.get("relationship_type") or "") == "rotation_entry"
            and str(other.get("relationship_type") or "") == "rotation_entry"
        )

    def _supersede_equivalent_pending_buys(self, submitted: dict[str, Any]) -> list[str]:
        superseded: list[str] = []
        rows = self.storage.fetch_all(
            "SELECT * FROM trade_proposals WHERE side='buy' AND status='pending' AND id<>? ORDER BY created_at,id",
            (submitted.get("id") or submitted.get("proposal_id"),),
        )
        for row in rows:
            other = _hydrate_proposal_row(row)
            if not self._buy_proposals_are_equivalent(submitted, other):
                continue
            changed = self.storage.execute(
                "UPDATE trade_proposals SET status='superseded' WHERE id=? AND status='pending'",
                (row["id"],),
            )
            if changed.rowcount == 1:
                superseded.append(str(row["id"]))
        if superseded:
            self.storage.audit(
                self.run_id,
                "equivalent_buy_proposals_superseded",
                {
                    "submitted_proposal_id": submitted.get("id") or submitted.get("proposal_id"),
                    "superseded_proposal_ids": superseded,
                    "scope": "equivalent_or_mutually_exclusive_only",
                },
            )
        return superseded

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
            if pm_type == "TAKE_PROFIT_PARTIAL":
                from .profit_milestones import remaining_take_profit_quantity

                decision = proposal.get("position_management_decision") or {}
                try:
                    level = int(decision.get("take_profit_level") or 0)
                except (AttributeError, TypeError, ValueError):
                    level = 0
                lifecycle_id = str(
                    proposal.get("position_lifecycle_id")
                    or PositionLifecycleManager(self.storage).active_id(symbol)
                    or ""
                )
                if level not in {1, 2, 3} or not lifecycle_id:
                    return "take-profit milestone lifecycle provenance is unavailable"
                remaining = remaining_take_profit_quantity(
                    self.storage,
                    position_lifecycle_id=lifecycle_id,
                    take_profit_level=level,
                )
                if remaining is not None:
                    if remaining <= 1e-12:
                        return "take-profit milestone is already fully filled"
                    proposal["qty"] = sell_qty = min(sell_qty, remaining)
                    proposal["take_profit_remaining_quantity"] = remaining
            price = float(refreshed_price or proposal.get("latest_price") or 0.0)
            min_notional = float(self.config.get("position_management", {}).get("profit_taking", {}).get("minimum_notional_to_sell", 1.0))
            if sell_qty * price < min_notional:
                return "position-management sell is below minimum notional"
            open_orders = self.broker.get_open_orders() if self.broker is not None else []
            if any(
                str(_value(o, "symbol", "")).upper() == symbol
                and str(_value(o, "side", "")).lower() == "sell"
                for o in open_orders
            ):
                return "conflicting open sell order exists for symbol"
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
            trend = proposal.get("trend_evidence") or proposal.get("indicators") or {}
            for key in ("ma_50", "ma_200"):
                value = trend.get(key) if isinstance(trend, dict) else None
                try:
                    if value is None or not math.isfinite(float(value)) or float(value) <= 0:
                        return f"healthy-pullback add requires complete {key.upper()} trend evidence"
                except (TypeError, ValueError):
                    return f"healthy-pullback add requires complete {key.upper()} trend evidence"
        return None

    def _evaluate_winner_expansion(
        self,
        proposal: dict[str, Any],
        *,
        decision_stage: str,
        approval_id: str | None = None,
    ) -> tuple[Any, str, str]:
        """Run and persist the one canonical operational winner-ADD decision.

        This method is intentionally used at both proposal and final approval.
        The latter can only preserve or reduce the displayed action because the
        caller has already applied the displayed quantity/notional ceilings.
        """
        if proposal.get("action") != "add" or str(proposal.get("side", "")).lower() != "buy":
            raise ValueError("winner expansion applies only to long ADD proposals")
        symbol = str(proposal.get("symbol") or "").upper()
        lifecycle_id = str(
            proposal.get("position_lifecycle_id")
            or PositionLifecycleManager(self.storage).active_id(symbol)
            or ""
        )
        if not symbol or not lifecycle_id:
            raise ValueError("active position lifecycle is required for winner expansion")

        runtime_state = self._authoritative_runtime_state(force=True)
        positions = list(runtime_state.get("positions") or [])
        account = runtime_state.get("account")
        position = next(
            (item for item in positions if str(_value(item, "symbol", "")).upper() == symbol),
            None,
        )
        if position is None:
            raise ValueError("authoritative broker position is unavailable")
        current_shares = float(_value(position, "qty", 0.0) or 0.0)
        average_entry = float(_value(position, "avg_entry_price", 0.0) or 0.0)
        current_price = float(proposal.get("latest_price") or _value(position, "current_price", 0.0) or 0.0)
        if current_shares <= 0 or average_entry <= 0 or current_price <= 0:
            raise ValueError("positive long-position shares, entry, and current price are required")

        state_rows = self.storage.fetch_all(
            "SELECT * FROM position_management_state WHERE symbol=? AND position_lifecycle_id=?",
            (symbol, lifecycle_id),
        )
        if not state_rows:
            raise ValueError("authoritative position-management state is absent")
        position_state = state_rows[0]
        if (
            not position_state.get("entry_fill_id")
            or not position_state.get("entry_order_intent_id")
            or not position_state.get("initial_risk_reconstruction_source")
            or not position_state.get("initial_risk_formula_version")
            or position_state.get("position_lifecycle_id") != lifecycle_id
        ):
            reason = position_state.get("r_multiple_unavailable_reason") or "current lifecycle entry-fill provenance is incomplete"
            raise ValueError(f"initial R provenance unavailable: {reason}")
        winner_cfg = self.config.get("winner_expansion", {}) or {}
        current_stop, stop_current = _validated_authoritative_stop(position_state, winner_cfg)
        if position_state.get("initial_stop_price") is None:
            raise ValueError("initial R provenance unavailable: current lifecycle initial stop missing")
        initial_stop = float(position_state["initial_stop_price"])
        initial_risk_per_share = average_entry - initial_stop
        if initial_risk_per_share <= 0:
            raise ValueError("initial R geometry is unavailable or invalid")

        try:
            # Final approval has already fetched, validated, and bound its
            # marketable limit to one authoritative quote. Revalidate that
            # persisted envelope locally instead of fetching a second quote
            # which could desynchronise quantity, limit, and ADD-risk proof.
            quote = (
                validate_quote_payload(
                    proposal, "buy", self.config, now=datetime.now(UTC)
                )
                if decision_stage == "final_revalidation"
                else validated_quote(
                    self.broker, symbol, self.config, now=datetime.now(UTC)
                )
            )
            proposal["quote_bid"] = float(quote["bid"])
            proposal["quote_ask"] = float(quote["ask"])
            proposal["quote_midpoint"] = float(quote["midpoint"])
            proposal["quote_timestamp"] = quote["timestamp"]
            proposal["quote_spread_bps"] = float(quote["spread_bps"])
            # Winner risk and its pending reservation use the worst executable
            # local price, not merely the ask.  Final paper orders use a
            # bounded marketable BUY limit which can sit above the ask.
            add_price = max(
                float(quote["ask"]),
                float(proposal.get("limit_price") or 0.0),
                float(bounded_marketable_limit(quote, "buy", self.config)),
            )
            proposal["winner_risk_reference_price"] = add_price
        except Exception as exc:
            raise ValueError("fresh validated ADD quote is unavailable") from exc
        add_shares = float(proposal.get("qty") or 0.0)
        if add_shares <= 0:
            raise ValueError("positive adaptive ADD quantity is required")

        current_r = max(0.0, (current_price - average_entry) / initial_risk_per_share)
        highest_price = max(
            current_price,
            float(position_state.get("highest_price_since_entry") or current_price),
        )
        peak_r = max(current_r, (highest_price - average_entry) / initial_risk_per_share)
        atr = float(proposal.get("atr_value") or 0.0)
        if atr <= 0:
            raise ValueError("current ATR is required for trend management")
        trend_evidence = proposal.get("trend_evidence") or {}
        ma50 = float(trend_evidence.get("ma_50") or 0.0)
        ma200 = float(trend_evidence.get("ma_200") or 0.0)
        deployment_mode = str(proposal.get("deployment_mode") or "NORMAL").upper()
        if deployment_mode not in {"DEFENSIVE", "NORMAL", "OPPORTUNISTIC", "AGGRESSIVE"}:
            raise ValueError("operational Adaptive Conviction deployment mode is unavailable")
        drawdown_rows = self.storage.fetch_all(
            "SELECT drawdown_pct FROM account_equity_watermarks ORDER BY updated_at DESC LIMIT 1"
        )
        account_drawdown = float(drawdown_rows[0]["drawdown_pct"] or 0.0) if drawdown_rows else 0.0
        max_spread = float((self.config.get("quotes", {}) or {}).get("max_spread_bps", 50.0))
        execution_quality = max(0.0, min(1.0, 1.0 - float(proposal["quote_spread_bps"]) / max(max_spread, 1.0)))
        integrity_report = DurableExecutionStore(self.storage).integrity_report()
        critical_integrity = {
            "terminal_intents_with_active_reservations", "active_intents_missing_reservations",
            "fills_exceeding_quantity", "stale_unknown_intents", "stale_partial_fills",
            "broker_relevant_missing_identity",
        }
        integrity_ok = not any(int(integrity_report.get(key, 0)) for key in critical_integrity)
        prior_mode = position_state.get("management_mode")
        market_regime = "favorable" if float(proposal.get("score") or 0.0) >= 80 and deployment_mode in {"OPPORTUNISTIC", "AGGRESSIVE"} else "normal"
        trend = TrendManagementEngine().evaluate(
            TrendManagementInput(
                symbol=symbol,
                position_lifecycle_id=lifecycle_id,
                current_price=current_price,
                average_entry_price=average_entry,
                highest_price_since_entry=highest_price,
                current_protective_stop=current_stop,
                atr=atr,
                current_r_multiple=current_r,
                peak_r_multiple=peak_r,
                trend_strength=max(0.0, min(100.0, float(proposal.get("score") or 0.0))),
                price_above_ma50=ma50 > 0 and current_price > ma50,
                ma50_above_ma200=ma50 > 0 and ma200 > 0 and ma50 > ma200,
                higher_highs_and_lows=bool(proposal.get("higher_highs_and_lows", True)),
                market_regime=market_regime,
                volatility_regime=str(proposal.get("volatility_regime") or "normal"),
                deployment_mode=deployment_mode,
                execution_quality=execution_quality,
                account_health=max(0.0, min(1.0, 1.0 - account_drawdown / 6.0)),
                account_drawdown_pct=max(0.0, account_drawdown),
                position_age_days=float((proposal.get("position_management_decision") or {}).get("position_age_days") or 1.0),
                previous_mode=str(prior_mode) if prior_mode else None,
                deterioration_detected=bool(proposal.get("exit_trigger_reason")),
                profit_protection_triggered=bool(position_state.get("profit_protection_active")),
                emergency_exit=bool(proposal.get("emergency_exit_triggered")),
                normal_exit_signal=False,
                integrity_warning=not integrity_ok,
                reconciliation_warning=not integrity_ok,
                as_of=iso_now(),
                minimum_price_increment=float((self.config.get("quotes", {}) or {}).get("price_increment_usd", 0.01)),
            )
        )
        winner_store = WinnerExpansionStore(self.storage)
        winner_store.persist_trend_decision(
            trend, run_id=self.run_id, config_hash=self.config.get("effective_config_hash")
        )
        stop_as_of = iso_now()

        canonical = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(positions, account)
        equity = float(canonical.portfolio_equity or 0.0)
        if equity <= 0 or canonical.held_open_stop_risk is None or canonical.projected_gross_exposure is None:
            raise ValueError("canonical portfolio risk snapshot is incomplete")
        reservations = DurableExecutionStore(self.storage).active_reservations()
        same_symbol_reservations = self.storage.fetch_all(
            """SELECT COALESCE(SUM(active_notional),0) notional,
                      COALESCE(SUM(active_stop_risk),0) stop_risk
               FROM risk_reservations WHERE state='active' AND symbol=?""",
            (symbol,),
        )[0]
        cluster = self._get_symbol_cluster(symbol)
        cluster_reserved_rows = self.storage.fetch_all(
            "SELECT COALESCE(SUM(active_notional),0) notional FROM risk_reservations WHERE state='active' AND cluster_name=?",
            (cluster,),
        ) if cluster else []
        current_position_open_risk = current_shares * max(current_price - current_stop, 0.0)
        current_position_notional = current_shares * current_price
        risk_profile = (self.config.get("phase3", {}) or {}).get("risk_profile", {})
        mode_allowances = (self.config.get("winner_expansion", {}) or {}).get("mode_incremental_risk_allowance_pct", {})
        risk_input = PositionRiskInput(
            symbol=symbol,
            position_lifecycle_id=lifecycle_id,
            deployment_mode=deployment_mode,
            current_shares=current_shares,
            proposed_add_shares=add_shares,
            current_market_price=current_price,
            proposed_add_price=add_price,
            current_protective_stop=current_stop,
            proposed_tightened_stop=trend.protective_stop,
            portfolio_equity=equity,
            same_symbol_reserved_stop_risk_dollars=float(same_symbol_reservations.get("stop_risk") or 0.0),
            active_reserved_stop_risk_dollars=float(reservations.get("active_reserved_stop_risk") or 0.0),
            portfolio_open_risk_excluding_position_dollars=max(0.0, float(canonical.held_open_stop_risk) - current_position_open_risk),
            active_reserved_exposure_dollars=float(reservations.get("active_reserved_notional") or 0.0),
            same_symbol_reserved_exposure_dollars=float(same_symbol_reservations.get("notional") or 0.0),
            cluster_reserved_exposure_dollars=float(cluster_reserved_rows[0]["notional"] or 0.0) if cluster_reserved_rows else 0.0,
            portfolio_gross_exposure_excluding_position_dollars=max(0.0, float(canonical.filled_gross_exposure or 0.0) - current_position_notional),
            cluster_exposure_excluding_position_dollars=max(0.0, float(canonical.cluster_exposure.get(cluster, 0.0)) - current_position_notional) if cluster else 0.0,
            adaptive_conviction_position_risk_cap_pct={"DEFENSIVE": 0.15, "NORMAL": 0.20, "OPPORTUNISTIC": 0.30, "AGGRESSIVE": 0.35}[deployment_mode],
            phase3_position_risk_cap_pct=float(risk_profile.get("max_trade_stop_risk_pct", 0.35)),
            portfolio_heat_cap_pct=float(risk_profile.get("max_portfolio_heat_pct", 1.75)),
            symbol_exposure_cap_pct=float(risk_profile.get("max_symbol_exposure_pct", 6.0)),
            cluster_exposure_cap_pct=float(risk_profile.get("max_cluster_exposure_pct", 15.0)),
            portfolio_gross_exposure_cap_pct=float(risk_profile.get("hard_gross_exposure_pct", 50.0)),
            mode_incremental_risk_allowance_pct=float(mode_allowances.get(deployment_mode, 0.0)),
            rounding_tolerance_dollars=float((self.config.get("winner_expansion", {}) or {}).get("rounding_tolerance_dollars", 0.01)),
            as_of=stop_as_of,
            config_hash=self.config.get("effective_config_hash"),
        )
        milestone_cfg = (self.config.get("winner_expansion", {}) or {}).get("milestones", {})
        prior_filled_adds = int(self.storage.fetch_all(
            "SELECT COUNT(*) count FROM pyramiding_milestones WHERE position_lifecycle_id=? AND status='FILLED'",
            (lifecycle_id,),
        )[0]["count"])
        milestone = MilestoneIdentity.build(
            symbol=symbol,
            position_lifecycle_id=lifecycle_id,
            current_r_multiple=current_r,
            price_advance_since_prior_entry_pct=max(0.0, (current_price / average_entry - 1.0) * 100.0),
            stop_advance_r=max(0.0, (trend.protective_stop - initial_stop) / initial_risk_per_share),
            prior_filled_adds=prior_filled_adds,
            trend_mode=trend.mode,
            r_step=float(milestone_cfg.get("r_multiple_step", 0.5)),
            price_advance_step_pct=float(milestone_cfg.get("price_advance_atr_step", 0.75)),
            stop_advance_step_r=float(milestone_cfg.get("stop_advance_atr_step", 0.5)),
        )
        existing_milestone = winner_store.get_milestone(lifecycle_id, milestone.milestone_key)
        proposal_id = str(proposal.get("proposal_id") or proposal.get("id") or "")
        if decision_stage == "proposal":
            milestone_available = existing_milestone is None
        else:
            milestone_available = bool(
                existing_milestone
                and existing_milestone.get("active_proposal_id") == proposal_id
                and existing_milestone.get("status") in {"PROPOSED", "APPROVED"}
                and proposal.get("pyramiding_milestone_key") == milestone.milestone_key
            )
        policy_state = str(proposal.get("strategy_state") or "SUSPENDED").upper()
        adaptive_record = proposal.get("adaptive_sizing_final") if decision_stage == "final_revalidation" else proposal.get("adaptive_sizing")
        winner_input = WinnerExpansionInput(
                risk_input=risk_input,
                trend_decision=trend,
                milestone=milestone,
                average_entry_price=average_entry,
                strategy_state=policy_state,
                policy_adds_allowed=policy_state in {"THROTTLED", "ACTIVE"},
                regime_supports_add=str(proposal.get("volatility_regime") or "normal").lower() not in {"extreme", "dislocated", "high"},
                setup_score=float(proposal.get("score") or 0.0),
                minimum_setup_score=float((self.config.get("add_to_position", {}) or {}).get("min_trade_score", 85.0)),
                score_improvement=float(proposal.get("score_improvement") or 0.0),
                minimum_score_improvement=(
                    0.0
                    if abs(float(proposal.get("score_improvement") or 0.0)) <= 1e-9
                    else float((self.config.get("add_to_position", {}) or {}).get("min_score_improvement", 5.0))
                ),
                exit_or_deterioration_warning=bool(proposal.get("exit_trigger_reason") or proposal.get("emergency_exit_triggered")),
                reconciliation_ok=integrity_ok,
                integrity_ok=integrity_ok,
                quote_current=True,
                spread_ok=float(proposal["quote_spread_bps"]) <= max_spread,
                liquidity_ok=float(proposal.get("average_dollar_volume") or 0.0) >= float(risk_profile.get("minimum_average_dollar_volume", 10_000_000.0)),
                stop_current=stop_current,
                milestone_available=milestone_available,
                adaptive_conviction_authorized=bool(proposal.get("adaptive_conviction")),
                adaptive_sizing_authorized=bool(adaptive_record and float(adaptive_record.get("operational_notional") or 0.0) > 0),
                phase3_validation_passed=False,
                phase3_validation_stage="pending",
                decision_stage=decision_stage,
            )
        decision = WinnerExpansionEngine().evaluate(winner_input)
        required_phase3_stage = "final" if decision_stage == "final_revalidation" else "proposal"
        phase3_blocker = f"Phase 3 {required_phase3_stage} validation has not passed"
        non_phase3_blockers = tuple(
            reason for reason in decision.blocking_reasons if reason != phase3_blocker
        )
        if non_phase3_blockers:
            raise ValueError(non_phase3_blockers[0])
        phase3_candidate = {
            **proposal,
            "stop_price": decision.required_protective_stop,
            "stop_distance_dollars": max(
                float(proposal.get("latest_price") or 0.0) - decision.required_protective_stop,
                0.0,
            ),
            "stop_risk_dollars": decision.risk_decision.consumed_risk,
            "risk_budget": decision.risk_decision.consumed_risk,
            "risk_budget_dollars": decision.risk_decision.consumed_risk,
            "pending_add_stop_risk": decision.proposed_add_shares
            * max(decision.risk_decision.proposed_add_price - decision.required_protective_stop, 0.0),
        }
        if decision_stage == "final_revalidation":
            phase3_candidate["client_order_id"] = stable_client_order_id(
                logical_action_key(phase3_candidate, "proposal")
            )
        phase3_context = self._portfolio_context(
            phase3_candidate, approval_valid=decision_stage == "final_revalidation"
        )
        if decision_stage == "final_revalidation":
            phase3_context["final_revalidation"] = True
        phase3_decision = self._risk_engine(
            proposal_id or "winner_add", f"winner_phase3_{required_phase3_stage}_preflight"
        ).evaluate(
            phase3_candidate,
            phase3_context,
            final=decision_stage == "final_revalidation",
        )
        if not phase3_decision.passed:
            raise ValueError(
                f"Phase 3 {required_phase3_stage} validation blocked: "
                + "; ".join(phase3_decision.reasons)
            )
        decision = WinnerExpansionEngine().evaluate(
            dataclasses.replace(
                winner_input,
                phase3_validation_passed=True,
                phase3_validation_stage=required_phase3_stage,
            )
        )
        if not decision.eligible:
            raise ValueError(decision.reason)
        if decision_stage == "final_revalidation":
            displayed_post_risk = float(proposal.get("approved_post_add_open_risk_ceiling") or 0.0)
            displayed_incremental = float(proposal.get("approved_incremental_risk_ceiling") or 0.0)
            displayed_stop_floor = float(proposal.get("approved_protective_stop_floor") or 0.0)
            if displayed_post_risk <= 0 or decision.post_add_open_risk > displayed_post_risk + 1e-9:
                raise ValueError("final ADD total position risk exceeds the displayed ceiling")
            if decision.risk_decision.consumed_risk > displayed_incremental + 1e-9:
                raise ValueError("final ADD incremental risk exceeds the displayed ceiling")
            if decision.required_protective_stop + 1e-9 < displayed_stop_floor:
                raise ValueError("final protective stop would loosen the displayed stop floor")
            winner_store.persist_authoritative_stop(
                trend,
                run_id=self.run_id,
                source="winner_expansion_final_revalidation",
                stop_as_of=stop_as_of,
                config_hash=self.config.get("effective_config_hash"),
                peak_r_multiple=peak_r,
            )
        if decision_stage == "proposal":
            claim = winner_store.claim_milestone(
                milestone,
                run_id=self.run_id,
                proposal_id=proposal_id,
                max_retries=int(milestone_cfg.get("max_retries", 0)),
            )
            if not claim.accepted:
                raise ValueError(claim.reason)
            milestone_id = claim.milestone_id
        else:
            milestone_id = str(existing_milestone["id"])
            winner_store.transition_milestone(
                lifecycle_id,
                milestone.milestone_key,
                "APPROVED",
                approval_id=approval_id,
            )
        decision_id = winner_store.persist_add_risk_decision(
            decision,
            run_id=self.run_id,
            proposal_id=proposal_id,
            approval_id=approval_id,
            milestone_id=milestone_id,
            config_hash=self.config.get("effective_config_hash"),
        )
        proposal.update({
            "winner_expansion_decision_id": decision_id,
            "pyramiding_milestone_id": milestone_id,
            "pyramiding_milestone_key": milestone.milestone_key,
            "management_mode": trend.mode,
            "current_shares": current_shares,
            "add_shares": decision.proposed_add_shares,
            "current_protective_stop": current_stop,
            "proposed_protective_stop": decision.required_protective_stop,
            "stop_price": decision.required_protective_stop,
            "stop_distance_dollars": max(
                float(proposal.get("latest_price") or 0.0) - decision.required_protective_stop,
                0.0,
            ),
            "pre_add_open_risk": decision.pre_add_open_risk,
            "post_add_open_risk": decision.post_add_open_risk,
            "incremental_risk": decision.incremental_risk,
            "pending_add_stop_risk": decision.proposed_add_shares
            * max(decision.risk_decision.proposed_add_price - decision.required_protective_stop, 0.0),
            "risk_released": decision.released_risk,
            "risk_operator_classification": "risk_neutral" if decision.incremental_risk <= risk_input.rounding_tolerance_dollars else "bounded",
            "binding_cap": decision.binding_cap,
            "winner_expansion_reason": decision.reason,
            "current_r_multiple": current_r,
            "position_lifecycle_id": lifecycle_id,
        })
        if decision_stage == "proposal":
            proposal.update({
                "approved_post_add_open_risk_ceiling": decision.post_add_open_risk,
                "approved_incremental_risk_ceiling": decision.risk_decision.consumed_risk,
                "approved_protective_stop_floor": decision.required_protective_stop,
            })
        return decision, decision_id, milestone_id

    def _evaluate_held_position_trend(
        self,
        *,
        symbol: str,
        current_price: float,
        average_entry_price: float,
        bars: pd.DataFrame,
        score: float,
        volatility_regime: str,
        strategy_version: str,
        position_age_days: float | None,
        normal_exit_signal: bool,
        emergency_exit: bool,
        deterioration_detected: bool,
        now: datetime,
    ) -> Any:
        """Persist the operational trend mode and monotonic protective stop."""
        lifecycle_id = PositionLifecycleManager(self.storage).active_id(symbol)
        state = self._position_management_state(symbol)
        if not lifecycle_id or not state:
            raise ValueError("active lifecycle and durable position-management state are required")
        stops = [
            float(value)
            for value in (
                state.get("initial_stop_price"), state.get("trailing_stop_price"),
                state.get("authoritative_protective_stop"),
            )
            if value is not None and float(value) > 0
        ]
        if not stops:
            raise ValueError("durable protective stop is required")
        current_stop = max(stops)
        initial_stop = float(state.get("initial_stop_price") or current_stop)
        initial_risk = average_entry_price - initial_stop
        if initial_risk <= 0 or bars.empty or len(bars) < 50:
            raise ValueError("valid initial R geometry and trend history are required")
        close = pd.to_numeric(bars["close"], errors="coerce")
        high = pd.to_numeric(bars["high"], errors="coerce")
        low = pd.to_numeric(bars["low"], errors="coerce")
        ma50 = float(close.tail(50).mean())
        ma200 = float(close.tail(min(200, len(close))).mean())
        prior_close = close.shift(1)
        atr = float(pd.concat(
            [(high - low).abs(), (high - prior_close).abs(), (low - prior_close).abs()], axis=1
        ).max(axis=1).tail(14).mean())
        if not all(math.isfinite(value) and value > 0 for value in (ma50, ma200, atr)):
            raise ValueError("trend moving-average or ATR evidence is unavailable")
        highest = max(
            current_price, float(state.get("highest_price_since_entry") or current_price),
            float(high.tail(min(200, len(high))).max()),
        )
        current_r = (current_price - average_entry_price) / initial_risk
        peak_r = max(float(state.get("peak_r_multiple") or current_r), (highest - average_entry_price) / initial_risk)
        drawdown_rows = self.storage.fetch_all(
            "SELECT drawdown_pct FROM account_equity_watermarks ORDER BY updated_at DESC LIMIT 1"
        )
        account_drawdown = max(0.0, float(drawdown_rows[0]["drawdown_pct"] or 0.0)) if drawdown_rows else 0.0
        execution = self._adaptive_conviction_execution_evidence(strategy_version)
        fill_rate = execution.get("execution_fill_rate")
        shortfall = execution.get("execution_shortfall_bps")
        execution_quality = 0.70 if fill_rate is None else max(0.0, min(1.0, float(fill_rate)))
        if shortfall is not None:
            execution_quality = min(execution_quality, max(0.0, 1.0 - float(shortfall) / 100.0))
        deployment_mode = "DEFENSIVE" if account_drawdown >= 4.0 or volatility_regime == "extreme" else "NORMAL"
        market_regime = "favorable" if score >= 80 and current_price > ma50 > ma200 else "normal"
        higher_highs_lows = bool(
            len(high) >= 20
            and float(high.tail(10).mean()) >= float(high.iloc[-20:-10].mean())
            and float(low.tail(10).mean()) >= float(low.iloc[-20:-10].mean())
        )
        decision = TrendManagementEngine().evaluate(TrendManagementInput(
            symbol=symbol, position_lifecycle_id=lifecycle_id, current_price=current_price,
            average_entry_price=average_entry_price, highest_price_since_entry=highest,
            current_protective_stop=current_stop, atr=atr, current_r_multiple=current_r,
            peak_r_multiple=peak_r, trend_strength=max(0.0, min(100.0, score)),
            price_above_ma50=current_price > ma50, ma50_above_ma200=ma50 > ma200,
            higher_highs_and_lows=higher_highs_lows, market_regime=market_regime,
            volatility_regime=volatility_regime, deployment_mode=deployment_mode,
            execution_quality=execution_quality,
            account_health=max(0.0, min(1.0, 1.0 - account_drawdown / 6.0)),
            account_drawdown_pct=account_drawdown,
            position_age_days=max(0.0, float(position_age_days or 0.0)),
            previous_mode=str(state.get("management_mode")) if state.get("management_mode") else None,
            deterioration_detected=deterioration_detected,
            profit_protection_triggered=bool(state.get("profit_protection_active")),
            emergency_exit=emergency_exit, normal_exit_signal=normal_exit_signal,
            as_of=now.isoformat(),
            minimum_price_increment=float((self.config.get("quotes", {}) or {}).get("price_increment_usd", 0.01)),
        ))
        store = WinnerExpansionStore(self.storage)
        store.persist_trend_decision(decision, run_id=self.run_id, config_hash=self.config.get("effective_config_hash"))
        stop_as_of = now.isoformat()
        store.persist_authoritative_stop(
            decision, run_id=self.run_id, source="position_trend_management",
            stop_as_of=stop_as_of, config_hash=self.config.get("effective_config_hash"),
            peak_r_multiple=peak_r,
        )
        return decision

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
        reservation_snapshot = DurableExecutionStore(self.storage).active_reservations()
        active_reserved_notional = float(reservation_snapshot["active_reserved_notional"])
        active_reserved_stop_risk = float(reservation_snapshot["active_reserved_stop_risk"])
        pending_execution = self._pending_execution_totals()
        canonical_risk = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(positions, account)
        reserved_pct = (active_reserved_notional / equity) * 100 if equity > 0 else float("inf")

        # Proposal notional
        proposal_notional = float(proposal.get("notional") or 0.0)
        proposal_notional_pct = (proposal_notional / equity) * 100 if equity > 0 else 0.0

        # Proposed total exposure %
        proposed_total_exposure_pct = snapshot["total_exposure_pct"] + reserved_pct + proposal_notional_pct

        # Proposed symbol exposure %
        symbol_reserved = float(reservation_snapshot["symbol_reserved_notional"].get(symbol.upper(), 0.0))
        current_symbol_exposure = snapshot["single_exposures"].get(symbol.upper(), 0.0) + ((symbol_reserved / equity) * 100 if equity > 0 else float("inf"))
        proposed_symbol_exposure_pct = current_symbol_exposure + proposal_notional_pct

        # Cluster parameters
        c_name = self._get_symbol_cluster(symbol)
        proposed_cluster_positions_count = 0
        proposed_cluster_exposure_pct = 0.0
        if c_name:
            current_cluster_count = snapshot["cluster_counts"].get(c_name, 0)
            cluster_reserved = float(reservation_snapshot["cluster_reserved_notional"].get(c_name, 0.0))
            current_cluster_exposure = snapshot["cluster_exposures"].get(c_name, 0.0) + ((cluster_reserved / equity) * 100 if equity > 0 else float("inf"))
            has_symbol_pos = any(str(_value(p, "symbol", "")).upper() == symbol.upper() for p in positions)
            proposed_cluster_positions_count = current_cluster_count + (0 if has_symbol_pos else 1)
            proposed_cluster_exposure_pct = current_cluster_exposure + proposal_notional_pct

        excluded_rotation_group_id = None
        relationship_group_id = str(
            proposal.get("rotation_group_id") or proposal.get("relationship_group_id") or ""
        )
        if proposal.get("relationship_type") == "rotation_entry" and relationship_group_id:
            rotation_rows = self.storage.fetch_all(
                """SELECT state,actual_released_notional,reconciliation_fingerprint
                   FROM rotation_groups WHERE id=?""",
                (relationship_group_id,),
            )
            reconciled_states = {
                "reconciled", "entry_revalidating", "entry_reserved",
                "entry_submitted", "completed", "entry_blocked",
            }
            if (
                len(rotation_rows) == 1
                and str(rotation_rows[0].get("state")) in reconciled_states
                and float(rotation_rows[0].get("actual_released_notional") or 0.0) > 0
                and rotation_rows[0].get("reconciliation_fingerprint")
            ):
                excluded_rotation_group_id = relationship_group_id
        exit_blocker = self._exit_blocker_context(
            orders, exclude_reconciled_rotation_group_id=excluded_rotation_group_id
        )
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

        universe_symbol_info = None
        active_dynamic = []
        if getattr(self, "storage", None):
            try:
                universe_symbol_row = self.storage.fetch_all(
                    "SELECT * FROM universe_symbols WHERE symbol=?", (symbol.upper(),)
                )
                if universe_symbol_row:
                    universe_symbol_info = dict(universe_symbol_row[0])
            except Exception:
                pass

            try:
                active_dynamic, _ = self._dynamic_universe_scan_symbols()
            except Exception:
                pass

        realized = LotLedger(self.storage).summary()
        accounting_rows = self.storage.fetch_all("SELECT account_equity_change,realized_fifo_pnl,unrealized_change,external_cash_flow,accounting_version,accounting_confidence FROM cash_snapshots ORDER BY id DESC LIMIT 1")
        accounting = accounting_rows[0] if accounting_rows else {}
        loss_metrics = build_loss_metrics(
            state.get("loss_metrics"),
            account_equity=equity,
            daily_realized_pl=realized.daily_realized_pl,
            weekly_realized_pl=realized.weekly_realized_pl,
            daily_confidence=realized.daily_confidence,
            weekly_confidence=realized.weekly_confidence,
            realized_provenance=realized.provenance,
        )
        probe_commitments = self._probe_commitments(str(proposal.get("strategy_version") or STRATEGY_VERSION))
        proposal_stop_risk = float(proposal.get("stop_risk_dollars") or proposal.get("initial_risk_dollars") or 0.0)
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
            **loss_metrics.as_context(),
            "daily_realized_pl": realized.daily_realized_pl,
            "weekly_realized_pl": realized.weekly_realized_pl,
            "daily_realized_pl_status": realized.daily_confidence,
            "weekly_realized_pl_status": realized.weekly_confidence,
            "account_equity_change": accounting.get("account_equity_change"),
            "realized_fifo_pnl": accounting.get("realized_fifo_pnl"),
            "unrealized_change": accounting.get("unrealized_change"),
            "external_cash_flow": accounting.get("external_cash_flow"),
            "accounting_version": accounting.get("accounting_version", ACCOUNTING_VERSION),
            "accounting_confidence": accounting.get("accounting_confidence", "unavailable"),
            # Broker account equity-loss metrics are an existing conservative
            # control whenever both authoritative values are available.
            "absolute_loss_control_reliable": loss_metrics.daily_loss_pct is not None and loss_metrics.weekly_loss_pct is not None,
            "buying_power": max(0.0, float(_value(account, "buying_power", 0) or 0) - active_reserved_notional) if account is not None else None,
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
            "universe_symbol_info": universe_symbol_info,
            "active_dynamic_paper_tradable_symbols": active_dynamic,
            "portfolio_equity": equity,
            "cash": snapshot["cash"],
            "active_reserved_exposure": active_reserved_notional,
            "active_reserved_stop_risk": active_reserved_stop_risk,
            "pending_buy_exposure_unknown": bool(pending_execution.get("unknown")),
            "pending_buy_exposure_unknown_reason": pending_execution.get("unknown_reason"),
            "pending_buy_exposure_unknown_rows": pending_execution.get("unknown_rows", []),
            "pending_buy_notional": float(pending_execution.get("total_notional") or 0.0),
            "pending_buy_stop_risk": float(pending_execution.get("total_stop_risk") or 0.0),
            "probe_active_count": int(probe_commitments["active_count"]),
            "probe_projected_count": int(probe_commitments["active_count"]) + (1 if proposal.get("phase4_mode") == "probe" and proposal.get("action", "entry") == "entry" else 0),
            "probe_projected_gross_notional": float(probe_commitments["gross_notional"]) + (proposal_notional if proposal.get("phase4_mode") == "probe" else 0.0),
            "probe_projected_stop_risk": float(probe_commitments["stop_risk"]) + (proposal_stop_risk if proposal.get("phase4_mode") == "probe" else 0.0),
            "held_open_stop_risk": canonical_risk.held_open_stop_risk,
            "unresolved_unknown_order_exposure": sum(
                float(row.get("active_notional") or 0)
                for row in self.storage.fetch_all(
                    """SELECT r.active_notional FROM risk_reservations r
                       JOIN order_intents i ON i.id=r.intent_id WHERE r.state='active' AND i.state='unknown'"""
                )
            ),
        }

    def _adaptive_conviction_execution_evidence(self, strategy_version: str) -> dict[str, Any]:
        rows = self.storage.fetch_all(
            """SELECT o.status,COALESCE(
                         (SELECT f.implementation_shortfall_bps FROM fills f
                          WHERE f.order_id=o.id AND f.implementation_shortfall_bps IS NOT NULL
                          ORDER BY f.filled_at DESC,f.id DESC LIMIT 1),
                         o.implementation_shortfall_bps) AS shortfall_bps
               FROM orders o JOIN trade_proposals p ON p.id=o.proposal_id
               WHERE p.strategy_version=? AND lower(o.side)='buy'
               ORDER BY o.created_at DESC LIMIT 50""",
            (strategy_version,),
        )
        if not rows:
            return {
                "execution_fill_rate": None,
                "execution_shortfall_bps": None,
                "execution_evidence_count": 0,
                "execution_evidence_calibrated": False,
            }
        completed = sum(str(row["status"] or "").lower() in {"filled", "partially_filled"} for row in rows)
        shortfalls = sorted(float(row["shortfall_bps"]) for row in rows if row["shortfall_bps"] is not None)
        middle = len(shortfalls) // 2
        median_shortfall = None if not shortfalls else (
            shortfalls[middle] if len(shortfalls) % 2 else (shortfalls[middle - 1] + shortfalls[middle]) / 2.0
        )
        return {
            "execution_fill_rate": completed / len(rows),
            "execution_shortfall_bps": median_shortfall,
            "execution_evidence_count": len(rows),
            "execution_evidence_calibrated": len(rows) >= 10 and median_shortfall is not None,
        }

    def _record_adaptive_conviction(
        self,
        proposal: dict[str, Any],
        sizing: dict[str, Any],
        portfolio: dict[str, Any],
        *,
        risk_checks_passed: bool,
        stage: str = "proposal",
        approval_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Gather authoritative inputs; formula and classification stay in the engine."""
        if proposal.get("action") not in {"entry", "add"} or str(proposal.get("side") or "").lower() != "buy":
            return None
        adaptive_config = self.config.get("adaptive_conviction")
        if not isinstance(adaptive_config, dict) or adaptive_config.get("enabled") is not True:
            return None
        equity = float(portfolio.get("portfolio_equity") or 0.0)
        proposal_pct = (float(proposal.get("notional") or 0.0) / equity * 100.0) if equity > 0 else None
        current_gross = None if proposal_pct is None else max(0.0, float(portfolio.get("proposed_total_exposure_pct") or 0.0) - proposal_pct)
        current_symbol = None if proposal_pct is None else max(0.0, float(portfolio.get("proposed_symbol_exposure_pct") or 0.0) - proposal_pct)
        current_cluster = None if proposal_pct is None else max(0.0, float(portfolio.get("proposed_cluster_exposure_pct") or 0.0) - proposal_pct)
        current_heat = None
        if equity > 0:
            current_heat = (
                float(portfolio.get("held_open_stop_risk") or 0.0)
                + float(portfolio.get("active_reserved_stop_risk") or 0.0)
                + float(portfolio.get("pending_buy_stop_risk") or 0.0)
            ) / equity * 100.0
        integrity = DurableExecutionStore(self.storage).integrity_report()
        integrity_ok = not any(integrity.values())
        execution = self._adaptive_conviction_execution_evidence(str(proposal.get("strategy_version") or ""))
        target_price = (proposal.get("indicators") or {}).get("target_price")
        reward_to_risk = None
        stop_price = proposal.get("stop_price")
        entry_price = proposal.get("proposal_price")
        try:
            if target_price is not None and stop_price is not None and entry_price is not None and float(entry_price) > float(stop_price):
                reward_to_risk = max(0.0, (float(target_price) - float(entry_price)) / (float(entry_price) - float(stop_price)))
        except (TypeError, ValueError):
            reward_to_risk = None
        if reward_to_risk is None:
            snapshot_id = sizing.get("performance_snapshot_id") or proposal.get("performance_snapshot_id")
            if snapshot_id:
                rows = self.storage.fetch_all(
                    "SELECT metrics_json FROM strategy_performance_snapshots WHERE id=? LIMIT 1",
                    (snapshot_id,),
                )
                if rows:
                    try:
                        metrics = json.loads(rows[0]["metrics_json"] or "{}")
                        payoff = metrics.get("payoff_ratio")
                        reward_to_risk = float(payoff) if payoff is not None and math.isfinite(float(payoff)) else None
                    except (TypeError, ValueError, json.JSONDecodeError):
                        reward_to_risk = None
        state = str(proposal.get("strategy_state") or "")
        strategy_version = str(proposal.get("strategy_version") or "")
        phase4_policy = (self._phase4_allocation_cache or {}).get("strategy_policies", {}).get(strategy_version, {})
        phase4_quality = phase4_policy.get("data_quality")
        policy_quality = sizing.get("strategy_quality_score", proposal.get("strategy_quality_score"))
        if policy_quality is not None:
            evidence_quality = float(policy_quality) / 100.0
            if phase4_quality is not None:
                evidence_quality = min(evidence_quality, float(phase4_quality))
        else:
            evidence_quality = None
        current_regime_performance = phase4_policy.get("current_regime_performance") or {}
        if str(current_regime_performance.get("regime") or "").lower() != str(proposal.get("volatility_regime") or "").lower():
            current_regime_performance = {}
        current_regime_negative = (
            current_regime_performance.get("reliable") is True
            and float(current_regime_performance.get("conservative_expected_return") or 0.0) <= 0.0
        )
        operational_risk = proposal.get("permitted_stop_risk_pct")
        if operational_risk is None and equity > 0:
            operational_risk = float(proposal.get("stop_risk_dollars") or 0.0) / equity * 100.0
        phase3_profile = ((self.config.get("phase3", {}) or {}).get("risk_profile", {}) or {})
        base_strategy_risk = (
            float(phase3_profile.get("add_stop_risk_pct", 0.10))
            if proposal.get("action") == "add"
            else float(phase3_profile.get("base_stop_risk_pct", 0.20))
        )
        if state == "ACTIVE":
            base_strategy_risk *= max(0.0, min(1.0, float(phase4_policy.get("risk_budget_multiplier", 0.0))))
        if state != "ACTIVE":
            base_strategy_risk = float(operational_risk or 0.0)
        inputs = {
            "evaluation_time": iso_now(),
            "run_id": proposal.get("run_id"), "proposal_id": proposal.get("id"),
            "candidate_id": proposal.get("signal_id"), "setup_id": proposal.get("setup_key"),
            "decision_stage": stage, "approval_id": approval_id,
            "strategy_version": proposal.get("strategy_version"),
            "policy_decision_id": sizing.get("policy_decision_id") or proposal.get("policy_decision_id"),
            "performance_snapshot_id": sizing.get("performance_snapshot_id") or proposal.get("performance_snapshot_id"),
            "action": proposal.get("action"), "side": proposal.get("side"),
            "strategy_authorized": state in {"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"},
            "strategy_policy_state": state,
            "base_strategy_risk_pct": base_strategy_risk,
            "strategy_stop_risk_cap_pct": (
                float((self.config.get("phase3", {}) or {}).get("risk_profile", {}).get("max_trade_stop_risk_pct", 0.35))
                if state == "ACTIVE" else float(operational_risk or 0.0)
            ),
            "evidence_quality": evidence_quality,
            "evidence_calibrated": state in {"EXPLORATION", "THROTTLED", "ACTIVE"} and (sizing.get("performance_snapshot_id") or proposal.get("performance_snapshot_id")) is not None,
            "market_regime": proposal.get("volatility_regime"),
            "account_drawdown_pct": sizing.get("phase3_account_drawdown_pct"),
            "daily_realized_loss_pct": portfolio.get("daily_loss_pct"),
            "weekly_realized_loss_pct": portfolio.get("weekly_loss_pct"),
            "execution_integrity_ok": integrity_ok,
            "reconciliation_ok": integrity_ok and not bool(portfolio.get("pending_buy_exposure_unknown")),
            "integrity_counts": integrity,
            "current_portfolio_heat_pct": current_heat,
            "current_gross_exposure_pct": current_gross,
            "symbol_exposure_pct": current_symbol,
            "cluster_exposure_pct": current_cluster,
            "correlation_score": None if current_cluster is None else min(1.0, current_cluster / float(adaptive_config["maximum_cluster_exposure_pct"])),
            "setup_score": proposal.get("score"),
            "stop_valid": proposal.get("stop_validation_status") == "validated",
            "stop_distance_pct": proposal.get("stop_distance_pct"),
            "reward_to_risk": reward_to_risk,
            "average_dollar_volume": proposal.get("average_dollar_volume"),
            "quote_spread_bps": proposal.get("quote_spread_bps"),
            "market_data_fresh": proposal.get("proposal_price_age_seconds_at_send") is not None and float(proposal.get("proposal_price_age_seconds_at_send")) <= float(self.config.get("telegram", {}).get("proposal_price_freshness_threshold_seconds", 60.0)),
            "risk_checks_passed": risk_checks_passed,
            "deterioration_detected": current_regime_negative or state == "THROTTLED" or "deterior" in str(proposal.get("binding_policy_reason") or "").lower(),
            "phase4_current_regime_performance": current_regime_performance,
            "operational_stop_risk_pct": operational_risk,
            **execution,
        }
        engine = AdaptiveConvictionEngine(self.config)
        adaptive = engine.evaluate(inputs)
        if adaptive is None:
            return None
        engine.persist(self.storage, adaptive)
        summary = adaptive.summary()
        self.storage.audit(self.run_id, "adaptive_conviction_operational_paper", summary)
        return summary

    def _operational_adaptive_enabled(self) -> bool:
        """Return whether the complete compatible paper-adaptive stack is active."""
        phase3 = self.config.get("phase3", {}) or {}
        phase4 = self.config.get("phase4", {}) or {}
        conviction = self.config.get("adaptive_conviction", {}) or {}
        sizing = self.config.get("adaptive_sizing", {}) or {}
        return bool(
            phase3.get("enabled") and phase3.get("active")
            and phase4.get("enabled") and phase4.get("active")
            and conviction.get("enabled") and conviction.get("enforcement_enabled")
            and conviction.get("mode") == "operational_paper"
            and sizing.get("enabled") and sizing.get("mode") == "operational_paper"
            and sizing.get("operational_enforcement") is True
            and sizing.get("allow_order_size_change") is True
        )

    def _record_adaptive_sizing(
        self,
        proposal: dict[str, Any],
        sizing: dict[str, Any],
        portfolio: dict[str, Any],
        adaptive_conviction: dict[str, Any],
        *,
        stage: str,
        approval_id: str | None = None,
        displayed_adaptive_ceiling: float | None = None,
        proposal_adaptive_notional: float | None = None,
        final_revalidation_blocked: bool = False,
        missing_inputs: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Gather authoritative inputs; all sizing formulae remain in the engine."""
        if proposal.get("action") not in {"entry", "add"} or str(proposal.get("side") or "").lower() != "buy":
            return None
        equity = float(portfolio.get("portfolio_equity") or 0.0)
        operational_notional = float(sizing.get("final_notional") or proposal.get("notional") or 0.0)
        proposal_notional = float(proposal.get("notional") or operational_notional)
        proposal_pct = proposal_notional / equity * 100.0 if equity > 0 else None
        current_gross = None if proposal_pct is None else max(0.0, float(portfolio.get("proposed_total_exposure_pct") or 0.0) - proposal_pct)
        current_symbol = None if proposal_pct is None else max(0.0, float(portfolio.get("proposed_symbol_exposure_pct") or 0.0) - proposal_pct)
        current_cluster = None if proposal_pct is None else max(0.0, float(portfolio.get("proposed_cluster_exposure_pct") or 0.0) - proposal_pct)
        current_heat = None
        if equity > 0:
            current_heat = (
                float(portfolio.get("held_open_stop_risk") or 0.0)
                + float(portfolio.get("active_reserved_stop_risk") or 0.0)
                + float(portfolio.get("pending_buy_stop_risk") or 0.0)
            ) / equity * 100.0
        phase3_profile = (self.config.get("phase3", {}) or {}).get("risk_profile", {}) or {}
        engine = AdaptiveSizingEngine(self.config)
        decision = engine.evaluate({
            "evaluation_time": iso_now(),
            "stage": stage, "run_id": proposal.get("run_id") or self.run_id,
            "proposal_id": proposal.get("id") or proposal.get("proposal_id"),
            "candidate_id": proposal.get("signal_id"), "setup_id": proposal.get("setup_key"),
            "approval_id": approval_id, "strategy_version": proposal.get("strategy_version"),
            "policy_id": proposal.get("policy_decision_id"), "action": proposal.get("action"), "side": proposal.get("side"),
            "adaptive_conviction": adaptive_conviction, "operational_sizing": sizing,
            "authoritative_equity": equity, "authoritative_cash": portfolio.get("cash"),
            "authoritative_buying_power": portfolio.get("buying_power"),
            "canonical_risk_snapshot": {
                "held_open_stop_risk": portfolio.get("held_open_stop_risk"),
                "pending_buy_stop_risk": portfolio.get("pending_buy_stop_risk"),
                "pending_buy_notional": portfolio.get("pending_buy_notional"),
            },
            "active_reservations": {
                "notional": portfolio.get("active_reserved_exposure"),
                "stop_risk": portfolio.get("active_reserved_stop_risk"),
            },
            "entry_price": proposal.get("latest_price") or proposal.get("proposal_price"),
            "stop_price": proposal.get("stop_price") or sizing.get("stop_price"),
            "stop_distance_dollars": proposal.get("stop_distance_dollars") or sizing.get("stop_distance_dollars"),
            "strategy_policy_state": sizing.get("strategy_state") or proposal.get("strategy_state"),
            "strategy_policy_version": sizing.get("strategy_policy_version") or proposal.get("strategy_policy_version"),
            "current_portfolio_heat_pct": current_heat, "current_gross_exposure_pct": current_gross,
            "current_symbol_exposure_pct": current_symbol, "current_cluster_exposure_pct": current_cluster,
            "hard_limits_pct": {
                "portfolio_heat": phase3_profile.get("max_portfolio_heat_pct"),
                "gross_exposure": phase3_profile.get("hard_gross_exposure_pct"),
                "symbol_exposure": phase3_profile.get("max_symbol_exposure_pct"),
                "cluster_exposure": phase3_profile.get("max_cluster_exposure_pct"),
            },
            "displayed_adaptive_ceiling": displayed_adaptive_ceiling,
            "displayed_quantity_ceiling": (
                proposal.get("approved_quantity_ceiling") or proposal.get("qty")
            ) if stage == "final_revalidation" else None,
            "displayed_stop_risk_dollars": (
                proposal.get("approved_stop_risk_ceiling") or proposal.get("stop_risk_dollars")
            ) if stage == "final_revalidation" else None,
            "proposal_adaptive_notional": proposal_adaptive_notional,
            "final_revalidation_blocked": final_revalidation_blocked,
            "missing_inputs": list(missing_inputs or []),
            "integrity": {
                "pending_buy_exposure_unknown": portfolio.get("pending_buy_exposure_unknown"),
                "reconciliation_checked": True,
            },
        })
        if decision is None:
            return None
        engine.persist(self.storage, decision)
        summary = decision.summary()
        self.storage.audit(self.run_id, "adaptive_sizing_operational_paper", summary)
        return summary

    def replay_adaptive_conviction(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Read-only engine replay; callers supply immutable historical inputs."""
        return AdaptiveConvictionEngine(self.config).replay(records)

    def _process_sleep_mode_emergency_timeouts(self) -> None:
        timed_out = self.storage.fetch_all(
            """
            SELECT *
            FROM trade_proposals
            WHERE status='pending'
              AND emergency_exit_triggered=1
              AND emergency_exit_mode='sleep'
              AND emergency_exit_auto_execute_due_at IS NOT NULL
              AND emergency_exit_auto_execute_due_at <= ?
            """,
            (iso_now(),),
        )
        for row in timed_out:
            proposal_id = row["id"]
            symbol = row["symbol"]
            self.storage.execute(
                """UPDATE trade_proposals SET emergency_exit_auto_execute_due_at=NULL,
                   emergency_exit_auto_execute_attempted_at=?,emergency_exit_final_decision='approval_required'
                   WHERE id=? AND status='pending'""",
                (iso_now(), proposal_id),
            )
            self.storage.audit(self.run_id, "emergency_exit_auto_timeout_suppressed", {
                "symbol": symbol, "proposal_id": proposal_id,
                "reason": "manual approval is mandatory for every paper order",
            })

        stale_timed = self.storage.fetch_all(
            """
            SELECT id, symbol
            FROM trade_proposals
            WHERE status='pending'
              AND emergency_exit_triggered=1
              AND COALESCE(emergency_exit_mode,'')!='sleep'
              AND emergency_exit_auto_execute_due_at IS NOT NULL
              AND emergency_exit_auto_execute_due_at <= ?
            """,
            (iso_now(),),
        )
        for row in stale_timed:
            self.storage.execute(
                "UPDATE trade_proposals SET emergency_exit_auto_execute_due_at=NULL, emergency_exit_auto_execute_attempted_at=? WHERE id=?",
                (iso_now(), row["id"]),
            )
            self.storage.audit(self.run_id, "emergency_exit_auto_timeout_suppressed", {"symbol": row["symbol"], "proposal_id": row["id"], "reason": "non_sleep_mode"})

    def _create_rotation_group(
        self,
        exit_proposals: list[dict[str, Any]],
        contingent_candidates: list[dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any]:
        from .rotation_coordinator import RotationCoordinator

        maximum = int((self.config.get("rotation", {}) or {}).get("maximum_contingent_entries", 1))
        if maximum != 1:
            raise ValueError("operational rotation supports exactly one contingent entry")
        candidates: list[dict[str, Any]] = []
        for row in contingent_candidates[:maximum]:
            notional = max(0.0, float(row.get("final_notional") or 0.0))
            price = max(0.0, float(row.get("price") or 0.0))
            quantity = max(0.0, float(row.get("suggested_shares") or 0.0))
            stop_distance = max(0.0, float(row.get("stop_distance_dollars") or 0.0))
            if notional <= 0 or price <= 0 or quantity <= 0 or stop_distance <= 0:
                continue
            signal = row["signal"]
            strategy = str(signal.strategy_version)
            candidate_key = f"{row.get('setup_key')}:{strategy}:{signal.action}:{signal.side}"
            displayed_proposal = dict(row.get("performance_proposal_payload") or {})
            proposal_id = str(row.get("proposal_id") or displayed_proposal.get("id") or "")
            if not proposal_id or not displayed_proposal:
                continue
            candidates.append({
                "proposal_id": proposal_id,
                "candidate_key": candidate_key,
                "strategy_version": strategy,
                "symbol": row["symbol"],
                "side": "buy",
                "max_quantity": quantity,
                "max_notional": notional,
                "max_stop_risk": quantity * stop_distance,
                "payload": {
                    **displayed_proposal,
                    "candidate_key": candidate_key,
                    "score": row.get("score"),
                    "volatility_regime": row.get("volatility_regime"),
                    "price": price,
                    "stop_price": row.get("stop_price"),
                    "stop_distance_dollars": stop_distance,
                    "setup_key": row.get("setup_key"),
                    "strategy_version": strategy,
                    "strategy_registry_snapshot_id": displayed_proposal.get("strategy_registry_snapshot_id"),
                    "sleeve_allocation_id": displayed_proposal.get("sleeve_allocation_id"),
                    "strategy_sleeve": displayed_proposal.get("strategy_sleeve"),
                    "rank": row.get("final_candidate_rank"),
                    "displayed_at": now.isoformat(),
                },
            })
        if not candidates:
            raise ValueError("no current risk-qualified contingent entry is available")
        exits: list[dict[str, Any]] = []
        for proposal in exit_proposals:
            quantity = float(proposal.get("qty") or 0.0)
            if quantity <= 0:
                continue
            state = self._position_management_state(str(proposal["symbol"])) or {}
            stops = [
                float(value) for value in (
                    state.get("initial_stop_price"), state.get("trailing_stop_price"),
                    state.get("authoritative_protective_stop"),
                ) if value is not None and float(value) > 0
            ]
            mark = float(proposal.get("latest_price") or 0.0)
            exits.append({
                "proposal_id": proposal["id"],
                "position_lifecycle_id": state.get("position_lifecycle_id") or proposal.get("position_lifecycle_id"),
                "symbol": proposal["symbol"],
                "side": "sell",
                "quantity": quantity,
                "estimated_notional": float(proposal.get("notional") or 0.0),
                "estimated_released_risk": quantity * max(0.0, mark - max(stops)) if stops else 0.0,
                "reason": proposal.get("reason"),
                "position_state": state.get("management_mode") or state.get("last_decision_type"),
            })
        expiry = now + timedelta(minutes=float((self.config.get("rotation", {}) or {}).get("group_expiry_minutes", 15)))
        for proposal in exit_proposals:
            try:
                expiry = min(expiry, _parse_datetime(proposal["expires_at"]))
            except Exception:
                pass
        coordinator = RotationCoordinator(self.storage, config_hash=self.config.get("effective_config_hash"))
        group = coordinator.create_group(
            run_id=self.run_id,
            exit_legs=exits,
            contingent_entries=candidates,
            expires_at=expiry,
            registry_snapshot_id=self._ensure_strategy_registry_snapshot(),
            allocation_id=(self._phase4_allocation_cache or {}).get("allocation_id"),
            evaluation_time=now,
        )
        for step in coordinator.steps(group["id"]):
            proposal_rows = self.storage.fetch_all("SELECT payload FROM trade_proposals WHERE id=?", (step["proposal_id"],))
            payload = json.loads(proposal_rows[0]["payload"] or "{}") if proposal_rows else {}
            payload.update({
                "rotation_group_id": group["id"], "rotation_step_id": step["id"],
                "relationship_type": "rotation_exit", "order_role": "rotation_exit",
            })
            self.storage.execute(
                """UPDATE trade_proposals SET rotation_group_id=?,rotation_step_id=?,relationship_type=?,payload=?
                   WHERE id=? AND status='pending'""",
                (group["id"], step["id"], "rotation_exit", json_dumps(payload), step["proposal_id"]),
            )
        for entry in coordinator.entries(group["id"]):
            if not entry.get("proposal_id"):
                continue
            proposal_rows = self.storage.fetch_all(
                "SELECT payload FROM trade_proposals WHERE id=?", (entry["proposal_id"],)
            )
            payload = json.loads(proposal_rows[0]["payload"] or "{}") if proposal_rows else {}
            payload.update({
                "rotation_group_id": group["id"], "rotation_step_id": entry["id"],
                "relationship_type": "rotation_entry", "order_role": "rotation_entry",
            })
            self.storage.execute(
                """UPDATE trade_proposals SET rotation_group_id=?,rotation_step_id=?,relationship_type=?,payload=?
                   WHERE id=? AND status='pending'""",
                (group["id"], entry["id"], "rotation_entry", json_dumps(payload), entry["proposal_id"]),
            )
        return group

    def _format_rotation_group_message(self, group: dict[str, Any]) -> str:
        from .rotation_coordinator import RotationCoordinator

        coordinator = RotationCoordinator(self.storage, config_hash=self.config.get("effective_config_hash"))
        exits = coordinator.steps(group["id"])
        entries = coordinator.entries(group["id"])
        exit_lines = []
        estimated_risk = 0.0
        for row in exits:
            payload = json.loads(row.get("payload") or "{}")
            estimated_risk += float(payload.get("estimated_released_risk") or 0.0)
            exit_lines.append(
                f"Exit first: {row['symbol']} {float(row.get('requested_quantity') or 0):.4f} shares"
                f" — {payload.get('reason') or 'risk reduction'}"
                f" (state: {payload.get('position_state') or 'current held position'})"
            )
        entry_lines = [
            f"Contingent entry: {row['symbol']} up to {float(row['displayed_max_quantity']):.4f} shares / ${float(row['displayed_max_notional']):.2f} / ${float(row['displayed_max_stop_risk']):.2f} stop risk"
            for row in entries
        ]
        code = str(group["id"])[:8]
        return "\n".join([
            "🔄 Paper exit-first rotation proposal",
            *exit_lines,
            *entry_lines,
            f"Estimated release: ${float(group.get('estimated_release_notional') or 0.0):.2f}; this is not available capacity yet.",
            f"Estimated stop risk released: ${estimated_risk:.2f}; actual fill and current stop will replace this estimate.",
            "Sequence: exit submission → authoritative fill → reconciliation → fresh entry validation.",
            "A partial fill can only reduce or block the contingent entry. No pre-fill BUY capital is reserved.",
            f"Expires: {format_sgt(_parse_datetime(group['expires_at']))}.",
            f"Reply exactly: APPROVE ROTATION {code}",
            f"To reject: REJECT ROTATION {code}",
        ])

    def _prepare_rotation_proposal_approval(
        self,
        *,
        row: dict[str, Any],
        sender: str,
        raw_text: str,
        targeting_method: str,
        safe_detail: str,
    ) -> tuple[str, dict[str, Any]]:
        """Create or resume the one durable derived approval for a group leg."""
        workflow_store = ApprovalWorkflowStore(self.storage)
        existing = self.storage.fetch_all(
            """SELECT w.*,a.consumed_at,a.sender_id,a.raw_message
               FROM approval_workflows w JOIN approvals a ON a.id=w.approval_id
               WHERE w.proposal_id=? AND a.proposal_targeting_method=?
               ORDER BY w.created_at DESC LIMIT 1""",
            (row["id"], targeting_method),
        )
        if existing:
            workflow = existing[0]
            approval_id = str(workflow["approval_id"])
        else:
            if str(row.get("status")) != "pending":
                raise ValueError("rotation proposal has no recoverable grouped approval")
            approval_id = str(uuid.uuid4())
            workflow = workflow_store.accept_approval(
                approval_id=approval_id,
                run_id=self.run_id,
                proposal_id=str(row["id"]),
                sender_id=sender,
                raw_message=raw_text,
                parsed_action="approve",
                telegram_update_id=None,
                reply_to_message_id=None,
                targeting_method=targeting_method,
                acknowledgement_status="received",
                approval_received_at=iso_now(),
            )
        state = ApprovalWorkflowState(workflow["state"])
        if state == ApprovalWorkflowState.TARGET_RESOLVED:
            workflow = workflow_store.transition(
                workflow["id"], ApprovalWorkflowState.VALIDATING,
                expected_state=ApprovalWorkflowState.TARGET_RESOLVED,
                safe_detail=safe_detail,
            )
            state = ApprovalWorkflowState.VALIDATING
        if state not in {
            ApprovalWorkflowState.VALIDATING,
            ApprovalWorkflowState.APPROVED_PENDING_INTENT,
        }:
            raise ValueError(f"rotation approval workflow is not resumable from {state.value}")
        if self._check_stale_listener_block(str(row.get("symbol") or ""), approval_id):
            if state == ApprovalWorkflowState.VALIDATING:
                workflow_store.transition(
                    workflow["id"], ApprovalWorkflowState.BLOCKED,
                    safe_detail="listener freshness blocked rotation approval",
                )
            raise ValueError("listener is running stale code")
        if str(row.get("status")) == "pending":
            if not self.storage.consume_approval(str(row["id"]), approval_id):
                raise ValueError("rotation approval could not be consumed")
        else:
            approval_rows = self.storage.fetch_all(
                "SELECT consumed_at FROM approvals WHERE id=?", (approval_id,)
            )
            if str(row.get("status")) != "approved" or not approval_rows or not approval_rows[0].get("consumed_at"):
                raise ValueError("rotation proposal approval state is inconsistent")
        return approval_id, workflow_store.get(str(workflow["id"]))

    def _approve_rotation_exit_step(
        self,
        *,
        group_id: str,
        step: dict[str, Any],
        sender: str,
        raw_text: str,
    ) -> tuple[bool, str | None]:
        from .rotation_coordinator import RotationCoordinator, RotationState, TERMINAL_STATES

        coordinator = RotationCoordinator(
            self.storage, config_hash=self.config.get("effective_config_hash")
        )
        try:
            group = coordinator.get_group(group_id)
        except KeyError:
            return False, "rotation group is unavailable"
        if (
            RotationState(group["state"]) in TERMINAL_STATES
            or _parse_datetime(group["expires_at"]) <= datetime.now(UTC)
        ):
            return False, "rotation group expired or became terminal before exit submission"

        rows = self.storage.fetch_all(
            "SELECT * FROM trade_proposals WHERE id=? AND status IN ('pending','approved')", (step["proposal_id"],)
        )
        if not rows:
            return False, "rotation exit proposal is no longer recoverable"
        row = rows[0]
        workflow_store = ApprovalWorkflowStore(self.storage)
        linked = self.storage.fetch_all(
            """SELECT id FROM order_intents WHERE proposal_id=? AND relationship_group_id=?
               AND relationship_type='rotation_exit' ORDER BY created_at DESC LIMIT 1""",
            (row["id"], group_id),
        )
        if linked:
            coordinator.record_exit_submitted(
                group_id, step_id=step["id"], intent_id=str(linked[0]["id"])
            )
            return True, str(linked[0]["id"])
        try:
            approval_id, workflow = self._prepare_rotation_proposal_approval(
                row=row, sender=sender, raw_text=raw_text,
                targeting_method="rotation_group",
                safe_detail="explicit rotation group approval validating exit leg",
            )
        except ValueError as exc:
            return False, str(exc)
        proposal = _hydrate_proposal_row(row)
        proposal.update({
            "rotation_group_id": group_id, "relationship_group_id": group_id,
            "rotation_step_id": step["id"], "relationship_type": "rotation_exit",
            "order_role": "rotation_exit",
        })
        result, *_ = self._execute_final_revalidation(
            row, proposal, str(row["symbol"]), "sell", False, approval_id
        )
        if not result.submitted or not result.intent_id:
            workflow_now = workflow_store.get(workflow["id"])
            if workflow_now["state"] == ApprovalWorkflowState.VALIDATING.value:
                workflow_store.transition(workflow["id"], ApprovalWorkflowState.BLOCKED, safe_detail=result.reason)
            self.storage.execute("UPDATE trade_proposals SET status='blocked' WHERE id=?", (row["id"],))
            return False, result.reason
        workflow_now = workflow_store.get(workflow["id"])
        if workflow_now["state"] == ApprovalWorkflowState.INTENT_CREATED.value:
            workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
            workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
            workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMITTED)
        self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=?", (row["id"],))
        coordinator.record_exit_submitted(
            group_id, step_id=step["id"], intent_id=result.intent_id
        )
        return True, result.intent_id

    def _handle_rotation_command(self, text: str, sender: str) -> bool:
        from .rotation_coordinator import RotationCoordinator, parse_rotation_approval

        normalized = " ".join(text.strip().lower().split())
        if not re.fullmatch(r"(approve|reject) rotation [a-f0-9]{6,64}", normalized):
            return False
        if not self.telegram.is_authorized(sender):
            self.storage.audit(self.run_id, "rotation_command_ignored_unauthorized", {"sender_id": sender})
            return True
        coordinator = RotationCoordinator(self.storage, config_hash=self.config.get("effective_config_hash"))
        pending = self.storage.fetch_all(
            "SELECT * FROM rotation_groups WHERE state='pending_group_approval' ORDER BY created_at"
        )
        parsed = parse_rotation_approval(text, pending)
        if not parsed.accepted or not parsed.group_id:
            self.telegram.send_message(f"Rotation command not accepted: {parsed.reason}.")
            return True
        if parsed.action == "reject":
            coordinator.reject(parsed.group_id, reason="explicit grouped rejection")
            for step in coordinator.steps(parsed.group_id):
                self.storage.execute("UPDATE trade_proposals SET status='rejected' WHERE id=? AND status='pending'", (step["proposal_id"],))
            self.telegram.send_message("Rotation rejected. No exit or contingent entry was submitted.")
            return True
        approval_id = str(uuid.uuid4())
        coordinator.approve(parsed.group_id, approval_id=approval_id, sender_id=sender, command=text)
        self.telegram.send_message("Rotation approval received. Running final checks and submitting exit legs first; no contingent BUY is reserved yet.")
        for step in coordinator.steps(parsed.group_id):
            submitted, reason = self._approve_rotation_exit_step(
                group_id=parsed.group_id, step=step, sender=sender, raw_text=text
            )
            if not submitted:
                coordinator.fail_exit(parsed.group_id, reason=reason or "rotation exit validation failed")
                self.telegram.send_message(f"Rotation terminated before entry. Exit leg was blocked: {reason or 'final validation failed'}.")
                return True
        self.storage.execute(
            "UPDATE rotation_group_approvals SET consumed_at=?,status='exit_submitted' WHERE group_id=? AND approval_id=?",
            (iso_now(), parsed.group_id, approval_id),
        )
        self.telegram.send_message("Rotation exit submitted in paper mode. The contingent entry remains zero-capital until an authoritative fill and reconciliation.")
        return True

    def _send_rotation_lifecycle_events(self) -> None:
        from .rotation_coordinator import RotationCoordinator

        coordinator = RotationCoordinator(self.storage, config_hash=self.config.get("effective_config_hash"))
        rows = self.storage.fetch_all(
            """SELECT group_id,event_key,event_type,safe_detail FROM rotation_events
               WHERE notification_sent_at IS NULL ORDER BY created_at,id"""
        )
        labels = {
            "exit_submitted": "Rotation exit submitted in paper mode. The contingent entry remains zero-capital until authoritative fill reconciliation.",
            "exit_partially_filled": "Rotation exit partially filled. Only actual released capacity will be considered.",
            "exit_filled": "Rotation exit filled. Authoritative account and position reconciliation is now required.",
            "reconciliation_complete": "Rotation reconciliation complete. The contingent entry is being revalidated from fresh data.",
            "contingent_entry_revalidated": "Rotation contingent entry revalidated without enlarging its displayed ceiling.",
            "entry_reduced": "Rotation contingent entry was reduced by fresh post-fill limits; it was not enlarged.",
            "entry_reserved": "Rotation contingent entry passed atomic post-fill reservation.",
            "entry_submitted": "Rotation contingent entry submitted in paper mode.",
            "completed": "Rotation group completed.",
            "entry_blocked": "Rotation contingent entry was blocked or stopped before a complete fill. No additional BUY will be submitted; any partial fill remains in the linked intent ledger.",
            "late_exit_fill_reconciled": "A later rotation exit fill was reconciled. Its additional capacity was not reused by the completed dependency decision.",
            "exit_failed": "Rotation terminated because the exit failed. No contingent BUY was submitted.",
            "expired": "Rotation expired. No further action is authorized.",
            "cancelled": "Rotation cancelled. No further dependent action is authorized.",
        }
        for row in rows:
            message = labels.get(str(row["event_type"]))
            if not message or not coordinator.claim_notification(row["group_id"], row["event_key"]):
                continue
            try:
                self.telegram.send_message(message)
                coordinator.mark_notification_sent(row["group_id"], row["event_key"])
            except Exception:
                coordinator.release_notification_claim(row["group_id"], row["event_key"])
                continue

    def _submit_revalidated_rotation_entry(self, group: dict[str, Any]) -> None:
        """Freshly size and submit the one displayed contingent entry."""
        from .rotation_coordinator import RotationCoordinator, RotationState

        coordinator = RotationCoordinator(self.storage, config_hash=self.config.get("effective_config_hash"))
        entries = [row for row in coordinator.entries(group["id"]) if row["state"] == "contingent"]
        if len(entries) != 1 or not coordinator.approval_is_current(group["id"]):
            coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason="current grouped approval or contingent entry is unavailable")
            return
        entry = entries[0]
        payload = json.loads(entry.get("payload") or "{}")
        symbol = str(entry["symbol"]).upper()
        strategy = str(entry["strategy_version"])
        action = str(payload.get("action") or "entry").lower()
        is_add = bool(payload.get("is_add")) or action == "add"
        try:
            runtime = self._authoritative_runtime_state(force=True)
            quote = validated_quote(self.broker, symbol, self.config, now=datetime.now(UTC))
            price = float(quote["ask"])
            bars = normalize_bars(self.broker.get_historical_bars(symbol, "1Day", 250), symbol)
            clock = self.broker.get_clock()
            strategy_config = {
                "maximum_volatility_20d": 0.45,
                "stop_drawdown_pct": 0.08,
                **((self.config.get("strategies", {}) or {}).get(strategy, {}) or {}),
            }
            fresh_signal = evaluate_symbol(
                symbol, bars, False, False, bool(_value(clock, "is_open", False)),
                float(strategy_config["maximum_volatility_20d"]),
                float(strategy_config["stop_drawdown_pct"]),
                position_drawdown_pct=0.0,
            )
            if fresh_signal.action != "ENTRY" or fresh_signal.side != "buy" or fresh_signal.strategy_version != strategy:
                raise ValueError("fresh strategy no longer emits the displayed BUY setup")
            fresh_now = datetime.now(UTC)
            fresh_price_at = _parse_datetime(quote["timestamp"])
            spy_return = None
            try:
                spy_bars = normalize_bars(self.broker.get_historical_bars("SPY", "1Day", 50), "SPY")
                if len(spy_bars) >= 20:
                    spy_return = float(spy_bars["close"].iloc[-1] / spy_bars["close"].iloc[-20]) - 1.0
            except Exception:
                pass
            asset_score = self._calculate_asset_selection_score(
                symbol, bars, fresh_price_at, fresh_signal, fresh_now, spy_return
            )
            score_components = self._canonical_trade_score_components(
                symbol=symbol, signal=fresh_signal, price=price, price_at=fresh_price_at,
                bars=bars, asset_score=asset_score, now=fresh_now,
            )
            fresh_regime = str(score_components["volatility_regime"])
            fresh_score = float(score_components["score"])
            fresh_setup_key = self._compute_setup_key(
                symbol, "buy", "ENTRY", fresh_signal.indicators, fresh_score
            )
            fresh_candidate_key = f"{fresh_setup_key}:{strategy}:ENTRY:buy"
            if fresh_candidate_key != str(entry["candidate_key"]):
                raise ValueError("contingent candidate changed materially; a new approval is required")
            snapshot = self._get_exposure_snapshot(runtime.get("positions", []), runtime.get("account"))
            self._phase4_allocation_cache = None
            size = self._calculate_dynamic_size(
                symbol, fresh_score, fresh_regime, price, bars, snapshot,
                is_add=is_add, strategy_version=strategy,
            )
            requested_notional = min(
                float(entry["displayed_max_notional"]), float(size.get("final_notional") or 0.0)
            )
            requested_quantity = min(
                float(entry["displayed_max_quantity"]), requested_notional / price if price > 0 else 0.0
            )
            stop_distance = float(size.get("stop_distance_dollars") or 0.0)
            registry_snapshot_id = str(size.get("strategy_registry_snapshot_id") or "")
            allocation_id = str(size.get("sleeve_allocation_id") or "")
            revalidated = coordinator.revalidate_entry(
                group["id"], entry["id"], candidate_key=fresh_candidate_key,
                price=price, requested_quantity=requested_quantity,
                stop_risk_per_share=stop_distance,
                allocation_notional_cap=float(size.get("sleeve_notional_ceiling") or 0.0),
                allocation_risk_cap=float(size.get("sleeve_stop_risk_ceiling") or 0.0),
                other_available_cash=min(
                    float(_value(runtime.get("account"), "cash", 0.0) or 0.0),
                    float(_value(runtime.get("account"), "buying_power", 0.0) or 0.0),
                ),
                minimum_notional=float((self.config.get("position_sizing", {}) or {}).get("minimum_executable_notional_usd", 1.0)),
                registry_snapshot_id=registry_snapshot_id, allocation_id=allocation_id,
            )
        except Exception as exc:
            coordinator.transition(
                group["id"], RotationState.ENTRY_BLOCKED,
                reason=f"fresh contingent entry validation unavailable ({type(exc).__name__})",
            )
            return
        if not revalidated.allowed:
            return
        proposal_id = str(entry.get("proposal_id") or "")
        proposal_rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,))
        if len(proposal_rows) != 1:
            coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason="displayed contingent proposal is unavailable")
            return
        row = proposal_rows[0]
        proposal = _hydrate_proposal_row(row)
        proposal.update({
            "id": proposal_id, "proposal_id": proposal_id,
            "symbol": symbol, "side": "buy", "action": action, "is_add": is_add,
            "notional": revalidated.final_notional, "qty": revalidated.final_quantity,
            "approved_notional_ceiling": revalidated.final_notional,
            "approved_quantity_ceiling": revalidated.final_quantity,
            "approved_stop_risk_ceiling": revalidated.final_stop_risk,
            "latest_price": price, "price_at": quote["timestamp"],
            "score": fresh_score, "volatility_regime": fresh_regime,
            "stop_price": size.get("stop_price"), "stop_distance_dollars": stop_distance,
            "stop_validation_status": size.get("stop_validation_status"),
            "stop_model_used": size.get("stop_model_used"), "atr_value": size.get("atr_value"),
            "technical_stop_price": size.get("technical_stop_price"),
            "stop_risk_dollars": revalidated.final_stop_risk,
            "strategy_registry_snapshot_id": registry_snapshot_id,
            "strategy_sleeve": size.get("strategy_sleeve"), "sleeve_allocation_id": allocation_id,
            "sleeve_stop_risk_ceiling": size.get("sleeve_stop_risk_ceiling"),
            "sleeve_notional_ceiling": size.get("sleeve_notional_ceiling"),
            "rotation_group_id": group["id"], "relationship_group_id": group["id"],
            "rotation_step_id": entry["id"], "relationship_type": "rotation_entry",
            "order_role": "rotation_entry", "cluster_name": self._get_symbol_cluster(symbol),
        })
        self.storage.execute(
            """UPDATE trade_proposals SET notional=?,payload=?,rotation_group_id=?,rotation_step_id=?,relationship_type=?
               WHERE id=? AND status IN ('pending','approved')""",
            (proposal["notional"], json_dumps(proposal), group["id"], entry["id"], "rotation_entry", proposal_id),
        )
        row = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=?", (proposal_id,))[0]
        approval_rows = self.storage.fetch_all(
            """SELECT * FROM rotation_group_approvals WHERE group_id=?
               AND status='exit_submitted' AND consumed_at IS NOT NULL ORDER BY created_at DESC LIMIT 1""",
            (group["id"],),
        )
        if not approval_rows:
            coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason="durable consumed group approval is missing")
            return
        linked_intents = self.storage.fetch_all(
            """SELECT id,state FROM order_intents WHERE proposal_id=? AND relationship_group_id=?
               AND relationship_type='rotation_entry' ORDER BY created_at DESC LIMIT 1""",
            (proposal_id, group["id"]),
        )
        if linked_intents:
            intent_id = str(linked_intents[0]["id"])
            coordinator.record_entry_reserved(group["id"], entry["id"], intent_id=intent_id)
            if str(linked_intents[0]["state"]) in {"submitted", "partially_filled", "filled", "cancel_pending"}:
                coordinator.record_entry_submitted(group["id"], entry["id"], intent_id=intent_id)
            return
        try:
            approval_id, _workflow = self._prepare_rotation_proposal_approval(
                row=row,
                sender=str(approval_rows[0]["sender_id"]),
                raw_text=str(approval_rows[0]["command"]),
                targeting_method="rotation_group_contingent",
                safe_detail="approved rotation contingent entry final validation",
            )
        except ValueError as exc:
            coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason=str(exc))
            return
        result, *_ = self._execute_final_revalidation(
            row, proposal, symbol, "buy", is_add, approval_id,
            rotation_dependency_authorized=True,
        )
        if not result.submitted or not result.intent_id:
            coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason=result.reason or "contingent entry blocked")
            self.storage.execute("UPDATE trade_proposals SET status='blocked' WHERE id=?", (proposal_id,))
            return
        coordinator.record_entry_reserved(group["id"], entry["id"], intent_id=result.intent_id)
        coordinator.record_entry_submitted(group["id"], entry["id"], intent_id=result.intent_id)
        self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=?", (proposal_id,))

    def _advance_rotation_workflows(self) -> None:
        from .rotation_coordinator import RotationCoordinator, RotationState

        coordinator = RotationCoordinator(self.storage, config_hash=self.config.get("effective_config_hash"))
        coordinator.expire_stale()
        for action in coordinator.recovery_actions():
            group = coordinator.get_group(action["group_id"])
            state = RotationState(group["state"])
            if state == RotationState.APPROVED_EXIT_PENDING:
                approvals = self.storage.fetch_all(
                    """SELECT * FROM rotation_group_approvals WHERE group_id=?
                       ORDER BY created_at DESC LIMIT 1""",
                    (group["id"],),
                )
                if not approvals:
                    coordinator.fail_exit(group["id"], reason="durable grouped approval disappeared")
                    continue
                approval = approvals[0]
                recovery_failed = False
                for step in coordinator.steps(group["id"]):
                    linked = self.storage.fetch_all(
                        """SELECT id,state FROM order_intents WHERE proposal_id=?
                           AND relationship_group_id=? AND relationship_type='rotation_exit'
                           ORDER BY created_at DESC LIMIT 1""",
                        (step["proposal_id"], group["id"]),
                    )
                    if linked:
                        coordinator.record_exit_submitted(
                            group["id"], step_id=step["id"], intent_id=str(linked[0]["id"])
                        )
                        continue
                    submitted, reason = self._approve_rotation_exit_step(
                        group_id=group["id"], step=step,
                        sender=str(approval["sender_id"]), raw_text=str(approval["command"]),
                    )
                    if not submitted:
                        coordinator.fail_exit(group["id"], reason=reason or "recovered exit validation failed")
                        recovery_failed = True
                        break
                if recovery_failed:
                    continue
                self.storage.execute(
                    """UPDATE rotation_group_approvals SET consumed_at=COALESCE(consumed_at,?),status='exit_submitted'
                       WHERE group_id=? AND approval_id=?""",
                    (iso_now(), group["id"], approval["approval_id"]),
                )
                group = coordinator.get_group(group["id"])
                state = RotationState(group["state"])
            if state in {RotationState.EXIT_SUBMITTED, RotationState.EXIT_PARTIALLY_FILLED}:
                self.storage.execute(
                    """UPDATE rotation_group_approvals SET consumed_at=COALESCE(consumed_at,?),status='exit_submitted'
                       WHERE group_id=? AND status='active'""",
                    (iso_now(), group["id"]),
                )
                missing_steps = [step for step in coordinator.steps(group["id"]) if not step.get("intent_id")]
                if missing_steps:
                    approval_rows = self.storage.fetch_all(
                        """SELECT * FROM rotation_group_approvals WHERE group_id=?
                           AND status IN ('active','exit_submitted') ORDER BY created_at DESC LIMIT 1""",
                        (group["id"],),
                    )
                    if not approval_rows:
                        coordinator.fail_exit(group["id"], reason="approved exit leg recovery lost grouped approval")
                        continue
                    missing_failed = False
                    for missing_step in missing_steps:
                        submitted, reason = self._approve_rotation_exit_step(
                            group_id=group["id"], step=missing_step,
                            sender=str(approval_rows[0]["sender_id"]),
                            raw_text=str(approval_rows[0]["command"]),
                        )
                        if not submitted:
                            coordinator.fail_exit(group["id"], reason=reason or "approved exit leg recovery failed")
                            missing_failed = True
                            break
                    if missing_failed:
                        continue
                    group = coordinator.get_group(group["id"])
                    state = RotationState(group["state"])

            exit_tracking_states = {
                RotationState.EXIT_SUBMITTED, RotationState.EXIT_PARTIALLY_FILLED,
                RotationState.EXIT_FILLED, RotationState.RECONCILIATION_PENDING,
                RotationState.RECONCILED, RotationState.ENTRY_REVALIDATING,
                RotationState.ENTRY_RESERVED, RotationState.ENTRY_SUBMITTED,
                RotationState.COMPLETED, RotationState.ENTRY_BLOCKED,
                RotationState.EXIT_FAILED, RotationState.EXPIRED,
                RotationState.CANCELLED,
            }
            if state in exit_tracking_states:
                terminal_without_fill: str | None = None
                allow_partial = bool((self.config.get("rotation", {}) or {}).get("allow_partial_fill_reallocation"))
                for step in coordinator.steps(group["id"]):
                    if not step.get("intent_id"):
                        continue
                    rows = self.storage.fetch_all(
                        """SELECT state,requested_quantity,filled_quantity,reference_price,average_fill_price
                           FROM order_intents WHERE id=?""", (step["intent_id"],)
                    )
                    if not rows:
                        coordinator.fail_exit(group["id"], reason="rotation exit intent linkage disappeared")
                        break
                    intent = rows[0]
                    intent_state = str(intent["state"])
                    filled = float(intent.get("filled_quantity") or 0.0)
                    if filled > float(step.get("filled_quantity") or 0.0) + 1e-12:
                        fill_price = float(intent.get("average_fill_price") or intent.get("reference_price") or 0.0)
                        pm_state = self._position_management_state(step["symbol"]) or {}
                        stops = [float(value) for value in (
                            pm_state.get("initial_stop_price"), pm_state.get("trailing_stop_price"),
                            pm_state.get("authoritative_protective_stop"),
                        ) if value is not None and float(value) > 0]
                        released_risk = filled * max(0.0, fill_price - max(stops)) if stops else 0.0
                        complete = intent_state == "filled" or filled + 1e-12 >= float(intent.get("requested_quantity") or 0.0)
                        coordinator.record_exit_fill(
                            group["id"], intent_id=step["intent_id"], cumulative_quantity=filled,
                            cumulative_notional=filled * fill_price, released_risk=released_risk,
                            exit_complete=complete,
                        )
                    if intent_state in {"rejected", "cancelled", "expired"} and filled <= 0:
                        terminal_without_fill = intent_state
                        break
                    if (
                        intent_state in {"rejected", "cancelled", "expired"}
                        and filled > 0
                        and filled + 1e-12 < float(intent.get("requested_quantity") or 0.0)
                    ):
                        coordinator.record_exit_terminal(
                            group["id"], intent_id=step["intent_id"], terminal_state=intent_state
                        )
                        if state not in {
                            RotationState.COMPLETED, RotationState.ENTRY_BLOCKED,
                            RotationState.EXIT_FAILED, RotationState.EXPIRED,
                            RotationState.CANCELLED,
                        } and not allow_partial:
                            terminal_without_fill = f"partially filled then {intent_state}"
                            break
                if terminal_without_fill:
                    if state not in {
                        RotationState.COMPLETED, RotationState.ENTRY_BLOCKED,
                        RotationState.EXIT_FAILED, RotationState.EXPIRED,
                        RotationState.CANCELLED,
                    }:
                        coordinator.fail_exit(group["id"], reason=f"rotation exit {terminal_without_fill} without a fill")
                    continue
                group = coordinator.get_group(group["id"])
                if group["state"] == RotationState.EXIT_FILLED.value or (
                    allow_partial and group["state"] == RotationState.EXIT_PARTIALLY_FILLED.value
                ):
                    coordinator.begin_reconciliation(group["id"])
                    group = coordinator.get_group(group["id"])
                    state = RotationState(group["state"])
            if state == RotationState.EXIT_FILLED:
                coordinator.begin_reconciliation(group["id"])
                group = coordinator.get_group(group["id"])
                state = RotationState(group["state"])
            if state == RotationState.RECONCILIATION_PENDING:
                runtime = self._authoritative_runtime_state(force=True)
                account = runtime.get("account")
                cash = float(_value(account, "cash", -1.0) or -1.0)
                buying_power = float(_value(account, "buying_power", -1.0) or -1.0)
                fingerprint = hashlib.sha256(json_dumps({
                    "cash": cash, "buying_power": buying_power,
                    "positions": sorted((str(_value(p, "symbol", "")), float(_value(p, "qty", 0.0) or 0.0)) for p in runtime.get("positions", [])),
                    "as_of": iso_now(),
                }).encode()).hexdigest()
                coordinator.record_reconciliation(
                    group["id"], cash=cash, buying_power=buying_power,
                    snapshot_fingerprint=fingerprint,
                )
                group = coordinator.get_group(group["id"])
                state = RotationState(group["state"])
            if state == RotationState.RECONCILED:
                self._phase4_allocation_cache = None
                self._submit_revalidated_rotation_entry(group)
                group = coordinator.get_group(group["id"])
                state = RotationState(group["state"])
            if state in {
                RotationState.ENTRY_REVALIDATING,
                RotationState.ENTRY_RESERVED,
                RotationState.ENTRY_SUBMITTED,
            }:
                entries = coordinator.entries(group["id"])
                if len(entries) != 1:
                    coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason="rotation entry linkage is ambiguous")
                    continue
                entry = entries[0]
                intent_rows = self.storage.fetch_all(
                    """SELECT id,state FROM order_intents WHERE rotation_step_id=?
                       OR (proposal_id=? AND relationship_group_id=?)
                       ORDER BY created_at DESC LIMIT 1""",
                    (entry["id"], entry.get("proposal_id"), group["id"]),
                )
                if state == RotationState.ENTRY_REVALIDATING and not intent_rows:
                    coordinator.reset_entry_for_revalidation(group["id"])
                    self._phase4_allocation_cache = None
                    self._submit_revalidated_rotation_entry(coordinator.get_group(group["id"]))
                    continue
                if not intent_rows:
                    coordinator.transition(group["id"], RotationState.ENTRY_BLOCKED, reason="rotation entry intent linkage disappeared")
                    continue
                intent = intent_rows[0]
                intent_id = str(intent["id"])
                intent_state = str(intent["state"])
                if state == RotationState.ENTRY_REVALIDATING:
                    coordinator.record_entry_reserved(group["id"], entry["id"], intent_id=intent_id)
                    state = RotationState.ENTRY_RESERVED
                if state == RotationState.ENTRY_RESERVED and intent_state in {
                    "submitted", "partially_filled", "filled", "cancel_pending"
                }:
                    coordinator.record_entry_submitted(group["id"], entry["id"], intent_id=intent_id)
                    state = RotationState.ENTRY_SUBMITTED
                elif state == RotationState.ENTRY_RESERVED and intent_state in {"rejected", "cancelled", "expired"}:
                    coordinator.transition(
                        group["id"], RotationState.ENTRY_BLOCKED,
                        reason=f"rotation entry {intent_state} before submission reconciliation",
                    )
                    continue
                if state == RotationState.ENTRY_SUBMITTED:
                    if intent_state == "filled":
                        coordinator.complete(group["id"])
                    elif intent_state in {"rejected", "cancelled", "expired"}:
                        coordinator.transition(
                            group["id"], RotationState.ENTRY_BLOCKED,
                            reason=f"rotation entry {intent_state} before a complete fill",
                        )
        self._send_rotation_lifecycle_events()

    def process_telegram(self) -> None:
        self._recover_local_workflows()
        self.storage.expire_proposals()
        self._expire_pending_batches(notify=False)
        self._process_sleep_mode_emergency_timeouts()
        updates = self.telegram.get_updates(timeout=0)
        if not updates:
            self.notify_expired_proposals()
            self._expire_pending_batches(notify=True)
            record_heartbeat(
                self.storage,
                "listener_poll",
                "healthy",
                attempted_at=iso_now(),
                completed_at=iso_now(),
                successful_at=iso_now(),
                detail={"updates_processed": 0},
            )
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
            text = str(message.get("text", "")).strip()
            sender = str((message.get("from") or {}).get("id", ""))

            # 0. Durable inbox. The received cursor is represented by this committed
            # row; business processing remains independently recoverable.
            is_mock_bot = getattr(self.telegram, "is_mock", False) or "Mock" in type(self.telegram).__name__
            if not is_mock_bot and update_id is not None:
                intent, target_symbol, target_proposal_id, _ = self._approval_intent_from_text(text)
                safe_type = "command" if text.startswith("/") else ("approval" if intent != "unknown" else "other")
                state = self.storage.ingest_telegram_update(
                    int(update_id),
                    message_id=message.get("message_id"),
                    message_timestamp=message.get("date"),
                    safe_message_type=safe_type,
                    normalized_action=intent,
                    target_hint=target_proposal_id or target_symbol,
                    sender_authorized=bool(self.telegram.is_authorized(sender)),
                )
                if state == "processed":
                    continue

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
                cmd_parts = text.strip().split()
                cmd = cmd_parts[0].lower() if cmd_parts else ""
                if cmd == "/status":
                    status_text = HealthMonitor(self.storage, self.config).format_status()
                    
                    self.storage.audit(self.run_id, "telegram_command", {"command": "/status", "authorized": self.telegram.is_authorized(sender)})
                    self.telegram.send_message(status_text, str((message.get("chat") or {}).get("id", self.telegram.chat_id)))
                    continue
                if cmd == "/performance":
                    from .strategy_performance import StrategyPerformanceEngine

                    performance_text = StrategyPerformanceEngine(self.storage, self.config).format_report()
                    performance_text += "\n\n" + AdaptiveConvictionEngine(self.config).format_report(self.storage)
                    performance_text += "\n\n" + AdaptiveSizingEngine(self.config).format_report(self.storage)
                    performance_text += "\n\n" + self._format_strategy_allocation_report()
                    self.storage.audit(self.run_id, "telegram_command", {"command": "/performance", "authorized": True, "report_only": True})
                    self.telegram.send_message(performance_text, str((message.get("chat") or {}).get("id", self.telegram.chat_id)))
                    continue
                else:
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

            # Rotation commands are explicit group-targeted approvals and must
            # never fall through to the generic YES/single-proposal parser.
            if self._handle_rotation_command(text, sender):
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
            workflow_store = ApprovalWorkflowStore(self.storage)
            approval_workflow = None
            if parsed.accepted and parsed.proposal_id:
                try:
                    approval_workflow = workflow_store.accept_approval(
                        approval_id=approval_id,
                        run_id=self.run_id,
                        proposal_id=str(parsed.proposal_id),
                        sender_id=sender,
                        raw_message=text,
                        parsed_action=parsed.action,
                        telegram_update_id=int(update_id) if update_id is not None else None,
                        reply_to_message_id=str(reply_to_message_id) if reply_to_message_id is not None else None,
                        targeting_method=targeting_method,
                        acknowledgement_status=ack_status,
                        approval_received_at=approval_received_at,
                    )
                    # Duplicate delivery reuses the original stable approval and
                    # workflow identity; never continue with the newly generated
                    # transient UUID.
                    approval_id = str(approval_workflow["approval_id"])
                except ApprovalWorkflowConflict:
                    self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                    continue
            else:
                self.storage.execute(
                    "INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at,reply_to_message_id,proposal_targeting_method,acknowledgement_status,approval_received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (approval_id, self.run_id, parsed.proposal_id, sender, text, parsed.action, int(self.telegram.is_authorized(sender)), "rejected", iso_now(), str(reply_to_message_id) if reply_to_message_id is not None else None, targeting_method, ack_status, approval_received_at),
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
            if approval_workflow is None:
                raise RuntimeError("accepted approval is missing its durable workflow")
            prop_symbol = row.get("symbol", "")
            prop_side = row.get("side", "").lower()
            if row.get("relationship_type") == "rotation_entry":
                workflow_store.transition(
                    approval_workflow["id"], ApprovalWorkflowState.BLOCKED,
                    safe_detail="generic approval cannot authorize a contingent rotation entry",
                )
                self.storage.execute(
                    """UPDATE approvals SET status='blocked',final_order_decision='blocked',
                       final_block_reason='rotation group command required' WHERE id=?""",
                    (approval_id,),
                )
                self.telegram.send_message(
                    "This is a contingent rotation entry. Only its exact APPROVE ROTATION command can authorize the dependency; no order was placed."
                )
                continue
            if parsed.action == "approve":
                approval_workflow = workflow_store.transition(
                    approval_workflow["id"],
                    ApprovalWorkflowState.VALIDATING,
                    expected_state=ApprovalWorkflowState.TARGET_RESOLVED,
                    safe_detail="final local validation started",
                )
            if parsed.action == "approve" and row.get("emergency_exit_triggered") != 1:
                proposal_for_sleep_check = _hydrate_proposal_row(row)
                if self._sleep_mode_blocks_approval(proposal_for_sleep_check):
                    msg = "Sleep mode is ON, so I did not process this BUY/ADD approval. Send awake first, then approve again if the proposal is still valid."
                    self.telegram.send_message(msg)
                    ack_sent = iso_now()
                    delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
                    self.storage.execute(
                        "UPDATE approvals SET status=?, acknowledgement_status='blocked', acknowledgement_sent_at=?, acknowledgement_delay_seconds=?, final_order_decision='blocked', final_block_reason=? WHERE id=?",
                        (f"sleep_blocked_{approval_id[:8]}", ack_sent, delay_sec, "sleep mode is active", approval_id),
                    )
                    workflow_store.transition(
                        approval_workflow["id"],
                        ApprovalWorkflowState.BLOCKED,
                        safe_detail="sleep mode blocked approval before intent creation",
                    )
                    continue

            if parsed.action == "reject":
                workflow_store.transition(
                    approval_workflow["id"],
                    ApprovalWorkflowState.BLOCKED,
                    safe_detail="operator rejected proposal; no intent permitted",
                )
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

                if self._check_stale_listener_block(prop_symbol, approval_id):
                    workflow_store.transition(
                        approval_workflow["id"],
                        ApprovalWorkflowState.BLOCKED,
                        safe_detail="listener freshness check blocked approval",
                    )
                    continue

                if not self.storage.consume_approval(parsed.proposal_id, approval_id):
                    workflow_store.transition(
                        approval_workflow["id"],
                        ApprovalWorkflowState.BLOCKED,
                        safe_detail="proposal approval was already consumed",
                    )
                    self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                    continue

                self.storage.audit(self.run_id, "emergency_exit_approved_by_user", {"symbol": prop_symbol, "proposal_id": parsed.proposal_id})

                proposal = _hydrate_proposal_row(row)
                success, err_reason = self.revalidate_and_execute_emergency_exit(proposal)
                if success:
                    workflow = workflow_store.get(approval_workflow["id"])
                    if workflow["state"] == ApprovalWorkflowState.INTENT_CREATED.value:
                        workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
                        workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
                        workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMITTED)
                    self.storage.execute("UPDATE trade_proposals SET status='approved', emergency_exit_final_decision='submitted', emergency_exit_user_response='yes' WHERE id=?", (parsed.proposal_id,))
                    self.telegram.send_message(f"✅ Paper order submitted: Sell {prop_symbol} for {proposal.get('qty', 0)} shares. Mode: paper only.")
                    self.storage.audit(self.run_id, "emergency_exit_submitted", {"symbol": prop_symbol, "score": row.get("emergency_exit_score")})
                else:
                    workflow = workflow_store.get(approval_workflow["id"])
                    workflow_state = ApprovalWorkflowState(workflow["state"])
                    if workflow_state == ApprovalWorkflowState.INTENT_CREATED:
                        workflow_store.transition(workflow["id"], ApprovalWorkflowState.UNKNOWN, safe_detail=err_reason)
                    elif workflow_state == ApprovalWorkflowState.VALIDATING:
                        workflow_store.transition(workflow["id"], ApprovalWorkflowState.BLOCKED, safe_detail=err_reason)
                    self.storage.execute("UPDATE trade_proposals SET status='blocked', emergency_exit_block_reason=?, emergency_exit_user_response='yes' WHERE id=?", (err_reason, parsed.proposal_id))
                    self.telegram.send_message(f"⚠️ Emergency exit was blocked. Reason: {err_reason}. No order was placed.")
                    self.storage.audit(self.run_id, "emergency_exit_blocked", {"symbol": prop_symbol, "reason": err_reason})
                continue

            # Send immediate acknowledgement message for YES
            self.telegram.send_message(f"✅ Received: YES for {prop_symbol} paper {prop_side} proposal. I will now run the final safety check. No order will be placed unless the final check passes.")
            ack_sent = iso_now()
            delay_sec = (datetime.fromisoformat(ack_sent.replace("Z", "+00:00")) - datetime.fromisoformat(approval_received_at.replace("Z", "+00:00"))).total_seconds()
            self.storage.execute("UPDATE approvals SET acknowledgement_sent_at=?, acknowledgement_delay_seconds=? WHERE id=?", (ack_sent, delay_sec, approval_id))

            if self._check_stale_listener_block(prop_symbol, approval_id):
                workflow_store.transition(
                    approval_workflow["id"],
                    ApprovalWorkflowState.BLOCKED,
                    safe_detail="listener freshness check blocked approval",
                )
                continue

            if not self.storage.consume_approval(parsed.proposal_id, approval_id):
                workflow_store.transition(
                    approval_workflow["id"],
                    ApprovalWorkflowState.BLOCKED,
                    safe_detail="proposal approval was already consumed",
                )
                self.telegram.send_message("I did not take any action because this proposal was already handled earlier.")
                continue

            final_revalidation_started_at = iso_now()
            proposal = _hydrate_proposal_row(row)
            is_add = proposal.get("action") == "add" or bool(proposal.get("is_add", False))

            result, refreshed_price_val, refreshed_price_at, price_refreshed_at, refreshed_price_age_seconds, price_move_bps_since_proposal = self._execute_final_revalidation(
                row, proposal, prop_symbol, prop_side, is_add, approval_id
            )

            final_revalidation_completed_at = iso_now()

            # Record order decision
            final_order_decision = "submitted" if result.submitted else ("unknown" if result.status == "unknown" else "blocked")
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
            self._persist_approval_directional_validation(approval_id, proposal)

            if result.intent_id:
                self.storage.link_executed_order_records(result.intent_id)
                self.storage.upsert_actual_trade_outcome_for_order(result.intent_id)

            if result.submitted:
                workflow = workflow_store.get(approval_workflow["id"])
                if workflow["state"] == ApprovalWorkflowState.INTENT_CREATED.value:
                    workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
                    workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
                    workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMITTED)
                self.storage.execute("UPDATE approvals SET acknowledgement_status='submitted' WHERE id=?", (approval_id,))
                self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=?", (parsed.proposal_id,))
                self._mark_position_management_proposal_handled(proposal, "submitted")
                
                # Format success message
                price_used = refreshed_price_val or proposal.get("latest_price") or 0.0
                qty_est = proposal.get("qty") or 0.0
                
                if prop_side == "buy":
                    action_type = "ADD TO WINNER" if is_add else "NEW ENTRY"
                    approved_notional = float(row.get("notional") or 0.0)
                    final_notional = float(proposal.get("notional") or 0.0)
                    if final_notional < approved_notional:
                        msg = f"Paper order submitted: {action_type} {prop_symbol} for ${final_notional:.2f}. Approved: ${approved_notional:.2f}. Final size reduced by validation. Price: ${price_used:.2f} (approx {qty_est:.4f} shares). Mode: paper only."
                    else:
                        msg = f"Paper order submitted: {action_type} {prop_symbol} for ${final_notional:.2f}. Approved: ${approved_notional:.2f}. Final: ${final_notional:.2f}. Price: ${price_used:.2f} (approx {qty_est:.4f} shares). Mode: paper only."
                else:
                    qty_str = f"{qty_est:.4f} shares" if qty_est > 0 else (f"{proposal.get('qty')} shares" if proposal.get('qty') is not None else "all shares")
                    msg = f"Paper order submitted: EXIT {prop_symbol} for {qty_str}. Price: ${price_used:.2f}. Mode: paper only."
                self.telegram.send_message("✅ " + msg)

                if prop_side == "buy":
                    superseded = self._supersede_equivalent_pending_buys(proposal)
                    if superseded:
                        self.telegram.send_message("Equivalent older BUY proposal(s) were superseded; unrelated pending opportunities remain independently approvable.")
                continue
            else:
                decision_status = "unknown" if result.status == "unknown" else "blocked"
                workflow = workflow_store.get(approval_workflow["id"])
                workflow_state = ApprovalWorkflowState(workflow["state"])
                if decision_status == "unknown" and workflow_state == ApprovalWorkflowState.INTENT_CREATED:
                    workflow_store.transition(
                        workflow["id"],
                        ApprovalWorkflowState.UNKNOWN,
                        safe_detail="broker submission outcome is ambiguous; reconciliation only",
                    )
                elif decision_status == "blocked" and workflow_state == ApprovalWorkflowState.VALIDATING:
                    workflow_store.transition(
                        workflow["id"],
                        ApprovalWorkflowState.BLOCKED,
                        validation_status="blocked",
                        safe_detail=result.reason,
                    )
                self.storage.execute("UPDATE approvals SET acknowledgement_status=? WHERE id=?", (decision_status, approval_id))
                self.storage.execute("UPDATE trade_proposals SET status=? WHERE id=?", (decision_status, parsed.proposal_id))
                
                # Format failure message
                if "could not get a fresh Alpaca price" in result.reason:
                    self.telegram.send_message(result.reason)
                elif "price movement too large" in result.reason.lower() or "price moved too much" in result.reason.lower():
                    self.telegram.send_message(f"No order placed for {prop_symbol}. {result.reason}. A refreshed proposal is required.")
                elif "no longer fresh" in result.reason:
                    self.telegram.send_message(f"Approved, but no order was placed. {result.reason}")
                else:
                    self.telegram.send_message(f"⚠️ Approved, but no order was placed for {prop_symbol}. Reason: {result.reason}.")
                continue
        self.notify_expired_proposals()
        self._expire_pending_batches(notify=True)
        durable_ids = {int(value) for value in processed_update_ids if value is not None}
        direct_ids: set[int] = set()
        workflow_store = ApprovalWorkflowStore(self.storage)
        for durable_id in durable_ids:
            workflows = self.storage.fetch_all(
                "SELECT id FROM approval_workflows WHERE telegram_update_id=?", (durable_id,)
            )
            if workflows:
                try:
                    workflow_store.mark_update_processed(workflows[0]["id"])
                except ApprovalWorkflowConflict:
                    # Business state is not durable yet. Leave the inbox row and
                    # cursor unchanged so restart recovery cannot hide the update.
                    continue
            else:
                direct_ids.add(durable_id)
        self.storage.complete_telegram_updates(direct_ids)
        remaining = self.storage.fetch_all(
            "SELECT MIN(update_id) first_unprocessed FROM telegram_updates WHERE processing_state!='processed'"
        )[0]["first_unprocessed"]
        cursor_id = min(max_id, int(remaining) - 1) if max_id > 0 and remaining is not None else max_id
        if cursor_id > 0:
            self.storage.set_control_state("telegram_last_processed_update_id", str(cursor_id), "system", "telegram", f"processed_{cursor_id}", cursor_id, None, None)
            self.telegram.get_updates(offset=cursor_id + 1, timeout=0)

        self._process_sleep_mode_emergency_timeouts()
        record_heartbeat(
            self.storage,
            "listener_poll",
            "healthy",
            attempted_at=iso_now(),
            completed_at=iso_now(),
            successful_at=iso_now(),
            detail={"updates_processed": len(processed_update_ids)},
        )

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

    def _check_stale_listener_block(self, symbol: str | None, approval_id: str) -> bool:
        from .utils import BOOT_COMMIT, get_git_commit
        current = get_git_commit()
        if BOOT_COMMIT != "unknown" and current != "unknown" and BOOT_COMMIT != current:
            self.storage.audit(self.run_id, "listener_stale_code_blocked_approval", {
                "boot_commit": BOOT_COMMIT,
                "current_commit": current,
                "symbol": symbol,
                "approval_id": approval_id
            })
            self.storage.execute(
                "UPDATE approvals SET status='blocked', final_order_decision='blocked', final_block_reason='listener is running stale code' WHERE id=?",
                (approval_id,)
            )
            msg = "Approval not processed because Telegram listener is running stale code. Please restart listener and wait for a fresh proposal."
            self.telegram.send_message(msg)
            return True
        return False

    def _calculate_volatility_aware_bps_limit(self, proposal_payload: dict[str, Any], base_bps: float, hard_cap_bps: float) -> float:
        stop_distance_pct = float(proposal_payload.get("stop_distance_pct") or 2.0)
        volatility_adjusted_bps = base_bps * (stop_distance_pct / 2.0)
        return min(max(base_bps, volatility_adjusted_bps), hard_cap_bps)

    def _persist_approval_directional_validation(
        self,
        approval_id: str,
        proposal: dict[str, Any],
    ) -> None:
        self.storage.execute(
            """UPDATE approvals SET proposal_reference_price=?,refreshed_bid=?,refreshed_ask=?,
                 directional_price_move_bps=?,movement_classification=?,final_limit_price=?,
                 directional_validation_reason=? WHERE id=?""",
            (
                proposal.get("proposal_reference_price"),
                proposal.get("quote_bid"),
                proposal.get("quote_ask"),
                proposal.get("directional_price_move_bps"),
                proposal.get("movement_classification"),
                proposal.get("final_limit_price", proposal.get("limit_price")),
                proposal.get("directional_validation_reason"),
                approval_id,
            ),
        )

    def _execute_final_revalidation(
        self,
        row: dict[str, Any],
        proposal: dict[str, Any],
        prop_symbol: str,
        prop_side: str,
        is_add: bool,
        approval_id: str,
        batch_row: dict[str, Any] = None,
        *,
        rotation_dependency_authorized: bool = False,
    ) -> tuple[Any, float | None, Any, str | None, float | None, float | None]:
        refreshed_price_val = None
        refreshed_price_at = None
        price_refreshed_at = None
        refreshed_price_age_seconds = None
        price_move_bps_since_proposal = None
        quote_data = None
        block_reason = None
        now_dt = datetime.now(UTC)

        # General safety pre-flights
        if self.config.get("mode") != "paper" or self.config.get("live_enabled") is not False:
            block_reason = "this build supports paper mode only"
        elif (PROJECT_ROOT / "config" / "KILL_SWITCH").exists():
            block_reason = "kill switch active"
        elif not self.storage.writable():
            block_reason = "database is not writable"
        elif (
            str(proposal.get("relationship_type") or row.get("relationship_type") or "")
            == "rotation_entry"
            and not rotation_dependency_authorized
        ):
            block_reason = "rotation contingent entry requires its current grouped dependency approval"
        elif (
            str(proposal.get("relationship_type") or row.get("relationship_type") or "")
            == "rotation_entry"
            and rotation_dependency_authorized
        ):
            from .rotation_coordinator import RotationCoordinator, RotationState

            rotation_group_id = str(
                proposal.get("rotation_group_id") or proposal.get("relationship_group_id") or ""
            )
            rotation = RotationCoordinator(
                self.storage, config_hash=self.config.get("effective_config_hash")
            )
            try:
                current_group = rotation.get_group(rotation_group_id)
            except KeyError:
                current_group = None
            if (
                current_group is None
                or current_group.get("state") != RotationState.ENTRY_REVALIDATING.value
                or not rotation.approval_is_current(rotation_group_id)
            ):
                block_reason = "rotation dependency or grouped approval is no longer current"

        # Entry/add approvals bind to both the policy captured with the
        # proposal and the latest current policy.  The conservative merge can
        # only reduce the permitted state/risk; protective exits do not enter
        # this gate.
        final_policy_override = None
        policy_bound_proposal = bool(
            proposal.get("strategy_version") or row.get("strategy_version")
            or proposal.get("policy_decision_id") or row.get("policy_decision_id")
        )
        if block_reason is None and prop_side == "buy" and "profitability_engine" in self.config and policy_bound_proposal:
            try:
                from .strategy_performance import StrategyPerformanceEngine, more_conservative_state

                strategy_version = str(proposal.get("strategy_version") or row.get("strategy_version") or STRATEGY_VERSION)
                engine = StrategyPerformanceEngine(self.storage, self.config)
                proposal_policy = engine.policy_by_id(proposal.get("policy_decision_id") or row.get("policy_decision_id"))
                latest_policy = engine.latest_valid_policy(strategy_version)
                if proposal_policy is None or latest_policy is None:
                    block_reason = "strategy performance policy unavailable, stale, or invalid; entry/add fails closed"
                else:
                    merged_state = more_conservative_state(proposal_policy.state, latest_policy.state)
                    final_policy_override = dataclasses.replace(
                        latest_policy,
                        state=merged_state,
                        reason=(
                            f"conservative final policy merge: proposal={proposal_policy.state}; "
                            f"latest={latest_policy.state}; selected={merged_state}"
                        ),
                        quality_score=min(proposal_policy.quality_score, latest_policy.quality_score),
                    )
                    proposal["final_policy_state"] = merged_state
                    proposal["final_policy_reason"] = final_policy_override.reason
                    proposal["final_policy_proposal_decision_id"] = proposal_policy.id
                    proposal["final_policy_current_decision_id"] = latest_policy.id
            except Exception as exc:
                logger.warning("Strategy policy final revalidation failed: %s", type(exc).__name__)
                block_reason = "strategy performance policy final revalidation failed; entry/add fails closed"

        # Expiry check
        if block_reason is None and self._proposal_or_candidate_expired(row, batch_row):
            block_reason = "Proposal expired"

        # Retrieve parameters from config
        telegram_cfg = self.config.get("telegram", {})
        refresh_required = telegram_cfg.get("approval_price_refresh_required", True)
        max_price_age = telegram_cfg.get("approval_max_price_age_seconds", 120)
        max_price_move_bps = telegram_cfg.get("approval_max_price_move_bps", 25)

        # Normal orders require a fresh authoritative two-sided quote. The
        # protective emergency path retains its separately validated paper
        # market-exit path.
        if block_reason is None and self.broker is not None:
            try:
                quote_data = validated_quote(self.broker, prop_symbol, self.config, now=now_dt)
                refreshed_price_val = float(quote_data["ask"] if prop_side == "buy" else quote_data["bid"])
                proposal["quote_bid"] = float(quote_data["bid"])
                proposal["quote_ask"] = float(quote_data["ask"])
                proposal["quote_timestamp"] = quote_data["timestamp"]
                proposal["quote_spread_bps"] = float(quote_data["spread_bps"])
                proposal["quote_source"] = quote_data["source"]
                refreshed_price_at = _dt(quote_data["timestamp"])
                if refreshed_price_at:
                    price_refreshed_at = refreshed_price_at.isoformat()
                    refreshed_price_age_seconds = (now_dt - refreshed_price_at).total_seconds()
            except Exception as e:
                logger.warning("Failed to refresh price for symbol %s: %s", prop_symbol, e)
                block_reason = (
                    "No order placed for " + prop_symbol + ". Final validation could not get a fresh Alpaca price within the allowed window. A new proposal is required."
                    if "stale" in str(e).lower() or "future" in str(e).lower()
                    else "Price refresh failed or price is unavailable"
                )

        # Check market open status
        market_open = False
        if block_reason is None and self.broker is not None:
            try:
                market_open = self.broker.is_market_open()
            except Exception:
                market_open = False

        # Get the proposal price
        proposal_price = proposal.get("latest_price") or row.get("current_price") or row.get("price")
        if proposal_price is not None:
            proposal_price = float(proposal_price)

        if block_reason is None and (proposal_price is None or proposal_price <= 0):
            block_reason = "proposal reference price is unavailable"

        if proposal_price is not None and proposal_price > 0 and refreshed_price_val is not None:
            directional_move_bps = ((refreshed_price_val - proposal_price) / proposal_price) * 10000
            price_move_bps_since_proposal = abs(directional_move_bps)
            proposal["proposal_reference_price"] = proposal_price
            proposal["directional_price_move_bps"] = directional_move_bps
            proposal["movement_classification"] = (
                ("adverse_upward" if directional_move_bps > 0 else "favourable_downward" if directional_move_bps < 0 else "unchanged")
                if prop_side == "buy"
                else ("adverse_downward_exit_urgent" if directional_move_bps < 0 else "favourable_upward_exit" if directional_move_bps > 0 else "unchanged")
            )

        if block_reason is None and prop_side == "sell":
            try:
                positions = self.broker.get_positions() if self.broker is not None else []
                position = next(
                    (item for item in positions if str(_value(item, "symbol", "")).upper() == prop_symbol.upper()),
                    None,
                )
                held_quantity = float(_value(position, "qty", 0.0) or 0.0) if position is not None else 0.0
                sell_quantity = float(proposal.get("qty") or 0.0)
                if held_quantity <= 0:
                    block_reason = "position no longer exists"
                elif sell_quantity <= 0 or sell_quantity > held_quantity + 1e-9:
                    block_reason = "sell quantity is invalid or exceeds held quantity"
                else:
                    proposal["held_quantity_at_revalidation"] = held_quantity
                    open_orders = self.broker.get_open_orders() if self.broker is not None else []
                    if any(
                        str(_value(order, "symbol", "")).upper() == prop_symbol.upper()
                        and str(_value(order, "side", "")).lower() == "sell"
                        for order in open_orders
                    ):
                        block_reason = "conflicting open sell order exists"
            except (AttributeError, TypeError, ValueError):
                block_reason = "current sell holdings could not be validated"

        # Perform revalidation checks
        if block_reason is None:
            if refresh_required:
                if refreshed_price_val is None or refreshed_price_val <= 0:
                    block_reason = "Price refresh failed or price is unavailable"
                elif refreshed_price_age_seconds is None or refreshed_price_age_seconds > max_price_age or refreshed_price_age_seconds < -5:
                    block_reason = "No order placed for " + prop_symbol + ". Final validation could not get a fresh Alpaca price within the allowed window. A new proposal is required."
                elif not market_open:
                    block_reason = "Market is closed"
                elif proposal_price is not None and proposal_price > 0:
                    directional_move_bps = float(proposal["directional_price_move_bps"])
                    # Make price movement limit volatility-aware
                    hard_cap_bps = float(telegram_cfg.get("approval_max_price_move_hard_cap_bps", 75.0))
                    volatility_aware_limit_bps = self._calculate_volatility_aware_bps_limit(proposal, float(max_price_move_bps), hard_cap_bps)
                    
                    # Store final limit used in proposal dict
                    proposal["approval_price_move_limit_bps"] = volatility_aware_limit_bps
                    
                    if prop_side == "buy":
                        proposal["movement_classification"] = (
                            "adverse_upward" if directional_move_bps > 0
                            else "favourable_downward" if directional_move_bps < 0
                            else "unchanged"
                        )
                        if directional_move_bps > volatility_aware_limit_bps:
                            block_reason = f"Price moved too much for BUY: adverse upward movement {directional_move_bps:.1f} bps exceeds limit {volatility_aware_limit_bps:.1f} bps"
                            proposal["directional_validation_reason"] = block_reason
                        else:
                            proposal["directional_validation_reason"] = "directional BUY movement remained within approval protections"
                    else:
                        proposal["movement_classification"] = (
                            "adverse_downward_exit_urgent" if directional_move_bps < 0
                            else "favourable_upward_exit" if directional_move_bps > 0
                            else "unchanged"
                        )
                        proposal["directional_validation_reason"] = (
                            "risk-reducing SELL remains eligible after fresh holdings, quote, order-conflict, and expiry validation"
                        )
            else:
                if not market_open:
                    block_reason = "Market is closed"

        if block_reason is None:
            try:
                proposal["quote_bid"] = float(quote_data["bid"])
                proposal["quote_ask"] = float(quote_data["ask"])
                proposal["quote_midpoint"] = float(quote_data["midpoint"])
                proposal["quote_timestamp"] = quote_data["timestamp"]
                proposal["quote_spread_bps"] = float(quote_data["spread_bps"])
                proposal["quote_source"] = quote_data["source"]
                if row.get("emergency_exit_triggered") == 1:
                    proposal["final_limit_price"] = None
                    proposal["protective_exit_mechanism"] = "protective_paper_exit"
                else:
                    proposal["order_type"] = "limit"
                    proposal["limit_price"] = bounded_marketable_limit(quote_data, prop_side, self.config)
                    proposal["final_limit_price"] = proposal["limit_price"]
            except (KeyError, TypeError, ValueError) as exc:
                block_reason = f"bounded marketable-limit validation failed: {type(exc).__name__}"

        execution_reference_price = refreshed_price_val
        if block_reason is None and prop_side == "buy" and row.get("emergency_exit_triggered") != 1:
            try:
                execution_reference_price = max(
                    float(refreshed_price_val or 0.0),
                    float(proposal.get("limit_price") or 0.0),
                )
                if execution_reference_price <= 0:
                    raise ValueError("nonpositive execution reference")
                proposal["execution_reference_price"] = execution_reference_price
            except (TypeError, ValueError):
                block_reason = "conservative BUY execution reference is unavailable"

        # The stop is persisted as an absolute price. Recompute its dollar
        # distance against the freshly validated entry quote before the final
        # risk check so stale proposal geometry cannot be misclassified.
        if block_reason is None and row.get("emergency_exit_triggered") != 1 and execution_reference_price is not None:
            try:
                persisted_stop = float(proposal.get("stop_price"))
                if prop_side == "buy" and persisted_stop > 0 and execution_reference_price > persisted_stop:
                    proposal["stop_distance_dollars"] = execution_reference_price - persisted_stop
            except (TypeError, ValueError):
                pass

        if block_reason is None:
            block_reason = self._final_revalidate_position_management(proposal, refreshed_price_val)

        if block_reason:
            proposal.setdefault("directional_validation_reason", block_reason)
            if prop_side == "buy" and proposal.get("action") in {"entry", "add"} and proposal.get("adaptive_sizing"):
                try:
                    proposal["proposal_price_age_seconds_at_send"] = refreshed_price_age_seconds
                    blocked_context = self._portfolio_context(proposal, approval_valid=True)
                    blocked_sizing = {
                        "score_adjusted_notional": float(row.get("notional") or 0.0),
                        "final_notional": 0.0,
                        "suggested_shares": 0.0,
                        "stop_risk_dollars": 0.0,
                        "minimum_executable_notional": (self.config.get("position_sizing", {}) or {}).get("minimum_executable_notional_usd"),
                        "sizing_caps": proposal.get("sizing_caps") or {},
                        "blocked_reason": block_reason,
                        "strategy_state": proposal.get("final_policy_state") or proposal.get("strategy_state"),
                        "strategy_policy_version": proposal.get("strategy_policy_version"),
                        "policy_decision_id": proposal.get("final_policy_current_decision_id") or proposal.get("policy_decision_id"),
                        "performance_snapshot_id": proposal.get("performance_snapshot_id"),
                        "strategy_quality_score": proposal.get("strategy_quality_score"),
                    }
                    blocked_conviction = self._record_adaptive_conviction(
                        proposal, blocked_sizing, blocked_context, risk_checks_passed=False,
                        stage="final_revalidation", approval_id=approval_id,
                    )
                    if blocked_conviction is not None:
                        proposal_adaptive = proposal.get("adaptive_sizing") or {}
                        self._record_adaptive_sizing(
                            proposal, blocked_sizing, blocked_context, blocked_conviction,
                            stage="final_revalidation", approval_id=approval_id,
                            displayed_adaptive_ceiling=proposal_adaptive.get("displayed_adaptive_ceiling"),
                            proposal_adaptive_notional=proposal_adaptive.get("adaptive_notional"),
                            final_revalidation_blocked=True,
                            missing_inputs=["complete_fresh_final_operational_sizing"],
                        )
                except Exception as exc:
                    logger.warning("Blocked final adaptive evidence unavailable: %s", type(exc).__name__)
            result = ExecutionResult(False, "blocked", None, reason=block_reason)
            return result, refreshed_price_val, refreshed_price_at, price_refreshed_at, refreshed_price_age_seconds, price_move_bps_since_proposal

        # Get authoritative runtime state to evaluate fresh exposure snapshot
        try:
            state = self._authoritative_runtime_state(force=True)
            snapshot_fresh = self._get_exposure_snapshot(state["positions"], state["account"])
        except Exception as e:
            logger.warning("Failed to retrieve authoritative snapshot during revalidation: %s", e)
            snapshot_fresh = None

        if refreshed_price_val is not None:
            proposal["latest_price"] = (
                execution_reference_price
                if prop_side == "buy" and execution_reference_price is not None
                else refreshed_price_val
            )
        if refreshed_price_at is not None:
            proposal["price_at"] = refreshed_price_at.isoformat()

        approved_notional = float(row.get("notional") or 0.0)
        proposal["approved_notional"] = approved_notional
        proposal["approved_notional_ceiling"] = approved_notional
        proposal["cluster_name"] = self._get_symbol_cluster(prop_symbol)
        size_dict: dict[str, Any] | None = None

        # Recalculate dynamic size if sizing enabled and buy
        if snapshot_fresh and self.config.get("position_sizing", {}).get("enabled", True) and prop_side == "buy":
            try:
                # Final approval must never reuse a proposal-cycle sleeve.
                # Rebuild from current held positions and reservations so a
                # fill/release between Telegram polls cannot leave stale room.
                if self.config.get("phase4", {}).get("active"):
                    self._phase4_allocation_cache = None
                bars_fresh = normalize_bars(self.broker.get_historical_bars(prop_symbol, "1Day", 250), prop_symbol)
                size_dict = self._calculate_dynamic_size(
                    prop_symbol,
                    float(proposal.get("score", 70.0) or 70.0),
                    proposal.get("volatility_regime", "normal"),
                    execution_reference_price,
                    bars_fresh,
                    snapshot_fresh,
                    is_add=is_add, strategy_version=str(proposal.get("strategy_version") or row.get("strategy_version") or STRATEGY_VERSION),
                    policy_override=final_policy_override,
                )

                # This is the fresh canonical safety baseline and cap set. The
                # adaptive engine below owns the final paper notional.
                proposal["canonical_revalidation_notional"] = float(size_dict["final_notional"] or 0.0)
                if not self._operational_adaptive_enabled():
                    final_notional = min(max(0.0, approved_notional), max(0.0, float(size_dict["final_notional"] or 0.0)))
                    proposal["notional"] = final_notional
                    proposal["qty"] = final_notional / execution_reference_price if execution_reference_price and execution_reference_price > 0 else 0.0
                proposal["phase4_mode"] = size_dict.get("phase4_mode")
                proposal["strategy_state"] = size_dict.get("strategy_state")
                proposal["strategy_policy_version"] = size_dict.get("strategy_policy_version")
                proposal["strategy_registry_snapshot_id"] = size_dict.get("strategy_registry_snapshot_id")
                proposal["strategy_sleeve"] = size_dict.get("strategy_sleeve")
                proposal["sleeve_allocation_id"] = size_dict.get("sleeve_allocation_id")
                proposal["sleeve_stop_risk_ceiling"] = size_dict.get("sleeve_stop_risk_ceiling")
                proposal["sleeve_notional_ceiling"] = size_dict.get("sleeve_notional_ceiling")
                proposal["strategy_sleeve_payload"] = size_dict.get("strategy_sleeve_payload")
                proposal["risk_value"] = size_dict.get("risk_value")
                proposal["risk_unit"] = size_dict.get("risk_unit")
                proposal["conversion_equity"] = size_dict.get("conversion_equity")
                proposal["conversion_equity_as_of"] = size_dict.get("conversion_equity_as_of")
                proposal["risk_formula_version"] = size_dict.get("risk_formula_version")
                proposal["average_dollar_volume"] = size_dict.get("average_dollar_volume")
                proposal["sizing_caps"] = size_dict.get("sizing_caps") or {}
                proposal["binding_caps"] = size_dict.get("binding_caps") or []
                proposal["stop_risk_dollars"] = size_dict.get("stop_risk_dollars")
            except Exception as e:
                logger.warning("Recalculate dynamic size failed during revalidation: %s", type(e).__name__)
                block_reason = "final validated sizing could not be recomputed"

        # Recompute operational Adaptive Conviction and Adaptive Sizing from
        # fresh authoritative inputs. Approval may preserve, reduce, or block;
        # the displayed approved ceiling can never be enlarged.
        context = self._portfolio_context(proposal, approval_valid=True)
        final_conviction = None
        if self._operational_adaptive_enabled() and prop_side == "buy" and proposal.get("action") in {"entry", "add"} and size_dict is not None:
            try:
                proposal["proposal_price_age_seconds_at_send"] = refreshed_price_age_seconds
                final_conviction = self._record_adaptive_conviction(
                    proposal,
                    size_dict,
                    context,
                    risk_checks_passed=(
                        block_reason is None
                        and float(size_dict.get("final_notional") or 0.0) > 0
                        and not size_dict.get("blocked_reason")
                    ),
                    stage="final_revalidation",
                    approval_id=approval_id,
                )
                proposal_adaptive = proposal.get("adaptive_sizing") or {}
                if proposal_adaptive.get("operating_mode") != "operational_paper":
                    block_reason = "proposal was not created by the compatible operational adaptive-sizing version"
                if final_conviction is not None:
                    proposal["adaptive_sizing_final"] = self._record_adaptive_sizing(
                        proposal,
                        size_dict,
                        context,
                        final_conviction,
                        stage="final_revalidation",
                        approval_id=approval_id,
                        displayed_adaptive_ceiling=proposal_adaptive.get("displayed_adaptive_ceiling"),
                        proposal_adaptive_notional=proposal_adaptive.get("adaptive_notional"),
                        final_revalidation_blocked=bool(block_reason),
                    )
            except Exception as exc:
                logger.warning("Operational adaptive sizing final revalidation unavailable: %s", type(exc).__name__)
                self.storage.audit(self.run_id, "adaptive_sizing_final_operational_unavailable", {
                    "proposal_id": row.get("id"), "approval_id": approval_id,
                    "error_type": type(exc).__name__, "order_submitted": False,
                })
                block_reason = "final operational adaptive sizing could not be recomputed"

            final_adaptive = proposal.get("adaptive_sizing_final") or {}
            final_notional = min(
                max(0.0, approved_notional),
                max(0.0, float(final_adaptive.get("operational_notional") or 0.0)),
            )
            if final_notional <= 0:
                block_reason = block_reason or "final operational adaptive sizing found no safe executable size"
            else:
                displayed_quantity = float(row.get("qty") or proposal.get("approved_quantity_ceiling") or proposal.get("qty") or 0.0)
                displayed_stop_risk = float(proposal.get("approved_stop_risk_ceiling") or proposal.get("stop_risk_dollars") or 0.0)
                recomputed_quantity = final_notional / execution_reference_price if execution_reference_price and execution_reference_price > 0 else 0.0
                stop_distance = float(proposal.get("stop_distance_dollars") or 0.0)
                risk_quantity_ceiling = displayed_stop_risk / stop_distance if displayed_stop_risk > 0 and stop_distance > 0 else recomputed_quantity
                final_quantity = min(recomputed_quantity, displayed_quantity, risk_quantity_ceiling)
                final_notional = min(final_notional, final_quantity * float(execution_reference_price or 0.0))
                proposal["notional_reduced_by_cap"] = final_notional < approved_notional - 1e-9
                proposal["notional"] = final_notional
                proposal["qty"] = final_quantity
                proposal["stop_risk_dollars"] = float(proposal["qty"]) * float(proposal.get("stop_distance_dollars") or 0.0)
                proposal["approved_quantity_ceiling"] = displayed_quantity
                proposal["approved_notional_ceiling"] = approved_notional
                proposal["sizing_caps"] = dict(final_adaptive.get("sizing_caps") or {})
                proposal["binding_caps"] = [final_adaptive.get("binding_adaptive_cap")]
                proposal["deployment_mode"] = final_conviction.get("deployment_mode") if final_conviction else proposal.get("deployment_mode")
                proposal["opportunity_class"] = final_conviction.get("opportunity_class") if final_conviction else proposal.get("opportunity_class")
                proposal["permitted_stop_risk_pct"] = final_conviction.get("recommended_stop_risk_pct") if final_conviction else proposal.get("permitted_stop_risk_pct")
                context = self._portfolio_context(proposal, approval_valid=True)
                if final_notional <= 0 or final_quantity <= 0:
                    block_reason = "displayed quantity or stop-risk ceiling leaves no executable final size"

        if (
            block_reason is None and prop_side == "buy" and proposal.get("action") == "add"
            and (self.config.get("winner_expansion", {}) or {}).get("enabled") is True
            and (self.config.get("phase3", {}) or {}).get("active") is True
            and (self.config.get("phase4", {}) or {}).get("active") is True
        ):
            try:
                if final_conviction is not None:
                    proposal["adaptive_conviction_final"] = final_conviction
                    proposal["deployment_mode"] = final_conviction.get("deployment_mode")
                winner_decision, _, _ = self._evaluate_winner_expansion(
                    proposal,
                    decision_stage="final_revalidation",
                    approval_id=approval_id,
                )
                proposal["stop_risk_dollars"] = winner_decision.risk_decision.consumed_risk
                proposal["risk_budget"] = winner_decision.risk_decision.consumed_risk
                proposal["risk_budget_dollars"] = winner_decision.risk_decision.consumed_risk
                context = self._portfolio_context(proposal, approval_valid=True)
            except Exception as exc:
                block_reason = f"final winner-expansion validation blocked: {str(exc)}"

        if block_reason:
            return ExecutionResult(False, "blocked", None, reason=block_reason), refreshed_price_val, refreshed_price_at, price_refreshed_at, refreshed_price_age_seconds, price_move_bps_since_proposal

        # This check is deliberately adjacent to Executor. Quote refresh,
        # sizing, and multi-leg recovery may take long enough to cross the
        # grouped approval expiry after the earlier preflight. No dependent
        # rotation action may reach broker submission on stale authority.
        relationship = str(
            proposal.get("relationship_type") or row.get("relationship_type") or ""
        )
        if relationship in {"rotation_exit", "rotation_entry"}:
            from .rotation_coordinator import RotationCoordinator, RotationState, TERMINAL_STATES

            group_id = str(
                proposal.get("rotation_group_id")
                or proposal.get("relationship_group_id")
                or row.get("rotation_group_id")
                or row.get("relationship_group_id")
                or ""
            )
            coordinator = RotationCoordinator(
                self.storage, config_hash=self.config.get("effective_config_hash")
            )
            try:
                group = coordinator.get_group(group_id)
            except KeyError:
                group = None
            rotation_block = None
            if (
                group is None
                or RotationState(group["state"]) in TERMINAL_STATES
                or _parse_datetime(group["expires_at"]) <= datetime.now(UTC)
            ):
                rotation_block = "rotation group expired or became terminal before broker submission"
            elif relationship == "rotation_entry":
                if (
                    RotationState(group["state"]) != RotationState.ENTRY_REVALIDATING
                    or not rotation_dependency_authorized
                    or not coordinator.approval_is_current(group_id)
                ):
                    rotation_block = "rotation entry dependency or grouped approval changed before broker submission"
            else:
                approvals = self.storage.fetch_all(
                    """SELECT approval_id,status FROM rotation_group_approvals
                       WHERE group_id=? ORDER BY created_at DESC LIMIT 1""",
                    (group_id,),
                )
                if (
                    RotationState(group["state"]) not in {
                        RotationState.APPROVED_EXIT_PENDING,
                        RotationState.EXIT_SUBMITTED,
                        RotationState.EXIT_PARTIALLY_FILLED,
                    }
                    or not approvals
                    or approvals[0].get("approval_id") != group.get("approval_id")
                    or approvals[0].get("status") not in {"active", "exit_submitted"}
                ):
                    rotation_block = "rotation exit grouped approval changed before broker submission"
            if rotation_block:
                return ExecutionResult(False, "blocked", None, reason=rotation_block), refreshed_price_val, refreshed_price_at, price_refreshed_at, refreshed_price_age_seconds, price_move_bps_since_proposal

        # Execute
        proposal["status"] = "approved"
        if row.get("emergency_exit_triggered") == 1:
            proposal["execution_path"] = "protective_paper_exit"
        context["execution_path"] = proposal.get("execution_path")
        result = Executor(
            self.broker,
            self._risk_engine(row.get("id"), "final"),
            self.storage,
            self.run_id,
        ).execute(
            proposal,
            context,
            source_type="emergency" if row.get("emergency_exit_triggered") == 1 else "proposal",
            approval_id=approval_id,
        )
        if proposal.get("action") == "add" and proposal.get("pyramiding_milestone_key"):
            try:
                WinnerExpansionStore(self.storage).transition_milestone(
                    str(proposal.get("position_lifecycle_id")),
                    str(proposal.get("pyramiding_milestone_key")),
                    "SUBMITTED" if result.submitted else "REJECTED",
                    approval_id=approval_id,
                    intent_id=result.intent_id,
                    terminal_reason=None if result.submitted else (result.reason or result.status),
                )
            except Exception as exc:
                logger.error("Winner milestone execution transition failed closed: %s", type(exc).__name__)
                if result.submitted:
                    # The intent already exists and remains idempotent; flag
                    # reconciliation rather than attempting any second order.
                    self.storage.audit(self.run_id, "winner_milestone_transition_reconciliation_required", {
                        "proposal_id": proposal.get("id"), "intent_id": result.intent_id,
                        "error_type": type(exc).__name__,
                    })

        return result, refreshed_price_val, refreshed_price_at, price_refreshed_at, refreshed_price_age_seconds, price_move_bps_since_proposal

    def _approve_batch_candidate(self, proposal_id: str, sender: str, raw_text: str, batch_row: dict[str, Any]) -> tuple[bool, str, str | None]:
        rows = self.storage.fetch_all("SELECT * FROM trade_proposals WHERE id=? AND status='pending'", (proposal_id,))
        if not rows:
            self.telegram.send_message("I did not take any action because this candidate was already handled earlier.")
            return False, "already_handled", "candidate already handled"

        row = rows[0]
        if row.get("relationship_type") == "rotation_entry":
            message = "This contingent candidate can only execute through its current approved rotation dependency."
            self.telegram.send_message(message)
            return False, "rotation_group_required", message
        if self._proposal_or_candidate_expired(row, batch_row):
            self.storage.execute("UPDATE trade_proposals SET status='expired' WHERE id=? AND status='pending'", (proposal_id,))
            self.storage.execute("UPDATE proposal_batch_candidates SET candidate_status='expired' WHERE proposal_id=? AND candidate_status='pending'", (proposal_id,))
            self._mark_proposal_expiry_notified(proposal_id)
            if batch_row.get("batch_id"):
                self._mark_batch_expiry_notified(str(batch_row["batch_id"]))
            self._update_batch_status(str(batch_row.get("batch_id") or ""))
            self.telegram.send_message("That candidate has expired, so I did not take action. I will not submit an order from an expired proposal.")
            return False, "expired", "candidate expired"
        if self._sleep_mode_blocks_approval(_hydrate_proposal_row(row)):
            msg = "Sleep mode is ON, so I did not process this BUY/ADD approval. Send awake first, then approve again if the proposal is still valid."
            self.telegram.send_message(msg)
            return False, "sleep_mode_active", msg

        approval_id = str(uuid.uuid4())
        approval_received_at = iso_now()
        workflow_store = ApprovalWorkflowStore(self.storage)
        approval_workflow = workflow_store.accept_approval(
            approval_id=approval_id,
            run_id=self.run_id,
            proposal_id=proposal_id,
            sender_id=sender,
            raw_message=raw_text,
            parsed_action="approve",
            telegram_update_id=None,
            reply_to_message_id=str(batch_row.get("telegram_message_id") or "") or None,
            targeting_method="batch",
            acknowledgement_status="received",
            approval_received_at=approval_received_at,
        )
        approval_id = str(approval_workflow["approval_id"])
        approval_workflow = workflow_store.transition(
            approval_workflow["id"],
            ApprovalWorkflowState.VALIDATING,
            expected_state=ApprovalWorkflowState.TARGET_RESOLVED,
            safe_detail="batch candidate final local validation started",
        )

        if self._check_stale_listener_block(row.get("symbol", ""), approval_id):
            workflow_store.transition(
                approval_workflow["id"], ApprovalWorkflowState.BLOCKED, safe_detail="listener freshness check blocked approval"
            )
            return False, "listener_stale_code_blocked_approval", "listener is running stale code"

        if not self.storage.consume_approval(proposal_id, approval_id):
            workflow_store.transition(
                approval_workflow["id"], ApprovalWorkflowState.BLOCKED, safe_detail="proposal approval was already consumed"
            )
            self.telegram.send_message("I did not take any action because this candidate was already handled earlier.")
            return False, "already_handled", "candidate already handled"

        prop_symbol = row.get("symbol", "")
        prop_side = row.get("side", "").lower()
        proposal = _hydrate_proposal_row(row, status="approved")
        final_revalidation_started_at = iso_now()
        proposal = _hydrate_proposal_row(row)
        is_add = proposal.get("action") == "add" or bool(proposal.get("is_add", False))

        result, refreshed_price_val, refreshed_price_at, price_refreshed_at, refreshed_price_age_seconds, price_move_bps_since_proposal = self._execute_final_revalidation(
            row, proposal, prop_symbol, prop_side, is_add, approval_id, batch_row
        )

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
        self._persist_approval_directional_validation(approval_id, proposal)
        if result.intent_id:
            self.storage.link_executed_order_records(result.intent_id)
            self.storage.upsert_actual_trade_outcome_for_order(result.intent_id)
        if result.submitted:
            workflow = workflow_store.get(approval_workflow["id"])
            if workflow["state"] == ApprovalWorkflowState.INTENT_CREATED.value:
                workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_PENDING)
                workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMISSION_STARTED)
                workflow_store.transition(workflow["id"], ApprovalWorkflowState.SUBMITTED)
            self.storage.execute("UPDATE approvals SET acknowledgement_status='submitted' WHERE id=?", (approval_id,))
            self.storage.execute("UPDATE trade_proposals SET status='submitted' WHERE id=?", (proposal_id,))
            self._mark_position_management_proposal_handled(proposal, "submitted")
            if prop_side == "buy":
                self._supersede_equivalent_pending_buys(proposal)
            
            # Format success message
            price_used = refreshed_price_val or proposal.get("latest_price") or 0.0
            qty_est = proposal.get("qty") or 0.0
            
            if prop_side == "buy":
                action_type = "ADD TO WINNER" if is_add else "NEW ENTRY"
                approved_notional = float(row.get("notional") or 0.0)
                final_notional = float(proposal.get("notional") or 0.0)
                if final_notional < approved_notional:
                    msg = f"Paper order submitted: {action_type} {prop_symbol} for ${final_notional:.2f}. Approved: ${approved_notional:.2f}. Final size reduced by validation. Price: ${price_used:.2f} (approx {qty_est:.4f} shares). Mode: paper only."
                else:
                    msg = f"Paper order submitted: {action_type} {prop_symbol} for ${final_notional:.2f}. Approved: ${approved_notional:.2f}. Final: ${final_notional:.2f}. Price: ${price_used:.2f} (approx {qty_est:.4f} shares). Mode: paper only."
            else:
                qty_str = f"{qty_est:.4f} shares" if qty_est > 0 else (f"{proposal.get('qty')} shares" if proposal.get('qty') is not None else "all shares")
                msg = f"Paper order submitted: EXIT {prop_symbol} for {qty_str}. Price: ${price_used:.2f}. Mode: paper only."
            self.telegram.send_message("✅ " + msg)
            return True, "submitted", None

        decision_status = "unknown" if result.status == "unknown" else "blocked"
        workflow = workflow_store.get(approval_workflow["id"])
        workflow_state = ApprovalWorkflowState(workflow["state"])
        if decision_status == "unknown" and workflow_state == ApprovalWorkflowState.INTENT_CREATED:
            workflow_store.transition(
                workflow["id"], ApprovalWorkflowState.UNKNOWN, safe_detail="broker submission outcome is ambiguous; reconciliation only"
            )
        elif decision_status == "blocked" and workflow_state in {
            ApprovalWorkflowState.VALIDATING,
            ApprovalWorkflowState.APPROVED_PENDING_INTENT,
        }:
            workflow_store.transition(
                workflow["id"], ApprovalWorkflowState.BLOCKED, validation_status="blocked", safe_detail=result.reason
            )
        self.storage.execute("UPDATE approvals SET acknowledgement_status=? WHERE id=?", (decision_status, approval_id))
        self.storage.execute("UPDATE trade_proposals SET status=? WHERE id=?", (decision_status, proposal_id))
        
        # Format failure message
        if "could not get a fresh Alpaca price" in result.reason:
            self.telegram.send_message(result.reason)
        elif "price movement too large" in result.reason.lower() or "price moved too much" in result.reason.lower():
            self.telegram.send_message(f"No order placed for {prop_symbol}. {result.reason}. A refreshed proposal is required.")
        elif "no longer fresh" in result.reason:
            self.telegram.send_message(f"Approved, but no order was placed. {result.reason}")
        else:
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
        try:
            require_protective_paper_exit_support()
        except PermissionError as exc:
            return False, str(exc)
        if int(proposal.get("emergency_exit_triggered") or 0) != 1 or not proposal.get("emergency_exit_trigger_reason"):
            return False, "ordinary workflows cannot use the protective paper-exit path"
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
            """SELECT id FROM order_intents WHERE symbol=? AND side='sell'
               AND state IN ('created','reserved','submitting','submitted','partially_filled','cancel_pending','unknown','reconciliation_required')""",
            (symbol,),
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

        approval_rows = self.storage.fetch_all(
            "SELECT id FROM approvals WHERE proposal_id=? AND consumed_at IS NOT NULL ORDER BY consumed_at DESC LIMIT 1",
            (proposal["id"],),
        )
        approval_id = approval_rows[0]["id"] if approval_rows else None
        if approval_id is None:
            return False, "manual approval is required before every protective paper exit"
        executable = {
            **proposal,
            "status": "approved",
            "side": "sell",
            "action": "exit",
            "qty": qty_held,
            "latest_price": refreshed_price,
            "price_at": refreshed_at.isoformat(),
            "trading_mode": "paper",
            "execution_path": "protective_paper_exit",
        }
        context = self._portfolio_context(executable, approval_valid=True)
        result = Executor(
            self.broker,
            self._risk_engine(proposal["id"], "emergency_final"),
            self.storage,
            self.run_id,
        ).execute(executable, context, source_type="emergency", approval_id=approval_id)
        if result.submitted:
            return True, result.status
        return False, result.reason or result.status

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

    def _canonical_trade_score_components(
        self,
        *,
        symbol: str,
        signal: Any,
        price: float,
        price_at: datetime,
        bars: pd.DataFrame,
        asset_score: float,
        now: datetime,
    ) -> dict[str, Any]:
        """Return the scanner's one canonical, current trade-score calculation."""
        today_start = now.date().isoformat() + "T00:00:00"
        prev_rows = self.storage.fetch_all(
            "SELECT price,score,signal FROM market_memory WHERE symbol=? ORDER BY created_at DESC LIMIT 1",
            (symbol,),
        )
        session_rows = self.storage.fetch_all(
            "SELECT price FROM market_memory WHERE symbol=? AND created_at>=? ORDER BY created_at ASC LIMIT 1",
            (symbol, today_start),
        )
        previous_price = float(prev_rows[0]["price"]) if prev_rows else price
        session_start_price = float(session_rows[0]["price"]) if session_rows else price
        score_rule = 25.0 if signal.action in {"ENTRY", "EXIT"} else 0.0
        score_asset = 15.0 if asset_score >= 80 else 12.0 if asset_score >= 65 else 8.0 if asset_score >= 50 else 3.0
        score_5m = 5.0
        if prev_rows:
            if signal.side == "buy":
                score_5m = 10.0 if price > previous_price else 5.0 if price == previous_price else 0.0
            elif signal.side == "sell":
                score_5m = 10.0 if price < previous_price else 5.0 if price == previous_price else 0.0
            else:
                score_5m = 5.0 if price == previous_price else 10.0 if price > previous_price else 0.0
        score_session = 5.0
        if session_rows:
            if signal.side == "buy":
                score_session = 10.0 if price > session_start_price else 5.0 if price == session_start_price else 0.0
            elif signal.side == "sell":
                score_session = 10.0 if price < session_start_price else 5.0 if price == session_start_price else 0.0
            else:
                score_session = 5.0 if price == session_start_price else 10.0 if price > session_start_price else 0.0
        vol_20 = signal.indicators.get("volatility_20")
        if vol_20 is None:
            score_vol, regime, gate = 0.0, "missing", "fail-safe HOLD"
        elif float(vol_20) > 0.45:
            score_vol, regime, gate = 0.0, "extreme", "blocked"
        elif float(vol_20) > 0.35:
            score_vol, regime, gate = 5.0, "high", "watch only"
        elif float(vol_20) >= 0.25:
            score_vol, regime, gate = 10.0, "elevated", "eligible"
        elif float(vol_20) >= 0.08:
            score_vol, regime, gate = 15.0, "normal", "eligible"
        elif float(vol_20) >= 0.0:
            score_vol, regime, gate = 8.0, "too quiet", "eligible"
        else:
            score_vol, regime, gate = 0.0, "unknown", "fail-safe HOLD"
        port_context = self._portfolio_context({"symbol": symbol, "side": signal.side or "buy", "action": "entry"})
        risk_budgeted = self._ranked_batch_mode_enabled()
        safety_ok = not port_context.get("duplicate_order")
        if not risk_budgeted and port_context.get("trades_today", 0) >= self.config["risk"].get("max_trades_per_day", 1):
            safety_ok = False
        if not risk_budgeted and signal.action == "ENTRY" and port_context.get("open_positions", 0) >= self.config["risk"].get("max_open_positions", 1):
            safety_ok = False
        score_safety = 15.0 if safety_ok else 0.0
        normalized_price_at = _parse_datetime(price_at)
        age = (now - normalized_price_at).total_seconds()
        fresh_price = -5 <= age <= self.config["risk"].get("max_price_age_seconds", 120)
        enough_bars = len(bars) >= self.config["risk"].get("min_historical_bars", 50)
        score_data = 10.0 if fresh_price and enough_bars else 5.0 if fresh_price else 0.0
        score = float(round(score_rule + score_asset + score_5m + score_session + score_vol + score_safety + score_data, 2))
        return {
            "score": score,
            "previous_price": previous_price,
            "session_start_price": session_start_price,
            "score_rule": score_rule,
            "score_asset": score_asset,
            "score_5m": score_5m,
            "score_session": score_session,
            "score_vol": score_vol,
            "score_safety": score_safety,
            "score_data": score_data,
            "volatility_regime": regime,
            "volatility_gate_result": gate,
        }

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
        self._current_cycle_exit_blocker = None
        if self.config.get("mode") == "live" and not self.config.get("live_enabled"):
            self.telegram.send_message("Blocked for safety: live trading is disabled.")
            return

        self._phase1_bar_cache = {}
        self._run_crypto_research_due()

        sleep_mode_active = int(self.storage.get_control_state("sleep_mode_active", "0")) == 1
        sleep_mode_started_at = self.storage.get_control_state("sleep_mode_started_at")
        sleep_mode_ended_at = self.storage.get_control_state("sleep_mode_ended_at")
        sleep_mode_reason = "sleep mode is active" if sleep_mode_active else None

        positions = self.broker.get_positions()
        orders = self.broker.get_open_orders()
        market_open = self.broker.is_market_open()
        strategy_config = {
            "maximum_volatility_20d": 0.45,
            "stop_drawdown_pct": 0.08,
            **((self.config.get("strategies", {}) or {}).get(STRATEGY_VERSION, {}) or {}),
        }
        now = datetime.now(UTC)
        today_start = now.date().isoformat() + "T00:00:00"

        try:
            account = self.broker.get_account()
        except Exception:
            account = None

        snapshot = self._get_exposure_snapshot(positions, account)
        snapshot["as_of"] = now.isoformat()
        snapshot["equity_as_of"] = now.isoformat()

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
            dynamic_active_set = set(dynamic_active)
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
                                elif sleep_mode_active:
                                    emergency_exit_mode = "sleep"
                                else:
                                    emergency_exit_mode = "normal"
                                emergency_exit_wait_seconds = None
                                emergency_exit_auto_execute_due_at = None
                                emergency_exit_final_decision = "approval_required"

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

                score_components = self._canonical_trade_score_components(
                    symbol=symbol, signal=signal, price=price, price_at=price_at,
                    bars=bars, asset_score=asset_score, now=now,
                )
                prev_price = float(score_components["previous_price"])
                session_start_price = float(score_components["session_start_price"])
                price_change = price - prev_price
                price_change_pct = (price / prev_price - 1) * 100 if prev_price > 0 else 0.0
                session_change = price - session_start_price
                session_change_pct = (price / session_start_price - 1) * 100 if session_start_price > 0 else 0.0
                score_vol = float(score_components["score_vol"])
                volatility_regime = str(score_components["volatility_regime"])
                volatility_gate_result = str(score_components["volatility_gate_result"])
                score = float(score_components["score"])
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
                final_notional = 0.0
                suggested_shares = 0.0
                stop_price = None
                stop_distance_pct = None
                stop_distance_dollars = None
                risk_budget = 0.0
                score_mult = 1.0
                vol_mult = 1.0
                stop_method = "default"
                risk_based_shares = 0.0
                score_adjusted_notional = 0.0
                vol_adjusted_notional = 0.0
                base_notional = 5.0
                atr_value = None
                technical_stop_price = None
                stop_validation_status = "blocked"
                sizing_caps: dict[str, float] = {}
                binding_caps: list[str] = []
                sizing_policy_version = SIZING_POLICY_VERSION
                stop_policy_version = STOP_POLICY_VERSION
                sizing_formula_version = RISK_DECISION_VERSION
                phase4_mode = "disabled"
                phase4_exploration_heat_cap_pct = None
                phase4_exploration_gross_cap_pct = None
                performance_snapshot_id = None
                policy_decision_id = None
                strategy_quality_score = None
                strategy_state = None
                strategy_risk_multiplier = None
                permitted_stop_risk_pct = None
                strategy_policy_version = None
                binding_policy_reason = None
                average_dollar_volume = None
                strategy_registry_snapshot_id = None
                strategy_sleeve = None
                sleeve_allocation_id = None
                sleeve_stop_risk_ceiling = None
                sleeve_notional_ceiling = None
                strategy_sleeve_payload = None

                if signal.action == "ENTRY" and signal.side == "buy":
                    size_dict = self._calculate_dynamic_size(
                        symbol, score, volatility_regime, price, bars, snapshot,
                        is_add=is_add, strategy_version=signal.strategy_version,
                    )
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
                    atr_value = size_dict.get("atr_value")
                    technical_stop_price = size_dict.get("technical_stop_price")
                    stop_validation_status = size_dict.get("stop_validation_status", "blocked")
                    sizing_caps = size_dict.get("sizing_caps") or {}
                    binding_caps = size_dict.get("binding_caps") or []
                    sizing_policy_version = size_dict.get("sizing_policy_version", SIZING_POLICY_VERSION)
                    stop_policy_version = size_dict.get("stop_policy_version", STOP_POLICY_VERSION)
                    sizing_formula_version = size_dict.get("formula_version", RISK_DECISION_VERSION)
                    phase4_mode = size_dict.get("phase4_mode", "disabled")
                    phase4_exploration_heat_cap_pct = size_dict.get("phase4_exploration_heat_cap_pct")
                    phase4_exploration_gross_cap_pct = size_dict.get("phase4_exploration_gross_cap_pct")
                    performance_snapshot_id = size_dict.get("performance_snapshot_id")
                    policy_decision_id = size_dict.get("policy_decision_id")
                    strategy_quality_score = size_dict.get("strategy_quality_score")
                    strategy_state = size_dict.get("strategy_state")
                    strategy_risk_multiplier = size_dict.get("strategy_risk_multiplier")
                    permitted_stop_risk_pct = size_dict.get("permitted_stop_risk_pct")
                    strategy_policy_version = size_dict.get("strategy_policy_version")
                    binding_policy_reason = size_dict.get("binding_policy_reason")
                    average_dollar_volume = size_dict.get("average_dollar_volume")
                    strategy_registry_snapshot_id = size_dict.get("strategy_registry_snapshot_id")
                    strategy_sleeve = size_dict.get("strategy_sleeve")
                    sleeve_allocation_id = size_dict.get("sleeve_allocation_id")
                    sleeve_stop_risk_ceiling = size_dict.get("sleeve_stop_risk_ceiling")
                    sleeve_notional_ceiling = size_dict.get("sleeve_notional_ceiling")
                    strategy_sleeve_payload = size_dict.get("strategy_sleeve_payload")

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
                position_management_decision_id = None
                position_management_sell_fraction = None
                position_management_sell_qty = None
                position_management_add_notional = None
                trend_management_decision = None
                if has_position and self.config.get("position_management", {}).get("enabled", True):
                    previous_pm_state = self._position_management_state(symbol)
                    position_age_days = None
                    if previous_pm_state and previous_pm_state.get("created_at"):
                        try:
                            created_dt = datetime.fromisoformat(str(previous_pm_state["created_at"]).replace("Z", "+00:00"))
                            created_dt = created_dt.replace(tzinfo=UTC) if created_dt.tzinfo is None else created_dt.astimezone(UTC)
                            position_age_days = max(0.0, (now - created_dt).total_seconds() / 86400.0)
                        except Exception:
                            position_age_days = None
                    position_age_cycles_rows = self.storage.fetch_all(
                        "SELECT COUNT(*) AS cnt FROM position_management_decisions WHERE symbol=?",
                        (symbol,),
                    )
                    position_age_cycles = int(position_age_cycles_rows[0]["cnt"] or 0) if position_age_cycles_rows else 0
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
                        position_age_days=position_age_days,
                        position_age_cycles=position_age_cycles,
                        now=now,
                    )
                    position_management_decision_id = self._record_position_management(position_management_decision, now)
                    try:
                        trend_management_decision = self._evaluate_held_position_trend(
                            symbol=symbol,
                            current_price=price,
                            average_entry_price=float(avg_entry_price or 0.0),
                            bars=bars,
                            score=score,
                            volatility_regime=volatility_regime,
                            strategy_version=signal.strategy_version,
                            position_age_days=position_age_days,
                            normal_exit_signal=normal_exit_signal,
                            emergency_exit=emergency_exit_triggered == 1,
                            deterioration_detected=position_management_decision.decision_type in {
                                "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT", "TIME_STOP_EXIT",
                            },
                            now=now,
                        )
                    except Exception as exc:
                        self.storage.audit(self.run_id, "trend_management_evidence_blocked", {
                            "symbol": symbol, "error_type": type(exc).__name__,
                            "add_authorized": False, "sell_safety_preserved": True,
                        })
                    trend_mode = trend_management_decision.mode if trend_management_decision else None
                    if trend_management_decision and trend_management_decision.is_exit_required and emergency_exit_triggered != 1:
                        position_management_sell_fraction = 1.0
                        position_management_sell_qty = float(qty_held or 0.0)
                        signal = dataclasses.replace(
                            signal, action="EXIT", side="sell",
                            reason=f"EXIT_REQUIRED: {trend_management_decision.reason}",
                        )
                        is_add = False
                        exit_trigger_reason = trend_management_decision.reason
                        score = max(score, float(self.config.get("ai", {}).get("ai_review_min_score", 65)))
                    elif (
                        trend_management_decision
                        and trend_management_decision.defer_fixed_profit_target
                        and position_management_decision.decision_type == "TAKE_PROFIT_PARTIAL"
                    ):
                        signal = dataclasses.replace(
                            signal, action="HOLD", side=None,
                            reason=f"{trend_mode}: fixed profit target deferred; monotonic trend stop remains authoritative",
                        )
                        is_add = False
                    if position_management_decision.is_actionable and emergency_exit_triggered != 1:
                        if trend_management_decision and trend_management_decision.is_exit_required:
                            pass
                        elif trend_management_decision and trend_management_decision.defer_fixed_profit_target and position_management_decision.decision_type == "TAKE_PROFIT_PARTIAL":
                            pass
                        elif position_management_decision.action == "sell":
                            position_management_sell_fraction = (
                                trend_management_decision.recommended_partial_exit_fraction
                                if trend_management_decision is not None
                                else position_management_decision.suggested_sell_fraction or 1.0
                            )
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
                            # Position management owns the final action. A prior
                            # healthy-pullback check may have set is_add before
                            # this sell decision was classified.
                            is_add = False
                            exit_trigger_reason = decision_reason
                            score = max(score, float(self.config.get("ai", {}).get("ai_review_min_score", 65)))
                        elif position_management_decision.decision_type == "HEALTHY_PULLBACK_ADD":
                            if trend_mode in {"DEFENSIVE_HARVEST", "PROFIT_PROTECT", "EXIT_REQUIRED"}:
                                signal = dataclasses.replace(
                                    signal, action="HOLD", side=None,
                                    reason=f"{trend_mode}: winner expansion is not currently authorized",
                                )
                                is_add = False
                            else:
                                signal = dataclasses.replace(
                                    signal, action="ENTRY", side="buy",
                                    reason=position_management_decision.reason,
                                )
                                is_add = True
                                score = max(score, float(self.config.get("position_management", {}).get("healthy_pullback_add", {}).get("minimum_trade_score", 85)))
                                # The historical fixed display amount is never
                                # executable; recompute through live ADD risk.
                                size_dict = self._calculate_dynamic_size(
                                    symbol, score, volatility_regime, price, bars, snapshot,
                                    is_add=True, strategy_version=signal.strategy_version,
                                )
                                final_notional = size_dict["final_notional"]
                                suggested_shares = size_dict["suggested_shares"]
                                position_management_add_notional = final_notional
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
                                atr_value = size_dict.get("atr_value")
                                technical_stop_price = size_dict.get("technical_stop_price")
                                stop_validation_status = size_dict.get("stop_validation_status", "blocked")
                                sizing_caps = size_dict.get("sizing_caps") or {}
                                binding_caps = size_dict.get("binding_caps") or []
                                phase4_mode = size_dict.get("phase4_mode", "disabled")
                                performance_snapshot_id = size_dict.get("performance_snapshot_id")
                                policy_decision_id = size_dict.get("policy_decision_id")
                                strategy_quality_score = size_dict.get("strategy_quality_score")
                                strategy_state = size_dict.get("strategy_state")
                                strategy_risk_multiplier = size_dict.get("strategy_risk_multiplier")
                                permitted_stop_risk_pct = size_dict.get("permitted_stop_risk_pct")
                                strategy_policy_version = size_dict.get("strategy_policy_version")
                                binding_policy_reason = size_dict.get("binding_policy_reason")
                                average_dollar_volume = size_dict.get("average_dollar_volume")
                                strategy_registry_snapshot_id = size_dict.get("strategy_registry_snapshot_id")
                                strategy_sleeve = size_dict.get("strategy_sleeve")
                                sleeve_allocation_id = size_dict.get("sleeve_allocation_id")
                                sleeve_stop_risk_ceiling = size_dict.get("sleeve_stop_risk_ceiling")
                                sleeve_notional_ceiling = size_dict.get("sleeve_notional_ceiling")
                                strategy_sleeve_payload = size_dict.get("strategy_sleeve_payload")
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
                self._phase1_bar_cache[symbol] = bars

                profile_results.append({
                    "symbol": symbol,
                    "universe_source": "dynamic" if symbol in dynamic_active_set else "static",
                    "approved_dynamic_paper_tradable": symbol in dynamic_active_set,
                    "approved_market_profile": profile_key,
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
                    "phase4_mode": phase4_mode,
                    "phase4_exploration_heat_cap_pct": phase4_exploration_heat_cap_pct,
                    "phase4_exploration_gross_cap_pct": phase4_exploration_gross_cap_pct,
                    "performance_snapshot_id": performance_snapshot_id,
                    "policy_decision_id": policy_decision_id,
                    "strategy_quality_score": strategy_quality_score,
                    "strategy_state": strategy_state,
                    "strategy_risk_multiplier": strategy_risk_multiplier,
                    "permitted_stop_risk_pct": permitted_stop_risk_pct,
                    "strategy_policy_version": strategy_policy_version,
                    "binding_policy_reason": binding_policy_reason,
                    "average_dollar_volume": average_dollar_volume,
                    "strategy_registry_snapshot_id": strategy_registry_snapshot_id,
                    "strategy_sleeve": strategy_sleeve,
                    "sleeve_allocation_id": sleeve_allocation_id,
                    "sleeve_stop_risk_ceiling": sleeve_stop_risk_ceiling,
                    "sleeve_notional_ceiling": sleeve_notional_ceiling,
                    "strategy_sleeve_payload": strategy_sleeve_payload,
                    "score_improvement": add_score_improvement,
                    "score_multiplier": score_mult,
                    "volatility_multiplier": vol_mult,
                    "stop_model_used": stop_method,
                    "stop_validation_status": stop_validation_status,
                    "stop_policy_version": stop_policy_version,
                    "technical_stop_price": technical_stop_price,
                    "sizing_caps": sizing_caps,
                    "binding_caps": binding_caps,
                    "sizing_policy_version": sizing_policy_version,
                    "formula_version": sizing_formula_version,
                    "risk_based_shares": risk_based_shares,
                    "score_adjusted_notional": score_adjusted_notional,
                    "vol_adjusted_notional": vol_adjusted_notional,
                    "base_notional": base_notional,
                    "position_management_decision": dataclasses.asdict(position_management_decision) if position_management_decision else None,
                    "position_management_decision_id": position_management_decision_id,
                    "position_management_decision_type": position_management_decision.decision_type if position_management_decision else None,
                    "position_lifecycle_id": PositionLifecycleManager(self.storage).active_id(symbol) if has_position else None,
                    "cycle_created_at": now.isoformat(),
                    "position_management_sell_fraction": position_management_sell_fraction,
                    "position_management_sell_qty": position_management_sell_qty,
                    "position_management_add_notional": position_management_add_notional,
                    "trend_management_decision": dataclasses.asdict(trend_management_decision) if trend_management_decision else None,
                    "management_mode": trend_management_decision.mode if trend_management_decision else None,
                })

            # Populate watchlist order first
            for idx, res in enumerate(profile_results):
                res["watchlist_order"] = idx + 1
                res["total_active_symbols"] = len(active_watchlist)

            self._run_phase2_shadow(profile_results, now)

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

            # Split exits vs buys. The exit-first gate is evaluated from the
            # current broker snapshot and current-cycle decisions, not from a
            # historical market-memory flag.
            exit_candidates = [r for r in profile_results if r["signal"].action == "EXIT" and r["has_position"]]
            buy_candidates_all = [r for r in profile_results if r["signal"].action == "ENTRY" and r["signal"].side == "buy"]

            exit_blocker = self._exit_blocker_context(
                orders,
                positions=positions,
                current_cycle_exits=exit_candidates,
                validation_at=now,
            )
            if exit_blocker.get("active"):
                self._current_cycle_exit_blocker = {
                    **exit_blocker,
                    "run_id": self.run_id,
                }
            exit_candidates_exist = bool(exit_blocker.get("active"))

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
                    "notional": res.get("final_notional", 0.0)
                })
                mock_prop = {
                    "symbol": symbol,
                    "universe_source": res.get("universe_source"),
                    "approved_dynamic_paper_tradable": res.get("approved_dynamic_paper_tradable", False),
                    "approved_market_profile": res.get("approved_market_profile"),
                    "side": "buy",
                    "action": "add" if res.get("is_add") else "entry",
                    "is_add": res.get("is_add", False),
                    "latest_price": res["price"],
                    "price_at": str(res["price_at"]),
                    "historical_bars": len(res["bars"]),
                    "volume": res["volume"],
                    "notional": res.get("final_notional", 0.0),
                    "created_at": now.isoformat(),
                    "expires_at": res["expiry"].isoformat(),
                    "strategy_version": res["signal"].strategy_version,
                    "reason": res["signal"].reason,
                    "stop_price": res.get("stop_price"),
                    "stop_distance_dollars": res.get("stop_distance_dollars"),
                    "atr_value": res.get("atr_value"),
                    "technical_stop_price": res.get("technical_stop_price"),
                    "stop_model_used": res.get("stop_model_used"),
                    "stop_validation_status": res.get("stop_validation_status"),
                    "cluster_name": self._get_symbol_cluster(symbol),
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
                # Proposal creation has no count cap. Every candidate that has
                # already passed signal, data, dedupe, and RiskEngine checks may
                # be proposed; execution risk controls remain authoritative.
                allowed_buy_symbols = {c["symbol"] for c in buy_candidates}
                suppressed_buy_symbols: set[str] = set()

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
            rotation_exit_proposals: list[dict[str, Any]] = []
            rotation_candidate_available = bool(
                (self.config.get("rotation", {}) or {}).get("enabled")
                and exit_candidates_exist
                and buy_candidates
            )

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
                    blocker_reason = self._exit_blocker_display_reason(exit_blocker)
                    dedupe_reason = f"suppressed due to exit priority: {blocker_reason}"
                    no_action_reason = f"new buy blocked because {blocker_reason}"
                    candidate_suppression_reason = "suppressed_due_to_exit_priority"
                    self.storage.audit(self.run_id, "proposal_suppressed", {
                        "symbol": symbol,
                        "reason": "suppressed_due_to_exit_priority",
                        "score": score,
                        "exit_blocker": self._exit_blocker_audit_detail(exit_blocker, "active", blocker_reason),
                    })

                # Check proposal send-time freshness
                price_age_seconds = None
                if price_at:
                    try:
                        if hasattr(price_at, "timestamp"):
                            if price_at.tzinfo is None:
                                price_at_utc = price_at.replace(tzinfo=UTC)
                            else:
                                price_at_utc = price_at.astimezone(UTC)
                            price_age_seconds = (now - price_at_utc).total_seconds()
                        else:
                            price_at_dt = datetime.fromisoformat(str(price_at).replace("Z", "+00:00"))
                            if price_at_dt.tzinfo is None:
                                price_at_dt = price_at_dt.replace(tzinfo=UTC)
                            price_age_seconds = (now - price_at_dt).total_seconds()
                    except Exception as e:
                        logger.warning("Error calculating price age for symbol %s: %s", symbol, e)

                send_time_threshold = float(self.config.get("telegram", {}).get("proposal_price_freshness_threshold_seconds", 60.0))
                price_is_stale_at_send = False
                if price <= 0.0 or price_at is None or price_age_seconds is None or price_age_seconds > send_time_threshold or price_age_seconds < -5:
                    price_is_stale_at_send = True

                if proposal_allowed and price_is_stale_at_send:
                    proposal_allowed = False
                    no_action_reason = "proposal not sent: stale Alpaca price at proposal creation (price timestamp must be fresh)"
                    self.storage.audit(self.run_id, "proposal_blocked", {
                        "symbol": symbol,
                        "reasons": [no_action_reason],
                        "price": price,
                        "price_at": str(price_at),
                        "price_age_seconds": price_age_seconds
                    })

                if not proposal_allowed:
                    if no_action_reason:
                        pass
                    elif is_buy and suppressed_by_sleep_mode == 1:
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
                            notional = res.get("final_notional", 0.0)
                            qty_val = res.get("suggested_shares", 0.0) if (signal.action == "ENTRY" and signal.side == "buy") else (res.get("position_management_sell_qty") or qty_held)
                            if pm_decision_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT", "TIME_STOP_EXIT"}:
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

                            proposal_action = _proposal_action_for_signal(signal.action, signal.side, bool(res.get("is_add")))
                            proposal_is_add = proposal_action == "add"
                            port_context = self._portfolio_context({
                                "symbol": symbol,
                                "side": signal.side,
                                "action": proposal_action,
                                "notional": notional,
                                "strategy_version": signal.strategy_version,
                                "volatility_regime": volatility_regime,
                                "score_improvement": res.get("score_improvement"),
                                "position_lifecycle_id": res.get("position_lifecycle_id"),
                                "higher_highs_and_lows": bool(
                                    len(bars) >= 20
                                    and float(bars["high"].tail(10).mean()) >= float(bars["high"].iloc[-20:-10].mean())
                                    and float(bars["low"].tail(10).mean()) >= float(bars["low"].iloc[-20:-10].mean())
                                ),
                                "phase4_mode": res.get("phase4_mode"),
                                "stop_risk_dollars": res.get("stop_risk_dollars"),
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
                                "universe_source": res.get("universe_source"),
                                "approved_dynamic_paper_tradable": res.get("approved_dynamic_paper_tradable", False),
                                "approved_market_profile": res.get("approved_market_profile"),
                                "side": signal.side,
                                "action": proposal_action,
                                "is_add": 1 if proposal_is_add else 0,
                                "notional": notional,
                                "qty": qty_val,
                                "notional_adjustment_note": notional_adjustment_note,
                                "latest_price": price,
                                "price_at": str(price_at),
                                "proposal_price": price,
                                "proposal_price_timestamp": price_at.isoformat() if hasattr(price_at, "isoformat") else str(price_at),
                                "proposal_price_source": "alpaca",
                                "proposal_price_age_seconds_at_send": price_age_seconds,
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
                                "phase4_mode": res.get("phase4_mode", "disabled"),
                                "phase4_probe_heat_cap_pct": res.get("phase4_probe_heat_cap_pct"),
                                "phase4_probe_gross_cap_pct": res.get("phase4_probe_gross_cap_pct"),
                                "phase4_probe_max_active_count": res.get("phase4_probe_max_active_count"),
                                "average_dollar_volume": res.get("average_dollar_volume"),
                                "performance_snapshot_id": res.get("performance_snapshot_id"),
                                "policy_decision_id": res.get("policy_decision_id"),
                                "strategy_quality_score": res.get("strategy_quality_score"),
                                "strategy_state": res.get("strategy_state"),
                                "strategy_risk_multiplier": res.get("strategy_risk_multiplier"),
                                "permitted_stop_risk_pct": res.get("permitted_stop_risk_pct"),
                                "strategy_policy_version": res.get("strategy_policy_version"),
                                "binding_policy_reason": res.get("binding_policy_reason"),
                                "strategy_registry_snapshot_id": res.get("strategy_registry_snapshot_id"),
                                "strategy_sleeve": res.get("strategy_sleeve"),
                                "sleeve_allocation_id": res.get("sleeve_allocation_id"),
                                "sleeve_stop_risk_ceiling": res.get("sleeve_stop_risk_ceiling"),
                                "sleeve_notional_ceiling": res.get("sleeve_notional_ceiling"),
                                "strategy_sleeve_payload": res.get("strategy_sleeve_payload"),
                                "risk_value": res.get("risk_value"),
                                "risk_unit": res.get("risk_unit"),
                                "conversion_equity": res.get("conversion_equity"),
                                "conversion_equity_as_of": res.get("conversion_equity_as_of"),
                                "risk_formula_version": res.get("risk_formula_version"),
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
                                "stop_validation_status": res.get("stop_validation_status"),
                                "stop_policy_version": res.get("stop_policy_version", STOP_POLICY_VERSION),
                                "technical_stop_price": res.get("technical_stop_price"),
                                "initial_stop_price": stop_price if stop_price is not None and price is not None and float(stop_price) < float(price) else None,
                                "initial_risk_per_share": (float(price) - float(stop_price)) if stop_price is not None and price is not None and float(stop_price) < float(price) else None,
                                "initial_risk_pct": stop_distance_pct if stop_price is not None and price is not None and float(stop_price) < float(price) else None,
                                "initial_risk_dollars": ((float(price) - float(stop_price)) * float(qty_val)) if stop_price is not None and price is not None and qty_val is not None and float(stop_price) < float(price) else None,
                                "stop_model": stop_model_used,
                                "stop_source": stop_model_used,
                                "entry_price_for_r": price,
                                "risk_model_version": SIZING_POLICY_VERSION,
                                "sizing_policy_version": res.get("sizing_policy_version", SIZING_POLICY_VERSION),
                                "sizing_caps": res.get("sizing_caps", {}),
                                "binding_caps": res.get("binding_caps", []),
                                "formula_version": res.get("formula_version", RISK_DECISION_VERSION),
                                "evidence_version": EVIDENCE_VERSION,
                                "effective_config_hash": self.config.get("effective_config_hash"),
                                "r_multiple_unavailable_reason": (
                                    None
                                    if stop_price is not None and price is not None and float(stop_price) < float(price)
                                    else ("r_multiple_unavailable_initial_stop_missing" if stop_price is None else "r_multiple_unavailable_initial_stop_invalid")
                                ),
                                "risk_budget": risk_budget,
                                "risk_budget_dollars": res.get("risk_budget_dollars", risk_budget),
                                "stop_risk_dollars": res.get("stop_risk_dollars", ((float(notional) / float(price) * float(stop_distance_dollars)) if notional > 0 and price > 0 and stop_distance_dollars else 0.0)),
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
                                "atr_value": res.get("atr_value", atr_value),
                                "trend_evidence": {key: signal.indicators.get(key) for key in ("ma_50", "ma_200")},
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

                            if self._operational_adaptive_enabled() and is_buy and proposal_action in {"entry", "add"}:
                                self._should_auto_execute(proposal)
                                # First prove the candidate and canonical inputs are
                                # executable, then let the independent engines own
                                # conviction and operational size. The actual size is
                                # checked again against the complete risk engine below.
                                baseline_decision = self._risk_engine(proposal_id, "proposal_baseline").evaluate(proposal, port_context)
                                proposal["adaptive_conviction"] = self._record_adaptive_conviction(
                                    proposal,
                                    res,
                                    port_context,
                                    risk_checks_passed=baseline_decision.passed,
                                    stage="proposal",
                                )
                                if proposal["adaptive_conviction"] is not None:
                                    proposal["adaptive_sizing"] = self._record_adaptive_sizing(
                                        proposal,
                                        res,
                                        port_context,
                                        proposal["adaptive_conviction"],
                                        stage="proposal",
                                    )
                                operational_adaptive = proposal.get("adaptive_sizing") or {}
                                adaptive_notional = float(operational_adaptive.get("operational_notional") or 0.0)
                                adaptive_quantity = float(operational_adaptive.get("operational_quantity") or 0.0)
                                if not baseline_decision.passed or adaptive_notional <= 0 or adaptive_quantity <= 0:
                                    reasons = baseline_decision.reasons if not baseline_decision.passed else ["adaptive operational sizing found no safe executable size"]
                                    no_action_reason = f"blocked by risk checks: {'; '.join(reasons)}"
                                    proposal_allowed = False
                                else:
                                    proposal["baseline_operational_notional"] = float(proposal.get("notional") or 0.0)
                                    proposal["notional"] = adaptive_notional
                                    proposal["qty"] = adaptive_quantity
                                    proposal["displayed_adaptive_ceiling"] = adaptive_notional
                                    proposal["approved_quantity_ceiling"] = adaptive_quantity
                                    proposal["approved_stop_risk_ceiling"] = float(operational_adaptive.get("stop_risk_dollars") or 0.0)
                                    proposal["stop_risk_dollars"] = adaptive_quantity * float(proposal.get("stop_distance_dollars") or 0.0)
                                    proposal["initial_risk_dollars"] = proposal["stop_risk_dollars"]
                                    proposal["initial_risk_pct"] = (
                                        proposal["stop_risk_dollars"] / float(snapshot["portfolio_equity"]) * 100.0
                                        if float(snapshot["portfolio_equity"] or 0.0) > 0 else 0.0
                                    )
                                    proposal["risk_budget"] = proposal["stop_risk_dollars"]
                                    proposal["risk_budget_dollars"] = proposal["stop_risk_dollars"]
                                    notional = adaptive_notional
                                    qty_val = adaptive_quantity
                                    risk_budget = proposal["stop_risk_dollars"]
                                    proposal["deployment_mode"] = proposal["adaptive_conviction"].get("deployment_mode")
                                    proposal["opportunity_class"] = proposal["adaptive_conviction"].get("opportunity_class")
                                    proposal["permitted_stop_risk_pct"] = proposal["adaptive_conviction"].get("recommended_stop_risk_pct")
                                    proposal["sizing_caps"] = dict(operational_adaptive.get("sizing_caps") or {})
                                    proposal["binding_caps"] = [operational_adaptive.get("binding_adaptive_cap")]
                                    if (
                                        proposal_action == "add"
                                        and (self.config.get("winner_expansion", {}) or {}).get("enabled") is True
                                        and (self.config.get("phase3", {}) or {}).get("active") is True
                                        and (self.config.get("phase4", {}) or {}).get("active") is True
                                    ):
                                        try:
                                            winner_decision, _, _ = self._evaluate_winner_expansion(
                                                proposal,
                                                decision_stage="proposal",
                                            )
                                            # The canonical winner engine owns
                                            # risk consumed/released and the
                                            # required persisted stop. Its
                                            # result replaces leg-only risk.
                                            proposal["stop_risk_dollars"] = winner_decision.risk_decision.consumed_risk
                                            proposal["risk_budget"] = winner_decision.risk_decision.consumed_risk
                                            proposal["risk_budget_dollars"] = winner_decision.risk_decision.consumed_risk
                                        except Exception as exc:
                                            no_action_reason = f"winner expansion blocked: {str(exc)}"
                                            proposal_allowed = False
                                    port_context = self._portfolio_context(proposal)
                                    proposal["proposed_total_exposure_pct"] = port_context.get("proposed_total_exposure_pct")
                                    proposal["proposed_cluster_exposure_pct"] = port_context.get("proposed_cluster_exposure_pct")
                                    decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, port_context)
                                    if proposal_allowed and not decision.passed:
                                        no_action_reason = f"blocked by risk checks: {'; '.join(decision.reasons)}"
                                        proposal_allowed = False
                            elif emergency_exit_triggered == 1:
                                # Exits remain independent of entry conviction and sizing.
                                pass
                            else:
                                self._should_auto_execute(proposal)
                                decision = self._risk_engine(proposal_id, "proposal").evaluate(proposal, port_context)
                                if not decision.passed:
                                    no_action_reason = f"blocked by risk checks: {'; '.join(decision.reasons)}"
                                    proposal_allowed = False

                            if proposal_allowed:
                                if is_buy:
                                    # AI is optional commentary only. It cannot
                                    # decide direction, size, stop, expiry, or
                                    # deterministic eligibility.
                                    require_gpt = False
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

                                    proposal_generated = True
                                    no_action_reason = "proposal generated"
                                    any_generated = True
                                    if not gpt_called:
                                        deferred_ai_review_reason = "commentary_unavailable"
                                        self.storage.audit(self.run_id, "ai_commentary_deferred", {
                                            "symbol": symbol, "reason": "commentary_unavailable", "eligibility_unchanged": True,
                                        })
                                    if review:
                                        proposal["ai_review_status"] = "Completed" if gpt_called else "Not available"
                                        proposal["ai_confidence"] = review.get("gpt_confidence", "Not called")
                                        proposal["ai_caution"] = review.get("gpt_caution", "Low")
                                elif is_exit:
                                    if emergency_exit_triggered == 1:
                                        # Emergency exits create approval-gated paper sell proposals.
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
                                        else:
                                            proposal["status"] = "pending"
                                            proposal["emergency_exit_final_decision"] = emergency_exit_final_decision
                                        if emergency_exit_mode != "blocked":
                                            self.telegram.send_message(
                                                f"🚨 [EMERGENCY EXIT ALERT] Paper sell proposal created for {symbol} ({qty_held} shares). Risk score: {emergency_exit_score:.1f}. Reason: {emergency_exit_trigger_reason}.\n\n"
                                                f"Reply YES to approve or NO to reject. Final validation is still required before any paper order."
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

                if proposal is not None:
                    proposal.update({
                        "strategy_version": signal.strategy_version,
                        "performance_snapshot_id": res.get("performance_snapshot_id"),
                        "policy_decision_id": res.get("policy_decision_id"),
                        "strategy_quality_score": res.get("strategy_quality_score"),
                        "strategy_state": res.get("strategy_state"),
                        "strategy_risk_multiplier": res.get("strategy_risk_multiplier"),
                        "permitted_stop_risk_pct": proposal.get("permitted_stop_risk_pct", res.get("permitted_stop_risk_pct")),
                        "strategy_policy_version": res.get("strategy_policy_version"),
                        "binding_policy_reason": res.get("binding_policy_reason"),
                        "strategy_registry_snapshot_id": res.get("strategy_registry_snapshot_id"),
                        "strategy_sleeve": res.get("strategy_sleeve"),
                        "sleeve_allocation_id": res.get("sleeve_allocation_id"),
                        "sleeve_stop_risk_ceiling": res.get("sleeve_stop_risk_ceiling"),
                        "risk_value": res.get("risk_value"),
                        "risk_unit": res.get("risk_unit"),
                        "conversion_equity": res.get("conversion_equity"),
                        "conversion_equity_as_of": res.get("conversion_equity_as_of"),
                        "risk_formula_version": res.get("risk_formula_version"),
                        "sleeve_notional_ceiling": res.get("sleeve_notional_ceiling"),
                    })

                res["proposal_allowed"] = proposal_allowed
                res["proposal_generated"] = proposal_generated
                res["proposal_id"] = proposal_id
                res["performance_action_decision"] = (
                    "proposed" if proposal_generated else
                    ("failed_final_validation" if no_action_reason and "final validation" in no_action_reason.lower() else
                     "blocked" if no_action_reason and "blocked" in no_action_reason.lower() else
                     "suppressed" if no_action_reason else "shadow_only")
                )
                res["performance_not_proposed_reason"] = None if proposal_generated else (no_action_reason or signal.reason)
                res["performance_candidate_suppression_reason"] = candidate_suppression_reason
                res["performance_price_age_seconds"] = price_age_seconds
                res["performance_decision_reasons"] = list(decision.reasons) if decision is not None and not decision.passed else []
                res["performance_proposed_notional"] = proposal.get("notional") if proposal else None
                res["performance_batch_id"] = None
                res["performance_proposal_payload"] = proposal or {}

                self.storage.execute(
                    "INSERT INTO market_memory(run_id,market_profile,symbol,price,prev_price,price_change,price_change_pct,session_start_price,session_change,volatility,signal,score,classification,reason,proposal_allowed,gpt_called,created_at,asset_score,asset_classification,symbol_rank,proposal_generated,no_action_reason,asset_selection_score,trade_decision_score,system_confidence,gpt_confidence,gpt_caution,expiry_minutes,expires_at_sgt,main_risk,volatility_regime,volatility_score_contribution,volatility_gate_result,dedupe_status,dedupe_reason,paper_size_adjustment,candidate_suppression_reason,deferred_ai_review_reason,true_score_rank,watchlist_order,setup_key,cooldown_applied,cooldown_remaining_minutes,cooldown_reason,revival_reason,last_proposal_status,score_delta,volatility_regime_change,exit_priority_applied,exit_trigger_reason,position_drawdown_pct,average_entry_price,latest_position_price,gpt_exit_explanation_status,gpt_exit_confidence,gpt_exit_caution,final_proposal_message_category,emergency_exit_score,emergency_exit_triggered,emergency_exit_trigger_reason,emergency_exit_hard_trigger,emergency_exit_mode,emergency_exit_wait_seconds,emergency_exit_user_response,emergency_exit_auto_execute_due_at,emergency_exit_auto_execute_attempted_at,emergency_exit_final_decision,emergency_exit_block_reason,current_price,atr_value,adverse_move_atr,minutes_to_close,sleep_mode_active,suppressed_by_sleep_mode,sleep_mode_reason,sleep_mode_suppressed_candidate,sleep_mode_started_at,sleep_mode_ended_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.run_id, profile_key, symbol, price, prev_price, price_change, price_change_pct, session_start_price, session_change, vol_20 or 0.0, signal.action, score, classification, signal.reason, int(proposal_allowed), int(gpt_called), now.isoformat(), asset_score, asset_classification, watchlist_order, int(proposal_generated), no_action_reason, asset_score, score, system_confidence, g_conf, g_caut, expiry_minutes, exp_sgt, m_risk, volatility_regime, score_vol, volatility_gate_result, dedupe_status, dedupe_reason, paper_size_adjustment, candidate_suppression_reason, deferred_ai_review_reason, true_score_rank, watchlist_order, setup_key, int(cooldown_applied), cooldown_remaining_minutes, cooldown_reason, revival_reason, last_proposal_status, score_delta, volatility_regime_change, int(exit_priority_applied), exit_trigger_reason, position_drawdown_pct, average_entry_price, latest_position_price, gpt_exit_explanation_status, gpt_exit_confidence, gpt_exit_caution, final_proposal_message_category, emergency_exit_score, emergency_exit_triggered, emergency_exit_trigger_reason, emergency_exit_hard_trigger, emergency_exit_mode, emergency_exit_wait_seconds, None, emergency_exit_auto_execute_due_at, None, emergency_exit_final_decision, emergency_exit_block_reason, price, atr_value, adverse_move_atr, minutes_to_close, 1 if sleep_mode_active else 0, suppressed_by_sleep_mode, sleep_mode_reason, sleep_mode_suppressed_candidate, sleep_mode_started_at, sleep_mode_ended_at)
                )
                self.storage.execute(
                    """UPDATE trade_proposals SET performance_snapshot_id=?,policy_decision_id=?,strategy_quality_score=?,
                       strategy_state=?,strategy_risk_multiplier=?,permitted_stop_risk_pct=?,strategy_policy_version=?,binding_policy_reason=? WHERE id=?""",
                    (res.get("performance_snapshot_id"), res.get("policy_decision_id"), res.get("strategy_quality_score"),
                     res.get("strategy_state"), res.get("strategy_risk_multiplier"), res.get("permitted_stop_risk_pct"),
                     res.get("strategy_policy_version"), res.get("binding_policy_reason"), proposal_id),
                )

                logger.info(
                    "Symbol: %s | Profile: %s | Asset Score: %.2f (%s) | Trade Score: %.2f (%s) | Watchlist Order: #%d | True Score Rank: %s | Previous-observation change: %.2f%% | UTC-day first-observation change: %.2f | Proposal Allowed: %s | GPT Called: %s | Proposal Generated: %s | No-Action Reason: %s",
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
                if pm_decision_type in {"TAKE_PROFIT_PARTIAL", "PROFIT_PROTECT_EXIT", "TRAILING_STOP_EXIT", "TIME_STOP_EXIT"}:
                    self.storage.execute(
                        "UPDATE profit_exit_events SET proposal_id=?, status='proposal_created' WHERE run_id=? AND symbol=? AND event_type=? AND proposal_id IS NULL",
                        (proposal_id, self.run_id, symbol, pm_decision_type),
                    )
                    self.storage.execute(
                        "UPDATE exit_review_events SET proposal_id=? WHERE run_id=? AND symbol=? AND review_type=? AND proposal_id IS NULL",
                        (proposal_id, self.run_id, symbol, pm_decision_type),
                    )

                if is_buy or (proposal_is_add and proposal_generated):
                    self.storage.execute(
                        "UPDATE trade_proposals SET sizing_caps_json=?,formula_versions_json=?,evidence_version=? WHERE id=?",
                        (json_dumps(proposal.get("sizing_caps") or {}), json_dumps({"stop_policy": STOP_POLICY_VERSION, "sizing_policy": SIZING_POLICY_VERSION, "risk_decision": RISK_DECISION_VERSION, "accounting": ACCOUNTING_VERSION, "evidence": EVIDENCE_VERSION}), EVIDENCE_VERSION, proposal_id),
                    )

                if is_buy or (proposal_is_add and proposal_generated):
                    sizing_decision_id = str(uuid.uuid4())
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
                            sizing_decision_id, self.run_id, symbol, now.isoformat(),
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
                    self.storage.execute(
                        """UPDATE position_sizing_decisions SET
                           stop_policy_version=?,sizing_policy_version=?,formula_version=?,sizing_caps_json=?,binding_caps_json=?,evidence_version=?,config_hash=?,
                           performance_snapshot_id=?,policy_decision_id=?,strategy_quality_score=?,strategy_state=?,strategy_risk_multiplier=?,
                           permitted_stop_risk_pct=?,strategy_policy_version=?,binding_policy_reason=?
                           WHERE id=?""",
                        (STOP_POLICY_VERSION, SIZING_POLICY_VERSION, RISK_DECISION_VERSION,
                         json_dumps(proposal.get("sizing_caps") or {}), json_dumps(proposal.get("binding_caps") or []),
                         EVIDENCE_VERSION, self.config.get("effective_config_hash"), res.get("performance_snapshot_id"),
                         res.get("policy_decision_id"), res.get("strategy_quality_score"), res.get("strategy_state"),
                         res.get("strategy_risk_multiplier"), proposal.get("permitted_stop_risk_pct", res.get("permitted_stop_risk_pct")), res.get("strategy_policy_version"),
                         res.get("binding_policy_reason"), sizing_decision_id),
                    )

                self.storage.execute("INSERT INTO ai_reviews(run_id,proposal_id,summary,risks,caution_level,payload,created_at) VALUES(?,?,?,?,?,?,?)", (self.run_id, proposal_id, review["summary"], json_dumps(review["risks"]), review["caution_level"], json_dumps(review), iso_now()))

                if rotation_candidate_available and is_buy and proposal.get("status", "pending") == "pending" and emergency_exit_triggered != 1:
                    # The grouped message is the sole approval surface for the
                    # displayed contingent BUY.
                    pass
                elif rotation_candidate_available and is_exit and proposal.get("status", "pending") == "pending" and emergency_exit_triggered != 1:
                    rotation_exit_proposals.append(proposal)
                elif batch_mode_enabled and proposal.get("status", "pending") == "pending" and emergency_exit_triggered != 1 and (is_buy or is_exit):
                    batch_proposals.append(proposal)
                else:
                    res_tg = self.telegram.send_message(format_proposal_message(proposal, self.config))
                    if res_tg and isinstance(res_tg, dict) and "message_id" in res_tg:
                        self.storage.execute("UPDATE trade_proposals SET telegram_message_id=? WHERE id=?", (str(res_tg["message_id"]), proposal_id))

            if rotation_exit_proposals:
                try:
                    rotation_candidates = [
                        candidate for candidate in buy_candidates
                        if candidate.get("symbol") in allowed_buy_symbols
                        and not candidate.get("preproposal_block_reason")
                        and not candidate.get("risk_budget_block_reason")
                    ]
                    rotation_group = self._create_rotation_group(
                        rotation_exit_proposals, rotation_candidates, now
                    )
                    self.telegram.send_message(self._format_rotation_group_message(rotation_group))
                except Exception as exc:
                    self.storage.audit(self.run_id, "rotation_group_creation_blocked", {
                        "error_type": type(exc).__name__, "exit_proposal_count": len(rotation_exit_proposals),
                        "contingent_candidate_count": len(buy_candidates), "exit_safety_preserved": True,
                    })
                    # A rotation feature failure must never suppress a genuine
                    # reduce-risk proposal.
                    for exit_proposal in rotation_exit_proposals:
                        res_tg = self.telegram.send_message(format_proposal_message(exit_proposal, self.config))
                        if res_tg and isinstance(res_tg, dict) and "message_id" in res_tg:
                            self.storage.execute(
                                "UPDATE trade_proposals SET telegram_message_id=? WHERE id=?",
                                (str(res_tg["message_id"]), exit_proposal["id"]),
                            )
                    for candidate in rotation_candidates:
                        buy_proposal = candidate.get("performance_proposal_payload") or {}
                        if not buy_proposal:
                            continue
                        res_tg = self.telegram.send_message(format_proposal_message(buy_proposal, self.config))
                        if res_tg and isinstance(res_tg, dict) and "message_id" in res_tg:
                            self.storage.execute(
                                "UPDATE trade_proposals SET telegram_message_id=? WHERE id=?",
                                (str(res_tg["message_id"]), buy_proposal["id"]),
                            )
            if batch_mode_enabled and batch_proposals:
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

    def _proposal_capacity_digest_line(self, window_start_iso: str, window_end_iso: str, performance_lab: dict[str, Any]) -> str:
        top_blocker_rows = self.storage.fetch_all(
            """
            SELECT blocker, COUNT(*) AS cnt
            FROM performance_blockers
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) <= datetime(?)
            GROUP BY blocker
            ORDER BY cnt DESC, blocker
            LIMIT 1
            """,
            (window_start_iso, window_end_iso),
        )
        top_blocker = top_blocker_rows[0]["blocker"] if top_blocker_rows else "none"
        suppressed = int(performance_lab.get("suppressed") or 0)
        return f"Setup tracking: {suppressed} suppressed or observation-only. Top blocker: {top_blocker}. Proposal count is uncapped."

    def _format_strategy_allocation_report(self, *, compact: bool = False) -> str:
        registry = self.storage.fetch_all(
            """SELECT d.strategy_version,d.authorized,d.policy_state,d.reason
               FROM strategy_registry_decisions d
               JOIN strategy_registry_snapshots s ON s.id=d.snapshot_id
               WHERE s.id=(SELECT id FROM strategy_registry_snapshots ORDER BY evaluated_at DESC,id DESC LIMIT 1)
               ORDER BY d.strategy_version"""
        )
        allocation_rows = self.storage.fetch_all(
            "SELECT decision,allocation_class,reason,payload FROM phase4_allocation_decisions ORDER BY decided_at DESC,id DESC LIMIT 1"
        )
        authorized = [str(row["strategy_version"]) for row in registry if int(row.get("authorized") or 0) == 1]
        rejected = [str(row["strategy_version"]) for row in registry if int(row.get("authorized") or 0) == 0]
        if not allocation_rows:
            return "Multi-strategy allocation: unavailable; entry risk fails closed."
        allocation = allocation_rows[0]
        payload = json.loads(allocation.get("payload") or "{}")
        sleeves = payload.get("strategy_sleeves") or {}
        sleeve_text = ", ".join(
            f"{strategy} {float(value.get('allocated_risk') or 0.0):.4f} allocated/"
            f"{float(value.get('remaining_risk') or 0.0):.4f} remaining {value.get('risk_unit') or ''}".strip()
            for strategy, value in sorted(sleeves.items())
        ) or "none"
        summary = (
            f"Multi-strategy allocation: {allocation['decision']} ({allocation['allocation_class']}); "
            f"authorized {', '.join(authorized) if authorized else 'none'}; sleeves {sleeve_text}."
        )
        if compact:
            return summary
        return (
            summary
            + f"\nRejected/research-only: {', '.join(rejected) if rejected else 'none'}."
            + f"\nPortfolio rationale: {allocation.get('reason') or 'bounded by Phase 3 risk and allocation constraints'}."
        )

    def check_and_send_digest(self) -> None:
        digest_config = self.config.get("digest", {})
        if not digest_config.get("telegram_digest_enabled", True):
            self.storage.audit(self.run_id, "digest_blocked_reason", {"reason": "disabled"})
            return

        now = datetime.now(UTC)
        interval_minutes = digest_config.get("telegram_digest_interval_minutes", 30)

        try:
            market_open = self.broker.is_market_open()
        except Exception:
            market_open = False

        if not market_open and not digest_config.get("telegram_digest_send_when_market_closed", False):
            self.storage.audit(self.run_id, "digest_blocked_reason", {"reason": "market_closed"})
            return

        # 1. Throttling
        last_sent = self.storage.fetch_all(
            "SELECT sent_at FROM telegram_digests WHERE status='sent' ORDER BY sent_at DESC LIMIT 1"
        )
        if last_sent:
            last_sent_dt = datetime.fromisoformat(last_sent[0]["sent_at"].replace("Z", "+00:00")).replace(tzinfo=UTC)
            elapsed_mins = (now - last_sent_dt).total_seconds() / 60
            if elapsed_mins < (interval_minutes - 2):
                self.storage.audit(
                    self.run_id,
                    "digest_blocked_reason",
                    {"reason": "throttle", "elapsed_minutes": elapsed_mins, "interval_minutes": interval_minutes},
                )
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
            self.storage.audit(
                self.run_id,
                "digest_blocked_reason",
                {"reason": "insufficient_cycles", "cycle_count": cycle_count, "min_cycles": min_cycles, "window_start": window_start_iso, "window_end": now.isoformat()},
            )
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
            self.storage.audit(self.run_id, "digest_blocked_reason", {"reason": "no_market_memory_rows", "window_start": window_start_iso, "window_end": now.isoformat()})
            return

        import collections
        symbol_rows = collections.defaultdict(list)
        for row in rows:
            sym = row["symbol"]
            if allowed_symbols and sym not in allowed_symbols:
                continue
            symbol_rows[sym].append(row)

        if not symbol_rows:
            self.storage.audit(self.run_id, "digest_blocked_reason", {"reason": "no_allowed_symbols", "window_start": window_start_iso, "window_end": now.isoformat()})
            return
        self.storage.audit(
            self.run_id,
            "digest_eligible",
            {"cycle_count": cycle_count, "min_cycles": min_cycles, "symbol_count": len(symbol_rows), "window_start": window_start_iso, "window_end": now.isoformat()},
        )

        current_positions: list[Any] | None
        try:
            current_positions = list(self.broker.get_positions())
        except Exception:
            # Preserve unknown broker state so an active unresolved exit is not
            # cleared merely because the digest could not read positions.
            current_positions = None
        try:
            current_orders = list(self.broker.get_open_orders())
        except Exception:
            current_orders = []
        digest_exit_blocker = self._digest_exit_blocker_context(
            current_orders,
            current_positions,
            now,
        )
        cluster_holdings = self._cluster_holdings(current_positions or [])

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
                status_info = self._digest_market_memory_status(
                    sym, latest_row, set(obs_watchlist), cluster_holdings,
                    current_exit_blocker=digest_exit_blocker,
                )

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

        active_proposals = self.storage.fetch_all(
            """
            SELECT COUNT(*) as cnt
            FROM trade_proposals
            WHERE status IN ('pending','approved')
              AND datetime(created_at) <= datetime(?)
              AND (expires_at IS NULL OR datetime(expires_at) > datetime(?))
            """,
            (now.isoformat(), now.isoformat()),
        )
        active_proposal_cnt = active_proposals[0]["cnt"] if active_proposals else 0
        performance_lab_rows = self.storage.fetch_all(
            """
            SELECT COUNT(*) AS tracked,
                   SUM(CASE WHEN proposed=1 THEN 1 ELSE 0 END) AS proposed,
                   SUM(CASE WHEN proposed=0 THEN 1 ELSE 0 END) AS suppressed
            FROM performance_setups
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) <= datetime(?)
            """,
            (window_start_iso, now.isoformat()),
        )
        performance_lab = {
            "tracked": int(performance_lab_rows[0].get("tracked") or 0) if performance_lab_rows else 0,
            "proposed": int(performance_lab_rows[0].get("proposed") or 0) if performance_lab_rows else 0,
            "suppressed": int(performance_lab_rows[0].get("suppressed") or 0) if performance_lab_rows else 0,
            "outcome_status": "outcomes pending",
        }

        promotions = self.storage.fetch_all(
            "SELECT symbol, from_tier, to_tier, reason, payload FROM symbol_promotion_decisions WHERE created_at>=? ORDER BY created_at DESC LIMIT 12",
            (window_start_iso,),
        )
        demotions = self.storage.fetch_all(
            "SELECT symbol, reason FROM symbol_demotion_decisions WHERE created_at>=? ORDER BY created_at DESC LIMIT 12",
            (window_start_iso,),
        )
        static_reconciled = sorted({r["symbol"] for r in promotions if r["to_tier"] == "paper_tradable" and '"existing_static":true' in str(r.get("payload") or "")})
        to_observation = sorted({r["symbol"] for r in promotions if r["to_tier"] == "observation" and ('"universe_lane":"alpaca_compatible_us"' in str(r.get("payload") or "") or "universe_lane" not in str(r.get("payload") or ""))})
        global_observation = sorted({r["symbol"] for r in promotions if r["to_tier"] == "observation" and '"universe_lane":"global_research_only"' in str(r.get("payload") or "")})
        to_tradable = sorted({r["symbol"] for r in promotions if r["to_tier"] == "paper_tradable" and '"existing_static":true' not in str(r.get("payload") or "")})
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

        exit_watch = "Exit watch: no exit triggers."
        if digest_exit_blocker.get("active"):
            exit_watch = f"Exit priority: {self._exit_blocker_display_reason(digest_exit_blocker)}."
        else:
            watch_rows = self.storage.fetch_all(
                """
                SELECT symbol, review_type, status, reason
                FROM exit_review_events
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) <= datetime(?)
                  AND status IN ('exit_candidate','exit_review_needed')
                ORDER BY datetime(created_at) DESC
                LIMIT 1
                """,
                (window_start_iso, now.isoformat()),
            )
            if watch_rows:
                reason = watch_rows[0].get("reason") or watch_rows[0].get("review_type") or "exit review"
                exit_watch = f"Exit watch: {watch_rows[0]['symbol']} {reason}; no proposal yet."

        proposal_capacity = self._proposal_capacity_digest_line(window_start_iso, now.isoformat(), performance_lab)
        strategy_policy_line = None
        try:
            from .strategy_performance import StrategyPerformanceEngine
            policy = StrategyPerformanceEngine(self.storage, self.config).latest_valid_policy(STRATEGY_VERSION)
            if policy is not None:
                strategy_policy_line = f"{policy.state} (quality {policy.quality_score:.2f})"
                if policy.state == "PROBE":
                    strategy_policy_line += "; entry-only, no adds, manual approval, controlled probe limits"
        except Exception:
            strategy_policy_line = "unavailable or invalid; new entries fail closed"
        adaptive_conviction_line = None
        adaptive_rows = self.storage.fetch_all(
            """SELECT deployment_mode,opportunity_class,recommended_stop_risk_pct,operational_stop_risk_pct,binding_cap
               FROM adaptive_conviction_operational_decisions WHERE created_at>=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (window_start_iso,),
        )
        if adaptive_rows:
            adaptive = adaptive_rows[0]
            adaptive_conviction_line = (
                f"Adaptive Conviction operational paper: {adaptive['deployment_mode']}/{adaptive['opportunity_class']}; "
                f"permitted {float(adaptive['recommended_stop_risk_pct']):.4f}%; "
                f"binding {adaptive['binding_cap']}"
            )
        adaptive_sizing_rows = self.storage.fetch_all(
            """SELECT operational_constrained_notional,final_operational_notional,comparison_direction,binding_adaptive_cap
               FROM adaptive_sizing_operational_decisions WHERE created_at>=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (window_start_iso,),
        )
        if adaptive_sizing_rows:
            sizing_operational = adaptive_sizing_rows[0]
            sizing_line = (
                f"Adaptive Sizing operational paper: baseline ${float(sizing_operational['operational_constrained_notional']):,.2f}; "
                f"actual ${float(sizing_operational['final_operational_notional']):,.2f}; "
                f"{sizing_operational['comparison_direction']}; binding {sizing_operational['binding_adaptive_cap']}"
            )
            adaptive_conviction_line = f"{adaptive_conviction_line}; {sizing_line}" if adaptive_conviction_line else sizing_line
        crypto_research_line = None
        if self.config.get("crypto", {}).get("enabled", False) and not crypto_quiet_hours_active(self.config, now):
            crypto_rows = self.storage.fetch_all(
                """
                SELECT symbol, score
                FROM crypto_observation_state
                ORDER BY symbol
                """
            )
            if crypto_rows:
                crypto_scores = ", ".join(f"{row['symbol']} {float(row['score'] or 0):.0f}" for row in crypto_rows[:2])
                crypto_research_line = f"Crypto research: {crypto_scores}. Research-only. No proposals/orders."

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
                "expired": expired_cnt,
                "active_proposals": active_proposal_cnt,
            },
            "exit_first_blocker": "; ".join(sorted({x.get("_blocker") for x in symbols_list if x.get("_blocker")} - {None})),
            "summary": summary_str,
            "universe_update": {
                "promoted_to_observation": to_observation,
                "global_research_only_updated": global_observation,
                "static_paper_tradable_reconciled": static_reconciled,
                "promoted_to_paper_tradable": to_tradable,
                "promoted_to_research_candidate": to_research,
                "demoted_retired": demoted,
                "actions_created": universe_actions_str
            },
            "provider_status": provider_status_str,
            "performance_lab": performance_lab,
            "exit_watch": exit_watch,
            "proposal_capacity": proposal_capacity,
            "strategy_policy": strategy_policy_line,
            "strategy_allocation": self._format_strategy_allocation_report(compact=True),
            "adaptive_conviction": adaptive_conviction_line,
            "crypto_research": crypto_research_line,
        }

        from .utils import format_digest_message
        message_text = format_digest_message(digest_data, self.config)

        try:
            self.telegram.send_message(message_text)
            status = "sent"
        except Exception as e:
            status = "error"
            self.storage.audit(self.run_id, "digest_send_failed", {"error": type(e).__name__})
            self.storage.record_check(self.run_id, "digest_send", False, str(e), stage="digest")

        symbols_str = ", ".join(f"{x['symbol']}:{x['status']}" for x in top_watched)
        self.storage.execute(
            "INSERT INTO telegram_digests(run_id,window_start,window_end,sent_at,symbols,summary_text,status) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, window_start_iso, now.isoformat(), now.isoformat(), symbols_str, summary_str, status)
        )
        self.storage.audit(self.run_id, "digest_processed", {"status": status, "window_start": window_start_iso, "window_end": now.isoformat()})

    def _digest_tier_snapshot(self, symbols_list: list[dict[str, Any]], window_start_iso: str, window_end_iso: str) -> dict[str, Any]:
        status_by_symbol = {str(item.get("symbol", "")).upper(): item for item in symbols_list}
        current_positions = []
        position_symbols = set()
        try:
            current_positions = list(self.broker.get_positions())
            position_symbols = {str(_value(p, "symbol", "")).upper() for p in current_positions}
        except Exception:
            rows = self.storage.fetch_all("SELECT symbol FROM positions WHERE created_at=(SELECT MAX(created_at) FROM positions)")
            position_symbols = {str(r.get("symbol", "")).upper() for r in rows}
        universe = self.storage.fetch_all(
            """
            SELECT symbol,tier,source,universe_lane,alpaca_compatible,executable,score,data_confidence,last_promoted_at,created_at,updated_at
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
        static_symbols: list[str] = []
        config_observation_symbols: list[str] = []
        for profile in self.config.get("market_profiles", {}).values():
            if profile.get("status", "active") != "active":
                continue
            if profile.get("broker") not in {None, "alpaca"}:
                continue
            if profile.get("execution_enabled") is False:
                continue
            static_symbols.extend(str(s).upper() for s in profile.get("watchlist", []))
            config_observation_symbols.extend(str(s).upper() for s in profile.get("observation_watchlist", []))
        if not static_symbols:
            static_symbols.extend(str(s).upper() for s in self.config.get("watchlist", []))
        static_symbols = list(dict.fromkeys(s for s in static_symbols if s and "." not in s))
        config_observation_symbols = list(dict.fromkeys(s for s in config_observation_symbols if s and "." not in s))
        universe_by_symbol = {str(row["symbol"]).upper(): row for row in universe}
        static_set = set(static_symbols)

        def is_eligible_dynamic_paper_row(row: dict[str, Any]) -> bool:
            return (
                str(row["tier"]) == PAPER_TRADABLE
                and str(row["symbol"]).upper() not in static_set
                and row.get("universe_lane") == "alpaca_compatible_us"
                and int(row.get("alpaca_compatible") or 0) == 1
                and int(row.get("executable") or 0) == 1
            )

        paper_display_symbols = set(static_symbols)
        paper_display_symbols.update(str(row["symbol"]).upper() for row in universe if is_eligible_dynamic_paper_row(row))
        missing_status_symbols = sorted(sym for sym in paper_display_symbols if sym not in status_by_symbol)
        if missing_status_symbols:
            placeholders = ",".join("?" for _ in missing_status_symbols)
            latest_rows = self.storage.fetch_all(
                f"""
                SELECT mm.*
                FROM market_memory mm
                INNER JOIN (
                    SELECT symbol, MAX(created_at) AS created_at
                    FROM market_memory
                    WHERE symbol IN ({placeholders})
                    GROUP BY symbol
                ) latest ON latest.symbol=mm.symbol AND latest.created_at=mm.created_at
                """,
                tuple(missing_status_symbols),
            )
            cluster_holdings = self._cluster_holdings(current_positions) if current_positions else {}
            for row in latest_rows:
                sym = str(row["symbol"]).upper()
                status_info = self._digest_market_memory_status(sym, row, set(), cluster_holdings)
                status_by_symbol[sym] = {
                    "symbol": sym,
                    "trade_score": row.get("score"),
                    "trade_classification": row.get("classification"),
                    "status": status_info.get("status"),
                    "_event": status_info.get("event"),
                    "_high_score": status_info.get("high_score", False),
                    "_cluster_name": status_info.get("cluster_name"),
                    "_held_symbols": status_info.get("held_symbols", []),
                    "_blocker": status_info.get("blocker"),
                }

        def proposal_status(symbol: str, tier: str, source: str) -> tuple[str, str]:
            status_item = status_by_symbol.get(symbol, {})
            status = str(status_item.get("status") or "")
            if tier != PAPER_TRADABLE:
                if tier == OBSERVATION:
                    return "no", "needs paper-tradable promotion"
                return "no", "needs observation promotion first"
            if "cluster limit" in status.lower():
                cleaned = status.replace("Watch — ", "").replace("Blocked — ", "").replace("Status: ", "")
                if "broad-market cluster limit reached" in cleaned.lower():
                    import re
                    syms = sorted({s for s in re.findall(r'\b[A-Z]{3,4}\b', cleaned) if s != symbol})
                    if syms:
                        return "blocked", f"broad-market cluster limit due {'/'.join(syms)}"
                    return "blocked", "broad-market cluster limit"
                return "blocked", cleaned
            if "cluster exposure limit" in status.lower():
                return "blocked", "cluster exposure limit"
            if status:
                cleaned = status.replace("Watch — ", "").replace("Watch only — ", "").replace("Blocked — ", "").replace("Status: ", "")
                if cleaned.lower() == "no proposal — score below threshold":
                    return "blocked", "score below threshold"
                if cleaned.lower() == "no entry signal" or cleaned.lower() == "no entry/exit signal":
                    return "blocked", "no ENTRY signal"
                if cleaned.lower() == "already held; no valid add setup":
                    return "blocked", "already held; no valid add setup"
                if cleaned.lower() == "waiting for fresh data":
                    return "blocked", "stale market data"
                if cleaned.lower() == "waiting for fresh market validation":
                    return "blocked", "failed freshness validation"
                if cleaned.lower() == "failed risk sizing":
                    return "blocked", "failed risk sizing"
                if cleaned.lower() == "portfolio exposure limit":
                    return "blocked", "portfolio exposure limit"
                if cleaned.lower() == "provider data unavailable":
                    return "blocked", "provider data unavailable"
                if cleaned.lower() == "dynamic symbol missing alpaca-approved scanner profile":
                    return "blocked", "Alpaca trading-data/profile block: missing approved scanner profile"
                if cleaned.lower() == "proposal builder returned no candidate":
                    return "blocked", "proposal builder returned no candidate"
                return "blocked", cleaned
            if source == "existing_static_watchlist":
                return "blocked", "no ENTRY signal"
            return "blocked", "requires setup, RiskEngine, Telegram approval, and final validation"

        def paper_item(symbol: str, source_label: str, source: str, universe_row: dict[str, Any] | None = None) -> dict[str, Any]:
            status_item = status_by_symbol.get(symbol, {})
            universe_score = universe_row.get("score") if universe_row else None
            trade_score = status_item.get("trade_score")
            if trade_score is not None:
                score_val = trade_score
                score_label = "Trade score"
            elif universe_score is not None:
                score_val = universe_score
                score_label = "Fallback score"
            else:
                score_val = None
                score_label = "Trade score"
            allowed, block = proposal_status(symbol, PAPER_TRADABLE, source)
            review = latest_reviews.get(symbol, {})
            return {
                "symbol": symbol,
                "tier": PAPER_TRADABLE,
                "source": source,
                "source_label": source_label,
                "score": universe_score,
                "score_val": score_val,
                "score_label": score_label,
                "data_confidence": universe_row.get("data_confidence") if universe_row else None,
                "tradable": True,
                "alpaca_compatible": True,
                "held": symbol in position_symbols,
                "proposal_allowed": allowed,
                "proposal_block_reason": block,
                "status": status_item.get("status"),
                "stage_reason": review.get("reason") or ("static core paper-tradable" if source_label == "static" else "dynamic paper-tradable"),
                "next_check": review.get("next_promotion_review_at") or "next scanner refresh",
                "decision": review.get("decision"),
            }

        rows_by_tier = {"paper_tradable": [], "static_paper_tradable": [], "dynamic_paper_tradable": [], "observation": [], "research_candidate": []}
        for symbol in static_symbols:
            item = paper_item(symbol, "static", "static_core", universe_by_symbol.get(symbol))
            rows_by_tier["paper_tradable"].append(item)
            rows_by_tier["static_paper_tradable"].append(item)

        universe_observation_symbols = {
            str(row["symbol"]).upper()
            for row in universe
            if str(row["tier"]) in {PAPER_TRADABLE, OBSERVATION, RESEARCH_CANDIDATE}
        }
        for symbol in config_observation_symbols:
            if symbol in static_set or symbol in universe_observation_symbols:
                continue
            status_item = status_by_symbol.get(symbol, {})
            score_val = status_item.get("trade_score")
            rows_by_tier["observation"].append(
                {
                    "symbol": symbol,
                    "tier": OBSERVATION,
                    "source": "static_observation_watchlist",
                    "source_label": None,
                    "score": score_val,
                    "score_val": score_val,
                    "score_label": "Trade score" if score_val is not None else "Score",
                    "data_confidence": None,
                    "tradable": False,
                    "alpaca_compatible": True,
                    "held": symbol in position_symbols,
                    "proposal_allowed": "no",
                    "proposal_block_reason": "needs paper-tradable promotion",
                    "status": status_item.get("status"),
                    "stage_reason": "configured observation watchlist",
                    "next_check": "next scanner refresh",
                    "decision": None,
                }
            )

        for row in universe:
            symbol = str(row["symbol"]).upper()
            tier = str(row["tier"])
            source = str(row.get("source") or "")
            if tier == PAPER_TRADABLE and symbol in static_set:
                continue
            if (
                tier == PAPER_TRADABLE
                and (
                    row.get("universe_lane") != "alpaca_compatible_us"
                    or int(row.get("alpaca_compatible") or 0) != 1
                    or int(row.get("executable") or 0) != 1
                )
            ):
                continue
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
                "source_label": "dynamic" if tier == PAPER_TRADABLE else None,
                "score": row.get("score"),
                "score_val": score_val,
                "score_label": score_label,
                "data_confidence": row.get("data_confidence"),
                "tradable": tier == PAPER_TRADABLE,
                "alpaca_compatible": bool(row.get("alpaca_compatible", 1)),
                "held": symbol in position_symbols,
                "proposal_allowed": allowed,
                "proposal_block_reason": block,
                "status": status_item.get("status"),
                "stage_reason": review.get("reason") or ("static core paper-tradable" if source == "existing_static_watchlist" else "needs next stage promotion"),
                "next_check": review.get("next_promotion_review_at") or "next scanner refresh",
                "decision": review.get("decision"),
            }
            if tier == PAPER_TRADABLE and source == "existing_static_watchlist":
                item["source_label"] = "static"
                rows_by_tier["paper_tradable"].append(item)
                rows_by_tier["static_paper_tradable"].append(item)
            elif tier == PAPER_TRADABLE:
                rows_by_tier["paper_tradable"].append(item)
                rows_by_tier["dynamic_paper_tradable"].append(item)
            elif tier == OBSERVATION:
                rows_by_tier["observation"].append(item)
            elif tier == RESEARCH_CANDIDATE:
                rows_by_tier["research_candidate"].append(item)

        for key in rows_by_tier:
            rows_by_tier[key].sort(key=lambda x: (-(x["score_val"] if x["score_val"] is not None else -1.0), x["symbol"]))

        return rows_by_tier

    def run_cycle(self, run_dynamic_universe: bool = True) -> None:
        # One immutable policy view is used by all scanner candidates in this
        # cycle. Final approval performs its own conservative current-policy
        # revalidation immediately before execution.
        self._strategy_policy_map = None
        self._phase4_allocation_cache = None
        self.storage.audit(self.run_id, "scan_cycle_started", {"run_dynamic_universe": run_dynamic_universe})
        BrokerReconciler(self.broker, self.storage, self.run_id, self.telegram).reconcile()
        self._refresh_strategy_performance()
        if (self.config.get("rotation", {}) or {}).get("enabled"):
            self._advance_rotation_workflows()
        if self.config.get("phase3", {}).get("active"):
            from .phase3_risk import Phase3Controller
            controller = Phase3Controller(self.storage, self.config, self.run_id)
            states = controller.refresh_strategy_states()
            healthy, report = controller.reconciliation_health()
            self.storage.audit(self.run_id, "phase3_active_risk_cycle", {
                "profile": "adaptive_operational_paper_risk_v2", "reconciliation_healthy": healthy,
                "strategy_states": states, "integrity": report, "manual_approval_required": True,
            })
        if self.config.get("phase4", {}).get("active"):
            from .phase4_allocator import AdaptiveAllocator
            from .phase3_risk import Phase3Controller
            runtime_state = self._authoritative_runtime_state(force=True)
            equity = float(_value(runtime_state.get("account"), "equity", 0) or 0)
            drawdown = Phase3Controller(self.storage, self.config, self.run_id).update_equity(equity)
            canonical_phase4 = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(runtime_state.get("positions", []), runtime_state.get("account"))
            reservations_phase4 = DurableExecutionStore(self.storage).active_reservations()
            pending_phase4 = self._pending_execution_totals()
            strategy_consumption = self._phase4_strategy_consumption(
                list(runtime_state.get("positions") or []), equity
            )
            phase3_profile = Phase3Controller(self.storage, self.config, self.run_id).profile
            allocation_as_of = iso_now()
            phase4_snapshot = {
                "portfolio_equity": equity,
                "as_of": allocation_as_of,
                "equity_as_of": allocation_as_of,
                "heat_before_pct": ((float(canonical_phase4.projected_total_open_risk or 0.0) + float(pending_phase4.get("total_stop_risk") or 0.0)) / equity * 100.0) if equity > 0 else None,
                "gross_exposure_before_pct": ((float(canonical_phase4.projected_gross_exposure or 0.0) + float(pending_phase4.get("total_notional") or 0.0)) / equity * 100.0) if equity > 0 else None,
                "symbol_exposure_before": canonical_phase4.symbol_exposure,
                "cluster_exposure_before": canonical_phase4.cluster_exposure,
                "pending_risk": float(pending_phase4.get("total_stop_risk") or 0.0),
                "reserved_risk": float(reservations_phase4.get("active_reserved_stop_risk") or 0.0),
                "strategy_risk_by_strategy": strategy_consumption["risk_dollars"] if strategy_consumption["complete"] else {},
                "strategy_risk_unit": "stop_risk_dollars",
                "strategy_notional_by_strategy": strategy_consumption["notional_dollars"] if strategy_consumption["complete"] else {},
                "active_reservation_ids_by_strategy": strategy_consumption["active_reservation_ids_by_strategy"] if strategy_consumption["complete"] else {},
                "pending_proposal_claims_by_strategy": strategy_consumption["pending_proposal_claims_by_strategy"] if strategy_consumption["complete"] else {},
                "strategy_attribution_complete": strategy_consumption["complete"],
                "strategy_attribution_reason": strategy_consumption["reason"],
                "phase3_gross_exposure_capacity_pct": phase3_profile.hard_gross_exposure_pct,
                "strategy_registry_snapshot_id": self._ensure_strategy_registry_snapshot(),
            }
            self._phase4_allocation_cache = AdaptiveAllocator(self.storage, self.config, self.run_id).run(
                regime="runtime_mixed_uncertain", drawdown_pct=drawdown, portfolio_snapshot=phase4_snapshot,
                strategy_policy_map=self._strategy_policy_map or {},
                as_of=allocation_as_of,
            )
            self.storage.audit(self.run_id, "phase4_active_adaptive_allocation", {
                "allocation_id": self._phase4_allocation_cache["allocation_id"],
                "decision": self._phase4_allocation_cache["decision"],
                "cash_weight": self._phase4_allocation_cache["cash_weight"],
                "exploration_heat_pct": self._phase4_allocation_cache.get("exploration_heat_pct", 0.0),
                "exploration_weights": self._phase4_allocation_cache.get("exploration_weights", {}),
                "probe_heat_pct": self._phase4_allocation_cache.get("probe_heat_pct", 0.0),
                "probe_weights": self._phase4_allocation_cache.get("probe_weights", {}),
                "allocation_class": self._phase4_allocation_cache.get("allocation_class", "unallocated"),
                "operational_kelly_used": self._phase4_allocation_cache.get("operational_kelly_used", False),
                "binding_caps": self._phase4_allocation_cache.get("binding_caps", {}),
                "evidence_versions": self._phase4_allocation_cache.get("evidence_versions", {}),
                "strategy_states": {key: value.state for key, value in self._phase4_allocation_cache.get("estimates", {}).items()},
                "manual_approval_required": True, "phase3_limits_authoritative": True,
            })
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
        self.storage.audit(self.run_id, "scan_cycle_completed", {"run_dynamic_universe": run_dynamic_universe})
        self._update_forward_outcomes()

    def _refresh_strategy_performance(self) -> None:
        """Refresh and freeze the cycle's persisted strategy-policy map."""
        if not (self.config.get("profitability_engine", {}) or {}).get("enabled", True):
            self._strategy_policy_map = {}
            return
        started_at = iso_now()
        try:
            from .strategy_performance import StrategyPerformanceEngine

            engine = StrategyPerformanceEngine(self.storage, self.config, as_of=started_at)
            snapshots = engine.refresh_all()
            self._strategy_policy_map = engine.valid_policy_map()
            from .phase3_risk import AVAILABLE_STRATEGY_IMPLEMENTATIONS
            from .strategy_execution_registry import StrategyExecutionRegistry, persist as persist_strategy_registry
            registry_evaluation = StrategyExecutionRegistry(
                self.config,
                available_implementations=AVAILABLE_STRATEGY_IMPLEMENTATIONS,
            ).evaluate(self._strategy_policy_map, as_of=iso_now())
            registry_persistence = persist_strategy_registry(
                self.storage, self.run_id, registry_evaluation
            )
            self._strategy_registry_snapshot_id = str(registry_persistence["snapshot_id"])
            detail = {
                "strategy_count": len(snapshots),
                "states": {key: snapshot.recommendation_state for key, snapshot in snapshots.items()},
                "valid_policy_count": len(self._strategy_policy_map),
                "enforcement_enabled": bool((self.config.get("profitability_engine", {}) or {}).get("enforcement_enabled")),
                "report_only": False,
                "strategy_registry_snapshot_id": self._strategy_registry_snapshot_id,
                "authorized_strategies": list(registry_evaluation.authorized_versions),
                "rejected_strategies": {
                    decision.strategy_version: list(decision.reasons)
                    for decision in registry_evaluation.rejected
                },
            }
            record_heartbeat(self.storage, "strategy_performance", "healthy", attempted_at=started_at, completed_at=iso_now(), successful_at=iso_now(), detail=detail)
            self.storage.audit(self.run_id, "strategy_performance_refresh_complete", detail)
        except Exception as exc:
            self._strategy_policy_map = {}
            self._strategy_registry_snapshot_id = None
            detail = {"error_type": type(exc).__name__, "report_only": False, "enforcement_enabled": True}
            record_heartbeat(self.storage, "strategy_performance", "failed", attempted_at=started_at, completed_at=iso_now(), blocked_reason="scorecard refresh failed", detail=detail)
            self.storage.audit(self.run_id, "strategy_performance_refresh_failed", detail)

    def _cycle_strategy_policy(self, strategy_version: str) -> Any:
        """Return the frozen policy used by this cycle, or a read-only fallback.

        The fallback supports direct unit-level sizing calls that do not run a
        scanner cycle. A release config with enforcement enabled still fails
        closed when no valid persisted policy exists.
        """
        if "profitability_engine" not in self.config:
            return None
        if self._strategy_policy_map is None:
            from .strategy_performance import StrategyPerformanceEngine
            self._strategy_policy_map = StrategyPerformanceEngine(self.storage, self.config).valid_policy_map()
        return self._strategy_policy_map.get(strategy_version)

    def _ensure_strategy_registry_snapshot(self) -> str | None:
        if self._strategy_registry_snapshot_id:
            return self._strategy_registry_snapshot_id
        rows = self.storage.fetch_all(
            "SELECT id FROM strategy_registry_snapshots WHERE run_id=? ORDER BY evaluated_at DESC,id DESC LIMIT 1",
            (self.run_id,),
        )
        if rows:
            self._strategy_registry_snapshot_id = str(rows[0]["id"])
            return self._strategy_registry_snapshot_id
        try:
            from .phase3_risk import AVAILABLE_STRATEGY_IMPLEMENTATIONS
            from .strategy_execution_registry import StrategyExecutionRegistry, persist as persist_strategy_registry

            policies = self._strategy_policy_map
            if policies is None:
                from .strategy_performance import StrategyPerformanceEngine
                policies = StrategyPerformanceEngine(self.storage, self.config).valid_policy_map()
                self._strategy_policy_map = policies
            evaluation = StrategyExecutionRegistry(
                self.config,
                available_implementations=AVAILABLE_STRATEGY_IMPLEMENTATIONS,
            ).evaluate(policies or {}, as_of=iso_now())
            persisted = persist_strategy_registry(self.storage, self.run_id, evaluation)
            self._strategy_registry_snapshot_id = str(persisted["snapshot_id"])
            return self._strategy_registry_snapshot_id
        except Exception:
            return None

    def notify_premarket_dynamic_universe_status(self, results: list[dict[str, Any]], trading_skipped_reason: str, now: datetime | None = None) -> str:
        if not results or not self.config.get("telegram", {}).get("dynamic_universe_premarket_updates_enabled", True):
            return "not_evaluated"
        phase = self._dynamic_universe_market_phase(results, trading_skipped_reason, now=now)
        completed = [r for r in results if r.get("status") == "completed"]
        skipped = [r for r in results if r.get("status") == "skipped"]
        snapshot: dict[str, Any] | None = None
        if completed:
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
            counts = self._dynamic_universe_compact_counts()
            brief_count = sum(int(r.get("candidate_briefs") or 0) for r in completed)
            provider_material = self._dynamic_universe_provider_material_status(phase)
            top = ", ".join(
                f"{row['symbol']} {float(row['research_score'] or 0):.0f} {str(row.get('main_positive_reasons') or 'score').split(',')[0]}"
                for row in briefs
            )
            provider_line = self._dynamic_universe_provider_line(phase, material_status=provider_material)
            next_line = self._dynamic_universe_next_line(phase)
            lines = [
                self._dynamic_universe_compact_header(phase, completed=True),
                f"Research candidates: {counts['research_candidate']} | Briefs: {brief_count} | Observation total: {counts['observation_total']} | Dynamic paper-tradable: {counts['dynamic_paper_tradable']} | Static paper-tradable: {counts['static_paper_tradable_total']}",
            ]
            if counts["global_research_only_observation"]:
                lines.append(f"Global research-only observation: {counts['global_research_only_observation']}.")
            if counts["held_positions"]:
                lines.append(
                    f"Held positions: {counts['held_positions']} total "
                    f"({counts['held_static_positions']} static, {counts['held_dynamic_positions']} dynamic)."
                )
            if top:
                lines.append(f"Top: {top}.")
            lines.extend([provider_line, next_line, "No trade proposals/orders created."])
            text = "\n".join(lines)
            symbol_sets = self._dynamic_universe_compact_symbol_sets()
            snapshot = self._dynamic_universe_notification_snapshot(
                phase,
                counts,
                symbol_sets,
                provider_material,
                next_line,
                completed=completed,
                skipped=skipped,
                trading_skipped_reason=trading_skipped_reason,
            )
        elif skipped:
            reason = skipped[0].get("reason") or "research skipped"
            text = f"{self._dynamic_universe_compact_header(phase, completed=False, reason=reason)}\nNo trade proposals/orders created."
            provider_material = self._dynamic_universe_provider_material_status(phase)
            symbol_sets = self._dynamic_universe_compact_symbol_sets()
            snapshot = self._dynamic_universe_notification_snapshot(
                phase,
                self._dynamic_universe_compact_counts(),
                symbol_sets,
                provider_material,
                self._dynamic_universe_next_line(phase),
                completed=completed,
                skipped=skipped,
                trading_skipped_reason=trading_skipped_reason,
            )
        else:
            text = f"Dynamic Universe {self._dynamic_universe_phase_label(phase)} checked. Trading remains blocked: {trading_skipped_reason}.\nNo trade proposals/orders created."
            provider_material = self._dynamic_universe_provider_material_status(phase)
            symbol_sets = self._dynamic_universe_compact_symbol_sets()
            snapshot = self._dynamic_universe_notification_snapshot(
                phase,
                self._dynamic_universe_compact_counts(),
                symbol_sets,
                provider_material,
                self._dynamic_universe_next_line(phase),
                completed=completed,
                skipped=skipped,
                trading_skipped_reason=trading_skipped_reason,
            )
        if self._should_suppress_market_closed_status(phase, snapshot):
            return "suppressed"
        try:
            self.telegram.send_message(text)
            detail = {"status": "sent", "trading_skipped_reason": trading_skipped_reason, "phase": phase}
            if snapshot is not None:
                detail["snapshot"] = snapshot
            self.storage.audit(self.run_id, "dynamic_universe_premarket_update_sent", detail)
            self._record_market_closed_status_snapshot(phase, snapshot)
            return "sent"
        except Exception as exc:
            self.storage.audit(self.run_id, "dynamic_universe_premarket_update_failed", {"error": type(exc).__name__, "trading_skipped_reason": trading_skipped_reason})
            return "failed"

    def _dynamic_universe_compact_counts(self) -> dict[str, int]:
        sets = self._dynamic_universe_compact_symbol_sets()
        return {
            "research_candidate": len(sets["research_candidate_symbols"]),
            "observation_total": len(sets["observation_symbols"]),
            "alpaca_compatible_observation": len(sets["alpaca_compatible_observation_symbols"]),
            "global_research_only_observation": len(sets["global_research_only_symbols"]),
            "dynamic_paper_tradable": len(sets["dynamic_paper_tradable_symbols"]),
            "static_paper_tradable_total": len(sets["static_paper_tradable_symbols"]),
            "held_positions": len(sets["held_symbols"]),
            "held_static_positions": len(sets["held_static_symbols"]),
            "held_dynamic_positions": len(sets["held_dynamic_symbols"]),
        }

    def _dynamic_universe_compact_symbol_sets(self) -> dict[str, list[str]]:
        rows = self.storage.fetch_all(
            """
            SELECT symbol, tier, source, universe_lane, executable, alpaca_compatible
            FROM universe_symbols
            WHERE tier IN ('research_candidate','observation','paper_tradable')
            ORDER BY symbol
            """
        )
        symbols: dict[str, set[str]] = {
            "research_candidate_symbols": set(),
            "observation_symbols": set(),
            "alpaca_compatible_observation_symbols": set(),
            "global_research_only_symbols": set(),
            "dynamic_paper_tradable_symbols": set(),
            "static_paper_tradable_symbols": set(self._configured_static_paper_symbols()),
            "held_symbols": set(),
            "held_static_symbols": set(),
            "held_dynamic_symbols": set(),
        }
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            tier = row.get("tier")
            source = str(row.get("source") or "")
            lane = str(row.get("universe_lane") or "")
            if tier == RESEARCH_CANDIDATE:
                symbols["research_candidate_symbols"].add(symbol)
            elif tier == OBSERVATION:
                symbols["observation_symbols"].add(symbol)
                if lane == "global_research_only":
                    symbols["global_research_only_symbols"].add(symbol)
                elif lane == "alpaca_compatible_us":
                    symbols["alpaca_compatible_observation_symbols"].add(symbol)
            elif tier == PAPER_TRADABLE and source == "existing_static_watchlist":
                continue
            elif (
                tier == PAPER_TRADABLE
                and lane == "alpaca_compatible_us"
                and int(row.get("alpaca_compatible") or 0) == 1
                and int(row.get("executable") or 0) == 1
            ):
                symbols["dynamic_paper_tradable_symbols"].add(symbol)
        for symbol in self._configured_observation_symbols():
            row = self.storage.fetch_all("SELECT 1 FROM universe_symbols WHERE symbol=? AND tier IN ('paper_tradable','observation','research_candidate') LIMIT 1", (symbol,))
            if not row:
                symbols["observation_symbols"].add(symbol)
                symbols["alpaca_compatible_observation_symbols"].add(symbol)
        static = symbols["static_paper_tradable_symbols"]
        dynamic = symbols["dynamic_paper_tradable_symbols"]
        try:
            positions = self.broker.get_positions()
            held = {str(_value(p, "symbol", "")).upper() for p in positions if str(_value(p, "symbol", "")).upper()}
            symbols["held_symbols"] = held
            symbols["held_static_symbols"] = held & static
            symbols["held_dynamic_symbols"] = held & dynamic
        except Exception:
            try:
                rows = self.storage.fetch_all("SELECT DISTINCT symbol FROM positions")
                held = {str(r.get("symbol") or "").upper() for r in rows if str(r.get("symbol") or "").upper()}
                symbols["held_symbols"] = held
                symbols["held_static_symbols"] = held & static
                symbols["held_dynamic_symbols"] = held & dynamic
            except Exception:
                symbols["held_symbols"] = set()
                symbols["held_static_symbols"] = set()
                symbols["held_dynamic_symbols"] = set()
        return {key: sorted(value) for key, value in symbols.items()}

    def _configured_static_paper_symbols(self) -> list[str]:
        symbols: list[str] = []
        for profile in self.config.get("market_profiles", {}).values():
            if profile.get("status", "active") != "active":
                continue
            if profile.get("broker") not in {None, "alpaca"}:
                continue
            if profile.get("execution_enabled") is False:
                continue
            symbols.extend(str(s).upper() for s in profile.get("watchlist", []))
        if not symbols:
            symbols.extend(str(s).upper() for s in self.config.get("watchlist", []))
        return list(dict.fromkeys(s for s in symbols if s and "." not in s))

    def _configured_observation_symbols(self) -> list[str]:
        symbols: list[str] = []
        static = set(self._configured_static_paper_symbols())
        for profile in self.config.get("market_profiles", {}).values():
            if profile.get("status", "active") != "active":
                continue
            if profile.get("broker") not in {None, "alpaca"}:
                continue
            if profile.get("execution_enabled") is False:
                continue
            symbols.extend(str(s).upper() for s in profile.get("observation_watchlist", []))
        return list(dict.fromkeys(s for s in symbols if s and "." not in s and s not in static))

    def _held_static_position_count(self) -> int:
        static = set(self._configured_static_paper_symbols())
        if not static:
            return 0
        try:
            positions = self.broker.get_positions()
            return len({str(_value(p, "symbol", "")).upper() for p in positions if str(_value(p, "symbol", "")).upper() in static})
        except Exception:
            try:
                rows = self.storage.fetch_all("SELECT DISTINCT symbol FROM positions")
                return len({str(r.get("symbol") or "").upper() for r in rows if str(r.get("symbol") or "").upper() in static})
            except Exception:
                return 0

    def _dynamic_universe_market_phase(self, results: list[dict[str, Any]], trading_skipped_reason: str, now: datetime | None = None) -> str:
        catchup_completed = any(bool(r.get("catchup")) or str(r.get("run_type") or "").endswith("_catchup") for r in results)
        try:
            if self.broker and self.broker.is_market_open():
                return MARKET_PHASE_REGULAR_CATCH_UP if catchup_completed else MARKET_PHASE_REGULAR
        except Exception:
            pass
        if catchup_completed:
            return MARKET_PHASE_CATCH_UP
        now_utc = now or datetime.now(UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)
        profile = self.config.get("market_profiles", {}).get("us_equities", {})
        tz = ZoneInfo(profile.get("timezone", "America/New_York"))
        local_now = now_utc.astimezone(tz)
        if local_now.weekday() >= 5:
            return MARKET_PHASE_WEEKEND
        start_str, end_str = str(profile.get("session_hours", "09:30-16:00")).split("-", 1)
        start = dt_time.fromisoformat(start_str)
        end = dt_time.fromisoformat(end_str)
        local_time = local_now.time()
        if local_time > end:
            return MARKET_PHASE_POST
        next_open_local = self._next_market_open_local(tz)
        if next_open_local is not None:
            if next_open_local.date() != local_now.date():
                return MARKET_PHASE_HOLIDAY if trading_skipped_reason == "market_closed" else MARKET_PHASE_NON_TRADING
            if local_time < start:
                premarket_start = self._configured_premarket_start_time()
                return MARKET_PHASE_PRE if local_time >= premarket_start else MARKET_PHASE_UNKNOWN_CLOSED
        if local_time < start:
            premarket_start = self._configured_premarket_start_time()
            return MARKET_PHASE_PRE if local_time >= premarket_start else MARKET_PHASE_UNKNOWN_CLOSED
        if start <= local_time <= end:
            return MARKET_PHASE_HOLIDAY if trading_skipped_reason == "market_closed" else MARKET_PHASE_UNKNOWN_CLOSED
        return MARKET_PHASE_UNKNOWN_CLOSED

    def _configured_premarket_start_time(self) -> dt_time:
        configured = (
            self.config.get("dynamic_universe", {})
            .get("schedules", {})
            .get("pre_market_window_start_local", "04:00")
        )
        try:
            return dt_time.fromisoformat(str(configured))
        except ValueError:
            return dt_time(hour=4)

    def _next_market_open_local(self, tz: ZoneInfo) -> datetime | None:
        if not self.broker or not hasattr(self.broker, "get_clock"):
            return None
        try:
            clock = self.broker.get_clock()
            next_open = getattr(clock, "next_open", None)
            if next_open is None:
                return None
            if isinstance(next_open, str):
                parsed = datetime.fromisoformat(next_open.replace("Z", "+00:00"))
            else:
                parsed = next_open
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(tz)
        except Exception:
            return None

    def _dynamic_universe_phase_label(self, phase: str) -> str:
        return {
            MARKET_PHASE_PRE: "pre-market universe scan",
            MARKET_PHASE_REGULAR: "intraday refresh",
            MARKET_PHASE_REGULAR_CATCH_UP: "intraday catch-up refresh",
            MARKET_PHASE_POST: "post-market research",
            MARKET_PHASE_WEEKEND: "market-closed research",
            MARKET_PHASE_HOLIDAY: "market-closed research",
            MARKET_PHASE_NON_TRADING: "market-closed research",
            MARKET_PHASE_CATCH_UP: "research catch-up",
            MARKET_PHASE_UNKNOWN_CLOSED: "market-closed research",
        }.get(phase, "market-closed research")

    def _dynamic_universe_compact_header(self, phase: str, completed: bool, reason: str | None = None) -> str:
        label = self._dynamic_universe_phase_label(phase)
        verb = "completed" if completed else f"skipped: {reason or 'research skipped'}"
        if phase == MARKET_PHASE_PRE:
            return f"Dynamic Universe {label} {verb}. Trading remains blocked until market open."
        if phase == MARKET_PHASE_REGULAR:
            return f"Dynamic Universe {label} {verb}. Trading remains paper-only and guarded by normal proposal rules."
        if phase == MARKET_PHASE_REGULAR_CATCH_UP:
            return f"Dynamic Universe {label} {verb}. Market is open; trading remains paper-only and guarded by normal proposal rules."
        if phase == MARKET_PHASE_POST:
            return f"Dynamic Universe {label} {verb}. Trading is blocked until the next market open."
        if phase == MARKET_PHASE_WEEKEND:
            return f"Dynamic Universe weekend market-closed research {verb}. Trading is blocked until the next regular US market open."
        if phase in {MARKET_PHASE_HOLIDAY, MARKET_PHASE_NON_TRADING}:
            return f"Dynamic Universe {label} {verb}. No regular US session today. Trading remains blocked until the next market open."
        if phase == MARKET_PHASE_UNKNOWN_CLOSED:
            return f"Dynamic Universe {label} {verb}. Trading remains blocked until the next market open."
        if phase == MARKET_PHASE_CATCH_UP:
            return f"Dynamic Universe {label} {verb}. Trading remains blocked unless market is open and all trading gates pass."
        return f"Dynamic Universe {label} {verb}. Trading remains blocked unless all trading gates pass."

    def _dynamic_universe_next_line(self, phase: str) -> str:
        if phase == MARKET_PHASE_PRE:
            return "Next: market-open refresh/promotion checks."
        if phase in {MARKET_PHASE_REGULAR, MARKET_PHASE_REGULAR_CATCH_UP}:
            return "Next: next intraday refresh or post-market review."
        if phase == MARKET_PHASE_POST:
            return "Next: next scheduled research/promotion review."
        if phase == MARKET_PHASE_CATCH_UP:
            return "Next: resume the configured Dynamic Universe schedule."
        if phase == MARKET_PHASE_WEEKEND:
            return "Next: next regular US market session or scheduled research review."
        if phase in {MARKET_PHASE_HOLIDAY, MARKET_PHASE_NON_TRADING}:
            return "Next: next scheduled research review after the market calendar reopens."
        return "Next: next scheduled market-open or research review."

    def _dynamic_universe_provider_material_status(self, phase: str) -> dict[str, Any]:
        rows = self.storage.fetch_all(
            "SELECT endpoint_name, available, plan_limited, disabled_until, last_status_code, last_error_category, detail FROM data_provider_capabilities ORDER BY endpoint_name"
        )
        active_cooldowns: list[str] = []
        stale_cooldowns: list[str] = []
        plan_limited: list[str] = []
        available: list[str] = []
        errors: list[str] = []
        symbol_no_data: dict[str, int] = {}
        now = datetime.now(UTC)
        for row in rows:
            endpoint = str(row.get("endpoint_name") or "")
            category = str(row.get("last_error_category") or "")
            status_code = int(row.get("last_status_code") or 0)
            symbol_level_no_data = endpoint == "eod_bars" and (category in {"not_found", "no_data", "symbol_not_found", "symbol_no_data"} or status_code in {404, 422})
            if symbol_level_no_data:
                symbol_no_data[endpoint] = symbol_no_data.get(endpoint, 0) + 1
            if int(row.get("available") or 0) == 1:
                available.append(endpoint)
            if int(row.get("plan_limited") or 0) == 1 and not symbol_level_no_data:
                plan_limited.append(endpoint)
            disabled_until = row.get("disabled_until")
            if disabled_until and not symbol_level_no_data:
                try:
                    disabled_dt = datetime.fromisoformat(str(disabled_until).replace("Z", "+00:00")).astimezone(UTC)
                    if disabled_dt > now:
                        active_cooldowns.append(endpoint)
                    else:
                        stale_cooldowns.append(endpoint)
                except Exception:
                    stale_cooldowns.append(endpoint)
            if category and category not in {"cooldown_active", "rate_limited", "forbidden", "plan_limited"} and not symbol_level_no_data:
                errors.append(f"{endpoint}:{category}")
        market_closed = phase in {MARKET_PHASE_POST, MARKET_PHASE_WEEKEND, MARKET_PHASE_HOLIDAY, MARKET_PHASE_NON_TRADING, MARKET_PHASE_UNKNOWN_CLOSED}
        core_endpoints = {"eod_bars", "screener", "technicals"} if market_closed else {"eod_bars", "intraday_bars", "realtime_quote", "screener", "technicals"}
        ignored_closed_endpoints = {"intraday_bars", "realtime_quote"} if market_closed else set()
        active_core_cooldowns = sorted(ep for ep in active_cooldowns if ep in core_endpoints and ep not in ignored_closed_endpoints)
        optional_cooldowns = sorted(ep for ep in active_cooldowns if ep not in core_endpoints and ep not in ignored_closed_endpoints)
        status = "ok"
        if errors or active_core_cooldowns:
            status = "degraded"
        return {
            "status": status,
            "available": sorted(available),
            "active_core_cooldowns": active_core_cooldowns,
            "optional_cooldowns": optional_cooldowns,
            "plan_limited": sorted(plan_limited),
            "errors": sorted(errors),
            "symbol_no_data": dict(sorted(symbol_no_data.items())),
            "intraday_not_needed": market_closed,
        }

    def _dynamic_universe_provider_line(self, phase: str, material_status: dict[str, Any] | None = None) -> str:
        status = material_status or self._dynamic_universe_provider_material_status(phase)
        available = list(status.get("available") or [])
        active_core_cooldowns = list(status.get("active_core_cooldowns") or [])
        optional_cooldowns = list(status.get("optional_cooldowns") or [])
        plan_limited = list(status.get("plan_limited") or [])
        errors = list(status.get("errors") or [])
        symbol_no_data = dict(status.get("symbol_no_data") or {})
        market_closed = bool(status.get("intraday_not_needed"))
        if market_closed:
            parts = ["Provider: EODHD"]
            if available:
                parts.append("core available")
            if status.get("intraday_not_needed"):
                parts.append("intraday not needed while market closed")
            eod_symbol_no_data = int(symbol_no_data.get("eod_bars") or 0)
            if eod_symbol_no_data:
                parts.append(f"EOD had symbol-level no-data for {eod_symbol_no_data} symbol" + ("" if eod_symbol_no_data == 1 else "s"))
            if active_core_cooldowns:
                parts.append(f"core cooldown: {', '.join(active_core_cooldowns)}")
            if optional_cooldowns:
                parts.append(f"optional cooldown: {', '.join(optional_cooldowns)}")
            if plan_limited:
                parts.append(f"plan-limited: {', '.join(plan_limited)}")
            if errors:
                parts.append(f"errors: {', '.join(errors)}")
            return "; ".join(parts) + "."
        active_cooldowns = active_core_cooldowns + optional_cooldowns
        if active_cooldowns or plan_limited or errors:
            parts = [f"Provider: {len(available)} endpoints available"]
            if active_cooldowns:
                parts.append(f"current cooldown: {', '.join(active_cooldowns)}")
            if plan_limited:
                parts.append(f"plan-limited: {', '.join(plan_limited)}")
            if errors:
                parts.append(f"errors: {', '.join(errors)}")
            return "; ".join(parts) + "."
        return f"Provider: {len(available)} endpoints available, 0 on cooldown."

    def _dynamic_universe_notification_snapshot(
        self,
        phase: str,
        counts: dict[str, int],
        symbol_sets: dict[str, list[str]],
        provider_material: dict[str, Any],
        next_line: str,
        *,
        completed: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        trading_skipped_reason: str,
    ) -> dict[str, Any]:
        return {
            "market_phase": phase,
            "research_candidate_count": counts.get("research_candidate", 0),
            "observation_total_count": counts.get("observation_total", 0),
            "alpaca_compatible_observation_count": counts.get("alpaca_compatible_observation", 0),
            "global_research_only_observation_count": counts.get("global_research_only_observation", 0),
            "dynamic_paper_tradable_count": counts.get("dynamic_paper_tradable", 0),
            "static_paper_tradable_total_count": counts.get("static_paper_tradable_total", 0),
            "held_position_count": counts.get("held_positions", 0),
            "held_static_position_count": counts.get("held_static_positions", 0),
            "held_dynamic_position_count": counts.get("held_dynamic_positions", 0),
            "research_candidate_symbols": symbol_sets.get("research_candidate_symbols") or [],
            "observation_symbols": symbol_sets.get("observation_symbols") or [],
            "alpaca_compatible_observation_symbols": symbol_sets.get("alpaca_compatible_observation_symbols") or [],
            "global_research_only_symbols": symbol_sets.get("global_research_only_symbols") or [],
            "dynamic_paper_tradable_symbols": symbol_sets.get("dynamic_paper_tradable_symbols") or [],
            "static_paper_tradable_symbols": symbol_sets.get("static_paper_tradable_symbols") or [],
            "held_symbols": symbol_sets.get("held_symbols") or [],
            "held_static_symbols": symbol_sets.get("held_static_symbols") or [],
            "held_dynamic_symbols": symbol_sets.get("held_dynamic_symbols") or [],
            "provider_material_status": {
                "status": provider_material.get("status"),
                "active_core_cooldowns": provider_material.get("active_core_cooldowns") or [],
                "optional_cooldowns": provider_material.get("optional_cooldowns") or [],
                "plan_limited": provider_material.get("plan_limited") or [],
                "errors": provider_material.get("errors") or [],
                "symbol_no_data": provider_material.get("symbol_no_data") or {},
                "intraday_not_needed": bool(provider_material.get("intraday_not_needed")),
            },
            "completed_run_types": sorted({str(r.get("run_type") or "") for r in completed if r.get("run_type")}),
            "skipped_reasons": sorted({str(r.get("reason") or "") for r in skipped if r.get("reason")}),
            "catchup_completed": any(bool(r.get("catchup")) or str(r.get("run_type") or "").endswith("_catchup") for r in completed),
            "user_requested": any(bool(r.get("user_requested")) for r in completed + skipped),
            "has_error_or_warning": any(str(r.get("status") or "") in {"error", "failed", "warning"} or bool(r.get("error") or r.get("warning")) for r in completed + skipped),
            "next_expected_check": next_line,
            "trading_skipped_reason": trading_skipped_reason,
            "no_proposals_orders": True,
        }

    def _should_suppress_market_closed_status(self, phase: str, snapshot: dict[str, Any] | None) -> bool:
        cfg = self.config.get("telegram", {}).get("market_closed_status", {})
        if not cfg.get("suppress_no_change", True):
            return False
        if phase not in MARKET_CLOSED_STATUS_PHASES or snapshot is None:
            return False
        if snapshot.get("user_requested"):
            return False
        if snapshot.get("catchup_completed") and cfg.get("always_send_catchup_completion", True):
            return False
        if snapshot.get("has_error_or_warning") and cfg.get("always_send_errors", True):
            return False
        previous = self._latest_market_closed_status_snapshot()
        if not previous:
            return False
        previous_key = self._market_closed_material_snapshot(previous)
        current_key = self._market_closed_material_snapshot(snapshot)
        if previous_key == current_key:
            self.storage.audit(
                self.run_id,
                "market_closed_status_suppressed_no_change",
                {"phase": phase, "snapshot": snapshot, "material_snapshot": current_key, "suppressed_at": iso_now()},
            )
            return True
        if self._market_closed_only_count_noise(previous, snapshot) and cfg.get("ignore_minor_count_noise", True):
            self.storage.audit(
                self.run_id,
                "market_closed_status_suppressed_count_noise",
                {"phase": phase, "previous": previous, "snapshot": snapshot, "suppressed_at": iso_now()},
            )
            return True
        max_minutes = int(cfg.get("max_frequency_minutes", 180) or 0)
        if max_minutes > 0 and not self._market_closed_material_change(previous, snapshot):
            last_sent = self._latest_market_closed_status_sent_at()
            if last_sent is not None and datetime.now(UTC) - last_sent < timedelta(minutes=max_minutes):
                self.storage.audit(
                    self.run_id,
                    "market_closed_status_suppressed_frequency",
                    {"phase": phase, "snapshot": snapshot, "max_frequency_minutes": max_minutes, "suppressed_at": iso_now()},
                )
                return True
        return False

    def _market_closed_material_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        provider = dict(snapshot.get("provider_material_status") or {})
        return {
            "market_phase": snapshot.get("market_phase"),
            "research_candidate_symbols": sorted(snapshot.get("research_candidate_symbols") or []),
            "observation_symbols": sorted(snapshot.get("observation_symbols") or []),
            "alpaca_compatible_observation_symbols": sorted(snapshot.get("alpaca_compatible_observation_symbols") or []),
            "global_research_only_symbols": sorted(snapshot.get("global_research_only_symbols") or []),
            "dynamic_paper_tradable_symbols": sorted(snapshot.get("dynamic_paper_tradable_symbols") or []),
            "static_paper_tradable_symbols": sorted(snapshot.get("static_paper_tradable_symbols") or []),
            "held_symbols": sorted(snapshot.get("held_symbols") or []),
            "held_static_symbols": sorted(snapshot.get("held_static_symbols") or []),
            "held_dynamic_symbols": sorted(snapshot.get("held_dynamic_symbols") or []),
            "provider_material_status": {
                "status": provider.get("status"),
                "active_core_cooldowns": sorted(provider.get("active_core_cooldowns") or []),
                "optional_cooldowns": sorted(provider.get("optional_cooldowns") or []),
                "plan_limited": sorted(provider.get("plan_limited") or []),
                "errors": sorted(provider.get("errors") or []),
                "symbol_no_data": dict(sorted((provider.get("symbol_no_data") or {}).items())),
                "intraday_not_needed": bool(provider.get("intraday_not_needed")),
            },
            "completed_run_types": sorted(snapshot.get("completed_run_types") or []),
            "skipped_reasons": sorted(snapshot.get("skipped_reasons") or []),
            "catchup_completed": bool(snapshot.get("catchup_completed")),
            "has_error_or_warning": bool(snapshot.get("has_error_or_warning")),
            "trading_skipped_reason": snapshot.get("trading_skipped_reason"),
        }

    def _market_closed_material_change(self, previous: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        return self._market_closed_material_snapshot(previous) != self._market_closed_material_snapshot(snapshot)

    def _market_closed_only_count_noise(self, previous: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        noisy_count_fields = {
            "research_candidate_count",
            "observation_total_count",
            "alpaca_compatible_observation_count",
            "global_research_only_observation_count",
            "dynamic_paper_tradable_count",
            "static_paper_tradable_total_count",
            "held_position_count",
            "held_static_position_count",
            "held_dynamic_position_count",
        }
        keys = set(previous) | set(snapshot)
        changed = {key for key in keys if previous.get(key) != snapshot.get(key)}
        return bool(changed) and changed <= noisy_count_fields and self._market_closed_material_snapshot(previous) == self._market_closed_material_snapshot(snapshot)

    def _latest_market_closed_status_sent_at(self) -> datetime | None:
        rows = self.storage.fetch_all(
            """
            SELECT created_at, detail
            FROM audit_events
            WHERE event_type='dynamic_universe_market_closed_status_snapshot'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        if not rows:
            return None
        try:
            detail = json.loads(rows[0]["detail"] or "{}")
            sent_at = detail.get("sent_at") or rows[0].get("created_at")
            return _parse_datetime(str(sent_at))
        except Exception:
            return None

    def _latest_market_closed_status_snapshot(self) -> dict[str, Any] | None:
        rows = self.storage.fetch_all(
            """
            SELECT detail
            FROM audit_events
            WHERE event_type='dynamic_universe_market_closed_status_snapshot'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        if not rows:
            return None
        try:
            detail = json.loads(rows[0]["detail"] or "{}")
            snapshot = detail.get("snapshot")
            return snapshot if isinstance(snapshot, dict) else None
        except Exception:
            return None

    def _record_market_closed_status_snapshot(self, phase: str, snapshot: dict[str, Any] | None) -> None:
        if phase not in MARKET_CLOSED_STATUS_PHASES or snapshot is None:
            return
        self.storage.audit(
            self.run_id,
            "dynamic_universe_market_closed_status_snapshot",
            {"phase": phase, "snapshot": snapshot, "sent_at": iso_now()},
        )

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

        cash = float(_value(account, "cash", equity) or equity)
        if cash <= 0:
            cash = equity
        buying_power = float(_value(account, "buying_power", cash * 4) or (cash * 4))
        if buying_power <= 0:
            buying_power = cash * 4
        return {
            "portfolio_equity": equity,
            "cash": cash,
            "buying_power": buying_power,
            "total_exposure_dollars": total_val,
            "total_exposure_pct": (total_val / equity) * 100,
            "single_exposures": single_exposures,
            "cluster_exposures": cluster_exposures,
            "cluster_counts": cluster_counts,
            "cluster_symbols": cluster_symbols,
        }

    def _probe_commitments(self, strategy_version: str) -> dict[str, Any]:
        """Return persisted probe slots, heat, and gross exposure.

        A partially filled probe with a remaining reservation is one slot, not
        two. Only proposals explicitly persisted as PROBE can consume a slot.
        """
        position_rows = self.storage.fetch_all(
            """SELECT pl.entry_proposal_id,pl.position_lifecycle_id,pl.symbol,pl.original_quantity,
                      pl.remaining_quantity,pl.unit_cost,pl.initial_risk_dollars
               FROM position_lots pl JOIN trade_proposals p ON p.id=pl.entry_proposal_id
               WHERE pl.strategy_version=? AND pl.remaining_quantity>0 AND p.strategy_state='PROBE'""",
            (strategy_version,),
        )
        reservation_rows = self.storage.fetch_all(
            """SELECT i.proposal_id,r.active_notional,r.active_stop_risk
               FROM risk_reservations r JOIN order_intents i ON i.id=r.intent_id
               JOIN trade_proposals p ON p.id=i.proposal_id
               WHERE r.state='active' AND i.strategy_version=? AND p.strategy_state='PROBE'""",
            (strategy_version,),
        )
        slots: set[str] = set()
        gross = 0.0
        heat = 0.0
        for row in position_rows:
            slot = str(row.get("entry_proposal_id") or row.get("position_lifecycle_id") or f"position:{row.get('symbol')}")
            slots.add(slot)
            original = float(row.get("original_quantity") or 0.0)
            remaining = float(row.get("remaining_quantity") or 0.0)
            gross += remaining * float(row.get("unit_cost") or 0.0)
            if original > 0 and row.get("initial_risk_dollars") is not None:
                heat += float(row["initial_risk_dollars"]) * remaining / original
        for row in reservation_rows:
            slots.add(str(row.get("proposal_id") or "reserved-probe"))
            gross += float(row.get("active_notional") or 0.0)
            heat += float(row.get("active_stop_risk") or 0.0)
        return {"active_count": len(slots), "gross_notional": gross, "stop_risk": heat, "slot_ids": sorted(slots)}

    def _pending_execution_totals(self) -> dict[str, Any]:
        """Return unreserved pending BUY exposure, failing closed on unknowns."""
        rows = self.storage.fetch_all(
            """SELECT p.id,p.symbol,p.payload,p.notional,p.strategy_version,p.status
               FROM trade_proposals p
               LEFT JOIN order_intents i ON i.proposal_id=p.id
               WHERE p.side='buy' AND p.status IN ('pending','approved') AND i.id IS NULL"""
        )
        total_notional = 0.0
        total_stop_risk = 0.0
        by_symbol: dict[str, float] = {}
        by_cluster: dict[str, float] = {}
        exploration_risk_by_strategy: dict[str, float] = {}
        exploration_notional = 0.0
        exploration_stop_risk = 0.0
        unknown_rows: list[dict[str, Any]] = []
        for row in rows:
            row_id = str(row.get("id") or "unknown")
            try:
                raw_payload = row.get("payload")
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
                notional = float(payload.get("notional") if payload.get("notional") is not None else row.get("notional"))
                price = float(payload.get("latest_price") or payload.get("reference_price"))
                stop = float(payload.get("stop_distance_dollars"))
                risk_dollars = float(payload.get("stop_risk_dollars") or 0.0)
                if risk_dollars <= 0 and notional > 0 and price > 0 and stop > 0:
                    risk_dollars = notional / price * stop
                symbol = str(row.get("symbol") or payload.get("symbol") or "").upper()
                cluster = str(payload.get("cluster_name") or self._get_symbol_cluster(symbol) or "") or None
                if not symbol or not math.isfinite(notional) or notional <= 0 or not math.isfinite(price) or price <= 0 or not math.isfinite(stop) or stop <= 0 or not math.isfinite(risk_dollars) or risk_dollars <= 0 or not cluster:
                    raise ValueError("pending buy exposure is incomplete")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                unknown_rows.append({"proposal_id": row_id, "reason": str(exc) or "malformed pending buy exposure"})
                continue
            total_notional += notional
            total_stop_risk += risk_dollars
            by_symbol[symbol] = by_symbol.get(symbol, 0.0) + notional
            if cluster:
                by_cluster[cluster] = by_cluster.get(cluster, 0.0) + notional
            if payload.get("phase4_mode") == "exploration":
                exploration_notional += notional
                exploration_stop_risk += risk_dollars
                strategy = str(row.get("strategy_version") or payload.get("strategy_version") or "")
                exploration_risk_by_strategy[strategy] = exploration_risk_by_strategy.get(strategy, 0.0) + risk_dollars
        return {
            "total_notional": total_notional,
            "total_stop_risk": total_stop_risk,
            "by_symbol": by_symbol,
            "by_cluster": by_cluster,
            "exploration_notional": exploration_notional,
            "exploration_stop_risk": exploration_stop_risk,
            "exploration_risk_by_strategy": exploration_risk_by_strategy,
            "unknown": bool(unknown_rows),
            "unknown_rows": unknown_rows,
            "unknown_reason": "; ".join(f"{row['proposal_id']}: {row['reason']}" for row in unknown_rows) if unknown_rows else None,
        }

    def _held_strategy_risk_pct(
        self,
        positions: list[Any],
        portfolio_equity: float,
    ) -> dict[str, float]:
        """Attribute current durable-stop risk to strategy lots.

        Active reservations are intentionally excluded: the allocator sleeve
        reports held consumption, while the atomic reservation transaction adds
        current active reservations exactly once immediately before an intent is
        created.
        """
        if portfolio_equity <= 0:
            return {}
        lots = self.storage.fetch_all(
            """SELECT symbol,strategy_version,remaining_quantity
               FROM position_lots
               WHERE remaining_quantity>0 AND strategy_version IS NOT NULL"""
        )
        quantity_by_symbol_strategy: dict[str, dict[str, float]] = {}
        for lot in lots:
            symbol = str(lot.get("symbol") or "").upper()
            strategy = str(lot.get("strategy_version") or "")
            quantity = max(0.0, float(lot.get("remaining_quantity") or 0.0))
            if symbol and strategy and quantity > 0:
                bucket = quantity_by_symbol_strategy.setdefault(symbol, {})
                bucket[strategy] = bucket.get(strategy, 0.0) + quantity
        attributed: dict[str, float] = {}
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            by_strategy = quantity_by_symbol_strategy.get(symbol) or {}
            attributed_qty = sum(by_strategy.values())
            current_price = float(_value(position, "current_price", 0.0) or 0.0)
            if attributed_qty <= 0 or current_price <= 0:
                continue
            state = self._position_management_state(symbol) or {}
            stops = [
                float(value)
                for value in (
                    state.get("initial_stop_price"),
                    state.get("trailing_stop_price"),
                    state.get("authoritative_protective_stop"),
                )
                if value is not None and float(value) > 0
            ]
            if not stops:
                continue
            risk_per_share = max(0.0, current_price - max(stops))
            for strategy, quantity in by_strategy.items():
                attributed[strategy] = attributed.get(strategy, 0.0) + quantity * risk_per_share
        return {
            strategy: risk_dollars / portfolio_equity * 100.0
            for strategy, risk_dollars in attributed.items()
        }

    def _phase4_strategy_consumption(
        self,
        positions: list[Any],
        portfolio_equity: float,
    ) -> dict[str, Any]:
        """Current-mark held plus active-reservation consumption by strategy."""
        if portfolio_equity <= 0:
            return {"complete": False, "risk_pct": {}, "notional_dollars": {}, "reason": "portfolio equity unavailable"}
        lots = self.storage.fetch_all(
            """SELECT symbol,strategy_version,remaining_quantity FROM position_lots
               WHERE remaining_quantity>0 AND strategy_version IS NOT NULL"""
        )
        position_map = {
            str(_value(position, "symbol", "")).upper(): {
                "quantity": max(0.0, float(_value(position, "qty", 0.0) or 0.0)),
                "price": max(0.0, float(_value(position, "current_price", 0.0) or 0.0)),
            }
            for position in positions
            if str(_value(position, "symbol", "")).strip()
        }
        quantities: dict[str, dict[str, float]] = {}
        for lot in lots:
            symbol = str(lot.get("symbol") or "").upper()
            strategy = str(lot.get("strategy_version") or "")
            quantity = max(0.0, float(lot.get("remaining_quantity") or 0.0))
            if symbol and strategy and quantity > 0:
                quantities.setdefault(symbol, {})[strategy] = quantities.setdefault(symbol, {}).get(strategy, 0.0) + quantity
        complete = True
        reasons: list[str] = []
        risk_dollars: dict[str, float] = {}
        notional_dollars: dict[str, float] = {}
        for symbol, position in sorted(position_map.items()):
            by_strategy = quantities.get(symbol, {})
            attributed_quantity = sum(by_strategy.values())
            if abs(attributed_quantity - position["quantity"]) > max(1e-8, position["quantity"] * 1e-8):
                complete = False
                reasons.append(f"{symbol}: lot quantity does not reconcile to broker position")
                continue
            if position["quantity"] > 0 and position["price"] <= 0:
                complete = False
                reasons.append(f"{symbol}: current broker mark unavailable")
                continue
            state = self._position_management_state(symbol) or {}
            stops = [
                float(value)
                for value in (
                    state.get("initial_stop_price"), state.get("trailing_stop_price"),
                    state.get("authoritative_protective_stop"),
                )
                if value is not None and float(value) > 0
            ]
            if position["quantity"] > 0 and not stops:
                complete = False
                reasons.append(f"{symbol}: authoritative stop unavailable")
                continue
            risk_per_share = max(0.0, position["price"] - max(stops)) if stops else 0.0
            for strategy, quantity in by_strategy.items():
                risk_dollars[strategy] = risk_dollars.get(strategy, 0.0) + quantity * risk_per_share
                notional_dollars[strategy] = notional_dollars.get(strategy, 0.0) + quantity * position["price"]
        orphan_symbols = sorted(set(quantities) - set(position_map))
        if orphan_symbols:
            complete = False
            reasons.append("open lots lack broker positions: " + ",".join(orphan_symbols))

        # One statement captures both the amounts and exact reservation IDs in
        # the same SQLite read snapshot. The persisted ID set is the atomic
        # hand-off to intent creation: reservations absent from this set are
        # incremental even when their wall-clock timestamp precedes allocation
        # persistence, closing the consumption-read/allocation-write race.
        reservations = self.storage.fetch_all(
            """SELECT id,strategy_version,active_stop_risk,active_notional
               FROM risk_reservations
               WHERE state='active' AND strategy_version IS NOT NULL
               ORDER BY strategy_version,id"""
        )
        reservation_ids_by_strategy: dict[str, list[str]] = {}
        for row in reservations:
            strategy = str(row.get("strategy_version") or "")
            if not strategy:
                continue
            reservation_ids_by_strategy.setdefault(strategy, []).append(str(row["id"]))
            risk_dollars[strategy] = risk_dollars.get(strategy, 0.0) + float(row.get("active_stop_risk") or 0.0)
            notional_dollars[strategy] = notional_dollars.get(strategy, 0.0) + float(row.get("active_notional") or 0.0)

        # Displayed ordinary entries and ADDs are zero-broker-state claims, but
        # they still compete for the next strategy allocation so a batch cannot
        # advertise the same sleeve repeatedly.  Rotation contingent entries
        # remain explicit zero-capital identity claims until their exit fill is
        # reconciled, as required by the exit-first contract.
        pending_rows = self.storage.fetch_all(
            """SELECT id,strategy_version,notional,payload FROM trade_proposals
               WHERE status='pending' AND lower(side)='buy'
                 AND (expires_at IS NULL OR expires_at>?)
                 AND COALESCE(relationship_type,'')!='rotation_entry'""",
            (iso_now(),),
        )
        pending_claims_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for row in pending_rows:
            try:
                payload = json.loads(row.get("payload") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            strategy = str(row.get("strategy_version") or payload.get("strategy_version") or "")
            if not strategy:
                complete = False
                reasons.append(f"pending proposal {row['id']}: strategy attribution unavailable")
                continue
            try:
                quantity = float(payload.get("qty") or 0.0)
                reference = max(
                    float(payload.get("latest_price") or 0.0),
                    float(payload.get("limit_price") or 0.0),
                )
                notional = max(
                    float(row.get("notional") or payload.get("notional") or 0.0),
                    quantity * reference,
                )
                risk = max(
                    float(payload.get("pending_add_stop_risk") or 0.0),
                    float(payload.get("stop_risk_dollars") or 0.0),
                )
            except (TypeError, ValueError):
                notional = risk = float("nan")
            if not math.isfinite(notional) or not math.isfinite(risk) or notional < 0 or risk < 0:
                complete = False
                reasons.append(f"pending proposal {row['id']}: risk or notional claim is invalid")
                continue
            risk_dollars[strategy] = risk_dollars.get(strategy, 0.0) + risk
            notional_dollars[strategy] = notional_dollars.get(strategy, 0.0) + notional
            pending_claims_by_strategy.setdefault(strategy, []).append({
                "proposal_id": str(row["id"]),
                "notional": notional,
                "stop_risk": risk,
            })
        return {
            "complete": complete,
            "risk_pct": {strategy: value / portfolio_equity * 100.0 for strategy, value in risk_dollars.items()},
            "risk_dollars": risk_dollars,
            "notional_dollars": notional_dollars,
            "active_reservation_ids_by_strategy": reservation_ids_by_strategy,
            "pending_proposal_claims_by_strategy": pending_claims_by_strategy,
            "reason": "; ".join(reasons) if reasons else None,
        }

    def _calculate_dynamic_size(
        self, symbol: str, score: float, volatility_regime: str, price: float,
        bars: pd.DataFrame, snapshot: dict[str, Any], is_add: bool = False,
        strategy_version: str | None = None,
        policy_override: Any = None,
    ) -> dict[str, Any]:
        """Calculate one conservative notional as the minimum of all ceilings.

        This method deliberately has no minimum clamp and no percentage-stop
        fallback. A missing stop, unknown pending exposure, or unavailable
        Phase 3/4 risk input returns zero with an auditable blocker.
        """
        sizing_cfg = self.config.get("position_sizing", {}) or {}
        strategy_version = strategy_version or STRATEGY_VERSION
        mode = str(sizing_cfg.get("mode"))
        resolved_policy: Any = None
        resolved_strategy_risk_multiplier: float | None = None
        resolved_permitted_stop_risk_pct: float | None = None

        def finite(value: Any, default: float | None = None) -> float | None:
            try:
                if value is None or isinstance(value, bool):
                    return default
                number = float(value)
                return number if math.isfinite(number) else default
            except (TypeError, ValueError):
                return default

        def empty(reason: str, *, atr_value: float | None = None, technical_stop_price: float | None = None, stop_model: str = "blocked") -> dict[str, Any]:
            return {
                "final_notional": 0.0, "suggested_shares": 0.0, "stop_price": None,
                "stop_distance_pct": None, "stop_distance_dollars": None, "risk_budget": 0.0,
                "risk_budget_dollars": 0.0, "stop_risk_dollars": 0.0, "score_multiplier": 1.0,
                "volatility_multiplier": 0.0, "stop_model_used": stop_model, "stop_validation_status": "blocked",
                "stop_policy_version": STOP_POLICY_VERSION, "sizing_policy_version": SIZING_POLICY_VERSION,
                "risk_based_shares": 0.0, "score_adjusted_notional": 0.0, "vol_adjusted_notional": 0.0,
                "base_notional": 0.0, "raw_risk_based_notional": 0.0, "quality_adjusted_notional": 0.0,
                "cash_cap": 0.0, "cash_available_cap": 0.0, "cash_usage_cap": 0.0, "buying_power_cap": 0.0,
                "position_cap": 0.0, "portfolio_cap": 0.0, "cluster_cap": 0.0, "stage_cap": 0.0,
                "equity_cap": 0.0, "absolute_cap": float("inf"), "stop_risk_cap": 0.0,
                "allocation_cap": 0.0, "exploration_cap": 0.0, "minimum_executable_notional": 0.0,
                "probe_cap": 0.0, "average_dollar_volume": None,
                "caps_applied": "none", "binding_caps": [], "sizing_caps": {}, "blocked_reason": reason,
                "atr_value": atr_value, "technical_stop_price": technical_stop_price,
                "pending_exposure_unknown": False, "formula_version": RISK_DECISION_VERSION,
                "strategy_version": strategy_version,
                "performance_snapshot_id": resolved_policy.performance_snapshot_id if resolved_policy else None,
                "policy_decision_id": resolved_policy.id if resolved_policy else None,
                "strategy_quality_score": resolved_policy.quality_score if resolved_policy else None,
                "strategy_state": resolved_policy.state if resolved_policy else ("SUSPENDED" if "profitability_engine" in self.config else None),
                "strategy_risk_multiplier": resolved_strategy_risk_multiplier if resolved_policy else (0.0 if "profitability_engine" in self.config else None),
                "permitted_stop_risk_pct": resolved_permitted_stop_risk_pct if resolved_policy else (0.0 if "profitability_engine" in self.config else None),
                "strategy_policy_version": resolved_policy.policy_version if resolved_policy else None,
                "binding_policy_reason": reason,
            }

        entry_price = finite(price)
        if entry_price is None or entry_price <= 0:
            return empty("validated entry price unavailable")
        if not sizing_cfg.get("enabled", True):
            return empty("position sizing is disabled; executable entries require validated risk sizing")

        equity = finite(snapshot.get("portfolio_equity"))
        cash = finite(snapshot.get("cash"))
        buying_power = finite(snapshot.get("buying_power"))
        if equity is None or equity <= 0:
            return empty("authoritative positive equity unavailable")
        if cash is None or buying_power is None:
            return empty("cash or buying-power accounting unavailable")

        phase3_enabled = bool(self.config.get("phase3", {}).get("enabled") and self.config.get("phase3", {}).get("active"))
        phase3_context: dict[str, Any] = {"phase4_mode": "disabled", "allocation_multiplier": 1.0, "regime_multiplier": 1.0, "drawdown_multiplier": 1.0}
        strategy_registry_snapshot_id = None
        sleeve_allocation_id = None
        strategy_sleeve = None
        sleeve_risk_remaining_dollars = None
        sleeve_notional_remaining = None
        strategy_sleeve_payload = None
        risk_per_trade_pct = finite(sizing_cfg.get("risk_per_trade_pct"))
        if risk_per_trade_pct is None or risk_per_trade_pct < 0:
            return empty("canonical stop-risk sizing policy is unavailable")
        base_risk_pct = risk_per_trade_pct
        if phase3_enabled:
            from .phase3_risk import Phase3Controller, drawdown_multiplier as phase3_drawdown_multiplier, regime_multiplier as phase3_regime_multiplier
            from .strategy_performance import state_risk_policy

            controller = Phase3Controller(self.storage, self.config, self.run_id)
            drawdown_pct = controller.update_equity(equity)
            persisted_policy = policy_override or self._cycle_strategy_policy(strategy_version)
            if "profitability_engine" in self.config and persisted_policy is None:
                return empty("latest strategy performance policy unavailable or invalid; new entries and adds fail closed")
            resolved_policy = persisted_policy
            states = controller.refresh_strategy_states()
            allocation_mult = controller.allocation(strategy_version, states)
            if persisted_policy is not None and allocation_mult <= 0:
                return empty("strategy execution registry or Phase 3 sleeve does not authorize entry risk")
            regime_mult = phase3_regime_multiplier(volatility_regime)
            drawdown_mult = phase3_drawdown_multiplier(drawdown_pct)
            phase4_mode = "disabled"
            exploration_gross_cap_pct = None
            exploration_heat_cap_pct = None
            probe_gross_cap_pct = None
            probe_heat_cap_pct = None
            probe_max_active_count = None
            permitted_stop_risk_pct = base_risk_pct
            strategy_risk_multiplier = None
            policy_reason = "legacy Phase 3 state authority"
            if persisted_policy is not None:
                permitted_stop_risk_pct, strategy_risk_multiplier, policy_reason = state_risk_policy(
                    persisted_policy.state,
                    initial_stop_risk_pct=controller.profile.base_stop_risk_pct,
                    add_stop_risk_pct=controller.profile.add_stop_risk_pct,
                    exploration_stop_risk_pct=float((self.config.get("phase4", {}) or {}).get("exploration_stop_risk_pct", 0.05)),
                    probe_stop_risk_pct=float((self.config.get("phase4", {}) or {}).get("probe_stop_risk_pct", 0.03)),
                    is_add=is_add,
                )
                resolved_strategy_risk_multiplier = strategy_risk_multiplier
                resolved_permitted_stop_risk_pct = permitted_stop_risk_pct
                if permitted_stop_risk_pct <= 0:
                    return empty(policy_reason)
                base_risk_pct = permitted_stop_risk_pct
            if self.config.get("phase4", {}).get("active"):
                from .phase4_allocator import AdaptiveAllocator
                if self._phase4_allocation_cache is None:
                    phase4_state = self._authoritative_runtime_state(force=True)
                    phase4_canonical = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(
                        phase4_state.get("positions", []), phase4_state.get("account")
                    )
                    phase4_pending = self._pending_execution_totals()
                    phase4_reservations = DurableExecutionStore(self.storage).active_reservations()
                    strategy_consumption = self._phase4_strategy_consumption(
                        list(phase4_state.get("positions") or []), equity
                    )
                    allocation_as_of = iso_now()
                    phase4_snapshot = {
                        "portfolio_equity": equity,
                        "as_of": allocation_as_of,
                        "equity_as_of": allocation_as_of,
                        "heat_before_pct": (
                            (float(phase4_canonical.projected_total_open_risk or 0.0)
                             + float(phase4_pending.get("total_stop_risk") or 0.0))
                            / equity * 100.0
                        ),
                        "gross_exposure_before_pct": (
                            (float(phase4_canonical.projected_gross_exposure or 0.0)
                             + float(phase4_pending.get("total_notional") or 0.0))
                            / equity * 100.0
                        ),
                        "pending_risk": float(phase4_pending.get("total_stop_risk") or 0.0),
                        "reserved_risk": float(phase4_reservations.get("active_reserved_stop_risk") or 0.0),
                        "strategy_risk_by_strategy": strategy_consumption["risk_dollars"] if strategy_consumption["complete"] else {},
                        "strategy_risk_unit": "stop_risk_dollars",
                        "strategy_notional_by_strategy": strategy_consumption["notional_dollars"] if strategy_consumption["complete"] else {},
                        "active_reservation_ids_by_strategy": strategy_consumption["active_reservation_ids_by_strategy"] if strategy_consumption["complete"] else {},
                        "pending_proposal_claims_by_strategy": strategy_consumption["pending_proposal_claims_by_strategy"] if strategy_consumption["complete"] else {},
                        "strategy_attribution_complete": strategy_consumption["complete"],
                        "strategy_attribution_reason": strategy_consumption["reason"],
                        "phase3_gross_exposure_capacity_pct": controller.profile.hard_gross_exposure_pct,
                        "strategy_registry_snapshot_id": self._ensure_strategy_registry_snapshot(),
                    }
                    self._phase4_allocation_cache = AdaptiveAllocator(self.storage, self.config, self.run_id).run(
                        regime=volatility_regime, drawdown_pct=drawdown_pct,
                        portfolio_snapshot=phase4_snapshot,
                        strategy_policy_map=self._strategy_policy_map or {},
                        as_of=allocation_as_of,
                    )
                phase4_policy = self._phase4_allocation_cache.get("strategy_policies", {}).get(strategy_version, {})
                sleeve = self._phase4_allocation_cache.get("strategy_sleeves", {}).get(strategy_version) or {}
                risk_unit = str(sleeve.get("risk_unit") or self._phase4_allocation_cache.get("phase3_available_risk_unit") or "pct_equity")
                sleeve_risk_remaining = float(sleeve.get("remaining_risk") or 0.0)
                sleeve_risk_remaining_dollars = (
                    equity * sleeve_risk_remaining / 100.0
                    if risk_unit == "pct_equity"
                    else sleeve_risk_remaining
                )
                sleeve_notional_remaining = float(sleeve.get("remaining_notional") or 0.0)
                strategy_registry_snapshot_id = self._ensure_strategy_registry_snapshot()
                sleeve_allocation_id = self._phase4_allocation_cache.get("allocation_id")
                strategy_sleeve = strategy_version
                strategy_sleeve_payload = sleeve
                phase4_mode = str(phase4_policy.get("mode") or "blocked")
                if phase4_mode == "probe":
                    if is_add:
                        return empty("PROBE permits new entries only; adds are blocked")
                    minimum_probe_score = float(self.config["phase4"]["probe_min_setup_score"])
                    if float(score) < minimum_probe_score:
                        return empty(f"PROBE setup trade score below threshold ({float(score):.2f} < {minimum_probe_score:.0f})")
                    probe_gross_cap_pct = float(self.config["phase4"]["probe_gross_exposure_pct"])
                    probe_heat_cap_pct = float(self.config["phase4"]["probe_portfolio_heat_pct"])
                    probe_max_active_count = int(self.config["phase4"]["probe_max_active_count"])
                if phase4_mode == "exploration":
                    base_risk_pct = min(base_risk_pct, float(phase4_policy.get("stop_risk_pct", 0.0)), float(phase4_policy.get("max_stop_risk_pct", 0.0)))
                    exploration_gross_cap_pct = float(phase4_policy.get("gross_exposure_cap_pct", 7.5))
                    exploration_heat_cap_pct = float(self.config["phase4"]["exploration_heat_pct"])
                    allocation_mult = base_risk_pct / controller.profile.base_stop_risk_pct if controller.profile.base_stop_risk_pct else 0.0
                elif phase4_mode == "adaptive":
                    allocation_mult = float(phase4_policy.get("risk_budget_multiplier", 0.0))
                    if allocation_mult <= 0:
                        return empty("Phase 4 evidence-aware allocation has no authorised risk capacity")
                    base_risk_pct = controller.profile.add_stop_risk_pct if is_add else controller.profile.base_stop_risk_pct
                elif phase4_mode in {"probe", "throttled"}:
                    allocation_mult = 1.0
                elif phase4_mode == "blocked":
                    return empty(str(phase4_policy.get("reason") or "Phase 4 policy blocks new entry risk"))
            elif persisted_policy is None:
                base_risk_pct = controller.profile.add_stop_risk_pct if is_add else controller.profile.base_stop_risk_pct
            risk_per_trade_pct = min(controller.profile.max_trade_stop_risk_pct, base_risk_pct * regime_mult * drawdown_mult * allocation_mult)
            phase3_context = {
                "controller": controller, "drawdown_pct": drawdown_pct, "states": states,
                "allocation_multiplier": allocation_mult, "regime_multiplier": regime_mult,
                "drawdown_multiplier": drawdown_mult, "scaled_stop_risk_pct": risk_per_trade_pct,
                "base_risk_pct": base_risk_pct, "phase4_mode": phase4_mode,
                "phase4_exploration_gross_cap_pct": exploration_gross_cap_pct,
                "phase4_exploration_heat_cap_pct": exploration_heat_cap_pct,
                "phase4_probe_gross_cap_pct": probe_gross_cap_pct,
                "phase4_probe_heat_cap_pct": probe_heat_cap_pct,
                "phase4_probe_max_active_count": probe_max_active_count,
                "policy": persisted_policy, "strategy_version": strategy_version,
                "strategy_risk_multiplier": strategy_risk_multiplier,
                "permitted_stop_risk_pct": permitted_stop_risk_pct,
                "binding_policy_reason": policy_reason,
                "strategy_registry_snapshot_id": strategy_registry_snapshot_id,
                "sleeve_allocation_id": sleeve_allocation_id,
                "strategy_sleeve": strategy_sleeve,
                "sleeve_risk_remaining_dollars": sleeve_risk_remaining_dollars,
                "sleeve_notional_remaining": sleeve_notional_remaining,
                "strategy_sleeve_payload": strategy_sleeve_payload,
            }
        risk_budget = equity * risk_per_trade_pct / 100.0

        stop_model_cfg = sizing_cfg.get("stop_model", {}) or {}
        atr_multiple = finite(stop_model_cfg.get("atr_multiple"))
        min_stop_pct = finite(stop_model_cfg.get("min_stop_pct"))
        max_stop_pct = finite(stop_model_cfg.get("max_stop_pct"))
        if atr_multiple is None or atr_multiple <= 0 or min_stop_pct is None or max_stop_pct is None or min_stop_pct < 0 or max_stop_pct < min_stop_pct:
            return empty("validated stop policy configuration is unavailable")
        atr_value: float | None = None
        technical_stop_price: float | None = None
        if not bars.empty and {"high", "low", "close"}.issubset(bars.columns):
            high = pd.to_numeric(bars["high"], errors="coerce")
            low = pd.to_numeric(bars["low"], errors="coerce")
            close = pd.to_numeric(bars["close"], errors="coerce")
            previous_close = close.shift(1)
            true_range = pd.concat([high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1).max(axis=1)
            atr_series = true_range.rolling(20, min_periods=20).mean().dropna()
            if not atr_series.empty and math.isfinite(float(atr_series.iloc[-1])) and float(atr_series.iloc[-1]) > 0:
                atr_value = float(atr_series.iloc[-1])
            ma50 = close.rolling(50, min_periods=50).mean().iloc[-1] if len(close) >= 50 else None
            recent_low = low.tail(20).min() if len(low) >= 20 else None
            technical_candidates = [float(value) for value in (ma50, recent_low) if value is not None and not pd.isna(value) and math.isfinite(float(value)) and 0 < float(value) < entry_price]
            if technical_candidates:
                technical_stop_price = min(technical_candidates)

        stop_candidates: list[tuple[str, float]] = []
        if atr_value is not None and atr_value * atr_multiple > 0:
            stop_candidates.append(("atr", atr_value * atr_multiple))
        if technical_stop_price is not None:
            stop_candidates.append(("technical", entry_price - technical_stop_price))
        if not stop_candidates:
            return empty("validated ATR or technical stop evidence unavailable", atr_value=atr_value, technical_stop_price=technical_stop_price, stop_model="blocked_missing_stop_evidence")
        stop_distance_dollars = max(distance for _source, distance in stop_candidates)
        sources = {source for source, distance in stop_candidates if math.isclose(distance, stop_distance_dollars, rel_tol=1e-9, abs_tol=1e-9)}
        stop_model_used = "atr_and_technical" if len(sources) > 1 else next(iter(sources))
        stop_price = entry_price - stop_distance_dollars
        stop_distance_pct = stop_distance_dollars / entry_price * 100.0
        if stop_distance_pct < min_stop_pct or stop_distance_pct > max_stop_pct:
            return empty("validated stop is outside configured risk bounds", atr_value=atr_value, technical_stop_price=technical_stop_price, stop_model="blocked_stop_out_of_bounds")
        stop_evidence = validate_stop_evidence(
            entry_price=entry_price, stop_price=stop_price, stop_distance_dollars=stop_distance_dollars,
            atr_value=atr_value, technical_stop_price=technical_stop_price, stop_model_used=stop_model_used,
            stop_validation_status="validated",
        )
        if not stop_evidence["valid"]:
            return empty(stop_evidence["reason"], atr_value=atr_value, technical_stop_price=technical_stop_price, stop_model="blocked_invalid_stop")

        score_mult = 1.0
        if not phase3_enabled:
            score_map = sizing_cfg.get("score_multiplier", {}) or {}
            score_mult = float(score_map.get("95_100" if score >= 95 else "85_94" if score >= 85 else "75_84" if score >= 75 else "65_74", 1.0))
        vol_map = sizing_cfg.get("volatility_multiplier", {}) or {}
        vol_mult = float(vol_map.get(str(volatility_regime), 1.0))
        risk_based_shares = risk_budget / stop_distance_dollars if stop_distance_dollars > 0 else 0.0
        risk_based_notional = risk_based_shares * entry_price
        try:
            policy = effective_notional_policy(self.config, equity, is_add=is_add)
        except (TypeError, ValueError) as exc:
            return empty(f"canonical notional policy is invalid: {exc}", atr_value=atr_value, technical_stop_price=technical_stop_price, stop_model=stop_model_used)
        if mode == "risk_portfolio":
            target_notional = risk_based_notional * score_mult * vol_mult
            if is_add:
                target_notional *= float(sizing_cfg["add_size_multiplier"])
        else:
            target_notional = policy.default_notional_usd * vol_mult

        if self.storage is None:
            reservations = {"active_reserved_notional": 0.0, "active_reserved_stop_risk": 0.0, "symbol_reserved_notional": {}, "cluster_reserved_notional": {}}
            pending = {"total_notional": 0.0, "total_stop_risk": 0.0, "by_symbol": {}, "by_cluster": {}, "exploration_stop_risk": 0.0, "exploration_risk_by_strategy": {}, "unknown": False}
        else:
            reservations = DurableExecutionStore(self.storage).active_reservations()
            pending = self._pending_execution_totals()
        if pending.get("unknown"):
            result = empty("pending buy exposure is malformed or incomplete; risk is unknown", atr_value=atr_value, technical_stop_price=technical_stop_price, stop_model=stop_model_used)
            result.update({"stop_price": stop_price, "stop_distance_pct": stop_distance_pct, "stop_distance_dollars": stop_distance_dollars,
                           "risk_budget": risk_budget, "risk_budget_dollars": risk_budget, "stop_validation_status": "validated",
                           "risk_based_shares": risk_based_shares, "raw_risk_based_notional": risk_based_notional,
                           "base_notional": policy.default_notional_usd, "minimum_executable_notional": policy.minimum_executable_notional_usd,
                           "pending_exposure_unknown": True, "pending_exposure_unknown_reason": pending.get("unknown_reason")})
            return result

        reserved_notional = float(reservations.get("active_reserved_notional") or 0.0)
        reserved_stop_risk = float(reservations.get("active_reserved_stop_risk") or 0.0)
        pending_notional = float(pending.get("total_notional") or 0.0)
        pending_stop_risk = float(pending.get("total_stop_risk") or 0.0)
        committed_notional = reserved_notional + pending_notional
        reserved_by_symbol = reservations.get("symbol_reserved_notional", {}) or {}
        reserved_by_cluster = reservations.get("cluster_reserved_notional", {}) or {}
        committed_symbol = float(reserved_by_symbol.get(symbol.upper(), 0.0)) + float((pending.get("by_symbol") or {}).get(symbol.upper(), 0.0))
        cluster_name = self._get_symbol_cluster(symbol)
        committed_cluster = float(reserved_by_cluster.get(cluster_name, 0.0)) + float((pending.get("by_cluster") or {}).get(cluster_name, 0.0)) if cluster_name else 0.0
        current_symbol_value = float((snapshot.get("single_exposures") or {}).get(symbol.upper(), 0.0)) / 100.0 * equity + committed_symbol
        current_total_value = float(snapshot.get("total_exposure_dollars") or 0.0) + committed_notional
        current_cluster_value = float((snapshot.get("cluster_exposures") or {}).get(cluster_name, 0.0)) / 100.0 * equity + committed_cluster if cluster_name else 0.0

        min_cash_reserve = equity * float(sizing_cfg["min_cash_reserve_pct"]) / 100.0
        cash_available_cap = max(0.0, cash - min_cash_reserve)
        cash_usage_cap = max(0.0, equity * float(sizing_cfg["max_cash_usage_pct"]) / 100.0)
        cash_cap = min(cash_available_cap, cash_usage_cap)
        buying_power_committed = pending_notional if snapshot.get("buying_power_includes_active_reservations") else committed_notional
        buying_power_cap = max(0.0, buying_power - buying_power_committed)
        symbol_limit = equity * float(sizing_cfg["max_position_notional_pct_equity"]) / 100.0
        symbol_cap = max(0.0, symbol_limit - current_symbol_value)
        portfolio_limit = equity * float(sizing_cfg["max_total_portfolio_exposure_pct"]) / 100.0
        portfolio_cap = max(0.0, portfolio_limit - current_total_value)
        cluster_cap = float("inf")
        if cluster_name:
            cluster_limit = equity * float(sizing_cfg["max_cluster_exposure_pct"]) / 100.0
            cluster_cap = max(0.0, cluster_limit - current_cluster_value)

        stage_cap = policy.stage_max_notional_usd if policy.stage_max_notional_usd is not None else float("inf")
        equity_cap = policy.equity_max_notional_usd if policy.equity_max_notional_usd is not None else float("inf")
        absolute_cap = policy.absolute_max_notional_usd if policy.absolute_max_notional_usd is not None else float("inf")
        stop_risk_ceiling = risk_budget
        if phase3_enabled:
            stop_risk_ceiling = equity * float(phase3_context["controller"].profile.max_trade_stop_risk_pct) / 100.0
        stop_risk_cap = notional_from_stop_risk(stop_risk_ceiling, entry_price, stop_distance_dollars)
        allocation_cap = notional_from_stop_risk(risk_budget, entry_price, stop_distance_dollars) if phase3_enabled else float("inf")
        exploration_cap = float("inf")
        probe_cap = float("inf")
        extra_caps: dict[str, float] = {}
        blocked_reason: str | None = None
        average_dollar_volume: float | None = None

        if phase3_enabled:
            state = self._authoritative_runtime_state()
            canonical = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(state["positions"], state["account"])
            profile = phase3_context["controller"].profile
            if canonical.portfolio_equity is None or canonical.projected_total_open_risk is None or canonical.projected_gross_exposure is None:
                blocked_reason = "Phase 3 exposure or stop-risk accounting unavailable"
            else:
                heat_cap_pct = profile.defensive_portfolio_heat_pct if phase3_context["regime_multiplier"] <= 0.5 else profile.max_portfolio_heat_pct
                if phase3_context.get("phase4_mode") == "exploration":
                    heat_cap_pct = min(heat_cap_pct, float(phase3_context.get("phase4_exploration_heat_cap_pct") or 0.25))
                current_heat = float(canonical.projected_total_open_risk) + pending_stop_risk
                heat_remaining = max(0.0, equity * heat_cap_pct / 100.0 - current_heat)
                extra_caps["phase3_heat_cap"] = notional_from_stop_risk(heat_remaining, entry_price, stop_distance_dollars)
                current_gross = float(canonical.projected_gross_exposure) + pending_notional
                gross_cap_pct = profile.favorable_gross_exposure_pct if phase3_context["regime_multiplier"] >= 1.0 else profile.normal_gross_exposure_pct
                if phase3_context.get("phase4_mode") == "exploration":
                    gross_cap_pct = min(gross_cap_pct, float(phase3_context.get("phase4_exploration_gross_cap_pct") or 7.5))
                extra_caps["gross_exposure_cap"] = max(0.0, equity * gross_cap_pct / 100.0 - current_gross)
                loss_metrics = build_loss_metrics(
                    state.get("loss_metrics"), account_equity=canonical.portfolio_equity,
                    daily_realized_pl=canonical.daily_realized_pl, weekly_realized_pl=canonical.weekly_realized_pl,
                    daily_confidence=canonical.daily_realized_pl_status, weekly_confidence=canonical.weekly_realized_pl_status,
                )
                if loss_metrics.daily_loss_pct is None or loss_metrics.weekly_loss_pct is None:
                    blocked_reason = "Phase 3 realized loss evidence unavailable"
                elif loss_metrics.daily_loss_pct >= profile.daily_loss_throttle_pct or loss_metrics.weekly_loss_pct >= profile.weekly_loss_throttle_pct:
                    blocked_reason = "Phase 3 realized loss throttle active"
                elif phase3_context["drawdown_multiplier"] == 0.0:
                    blocked_reason = "Phase 3 account drawdown halt active"
                average_dollar_volume = 0.0
                if not bars.empty and {"volume", "close"}.issubset(bars.columns):
                    average_dollar_volume = float((pd.to_numeric(bars["volume"], errors="coerce").tail(20) * pd.to_numeric(bars["close"], errors="coerce").tail(20)).mean())
                if not math.isfinite(average_dollar_volume) or average_dollar_volume < profile.minimum_average_dollar_volume:
                    blocked_reason = "Phase 3 liquidity floor failed"
                if phase3_context.get("phase4_mode") == "exploration":
                    existing_exploration = float(pending.get("exploration_stop_risk") or 0.0) + reserved_stop_risk
                    heat_remaining = max(0.0, equity * float(phase3_context.get("phase4_exploration_heat_cap_pct") or 0.25) / 100.0 - existing_exploration)
                    strategy_remaining = max(0.0, equity * float(self.config["phase4"]["max_exploration_stop_risk_pct"]) / 100.0 - float((pending.get("exploration_risk_by_strategy") or {}).get(strategy_version, 0.0)))
                    exploration_cap = min(
                        notional_from_stop_risk(heat_remaining, entry_price, stop_distance_dollars),
                        notional_from_stop_risk(strategy_remaining, entry_price, stop_distance_dollars),
                        extra_caps["gross_exposure_cap"],
                    )
                    extra_caps["exploration_heat_cap"] = notional_from_stop_risk(heat_remaining, entry_price, stop_distance_dollars)
                    extra_caps["exploration_strategy_cap"] = notional_from_stop_risk(strategy_remaining, entry_price, stop_distance_dollars)
                    extra_caps["exploration_gross_cap"] = extra_caps["gross_exposure_cap"]
                if phase3_context.get("phase4_mode") == "probe":
                    commitments = self._probe_commitments(strategy_version)
                    if int(commitments["active_count"]) >= int(phase3_context.get("phase4_probe_max_active_count") or 1):
                        blocked_reason = "PROBE active position or reserved-intent limit reached"
                    probe_heat_remaining = max(
                        0.0,
                        equity * float(phase3_context.get("phase4_probe_heat_cap_pct") or 0.10) / 100.0
                        - float(commitments["stop_risk"]),
                    )
                    probe_gross_remaining = max(
                        0.0,
                        equity * float(phase3_context.get("phase4_probe_gross_cap_pct") or 2.5) / 100.0
                        - float(commitments["gross_notional"]),
                    )
                    probe_cap = min(
                        notional_from_stop_risk(probe_heat_remaining, entry_price, stop_distance_dollars),
                        probe_gross_remaining,
                    )
                    extra_caps["probe_heat_cap"] = notional_from_stop_risk(probe_heat_remaining, entry_price, stop_distance_dollars)
                    extra_caps["probe_gross_cap"] = probe_gross_remaining
                    extra_caps["probe_active_count_cap"] = 0.0 if blocked_reason else float("inf")
                if self.config.get("phase4", {}).get("active"):
                    sleeve_risk_dollars = float(phase3_context.get("sleeve_risk_remaining_dollars") or 0.0)
                    sleeve_notional_dollars = float(phase3_context.get("sleeve_notional_remaining") or 0.0)
                    extra_caps["strategy_sleeve_stop_risk"] = notional_from_stop_risk(
                        sleeve_risk_dollars, entry_price, stop_distance_dollars
                    )
                    extra_caps["strategy_sleeve_notional"] = sleeve_notional_dollars
                    if not phase3_context.get("strategy_registry_snapshot_id") or not phase3_context.get("sleeve_allocation_id"):
                        blocked_reason = "strategy registry or sleeve allocation provenance is unavailable"

        caps: dict[str, float] = {
            "stage": stage_cap, "stop_risk": stop_risk_cap, "equity": equity_cap, "cash": cash_cap,
            "cash_available": cash_available_cap, "cash_usage": cash_usage_cap, "buying_power": buying_power_cap,
            "symbol": symbol_cap, "cluster": cluster_cap, "portfolio": portfolio_cap,
            "allocation": allocation_cap, "exploration": exploration_cap, "probe": probe_cap, "absolute": absolute_cap,
            **extra_caps,
        }
        finite_caps = {name: value for name, value in caps.items() if math.isfinite(value)}
        final_notional = min([max(0.0, float(target_notional)), *[max(0.0, value) for value in finite_caps.values()]])
        if blocked_reason is not None:
            final_notional = 0.0
        binding_caps = [name for name, value in finite_caps.items() if target_notional > 0 and value <= final_notional + 1e-9]
        caps_applied = [name for name, value in finite_caps.items() if target_notional > 0 and value < target_notional - 1e-9]
        minimum_executable_notional = policy.minimum_executable_notional_usd
        if final_notional > 0 and final_notional < minimum_executable_notional and blocked_reason is None:
            blocked_reason = "constrained notional is below the executable minimum"
            final_notional = 0.0
        if final_notional <= 0 and blocked_reason is None:
            blocked_reason = "no safe executable notional remains after ceiling constraints"
        if vol_mult <= 0 or volatility_regime == "extreme":
            final_notional = 0.0
            blocked_reason = f"blocked by volatility multiplier: {volatility_regime}"
        final_notional = max(0.0, final_notional)
        suggested_shares = final_notional / entry_price
        sizing_caps = {name: value for name, value in caps.items() if math.isfinite(value)}

        if phase3_enabled:
            controller = phase3_context["controller"]
            state = self._authoritative_runtime_state()
            canonical = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(state["positions"], state["account"])
            before_heat_pct = ((float(canonical.projected_total_open_risk or 0.0) + pending_stop_risk) / equity * 100.0)
            before_gross_pct = ((float(canonical.projected_gross_exposure or 0.0) + pending_notional) / equity * 100.0)
            before_symbol_pct = dict(canonical.symbol_exposure)
            before_cluster_pct = dict(canonical.cluster_exposure)
            after_heat_pct = before_heat_pct + (final_notional / entry_price * stop_distance_dollars / equity * 100.0)
            after_gross_pct = before_gross_pct + final_notional / equity * 100.0
            after_symbol_pct = dict(before_symbol_pct); after_symbol_pct[symbol.upper()] = after_symbol_pct.get(symbol.upper(), 0.0) + final_notional / equity * 100.0
            after_cluster_pct = dict(before_cluster_pct)
            if cluster_name: after_cluster_pct[cluster_name] = after_cluster_pct.get(cluster_name, 0.0) + final_notional / equity * 100.0
            decision_id = str(uuid.uuid4())
            decision = "ELIGIBLE" if final_notional > 0 and not blocked_reason else "BLOCKED"
            payload = {
                "score_used_for_sizing": False, "kelly_used": False, "kelly_diagnostic_only": phase3_context.get("phase4_mode") == "adaptive",
                "phase4_mode": phase3_context.get("phase4_mode"), "sizing_caps": sizing_caps,
                "binding_caps": binding_caps, "pending_exposure_unknown": bool(pending.get("unknown")),
                "manual_approval_required": True, "formula_version": PHASE3_DECISION_VERSION,
                "evidence_version": EVIDENCE_VERSION, "config_hash": self.config.get("effective_config_hash"),
                "strategy_version": strategy_version,
                "performance_snapshot_id": phase3_context.get("policy").performance_snapshot_id if phase3_context.get("policy") else None,
                "policy_decision_id": phase3_context.get("policy").id if phase3_context.get("policy") else None,
                "quality_score": phase3_context.get("policy").quality_score if phase3_context.get("policy") else None,
                "strategy_state": phase3_context.get("policy").state if phase3_context.get("policy") else None,
                "strategy_risk_multiplier": phase3_context.get("strategy_risk_multiplier"),
                "permitted_stop_risk_pct": phase3_context.get("permitted_stop_risk_pct"),
                "strategy_policy_version": phase3_context.get("policy").policy_version if phase3_context.get("policy") else None,
                "binding_policy_reason": phase3_context.get("binding_policy_reason"),
                "probe_limits": {
                    "stop_risk_pct": float((self.config.get("phase4", {}) or {}).get("probe_stop_risk_pct", 0.03)),
                    "portfolio_heat_pct": phase3_context.get("phase4_probe_heat_cap_pct"),
                    "gross_exposure_pct": phase3_context.get("phase4_probe_gross_cap_pct"),
                    "max_active_count": phase3_context.get("phase4_probe_max_active_count"),
                } if phase3_context.get("phase4_mode") == "probe" else None,
            }
            controller.storage.execute(
                # Keep the audit insert named and count-checked as the schema
                # grows; the values below correspond to 44 named columns.
                f"""INSERT INTO phase3_risk_decisions(
                   id,run_id,symbol,strategy_version,decision_time,decision,reason,equity,account_drawdown_pct,
                   base_stop_risk_pct,scaled_stop_risk_pct,stop_price,stop_distance,risk_budget,requested_notional,
                   stop_risk_cap,stage_cap,equity_cap,cash_cap,buying_power_cap,symbol_cap,cluster_cap,portfolio_cap,allocation_cap,exploration_cap,
                   pending_risk_before,reserved_risk_before,pending_risk_after,reserved_risk_after,
                   portfolio_heat_before_pct,portfolio_heat_after_pct,gross_exposure_after_pct,symbol_exposure_after_pct,cluster_exposure_after_pct,
                   regime,regime_multiplier,drawdown_multiplier,allocation_multiplier,binding_caps_json,evidence_version,formula_version,config_hash,profile_version,payload)
                 VALUES({','.join('?' for _ in range(44))})""",
                (decision_id,self.run_id,symbol,strategy_version,iso_now(),decision,blocked_reason or "within Phase 3 limits",equity,
                 phase3_context["drawdown_pct"],phase3_context["base_risk_pct"],phase3_context["scaled_stop_risk_pct"],stop_price,stop_distance_dollars,risk_budget,final_notional,
                 sizing_caps.get("stop_risk"),sizing_caps.get("stage"),sizing_caps.get("equity"),sizing_caps.get("cash"),sizing_caps.get("buying_power"),sizing_caps.get("symbol"),sizing_caps.get("cluster"),sizing_caps.get("portfolio"),sizing_caps.get("allocation"),sizing_caps.get("exploration"),
                 pending_stop_risk,reserved_stop_risk,pending_stop_risk + final_notional / entry_price * stop_distance_dollars,reserved_stop_risk,
                 before_heat_pct,after_heat_pct,after_gross_pct,after_symbol_pct.get(symbol.upper(), 0.0),after_cluster_pct.get(cluster_name, 0.0) if cluster_name else None,
                 volatility_regime,phase3_context["regime_multiplier"],phase3_context["drawdown_multiplier"],phase3_context["allocation_multiplier"],json_dumps(binding_caps),EVIDENCE_VERSION,PHASE3_DECISION_VERSION,self.config.get("effective_config_hash"),"adaptive_operational_paper_risk_v2",json_dumps(payload)))
            self.storage.execute(
                """UPDATE phase3_risk_decisions SET performance_snapshot_id=?,policy_decision_id=?,strategy_quality_score=?,
                   strategy_state=?,strategy_risk_multiplier=?,permitted_stop_risk_pct=?,strategy_policy_version=?,binding_policy_reason=? WHERE id=?""",
                (payload["performance_snapshot_id"], payload["policy_decision_id"], payload["quality_score"], payload["strategy_state"],
                 payload["strategy_risk_multiplier"], payload["permitted_stop_risk_pct"], payload["strategy_policy_version"],
                 payload["binding_policy_reason"], decision_id),
            )

        return {
            "final_notional": final_notional, "suggested_shares": suggested_shares, "stop_price": stop_price,
            "stop_distance_pct": stop_distance_pct, "stop_distance_dollars": stop_distance_dollars,
            "risk_budget": risk_budget, "risk_budget_dollars": risk_budget,
            "stop_risk_dollars": final_notional / entry_price * stop_distance_dollars,
            "phase4_mode": phase3_context.get("phase4_mode", "disabled"),
            "phase4_exploration_heat_cap_pct": phase3_context.get("phase4_exploration_heat_cap_pct"),
            "phase4_exploration_gross_cap_pct": phase3_context.get("phase4_exploration_gross_cap_pct"),
            "phase4_probe_heat_cap_pct": phase3_context.get("phase4_probe_heat_cap_pct"),
            "phase4_probe_gross_cap_pct": phase3_context.get("phase4_probe_gross_cap_pct"),
            "phase4_probe_max_active_count": phase3_context.get("phase4_probe_max_active_count"),
            "score_multiplier": score_mult, "volatility_multiplier": vol_mult, "stop_model_used": stop_model_used,
            "stop_validation_status": "validated", "stop_policy_version": STOP_POLICY_VERSION,
            "atr_value": atr_value, "technical_stop_price": technical_stop_price,
            "risk_based_shares": risk_based_shares, "score_adjusted_notional": target_notional,
            "vol_adjusted_notional": target_notional, "base_notional": policy.default_notional_usd,
            "raw_risk_based_notional": risk_based_notional, "quality_adjusted_notional": target_notional,
            "cash_cap": cash_cap, "cash_available_cap": cash_available_cap, "cash_usage_cap": cash_usage_cap,
            "buying_power_cap": buying_power_cap, "position_cap": symbol_cap, "portfolio_cap": portfolio_cap,
            "cluster_cap": cluster_cap, "stage_cap": stage_cap, "equity_cap": equity_cap, "absolute_cap": absolute_cap,
            "stop_risk_cap": stop_risk_cap, "allocation_cap": allocation_cap, "exploration_cap": exploration_cap, "probe_cap": probe_cap,
            "average_dollar_volume": average_dollar_volume if phase3_enabled else None,
            "minimum_executable_notional": minimum_executable_notional, "caps_applied": ", ".join(caps_applied) if caps_applied else "none",
            "binding_caps": binding_caps, "sizing_caps": sizing_caps, "blocked_reason": blocked_reason,
            "pending_exposure_unknown": False, "sizing_policy_version": SIZING_POLICY_VERSION,
            "formula_version": RISK_DECISION_VERSION,
            "phase3_account_drawdown_pct": phase3_context.get("drawdown_pct"),
            "strategy_version": strategy_version,
            "performance_snapshot_id": phase3_context.get("policy").performance_snapshot_id if phase3_context.get("policy") else None,
            "policy_decision_id": phase3_context.get("policy").id if phase3_context.get("policy") else None,
            "strategy_quality_score": phase3_context.get("policy").quality_score if phase3_context.get("policy") else None,
            "strategy_state": phase3_context.get("policy").state if phase3_context.get("policy") else None,
            "strategy_risk_multiplier": phase3_context.get("strategy_risk_multiplier"),
            "permitted_stop_risk_pct": phase3_context.get("permitted_stop_risk_pct"),
            "strategy_policy_version": phase3_context.get("policy").policy_version if phase3_context.get("policy") else None,
            "binding_policy_reason": phase3_context.get("binding_policy_reason"),
            "strategy_registry_snapshot_id": phase3_context.get("strategy_registry_snapshot_id"),
            "strategy_sleeve": phase3_context.get("strategy_sleeve"),
            "sleeve_allocation_id": phase3_context.get("sleeve_allocation_id"),
            "sleeve_stop_risk_ceiling": phase3_context.get("sleeve_risk_remaining_dollars"),
            "sleeve_notional_ceiling": phase3_context.get("sleeve_notional_remaining"),
            "strategy_sleeve_payload": phase3_context.get("strategy_sleeve_payload"),
            "risk_value": phase3_context.get("sleeve_risk_remaining_dollars"),
            "risk_unit": "stop_risk_dollars",
            "conversion_equity": (phase3_context.get("strategy_sleeve_payload") or {}).get("conversion_equity"),
            "conversion_equity_as_of": (phase3_context.get("strategy_sleeve_payload") or {}).get("conversion_equity_as_of"),
            "risk_formula_version": (phase3_context.get("strategy_sleeve_payload") or {}).get("risk_formula_version"),
        }

    def _rank_candidates(self, buy_candidates: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        from .phase4_allocator import candidate_allocation_rank

        ranked = []
        for c in buy_candidates:
            symbol = c["symbol"]
            c_name = self._get_symbol_cluster(symbol)
            cluster_exp = snapshot["cluster_exposures"].get(c_name or "", 0.0)
            strategy = str(getattr(c.get("signal"), "strategy_version", None) or c.get("strategy_version") or STRATEGY_VERSION)
            phase4_policy = (self._phase4_allocation_cache or {}).get("strategy_policies", {}).get(strategy, {})
            execution = self._adaptive_conviction_execution_evidence(strategy)
            current_regime = phase4_policy.get("current_regime_performance") or {}
            if str(current_regime.get("regime") or "").lower() != str(c.get("volatility_regime") or "").lower():
                current_regime = {}
            risk_value = c.get("risk_value", c.get("stop_risk_dollars"))
            risk_unit = c.get("risk_unit")
            if risk_value is None:
                try:
                    risk_value = (
                        float(c["final_notional"]) / float(c["price"])
                        * float(c["stop_distance_dollars"])
                    )
                    risk_unit = "stop_risk_dollars"
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    risk_value = None
            try:
                rank = candidate_allocation_rank({
                    "setup_score": c.get("score"),
                    "evidence_quality": c.get("strategy_quality_score"),
                    "regime": c.get("volatility_regime"),
                    "execution_fill_rate": execution.get("execution_fill_rate"),
                    "execution_shortfall_bps": execution.get("execution_shortfall_bps"),
                    "conservative_expected_return": (
                        current_regime.get("conservative_expected_return")
                        if current_regime.get("reliable") is True
                        else phase4_policy.get("conservative_expected_return")
                    ),
                    "uncertainty": phase4_policy.get("uncertainty"),
                    "deterioration_score": phase4_policy.get("deterioration_score"),
                    "symbol_exposure_pct": snapshot["single_exposures"].get(symbol, 0.0),
                    "cluster_exposure_pct": cluster_exp,
                    "risk_value": risk_value,
                    "risk_unit": risk_unit,
                    "conversion_equity": snapshot.get("portfolio_equity"),
                    "conversion_equity_as_of": snapshot.get("equity_as_of", snapshot.get("as_of")),
                    "evaluation_time": snapshot.get("as_of"),
                })
            except ValueError:
                # Ranking is an entry-admission step. Missing or stale canonical
                # conversion evidence rejects the candidate rather than assigning
                # a synthetic worst-risk score that could later become actionable.
                continue
            ranking_score = rank["ranking_score"]

            if c.get("is_observation"):
                ranking_score -= 50.0
            if c.get("dedupe_status") == "suppressed" or c.get("cooldown_applied") == 1:
                ranking_score -= 50.0

            ranked.append({
                **c,
                **rank,
                "ranking_score": ranking_score,
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
        current_exit_blocker: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest_score = latest_row.get("score") or 0.0
        latest_signal = latest_row.get("signal")
        no_action_reason = latest_row.get("no_action_reason") or ""
        no_act = no_action_reason.lower()
        row_reason = str(latest_row.get("reason") or "")
        row_reason_l = row_reason.lower()
        score_threshold = self.config.get("ai", {}).get("ai_review_min_score", 65)
        cluster_name = self._get_symbol_cluster(symbol)
        held_symbols = [s for s in cluster_holdings.get(cluster_name or "", []) if s != symbol]
        cluster_display = self._digest_display_cluster_name(cluster_name)
        has_position = bool(
            latest_row.get("average_entry_price")
            or latest_row.get("latest_position_price")
            or "position already exists" in no_act
            or "position already exists" in row_reason_l
        )

        if symbol in obs_watchlist:
            return {
                "status": "Observation only — no proposal allowed",
                "event": "observation_only",
                "high_score": latest_score >= score_threshold,
            }
        if latest_score < score_threshold:
            return {"status": "No proposal — score below threshold", "event": "below_threshold", "high_score": False}
        if latest_signal not in {"ENTRY", "EXIT"}:
            if has_position:
                return {"status": "Watch — already held; no valid add setup", "event": "no_add", "high_score": True}
            return {"status": "Watch — no ENTRY signal", "event": "no_entry", "high_score": True}
        if "sleep" in no_act:
            return {"status": "Watch — BUY suppressed by sleep mode", "event": "sleep", "high_score": True}
        if "cooldown" in no_act or "dedupe" in no_act:
            return {"status": "Watch — cooldown active", "event": "cooldown", "high_score": True}
        if "gpt review" in no_act or "deferred due to ai" in no_act:
            return {"status": "Watch — GPT review unavailable", "event": "gpt_unavailable", "high_score": True}
        if "provider" in no_act and ("unavailable" in no_act or "missing" in no_act or "cooldown" in no_act):
            return {"status": "Watch — provider data unavailable", "event": "provider_unavailable", "high_score": True}
        if "stale" in no_act or "stale" in row_reason_l:
            return {"status": "Watch — waiting for fresh data", "event": "stale_data", "high_score": True}
        if "price timestamp must be fresh" in no_act:
            return {"status": "Watch — waiting for fresh market validation", "event": "freshness_failed", "high_score": True}
        if "signal/proposal must be current" in no_act or "fresh market validation" in no_act:
            return {"status": "Watch — waiting for fresh market validation", "event": "freshness_failed", "high_score": True}
        if "no matching market profile" in no_act or "not in active watchlist" in no_act:
            return {"status": "Blocked — dynamic symbol missing Alpaca-approved scanner profile", "event": "dynamic_profile_validation", "high_score": True}
        # A historical exit reason is displayable only after this digest has
        # revalidated a current authoritative source. This prevents a cleared
        # proposal or prior-run decision from becoming a permanent blocker.
        if (
            current_exit_blocker
            and current_exit_blocker.get("active")
            and latest_signal == "ENTRY"
            and (
                "exit" in no_act
                or latest_row.get("exit_priority_applied")
            )
        ):
            blocker = self._exit_blocker_display_reason(current_exit_blocker)
            return {
                "status": f"Watch — New buy blocked — {blocker}",
                "event": "exit_blocked",
                "high_score": True,
                "blocker": blocker,
                "exit_blocker": current_exit_blocker,
            }
        if latest_signal == "ENTRY" and "exit" in no_act:
            stale_detail = {
                "source_type": "market_memory_exit_flag",
                "source_id": str(latest_row.get("id") or "") or None,
                "symbol": symbol,
                "status": "historical",
                "created_at": latest_row.get("created_at"),
                "expires_at": latest_row.get("expires_at_sgt"),
                "latest_validation_at": (current_exit_blocker or {}).get("latest_validation_at"),
                "classification": "stale",
                "reason": "market-memory exit wording was not reproduced by a current authoritative source",
            }
            self.storage.audit(self.run_id, "exit_blocker_ignored_stale", stale_detail)
        if "notional" in no_act or "sizing" in no_act or "buying power" in no_act:
            return {"status": "Blocked — failed risk sizing", "event": "risk_sizing", "high_score": True}
        if "total portfolio exposure" in no_act or "portfolio_total_exposure" in no_act:
            return {"status": "Blocked — portfolio exposure limit", "event": "exposure_cap", "high_score": True}
        if "single symbol exposure" in no_act or "portfolio_single_symbol_exposure" in no_act:
            return {"status": "Blocked — portfolio exposure limit", "event": "single_symbol_cap", "high_score": True}
        if "cluster positions limit" in no_act or "portfolio_cluster_positions_limit" in no_act:
            if held_symbols:
                status = f"Blocked — {cluster_display} cluster limit reached: existing {' and '.join(held_symbols)} positions"
            else:
                status = f"Blocked — {cluster_display} cluster limit reached"
            return {
                "status": status,
                "event": "cluster_limit",
                "cluster_name": cluster_display,
                "held_symbols": held_symbols,
                "high_score": True,
            }
        if "cluster exposure limit" in no_act or "portfolio_cluster_exposure_limit" in no_act:
            return {
                "status": f"Blocked — {cluster_display} cluster exposure limit",
                "event": "cluster_exposure",
                "cluster_name": cluster_display,
                "held_symbols": held_symbols,
                "high_score": True,
            }
        # Do not interpret exit wording in market_memory as a live blocker.
        # The current source check above is the only path that can emit the
        # exit_blocked digest event.
        if "emergency exit score is" in no_act or "block_new_buy_if_emergency_exit_score_above" in no_act:
            return {"status": "Watch — new buy blocked due to emergency exit risk", "event": "emergency_risk", "high_score": True}
        if "pyramiding check failed" in no_act or "add_on_check_failed" in no_act or "position not sufficiently profitable" in no_act or "cannot average down" in no_act:
            return {"status": "Watch — already held; no valid add setup", "event": "no_add", "high_score": True}
        if "no entry/exit signal" in no_act:
            if has_position or "position already exists" in no_act:
                return {"status": "Watch — already held; no valid add setup", "event": "no_add", "high_score": True}
            return {"status": "Watch — no ENTRY signal", "event": "no_entry", "high_score": True}
        if "blocked by risk checks" in no_act:
            return {"status": "Blocked — failed risk sizing", "event": "risk_blocked", "high_score": True}
        return {"status": "Watch — proposal builder returned no candidate", "event": "proposal_builder_no_candidate", "high_score": True}

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
        elif strongest_event == "exit_blocked":
            blocker = strongest.get("_blocker") or "an actionable exit has priority"
            parts.append(f"{strongest['symbol']} scored highest, but the new buy was held back because {blocker}.")
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
        static_reconciled = sorted({r["symbol"] for r in promotions if r["to_tier"] == "paper_tradable" and '"existing_static":true' in str(r.get("payload") or "")})
        to_observation = sorted({r["symbol"] for r in promotions if r["to_tier"] == "observation" and ('"universe_lane":"alpaca_compatible_us"' in str(r.get("payload") or "") or "universe_lane" not in str(r.get("payload") or ""))})
        global_observation = sorted({r["symbol"] for r in promotions if r["to_tier"] == "observation" and '"universe_lane":"global_research_only"' in str(r.get("payload") or "")})
        to_tradable = sorted({r["symbol"] for r in promotions if r["to_tier"] == "paper_tradable" and '"existing_static":true' not in str(r.get("payload") or "")})
        to_research = sorted({r["symbol"] for r in promotions if r["to_tier"] == "research_candidate"})
        demoted = sorted({r["symbol"] for r in demotions})
        parts = ["Universe update:"]
        if to_research:
            parts.append(f"Research candidates: {', '.join(to_research)}.")
        if static_reconciled:
            parts.append(f"Static paper-tradable reconciled: {', '.join(static_reconciled)}.")
        if global_observation:
            parts.append(f"Global research-only tracked: {', '.join(global_observation)}.")
        if to_observation:
            parts.append(f"Observation promoted: {', '.join(to_observation)}.")
        if not to_tradable:
            parts.append("Dynamic paper-tradable promotions: none.")
        if to_tradable:
            parts.append(f"Dynamic paper-tradable promotions: {', '.join(to_tradable)}.")
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
            "min_notional": float(sizing["minimum_executable_notional_usd"]),
        }

    def _buying_power(self, account: Any) -> float:
        return float(_value(account, "buying_power", _value(account, "cash", 0.0)) or 0.0)

    def _record_risk_budget_snapshot(self, snapshot: dict[str, Any], account: Any, now: datetime) -> dict[str, Any]:
        cfg = self._risk_budget_cfg()
        buying_power = self._buying_power(account)
        state = self._authoritative_runtime_state()
        canonical_builder = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster)
        canonical = canonical_builder.build(state.get("positions", []), account, source_at=now.isoformat())
        canonical_builder.persist(self.run_id, canonical)
        equity = canonical.portfolio_equity
        row = {
            "total_exposure_pct": ((canonical.projected_gross_exposure / equity) * 100) if canonical.projected_gross_exposure is not None and equity else None,
            "open_risk_pct": ((canonical.projected_total_open_risk / equity) * 100) if canonical.projected_total_open_risk is not None and equity else None,
            "daily_realized_loss_pct": canonical.daily_realized_loss_pct,
            "max_open_risk_pct": cfg["max_open_risk_pct"],
            "buying_power": buying_power,
            "portfolio_equity": equity,
            "cash": canonical.cash,
            "risk_snapshot_status": canonical.source_status,
            "risk_snapshot_unavailable": list(canonical.unavailable),
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
        canonical = RiskSnapshotBuilder(self.storage, self._get_symbol_cluster).build(
            self._authoritative_runtime_state().get("positions", []), account, source_at=now.isoformat()
        )
        buying_power_remaining = max(0.0, self._buying_power(account) - canonical.active_reserved_exposure)
        total_exposure_after = (
            canonical.projected_gross_exposure / equity * 100
            if canonical.projected_gross_exposure is not None and equity > 0
            else float("inf")
        )
        single_after = dict(snapshot.get("single_exposures", {}) or {})
        cluster_after = dict(snapshot.get("cluster_exposures", {}) or {})
        open_risk_after = (
            canonical.projected_total_open_risk / equity * 100
            if canonical.projected_total_open_risk is not None and equity > 0
            else float("inf")
        )
        allowed: set[str] = set()
        reasons: dict[str, str] = {}

        sleeve_inputs: dict[str, dict[str, Any]] = {}
        sleeve_notional_remaining: dict[str, float] = {}
        global_sleeve_remaining = 0.0
        if self.config.get("phase4", {}).get("active") and self._phase4_allocation_cache:
            for strategy, sleeve in (self._phase4_allocation_cache.get("strategy_sleeves") or {}).items():
                row = dict(sleeve)
                remaining = float(row.get("remaining_risk") or 0.0)
                if str(row.get("risk_unit") or "pct_equity") == "pct_equity":
                    remaining = equity * remaining / 100.0
                row["remaining_risk"] = max(0.0, remaining)
                row["risk_unit"] = "stop_risk_dollars"
                sleeve_inputs[str(strategy)] = row
                sleeve_notional_remaining[str(strategy)] = max(0.0, float(row.get("remaining_notional") or 0.0))
            global_sleeve_remaining = sum(
                float(row.get("remaining_risk") or 0.0) for row in sleeve_inputs.values()
            )

        for candidate in ranked_candidates:
            symbol = str(candidate["symbol"]).upper()
            rank = int(candidate.get("final_candidate_rank") or 0)
            raw_notional = float(candidate.get("final_notional", 0.0) or 0.0)
            price = float(candidate.get("price", candidate.get("latest_price", 0.0)) or 0.0)
            stop_distance_dollars = candidate.get("stop_distance_dollars")
            stop_evidence = validate_stop_evidence(
                entry_price=price,
                stop_price=candidate.get("stop_price"),
                stop_distance_dollars=stop_distance_dollars,
                atr_value=candidate.get("atr_value"),
                technical_stop_price=candidate.get("technical_stop_price"),
                stop_model_used=candidate.get("stop_model_used"),
                stop_validation_status=candidate.get("stop_validation_status"),
            )
            stop_distance_pct = None
            if stop_evidence["valid"] and price > 0:
                stop_distance_pct = float(stop_evidence["stop_distance_dollars"]) / price * 100.0

            cap_reason = None
            reduction_reason = None
            final_notional = max(0.0, raw_notional)

            current_symbol_pct = float(single_after.get(symbol, 0.0) or 0.0)
            cluster_name = self._get_symbol_cluster(symbol)
            current_cluster_pct = float(cluster_after.get(cluster_name or "", 0.0) or 0.0)

            def pct_to_notional(remaining_pct: float) -> float:
                return max(0.0, equity * remaining_pct / 100)

            limits: list[tuple[float, str]] = []
            if not stop_evidence["valid"]:
                cap_reason = "not actionable - validated ATR or technical stop evidence is required"
                reduction_reason = stop_evidence["reason"]
            elif price <= 0 or stop_distance_pct is None or stop_distance_pct <= 0:
                cap_reason = "not actionable - validated stop geometry is unavailable"
            else:
                try:
                    policy = effective_notional_policy(
                        self.config, equity, is_add=bool(candidate.get("is_add")),
                    )
                    limits.extend((value, f"{name} ceiling") for name, value in policy.named_ceilings().items())
                except (TypeError, ValueError) as exc:
                    cap_reason = f"not actionable - canonical notional policy invalid: {exc}"
                sizing_caps = candidate.get("sizing_caps") or {}
                if isinstance(sizing_caps, dict):
                    for name, value in sizing_caps.items():
                        try:
                            value_f = float(value)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(value_f):
                            limits.append((value_f, f"{name} ceiling"))
                if cap_reason is None:
                    limits.extend([
                        (pct_to_notional(cfg["max_total_portfolio_exposure_pct"] - total_exposure_after), "portfolio exposure budget"),
                        (pct_to_notional(cfg["max_single_symbol_exposure_pct"] - current_symbol_pct), "single-symbol exposure budget"),
                        (pct_to_notional(cfg["max_cluster_exposure_pct"] - current_cluster_pct), "cluster exposure budget"),
                        (buying_power_remaining, "paper buying power"),
                    ])
                    try:
                        sizing = self.config["position_sizing"]
                        cash_value = float(canonical.cash)
                        min_cash_reserve_pct = float(sizing["min_cash_reserve_pct"])
                        max_cash_usage_pct = float(sizing["max_cash_usage_pct"])
                        cash_available = max(0.0, cash_value - equity * min_cash_reserve_pct / 100.0)
                        cash_usage = max(0.0, equity * max_cash_usage_pct / 100.0)
                        limits.extend([(cash_available, "cash available ceiling"), (cash_usage, "cash usage ceiling")])
                    except (KeyError, TypeError, ValueError):
                        cap_reason = "not actionable - canonical cash ceiling policy is unavailable"
                    if cap_reason is None:
                        stop_risk_factor = stop_distance_pct / 100.0
                        per_trade_risk_cap_notional = pct_to_notional(cfg["risk_per_trade_pct"]) / stop_risk_factor
                        limits.append((per_trade_risk_cap_notional, "per-trade risk budget"))
                        remaining_open_risk_pct = cfg["max_open_risk_pct"] - open_risk_after
                        risk_cap_notional = pct_to_notional(remaining_open_risk_pct) / stop_risk_factor
                        limits.append((risk_cap_notional, "open risk budget"))

            if cap_reason is None:
                for limit_value, reason in limits:
                    if final_notional > limit_value:
                        final_notional = max(0.0, limit_value)
                        reduction_reason = reason

            if cap_reason is None and candidate.get("preproposal_block_reason"):
                cap_reason = f"not actionable - pre-proposal risk check failed: {candidate['preproposal_block_reason']}"
            if cap_reason is None and final_notional < cfg["min_notional"]:
                cap_reason = "not actionable - insufficient risk budget after higher-ranked candidates"

            if cap_reason is None and sleeve_inputs:
                from .phase4_allocator import allocate_candidates_to_sleeves

                signal = candidate.get("signal")
                strategy = str(
                    candidate.get("strategy_version")
                    or getattr(signal, "strategy_version", "")
                    or STRATEGY_VERSION
                )
                remaining_notional = sleeve_notional_remaining.get(strategy)
                if remaining_notional is None:
                    cap_reason = "not actionable - Phase 4 strategy sleeve allocation rejected the candidate"
                else:
                    if final_notional > remaining_notional:
                        final_notional = max(0.0, remaining_notional)
                        reduction_reason = "Phase 4 strategy sleeve candidate-set notional budget"
                    if final_notional < cfg["min_notional"]:
                        cap_reason = "not actionable - Phase 4 sleeve remainder is below executable minimum"
                    requested_risk = (
                        final_notional * float(stop_distance_pct or 0.0) / 100.0
                        if cap_reason is None else 0.0
                    )
                    sleeve_plan = allocate_candidates_to_sleeves(
                        [{
                            "candidate_id": f"{strategy}:{symbol}",
                            "strategy_version": strategy,
                            "symbol": symbol,
                            "action": "add" if candidate.get("is_add") else "entry",
                            "side": "buy",
                            "risk_value": requested_risk,
                            "risk_unit": "stop_risk_dollars",
                            "minimum_risk_value": cfg["min_notional"] * float(stop_distance_pct or 0.0) / 100.0,
                            "minimum_risk_unit": "stop_risk_dollars",
                            "setup_score": candidate.get("score"),
                            "evidence_quality": candidate.get("strategy_quality_score"),
                            "regime": candidate.get("volatility_regime"),
                            "spread_bps": candidate.get("quote_spread_bps"),
                            "average_dollar_volume": candidate.get("average_dollar_volume"),
                        }],
                        sleeve_inputs,
                        global_available_risk=global_sleeve_remaining,
                        global_risk_unit="stop_risk_dollars",
                        conversion_equity=equity,
                        conversion_equity_as_of=allocation_as_of,
                        evaluation_time=allocation_as_of,
                    )
                    sleeve_decision = sleeve_plan["decisions"][0]
                    if sleeve_decision.get("decision") not in {"ALLOCATE", "ALLOCATE_PARTIAL"}:
                        cap_reason = "not actionable - Phase 4 strategy sleeve allocation rejected the candidate"
                    else:
                        allocated_risk = float(sleeve_decision["allocated_risk"])
                        risk_factor = float(stop_distance_pct or 0.0) / 100.0
                        sleeve_notional_cap = allocated_risk / risk_factor if risk_factor > 0 else 0.0
                        if final_notional > sleeve_notional_cap:
                            final_notional = max(0.0, sleeve_notional_cap)
                            reduction_reason = "Phase 4 strategy sleeve candidate-set risk budget"
                        candidate["phase4_candidate_allocation"] = sleeve_decision
                        sleeve_inputs = {
                            str(key): dict(value) for key, value in sleeve_plan["sleeves_after"].items()
                        }
                        global_sleeve_remaining = float(sleeve_plan["global_remaining_risk"])
                        sleeve_notional_remaining[strategy] = max(0.0, remaining_notional - final_notional)

            risk_pct = (final_notional * (stop_distance_pct / 100) / equity) * 100 if equity and stop_distance_pct else 0.0
            exposure_pct = (final_notional / equity) * 100 if equity else 0.0
            total_after_candidate = total_exposure_after + exposure_pct
            single_after_candidate = current_symbol_pct + exposure_pct
            cluster_after_candidate = current_cluster_pct + exposure_pct
            open_risk_after_candidate = open_risk_after + risk_pct

            passed = True
            if candidate.get("preproposal_block_reason"):
                passed = False
                cap_reason = f"not actionable - pre-proposal risk check failed: {candidate['preproposal_block_reason']}"
            elif cap_reason:
                passed = False
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
            f"- Broker-reported paper buying power: ${risk_snapshot.get('buying_power', 0.0):,.2f} (sizing remains cash-only; margin and leverage are disabled)",
            f"- Account Equity: ${risk_snapshot.get('portfolio_equity', 0.0):,.2f}",
            f"- Available Cash: ${risk_snapshot.get('cash', 0.0):,.2f}",
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

            action_type_label = "NEW ENTRY"
            if proposal.get("is_add") or pm_type == "HEALTHY_PULLBACK_ADD":
                action_type_label = "ADD TO WINNER"
            elif pm_type or proposal.get("side") == "sell":
                action_type_label = "EXIT"

            lines.extend([
                f"{idx}. {action_word} {proposal['symbol']} - ${float(proposal.get('notional') or 0.0):.2f} / approx. {qty:.6f} shares",
                f"   Action: {action_type_label}",
                f"   Score: {float(proposal.get('score') or 0.0):.0f}",
            ])

            if proposal.get("side") == "buy":
                risk_bud = proposal.get("risk_budget")
                stop_dist_d = proposal.get("stop_distance_dollars")
                stop_dist_p = proposal.get("stop_distance_pct")
                score_mult = proposal.get("score_multiplier")
                vol_mult = proposal.get("volatility_multiplier")
                caps = proposal.get("caps_applied")

                sizing_basis_parts = []
                if risk_bud is not None:
                    sizing_basis_parts.append(f"risk budget: ${risk_bud:.2f}")
                if stop_dist_d is not None and stop_dist_p is not None:
                    sizing_basis_parts.append(f"stop: ${stop_dist_d:.2f} ({stop_dist_p:.1f}%)")
                if score_mult is not None:
                    sizing_basis_parts.append(f"score mult: {score_mult:.2f}x")
                if vol_mult is not None:
                    sizing_basis_parts.append(f"vol mult: {vol_mult:.2f}x")

                if sizing_basis_parts:
                    lines.append(f"   Sizing Basis: {', '.join(sizing_basis_parts)}")
                if caps and caps != "none":
                    lines.append(f"   Caps Applied: {caps}")
            else:
                lines.extend([
                    "   Risk: normal",
                    "   Portfolio fit: passes risk budget",
                ])

            lines.append(f"   Reason: {_normalize_ranked_candidate_reason(proposal.get('selection_reason') or proposal.get('reason'), idx)}")
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
        lines.append("")
        lines.append("⚠️ Final order size will be revalidated before placement.")
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

    def _run_phase2_shadow(self, profile_results: list[dict[str, Any]], now: datetime) -> None:
        phase2 = self.config.get("phase2_shadow_strategies", {})
        if not phase2.get("enabled", False):
            return
        if phase2.get("mode") != "SHADOW_ONLY":
            raise RuntimeError("Phase 2 strategies require mode=SHADOW_ONLY")
        from .shadow_strategies import ShadowStrategyEngine

        insights = ShadowStrategyEngine(self.storage, self.run_id).evaluate(profile_results, observed_at=now)
        self.storage.audit(
            self.run_id,
            "phase2_shadow_insights_recorded",
            {
                "mode": "SHADOW_ONLY",
                "insights": len(insights),
                "active": sum(insight.signal == "active" for insight in insights),
                "sleeves": sorted({insight.sleeve for insight in insights}),
                "execution_surfaces_called": 0,
            },
        )

    def _run_performance_lab(self, profile_results: list[dict[str, Any]], active_watchlist: list[str], positions: list[Any], now: datetime, snapshot: dict[str, Any]) -> None:
        qualified_setups_cnt = 0
        shadow_trades_cnt = 0
        actual_trades_cnt = 0
        active_set = {s.upper() for s in active_watchlist}
        held = {str(_value(p, "symbol", "")).upper() for p in positions}

        for res in profile_results:
            score = float(res.get("score") or 0.0)
            symbol = str(res["symbol"]).upper()
            signal = res["signal"]
            reason = res.get("performance_not_proposed_reason") or res.get("no_action_reason") or signal.reason
            is_near_miss = score >= 55 or bool(res.get("performance_candidate_suppression_reason")) or symbol not in active_set
            is_meaningful = score >= 65 or signal.action in ("ENTRY", "EXIT") or is_near_miss
            if not is_meaningful:
                continue

            qualified_setups_cnt += 1
            setup_id = str(uuid.uuid4())
            tier_rows = self.storage.fetch_all("SELECT tier, asset_class FROM universe_symbols WHERE symbol=? LIMIT 1", (symbol,))
            tier = tier_rows[0]["tier"] if tier_rows else ("held_position" if symbol in held else ("paper_tradable" if symbol in active_set else "observation"))
            asset_class = str((tier_rows[0].get("asset_class") if tier_rows else None) or "equity").lower()
            if asset_class in {"us_equity", "common_stock", "stock"}:
                asset_class = "equity"
            elif asset_class in {"fund", "etf"} or symbol in {"SPY", "QQQ", "DIA", "IWM", "XLK", "XLF", "XLV", "XLE", "XLY"}:
                asset_class = "etf"

            if signal.action == "ENTRY" and res.get("is_add"):
                setup_type = "add_to_winner"
            elif signal.action == "ENTRY":
                setup_type = "new_entry"
            elif signal.action == "EXIT":
                setup_type = "exit"
            elif score >= 55:
                setup_type = "near_miss"
            elif reason:
                setup_type = "suppressed_candidate"
            else:
                setup_type = "hold_watch"

            proposed = int(bool(res.get("proposal_generated")))
            action_decision = res.get("performance_action_decision") or ("proposed" if proposed else "shadow_only")
            proposed_notional = res.get("performance_proposed_notional")
            hypothetical_notional = proposed_notional if proposed_notional is not None else res.get("final_notional")
            price_age = res.get("performance_price_age_seconds")
            data_freshness = "fresh" if isinstance(price_age, (int, float)) and -5 <= price_age <= self.config.get("risk", {}).get("max_price_age_seconds", 120) else "stale_or_unknown"

            score_components = {
                "asset_score": res.get("asset_score"),
                "trade_score": score,
                "volatility_score_contribution": res.get("score_vol"),
                "setup_quality_score": res.get("setup_quality_score"),
                "portfolio_fit_score": res.get("portfolio_fit_score"),
                "diversification_score": res.get("diversification_score"),
                "sizing_score": res.get("sizing_score"),
            }
            signal_state = {"action": signal.action, "side": signal.side, "reason": signal.reason, "confidence": signal.confidence}
            trend_metrics = {k: signal.indicators.get(k) for k in ("ma_50", "ma_200", "close") if signal.indicators and k in signal.indicators}
            volatility_metrics = {
                "volatility_20": res.get("vol_20"),
                "volatility_regime": res.get("volatility_regime"),
                "atr_value": res.get("atr_value"),
                "adverse_move_atr": res.get("adverse_move_atr"),
            }
            liquidity_metrics = {"volume": res.get("volume")}
            relative_strength_metrics = {"asset_score": res.get("asset_score"), "true_score_rank": res.get("true_score_rank")}
            portfolio_exposure = {
                "portfolio_equity": snapshot.get("portfolio_equity"),
                "total_exposure_pct": snapshot.get("total_exposure_pct"),
                "single_symbol_exposure_pct": (snapshot.get("single_exposures") or {}).get(symbol, 0.0),
            }
            cluster_exposure = {"cluster_exposures": snapshot.get("cluster_exposures")}
            risk_budget = {"risk_budget": res.get("risk_budget"), "dedupe_status": res.get("dedupe_status"), "cooldown_reason": res.get("cooldown_reason")}

            proposal_id = res.get("proposal_id")
            batch_id = res.get("performance_batch_id")
            self.storage.execute(
                """
                INSERT INTO performance_setups(
                    id,timestamp,run_id,symbol,asset_class,tier,setup_type,action_decision,proposed,proposal_id,batch_id,
                    not_proposed_reason,score,score_components,signal_state,entry_signal,exit_signal,add_signal,current_price,
                    price_timestamp,data_freshness,trend_metrics,volatility_metrics,liquidity_metrics,relative_strength_metrics,
                    portfolio_exposure,cluster_exposure,risk_budget,proposed_notional,hypothetical_notional,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    setup_id, now.isoformat(), self.run_id, symbol, asset_class, tier, setup_type, action_decision,
                    proposed, proposal_id, batch_id, None if proposed else reason, score, json_dumps(score_components),
                    json_dumps(signal_state), int(signal.action == "ENTRY" and signal.side == "buy"),
                    int(signal.action == "EXIT"), int(bool(res.get("is_add"))), res.get("price"),
                    str(res.get("price_at")) if res.get("price_at") is not None else None, data_freshness,
                    json_dumps(trend_metrics), json_dumps(volatility_metrics), json_dumps(liquidity_metrics),
                    json_dumps(relative_strength_metrics), json_dumps(portfolio_exposure), json_dumps(cluster_exposure),
                    json_dumps(risk_budget), proposed_notional, hypothetical_notional, now.isoformat(), now.isoformat(),
                ),
            )

            blockers = self._performance_lab_blockers(res, signal, reason, active_set, data_freshness)
            for blocker, blocker_reason in blockers:
                self.storage.execute(
                    "INSERT INTO performance_blockers(id,setup_id,run_id,symbol,blocker,reason,severity,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), setup_id, self.run_id, symbol, blocker, blocker_reason, "blocking", now.isoformat()),
                )

            actual_or_shadow = "actual" if proposed else "shadow"
            if proposed:
                actual_trades_cnt += 1
            else:
                shadow_trades_cnt += 1
                if signal.action == "ENTRY" and signal.side == "buy" and score >= float(self.config.get("ai", {}).get("ai_review_min_score", 65)):
                    shadow_id = str(uuid.uuid4())
                    port_state = {
                        "portfolio_equity": snapshot.get("portfolio_equity"),
                        "total_exposure_pct": snapshot.get("total_exposure_pct"),
                        "single_exposure_pct": (snapshot.get("single_exposures") or {}).get(symbol, 0.0),
                        "cluster_exposures": snapshot.get("cluster_exposures"),
                    }
                    self.storage.execute(
                        """INSERT INTO shadow_trades(
                            id, run_id, setup_id, symbol, side, would_have_entry_price, would_have_entry_time,
                            would_have_notional, would_have_shares, would_have_stop_price, would_have_stop_distance_pct,
                            reason_not_executed, score, volatility_regime, gpt_confidence, gpt_caution, setup_key,
                            portfolio_state_json, sleep_mode_active, cooldown_state, selected_actual_trade_this_cycle
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            shadow_id, self.run_id, setup_id, symbol, "buy", res.get("price"), now.isoformat(),
                            hypothetical_notional, res.get("suggested_shares", 0.0), res.get("stop_price"),
                            res.get("stop_distance_pct"), reason or "suppressed", score, res.get("volatility_regime"),
                            res.get("gpt_confidence"), res.get("gpt_caution"), res.get("setup_key"),
                            json_dumps(port_state), res.get("sleep_mode_active", 0), res.get("dedupe_status"), 0,
                        ),
                    )
                    self.storage.execute(
                        """INSERT INTO trade_outcomes(
                            id, trade_id, actual_or_shadow, symbol, entry_time, entry_price, outcome_status,
                            stop_hit, target_reached, add_on_improved, beat_shadow_alternatives, updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), shadow_id, "shadow", symbol, now.isoformat(), res.get("price"),
                            "pending", 0, 0, None, None, now.isoformat(),
                        ),
                    )
            self.storage.execute(
                """
                INSERT INTO performance_outcomes(
                    id,setup_id,run_id,symbol,proposal_id,batch_id,actual_or_shadow,entry_time,entry_price,
                    entry_notional,entry_qty,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()), setup_id, self.run_id, symbol, proposal_id, batch_id, actual_or_shadow,
                    now.isoformat(), res.get("price"), hypothetical_notional, res.get("suggested_shares"),
                    "pending_forward_returns", now.isoformat(), now.isoformat(),
                ),
            )
            for horizon in (1, 5, 20):
                from .research_validation import ExchangeSessions

                due_session = ExchangeSessions().add_sessions(now.date(), horizon)
                due_at = datetime.combine(due_session, now.timetz()).astimezone(UTC)
                self.storage.execute(
                    """
                    INSERT INTO performance_forward_returns(
                        id,setup_id,run_id,symbol,horizon_days,due_at,eligible_to_update,status,reason
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (str(uuid.uuid4()), setup_id, self.run_id, symbol, horizon, due_at.isoformat(), 0, "pending", "waiting_for_elapsed_horizon"),
                )
            if not proposed:
                self.storage.execute(
                    """
                    INSERT INTO performance_counterfactuals(
                        id,setup_id,run_id,symbol,counterfactual_type,would_enter,would_add,would_exit,
                        hypothetical_entry_price,hypothetical_notional,reason,comparison_status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()), setup_id, self.run_id, symbol, setup_type,
                        int(signal.action == "ENTRY" and not res.get("is_add")), int(bool(res.get("is_add"))),
                        int(signal.action == "EXIT"), res.get("price"), hypothetical_notional, reason,
                        "pending_forward_outcome", now.isoformat(), now.isoformat(),
                    ),
                )

            if tier_rows:
                self.storage.execute(
                    "INSERT INTO dynamic_universe_performance(id,run_id,symbol,tier,metric,value,created_at,payload) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), self.run_id, symbol, tier, "performance_lab_setup_score", score,
                        now.isoformat(), json_dumps({"setup_id": setup_id, "setup_type": setup_type, "action_decision": action_decision}),
                    ),
                )

        self._sync_performance_lab_order_links()
        self.storage.execute(
            "INSERT INTO performance_lab_summaries(id, run_id, timestamp, total_qualified_setups, total_shadow_trades, total_actual_trades) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), self.run_id, now.isoformat(), qualified_setups_cnt, shadow_trades_cnt, actual_trades_cnt),
        )

    def _run_crypto_research_due(self) -> list[Any]:
        crypto_cfg = self.config.get("crypto") or {}
        if not crypto_cfg.get("enabled", False):
            return []
        try:
            return CryptoResearchEngine(self.config, self.storage, self.broker, self.telegram, self.run_id).run_due(datetime.now(UTC))
        except Exception as exc:
            logger.warning("crypto_research_due_failed: %s", exc)
            self.storage.audit(self.run_id, "crypto_research_due_failed", {"error": type(exc).__name__})
            return []

    def run_crypto_research_due(self) -> list[Any]:
        return self._run_crypto_research_due()

    def _performance_lab_blockers(self, res: dict[str, Any], signal: Any, reason: str | None, active_set: set[str], data_freshness: str) -> list[tuple[str, str]]:
        blockers: list[tuple[str, str]] = []
        symbol = str(res.get("symbol", "")).upper()
        score = float(res.get("score") or 0.0)
        threshold = float(self.config.get("ai", {}).get("ai_review_min_score", 65))
        reason_text = str(reason or "")
        lower_reason = reason_text.lower()
        if score < threshold:
            blockers.append(("score_below_threshold", reason_text or f"score {score:.2f} below {threshold:.2f}"))
        if signal.action != "ENTRY" and "entry" in lower_reason:
            blockers.append(("no_entry_signal", reason_text))
        if res.get("has_position") and signal.action != "ENTRY" and not res.get("is_add"):
            blockers.append(("already_held_no_valid_add", reason_text or "already held without valid add setup"))
        if res.get("is_add") and not res.get("proposal_generated"):
            blockers.append(("no_add_signal", reason_text or "add setup did not pass all add gates"))
        if "risk" in lower_reason:
            blockers.append(("risk_gate", reason_text))
        if "cluster" in lower_reason:
            blockers.append(("cluster_gate", reason_text))
        if "exposure" in lower_reason:
            blockers.append(("exposure_gate", reason_text))
        if data_freshness != "fresh" or "stale" in lower_reason:
            blockers.append(("stale_price", reason_text or "price timestamp stale or unknown"))
        if "missing" in lower_reason or "insufficient" in lower_reason:
            blockers.append(("missing_data", reason_text))
        if "cooldown" in lower_reason or res.get("cooldown_applied"):
            blockers.append(("cooldown", reason_text or str(res.get("cooldown_reason") or "cooldown applied")))
        if "max daily" in lower_reason or "trades_today" in lower_reason:
            blockers.append(("max_daily_trades", reason_text))
        if "market closed" in lower_reason:
            blockers.append(("market_closed", reason_text))
        if "provider" in lower_reason or "alpaca" in lower_reason:
            blockers.append(("provider_guard", reason_text))
        if symbol not in active_set:
            blockers.append(("observation_only", reason_text or "symbol not in active paper-tradable scanner set"))
        if str(res.get("tier") or "").lower() == "research_candidate":
            blockers.append(("research_only", reason_text or "research candidate only"))
        if not blockers and not res.get("proposal_generated"):
            blockers.append(("other", reason_text or "measurement-only setup was not proposed"))
        return blockers

    def _sync_performance_lab_order_links(self) -> None:
        rows = self.storage.fetch_all(
            """
            SELECT ps.id AS setup_id, ps.proposal_id, o.id AS order_id, o.broker_order_id, o.status AS order_status,
                   o.notional AS submitted_notional, f.id AS fill_id, f.price AS fill_price, f.qty AS fill_qty,
                   c.batch_id
            FROM performance_setups ps
            LEFT JOIN orders o ON o.proposal_id=ps.proposal_id
            LEFT JOIN fills f ON f.order_id=o.id
            LEFT JOIN proposal_batch_candidates c ON c.proposal_id=ps.proposal_id
            WHERE ps.proposal_id IS NOT NULL
            """
        )
        now_iso = iso_now()
        for row in rows:
            self.storage.execute(
                """
                UPDATE performance_setups
                SET batch_id=COALESCE(?, batch_id), final_submitted_notional=COALESCE(?, final_submitted_notional),
                    order_id=COALESCE(?, order_id), broker_order_id=COALESCE(?, broker_order_id),
                    fill_id=COALESCE(?, fill_id), order_status=COALESCE(?, order_status),
                    fill_price=COALESCE(?, fill_price), fill_qty=COALESCE(?, fill_qty), updated_at=?
                WHERE id=?
                """,
                (
                    row.get("batch_id"), row.get("submitted_notional"), row.get("order_id"), row.get("broker_order_id"),
                    str(row.get("fill_id")) if row.get("fill_id") is not None else None, row.get("order_status"),
                    row.get("fill_price"), row.get("fill_qty"), now_iso, row.get("setup_id"),
                ),
            )
            self.storage.execute(
                """
                UPDATE performance_outcomes
                SET batch_id=COALESCE(?, batch_id), order_id=COALESCE(?, order_id),
                    broker_order_id=COALESCE(?, broker_order_id), fill_id=COALESCE(?, fill_id),
                    entry_price=COALESCE(?, entry_price), entry_qty=COALESCE(?, entry_qty),
                    entry_notional=COALESCE(?, entry_notional), updated_at=?
                WHERE setup_id=?
                """,
                (
                    row.get("batch_id"), row.get("order_id"), row.get("broker_order_id"),
                    str(row.get("fill_id")) if row.get("fill_id") is not None else None,
                    row.get("fill_price"), row.get("fill_qty"), row.get("submitted_notional"), now_iso, row.get("setup_id"),
                ),
            )

    def _update_forward_outcomes(self) -> None:
        # Phase 1 owns outcome calculation. Both legacy tables below are now
        # compatibility projections from one exchange-session-aware result.
        from .research_validation import update_service_outcomes

        now = datetime.now(UTC)
        self._sync_performance_lab_order_links()
        cfg = self._runtime_orchestration_cfg()
        result = update_service_outcomes(
            self.storage,
            self.broker,
            now=now,
            max_updates=int(cfg.get("max_forward_outcome_updates_per_cycle", 25)),
            run_id=self.run_id,
            bar_cache=self._phase1_bar_cache,
        )
        self.storage.audit(self.run_id, "canonical_forward_outcomes_updated", result)
        return

    def _update_performance_forward_returns(self, now: datetime | None = None) -> None:
        from .research_validation import update_service_outcomes

        now = now or datetime.now(UTC)
        cfg = self._runtime_orchestration_cfg()
        update_service_outcomes(
            self.storage,
            self.broker,
            now=now,
            max_updates=int(cfg.get("max_forward_outcome_updates_per_cycle", 25)),
            run_id=self.run_id,
            bar_cache=self._phase1_bar_cache or None,
        )
        return

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
