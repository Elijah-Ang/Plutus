"""Authoritative, persisted execution-time risk evidence.

The execution caller is intentionally outside the trust boundary.  Broker,
SQLite and measured health/control providers are the only sources used to
construct the final RiskEngine context.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .approval_authority import canonical_json
from .canonical_sizing import canonical_sizing
from .formula_versions import (
    ACCOUNTING_VERSION,
    ADAPTIVE_CONVICTION_FORMULA_VERSION,
    ADAPTIVE_SIZING_FORMULA_VERSION,
    EVIDENCE_VERSION,
    PHASE4_ALLOCATION_VERSION,
    PROFITABILITY_RANKING_FORMULA_VERSION,
    PROFITABILITY_VALIDATION_FORMULA_VERSION,
    PROFIT_ATTRIBUTION_FORMULA_VERSION,
    RISK_DECISION_VERSION,
    SIZING_POLICY_VERSION,
    STOP_POLICY_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
    STRATEGY_POLICY_VERSION,
    TRADE_ECONOMICS_FORMULA_VERSION,
)
from .internet import internet_available
from .loss_controls import LOSS_METRICS_VERSION, build_loss_metrics
from .power import get_power_status
from .utils import PROJECT_ROOT


RISK_SNAPSHOT_VERSION = "execution_risk_snapshot_v2_authoritative"
RISK_SNAPSHOT_TTL_SECONDS = 30
LOSS_EVIDENCE_MAX_AGE_SECONDS = 300

REQUIRED_FORMULA_VERSIONS = {
    "stop_policy": STOP_POLICY_VERSION,
    "sizing_policy": SIZING_POLICY_VERSION,
    "risk_decision": RISK_DECISION_VERSION,
    "accounting": ACCOUNTING_VERSION,
    "evidence": EVIDENCE_VERSION,
    "strategy_performance": STRATEGY_PERFORMANCE_VERSION,
    "strategy_policy": STRATEGY_POLICY_VERSION,
    "trade_economics": TRADE_ECONOMICS_FORMULA_VERSION,
    "profitability_ranking": PROFITABILITY_RANKING_FORMULA_VERSION,
    "profitability_validation": PROFITABILITY_VALIDATION_FORMULA_VERSION,
    "profit_attribution": PROFIT_ATTRIBUTION_FORMULA_VERSION,
    "adaptive_conviction": ADAPTIVE_CONVICTION_FORMULA_VERSION,
    "adaptive_sizing": ADAPTIVE_SIZING_FORMULA_VERSION,
    "phase4_allocation": PHASE4_ALLOCATION_VERSION,
    "strategy_registry": STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
}

ACTIVE_INTENT_STATES = (
    "created",
    "reserved",
    "retryable_pre_submission",
    "submitting",
    "submitted",
    "partially_filled",
    "cancel_pending",
    "unknown",
    "reconciliation_required",
)

Provider = Callable[[], Any]


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _value(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _utc(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _finite(value: Any, label: str, *, nonnegative: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} is invalid") from exc
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise RuntimeError(f"{label} is invalid")
    return number


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_items(values: Any, identity_fields: tuple[str, ...]) -> list[Any]:
    items = [_plain(item) for item in (values or [])]

    def key(item: Any) -> tuple[str, ...]:
        if not isinstance(item, Mapping):
            return (canonical_json({"value": item}),)
        return tuple(str(item.get(field) or "") for field in identity_fields) + (canonical_json(item),)

    return sorted(items, key=key)


def _required_configuration(config: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    testing = os.getenv("TRADING_AGENT_TESTING") == "1"
    config_hash = str(config.get("effective_config_hash") or "").strip()
    formulas = config.get("formula_versions")
    if testing and not config_hash:
        config_hash = "isolated-test-config"
    if testing and not isinstance(formulas, Mapping):
        formulas = dict(REQUIRED_FORMULA_VERSIONS)
    if not config_hash:
        raise RuntimeError("current effective configuration hash is missing")
    if not isinstance(formulas, Mapping):
        raise RuntimeError("current formula versions are missing")
    canonical = {str(key): str(formulas.get(key) or "") for key in sorted(REQUIRED_FORMULA_VERSIONS)}
    missing = [key for key, expected in REQUIRED_FORMULA_VERSIONS.items() if canonical.get(key) != expected]
    if missing:
        raise RuntimeError("current formula versions are missing or mismatched: " + ", ".join(sorted(missing)))
    return config_hash, canonical


def paper_account_identity_hash(identity: Any, account: Any) -> str:
    verified = bool(_value(identity, "verified", False))
    sandbox = bool(
        _value(
            identity,
            "sdk_sandbox_evidence",
            _value(identity, "sdk_sandbox", _value(identity, "sandbox", False)),
        )
    )
    mode = str(_value(identity, "mode", "paper") or "").lower()
    if not verified or not sandbox or mode != "paper":
        raise RuntimeError("paper account identity verification failed")
    account_identity = str(
        _value(identity, "account_id")
        or _value(identity, "id")
        or _value(account, "id")
        or ""
    ).strip()
    if not account_identity or account_identity == "paper-account":
        raise RuntimeError("stable paper account identity is missing")
    return hashlib.sha256(account_identity.encode("utf-8")).hexdigest()


def _synthetic_broker_evidence() -> tuple[dict[str, Any], dict[str, Any], list[Any], list[Any], dict[str, Any]]:
    identity = {
        "verified": True,
        "mode": "paper",
        "account_id": "isolated-test-account",
        "sdk_sandbox_evidence": True,
    }
    account = {
        "id": "isolated-test-account",
        "status": "ACTIVE",
        "equity": 100_000.0,
        "cash": 100_000.0,
        "buying_power": 100_000.0,
        "long_market_value": 0.0,
        "short_market_value": 0.0,
    }
    return identity, account, [], [], {"is_open": True, "timestamp": datetime.now(UTC).isoformat()}


def _broker_evidence(broker: Any) -> tuple[Any, Any, Any, Any, Any, bool]:
    required = ("paper_account_identity", "get_account", "get_positions", "get_open_orders", "get_clock")
    complete = broker is not None and all(callable(getattr(broker, name, None)) for name in required)
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        identity, account, positions, orders, clock = _synthetic_broker_evidence()
        identity_method = getattr(broker, "paper_account_identity", None) if broker is not None else None
        if callable(identity_method):
            try:
                candidate_identity = identity_method()
                if (
                    bool(_value(candidate_identity, "verified", False))
                    and bool(_value(candidate_identity, "sdk_sandbox_evidence", _value(candidate_identity, "sandbox", False)))
                    and str(_value(candidate_identity, "account_id", _value(candidate_identity, "id", "")) or "") not in {"", "paper-account"}
                ):
                    identity = candidate_identity
            except Exception:
                pass
        for method_name, target in (
            ("get_account", "account"), ("get_positions", "positions"),
            ("get_open_orders", "orders"), ("get_clock", "clock"),
        ):
            method = getattr(broker, method_name, None) if broker is not None else None
            if not callable(method):
                continue
            try:
                value = method()
            except Exception:
                continue
            if target == "account" and value is not None:
                value_status = str(_value(value, "status", "") or "").upper()
                try:
                    account_numbers = tuple(
                        float(_value(value, field)) for field in ("equity", "cash", "buying_power")
                    )
                except (TypeError, ValueError):
                    account_numbers = ()
                if (
                    value_status in {"ACTIVE", "ACCOUNT_STATUS.ACTIVE"}
                    and len(account_numbers) == 3
                    and all(math.isfinite(number) and number >= 0 for number in account_numbers)
                ):
                    account = value
            elif target == "positions" and value is not None:
                positions = value
            elif target == "orders" and value is not None:
                orders = value
            elif target == "clock" and value is not None:
                clock = value
        return identity, account, positions, orders, clock, not complete
    if complete:
        return (
            broker.paper_account_identity(),
            broker.get_account(),
            broker.get_positions(),
            broker.get_open_orders(),
            broker.get_clock(),
            False,
        )
    raise RuntimeError("authoritative paper broker risk snapshot is unavailable")


def execution_candidate_evidence(candidate: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "proposal_id", "id", "source_id", "candidate_id", "symbol", "side", "action",
        "intended_action", "approval_source_type", "execution_path", "request_basis",
        "position_lifecycle_id", "strategy_version", "relationship_type",
        "relationship_group_id", "rotation_group_id", "rotation_step_id",
        "emergency_exit_triggered", "emergency_exit_hard_trigger",
        "emergency_exit_trigger_reason", "emergency_exit_mode", "proposal_version",
        "approved_quantity_ceiling", "approved_notional_ceiling",
        "approved_stop_risk_ceiling", "qty", "quantity", "notional", "latest_price",
        "reference_price", "limit_price", "stop_price", "intended_stop_price",
        "stop_risk_dollars", "quote_bid", "quote_ask", "quote_timestamp",
        "quote_spread_bps", "order_type", "expires_at", "config_hash",
        "formula_versions", "strategy_registry_snapshot_id", "sleeve_allocation_id",
        "strategy_sleeve", "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling",
        "winner_expansion_decision_id", "pyramiding_milestone_id",
        "pyramiding_milestone_key", "management_mode", "pre_add_open_risk",
        "post_add_open_risk", "incremental_risk", "cluster_name", "phase4_mode",
        "strategy_state", "strategy_policy_version", "order_role", "trading_mode",
        "client_order_id", "display_envelope_id", "display_context_type",
        "display_context_id", "approval_authority_fingerprint", "displayed_fingerprint",
        "position_management_decision_type", "position_management_decision",
        "take_profit_level", "take_profit_target_quantity",
    )
    result = {field: _plain(candidate.get(field)) for field in fields if candidate.get(field) is not None}
    result["proposal_id"] = str(candidate.get("proposal_id") or candidate.get("id") or "")
    result["symbol"] = str(candidate.get("symbol") or "").upper()
    result["side"] = str(candidate.get("side") or "").lower()
    result["action"] = str(
        candidate.get("action")
        or candidate.get("intended_action")
        or ("exit" if result["side"] == "sell" else "entry")
    ).lower()
    return result


def _remaining_order_quantity(order: Mapping[str, Any]) -> float:
    qty = _value(
        order,
        "qty",
        _value(order, "quantity", _value(order, "requested_quantity", 0)),
    )
    filled = _value(order, "filled_qty", _value(order, "filled_quantity", 0))
    try:
        return max(0.0, float(qty or 0) - float(filled or 0))
    except (TypeError, ValueError):
        return 0.0


def _order_side(order: Mapping[str, Any]) -> str:
    value = str(_value(order, "side", "") or "").lower()
    return value.rsplit(".", 1)[-1]


def _position_quantity(position: Mapping[str, Any]) -> float:
    try:
        return max(0.0, float(_value(position, "qty", _value(position, "quantity", 0)) or 0))
    except (TypeError, ValueError):
        return 0.0


def _position_value(position: Mapping[str, Any]) -> float:
    for name in ("market_value", "notional"):
        value = _value(position, name)
        if value not in (None, ""):
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                pass
    quantity = _position_quantity(position)
    price = _value(position, "current_price", _value(position, "market_price", 0))
    try:
        return quantity * max(0.0, float(price or 0))
    except (TypeError, ValueError):
        return 0.0


def _measured(provider: Provider | None, *, default: Provider | None = None) -> bool:
    probe = provider or default
    if not callable(probe):
        return False
    try:
        value = probe()
        if hasattr(value, "connected"):
            value = value.connected
        return value is True
    except Exception:
        return False


def _loss_controls(
    broker: Any,
    providers: Mapping[str, Provider],
    *,
    account_equity: float,
    now: datetime,
) -> dict[str, Any]:
    probe = providers.get("loss_controls")
    if probe is None and broker is not None:
        candidate = getattr(broker, "get_loss_metrics", None)
        probe = candidate if callable(candidate) else None
    try:
        raw = _plain(probe()) if callable(probe) else {}
    except Exception:
        if os.getenv("TRADING_AGENT_TESTING") != "1":
            raise
        raw = {}
    if not raw and os.getenv("TRADING_AGENT_TESTING") == "1":
        raw = {
            "daily_loss_dollars": 0.0,
            "weekly_loss_dollars": 0.0,
            "daily_loss_confidence": "verified",
            "weekly_loss_confidence": "verified",
            "reference_equity": account_equity,
            "captured_at": now.isoformat(),
            "provenance": "isolated_test_provider",
        }
    if not isinstance(raw, Mapping):
        raise RuntimeError("trusted loss-control provider returned invalid evidence")
    as_of_value = raw.get("captured_at") or raw.get("as_of") or now.isoformat()
    try:
        as_of = _utc(as_of_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("trusted loss-control evidence timestamp is invalid") from exc
    age = (now - as_of).total_seconds()
    if age < -5 or age > LOSS_EVIDENCE_MAX_AGE_SECONDS:
        raise RuntimeError("trusted loss-control evidence is stale")
    metrics = build_loss_metrics(raw, account_equity=account_equity)
    context = metrics.as_context()
    context.update(
        evidence_as_of=as_of.isoformat(),
        evidence_age_seconds=age,
        authoritative=metrics.metrics_version == LOSS_METRICS_VERSION,
        raw_provenance=str(raw.get("provenance") or raw.get("loss_provenance") or "trusted_provider"),
    )
    return context


def _verified_context(
    *,
    conn: Any,
    candidate: Mapping[str, Any],
    account: Any,
    equity: float,
    cash: float,
    buying_power: float,
    positions: list[Mapping[str, Any]],
    broker_orders: list[Mapping[str, Any]],
    durable_intents: list[Mapping[str, Any]],
    reservations: list[Mapping[str, Any]],
    loss_controls: Mapping[str, Any],
    market_open: bool,
    health: Mapping[str, bool],
    kill_switch_active: bool,
    config_hash: str,
    formula_versions: Mapping[str, str],
    position_fingerprint: str,
    open_order_fingerprint: str,
    cluster_provider: Callable[[str], Any] | None,
    config: Mapping[str, Any],
    recovery_intent_id: str | None = None,
) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "").upper()
    side = str(candidate.get("side") or "").lower()
    action = str(candidate.get("action") or candidate.get("intended_action") or "entry").lower()
    is_entry = action in {"entry", "add"}

    known_order_keys: set[str] = set()
    active_orders: list[Mapping[str, Any]] = []
    for order in broker_orders:
        keys = {
            str(_value(order, field, "") or "")
            for field in ("client_order_id", "id", "broker_order_id")
            if _value(order, field, None)
        }
        known_order_keys.update(keys)
        active_orders.append(order)
    order_intents = [
        intent for intent in durable_intents
        if str(intent.get("id") or "") != str(recovery_intent_id or "")
    ]
    for intent in order_intents:
        keys = {
            str(intent.get(field) or "")
            for field in ("client_order_id", "id", "broker_order_id")
            if intent.get(field)
        }
        if keys & known_order_keys:
            continue
        known_order_keys.update(keys)
        active_orders.append(intent)

    symbol_orders = [order for order in active_orders if str(_value(order, "symbol", "")).upper() == symbol]
    conflicting_buy = any(_order_side(order) == "buy" for order in symbol_orders)
    conflicting_sell = any(_order_side(order) == "sell" for order in symbol_orders)
    open_sell_quantity = sum(
        _remaining_order_quantity(order)
        for order in symbol_orders
        if _order_side(order) == "sell"
    )
    holdings = sum(
        _position_quantity(position)
        for position in positions
        if str(_value(position, "symbol", "")).upper() == symbol
    )

    active_notional = sum(float(row.get("active_notional") or 0) for row in reservations)
    active_stop_risk = sum(float(row.get("active_stop_risk") or 0) for row in reservations)
    recovery_reservation = next(
        (
            row for row in reservations
            if str(row.get("intent_id") or "") == str(recovery_intent_id or "")
        ),
        None,
    )
    candidate_already_reserved = recovery_reservation is not None
    proposed_notional = float(candidate.get("notional") or 0)
    if proposed_notional <= 0 and candidate.get("qty") is not None:
        proposed_notional = float(candidate.get("qty") or 0) * max(
            float(candidate.get("latest_price") or 0),
            float(candidate.get("reference_price") or 0),
            float(candidate.get("limit_price") or 0),
        )
    position_values: dict[str, float] = {}
    for position in positions:
        key = str(_value(position, "symbol", "")).upper()
        if key:
            position_values[key] = position_values.get(key, 0.0) + _position_value(position)
    current_total = sum(position_values.values())
    candidate_increment = (
        proposed_notional
        if is_entry and side == "buy" and not candidate_already_reserved
        else 0.0
    )
    proposed_total = current_total + active_notional + candidate_increment
    symbol_reserved = sum(
        float(row.get("active_notional") or 0)
        for row in reservations
        if str(row.get("symbol") or "").upper() == symbol
    )
    proposed_symbol = position_values.get(symbol, 0.0) + symbol_reserved + (
        candidate_increment
    )

    def cluster(value: str) -> str:
        if callable(cluster_provider):
            try:
                result = str(cluster_provider(value) or "")
                if result:
                    return result
            except Exception:
                pass
        return str(candidate.get("cluster_name") or "unclassified") if value == symbol else "unclassified"

    target_cluster = cluster(symbol)
    cluster_symbols = {key for key in position_values if cluster(key) == target_cluster}
    cluster_value = sum(position_values[key] for key in cluster_symbols)
    cluster_value += sum(
        float(row.get("active_notional") or 0)
        for row in reservations
        if cluster(str(row.get("symbol") or "").upper()) == target_cluster
    )
    if is_entry and side == "buy" and not candidate_already_reserved:
        cluster_value += proposed_notional

    risk_budget = config.get("risk_budget", {}) or {}
    portfolio = config.get("portfolio_behavior", {}) or {}
    optimizer = config.get("portfolio_optimizer", {}) or {}
    latest_risk = conn.execute(
        "SELECT held_open_stop_risk,calculated_at FROM risk_snapshots_v2 ORDER BY calculated_at DESC,id DESC LIMIT 1"
    ).fetchone()
    held_open_risk = float(latest_risk["held_open_stop_risk"] or 0) if latest_risk else (0.0 if os.getenv("TRADING_AGENT_TESTING") == "1" else None)
    try:
        candidate_sizing = canonical_sizing(candidate)
        candidate_stop_risk = candidate_sizing.stop_risk
    except (TypeError, ValueError):
        candidate_stop_risk = 0.0
    total_ceiling = equity * float(portfolio.get("max_total_portfolio_exposure_pct", 6.0)) / 100.0
    symbol_ceiling = equity * float(portfolio.get("max_single_symbol_exposure_pct", 2.5)) / 100.0
    cluster_ceiling = equity * float(optimizer.get("max_same_cluster_exposure_pct", 5.0)) / 100.0
    open_risk_ceiling = equity * float(risk_budget.get("max_open_risk_pct", 0.30)) / 100.0
    reservation_limits = {
        "base_total_notional": current_total,
        "base_symbol_notional": position_values.get(symbol, 0.0),
        "base_cluster_notional": sum(position_values[key] for key in cluster_symbols),
        "total_notional_ceiling": total_ceiling,
        "symbol_notional_ceiling": symbol_ceiling,
        "cluster_notional_ceiling": cluster_ceiling,
        "base_open_risk": held_open_risk if held_open_risk is not None else open_risk_ceiling + max(candidate_stop_risk, 1.0),
        "open_risk_ceiling": open_risk_ceiling,
        "buying_power_ceiling": buying_power,
    }

    today = datetime.now(UTC).date().isoformat()
    submitted_states = ("submitted", "partially_filled", "filled")
    trades_today = int(conn.execute(
        "SELECT COUNT(*) FROM orders WHERE substr(created_at,1,10)=? AND lower(status) IN (?,?,?)",
        (today, *submitted_states),
    ).fetchone()[0])
    buy_trades_today = int(conn.execute(
        """SELECT COUNT(*) FROM orders
           WHERE lower(side)='buy' AND substr(created_at,1,10)=?
             AND lower(status) IN (?,?,?)""",
        (today, *submitted_states),
    ).fetchone()[0])
    exit_blocker = conn.execute(
        "SELECT symbol,state,recovery_classification,user_action_required FROM exit_blocker_states WHERE active=1 ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    pending_unknown = [row for row in order_intents if str(row.get("state")) in {"unknown", "reconciliation_required"} and str(row.get("side")).lower() == "buy"]
    short_value = _finite(_value(account, "short_market_value", 0) or 0, "short market value", nonnegative=False)
    uses_margin = cash < -1e-9 or abs(short_value) > 1e-9
    universe_row = conn.execute(
        """SELECT symbol,tier,universe_lane,alpaca_compatible,executable,observation_only
           FROM universe_symbols WHERE symbol=? ORDER BY updated_at DESC,id DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    active_dynamic_symbols = [
        str(row["symbol"]).upper()
        for row in conn.execute(
            """SELECT symbol FROM universe_symbols
               WHERE tier='paper_tradable' AND alpaca_compatible=1 AND executable=1
                 AND COALESCE(observation_only,0)=0 ORDER BY symbol"""
        ).fetchall()
    ]

    context = {
        "power_connected": bool(health["power"]),
        "internet_available": bool(health["internet"]),
        "database_writable": bool(health["database"]),
        "broker_available": bool(health["broker"]),
        "telegram_available": bool(health["telegram"]),
        "market_open": market_open,
        "kill_switch": kill_switch_active,
        "open_positions": len([position for position in positions if _position_quantity(position) > 0]),
        "trades_today": trades_today,
        "buy_trades_today": buy_trades_today,
        "conflicting_buy_order": conflicting_buy,
        "conflicting_sell_order": conflicting_sell,
        "open_sell_quantity": open_sell_quantity,
        "current_holdings_quantity": holdings,
        "sellable_quantity": max(0.0, holdings - open_sell_quantity),
        "same_symbol_position": holdings > 0,
        "uses_margin": uses_margin,
        "portfolio_equity": equity,
        "cash": cash,
        "buying_power": max(0.0, buying_power - active_notional),
        "active_reserved_exposure": active_notional,
        "active_reserved_stop_risk": active_stop_risk,
        "proposed_total_exposure_pct": proposed_total / equity * 100 if equity > 0 else float("inf"),
        "proposed_symbol_exposure_pct": proposed_symbol / equity * 100 if equity > 0 else float("inf"),
        "proposed_cluster_positions_count": len(cluster_symbols | ({symbol} if is_entry and side == "buy" else set())),
        "proposed_cluster_exposure_pct": cluster_value / equity * 100 if equity > 0 else float("inf"),
        "pending_buy_exposure_unknown": bool(pending_unknown),
        "pending_buy_exposure_unknown_reason": "unresolved broker BUY exposure" if pending_unknown else None,
        "pending_buy_exposure_unknown_rows": [str(row.get("id") or "") for row in pending_unknown],
        "exit_pending": bool(exit_blocker) or any(str(row.get("side")).lower() == "sell" for row in order_intents),
        "exit_pending_symbol": str(exit_blocker["symbol"]) if exit_blocker else None,
        "exit_pending_reason": str(exit_blocker["user_action_required"] or "") if exit_blocker else None,
        "exit_pending_status": str(exit_blocker["state"] or "") if exit_blocker else None,
        "exit_pending_stale": False,
        "position_fingerprint": position_fingerprint,
        "open_order_fingerprint": open_order_fingerprint,
        "effective_config_hash": config_hash,
        "formula_versions": dict(formula_versions),
        "reservation_limits": reservation_limits,
        "recovery_exclusion": {
            "intent_id": recovery_intent_id,
            "candidate_already_reserved": candidate_already_reserved,
            "excluded_from_order_conflicts_only": bool(recovery_intent_id),
        },
        "universe_symbol_info": dict(universe_row) if universe_row is not None else None,
        "active_dynamic_paper_tradable_symbols": active_dynamic_symbols,
        **dict(loss_controls),
    }
    return context


