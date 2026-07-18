"""Trusted portfolio-risk evidence for supervised Alpaca spot crypto.

This lane deliberately stops before proposal or execution authority.  It reads
the current paper account, positions, open orders, account loss evidence and
hourly crypto bars, then combines those sources with durable SQLite intents and
reservations in one ``BEGIN IMMEDIATE`` transaction.  The resulting immutable
snapshot constrains canonical Decimal sizing but can never submit an order.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .approval_authority import canonical_json
from .crypto_capabilities import CryptoCapabilityStore, CryptoCapabilitySnapshot
from .crypto_market_data import CryptoMarketDataStore, CryptoMarketEvidence
from .crypto_sizing import (
    CryptoSizingAuthority,
    CryptoSizingDecision,
    CryptoSizingError,
    CryptoSizingRequest,
    apply_crypto_sizing_schema,
    calculate_crypto_sizing,
    insert_crypto_sizing,
)
from .formula_versions import (
    CRYPTO_RISK_FORMULA_VERSION,
    CRYPTO_RISK_SCHEMA_VERSION,
    CRYPTO_SIZING_FORMULA_VERSION,
)
from .utils import iso_now, json_dumps


ZERO = Decimal("0")
ONE = Decimal("1")
PERCENT = Decimal("100")
ACTIVE_INTENT_STATES = frozenset(
    {
        "created", "reserved", "retryable_pre_submission", "submitting",
        "submitted", "partially_filled", "cancel_pending", "unknown",
        "reconciliation_required",
    }
)
AMBIGUOUS_INTENT_STATES = frozenset({"unknown", "reconciliation_required", "submitting"})
TERMINAL_BROKER_STATES = frozenset({"canceled", "cancelled", "expired", "filled", "rejected"})


class CryptoRiskError(RuntimeError):
    pass


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _decimal(value: Any, label: str, *, minimum: Decimal | None = None) -> Decimal:
    if value is None or isinstance(value, bool):
        raise CryptoRiskError(f"{label} is missing or invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoRiskError(f"{label} is missing or invalid") from exc
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise CryptoRiskError(f"{label} must be finite and at least {_text(minimum)}")
    return number


def _value(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(value, Mapping) and key in value:
            return value[key]
        if hasattr(value, key):
            return getattr(value, key)
    return default


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CryptoRiskError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _valid_hash(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _configured_symbols(config: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(value or "").strip().upper() for value in ((config.get("crypto") or {}).get("symbols") or ()))


def _normalize_crypto_symbol(value: Any, configured: Sequence[str]) -> str | None:
    raw = str(value or "").strip().upper().replace("-", "/")
    if raw in configured:
        return raw
    compact = raw.replace("/", "")
    return next((pair for pair in configured if pair.replace("/", "") == compact), None)


def _risk_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = config.get("crypto") or {}
    policy = cfg.get("risk_policy") or {}
    failures: list[str] = []
    if policy.get("mode") != "research_only":
        failures.append("risk_policy_mode_not_research_only")
    if policy.get("formula_version") != CRYPTO_RISK_FORMULA_VERSION:
        failures.append("risk_formula_identity_mismatch")
    if policy.get("schema_version") != CRYPTO_RISK_SCHEMA_VERSION:
        failures.append("risk_schema_identity_mismatch")
    formulas = config.get("formula_versions") or {}
    if formulas.get("crypto_risk") != CRYPTO_RISK_FORMULA_VERSION:
        failures.append("configuration_crypto_risk_formula_mismatch")
    if formulas.get("crypto_sizing") != CRYPTO_SIZING_FORMULA_VERSION:
        failures.append("configuration_crypto_sizing_formula_mismatch")
    decimal_names = (
        "maximum_total_portfolio_gross_exposure_pct_equity",
        "maximum_gross_exposure_pct_equity",
        "maximum_symbol_exposure_pct_equity",
        "maximum_cluster_exposure_pct_equity",
        "maximum_stop_risk_per_trade_pct_equity",
        "maximum_stop_heat_pct_equity",
        "minimum_cash_reserve_pct_equity",
        "daily_account_loss_halt_pct_equity",
        "weekly_account_loss_halt_pct_equity",
        "daily_crypto_loss_halt_pct_equity",
        "weekly_crypto_loss_halt_pct_equity",
        "drawdown_throttle_start_pct_equity",
        "drawdown_halt_pct_equity",
        "drawdown_throttle_multiplier",
        "volatility_throttle_annualized",
        "volatility_halt_annualized",
        "volatility_throttle_multiplier",
    )
    decimals: dict[str, Decimal] = {}
    for name in decimal_names:
        try:
            decimals[name] = _decimal(policy.get(name), f"crypto.risk_policy.{name}", minimum=ZERO)
        except CryptoRiskError:
            failures.append(f"invalid_{name}")
    integer_names = (
        "snapshot_ttl_seconds", "maximum_positions",
        "volatility_minimum_hourly_observations", "volatility_latest_bar_max_age_seconds",
    )
    integers: dict[str, int] = {}
    for name in integer_names:
        try:
            value = int(policy.get(name))
            if value <= 0:
                raise ValueError
            integers[name] = value
        except (TypeError, ValueError):
            failures.append(f"invalid_{name}")
    required_true = (
        "require_cash_funded", "require_verified_loss_evidence",
        "block_on_any_open_crypto_order",
    )
    for name in required_true:
        if policy.get(name) is not True:
            failures.append(f"{name}_must_be_true")
    if str(policy.get("loss_session_timezone") or "") != "UTC":
        failures.append("loss_session_timezone_must_be_utc")
    clusters = policy.get("correlation_clusters") or {}
    configured = _configured_symbols(config)
    if not isinstance(clusters, Mapping) or sorted(clusters.get("crypto_major") or ()) != sorted(configured):
        failures.append("initial_crypto_cluster_must_bind_all_configured_pairs")
    if decimals:
        gross = decimals.get("maximum_gross_exposure_pct_equity", ZERO)
        symbol = decimals.get("maximum_symbol_exposure_pct_equity", ZERO)
        cluster = decimals.get("maximum_cluster_exposure_pct_equity", ZERO)
        total = decimals.get("maximum_total_portfolio_gross_exposure_pct_equity", ZERO)
        if not (ZERO < symbol <= cluster <= gross <= total <= PERCENT):
            failures.append("crypto_exposure_limits_are_not_monotonic")
        trade_risk = decimals.get("maximum_stop_risk_per_trade_pct_equity", ZERO)
        heat = decimals.get("maximum_stop_heat_pct_equity", ZERO)
        if not (ZERO < trade_risk <= heat <= PERCENT):
            failures.append("crypto_stop_risk_limits_are_not_monotonic")
        if decimals.get("minimum_cash_reserve_pct_equity", ZERO) > PERCENT:
            failures.append("crypto_cash_reserve_exceeds_equity")
        if not (
            decimals.get("drawdown_throttle_start_pct_equity", ZERO)
            < decimals.get("drawdown_halt_pct_equity", ZERO)
            <= PERCENT
        ):
            failures.append("crypto_drawdown_thresholds_are_invalid")
        if not (
            ZERO < decimals.get("drawdown_throttle_multiplier", ZERO) <= ONE
            and ZERO < decimals.get("volatility_throttle_multiplier", ZERO) <= ONE
        ):
            failures.append("crypto_throttle_multipliers_are_invalid")
        if not (
            ZERO < decimals.get("volatility_throttle_annualized", ZERO)
            < decimals.get("volatility_halt_annualized", ZERO)
        ):
            failures.append("crypto_volatility_thresholds_are_invalid")
    if failures:
        raise CryptoRiskError("invalid crypto risk policy: " + ", ".join(sorted(set(failures))))
    return {**policy, **decimals, **integers}


def _account_evidence(broker: Any, failures: list[str]) -> dict[str, Any]:
    identity: Mapping[str, Any] = {}
    account: Any = None
    try:
        raw_identity = broker.paper_account_identity()
        identity = raw_identity if isinstance(raw_identity, Mapping) else {}
    except Exception as exc:
        failures.append(f"paper_account_identity_unavailable:{type(exc).__name__}")
    try:
        account = broker.get_account()
    except Exception as exc:
        failures.append(f"paper_account_unavailable:{type(exc).__name__}")
    account_id = str(_value(account, "id", "account_number", default="") or "").strip()
    recomputed_hash = hashlib.sha256(account_id.encode("utf-8")).hexdigest() if account_id else ""
    identity_hash = str(identity.get("account_id_hash") or "").strip().lower()
    status = _enum(_value(account, "status", default=identity.get("account_status")))
    currency = str(_value(account, "currency", default=identity.get("account_currency")) or "").upper()
    if identity.get("verified") is not True or identity.get("mode") != "paper" or identity.get("endpoint_class") != "paper":
        failures.append("paper_account_identity_not_verified")
    if not _valid_hash(identity_hash) or recomputed_hash != identity_hash:
        failures.append("paper_account_identity_hash_mismatch")
    if status != "active":
        failures.append("paper_account_not_active")
    if currency != "USD":
        failures.append("paper_account_currency_not_usd")
    if bool(_value(account, "account_blocked", default=False)) or bool(_value(account, "trading_blocked", default=False)):
        failures.append("paper_account_is_blocked")
    values: dict[str, str | None] = {}
    for key, aliases in {
        "equity": ("equity",),
        "cash": ("cash",),
        "non_marginable_buying_power": ("non_marginable_buying_power",),
    }.items():
        try:
            values[key] = _text(_decimal(_value(account, *aliases), f"account.{key}", minimum=ZERO))
        except CryptoRiskError:
            values[key] = None
            failures.append(f"paper_account_{key}_invalid")
    if values["equity"] in {None, "0"}:
        failures.append("paper_account_equity_not_positive")
    return {
        "paper_account_id_hash": identity_hash,
        "recomputed_account_id_hash": recomputed_hash,
        "status": status,
        "currency": currency,
        **values,
        "account_blocked": bool(_value(account, "account_blocked", default=False)),
        "trading_blocked": bool(_value(account, "trading_blocked", default=False)),
        "identity_verified": identity.get("verified") is True,
        "endpoint_class": identity.get("endpoint_class"),
    }


def _position_evidence(
    broker: Any,
    config: Mapping[str, Any],
    failures: list[str],
) -> list[dict[str, Any]]:
    try:
        raw_positions = list(broker.get_positions())
    except Exception as exc:
        failures.append(f"broker_positions_unavailable:{type(exc).__name__}")
        return []
    configured = _configured_symbols(config)
    positions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_positions):
        raw_symbol = str(_value(raw, "symbol", default="") or "").strip().upper()
        symbol = _normalize_crypto_symbol(raw_symbol, configured)
        asset_class = _enum(_value(raw, "asset_class", "class", default=""))
        label = f"broker_position_{index}"
        try:
            quantity = _decimal(_value(raw, "qty", "quantity"), f"{label}.quantity", minimum=ZERO)
            broker_market_value = _decimal(_value(raw, "market_value"), f"{label}.market_value", minimum=ZERO)
            current_price = _decimal(_value(raw, "current_price"), f"{label}.current_price", minimum=ZERO)
        except CryptoRiskError as exc:
            failures.append(str(exc).replace(" ", "_"))
            continue
        canonical = symbol or raw_symbol
        if not canonical:
            failures.append(f"{label}_symbol_missing")
            continue
        if canonical in seen:
            failures.append(f"duplicate_broker_position:{canonical}")
        seen.add(canonical)
        is_crypto = symbol is not None or asset_class == "crypto"
        if is_crypto and symbol is None:
            failures.append(f"unsupported_crypto_position:{raw_symbol}")
        if symbol is not None and asset_class not in {"crypto"}:
            failures.append(f"configured_crypto_position_wrong_asset_class:{symbol}")
        if _enum(_value(raw, "side", default="long")) not in {"", "long"}:
            failures.append(f"short_crypto_position_detected:{canonical}")
        calculated_market_value = quantity * current_price
        market_value = max(broker_market_value, calculated_market_value)
        reconciliation_tolerance = max(Decimal("0.01"), calculated_market_value * Decimal("0.01"))
        if abs(broker_market_value - calculated_market_value) > reconciliation_tolerance:
            failures.append(f"broker_position_value_reconciliation_mismatch:{canonical}")
        positions.append(
            {
                "raw_symbol": raw_symbol,
                "symbol": canonical,
                "asset_class": asset_class,
                "is_crypto": is_crypto,
                "quantity": _text(quantity),
                "market_value": _text(market_value),
                "broker_market_value": _text(broker_market_value),
                "calculated_market_value": _text(calculated_market_value),
                "current_price": _text(current_price),
                # Until the crypto position-management stage persists a tighter
                # stop, heat treats the full crypto market value as downside.
                "conservative_open_stop_risk": _text(market_value if is_crypto else ZERO),
            }
        )
    return sorted(positions, key=lambda item: (str(item["symbol"]), str(item["asset_class"])))


def _order_evidence(
    broker: Any,
    config: Mapping[str, Any],
    market: CryptoMarketEvidence,
    failures: list[str],
) -> list[dict[str, Any]]:
    try:
        raw_orders = list(broker.get_open_orders())
    except Exception as exc:
        failures.append(f"broker_open_orders_unavailable:{type(exc).__name__}")
        return []
    configured = _configured_symbols(config)
    orders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_orders):
        raw_symbol = str(_value(raw, "symbol", default="") or "").strip().upper()
        symbol = _normalize_crypto_symbol(raw_symbol, configured)
        status = _enum(_value(raw, "status", default=""))
        side = _enum(_value(raw, "side", default=""))
        client_order_id = str(_value(raw, "client_order_id", default="") or "").strip()
        broker_order_id = str(_value(raw, "id", "order_id", default="") or "").strip()
        identity = client_order_id or broker_order_id
        if not identity or identity in seen:
            failures.append("broker_open_order_identity_missing_or_duplicate")
        seen.add(identity)
        if status in TERMINAL_BROKER_STATES or not status:
            failures.append("broker_open_orders_response_contains_nonopen_state")
        if side not in {"buy", "sell"}:
            failures.append("broker_open_order_side_invalid")
        try:
            quantity_raw = _value(raw, "qty", "quantity", default=None)
            filled_raw = _value(raw, "filled_qty", "filled_quantity", default=None)
            quantity = ZERO if quantity_raw in (None, "") else _decimal(quantity_raw, "broker_order.quantity", minimum=ZERO)
            filled = ZERO if filled_raw in (None, "") else _decimal(filled_raw, "broker_order.filled_quantity", minimum=ZERO)
            notional_raw = _value(raw, "notional", default=None)
            notional = None if notional_raw in (None, "") else _decimal(notional_raw, "broker_order.notional", minimum=ZERO)
            limit_raw = _value(raw, "limit_price", default=None)
            limit_price = None if limit_raw in (None, "") else _decimal(limit_raw, "broker_order.limit_price", minimum=ZERO)
        except CryptoRiskError as exc:
            failures.append(str(exc).replace(" ", "_"))
            quantity = filled = ZERO
            notional = limit_price = None
        remaining_quantity = max(ZERO, quantity - filled)
        pending_notional = ZERO
        if side == "buy":
            if notional is not None and notional > ZERO:
                pending_notional = notional
            elif remaining_quantity > ZERO:
                price = limit_price
                if price is None and symbol == market.symbol:
                    price = _decimal(market.ask_price, "current market ask", minimum=ZERO)
                if price is None or price <= ZERO:
                    failures.append(f"broker_crypto_buy_order_exposure_unknown:{raw_symbol}")
                else:
                    pending_notional = remaining_quantity * price
        elif side == "sell" and symbol is not None and remaining_quantity <= ZERO:
            failures.append(f"broker_crypto_sell_order_quantity_unknown:{raw_symbol}")
        if symbol is None and _enum(_value(raw, "asset_class", default="")) == "crypto":
            failures.append(f"unsupported_crypto_open_order:{raw_symbol}")
        orders.append(
            {
                "broker_order_id": broker_order_id,
                "client_order_id": client_order_id,
                "raw_symbol": raw_symbol,
                "symbol": symbol or raw_symbol,
                "is_crypto": symbol is not None,
                "side": side,
                "status": status,
                "quantity": _text(quantity),
                "filled_quantity": _text(filled),
                "remaining_quantity": _text(remaining_quantity),
                "notional": _text(notional),
                "limit_price": _text(limit_price),
                "pending_buy_notional": _text(pending_notional),
            }
        )
    return sorted(orders, key=lambda item: (str(item["symbol"]), str(item["client_order_id"]), str(item["broker_order_id"])))


def _bar_rows(raw: Any, symbol: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    elif hasattr(raw, "reset_index") and hasattr(raw, "to_dict"):
        try:
            values = raw.reset_index().to_dict("records")
        except Exception:
            return []
    else:
        return []
    rows: list[dict[str, Any]] = []
    for item in values:
        row_symbol = str(_value(item, "symbol", default=symbol) or symbol).upper().replace("-", "/")
        if row_symbol.replace("/", "") != symbol.replace("/", ""):
            continue
        rows.append(
            {
                "timestamp": _value(item, "timestamp", "time", "t"),
                "close": _value(item, "close", "c"),
            }
        )
    return rows


def _volatility_evidence(
    broker: Any,
    symbol: str,
    policy: Mapping[str, Any],
    now: datetime,
    failures: list[str],
) -> dict[str, Any]:
    try:
        raw = broker.get_crypto_historical_bars(symbol, timeframe="1Hour", limit=169)
    except Exception as exc:
        failures.append(f"crypto_volatility_bars_unavailable:{type(exc).__name__}")
        return {"symbol": symbol, "observations": 0, "annualized_volatility": None, "latest_bar_at": None}
    parsed: list[tuple[datetime, Decimal]] = []
    for index, row in enumerate(_bar_rows(raw, symbol)):
        try:
            timestamp = _utc(row["timestamp"], f"crypto volatility bar {index}")
            close = _decimal(row["close"], f"crypto volatility close {index}", minimum=Decimal("0.000000001"))
            parsed.append((timestamp, close))
        except CryptoRiskError as exc:
            failures.append(str(exc).replace(" ", "_"))
    parsed.sort(key=lambda item: item[0])
    deduped: list[tuple[datetime, Decimal]] = []
    for item in parsed:
        if deduped and item[0] == deduped[-1][0]:
            failures.append("duplicate_crypto_volatility_bar_timestamp")
            continue
        deduped.append(item)
    returns = [deduped[index][1] / deduped[index - 1][1] - ONE for index in range(1, len(deduped))]
    minimum = int(policy["volatility_minimum_hourly_observations"])
    if len(returns) < minimum:
        failures.append("crypto_volatility_observations_insufficient")
    latest_at = deduped[-1][0] if deduped else None
    if latest_at is None:
        failures.append("crypto_volatility_latest_bar_missing")
    else:
        age = Decimal(str((now - latest_at).total_seconds()))
        if age < Decimal("-1"):
            failures.append("crypto_volatility_latest_bar_in_future")
        if age > Decimal(str(policy["volatility_latest_bar_max_age_seconds"])):
            failures.append("crypto_volatility_latest_bar_stale")
    annualized: Decimal | None = None
    if len(returns) >= 2:
        mean = sum(returns, ZERO) / Decimal(len(returns))
        variance = sum(((value - mean) ** 2 for value in returns), ZERO) / Decimal(len(returns) - 1)
        annualized = variance.sqrt() * Decimal("8760").sqrt()
    elif returns:
        failures.append("crypto_volatility_variance_unavailable")
    return {
        "symbol": symbol,
        "observations": len(returns),
        "annualized_volatility": _text(annualized),
        "latest_bar_at": latest_at.isoformat() if latest_at else None,
        "first_bar_at": deduped[0][0].isoformat() if deduped else None,
        "close_fingerprint": _hash([{"timestamp": item[0].isoformat(), "close": _text(item[1])} for item in deduped]),
        "formula": "sample_std_simple_hourly_returns_sqrt_8760_decimal",
    }


def _read_broker_loss_metrics(broker: Any, failures: list[str]) -> Mapping[str, Any]:
    try:
        raw = broker.get_loss_metrics()
        if isinstance(raw, Mapping):
            return dict(raw)
        failures.append("broker_loss_evidence_shape_invalid")
    except Exception as exc:
        failures.append(f"broker_loss_evidence_unavailable:{type(exc).__name__}")
    return {}


def _loss_evidence(metrics: Mapping[str, Any], conn: Any, now: datetime, failures: list[str], policy: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, str | None] = {}
    for name in ("daily_loss_dollars", "weekly_loss_dollars", "reference_equity"):
        raw = metrics.get(name)
        if raw is None:
            values[name] = None
            failures.append(f"broker_{name}_missing")
        else:
            try:
                values[name] = _text(_decimal(raw, f"broker.{name}", minimum=ZERO))
            except CryptoRiskError:
                values[name] = None
                failures.append(f"broker_{name}_invalid")
    if policy.get("require_verified_loss_evidence") is True:
        if metrics.get("daily_loss_confidence") != "verified":
            failures.append("daily_loss_evidence_not_verified")
        if metrics.get("weekly_loss_confidence") != "verified":
            failures.append("weekly_loss_evidence_not_verified")
    if str(metrics.get("metrics_version") or "") != "loss_controls_v2":
        failures.append("loss_evidence_formula_version_mismatch")
    day = now.date().isoformat()
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    rows = conn.execute(
        """SELECT symbol,trading_day,realized_pl,confidence FROM realized_pnl_events
           WHERE trading_day>=? AND trading_day<=?""",
        (week_start, day),
    ).fetchall()
    daily_crypto_pnl = ZERO
    weekly_crypto_pnl = ZERO
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        if "/" not in symbol and symbol not in {"BTCUSD", "ETHUSD"}:
            continue
        if str(row["confidence"] or "") != "verified":
            failures.append("crypto_realized_loss_evidence_not_verified")
        pnl = _decimal(row["realized_pl"] or ZERO, "durable crypto realized PnL")
        weekly_crypto_pnl += pnl
        if row["trading_day"] == day:
            daily_crypto_pnl += pnl
    daily_crypto = max(ZERO, -daily_crypto_pnl)
    weekly_crypto = max(ZERO, -weekly_crypto_pnl)
    return {
        **values,
        "daily_loss_confidence": metrics.get("daily_loss_confidence"),
        "weekly_loss_confidence": metrics.get("weekly_loss_confidence"),
        "provenance": metrics.get("provenance"),
        "metrics_version": metrics.get("metrics_version"),
        "captured_at": now.isoformat(),
        "loss_session_timezone": "UTC",
        "session_day": day,
        "session_week_start": week_start,
        "daily_crypto_realized_loss_dollars": _text(daily_crypto),
        "weekly_crypto_realized_loss_dollars": _text(weekly_crypto),
        "daily_crypto_realized_pnl_dollars": _text(daily_crypto_pnl),
        "weekly_crypto_realized_pnl_dollars": _text(weekly_crypto_pnl),
    }


def _failed_derived() -> dict[str, Any]:
    """Deterministic zero authority when source evidence cannot be derived."""

    return {
        "derivation_status": "failed_closed",
        "drawdown_pct_equity": None,
        "drawdown_multiplier": "0",
        "volatility_multiplier": "0",
        "combined_risk_multiplier": "0",
        "loss_halt": True,
        "notional_capacities": {},
        "stop_risk_capacities": {},
        "hard_notional_ceiling": "0",
        "hard_stop_risk_ceiling": "0",
        "checks": [
            {
                "name": "complete_risk_inputs",
                "passed": False,
                "reason": "crypto risk inputs are incomplete or malformed",
            }
        ],
    }


def _durable_evidence(conn: Any, config: Mapping[str, Any], broker_orders: Sequence[Mapping[str, Any]], failures: list[str]) -> dict[str, Any]:
    configured = _configured_symbols(config)
    rows = conn.execute(
        """SELECT i.id,i.symbol,i.side,i.state,i.client_order_id,i.requested_quantity,
                  i.filled_quantity,i.requested_notional,i.reserved_notional,
                  i.reserved_stop_risk,r.id reservation_id,r.state reservation_state,
                  r.active_notional,r.active_stop_risk
           FROM order_intents i
           LEFT JOIN risk_reservations r ON r.intent_id=i.id
           WHERE i.state IN ('created','reserved','retryable_pre_submission','submitting',
             'submitted','partially_filled','cancel_pending','unknown','reconciliation_required')"""
    ).fetchall()
    intents: list[dict[str, Any]] = []
    for row in rows:
        symbol = _normalize_crypto_symbol(row["symbol"], configured)
        if symbol is None:
            continue
        state = str(row["state"] or "")
        if state not in ACTIVE_INTENT_STATES:
            failures.append("crypto_durable_intent_state_invalid")
        if not row["reservation_id"] or row["reservation_state"] != "active":
            failures.append(f"active_crypto_intent_missing_active_reservation:{row['id']}")
        quantity = _decimal(row["requested_quantity"] or ZERO, "durable intent quantity", minimum=ZERO)
        filled = _decimal(row["filled_quantity"] or ZERO, "durable intent filled quantity", minimum=ZERO)
        active_notional = _decimal(row["active_notional"] or ZERO, "durable reservation notional", minimum=ZERO)
        active_stop = _decimal(row["active_stop_risk"] or ZERO, "durable reservation stop risk", minimum=ZERO)
        if state in AMBIGUOUS_INTENT_STATES:
            failures.append(f"ambiguous_crypto_intent_requires_reconciliation:{row['id']}")
        intents.append(
            {
                "intent_id": row["id"], "symbol": symbol,
                "side": str(row["side"] or "").lower(), "state": state,
                "client_order_id": str(row["client_order_id"] or ""),
                "remaining_quantity": _text(max(ZERO, quantity - filled)),
                "active_notional": _text(active_notional),
                "active_stop_risk": _text(active_stop),
                "reservation_id": row["reservation_id"],
            }
        )
    broker_client_ids = {str(order.get("client_order_id") or "") for order in broker_orders if order.get("client_order_id")}
    durable_client_ids = {str(intent["client_order_id"]) for intent in intents if intent["client_order_id"]}
    duplicate_links = sorted(broker_client_ids & durable_client_ids)
    return {
        "intents": sorted(intents, key=lambda item: str(item["intent_id"])),
        "broker_durable_duplicate_client_order_ids": duplicate_links,
    }


def _aggregate(
    *,
    symbol: str,
    positions: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    durable: Mapping[str, Any],
) -> dict[str, Decimal | int]:
    crypto_positions = [item for item in positions if item.get("is_crypto")]
    all_gross = sum((_decimal(item["market_value"], "position market value", minimum=ZERO) for item in positions), ZERO)
    crypto_gross = sum((_decimal(item["market_value"], "crypto market value", minimum=ZERO) for item in crypto_positions), ZERO)
    symbol_gross = sum((_decimal(item["market_value"], "symbol market value", minimum=ZERO) for item in crypto_positions if item["symbol"] == symbol), ZERO)
    position_quantity = sum((_decimal(item["quantity"], "position quantity", minimum=ZERO) for item in crypto_positions if item["symbol"] == symbol), ZERO)
    open_stop_risk = sum((_decimal(item["conservative_open_stop_risk"], "open stop risk", minimum=ZERO) for item in crypto_positions), ZERO)

    # Broker orders and matching durable reservations represent one economic
    # exposure.  Group by stable client identity and take the conservative
    # maximum for a duplicate, then add unrelated records independently.
    exposure_groups: dict[str, dict[str, Decimal]] = {}
    for index, order in enumerate(orders):
        key = str(order.get("client_order_id") or order.get("broker_order_id") or f"broker:{index}")
        group = exposure_groups.setdefault(key, {"buy": ZERO, "sell": ZERO, "risk": ZERO, "all_buy": ZERO})
        pending_buy = _decimal(order.get("pending_buy_notional") or ZERO, "broker pending buy", minimum=ZERO)
        group["all_buy"] = max(group["all_buy"], pending_buy)
        if order.get("is_crypto"):
            if order.get("side") == "buy":
                group["buy"] = max(group["buy"], pending_buy)
            elif order.get("side") == "sell" and order.get("symbol") == symbol:
                group["sell"] = max(group["sell"], _decimal(order.get("remaining_quantity") or ZERO, "broker pending sell", minimum=ZERO))
    for index, intent in enumerate(durable.get("intents") or ()):
        key = str(intent.get("client_order_id") or f"intent:{intent.get('intent_id') or index}")
        group = exposure_groups.setdefault(key, {"buy": ZERO, "sell": ZERO, "risk": ZERO, "all_buy": ZERO})
        if intent.get("side") == "buy":
            active = _decimal(intent.get("active_notional") or ZERO, "durable pending buy", minimum=ZERO)
            group["buy"] = max(group["buy"], active)
            group["all_buy"] = max(group["all_buy"], active)
            group["risk"] = max(group["risk"], _decimal(intent.get("active_stop_risk") or ZERO, "durable pending risk", minimum=ZERO))
        elif intent.get("side") == "sell" and intent.get("symbol") == symbol:
            group["sell"] = max(group["sell"], _decimal(intent.get("remaining_quantity") or ZERO, "durable pending sell", minimum=ZERO))
    pending_crypto_buy = sum((group["buy"] for group in exposure_groups.values()), ZERO)
    pending_all_buy = sum((group["all_buy"] for group in exposure_groups.values()), ZERO)
    pending_sell = sum((group["sell"] for group in exposure_groups.values()), ZERO)
    pending_stop_risk = sum((group["risk"] for group in exposure_groups.values()), ZERO)
    symbol_open_buy_count = len(
        [item for item in orders if item.get("is_crypto") and item.get("symbol") == symbol and item.get("side") == "buy"]
    )
    symbol_active_buy_intent_count = len(
        [item for item in (durable.get("intents") or ()) if item.get("symbol") == symbol and item.get("side") == "buy"]
    )
    return {
        "all_position_gross": all_gross,
        "crypto_position_gross": crypto_gross,
        "symbol_position_gross": symbol_gross,
        "position_quantity": position_quantity,
        "crypto_open_stop_risk": open_stop_risk,
        "pending_crypto_buy_notional": pending_crypto_buy,
        "pending_all_buy_notional": pending_all_buy,
        "pending_symbol_sell_quantity": pending_sell,
        "pending_crypto_stop_risk": pending_stop_risk,
        "crypto_position_count": len([item for item in crypto_positions if _decimal(item["quantity"], "position quantity") > ZERO]),
        "open_crypto_order_count": len([item for item in orders if item.get("is_crypto")]),
        "active_crypto_intent_count": len(durable.get("intents") or ()),
        "symbol_open_buy_order_count": symbol_open_buy_count,
        "symbol_active_buy_intent_count": symbol_active_buy_intent_count,
    }


def _peak_equity(conn: Any, equity: Decimal) -> tuple[Decimal, str]:
    values: list[tuple[Decimal, str]] = [(equity, "current_paper_account_equity")]
    for query, source in (
        ("SELECT MAX(equity) value FROM cash_snapshots WHERE equity IS NOT NULL", "durable_cash_snapshots"),
        ("SELECT MAX(peak_equity) value FROM account_equity_watermarks WHERE peak_equity IS NOT NULL", "durable_account_equity_watermarks"),
    ):
        row = conn.execute(query).fetchone()
        if row and row["value"] is not None:
            try:
                values.append((_decimal(row["value"], source, minimum=ZERO), source))
            except CryptoRiskError:
                pass
    return max(values, key=lambda item: item[0])


def _derive(
    *,
    config: Mapping[str, Any],
    request: Mapping[str, Any],
    account: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    loss: Mapping[str, Any],
    volatility: Mapping[str, Any],
    peak_equity: Decimal,
) -> dict[str, Any]:
    policy = _risk_policy(config)
    equity = _decimal(account["equity"], "account equity", minimum=Decimal("0.000000001"))
    cash = _decimal(account["cash"], "account cash", minimum=ZERO)
    buying_power = _decimal(account["non_marginable_buying_power"], "non-marginable buying power", minimum=ZERO)
    side = str(request.get("side") or "")
    action = str(request.get("action") or "")
    symbol_has_position = _decimal(aggregate["position_quantity"], "position quantity", minimum=ZERO) > ZERO
    drawdown = max(ZERO, (peak_equity - equity) / peak_equity * PERCENT) if peak_equity > ZERO else PERCENT
    volatility_value = _decimal(volatility.get("annualized_volatility"), "annualized volatility", minimum=ZERO)
    drawdown_multiplier = ONE
    if drawdown >= policy["drawdown_halt_pct_equity"]:
        drawdown_multiplier = ZERO
    elif drawdown >= policy["drawdown_throttle_start_pct_equity"]:
        drawdown_multiplier = policy["drawdown_throttle_multiplier"]
    volatility_multiplier = ONE
    if volatility_value >= policy["volatility_halt_annualized"]:
        volatility_multiplier = ZERO
    elif volatility_value >= policy["volatility_throttle_annualized"]:
        volatility_multiplier = policy["volatility_throttle_multiplier"]
    throttle = min(drawdown_multiplier, volatility_multiplier)

    daily_account = _decimal(loss["daily_loss_dollars"], "daily account loss", minimum=ZERO)
    weekly_account = _decimal(loss["weekly_loss_dollars"], "weekly account loss", minimum=ZERO)
    daily_crypto = _decimal(loss["daily_crypto_realized_loss_dollars"], "daily crypto loss", minimum=ZERO)
    weekly_crypto = _decimal(loss["weekly_crypto_realized_loss_dollars"], "weekly crypto loss", minimum=ZERO)
    loss_halt = any(
        (
            daily_account >= equity * policy["daily_account_loss_halt_pct_equity"] / PERCENT,
            weekly_account >= equity * policy["weekly_account_loss_halt_pct_equity"] / PERCENT,
            daily_crypto >= equity * policy["daily_crypto_loss_halt_pct_equity"] / PERCENT,
            weekly_crypto >= equity * policy["weekly_crypto_loss_halt_pct_equity"] / PERCENT,
        )
    )
    if loss_halt:
        throttle = ZERO

    pending_crypto = _decimal(aggregate["pending_crypto_buy_notional"], "pending crypto buy", minimum=ZERO)
    pending_all = _decimal(aggregate["pending_all_buy_notional"], "pending all buy", minimum=ZERO)
    crypto_gross = _decimal(aggregate["crypto_position_gross"], "crypto gross", minimum=ZERO)
    symbol_gross = _decimal(aggregate["symbol_position_gross"], "symbol gross", minimum=ZERO)
    all_gross = _decimal(aggregate["all_position_gross"], "portfolio gross", minimum=ZERO)
    open_heat = _decimal(aggregate["crypto_open_stop_risk"], "crypto open heat", minimum=ZERO)
    pending_heat = _decimal(aggregate["pending_crypto_stop_risk"], "pending crypto heat", minimum=ZERO)
    reserve = equity * policy["minimum_cash_reserve_pct_equity"] / PERCENT
    capacities = {
        "configured_order_notional": _decimal((config.get("crypto") or {}).get("sizing_policy", {}).get("maximum_order_notional_usd"), "configured maximum order notional", minimum=ZERO),
        "total_portfolio_gross": max(ZERO, equity * policy["maximum_total_portfolio_gross_exposure_pct_equity"] / PERCENT - all_gross - pending_all),
        "crypto_gross": max(ZERO, equity * policy["maximum_gross_exposure_pct_equity"] / PERCENT - crypto_gross - pending_crypto),
        "symbol_gross": max(ZERO, equity * policy["maximum_symbol_exposure_pct_equity"] / PERCENT - symbol_gross - pending_crypto),
        "cluster_gross": max(ZERO, equity * policy["maximum_cluster_exposure_pct_equity"] / PERCENT - crypto_gross - pending_crypto),
        "cash_after_reserve": max(ZERO, cash - reserve - pending_crypto),
        "non_marginable_buying_power": buying_power,
    }
    hard_notional = min(capacities.values()) * throttle
    if side == "buy" and not symbol_has_position and int(aggregate["crypto_position_count"]) >= int(policy["maximum_positions"]):
        hard_notional = ZERO
    if side == "buy" and action == "entry" and symbol_has_position:
        hard_notional = ZERO
    if side == "buy" and action == "add" and not symbol_has_position:
        hard_notional = ZERO
    if side == "buy" and action == "add" and (config.get("crypto") or {}).get("allow_add_to_winner") is not True:
        hard_notional = ZERO
    if side == "buy" and policy.get("block_on_any_open_crypto_order") is True and int(aggregate["open_crypto_order_count"]) > 0:
        hard_notional = ZERO
    if side == "buy" and int(aggregate["active_crypto_intent_count"]) > 0:
        hard_notional = ZERO
    stop_capacities = {
        "per_trade_stop_risk": equity * policy["maximum_stop_risk_per_trade_pct_equity"] / PERCENT * throttle,
        "portfolio_stop_heat": max(ZERO, equity * policy["maximum_stop_heat_pct_equity"] / PERCENT - open_heat - pending_heat) * throttle,
    }
    hard_stop_risk = min(stop_capacities.values())
    checks = [
        {"name": "paper_account", "passed": account.get("identity_verified") is True and account.get("status") == "active" and account.get("currency") == "USD", "reason": "stable active USD Alpaca paper identity is required"},
        {"name": "long_only", "passed": all(_decimal(item, "position quantity", minimum=ZERO) >= ZERO for item in [aggregate["position_quantity"]]), "reason": "crypto lane is long-only"},
        {"name": "cash_funded", "passed": side == "sell" or (cash >= reserve and buying_power >= ZERO), "reason": "risk-increasing crypto exposure must be cash funded after reserve"},
        {"name": "daily_account_loss", "passed": side == "sell" or daily_account < equity * policy["daily_account_loss_halt_pct_equity"] / PERCENT, "reason": "daily account loss halts risk increases but not a validated reduction"},
        {"name": "weekly_account_loss", "passed": side == "sell" or weekly_account < equity * policy["weekly_account_loss_halt_pct_equity"] / PERCENT, "reason": "weekly account loss halts risk increases but not a validated reduction"},
        {"name": "daily_crypto_loss", "passed": side == "sell" or daily_crypto < equity * policy["daily_crypto_loss_halt_pct_equity"] / PERCENT, "reason": "daily crypto loss halts risk increases but not a validated reduction"},
        {"name": "weekly_crypto_loss", "passed": side == "sell" or weekly_crypto < equity * policy["weekly_crypto_loss_halt_pct_equity"] / PERCENT, "reason": "weekly crypto loss halts risk increases but not a validated reduction"},
        {"name": "drawdown", "passed": side == "sell" or drawdown < policy["drawdown_halt_pct_equity"], "reason": "account drawdown halts risk increases but not a validated reduction"},
        {"name": "volatility", "passed": side == "sell" or volatility_value < policy["volatility_halt_annualized"], "reason": "extreme crypto volatility halts risk increases but not a validated reduction"},
        {"name": "open_crypto_orders", "passed": (int(aggregate["open_crypto_order_count"]) == 0 if side == "buy" else int(aggregate["symbol_open_buy_order_count"]) == 0), "reason": "crypto order conflicts must be absent; pending SELL quantity is reserved exactly"},
        {"name": "durable_crypto_intents", "passed": (int(aggregate["active_crypto_intent_count"]) == 0 if side == "buy" else int(aggregate["symbol_active_buy_intent_count"]) == 0), "reason": "crypto durable-intent conflicts must be absent"},
        {"name": "maximum_positions", "passed": side == "sell" or symbol_has_position or int(aggregate["crypto_position_count"]) < int(policy["maximum_positions"]), "reason": "crypto maximum-position ceiling"},
        {"name": "entry_position_state", "passed": side != "buy" or action != "entry" or not symbol_has_position, "reason": "crypto entry requires no existing same-symbol position"},
        {"name": "add_position_state", "passed": side != "buy" or action != "add" or symbol_has_position, "reason": "crypto ADD requires an existing same-symbol position"},
        {"name": "add_policy", "passed": action != "add" or (config.get("crypto") or {}).get("allow_add_to_winner") is True, "reason": "crypto ADDs remain disabled in this stage"},
        {"name": "notional_capacity", "passed": side == "sell" or hard_notional > ZERO, "reason": "positive portfolio, sleeve, symbol, cluster, cash and buying-power capacity is required"},
        {"name": "stop_risk_capacity", "passed": side == "sell" or hard_stop_risk > ZERO, "reason": "positive per-trade and portfolio stop-risk capacity is required"},
    ]
    return {
        "drawdown_pct_equity": _text(drawdown),
        "drawdown_multiplier": _text(drawdown_multiplier),
        "volatility_multiplier": _text(volatility_multiplier),
        "combined_risk_multiplier": _text(throttle),
        "loss_halt": loss_halt,
        "notional_capacities": {key: _text(value) for key, value in sorted(capacities.items())},
        "stop_risk_capacities": {key: _text(value) for key, value in sorted(stop_capacities.items())},
        "hard_notional_ceiling": _text(hard_notional),
        "hard_stop_risk_ceiling": _text(hard_stop_risk),
        "checks": checks,
    }


@dataclass(frozen=True)
class CryptoRiskEvaluation:
    snapshot_id: str
    snapshot_fingerprint: str
    sizing: CryptoSizingDecision
    decision_id: str
    risk_eligible: bool
    execution_authorized: bool
    reasons: tuple[str, ...]
    captured_at: str
    expires_at: str


def apply_crypto_risk_schema(conn: Any, *, record_migration: bool = True) -> None:
    apply_crypto_sizing_schema(conn, record_migration=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_risk_snapshots(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          request_fingerprint TEXT NOT NULL,
          capability_snapshot_id TEXT NOT NULL,
          capability_snapshot_fingerprint TEXT NOT NULL,
          market_evidence_id TEXT NOT NULL,
          market_evidence_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,
          paper_account_id_hash TEXT NOT NULL,
          account_json TEXT NOT NULL,
          account_fingerprint TEXT NOT NULL,
          positions_json TEXT NOT NULL,
          positions_fingerprint TEXT NOT NULL,
          open_orders_json TEXT NOT NULL,
          open_orders_fingerprint TEXT NOT NULL,
          durable_state_json TEXT NOT NULL,
          durable_state_fingerprint TEXT NOT NULL,
          loss_evidence_json TEXT NOT NULL,
          loss_evidence_fingerprint TEXT NOT NULL,
          volatility_evidence_json TEXT NOT NULL,
          volatility_evidence_fingerprint TEXT NOT NULL,
          aggregate_json TEXT NOT NULL,
          derived_authority_json TEXT NOT NULL,
          authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
          failure_reasons_json TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          snapshot_json TEXT NOT NULL,
          snapshot_fingerprint TEXT NOT NULL UNIQUE,
          FOREIGN KEY(capability_snapshot_id) REFERENCES crypto_capability_snapshots(id),
          FOREIGN KEY(market_evidence_id) REFERENCES crypto_market_data_evidence(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_risk_decisions(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          snapshot_id TEXT NOT NULL UNIQUE,
          snapshot_fingerprint TEXT NOT NULL,
          sizing_decision_id TEXT NOT NULL UNIQUE,
          sizing_fingerprint TEXT NOT NULL,
          risk_eligible INTEGER NOT NULL CHECK(risk_eligible IN (0,1)),
          execution_authorized INTEGER NOT NULL CHECK(execution_authorized=0),
          checks_json TEXT NOT NULL,
          reasons_json TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          decision_json TEXT NOT NULL,
          decision_fingerprint TEXT NOT NULL UNIQUE,
          FOREIGN KEY(snapshot_id) REFERENCES crypto_risk_snapshots(id),
          FOREIGN KEY(sizing_decision_id) REFERENCES crypto_sizing_decisions(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_risk_symbol_time ON crypto_risk_snapshots(symbol,captured_at)")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                CRYPTO_RISK_SCHEMA_VERSION,
                iso_now(),
                "authoritative Alpaca paper crypto account, positions, orders, loss, volatility, durable exposure and Decimal portfolio risk",
            ),
        )


class CryptoRiskStore:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def evaluate(
        self,
        config: Mapping[str, Any],
        broker: Any,
        run_id: str,
        capability_snapshot_id: str,
        market_evidence_id: str,
        request: CryptoSizingRequest,
        *,
        now: datetime | None = None,
    ) -> CryptoRiskEvaluation:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        policy = _risk_policy(config)
        request_payload = request.payload()
        request_fingerprint = _hash(request_payload)
        failures: list[str] = []
        capability = CryptoCapabilityStore(self.storage).load_verified(
            capability_snapshot_id, config, now=current
        )
        market = CryptoMarketDataStore(self.storage).load_verified(market_evidence_id, config)
        if market.symbol != request_payload["symbol"]:
            raise CryptoRiskError("crypto risk request and market symbol differ")
        if market.capability_snapshot_id != capability.id:
            raise CryptoRiskError("crypto risk market/capability relationship differs")
        quote_at = _utc(market.quote_timestamp, "crypto quote")
        maximum_age = _decimal((config.get("crypto") or {}).get("max_price_age_seconds"), "crypto max price age", minimum=ZERO)
        if Decimal(str((current - quote_at).total_seconds())) > maximum_age:
            failures.append("crypto_market_evidence_expired_before_risk_evaluation")
        if not market.authoritative or not market.execution_eligible:
            failures.append("crypto_market_evidence_not_execution_eligible")

        account = _account_evidence(broker, failures)
        positions = _position_evidence(broker, config, failures)
        orders = _order_evidence(broker, config, market, failures)
        volatility = _volatility_evidence(broker, market.symbol, policy, current, failures)
        broker_loss_metrics = _read_broker_loss_metrics(broker, failures)
        if account.get("paper_account_id_hash") != capability.paper_account_id_hash:
            failures.append("risk_account_identity_differs_from_capability_snapshot")
        config_hash = str(config.get("effective_config_hash") or "")
        if not _valid_hash(config_hash):
            failures.append("configuration_hash_missing_or_invalid")

        snapshot_id = str(uuid.uuid4())
        sizing_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        expires_at = current + timedelta(seconds=int(policy["snapshot_ttl_seconds"]))
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            apply_crypto_risk_schema(conn, record_migration=False)
            capability_row = conn.execute(
                "SELECT snapshot_fingerprint,config_hash FROM crypto_capability_snapshots WHERE id=?",
                (capability.id,),
            ).fetchone()
            market_row = conn.execute(
                "SELECT evidence_fingerprint,capability_snapshot_id,config_hash FROM crypto_market_data_evidence WHERE id=?",
                (market.id,),
            ).fetchone()
            if (
                capability_row is None or market_row is None
                or capability_row["snapshot_fingerprint"] != capability.snapshot_fingerprint
                or market_row["evidence_fingerprint"] != market.evidence_fingerprint
                or market_row["capability_snapshot_id"] != capability.id
                or capability_row["config_hash"] != config_hash
                or market_row["config_hash"] != config_hash
            ):
                raise CryptoRiskError("crypto risk evidence changed before durable capture")
            durable = _durable_evidence(conn, config, orders, failures)
            loss = _loss_evidence(broker_loss_metrics, conn, current, failures, policy)
            aggregate = _aggregate(symbol=market.symbol, positions=positions, orders=orders, durable=durable)
            try:
                equity = _decimal(account.get("equity"), "account equity", minimum=Decimal("0.000000001"))
                peak, peak_source = _peak_equity(conn, equity)
                derived = _derive(
                    config=config, request=request_payload, account=account, aggregate=aggregate,
                    loss=loss, volatility=volatility, peak_equity=peak,
                )
            except CryptoRiskError as exc:
                failures.append("risk_derivation_failed:" + str(exc).replace(" ", "_"))
                peak, peak_source = ZERO, "unavailable_failed_closed"
                derived = _failed_derived()
            failures = sorted(set(failures))
            authoritative = not failures
            source = {
                "id": snapshot_id,
                "run_id": str(run_id),
                "request": request_payload,
                "request_fingerprint": request_fingerprint,
                "capability_snapshot_id": capability.id,
                "capability_snapshot_fingerprint": capability.snapshot_fingerprint,
                "market_evidence_id": market.id,
                "market_evidence_fingerprint": market.evidence_fingerprint,
                "symbol": market.symbol,
                "paper_account_id_hash": str(account.get("paper_account_id_hash") or ""),
                "account": account,
                "account_fingerprint": _hash(account),
                "positions": positions,
                "positions_fingerprint": _hash(positions),
                "open_orders": orders,
                "open_orders_fingerprint": _hash(orders),
                "durable_state": durable,
                "durable_state_fingerprint": _hash(durable),
                "loss_evidence": loss,
                "loss_evidence_fingerprint": _hash(loss),
                "volatility_evidence": volatility,
                "volatility_evidence_fingerprint": _hash(volatility),
                "aggregate": {key: _text(value) if isinstance(value, Decimal) else value for key, value in sorted(aggregate.items())},
                "peak_equity": _text(peak),
                "peak_equity_source": peak_source,
                "derived_authority": derived,
                "authoritative": authoritative,
                "failure_reasons": failures,
                "config_hash": config_hash,
                "formula_version": CRYPTO_RISK_FORMULA_VERSION,
                "schema_version": CRYPTO_RISK_SCHEMA_VERSION,
                "captured_at": current.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            snapshot_fingerprint = _hash(source)
            conn.execute(
                """INSERT INTO crypto_risk_snapshots(
                  id,run_id,request_fingerprint,capability_snapshot_id,capability_snapshot_fingerprint,
                  market_evidence_id,market_evidence_fingerprint,symbol,paper_account_id_hash,
                  account_json,account_fingerprint,positions_json,positions_fingerprint,
                  open_orders_json,open_orders_fingerprint,durable_state_json,durable_state_fingerprint,
                  loss_evidence_json,loss_evidence_fingerprint,volatility_evidence_json,
                  volatility_evidence_fingerprint,aggregate_json,derived_authority_json,
                  authoritative,failure_reasons_json,config_hash,formula_version,schema_version,
                  captured_at,expires_at,snapshot_json,snapshot_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id, str(run_id), request_fingerprint, capability.id,
                    capability.snapshot_fingerprint, market.id, market.evidence_fingerprint,
                    market.symbol, source["paper_account_id_hash"], json_dumps(account),
                    source["account_fingerprint"], json_dumps(positions), source["positions_fingerprint"],
                    json_dumps(orders), source["open_orders_fingerprint"], json_dumps(durable),
                    source["durable_state_fingerprint"], json_dumps(loss), source["loss_evidence_fingerprint"],
                    json_dumps(volatility), source["volatility_evidence_fingerprint"],
                    json_dumps(source["aggregate"]), json_dumps(derived), int(authoritative),
                    json_dumps(failures), config_hash, CRYPTO_RISK_FORMULA_VERSION,
                    CRYPTO_RISK_SCHEMA_VERSION, current.isoformat(), expires_at.isoformat(),
                    json_dumps(source), snapshot_fingerprint,
                ),
            )
            authority = CryptoSizingAuthority(
                risk_snapshot_id=snapshot_id,
                risk_snapshot_fingerprint=snapshot_fingerprint,
                paper_account_id_hash=source["paper_account_id_hash"],
                config_hash=config_hash,
                hard_notional_ceiling=_decimal(derived["hard_notional_ceiling"], "hard notional ceiling", minimum=ZERO),
                hard_stop_risk_ceiling=_decimal(derived["hard_stop_risk_ceiling"], "hard stop-risk ceiling", minimum=ZERO),
                current_position_quantity=_decimal(source["aggregate"]["position_quantity"], "position quantity", minimum=ZERO),
                pending_sell_quantity=_decimal(source["aggregate"]["pending_symbol_sell_quantity"], "pending sell quantity", minimum=ZERO),
                authoritative=authoritative,
                failure_reasons=tuple(failures),
            )
            try:
                sizing = calculate_crypto_sizing(
                    decision_id=sizing_id, run_id=str(run_id), request=request,
                    authority=authority, capability=capability, market=market,
                    config=config, created_at=current.isoformat(),
                )
            except CryptoSizingError as exc:
                raise CryptoRiskError(f"canonical crypto sizing failed: {exc}") from exc
            insert_crypto_sizing(conn, sizing)
            checks = list(derived["checks"])
            checks.extend(
                [
                    {"name": "snapshot_authoritative", "passed": authoritative, "reason": "all broker, durable, loss and volatility evidence must be authoritative"},
                    {"name": "canonical_sizing", "passed": sizing.eligible and sizing.authoritative, "reason": "canonical Decimal sizing must pass every precision and capacity check"},
                    {"name": "research_only_boundary", "passed": (config.get("crypto") or {}).get("mode") == "research_only" and (config.get("crypto") or {}).get("paper_trading_enabled") is False and (config.get("crypto") or {}).get("proposals_enabled") is False, "reason": "this stage must remain incapable of proposals or execution"},
                ]
            )
            reasons = sorted(
                set(failures + list(sizing.blockers) + [str(check["reason"]) for check in checks if not check["passed"]])
            )
            risk_eligible = not reasons
            decision_payload = {
                "id": decision_id,
                "run_id": str(run_id),
                "snapshot_id": snapshot_id,
                "snapshot_fingerprint": snapshot_fingerprint,
                "sizing_decision_id": sizing.id,
                "sizing_fingerprint": sizing.decision_fingerprint,
                "risk_eligible": risk_eligible,
                "execution_authorized": False,
                "checks": checks,
                "reasons": reasons,
                "config_hash": config_hash,
                "formula_version": CRYPTO_RISK_FORMULA_VERSION,
                "schema_version": CRYPTO_RISK_SCHEMA_VERSION,
                "created_at": current.isoformat(),
            }
            decision_fingerprint = _hash(decision_payload)
            conn.execute(
                """INSERT INTO crypto_risk_decisions(
                  id,run_id,snapshot_id,snapshot_fingerprint,sizing_decision_id,
                  sizing_fingerprint,risk_eligible,execution_authorized,checks_json,
                  reasons_json,config_hash,formula_version,schema_version,created_at,
                  decision_json,decision_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, str(run_id), snapshot_id, snapshot_fingerprint,
                    sizing.id, sizing.decision_fingerprint, int(risk_eligible), 0,
                    json_dumps(checks), json_dumps(reasons), config_hash,
                    CRYPTO_RISK_FORMULA_VERSION, CRYPTO_RISK_SCHEMA_VERSION,
                    current.isoformat(), json_dumps(decision_payload), decision_fingerprint,
                ),
            )
        return CryptoRiskEvaluation(
            snapshot_id=snapshot_id, snapshot_fingerprint=snapshot_fingerprint,
            sizing=sizing, decision_id=decision_id, risk_eligible=risk_eligible,
            execution_authorized=False, reasons=tuple(reasons),
            captured_at=current.isoformat(), expires_at=expires_at.isoformat(),
        )

    def load_verified(
        self,
        snapshot_id: str,
        config: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        rows = self.storage.fetch_all("SELECT * FROM crypto_risk_snapshots WHERE id=?", (snapshot_id,))
        if len(rows) != 1:
            raise CryptoRiskError("crypto risk snapshot is missing or duplicated")
        row = dict(rows[0])
        try:
            payload = json.loads(row["snapshot_json"])
            failures = json.loads(row["failure_reasons_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoRiskError("crypto risk snapshot JSON is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(failures, list):
            raise CryptoRiskError("crypto risk snapshot shape is invalid")
        if _hash(payload) != row["snapshot_fingerprint"]:
            raise CryptoRiskError("crypto risk snapshot fingerprint mismatch")
        mappings = {
            "account": ("account_json", "account_fingerprint"),
            "positions": ("positions_json", "positions_fingerprint"),
            "open_orders": ("open_orders_json", "open_orders_fingerprint"),
            "durable_state": ("durable_state_json", "durable_state_fingerprint"),
            "loss_evidence": ("loss_evidence_json", "loss_evidence_fingerprint"),
            "volatility_evidence": ("volatility_evidence_json", "volatility_evidence_fingerprint"),
        }
        for key, (json_column, fingerprint_column) in mappings.items():
            try:
                column_value = json.loads(row[json_column])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CryptoRiskError(f"crypto risk {key} JSON is invalid") from exc
            if column_value != payload.get(key) or _hash(column_value) != row[fingerprint_column] or row[fingerprint_column] != payload.get(fingerprint_column):
                raise CryptoRiskError(f"crypto risk {key} binding mismatch")
        try:
            aggregate_column = json.loads(row["aggregate_json"])
            derived_column = json.loads(row["derived_authority_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoRiskError("crypto risk aggregate or derived JSON is invalid") from exc
        if aggregate_column != payload.get("aggregate") or derived_column != payload.get("derived_authority"):
            raise CryptoRiskError("crypto risk aggregate or derived column binding mismatch")
        recomputed_aggregate = _aggregate(
            symbol=str(payload.get("symbol") or ""),
            positions=payload.get("positions") or (),
            orders=payload.get("open_orders") or (),
            durable=payload.get("durable_state") or {},
        )
        recomputed_aggregate_payload = {
            key: _text(value) if isinstance(value, Decimal) else value
            for key, value in sorted(recomputed_aggregate.items())
        }
        if recomputed_aggregate_payload != aggregate_column:
            raise CryptoRiskError("crypto risk aggregate independent recomputation mismatch")
        if _hash(payload.get("request")) != row["request_fingerprint"]:
            raise CryptoRiskError("crypto risk request fingerprint mismatch")
        account_payload = payload.get("account") or {}
        if (
            not isinstance(account_payload, dict)
            or account_payload.get("paper_account_id_hash") != row["paper_account_id_hash"]
            or (bool(row["authoritative"]) and account_payload.get("recomputed_account_id_hash") != row["paper_account_id_hash"])
            or (bool(row["authoritative"]) and not _valid_hash(row["paper_account_id_hash"]))
        ):
            raise CryptoRiskError("crypto risk paper account evidence mismatch")
        scalar = (
            "id", "run_id", "request_fingerprint", "capability_snapshot_id",
            "capability_snapshot_fingerprint", "market_evidence_id",
            "market_evidence_fingerprint", "symbol", "paper_account_id_hash",
            "config_hash", "formula_version", "schema_version", "captured_at", "expires_at",
        )
        for key in scalar:
            if row[key] != payload.get(key):
                raise CryptoRiskError(f"crypto risk persisted column mismatch: {key}")
        if bool(row["authoritative"]) != payload.get("authoritative") or failures != payload.get("failure_reasons"):
            raise CryptoRiskError("crypto risk authority classification mismatch")
        if bool(row["authoritative"]) != (not failures):
            raise CryptoRiskError("crypto risk authority is inconsistent with failures")
        config_hash = str(config.get("effective_config_hash") or "")
        if row["config_hash"] != config_hash:
            raise CryptoRiskError("crypto risk configuration identity changed")
        _risk_policy(config)
        if row["formula_version"] != CRYPTO_RISK_FORMULA_VERSION or row["schema_version"] != CRYPTO_RISK_SCHEMA_VERSION:
            raise CryptoRiskError("crypto risk snapshot version is obsolete")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        captured = _utc(row["captured_at"], "crypto risk capture")
        expires = _utc(row["expires_at"], "crypto risk expiry")
        policy = _risk_policy(config)
        if expires - captured != timedelta(seconds=int(policy["snapshot_ttl_seconds"])):
            raise CryptoRiskError("crypto risk snapshot expiry policy mismatch")
        if current < captured - timedelta(seconds=1):
            raise CryptoRiskError("crypto risk snapshot capture is in the future")
        if current > expires:
            raise CryptoRiskError("crypto risk snapshot expired")
        # Recompute every derived cap and check from persisted source evidence.
        if (payload.get("derived_authority") or {}).get("derivation_status") == "failed_closed":
            if bool(row["authoritative"]):
                raise CryptoRiskError("authoritative crypto risk snapshot has failed derivation")
            derived = _failed_derived()
        else:
            derived = _derive(
                config=config,
                request=payload["request"],
                account=payload["account"],
                aggregate=payload["aggregate"],
                loss=payload["loss_evidence"],
                volatility=payload["volatility_evidence"],
                peak_equity=_decimal(payload["peak_equity"], "peak equity", minimum=Decimal("0.000000001")),
            )
        if derived != payload.get("derived_authority") or json.loads(row["derived_authority_json"]) != derived:
            raise CryptoRiskError("crypto risk derived authority mismatch")
        capability = self.storage.fetch_all(
            "SELECT snapshot_fingerprint,config_hash,paper_account_id_hash,authoritative FROM crypto_capability_snapshots WHERE id=?",
            (row["capability_snapshot_id"],),
        )
        market = self.storage.fetch_all(
            "SELECT evidence_fingerprint,capability_snapshot_id,config_hash,authoritative,execution_eligible FROM crypto_market_data_evidence WHERE id=?",
            (row["market_evidence_id"],),
        )
        if len(capability) != 1 or len(market) != 1 or (
            capability[0]["snapshot_fingerprint"] != row["capability_snapshot_fingerprint"]
            or (bool(row["authoritative"]) and capability[0]["paper_account_id_hash"] != row["paper_account_id_hash"])
            or (bool(row["authoritative"]) and not bool(capability[0]["authoritative"]))
            or market[0]["evidence_fingerprint"] != row["market_evidence_fingerprint"]
            or market[0]["capability_snapshot_id"] != row["capability_snapshot_id"]
            or (bool(row["authoritative"]) and (not bool(market[0]["authoritative"]) or not bool(market[0]["execution_eligible"])))
            or capability[0]["config_hash"] != config_hash
            or market[0]["config_hash"] != config_hash
        ):
            raise CryptoRiskError("crypto risk evidence relationship mismatch")
        return payload

    def load_verified_decision(
        self,
        decision_id: str,
        config: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        rows = self.storage.fetch_all("SELECT * FROM crypto_risk_decisions WHERE id=?", (decision_id,))
        if len(rows) != 1:
            raise CryptoRiskError("crypto risk decision is missing or duplicated")
        row = dict(rows[0])
        try:
            payload = json.loads(row["decision_json"])
            checks = json.loads(row["checks_json"])
            reasons = json.loads(row["reasons_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoRiskError("crypto risk decision JSON is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(checks, list) or not isinstance(reasons, list):
            raise CryptoRiskError("crypto risk decision shape is invalid")
        if _hash(payload) != row["decision_fingerprint"]:
            raise CryptoRiskError("crypto risk decision fingerprint mismatch")
        scalar = (
            "id", "run_id", "snapshot_id", "snapshot_fingerprint",
            "sizing_decision_id", "sizing_fingerprint", "config_hash",
            "formula_version", "schema_version", "created_at",
        )
        for key in scalar:
            if row[key] != payload.get(key):
                raise CryptoRiskError(f"crypto risk decision persisted column mismatch: {key}")
        if checks != payload.get("checks") or reasons != payload.get("reasons"):
            raise CryptoRiskError("crypto risk decision check/reason binding mismatch")
        expected_eligible = not reasons and all(check.get("passed") is True for check in checks)
        if bool(row["risk_eligible"]) != expected_eligible or payload.get("risk_eligible") is not expected_eligible:
            raise CryptoRiskError("crypto risk eligibility classification mismatch")
        if bool(row["execution_authorized"]) or payload.get("execution_authorized") is not False:
            raise CryptoRiskError("crypto risk decision unexpectedly authorizes execution")
        config_hash = str(config.get("effective_config_hash") or "")
        if row["config_hash"] != config_hash:
            raise CryptoRiskError("crypto risk decision configuration identity changed")
        if row["formula_version"] != CRYPTO_RISK_FORMULA_VERSION or row["schema_version"] != CRYPTO_RISK_SCHEMA_VERSION:
            raise CryptoRiskError("crypto risk decision version is obsolete")
        snapshot = self.load_verified(row["snapshot_id"], config, now=now)
        from .crypto_sizing import load_verified_crypto_sizing

        sizing = load_verified_crypto_sizing(self.storage, row["sizing_decision_id"], config)
        if (
            row["snapshot_fingerprint"] != _hash(snapshot)
            or row["sizing_fingerprint"] != sizing.decision_fingerprint
            or sizing.risk_snapshot_id != row["snapshot_id"]
            or sizing.risk_snapshot_fingerprint != row["snapshot_fingerprint"]
        ):
            raise CryptoRiskError("crypto risk decision evidence relationship mismatch")
        return payload


__all__ = [
    "CryptoRiskError",
    "CryptoRiskEvaluation",
    "CryptoRiskStore",
    "apply_crypto_risk_schema",
]