def _snapshot_body_from_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_version": values["snapshot_version"],
        "run_id": values.get("run_id"),
        "proposal_id": values["proposal_id"],
        "approval_id": values["approval_id"],
        "account_id_hash": values["account_id_hash"],
        "trading_mode": values["trading_mode"],
        "account_status": values["account_status"],
        "equity": float(values["equity"]),
        "cash": float(values["cash"]),
        "buying_power": float(values["buying_power"]),
        "positions": values["positions"],
        "position_fingerprint": values["position_fingerprint"],
        "open_orders": values["open_orders"],
        "open_order_fingerprint": values["open_order_fingerprint"],
        "active_reservations": values["active_reservations"],
        "durable_state": values["durable_state"],
        "durable_state_fingerprint": values["durable_state_fingerprint"],
        "loss_controls": values["loss_controls"],
        "kill_switch_active": bool(values["kill_switch_active"]),
        "market_clock": values["market_clock"],
        "market_open": bool(values["market_open"]),
        "data_health": values["data_health"],
        "control_evidence": values["control_evidence"],
        "control_evidence_fingerprint": values["control_evidence_fingerprint"],
        "config_hash": values["config_hash"],
        "formula_versions": values["formula_versions"],
        "risk_context": values["risk_context"],
        "risk_context_fingerprint": values["risk_context_fingerprint"],
        "execution_candidate": values["execution_candidate"],
        "execution_candidate_fingerprint": values["execution_candidate_fingerprint"],
        "captured_at": values["captured_at"],
        "expires_at": values["expires_at"],
    }


def _validated_recovery_exclusion(
    conn: Any,
    *,
    recovery_intent_id: str | None,
    recovery_logical_action_key: str | None,
    recovery_proven_no_invocation: bool,
    proposal_id: str,
    approval_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Authorize one exact pre-I/O intent exclusion, or fail closed.

    The intent remains in the durable snapshot and its active reservation
    remains fully counted.  Only its order-conflict/pending-order projection is
    excluded, preventing a recovery from conflicting with itself.
    """
    intent_id = str(recovery_intent_id or "").strip()
    action_key = str(recovery_logical_action_key or "").strip()
    if not intent_id and not action_key:
        return None, None
    if not intent_id or not action_key:
        raise RuntimeError("recovery exclusion authority is incomplete")
    row = conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
    if row is None:
        raise RuntimeError("recovery exclusion intent does not exist")
    intent = dict(row)
    if (
        str(intent.get("proposal_id") or "") != proposal_id
        or str(intent.get("approval_id") or "") != approval_id
        or str(intent.get("logical_action_key") or "") != action_key
    ):
        raise RuntimeError("recovery exclusion authority does not match proposal, approval, and logical action")
    state = str(intent.get("state") or "")
    invocation = int(intent.get("broker_invocation_occurred") or 0)
    permitted = state == "retryable_pre_submission" or (
        state == "submitting" and recovery_proven_no_invocation
    )
    if not permitted or invocation != 0:
        raise RuntimeError("recovery exclusion intent is not proven pre-invocation and retryable")
    if state in {"unknown", "reconciliation_required"}:
        raise RuntimeError("ambiguous intents can never receive recovery exclusion authority")
    matching = [
        dict(item) for item in conn.execute(
            "SELECT * FROM risk_reservations WHERE intent_id=? AND state='active'",
            (intent_id,),
        ).fetchall()
    ]
    if len(matching) != 1:
        raise RuntimeError("recovery exclusion requires exactly one active reservation")
    evidence = {
        "intent_id": intent_id,
        "logical_action_key": action_key,
        "proposal_id": proposal_id,
        "approval_id": approval_id,
        "state": state,
        "broker_invocation_occurred": invocation,
        "active_reservation_id": str(matching[0].get("id") or ""),
        "scope": "exact_intent_order_conflicts_and_pending_quantity_only",
    }
    return intent, evidence


def _json_column(row: Mapping[str, Any], name: str, *, items: bool = False) -> Any:
    try:
        parsed = json.loads(str(row.get(name) or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"risk snapshot {name} is invalid") from exc
    if items:
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("items"), list):
            raise RuntimeError(f"risk snapshot {name} is invalid")
        return parsed["items"]
    return parsed


def snapshot_body_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return _snapshot_body_from_values({
        **row,
        "positions": _json_column(row, "positions_json", items=True),
        "open_orders": _json_column(row, "open_orders_json", items=True),
        "active_reservations": _json_column(row, "active_reservations_json", items=True),
        "durable_state": _json_column(row, "durable_state_json"),
        "loss_controls": _json_column(row, "loss_controls_json"),
        "market_clock": _json_column(row, "market_clock_json"),
        "data_health": _json_column(row, "data_health_json"),
        "control_evidence": _json_column(row, "control_evidence_json"),
        "formula_versions": _json_column(row, "formula_versions_json"),
        "risk_context": _json_column(row, "risk_context_json"),
        "execution_candidate": _json_column(row, "execution_candidate_json"),
    })


def verify_execution_risk_snapshot(
    conn: Any,
    snapshot_id: str,
    *,
    proposal_id: str,
    approval_id: str,
    run_id: str | None,
    config_hash: str,
    formula_versions: Mapping[str, Any],
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = conn.execute("SELECT * FROM execution_risk_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise RuntimeError("authoritative risk snapshot is missing")
    row_dict = dict(row)
    if str(row_dict.get("snapshot_version") or "") != RISK_SNAPSHOT_VERSION:
        raise RuntimeError("authoritative risk snapshot version is invalid")
    body = snapshot_body_from_row(row_dict)
    fingerprint = _fingerprint(body)
    if fingerprint != str(row_dict.get("snapshot_fingerprint") or ""):
        raise RuntimeError("authoritative risk snapshot fingerprint is invalid")
    if str(row_dict.get("proposal_id") or "") != str(proposal_id):
        raise RuntimeError("risk snapshot belongs to a different proposal")
    if str(row_dict.get("approval_id") or "") != str(approval_id):
        raise RuntimeError("risk snapshot belongs to a different approval")
    if str(row_dict.get("run_id") or "") != str(run_id or ""):
        raise RuntimeError("risk snapshot belongs to a different run")
    if not str(row_dict.get("account_id_hash") or ""):
        raise RuntimeError("risk snapshot paper account identity is missing")
    if str(row_dict.get("config_hash") or "") != str(config_hash or ""):
        raise RuntimeError("risk snapshot configuration hash is stale")
    expected_formulas = {str(key): str(formula_versions.get(key) or "") for key in sorted(REQUIRED_FORMULA_VERSIONS)}
    if body["formula_versions"] != expected_formulas:
        raise RuntimeError("risk snapshot formula versions are stale")
    if int(row_dict.get("authoritative") or 0) != 1:
        raise RuntimeError("risk snapshot is not authoritative")
    if not bool(body["market_open"]):
        raise RuntimeError("risk snapshot does not prove an open market")
    if _fingerprint({"items": body["positions"]}) != str(row_dict.get("position_fingerprint") or ""):
        raise RuntimeError("risk snapshot position fingerprint is invalid")
    if _fingerprint({"items": body["open_orders"]}) != str(row_dict.get("open_order_fingerprint") or ""):
        raise RuntimeError("risk snapshot open-order fingerprint is invalid")
    if _fingerprint(body["durable_state"]) != str(row_dict.get("durable_state_fingerprint") or ""):
        raise RuntimeError("risk snapshot durable-state fingerprint is invalid")
    if _fingerprint(body["risk_context"]) != str(row_dict.get("risk_context_fingerprint") or ""):
        raise RuntimeError("risk snapshot context fingerprint is invalid")
    if _fingerprint(body["execution_candidate"]) != str(row_dict.get("execution_candidate_fingerprint") or ""):
        raise RuntimeError("risk snapshot candidate fingerprint is invalid")
    instant = now or datetime.now(UTC)
    try:
        captured_at = _utc(row_dict["captured_at"])
        expires_at = _utc(row_dict["expires_at"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("risk snapshot freshness timestamps are invalid") from exc
    if captured_at > instant + timedelta(seconds=5) or expires_at <= instant:
        raise RuntimeError("authoritative risk snapshot has expired")
    return row_dict, body


def capture_execution_risk_snapshot(
    storage: Any,
    broker: Any,
    *,
    proposal_id: str,
    approval_id: str,
    run_id: str | None,
    context: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    candidate: Mapping[str, Any] | None = None,
    trusted_providers: Mapping[str, Provider] | None = None,
    cluster_provider: Callable[[str], Any] | None = None,
    recovery_intent_id: str | None = None,
    recovery_logical_action_key: str | None = None,
    recovery_proven_no_invocation: bool = False,
    telegram_required: bool = True,
) -> dict[str, Any]:
    caller_hint_keys = sorted(str(key) for key in (context or {}).keys())
    # Caller data is intentionally non-authoritative and never merged.
    proposal_id = str(proposal_id or "").strip()
    approval_id = str(approval_id or "").strip()
    run_identity = str(run_id or "").strip()
    if not proposal_id or not approval_id or not run_identity:
        raise RuntimeError("risk snapshot requires proposal, approval, and run identity")
    config_hash, formula_versions = _required_configuration(config)
    providers = dict(trusted_providers or {})
    identity, account, raw_positions, raw_orders, clock, synthetic = _broker_evidence(broker)
    status = str(_value(account, "status", "") or "").upper()
    if status not in {"ACTIVE", "ACCOUNT_STATUS.ACTIVE"}:
        raise RuntimeError("paper account is not active")
    equity = _finite(_value(account, "equity"), "authoritative account equity")
    cash = _finite(_value(account, "cash"), "authoritative account cash")
    buying_power = _finite(_value(account, "buying_power", cash), "authoritative account buying power")
    account_id_hash = paper_account_identity_hash(identity, account)
    positions = _canonical_items(raw_positions, ("symbol", "asset_id", "id"))
    open_orders = _canonical_items(raw_orders, ("symbol", "side", "client_order_id", "id"))
    position_fingerprint = _fingerprint({"items": positions})
    open_order_fingerprint = _fingerprint({"items": open_orders})
    market_clock = _plain(clock)
    market_open = bool(_value(clock, "is_open", False))
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=RISK_SNAPSHOT_TTL_SECONDS)
    candidate_evidence = execution_candidate_evidence(candidate or {"proposal_id": proposal_id})
    if candidate_evidence["proposal_id"] != proposal_id:
        raise RuntimeError("risk snapshot candidate belongs to a different proposal")

    testing = os.getenv("TRADING_AGENT_TESTING") == "1"
    internet_probe = providers.get("internet") or (lambda: True if testing else internet_available())
    power_probe = providers.get("power") or (lambda: True if testing else get_power_status())
    telegram_probe = providers.get("telegram") or (lambda: True if testing else False)
    kill_switch_probe = providers.get("kill_switch")
    if kill_switch_probe is None:
        kill_switch_path = Path(PROJECT_ROOT) / "config" / "KILL_SWITCH"
        kill_switch_probe = kill_switch_path.exists
    kill_switch_active = bool(kill_switch_probe())
    loss_controls = _loss_controls(broker if not synthetic else None, providers, account_equity=equity, now=now)

    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _recovery_intent, recovery_exclusion = _validated_recovery_exclusion(
            conn,
            recovery_intent_id=recovery_intent_id,
            recovery_logical_action_key=recovery_logical_action_key,
            recovery_proven_no_invocation=recovery_proven_no_invocation,
            proposal_id=proposal_id,
            approval_id=approval_id,
        )
        database_probe = providers.get("database")
        database_ok = str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
        if database_probe is not None:
            database_ok = database_ok and _measured(database_probe)
        health = {
            "database": database_ok,
            "internet": _measured(internet_probe),
            "power": _measured(power_probe),
            "telegram": _measured(telegram_probe),
            "broker": not synthetic or testing,
        }
        required_health = {"database", "internet", "power", "broker"}
        if telegram_required:
            required_health.add("telegram")
        failed_health = sorted(key for key in required_health if not health[key])
        if failed_health:
            failed = ", ".join(failed_health)
            raise RuntimeError(f"trusted execution health evidence is not authoritative: {failed}")
        if kill_switch_active:
            raise RuntimeError("kill switch is active")
        placeholders = ",".join("?" for _ in ACTIVE_INTENT_STATES)
        durable_intents = [dict(row) for row in conn.execute(
            f"SELECT * FROM order_intents WHERE state IN ({placeholders}) ORDER BY created_at,id",
            ACTIVE_INTENT_STATES,
        ).fetchall()]
        reservations = [dict(row) for row in conn.execute(
            "SELECT * FROM risk_reservations WHERE state='active' ORDER BY created_at,id"
        ).fetchall()]
        durable_state = {
            "active_intents": durable_intents,
            "active_reservations": reservations,
            "transaction_mode": "BEGIN IMMEDIATE",
        }
        durable_state_fingerprint = _fingerprint(durable_state)
        risk_context = _verified_context(
            conn=conn,
            candidate=candidate_evidence,
            account=account,
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            positions=positions,
            broker_orders=open_orders,
            durable_intents=durable_intents,
            reservations=reservations,
            loss_controls=loss_controls,
            market_open=market_open,
            health=health,
            kill_switch_active=kill_switch_active,
            config_hash=config_hash,
            formula_versions=formula_versions,
            position_fingerprint=position_fingerprint,
            open_order_fingerprint=open_order_fingerprint,
            cluster_provider=cluster_provider,
            config=config,
            recovery_intent_id=(
                str(recovery_exclusion["intent_id"])
                if recovery_exclusion is not None
                else None
            ),
        )
        data_health = {"authoritative": True, "components": health, "measured_at": now.isoformat()}
        control_evidence = {
            "kill_switch_source": "durable_config_file",
            "kill_switch_active": kill_switch_active,
            "loss_controls": loss_controls,
            "caller_hint_keys_ignored": caller_hint_keys,
            "recovery_exclusion": recovery_exclusion,
            "telegram_required": telegram_required,
        }
        values = {
            "snapshot_version": RISK_SNAPSHOT_VERSION,
            "run_id": run_identity,
            "proposal_id": proposal_id,
            "approval_id": approval_id,
            "account_id_hash": account_id_hash,
            "trading_mode": "paper",
            "account_status": status,
            "equity": equity,
            "cash": cash,
            "buying_power": buying_power,
            "positions": positions,
            "position_fingerprint": position_fingerprint,
            "open_orders": open_orders,
            "open_order_fingerprint": open_order_fingerprint,
            "active_reservations": reservations,
            "durable_state": durable_state,
            "durable_state_fingerprint": durable_state_fingerprint,
            "loss_controls": loss_controls,
            "kill_switch_active": kill_switch_active,
            "market_clock": market_clock,
            "market_open": market_open,
            "data_health": data_health,
            "control_evidence": control_evidence,
            "control_evidence_fingerprint": _fingerprint(control_evidence),
            "config_hash": config_hash,
            "formula_versions": formula_versions,
            "risk_context": risk_context,
            "risk_context_fingerprint": _fingerprint(risk_context),
            "execution_candidate": candidate_evidence,
            "execution_candidate_fingerprint": _fingerprint(candidate_evidence),
            "captured_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        body = _snapshot_body_from_values(values)
        fingerprint = _fingerprint(body)
        snapshot_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO execution_risk_snapshots(
                   id,run_id,proposal_id,approval_id,account_id_hash,trading_mode,account_status,
                   equity,cash,buying_power,positions_json,open_orders_json,active_reservations_json,
                   loss_controls_json,kill_switch_active,market_clock_json,data_health_json,config_hash,
                   formula_versions_json,captured_at,expires_at,snapshot_fingerprint,authoritative,
                   snapshot_version,position_fingerprint,open_order_fingerprint,durable_state_json,
                   durable_state_fingerprint,risk_context_json,risk_context_fingerprint,
                   execution_candidate_json,execution_candidate_fingerprint,market_open,
                   control_evidence_json,control_evidence_fingerprint)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id, run_identity, proposal_id, approval_id, account_id_hash, "paper", status,
                equity, cash, buying_power, canonical_json({"items": positions}),
                canonical_json({"items": open_orders}), canonical_json({"items": reservations}),
                canonical_json(loss_controls), int(kill_switch_active), canonical_json(market_clock),
                canonical_json(data_health), config_hash, canonical_json(formula_versions),
                now.isoformat(), expires.isoformat(), fingerprint, RISK_SNAPSHOT_VERSION,
                position_fingerprint, open_order_fingerprint, canonical_json(durable_state),
                durable_state_fingerprint, canonical_json(risk_context), _fingerprint(risk_context),
                canonical_json(candidate_evidence), _fingerprint(candidate_evidence), int(market_open),
                canonical_json(control_evidence), _fingerprint(control_evidence),
            ),
        )
        row = dict(conn.execute("SELECT * FROM execution_risk_snapshots WHERE id=?", (snapshot_id,)).fetchone())
        verify_execution_risk_snapshot(
            conn,
            snapshot_id,
            proposal_id=proposal_id,
            approval_id=approval_id,
            run_id=run_identity,
            config_hash=config_hash,
            formula_versions=formula_versions,
            now=now,
        )
    return row


def verify_snapshot_immediately_before_broker(
    storage: Any,
    broker: Any,
    *,
    snapshot_id: str,
    intent_id: str,
    logical_action_key: str,
    proposal_id: str,
    approval_id: str,
    run_id: str | None,
    config: Mapping[str, Any],
    candidate: Mapping[str, Any],
    source_type: str,
    trusted_providers: Mapping[str, Provider] | None = None,
    cluster_provider: Callable[[str], Any] | None = None,
    telegram_required: bool = True,
) -> dict[str, Any]:
    """Refresh every critical control and durable authority before adapter I/O."""
    config_hash, formula_versions = _required_configuration(config)
    with storage.connect() as conn:
        prior_row, _prior_body = verify_execution_risk_snapshot(
            conn,
            snapshot_id,
            proposal_id=proposal_id,
            approval_id=approval_id,
            run_id=run_id,
            config_hash=config_hash,
            formula_versions=formula_versions,
        )
    if broker is None:
        raise RuntimeError("broker client unavailable before invocation")
    identity, account, _positions, _orders, _clock, _synthetic = _broker_evidence(broker)
    if paper_account_identity_hash(identity, account) != str(prior_row["account_id_hash"]):
        raise RuntimeError("paper account identity changed after risk capture")

    refreshed = capture_execution_risk_snapshot(
        storage,
        broker,
        proposal_id=proposal_id,
        approval_id=approval_id,
        run_id=run_id,
        context={},
        config=config,
        candidate=candidate,
        trusted_providers=trusted_providers,
        cluster_provider=cluster_provider,
        recovery_intent_id=intent_id,
        recovery_logical_action_key=logical_action_key,
        recovery_proven_no_invocation=True,
        telegram_required=telegram_required,
    )
    if str(refreshed.get("account_id_hash") or "") != str(prior_row["account_id_hash"]):
        raise RuntimeError("paper account identity changed during final risk refresh")

    # Re-read the complete local execution authority after the refreshed
    # broker/control snapshot.  This transaction closes reservation revocation,
    # approval supersession, and workflow mutation races before the invocation
    # marker can be written.
    from .approval_display import validate_consumed_display_authority

    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        intent_row = conn.execute("SELECT * FROM order_intents WHERE id=?", (intent_id,)).fetchone()
        if intent_row is None:
            raise RuntimeError("current execution intent disappeared before broker invocation")
        intent = dict(intent_row)
        if (
            str(intent.get("proposal_id") or "") != proposal_id
            or str(intent.get("approval_id") or "") != approval_id
            or str(intent.get("run_id") or "") != str(run_id or "")
            or str(intent.get("logical_action_key") or "") != logical_action_key
            or str(intent.get("state") or "") != "submitting"
            or int(intent.get("broker_invocation_occurred") or 0) != 0
        ):
            raise RuntimeError("current execution intent is no longer valid for pre-invocation submission")
        reservation_count = int(conn.execute(
            "SELECT COUNT(*) FROM risk_reservations WHERE intent_id=? AND state='active'",
            (intent_id,),
        ).fetchone()[0])
        if reservation_count != 1:
            raise RuntimeError("current execution reservation was revoked or duplicated")
        _proposal, approval, envelope = validate_consumed_display_authority(
            conn,
            approval_id=approval_id,
            proposal_id=proposal_id,
            source_type=source_type,
        )
        workflow = conn.execute(
            "SELECT * FROM approval_workflows WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if (
            workflow is None
            or str(workflow["intent_id"] or "") != intent_id
            or str(workflow["proposal_id"] or "") != proposal_id
            or str(workflow["state"] or "") != "submission_started"
        ):
            raise RuntimeError("approval workflow is no longer executable before broker invocation")
        if str(approval.get("displayed_fingerprint") or "") != str(intent.get("displayed_fingerprint") or ""):
            raise RuntimeError("display authority changed before broker invocation")
        if str(envelope.get("config_hash") or "") != config_hash:
            raise RuntimeError("configuration identity changed after approval")
        envelope_formulas = envelope.get("formula_versions")
        if not isinstance(envelope_formulas, Mapping) or {
            str(key): str(envelope_formulas.get(key) or "") for key in sorted(REQUIRED_FORMULA_VERSIONS)
        } != formula_versions:
            raise RuntimeError("formula identity changed after approval")
        conn.execute(
            "UPDATE order_intents SET risk_snapshot_id=?,updated_at=? WHERE id=?",
            (str(refreshed["id"]), datetime.now(UTC).isoformat(), intent_id),
        )
    return snapshot_body_from_row(refreshed)


__all__ = [
    "REQUIRED_FORMULA_VERSIONS",
    "RISK_SNAPSHOT_VERSION",
    "capture_execution_risk_snapshot",
    "execution_candidate_evidence",
    "paper_account_identity_hash",
    "snapshot_body_from_row",
    "verify_execution_risk_snapshot",
    "verify_snapshot_immediately_before_broker",
]
