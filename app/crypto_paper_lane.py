"""Bounded, supervised paper execution for the crypto evidence lane.

The research modules intentionally never create ordinary ``trade_proposals``
or call the equity order adapter.  This module is the separately gated stage
which turns a *verified* crypto strategy/risk/sizing chain into a durable,
manually approved paper order.  It is disabled unless
``crypto.supervised_paper_lane.enabled`` is explicitly enabled in a separately
reviewed configuration.

The lane has its own tables rather than masquerading a continuous crypto pair
as an equity.  Every write is bound to the persisted evidence fingerprints and
the displayed envelope.  Broker invocation is marked in SQLite before the
adapter call; failures before that mark are deterministic retryable states,
while failures after it are ambiguous and never automatically resubmitted.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .approval_authority import canonical_json
from .broker_interface import BrokerSubmissionNotAttempted
from .crypto_capabilities import CryptoCapabilityStore
from .crypto_market_data import CryptoMarketDataStore
from .crypto_risk import CryptoRiskStore
from .crypto_sizing import CryptoSizingRequest, load_verified_crypto_sizing
from .crypto_strategies import CryptoStrategyStore
from .formula_versions import (
    CRYPTO_CAPABILITY_FORMULA_VERSION,
    CRYPTO_MARKET_DATA_FORMULA_VERSION,
    CRYPTO_PAPER_EXECUTION_FORMULA_VERSION,
    CRYPTO_PAPER_EXECUTION_SCHEMA_VERSION,
    CRYPTO_PROPOSAL_FORMULA_VERSION,
    CRYPTO_RISK_FORMULA_VERSION,
    CRYPTO_SIZING_FORMULA_VERSION,
    CRYPTO_STRATEGY_FORMULA_VERSION,
)
from .utils import iso_now, json_dumps
from .utils import kill_switch_active


ZERO = Decimal("0")
ONE = Decimal("1")
ACTIVE_INTENT_STATES = {"reserved", "submitting", "submitted", "partially_filled", "retryable_pre_submission"}
TERMINAL_INTENT_STATES = {"filled", "cancelled", "rejected", "expired"}
AMBIGUOUS_INTENT_STATES = {"unknown", "reconciliation_required"}


class CryptoPaperLaneError(RuntimeError):
    """Raised when a crypto paper authority is missing or inconsistent."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> str:
    return _hash(value)


def _valid_hash(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _reservation_fingerprint(row: Mapping[str, Any]) -> str:
    """Fingerprint the complete mutable reservation authority envelope."""

    return _hash(
        {
            "id": str(row.get("id") or ""),
            "intent_id": str(row.get("intent_id") or ""),
            "symbol": str(row.get("symbol") or "").upper(),
            "initial_notional": str(row.get("initial_notional") or ""),
            "active_notional": str(row.get("active_notional") or ""),
            "initial_stop_risk": str(row.get("initial_stop_risk") or ""),
            "active_stop_risk": str(row.get("active_stop_risk") or ""),
            "state": str(row.get("state") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "released_at": row.get("released_at"),
            "release_reason": row.get("release_reason"),
        }
    )


def _crypto_lot_fingerprint(row: Mapping[str, Any]) -> str:
    """Fingerprint one crypto-lane FIFO lot, including its lifecycle binding."""

    return _hash(
        {
            "id": str(row.get("id") or ""),
            "symbol": str(row.get("symbol") or "").upper(),
            "source_fill_event_key": str(row.get("source_fill_event_key") or ""),
            "opened_at": str(row.get("opened_at") or ""),
            "original_quantity": str(row.get("original_quantity") or ""),
            "remaining_quantity": str(row.get("remaining_quantity") or ""),
            "unit_cost": str(row.get("unit_cost") or ""),
            "fees_allocated": str(row.get("fees_allocated") or ""),
            "created_at": str(row.get("created_at") or ""),
            "position_lifecycle_id": str(row.get("position_lifecycle_id") or ""),
        }
    )


def _crypto_pnl_fingerprint(row: Mapping[str, Any]) -> str:
    """Fingerprint the exact realised-P&L projection and its authority links."""

    return _hash(
        {
            "id": str(row.get("id") or ""),
            "broker_event_key": str(row.get("broker_event_key") or ""),
            "intent_id": str(row.get("intent_id") or ""),
            "position_lifecycle_id": str(row.get("position_lifecycle_id") or ""),
            "symbol": str(row.get("symbol") or "").upper(),
            "quantity": str(row.get("quantity") or ""),
            "gross_proceeds": str(row.get("gross_proceeds") or ""),
            "cost_basis": str(row.get("cost_basis") or ""),
            "fees": str(row.get("fees") or ""),
            "realized_pl": str(row.get("realized_pl") or ""),
            "occurred_at": str(row.get("occurred_at") or ""),
            "created_at": str(row.get("created_at") or ""),
            "evidence_fingerprint": str(row.get("evidence_fingerprint") or ""),
            "confidence": str(row.get("confidence") or ""),
        }
    )


def _decimal(value: Any, label: str, *, minimum: Decimal = ZERO, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CryptoPaperLaneError(f"{label} is missing or invalid")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoPaperLaneError(f"{label} is invalid") from exc
    if not number.is_finite() or number < minimum or (positive and number <= ZERO):
        raise CryptoPaperLaneError(f"{label} must be finite and nonnegative")
    return number


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CryptoPaperLaneError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise CryptoPaperLaneError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _value(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if not isinstance(value, Mapping) and hasattr(value, name):
            return getattr(value, name)
    return default


def _policy(config: Mapping[str, Any]) -> dict[str, Any]:
    crypto = config.get("crypto") or {}
    policy = crypto.get("supervised_paper_lane") or {}
    failures: list[str] = []
    if policy.get("enabled") is not True:
        failures.append("supervised_crypto_paper_lane_disabled")
    if policy.get("paper_only") is not True:
        failures.append("supervised_crypto_lane_not_paper_only")
    if policy.get("manual_approval_required") is not True:
        failures.append("supervised_crypto_lane_manual_approval_required")
    if policy.get("autonomous_execution") is not False:
        failures.append("supervised_crypto_autonomous_execution_must_be_false")
    if policy.get("live_enabled") is not False:
        failures.append("supervised_crypto_live_execution_must_be_false")
    if policy.get("execution_enabled") is not True:
        failures.append("supervised_crypto_paper_execution_not_enabled")
    if crypto.get("live_enabled") is not False or crypto.get("allow_margin") is not False or crypto.get("allow_shorting") is not False:
        failures.append("crypto_lane_global_safety_boundary_changed")
    if crypto.get("broker") != "alpaca_paper_spot" or crypto.get("market_profile") != "continuous_24_7":
        failures.append("crypto_lane_broker_or_market_profile_invalid")
    if str(policy.get("order_type") or "").lower() != "limit":
        failures.append("crypto_lane_only_limit_orders_are_supported")
    if str(policy.get("time_in_force") or "").lower() not in {"gtc", "ioc"}:
        failures.append("crypto_lane_time_in_force_invalid")
    try:
        expiry_minutes = int(policy.get("proposal_expiry_minutes"))
    except (TypeError, ValueError):
        expiry_minutes = 0
    if not 1 <= expiry_minutes <= 5:
        failures.append("crypto_lane_proposal_expiry_invalid")
    try:
        max_notional = _decimal(policy.get("maximum_order_notional_usd"), "crypto lane maximum order notional", positive=True)
        if max_notional > Decimal("50000"):
            failures.append("crypto_lane_order_notional_exceeds_operational_ceiling")
    except CryptoPaperLaneError:
        max_notional = ZERO
        failures.append("crypto_lane_maximum_order_notional_invalid")
    symbols = tuple(str(symbol or "").strip().upper() for symbol in crypto.get("symbols") or ())
    if symbols != ("BTC/USD", "ETH/USD"):
        failures.append("crypto_lane_symbols_must_be_btc_and_eth_usd")
    if failures:
        raise CryptoPaperLaneError("invalid supervised crypto paper policy: " + ", ".join(sorted(set(failures))))
    return {
        **policy,
        "expiry_minutes": expiry_minutes,
        "maximum_order_notional": max_notional,
        "time_in_force": str(policy.get("time_in_force") or "gtc").lower(),
    }


def apply_crypto_paper_lane_schema(conn: Any, *, record_migration: bool = True) -> None:
    """Create the isolated crypto paper execution ledger idempotently."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_proposals(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
          strategy_decision_id TEXT NOT NULL,strategy_decision_fingerprint TEXT NOT NULL,
          risk_decision_id TEXT NOT NULL,risk_decision_fingerprint TEXT NOT NULL,
          risk_snapshot_id TEXT NOT NULL,risk_snapshot_fingerprint TEXT NOT NULL,
          sizing_decision_id TEXT NOT NULL,sizing_decision_fingerprint TEXT NOT NULL,
          capability_snapshot_id TEXT NOT NULL,capability_snapshot_fingerprint TEXT NOT NULL,
          market_evidence_id TEXT NOT NULL,market_evidence_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,side TEXT NOT NULL CHECK(side IN ('buy','sell')),action TEXT NOT NULL,
          request_basis TEXT NOT NULL CHECK(request_basis IN ('quantity','notional')),
          quantity TEXT,notional TEXT,limit_price TEXT NOT NULL,stop_price TEXT,stop_risk TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','intent_created','submitted','partially_filled','filled','manual_review')),
          created_at TEXT NOT NULL,expires_at TEXT NOT NULL,config_hash TEXT NOT NULL,
          formula_versions_json TEXT NOT NULL,schema_version TEXT NOT NULL,
          display_json TEXT NOT NULL,display_fingerprint TEXT NOT NULL UNIQUE,
          proposal_json TEXT NOT NULL,proposal_fingerprint TEXT NOT NULL UNIQUE,telegram_message_id TEXT,
          telegram_chat_id TEXT,telegram_display_text TEXT,telegram_display_fingerprint TEXT,telegram_bound_at TEXT,
          UNIQUE(strategy_decision_id,risk_decision_id,sizing_decision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_approvals(
          id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL UNIQUE,sender_id TEXT NOT NULL,
          raw_message TEXT NOT NULL,reply_to_message_id TEXT,parsed_action TEXT NOT NULL CHECK(parsed_action='approve'),
          status TEXT NOT NULL CHECK(status IN ('active','consumed','rejected','expired')),
          approved_at TEXT NOT NULL,consumed_at TEXT,display_fingerprint TEXT NOT NULL,
          telegram_chat_id TEXT,telegram_message_fingerprint TEXT,
          approval_fingerprint TEXT NOT NULL UNIQUE,
          FOREIGN KEY(proposal_id) REFERENCES crypto_paper_proposals(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_reservations(
          id TEXT PRIMARY KEY,intent_id TEXT NOT NULL UNIQUE,symbol TEXT NOT NULL,
          initial_notional TEXT NOT NULL,active_notional TEXT NOT NULL,initial_stop_risk TEXT NOT NULL,
          active_stop_risk TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('active','released')),
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,released_at TEXT,release_reason TEXT,
          reservation_fingerprint TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_intents(
          id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL,approval_id TEXT NOT NULL,
          logical_action_key TEXT NOT NULL UNIQUE,client_order_id TEXT NOT NULL UNIQUE,
          symbol TEXT NOT NULL,side TEXT NOT NULL CHECK(side IN ('buy','sell')),request_basis TEXT NOT NULL,
          requested_quantity TEXT,requested_notional TEXT,limit_price TEXT NOT NULL,stop_price TEXT,
          reserved_notional TEXT NOT NULL,reserved_stop_risk TEXT NOT NULL,state TEXT NOT NULL,
          position_lifecycle_id TEXT,
          broker_invocation_occurred INTEGER NOT NULL DEFAULT 0 CHECK(broker_invocation_occurred IN (0,1)),
          submission_attempt_count INTEGER NOT NULL DEFAULT 0,broker_order_id TEXT,last_error TEXT,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,first_submission_at TEXT,terminal_at TEXT,
          FOREIGN KEY(proposal_id) REFERENCES crypto_paper_proposals(id),
          FOREIGN KEY(approval_id) REFERENCES crypto_paper_approvals(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_order_events(
          id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,event_key TEXT NOT NULL UNIQUE,
          from_state TEXT,to_state TEXT NOT NULL,event_type TEXT NOT NULL,safe_detail TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_fills(
          id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,broker_event_key TEXT NOT NULL UNIQUE,
          quantity TEXT NOT NULL,price TEXT NOT NULL,fees TEXT NOT NULL DEFAULT '0',
          occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL,
          broker_order_id TEXT,client_order_id TEXT,evidence_id TEXT,evidence_fingerprint TEXT,payload_fingerprint TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_lots(
          id TEXT PRIMARY KEY,symbol TEXT NOT NULL,source_fill_event_key TEXT NOT NULL UNIQUE,
          opened_at TEXT NOT NULL,original_quantity TEXT NOT NULL,remaining_quantity TEXT NOT NULL,
          unit_cost TEXT NOT NULL,fees_allocated TEXT NOT NULL DEFAULT '0',created_at TEXT NOT NULL,
          position_lifecycle_id TEXT,lot_fingerprint TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_realized_pnl(
          id TEXT PRIMARY KEY,broker_event_key TEXT NOT NULL UNIQUE,intent_id TEXT NOT NULL,
          symbol TEXT NOT NULL,quantity TEXT NOT NULL,gross_proceeds TEXT NOT NULL,
          cost_basis TEXT NOT NULL,fees TEXT NOT NULL,realized_pl TEXT NOT NULL,
          occurred_at TEXT NOT NULL,created_at TEXT NOT NULL,evidence_fingerprint TEXT,confidence TEXT,
          position_lifecycle_id TEXT,pnl_fingerprint TEXT
        )
        """
    )
    # The lane was introduced incrementally.  Keep upgrades explicit and
    # idempotent so an existing paper database cannot silently run the new
    # authority code against the legacy shape.
    additions = {
        "crypto_paper_proposals": {
            "telegram_chat_id": "TEXT",
            "telegram_display_text": "TEXT",
            "telegram_display_fingerprint": "TEXT",
            "telegram_bound_at": "TEXT",
        },
        "crypto_paper_approvals": {"telegram_chat_id": "TEXT", "telegram_message_fingerprint": "TEXT"},
        "crypto_paper_fills": {
            "broker_order_id": "TEXT",
            "client_order_id": "TEXT",
            "evidence_id": "TEXT",
            "evidence_fingerprint": "TEXT",
            "payload_fingerprint": "TEXT",
        },
        "crypto_paper_intents": {
            "position_lifecycle_id": "TEXT",
        },
        "crypto_paper_reservations": {
            "reservation_fingerprint": "TEXT",
        },
        "crypto_paper_lots": {
            "position_lifecycle_id": "TEXT",
            "lot_fingerprint": "TEXT",
        },
        "crypto_paper_realized_pnl": {
            "evidence_fingerprint": "TEXT",
            "confidence": "TEXT",
            "position_lifecycle_id": "TEXT",
            "pnl_fingerprint": "TEXT",
        },
    }
    for table, columns in additions.items():
        existing_columns = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, column_type in columns.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                existing_columns.add(column)
    # Existing paper rows predate the explicit lane fingerprints.  Backfill
    # them from their immutable text fields during the additive migration; all
    # subsequent state transitions refresh the same digest transactionally.
    for raw in conn.execute("SELECT * FROM crypto_paper_reservations").fetchall():
        row = dict(raw)
        if not row.get("reservation_fingerprint"):
            conn.execute(
                "UPDATE crypto_paper_reservations SET reservation_fingerprint=? WHERE id=?",
                (_reservation_fingerprint(row), row["id"]),
            )
    for raw in conn.execute("SELECT * FROM crypto_paper_lots").fetchall():
        row = dict(raw)
        if not row.get("lot_fingerprint"):
            conn.execute(
                "UPDATE crypto_paper_lots SET lot_fingerprint=? WHERE id=?",
                (_crypto_lot_fingerprint(row), row["id"]),
            )
    for raw in conn.execute("SELECT * FROM crypto_paper_realized_pnl").fetchall():
        row = dict(raw)
        if not row.get("pnl_fingerprint"):
            conn.execute(
                "UPDATE crypto_paper_realized_pnl SET pnl_fingerprint=? WHERE id=?",
                (_crypto_pnl_fingerprint(row), row["id"]),
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_order_evidence(
          id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,broker_order_id TEXT NOT NULL,
          client_order_id TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,
          status TEXT NOT NULL,requested_quantity TEXT,requested_notional TEXT,
          filled_quantity TEXT NOT NULL,filled_average_price TEXT,fees TEXT NOT NULL,
          payload TEXT NOT NULL,payload_fingerprint TEXT NOT NULL UNIQUE,captured_at TEXT NOT NULL,
          verified INTEGER NOT NULL CHECK(verified IN (0,1)),verification_error TEXT,
          FOREIGN KEY(intent_id) REFERENCES crypto_paper_intents(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_reconciliation_events(
          id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,event_type TEXT NOT NULL,
          broker_order_id TEXT,client_order_id TEXT,payload TEXT NOT NULL,
          payload_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
          UNIQUE(intent_id,event_type,payload_fingerprint),
          FOREIGN KEY(intent_id) REFERENCES crypto_paper_intents(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_rejections(
          id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL UNIQUE,sender_id TEXT NOT NULL,
          telegram_chat_id TEXT,
          raw_message TEXT NOT NULL,reply_to_message_id TEXT NOT NULL,
          telegram_message_fingerprint TEXT NOT NULL,rejection_fingerprint TEXT NOT NULL UNIQUE,
          rejected_at TEXT NOT NULL,
          FOREIGN KEY(proposal_id) REFERENCES crypto_paper_proposals(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_position_management(
          id TEXT PRIMARY KEY,symbol TEXT NOT NULL UNIQUE,quantity TEXT NOT NULL,
          average_entry_price TEXT NOT NULL,peak_price TEXT NOT NULL,stop_price TEXT,
          profit_target_price TEXT,time_stop_at TEXT,thesis_fingerprint TEXT,
          last_action TEXT,last_proposal_id TEXT,updated_at TEXT NOT NULL,created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_performance_links(
          id TEXT PRIMARY KEY,fill_id TEXT NOT NULL UNIQUE,intent_id TEXT NOT NULL,
          setup_id TEXT,outcome_id TEXT,broker_order_id TEXT NOT NULL,
          evidence_fingerprint TEXT NOT NULL,realized_pl TEXT,link_fingerprint TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          position_lifecycle_id TEXT,order_status TEXT,
          FOREIGN KEY(fill_id) REFERENCES crypto_paper_fills(id),
          FOREIGN KEY(intent_id) REFERENCES crypto_paper_intents(id)
        )
        """
    )
    performance_link_columns = {
        "side": "TEXT",
        "action": "TEXT",
        "quantity": "TEXT",
        "price": "TEXT",
        "fees": "TEXT",
        "fill_type": "TEXT",
        "position_lifecycle_id": "TEXT",
        "order_status": "TEXT",
    }
    existing_link_columns = {
        str(item[1])
        for item in conn.execute("PRAGMA table_info(crypto_performance_links)").fetchall()
    }
    for column, column_type in performance_link_columns.items():
        if column not in existing_link_columns:
            conn.execute(f"ALTER TABLE crypto_performance_links ADD COLUMN {column} {column_type}")
            existing_link_columns.add(column)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_proposals_status ON crypto_paper_proposals(status,expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_intents_state ON crypto_paper_intents(state,updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_evidence_intent ON crypto_paper_order_evidence(intent_id,captured_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_reconciliation_intent ON crypto_paper_reconciliation_events(intent_id,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_position_symbol ON crypto_paper_position_management(symbol)")
    # Evidence and fills are append-only.  Updates/deletes would destroy the
    # broker payload that justified a downstream accounting decision.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_paper_order_evidence_immutable_update
        BEFORE UPDATE ON crypto_paper_order_evidence
        BEGIN SELECT RAISE(ABORT,'crypto paper order evidence is immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_paper_order_evidence_immutable_delete
        BEFORE DELETE ON crypto_paper_order_evidence
        BEGIN SELECT RAISE(ABORT,'crypto paper order evidence is immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_paper_fills_immutable_update
        BEFORE UPDATE ON crypto_paper_fills
        BEGIN SELECT RAISE(ABORT,'crypto paper fills are immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_paper_fills_immutable_delete
        BEFORE DELETE ON crypto_paper_fills
        BEGIN SELECT RAISE(ABORT,'crypto paper fills are immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_paper_realized_pnl_immutable_update
        BEFORE UPDATE ON crypto_paper_realized_pnl
        BEGIN SELECT RAISE(ABORT,'crypto paper realised P&L is immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_paper_realized_pnl_immutable_delete
        BEFORE DELETE ON crypto_paper_realized_pnl
        BEGIN SELECT RAISE(ABORT,'crypto paper realised P&L is immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_performance_links_immutable_update
        BEFORE UPDATE ON crypto_performance_links
        BEGIN SELECT RAISE(ABORT,'crypto performance links are immutable'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_crypto_performance_links_immutable_delete
        BEFORE DELETE ON crypto_performance_links
        BEGIN SELECT RAISE(ABORT,'crypto performance links are immutable'); END
        """
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (CRYPTO_PAPER_EXECUTION_SCHEMA_VERSION, iso_now(), "isolated supervised manual crypto paper execution ledger"),
        )


@dataclass(frozen=True)
class CryptoPaperProposal:
    id: str
    run_id: str
    symbol: str
    side: str
    action: str
    request_basis: str
    quantity: str | None
    notional: str | None
    limit_price: str
    stop_price: str | None
    stop_risk: str
    status: str
    created_at: str
    expires_at: str
    display_fingerprint: str
    proposal_fingerprint: str
    display: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CryptoPaperIntent:
    id: str
    proposal_id: str
    approval_id: str
    logical_action_key: str
    client_order_id: str
    symbol: str
    request_basis: str
    requested_quantity: str | None
    requested_notional: str | None
    limit_price: str
    state: str
    broker_invocation_occurred: bool


def _formula_versions(config: Mapping[str, Any]) -> dict[str, str]:
    configured = config.get("formula_versions") or {}
    expected = {
        "crypto_capability": CRYPTO_CAPABILITY_FORMULA_VERSION,
        "crypto_market_data": CRYPTO_MARKET_DATA_FORMULA_VERSION,
        "crypto_sizing": CRYPTO_SIZING_FORMULA_VERSION,
        "crypto_risk": CRYPTO_RISK_FORMULA_VERSION,
        "crypto_strategy": CRYPTO_STRATEGY_FORMULA_VERSION,
        "crypto_proposal": CRYPTO_PROPOSAL_FORMULA_VERSION,
        "crypto_paper_execution": CRYPTO_PAPER_EXECUTION_FORMULA_VERSION,
        "crypto_paper_execution_schema": CRYPTO_PAPER_EXECUTION_SCHEMA_VERSION,
    }
    for key, value in expected.items():
        if str(configured.get(key) or "") != value:
            raise CryptoPaperLaneError(f"formula identity mismatch: {key}")
    return expected


def _display_from_authority(
    *, preview_id: str, strategy: Any, risk_decision: Mapping[str, Any], risk_snapshot: Mapping[str, Any],
    sizing: Any, market: Any, expires_at: datetime,
) -> dict[str, Any]:
    quantity = _decimal(sizing.canonical_quantity, "canonical quantity", positive=True)
    notional = _decimal(sizing.canonical_notional, "canonical notional", positive=True)
    limit_price = _decimal(sizing.limit_price, "canonical limit price", positive=True)
    stop_risk = _decimal(sizing.canonical_stop_risk, "canonical stop risk")
    stop_price = None if sizing.stop_price is None else _decimal(sizing.stop_price, "canonical stop price", positive=True)
    aggregate = risk_snapshot.get("aggregate") or {}
    account = risk_snapshot.get("account") or {}
    exposure = _decimal(aggregate.get("crypto_position_gross"), "existing crypto exposure")
    total_exposure = _decimal(aggregate.get("all_position_gross"), "existing total exposure")
    side = str(sizing.side or "").lower()
    action = str(sizing.action or "").lower()
    is_buy = side == "buy"
    # Expected reward is an explicit strategy-authority field, not a value
    # inferred from a caller's free-form message.  Exit/reduce proposals are
    # risk-reducing and therefore display a zero expected reward rather than
    # pretending that an entry target exists.
    expected_reward_r = ZERO
    raw_reward_r = getattr(strategy, "expected_reward_r", None)
    if is_buy and raw_reward_r is not None:
        expected_reward_r = _decimal(raw_reward_r, "expected reward R")
    expected_reward = stop_risk * expected_reward_r
    action_label = ("BUY" if is_buy else "SELL") + " " + action.upper()
    projected_exposure = exposure + notional if is_buy else max(ZERO, exposure - notional)
    projected_total_exposure = total_exposure + notional if is_buy else max(ZERO, total_exposure - notional)
    return {
        "header": "CRYPTO PAPER ORDER — MANUAL APPROVAL REQUIRED",
        "symbol": strategy.symbol,
        "strategy": strategy.selected_strategy,
        "strategy_lifecycle": strategy.lifecycle,
        "action": action_label,
        "side": side,
        "request_basis": sizing.request_basis,
        "quantity": _text(quantity),
        "notional_usd": _text(notional),
        "current_bid": market.bid_price,
        "current_ask": market.ask_price,
        "spread_bps": market.spread_bps,
        "annualized_volatility": (risk_snapshot.get("volatility_evidence") or {}).get("annualized_volatility"),
        "limit_price": _text(limit_price),
        "stop_price": _text(stop_price),
        "expected_reward_usd": _text(expected_reward),
        "maximum_loss_usd": _text(stop_risk),
        "expected_execution_cost_usd": sizing.estimated_fees,
        "existing_crypto_exposure_usd": _text(exposure),
        "projected_crypto_exposure_usd": _text(projected_exposure),
        "total_portfolio_exposure_usd": _text(total_exposure),
        "projected_total_portfolio_exposure_usd": _text(projected_total_exposure),
        "paper_account_equity_usd": account.get("equity"),
        "expires_at": expires_at.isoformat(),
        "approval_command": f"YES CRYPTO {preview_id}",
        "paper_only_warning": "PAPER ONLY • MANUAL APPROVAL REQUIRED • NO LIVE OR AUTONOMOUS EXECUTION",
    }


def format_crypto_paper_proposal(proposal: CryptoPaperProposal) -> str:
    d = proposal.display
    return (
        "🪙 CRYPTO PAPER ORDER — MANUAL APPROVAL REQUIRED\n\n"
        f"{d['action']} {d['symbol']} | {d['strategy']}\n"
        f"Basis: {d['request_basis']} | qty {d['quantity']} | ${d['notional_usd']}\n"
        f"Bid ${d['current_bid']} | ask ${d['current_ask']} | spread {d['spread_bps']} bps\n"
        f"Limit ${d['limit_price']} | stop ${d['stop_price']} | expected reward ${d['expected_reward_usd']} | max loss ${d['maximum_loss_usd']}\n"
        f"Expected execution cost ${d['expected_execution_cost_usd']} | volatility {d['annualized_volatility']}\n"
        f"Existing crypto exposure ${d['existing_crypto_exposure_usd']} → ${d['projected_crypto_exposure_usd']}\n"
        f"Total portfolio exposure ${d['total_portfolio_exposure_usd']} → ${d['projected_total_portfolio_exposure_usd']}\n"
        f"Expires {d['expires_at']}\n"
        f"Approve only by replying: {d['approval_command']}\n\n"
        f"{d['paper_only_warning']}"
    )


class CryptoPaperLaneStore:
    """Durable proposal, approval, intent and fill operations for crypto."""

    def __init__(self, storage: Any, *, control_providers: Mapping[str, Any] | None = None) -> None:
        self.storage = storage
        self.control_providers = dict(control_providers or {})
        self._last_final_risk_snapshot_id: str | None = None

    def _load_proposal_row(self, proposal_id: str) -> dict[str, Any]:
        rows = self.storage.fetch_all("SELECT * FROM crypto_paper_proposals WHERE id=?", (proposal_id,))
        if len(rows) != 1:
            raise CryptoPaperLaneError("crypto paper proposal is missing or duplicated")
        return dict(rows[0])

    def _verify_proposal_row(self, row: Mapping[str, Any], config: Mapping[str, Any], *, now: datetime | None = None) -> CryptoPaperProposal:
        _policy(config)
        try:
            payload = json.loads(row["proposal_json"])
            display = json.loads(row["display_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoPaperLaneError("crypto paper proposal JSON is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(display, dict):
            raise CryptoPaperLaneError("crypto paper proposal shape is invalid")
        if _hash(payload) != row["proposal_fingerprint"] or _hash(display) != row["display_fingerprint"]:
            raise CryptoPaperLaneError("crypto paper proposal fingerprint mismatch")
        if payload.get("display") != display or payload.get("display_fingerprint") != row["display_fingerprint"]:
            raise CryptoPaperLaneError("crypto paper proposal display binding mismatch")
        for key in ("id", "run_id", "symbol", "side", "action", "request_basis", "quantity", "notional", "limit_price", "stop_price", "stop_risk", "created_at", "expires_at", "config_hash", "schema_version"):
            if str(row[key]) != str(payload.get(key)):
                raise CryptoPaperLaneError(f"crypto paper proposal persisted column mismatch: {key}")
        if row["config_hash"] != str(config.get("effective_config_hash") or "") or not _valid_hash(row["config_hash"]):
            raise CryptoPaperLaneError("crypto paper proposal configuration identity changed")
        if row["schema_version"] != CRYPTO_PAPER_EXECUTION_SCHEMA_VERSION:
            raise CryptoPaperLaneError("crypto paper proposal schema version is obsolete")
        try:
            persisted_formulas = json.loads(row["formula_versions_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoPaperLaneError("crypto paper proposal formula identity is invalid") from exc
        if persisted_formulas != payload.get("formula_versions") or persisted_formulas != _formula_versions(config):
            raise CryptoPaperLaneError("crypto paper proposal formula identity changed")
        try:
            strategy = (
                CryptoStrategyStore(self.storage).load_verified(row["strategy_decision_id"], config)
                if str(row["strategy_decision_id"] or "")
                else None
            )
            risk_store = CryptoRiskStore(self.storage)
            risk_decision = risk_store.load_verified_decision(row["risk_decision_id"], config, now=now)
            risk_snapshot = risk_store.load_verified(row["risk_snapshot_id"], config, now=now)
            sizing = load_verified_crypto_sizing(self.storage, row["sizing_decision_id"], config)
            market = CryptoMarketDataStore(self.storage).load_verified(row["market_evidence_id"], config)
            risk_fp_rows = self.storage.fetch_all("SELECT decision_fingerprint FROM crypto_risk_decisions WHERE id=?", (row["risk_decision_id"],))
            risk_fp = str(risk_fp_rows[0]["decision_fingerprint"]) if len(risk_fp_rows) == 1 else ""
            expected = {
                "strategy_decision_fingerprint": strategy.decision_fingerprint if strategy is not None else "",
                "risk_decision_fingerprint": risk_fp,
                "risk_snapshot_fingerprint": _hash(risk_snapshot),
                "sizing_decision_fingerprint": sizing.decision_fingerprint,
                "capability_snapshot_fingerprint": sizing.capability_snapshot_fingerprint,
                "market_evidence_fingerprint": market.evidence_fingerprint,
            }
            for key, value in expected.items():
                if str(row[key]) != str(value):
                    raise CryptoPaperLaneError(f"crypto paper {key} changed after display")
            if strategy is not None and strategy.symbol != row["symbol"]:
                raise CryptoPaperLaneError("crypto paper strategy symbol changed")
            if sizing.symbol != row["symbol"] or sizing.side != row["side"] or sizing.action != row["action"]:
                raise CryptoPaperLaneError("crypto paper proposal child identity changed")
            if sizing.side == "buy" and (strategy is None or strategy.action != "entry"):
                raise CryptoPaperLaneError("crypto paper BUY strategy authority is missing")
            if sizing.side == "sell" and (strategy is not None or sizing.action not in {"exit", "reduce"} or sizing.request_basis != "quantity"):
                raise CryptoPaperLaneError("crypto paper SELL authority is invalid")
            if str(row["run_id"]) != str(risk_decision.get("run_id") or "") or str(row["run_id"]) != str(sizing.run_id):
                raise CryptoPaperLaneError("crypto paper run identity changed")
            if not risk_decision.get("risk_eligible") or not sizing.eligible or not sizing.authoritative or not market.authoritative or not market.execution_eligible:
                raise CryptoPaperLaneError("crypto paper proposal child authority is no longer executable")
        except CryptoPaperLaneError:
            raise
        except Exception as exc:
            raise CryptoPaperLaneError("crypto paper proposal authority reload failed") from exc
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current > _utc(row["expires_at"], "crypto paper proposal expiry") and row["status"] not in {"expired", "rejected", "filled"}:
            raise CryptoPaperLaneError("crypto paper proposal expired")
        return CryptoPaperProposal(
            id=str(row["id"]), run_id=str(row["run_id"]), symbol=str(row["symbol"]), side=str(row["side"]),
            action=str(row["action"]), request_basis=str(row["request_basis"]), quantity=row["quantity"],
            notional=row["notional"], limit_price=str(row["limit_price"]), stop_price=row["stop_price"],
            stop_risk=str(row["stop_risk"]), status=str(row["status"]), created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]), display_fingerprint=str(row["display_fingerprint"]),
            proposal_fingerprint=str(row["proposal_fingerprint"]), display=display, payload=payload,
        )

    def create_proposal(
        self,
        config: Mapping[str, Any],
        strategy_decision_id: str | None,
        risk_decision_id: str,
        *,
        now: datetime | None = None,
        telegram_message_id: str | None = None,
    ) -> CryptoPaperProposal:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        policy = _policy(config)
        formulas = _formula_versions(config)
        strategy = (
            CryptoStrategyStore(self.storage).load_verified(strategy_decision_id, config)
            if strategy_decision_id
            else None
        )
        risk_store = CryptoRiskStore(self.storage)
        risk_decision = risk_store.load_verified_decision(risk_decision_id, config, now=current)
        risk_decision_rows = self.storage.fetch_all(
            "SELECT decision_fingerprint FROM crypto_risk_decisions WHERE id=?", (risk_decision_id,)
        )
        if len(risk_decision_rows) != 1:
            raise CryptoPaperLaneError("crypto risk decision fingerprint is missing")
        risk_decision_fingerprint = str(risk_decision_rows[0]["decision_fingerprint"])
        risk_snapshot = risk_store.load_verified(risk_decision["snapshot_id"], config, now=current)
        sizing = load_verified_crypto_sizing(self.storage, risk_decision["sizing_decision_id"], config)
        if not risk_decision.get("risk_eligible") or not sizing.eligible or not sizing.authoritative:
            raise CryptoPaperLaneError("crypto risk/sizing authority is not eligible")
        if sizing.side == "buy":
            if strategy is None or not strategy.signal_eligible or strategy.selected_strategy is None or strategy.action != "entry":
                raise CryptoPaperLaneError("crypto strategy is not an eligible entry")
            if sizing.action not in {"entry", "add"}:
                raise CryptoPaperLaneError("crypto BUY action must be entry or add")
            strategy_context = strategy
            strategy_id = strategy.id
            strategy_fingerprint = strategy.decision_fingerprint
            run_id = strategy.run_id
        elif sizing.side == "sell":
            if sizing.action not in {"exit", "reduce"} or sizing.request_basis != "quantity":
                raise CryptoPaperLaneError("crypto SELL proposals require a quantity exit or reduce action")
            if strategy is not None:
                raise CryptoPaperLaneError("crypto SELL proposals must not reuse an entry strategy decision")
            from types import SimpleNamespace

            strategy_context = SimpleNamespace(
                symbol=sizing.symbol, selected_strategy="position_management_exit",
                lifecycle="POSITION_MANAGEMENT", action=sizing.action,
            )
            strategy_id = ""
            strategy_fingerprint = ""
            run_id = str(risk_decision.get("run_id") or "")
        else:
            raise CryptoPaperLaneError("crypto paper side is unsupported")
        config_hash = str(config.get("effective_config_hash") or "").strip().lower()
        config_identities = [risk_decision.get("config_hash"), risk_snapshot.get("config_hash"), sizing.config_hash]
        if strategy is not None:
            config_identities.append(strategy.config_hash)
        if not _valid_hash(config_hash) or any(str(value) != config_hash for value in config_identities):
            raise CryptoPaperLaneError("crypto paper proposal configuration identity differs")
        market = CryptoMarketDataStore(self.storage).load_verified(sizing.market_evidence_id, config)
        if not market.authoritative or not market.execution_eligible:
            raise CryptoPaperLaneError("crypto market evidence is not execution eligible")
        expires_at = min(current + timedelta(minutes=int(policy["expiry_minutes"])), _utc(risk_snapshot["expires_at"], "risk snapshot expiry"))
        if expires_at <= current:
            raise CryptoPaperLaneError("crypto risk authority expires before proposal")
        preview_id = str(uuid.uuid4())
        display = _display_from_authority(
            preview_id=preview_id, strategy=strategy_context, risk_decision=risk_decision,
            risk_snapshot=risk_snapshot, sizing=sizing, market=market, expires_at=expires_at,
        )
        payload = {
            "id": preview_id, "run_id": run_id,
            "strategy_decision_id": strategy_id, "strategy_decision_fingerprint": strategy_fingerprint,
            "risk_decision_id": risk_decision["id"], "risk_decision_fingerprint": risk_decision_fingerprint,
            "risk_snapshot_id": risk_snapshot["id"], "risk_snapshot_fingerprint": _hash(risk_snapshot),
            "sizing_decision_id": sizing.id, "sizing_decision_fingerprint": sizing.decision_fingerprint,
            "capability_snapshot_id": sizing.capability_snapshot_id, "capability_snapshot_fingerprint": sizing.capability_snapshot_fingerprint,
            "market_evidence_id": market.id, "market_evidence_fingerprint": market.evidence_fingerprint,
            "symbol": sizing.symbol, "side": sizing.side, "action": sizing.action,
            "request_basis": sizing.request_basis, "quantity": sizing.canonical_quantity,
            "notional": sizing.canonical_notional, "limit_price": sizing.limit_price,
            "stop_price": sizing.stop_price, "stop_risk": sizing.canonical_stop_risk,
            "status": "pending", "created_at": current.isoformat(), "expires_at": expires_at.isoformat(),
            "config_hash": config_hash, "formula_versions": formulas, "schema_version": CRYPTO_PAPER_EXECUTION_SCHEMA_VERSION, "display": display,
            "display_fingerprint": _hash(display), "paper_only": True, "manual_approval_required": True,
            "autonomous_execution": False, "live_enabled": False,
        }
        fingerprint = _hash(payload)
        existing_row: dict[str, Any] | None = None
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            apply_crypto_paper_lane_schema(conn, record_migration=False)
            existing = conn.execute(
                "SELECT * FROM crypto_paper_proposals WHERE strategy_decision_id=? AND risk_decision_id=? AND sizing_decision_id=?",
                (strategy_id, risk_decision["id"], sizing.id),
            ).fetchone()
            if existing:
                existing_row = dict(existing)
            else:
                conn.execute(
                """INSERT INTO crypto_paper_proposals(
                  id,run_id,strategy_decision_id,strategy_decision_fingerprint,risk_decision_id,risk_decision_fingerprint,
                  risk_snapshot_id,risk_snapshot_fingerprint,sizing_decision_id,sizing_decision_fingerprint,
                  capability_snapshot_id,capability_snapshot_fingerprint,market_evidence_id,market_evidence_fingerprint,
                  symbol,side,action,request_basis,quantity,notional,limit_price,stop_price,stop_risk,status,created_at,
                  expires_at,config_hash,formula_versions_json,schema_version,display_json,display_fingerprint,proposal_json,proposal_fingerprint,telegram_message_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                    payload["id"], payload["run_id"], payload["strategy_decision_id"], payload["strategy_decision_fingerprint"],
                    payload["risk_decision_id"], payload["risk_decision_fingerprint"], payload["risk_snapshot_id"], payload["risk_snapshot_fingerprint"],
                    payload["sizing_decision_id"], payload["sizing_decision_fingerprint"], payload["capability_snapshot_id"], payload["capability_snapshot_fingerprint"],
                    payload["market_evidence_id"], payload["market_evidence_fingerprint"], payload["symbol"], payload["side"], payload["action"],
                    payload["request_basis"], payload["quantity"], payload["notional"], payload["limit_price"], payload["stop_price"], payload["stop_risk"],
                    payload["status"], payload["created_at"], payload["expires_at"], config_hash, json_dumps(formulas), payload["schema_version"], json_dumps(display),
                    payload["display_fingerprint"], json_dumps(payload), fingerprint, telegram_message_id,
                    ),
                )
        if existing_row is not None:
            return self._verify_proposal_row(existing_row, config, now=current)
        return self.load_proposal(preview_id, config, now=current)

    def load_proposal(self, proposal_id: str, config: Mapping[str, Any], *, now: datetime | None = None) -> CryptoPaperProposal:
        row = self._load_proposal_row(proposal_id)
        return self._verify_proposal_row(row, config, now=now)

    def bind_telegram_message(
        self,
        proposal_id: str,
        message_id: str,
        config: Mapping[str, Any],
        *,
        chat_id: str | None = None,
        rendered_text: str | None = None,
        now: datetime | None = None,
    ) -> CryptoPaperProposal:
        if not str(message_id or "").strip():
            raise CryptoPaperLaneError("Telegram message identity is required")
        proposal = self.load_proposal(proposal_id, config, now=now)
        rendered = str(rendered_text if rendered_text is not None else format_crypto_paper_proposal(proposal))
        if not rendered.strip():
            raise CryptoPaperLaneError("Telegram display text is required")
        rendered_fingerprint = _sha256(rendered)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM crypto_paper_proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise CryptoPaperLaneError("crypto paper proposal is missing")
            if row["status"] != "pending":
                raise CryptoPaperLaneError("crypto paper proposal is no longer display-bindable")
            if row["telegram_message_id"] not in (None, str(message_id)):
                raise CryptoPaperLaneError("crypto paper proposal Telegram identity changed")
            if row["telegram_display_fingerprint"] not in (None, rendered_fingerprint):
                raise CryptoPaperLaneError("crypto paper proposal Telegram display changed")
            if row["telegram_display_text"] not in (None, rendered):
                raise CryptoPaperLaneError("crypto paper proposal Telegram display text changed")
            if row["telegram_chat_id"] not in (None, "") and str(chat_id) != str(row["telegram_chat_id"]):
                raise CryptoPaperLaneError("crypto paper proposal Telegram chat identity changed")
            bound_chat_id = row["telegram_chat_id"] if row["telegram_chat_id"] not in (None, "") else (None if chat_id is None else str(chat_id))
            conn.execute(
                """UPDATE crypto_paper_proposals
                   SET telegram_message_id=?,telegram_chat_id=?,telegram_display_text=?,
                       telegram_display_fingerprint=?,telegram_bound_at=? WHERE id=?""",
                (
                    str(message_id), bound_chat_id, rendered,
                    rendered_fingerprint, (now or datetime.now(UTC)).astimezone(UTC).isoformat(), proposal_id,
                ),
            )
        return self.load_proposal(proposal_id, config, now=now)

    def approve_proposal(
        self,
        proposal_id: str,
        config: Mapping[str, Any],
        *,
        sender_id: str,
        allowed_sender_id: str,
        raw_message: str | None = None,
        reply_to_message_id: str | None = None,
        chat_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if str(sender_id) != str(allowed_sender_id):
            raise CryptoPaperLaneError("unauthorized crypto paper approval sender")
        proposal = self.load_proposal(proposal_id, config, now=current)
        if proposal.status != "pending":
            raise CryptoPaperLaneError("crypto paper proposal is not pending")
        if proposal.display.get("approval_command") != f"YES CRYPTO {proposal.id}":
            raise CryptoPaperLaneError("crypto paper approval command identity mismatch")
        stored_row = self._load_proposal_row(proposal.id)
        stored_message = stored_row.get("telegram_message_id")
        if stored_message in (None, ""):
            raise CryptoPaperLaneError("crypto paper proposal has no bound Telegram display message")
        stored_text = str(stored_row.get("telegram_display_text") or "")
        stored_display_fingerprint = str(stored_row.get("telegram_display_fingerprint") or "")
        stored_chat_id = stored_row.get("telegram_chat_id")
        if stored_chat_id not in (None, "") and str(chat_id) != str(stored_chat_id):
            raise CryptoPaperLaneError("crypto paper approval chat identity does not match displayed proposal")
        expected_display_text = format_crypto_paper_proposal(proposal)
        if (
            not stored_text
            or stored_text != expected_display_text
            or _sha256(stored_text) != stored_display_fingerprint
        ):
            raise CryptoPaperLaneError("crypto paper Telegram display binding is missing or changed")
        if stored_message is not None and str(reply_to_message_id) != str(stored_message):
            raise CryptoPaperLaneError("crypto paper approval reply target does not match displayed Telegram message")
        expected_message = f"YES CRYPTO {proposal.id}"
        if raw_message is None or str(raw_message) != expected_message:
            raise CryptoPaperLaneError("crypto paper approval command is not exact")
        approval_id = str(uuid.uuid4())
        body = {
            "id": approval_id, "proposal_id": proposal.id, "sender_id": str(sender_id),
            "raw_message": str(raw_message),
            "reply_to_message_id": reply_to_message_id, "parsed_action": "approve",
            "telegram_chat_id": None if chat_id is None else str(chat_id),
            "display_fingerprint": proposal.display_fingerprint,
            "telegram_message_fingerprint": stored_display_fingerprint,
            "approved_at": current.isoformat(),
        }
        fingerprint = _hash(body)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM crypto_paper_proposals WHERE id=?", (proposal.id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise CryptoPaperLaneError("crypto paper proposal changed before approval")
            if _utc(row["expires_at"], "crypto paper proposal expiry") <= current:
                conn.execute("UPDATE crypto_paper_proposals SET status='expired' WHERE id=?", (proposal.id,))
                raise CryptoPaperLaneError("crypto paper proposal expired")
            existing = conn.execute("SELECT id FROM crypto_paper_approvals WHERE proposal_id=?", (proposal.id,)).fetchone()
            if existing:
                raise CryptoPaperLaneError("crypto paper proposal already has an approval")
            conn.execute(
                """INSERT INTO crypto_paper_approvals(
                   id,proposal_id,sender_id,raw_message,reply_to_message_id,parsed_action,status,
                   approved_at,display_fingerprint,telegram_chat_id,telegram_message_fingerprint,approval_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id, proposal.id, str(sender_id), body["raw_message"], reply_to_message_id,
                    "approve", "active", current.isoformat(), proposal.display_fingerprint,
                    None if chat_id is None else str(chat_id), stored_display_fingerprint, fingerprint,
                ),
            )
            conn.execute("UPDATE crypto_paper_proposals SET status='approved' WHERE id=?", (proposal.id,))
        return {**body, "approval_fingerprint": fingerprint, "status": "active"}

    def reject_proposal(
        self,
        proposal_id: str,
        config: Mapping[str, Any],
        *,
        sender_id: str,
        allowed_sender_id: str,
        raw_message: str | None = None,
        reply_to_message_id: str | None = None,
        chat_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist an exact, sender- and display-bound crypto rejection."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        if str(sender_id) != str(allowed_sender_id):
            raise CryptoPaperLaneError("unauthorized crypto paper rejection sender")
        proposal = self.load_proposal(proposal_id, config, now=current)
        if proposal.status != "pending":
            raise CryptoPaperLaneError("crypto paper proposal is not pending")
        row = self._load_proposal_row(proposal.id)
        message_id = str(row.get("telegram_message_id") or "")
        display_text = str(row.get("telegram_display_text") or "")
        display_fingerprint = str(row.get("telegram_display_fingerprint") or "")
        stored_chat_id = row.get("telegram_chat_id")
        if stored_chat_id not in (None, "") and str(chat_id) != str(stored_chat_id):
            raise CryptoPaperLaneError("crypto paper rejection chat identity does not match displayed proposal")
        if not message_id or not display_text or _sha256(display_text) != display_fingerprint:
            raise CryptoPaperLaneError("crypto paper Telegram display binding is missing or changed")
        if str(reply_to_message_id) != message_id:
            raise CryptoPaperLaneError("crypto paper rejection reply target does not match displayed Telegram message")
        expected = f"NO CRYPTO {proposal.id}"
        if raw_message is None or str(raw_message) != expected:
            raise CryptoPaperLaneError("crypto paper rejection command is not exact")
        rejection_id = str(uuid.uuid4())
        body = {
            "id": rejection_id, "proposal_id": proposal.id, "sender_id": str(sender_id),
            "raw_message": str(raw_message), "reply_to_message_id": str(reply_to_message_id),
            "telegram_chat_id": None if chat_id is None else str(chat_id),
            "telegram_message_fingerprint": display_fingerprint, "rejected_at": current.isoformat(),
        }
        fingerprint = _hash(body)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute("SELECT status FROM crypto_paper_proposals WHERE id=?", (proposal.id,)).fetchone()
            if current_row is None or current_row["status"] != "pending":
                raise CryptoPaperLaneError("crypto paper proposal changed before rejection")
            existing = conn.execute("SELECT id FROM crypto_paper_rejections WHERE proposal_id=?", (proposal.id,)).fetchone()
            if existing:
                raise CryptoPaperLaneError("crypto paper proposal already has a rejection")
            conn.execute(
                """INSERT INTO crypto_paper_rejections(
                   id,proposal_id,sender_id,telegram_chat_id,raw_message,reply_to_message_id,
                   telegram_message_fingerprint,rejection_fingerprint,rejected_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    rejection_id, proposal.id, str(sender_id), None if chat_id is None else str(chat_id),
                    str(raw_message), str(reply_to_message_id),
                    display_fingerprint, fingerprint, current.isoformat(),
                ),
            )
            conn.execute("UPDATE crypto_paper_proposals SET status='rejected' WHERE id=?", (proposal.id,))
        return {**body, "rejection_fingerprint": fingerprint, "status": "rejected"}

    def _active_reservation_totals(self, conn: Any, symbol: str) -> tuple[Decimal, Decimal]:
        rows = conn.execute(
            "SELECT active_notional,active_stop_risk FROM crypto_paper_reservations WHERE state='active' AND symbol=? ORDER BY id",
            (symbol,),
        ).fetchall()
        total_notional = sum((_decimal(row["active_notional"], "active crypto reservation notional") for row in rows), ZERO)
        total_stop_risk = sum((_decimal(row["active_stop_risk"], "active crypto reservation stop risk") for row in rows), ZERO)
        return total_notional, total_stop_risk

    def create_intent(self, proposal_id: str, config: Mapping[str, Any], *, now: datetime | None = None) -> CryptoPaperIntent:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        proposal = self.load_proposal(proposal_id, config, now=current)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM crypto_paper_proposals WHERE id=?", (proposal.id,)).fetchone()
            if row is None:
                raise CryptoPaperLaneError("crypto paper proposal disappeared")
            if row["status"] not in {"approved", "intent_created"}:
                raise CryptoPaperLaneError("crypto paper proposal is not approved for intent creation")
            existing = conn.execute("SELECT * FROM crypto_paper_intents WHERE proposal_id=?", (proposal.id,)).fetchone()
            if existing:
                return self._intent(existing)
            approval = conn.execute("SELECT * FROM crypto_paper_approvals WHERE proposal_id=? AND status='active'", (proposal.id,)).fetchone()
            if approval is None:
                raise CryptoPaperLaneError("active crypto paper approval is missing")
            if approval["display_fingerprint"] != row["display_fingerprint"] or _hash({
                "id": approval["id"], "proposal_id": approval["proposal_id"], "sender_id": approval["sender_id"],
                "raw_message": approval["raw_message"], "reply_to_message_id": approval["reply_to_message_id"],
                "parsed_action": approval["parsed_action"], "display_fingerprint": approval["display_fingerprint"],
                "telegram_message_fingerprint": approval["telegram_message_fingerprint"],
                "telegram_chat_id": approval["telegram_chat_id"],
                "approved_at": approval["approved_at"],
            }) != approval["approval_fingerprint"]:
                raise CryptoPaperLaneError("crypto paper approval fingerprint mismatch")
            if _utc(row["expires_at"], "crypto paper proposal expiry") <= current:
                conn.execute("UPDATE crypto_paper_proposals SET status='expired' WHERE id=?", (proposal.id,))
                conn.execute("UPDATE crypto_paper_approvals SET status='expired' WHERE id=?", (approval["id"],))
                raise CryptoPaperLaneError("crypto paper proposal expired")
            notional = _decimal(row["notional"], "crypto requested notional", positive=True)
            stop_risk = _decimal(row["stop_risk"], "crypto stop risk")
            policy = _policy(config)
            if str(row["side"]).lower() == "buy" and notional > policy["maximum_order_notional"]:
                raise CryptoPaperLaneError("crypto paper notional exceeds configured lane ceiling")
            # Re-open the risk authority to ensure the persisted snapshot still
            # has positive capacity.  Never derive a new size from a caller.
            risk_row = conn.execute("SELECT snapshot_json,snapshot_fingerprint,expires_at,authoritative FROM crypto_risk_snapshots WHERE id=?", (row["risk_snapshot_id"],)).fetchone()
            if risk_row is None or int(risk_row["authoritative"] or 0) != 1 or _utc(risk_row["expires_at"], "risk snapshot expiry") <= current:
                raise CryptoPaperLaneError("crypto risk snapshot is stale or non-authoritative")
            try:
                risk_snapshot = json.loads(risk_row["snapshot_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CryptoPaperLaneError("crypto risk snapshot JSON is invalid") from exc
            if _hash(risk_snapshot) != risk_row["snapshot_fingerprint"]:
                raise CryptoPaperLaneError("crypto risk snapshot fingerprint mismatch")
            derived = risk_snapshot.get("derived_authority") or {}
            hard_notional = _decimal(derived.get("hard_notional_ceiling"), "risk hard notional ceiling")
            hard_stop = _decimal(derived.get("hard_stop_risk_ceiling"), "risk hard stop ceiling")
            current_n, current_r = self._active_reservation_totals(conn, row["symbol"])
            if str(row["side"]).lower() == "buy" and (current_n + notional > hard_notional + Decimal("0.000000001") or current_r + stop_risk > hard_stop + Decimal("0.000000001")):
                raise CryptoPaperLaneError("crypto paper reservation exceeds authoritative risk capacity")
            action_body = {
                "proposal_id": proposal.id, "approval_id": approval["id"], "symbol": row["symbol"], "side": row["side"],
                "action": row["action"], "request_basis": row["request_basis"], "quantity": row["quantity"], "notional": row["notional"],
                "limit_price": row["limit_price"], "stop_price": row["stop_price"], "config_hash": row["config_hash"],
                "risk_snapshot_id": row["risk_snapshot_id"], "proposal_fingerprint": row["proposal_fingerprint"],
            }
            logical_key = _hash(action_body)
            client_order_id = "cp-" + logical_key[:32]
            intent_id = str(uuid.uuid4())
            reservation_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO crypto_paper_intents(
                  id,proposal_id,approval_id,logical_action_key,client_order_id,symbol,side,request_basis,
                  requested_quantity,requested_notional,limit_price,stop_price,reserved_notional,reserved_stop_risk,
                  state,broker_invocation_occurred,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intent_id, proposal.id, approval["id"], logical_key, client_order_id, row["symbol"], row["side"], row["request_basis"], row["quantity"], row["notional"], row["limit_price"], row["stop_price"], row["notional"], row["stop_risk"], "reserved", 0, current.isoformat(), current.isoformat()),
            )
            conn.execute(
                """INSERT INTO crypto_paper_reservations(
                  id,intent_id,symbol,initial_notional,active_notional,initial_stop_risk,active_stop_risk,state,created_at,updated_at
                  ,reservation_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reservation_id, intent_id, row["symbol"], row["notional"], row["notional"],
                    row["stop_risk"], row["stop_risk"], "active", current.isoformat(),
                    current.isoformat(),
                    _reservation_fingerprint({
                        "id": reservation_id, "intent_id": intent_id, "symbol": row["symbol"],
                        "initial_notional": row["notional"], "active_notional": row["notional"],
                        "initial_stop_risk": row["stop_risk"], "active_stop_risk": row["stop_risk"],
                        "state": "active", "created_at": current.isoformat(),
                        "updated_at": current.isoformat(), "released_at": None,
                        "release_reason": None,
                    }),
                ),
            )
            conn.execute(
                "INSERT INTO crypto_paper_order_events(id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), intent_id, f"{intent_id}:reserved", None, "reserved", "intent_reserved", "manual approval and authoritative risk capacity committed", current.isoformat()),
            )
            conn.execute(
                "UPDATE crypto_paper_approvals SET status='consumed',consumed_at=? WHERE id=? AND status='active'",
                (current.isoformat(), approval["id"]),
            )
            conn.execute("UPDATE crypto_paper_proposals SET status='intent_created' WHERE id=?", (proposal.id,))
            refreshed = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
        return self._intent(refreshed)

    @staticmethod
    def _normalize_broker_order(response: Any, *, intent: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize an Alpaca order response without introducing binary floats."""

        broker_order_id = str(_value(response, "id", "order_id", default="") or "").strip()
        client_order_id = str(_value(response, "client_order_id", default="") or "").strip()
        symbol = str(_value(response, "symbol", default="") or "").strip().upper().replace("-", "/")
        side = _enum(_value(response, "side", default=""))
        status = _enum(_value(response, "status", default="")) or "unknown"
        quantity_raw = _value(response, "qty", "quantity", default=None)
        requested_quantity = None if quantity_raw in (None, "") else _text(_decimal(quantity_raw, "broker order quantity", minimum=ZERO))
        requested_notional_raw = _value(response, "notional", default=None)
        requested_notional = None if requested_notional_raw in (None, "") else _text(_decimal(requested_notional_raw, "broker order notional", minimum=ZERO))
        filled_raw = _value(response, "filled_qty", "filled_quantity", default=None)
        cumulative = ZERO if filled_raw in (None, "") else _decimal(filled_raw, "broker cumulative filled quantity", minimum=ZERO)
        avg_raw = _value(response, "filled_avg_price", "filled_average_price", "avg_fill_price", default=None)
        average = None if avg_raw in (None, "") else _text(_decimal(avg_raw, "broker average fill price", positive=True))
        fees_raw = _value(response, "fees", "fee", default="0")
        fee = _text(_decimal(fees_raw, "broker order fees", minimum=ZERO))
        submitted_at = _value(response, "submitted_at", default=None)
        updated_at = _value(response, "updated_at", default=None)
        limit_raw = _value(response, "limit_price", default=None)
        limit_price = None if limit_raw in (None, "") else _text(_decimal(limit_raw, "broker order limit price", positive=True))
        return {
            "broker_order_id": broker_order_id, "client_order_id": client_order_id,
            "symbol": symbol, "side": side, "status": status,
            "requested_quantity": requested_quantity or intent.get("requested_quantity"),
            "requested_notional": requested_notional or intent.get("requested_notional"),
            "cumulative_filled_quantity": _text(cumulative), "filled_average_price": average,
            "fees": fee, "limit_price": limit_price,
            "paper_account_id_hash": _value(response, "paper_account_id_hash", default=None),
            "submitted_at": None if submitted_at is None else str(submitted_at),
            "updated_at": None if updated_at is None else str(updated_at),
            "payload": {
                "id": broker_order_id, "client_order_id": client_order_id, "symbol": symbol,
                "side": side, "status": status, "qty": requested_quantity,
                "notional": requested_notional, "filled_qty": _text(cumulative),
                "filled_avg_price": average, "fees": fee, "limit_price": limit_price,
                "submitted_at": None if submitted_at is None else str(submitted_at),
                "updated_at": None if updated_at is None else str(updated_at),
            },
        }

    @staticmethod
    def _mapped_order_state(status: str) -> str:
        value = str(status or "").lower()
        return {
            "filled": "filled", "partially_filled": "partially_filled", "partial_fill": "partially_filled",
            "rejected": "rejected", "canceled": "cancelled", "cancelled": "cancelled", "expired": "expired",
        }.get(value, "submitted")

    def _record_reconciliation_event_locked(
        self,
        conn: Any,
        *,
        intent: Mapping[str, Any],
        event_type: str,
        evidence: Mapping[str, Any],
        now: datetime,
    ) -> None:
        payload = self._evidence_payload(evidence)
        fingerprint = _hash(payload)
        conn.execute(
            """INSERT OR IGNORE INTO crypto_paper_reconciliation_events(
               id,intent_id,event_type,broker_order_id,client_order_id,payload,payload_fingerprint,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), intent["id"], event_type, evidence.get("broker_order_id"),
                evidence.get("client_order_id"), json_dumps(payload), fingerprint, now.isoformat(),
            ),
        )

    @staticmethod
    def _mark_reconciliation_required_locked(
        conn: Any,
        *,
        intent_id: str,
        proposal_id: str,
        error: str,
        now: datetime,
    ) -> None:
        """Move both order and proposal authority to an explicit review state."""

        conn.execute(
            "UPDATE crypto_paper_intents SET state='reconciliation_required',last_error=?,updated_at=? WHERE id=?",
            (error, now.isoformat(), intent_id),
        )
        conn.execute(
            "UPDATE crypto_paper_proposals SET status='manual_review' WHERE id=? AND status NOT IN ('rejected','expired')",
            (proposal_id,),
        )

    def reconcile_intent(self, intent_id: str, config: Mapping[str, Any], broker: Any, *, now: datetime | None = None) -> dict[str, Any]:
        """Resolve a post-invocation intent by broker client-order identity.

        A missing, malformed, or conflicting lookup is retained as a durable
        reconciliation state. It is never converted into a fresh submission.
        Verified cumulative fills are applied monotonically and late fills after
        cancellation remain eligible for accounting.
        """

        current = (now or datetime.now(UTC)).astimezone(UTC)
        _policy(config)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise CryptoPaperLaneError("crypto paper intent is missing")
            intent = dict(row)
        if not intent["broker_invocation_occurred"]:
            return {**intent, "state": intent["state"], "reconciliation": "not_invoked"}
        lookup = getattr(broker, "get_order_by_client_order_id", None)
        lookup_by_id = getattr(broker, "get_order", None)
        if not callable(lookup) and not callable(lookup_by_id):
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._record_reconciliation_event_locked(conn, intent=intent, event_type="lookup_unavailable", evidence={"client_order_id": intent["client_order_id"]}, now=current)
                self._mark_reconciliation_required_locked(
                    conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                    error="crypto broker order lookup unavailable", now=current,
                )
            return {**intent, "state": "reconciliation_required", "reconciliation": "lookup_unavailable"}
        try:
            response = lookup(str(intent["client_order_id"])) if callable(lookup) else None
            if response is None and callable(lookup_by_id) and intent.get("broker_order_id"):
                response = lookup_by_id(str(intent["broker_order_id"]))
        except Exception as exc:
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._record_reconciliation_event_locked(conn, intent=intent, event_type="lookup_failed", evidence={"client_order_id": intent["client_order_id"], "error_type": type(exc).__name__}, now=current)
                self._mark_reconciliation_required_locked(
                    conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                    error=f"crypto order lookup failed:{type(exc).__name__}", now=current,
                )
            return {**intent, "state": "reconciliation_required", "reconciliation": "lookup_failed", "last_error": type(exc).__name__}
        if response is None:
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._record_reconciliation_event_locked(conn, intent=intent, event_type="order_not_found", evidence={"client_order_id": intent["client_order_id"]}, now=current)
                self._mark_reconciliation_required_locked(
                    conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                    error="broker order not found by client_order_id", now=current,
                )
            return {**intent, "state": "reconciliation_required", "reconciliation": "order_not_found"}
        try:
            evidence = self._normalize_broker_order(response, intent=intent)
        except CryptoPaperLaneError as exc:
            evidence = {"client_order_id": intent["client_order_id"], "status": "unknown", "verified": False, "verification_error": str(exc)}
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._record_reconciliation_event_locked(conn, intent=intent, event_type="malformed_order", evidence=evidence, now=current)
                self._mark_reconciliation_required_locked(
                    conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                    error=str(exc), now=current,
                )
            return {**intent, "state": "reconciliation_required", "reconciliation": "malformed_order", "last_error": str(exc)}
        verification_errors: list[str] = []
        if not evidence["broker_order_id"]:
            verification_errors.append("broker_order_id_missing")
        if evidence["client_order_id"] != str(intent["client_order_id"]):
            verification_errors.append("client_order_id_mismatch")
        if evidence["symbol"] != str(intent["symbol"]).upper() or evidence["side"] != str(intent["side"]).lower():
            verification_errors.append("symbol_or_side_mismatch")
        if intent.get("broker_order_id") and evidence["broker_order_id"] != str(intent["broker_order_id"]):
            verification_errors.append("broker_order_id_mismatch")
        try:
            identity_reader = getattr(broker, "paper_account_identity", None)
            identity = identity_reader() if callable(identity_reader) else None
            if not isinstance(identity, Mapping) or identity.get("verified") is not True or identity.get("mode") != "paper" or identity.get("endpoint_class") != "paper":
                verification_errors.append("paper_account_identity_not_verified")
            else:
                account_hash = str(identity.get("account_id_hash") or "")
                if not account_hash:
                    verification_errors.append("paper_account_id_hash_missing")
                else:
                    evidence["paper_account_id_hash"] = account_hash
        except Exception as exc:
            verification_errors.append(f"paper_account_identity_unavailable:{type(exc).__name__}")
        evidence["verified"] = not verification_errors
        evidence["verification_error"] = ";".join(verification_errors) if verification_errors else None
        if verification_errors:
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._record_order_evidence_locked(conn, intent=intent, evidence=evidence, captured_at=current)
                self._record_reconciliation_event_locked(conn, intent=intent, event_type="identity_conflict", evidence=evidence, now=current)
                self._mark_reconciliation_required_locked(
                    conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                    error=";".join(verification_errors), now=current,
                )
            return {**intent, "state": "reconciliation_required", "reconciliation": "identity_conflict", "last_error": ";".join(verification_errors)}
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            refreshed = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
            if refreshed is None:
                raise CryptoPaperLaneError("crypto paper intent disappeared during reconciliation")
            intent = dict(refreshed)
            evidence_id, evidence_fingerprint = self._record_order_evidence_locked(conn, intent=intent, evidence=evidence, captured_at=current)
            mapped = self._mapped_order_state(evidence["status"])
            conn.execute(
                "UPDATE crypto_paper_intents SET state=?,broker_order_id=?,last_error=NULL,updated_at=?,terminal_at=CASE WHEN ? IN ('rejected','cancelled','expired') THEN ? ELSE terminal_at END WHERE id=?",
                (mapped, evidence["broker_order_id"], current.isoformat(), mapped, current.isoformat(), intent_id),
            )
            conn.execute("UPDATE crypto_paper_proposals SET status=? WHERE id=?", (mapped, intent["proposal_id"]))
            if mapped in {"rejected", "cancelled", "expired"} and _decimal(evidence["cumulative_filled_quantity"], "reconciled cumulative quantity") == ZERO:
                conn.execute(
                    "UPDATE crypto_paper_reservations SET active_notional='0',active_stop_risk='0',state='released',released_at=?,release_reason=?,updated_at=? WHERE intent_id=? AND state='active'",
                    (current.isoformat(), mapped, current.isoformat(), intent_id),
                )
                self._refresh_reservation_fingerprint_locked(conn, intent_id)
            self._record_reconciliation_event_locked(conn, intent=intent, event_type="order_verified", evidence={**evidence, "evidence_id": evidence_id, "evidence_fingerprint": evidence_fingerprint}, now=current)
        cumulative = _decimal(evidence["cumulative_filled_quantity"], "reconciled cumulative quantity")
        with self.storage.connect() as conn:
            fill_totals = conn.execute(
                "SELECT quantity,price,fees FROM crypto_paper_fills WHERE intent_id=? ORDER BY occurred_at,id",
                (intent_id,),
            ).fetchall()
            total = sum(
                (_decimal(item["quantity"], "crypto persisted fill quantity", minimum=ZERO) for item in fill_totals),
                ZERO,
            )
            prior_notional = sum(
                (
                    _decimal(item["quantity"], "crypto persisted fill quantity", minimum=ZERO)
                    * _decimal(item["price"], "crypto persisted fill price", positive=True)
                    for item in fill_totals
                ),
                ZERO,
            )
            prior_fees = sum((_decimal(item["fees"], "crypto persisted fill fees") for item in fill_totals), ZERO)
        result: dict[str, Any] = {**intent, "state": self._mapped_order_state(evidence["status"]), "broker_order_id": evidence["broker_order_id"], "reconciliation": "verified", "evidence_fingerprint": evidence_fingerprint}
        if cumulative > total:
            if evidence.get("filled_average_price") is None:
                with self.storage.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._mark_reconciliation_required_locked(
                        conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                        error="broker fill quantity is present without average fill price", now=current,
                    )
                return {**result, "state": "reconciliation_required", "reconciliation": "fill_price_missing"}
            cumulative_average = _decimal(
                evidence["filled_average_price"],
                "reconciled cumulative average fill price",
                positive=True,
            )
            reported_fees = _decimal(evidence.get("fees") or "0", "reconciled cumulative fees")
            if reported_fees < prior_fees:
                with self.storage.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._record_reconciliation_event_locked(
                        conn,
                        intent=intent,
                        event_type="fee_accounting_regressed",
                        evidence={**evidence, "prior_fees": _text(prior_fees)},
                        now=current,
                    )
                    self._mark_reconciliation_required_locked(
                        conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                        error="broker cumulative fees regressed", now=current,
                    )
                return {**result, "state": "reconciliation_required", "reconciliation": "fee_accounting_regressed"}
            fee_delta = reported_fees - prior_fees
            delta = cumulative - total
            cumulative_notional = cumulative * cumulative_average
            delta_notional = cumulative_notional - prior_notional
            if delta_notional <= ZERO:
                with self.storage.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._record_reconciliation_event_locked(
                        conn,
                        intent=intent,
                        event_type="fill_notional_regressed",
                        evidence={**evidence, "prior_notional": _text(prior_notional)},
                        now=current,
                    )
                    self._mark_reconciliation_required_locked(
                        conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                        error="broker cumulative fill notional regressed", now=current,
                    )
                return {**result, "state": "reconciliation_required", "reconciliation": "fill_notional_regressed"}
            delta_price = delta_notional / delta
            event_key = f"{evidence['broker_order_id']}:cumulative:{_text(cumulative)}"
            try:
                fill = self.record_verified_fill(
                    intent_id, event_key, delta, delta_price, fee_delta, config,
                    broker_evidence=evidence, occurred_at=current,
                )
            except CryptoPaperLaneError as exc:
                with self.storage.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._record_reconciliation_event_locked(
                        conn,
                        intent=intent,
                        event_type="fill_accounting_failed",
                        evidence={**evidence, "accounting_error": str(exc)},
                        now=current,
                    )
                    self._mark_reconciliation_required_locked(
                        conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                        error=f"verified fill accounting failed:{exc}", now=current,
                    )
                return {
                    **result,
                    "state": "reconciliation_required",
                    "reconciliation": "fill_accounting_failed",
                    "last_error": str(exc),
                }
            result.update({"fill": fill, "state": fill["state"]})
        elif cumulative < total:
            result.update({"state": "reconciliation_required", "reconciliation": "broker_cumulative_regressed"})
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._mark_reconciliation_required_locked(
                    conn, intent_id=intent_id, proposal_id=str(intent["proposal_id"]),
                    error="broker cumulative fill quantity regressed", now=current,
                )
        return result

    def recover_pending(self, config: Mapping[str, Any], broker: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Reconcile all crypto intents that could have reached the broker."""

        rows = self.storage.fetch_all(
            """SELECT id FROM crypto_paper_intents
               WHERE broker_invocation_occurred=1
                 AND state IN ('submitting','submitted','partially_filled','unknown','reconciliation_required','cancel_pending')
               ORDER BY created_at,id"""
        )
        return [self.reconcile_intent(str(row["id"]), config, broker, now=now) for row in rows]

    @staticmethod
    def _intent(row: Mapping[str, Any]) -> CryptoPaperIntent:
        return CryptoPaperIntent(
            id=str(row["id"]), proposal_id=str(row["proposal_id"]), approval_id=str(row["approval_id"]),
            logical_action_key=str(row["logical_action_key"]), client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]), request_basis=str(row["request_basis"]), requested_quantity=row["requested_quantity"],
            requested_notional=row["requested_notional"], limit_price=str(row["limit_price"]), state=str(row["state"]),
            broker_invocation_occurred=bool(row["broker_invocation_occurred"]),
        )

    def _final_controls(self, conn: Any, row: Mapping[str, Any], config: Mapping[str, Any], broker: Any, *, now: datetime) -> list[str]:
        failures: list[str] = []
        for name in ("power", "internet", "database", "telegram"):
            probe = self.control_providers.get(name)
            if probe is None:
                failures.append(f"{name}_health_probe_missing")
                continue
            try:
                if probe() is not True:
                    failures.append(f"{name}_health_not_verified")
            except Exception as exc:
                failures.append(f"{name}_health_probe_failed:{type(exc).__name__}")
        try:
            identity = broker.paper_account_identity()
            if not isinstance(identity, Mapping) or identity.get("verified") is not True or identity.get("mode") != "paper" or identity.get("endpoint_class") != "paper":
                failures.append("paper_account_identity_not_verified")
            identity_hash = str((identity or {}).get("account_id_hash") or "") if isinstance(identity, Mapping) else ""
        except Exception as exc:
            failures.append(f"paper_account_identity_unavailable:{type(exc).__name__}")
            identity_hash = ""
        snapshot_row = conn.execute(
            "SELECT snapshot_json,snapshot_fingerprint,authoritative,expires_at FROM crypto_risk_snapshots WHERE id=(SELECT risk_snapshot_id FROM crypto_paper_proposals WHERE id=?)",
            (row["proposal_id"],),
        ).fetchone()
        fresh_snapshot_payload: dict[str, Any] | None = None
        if snapshot_row is None or int(snapshot_row["authoritative"] or 0) != 1:
            failures.append("crypto_risk_authority_missing_at_final_check")
        else:
            try:
                snapshot = json.loads(snapshot_row["snapshot_json"])
                if _hash(snapshot) != snapshot_row["snapshot_fingerprint"]:
                    failures.append("crypto_risk_snapshot_fingerprint_mismatch_at_final_check")
                expected_hash = str((snapshot.get("account") or {}).get("paper_account_id_hash") or "")
                if not expected_hash or expected_hash != identity_hash:
                    failures.append("crypto_paper_account_identity_changed")
                if _utc(snapshot_row["expires_at"], "crypto risk expiry") <= now:
                    failures.append("crypto_risk_snapshot_expired_at_final_check")
            except (TypeError, ValueError, json.JSONDecodeError, CryptoPaperLaneError):
                failures.append("crypto_risk_snapshot_invalid_at_final_check")
        if self._last_final_risk_snapshot_id:
            fresh_rows = conn.execute(
                "SELECT snapshot_json,snapshot_fingerprint,authoritative FROM crypto_risk_snapshots WHERE id=?",
                (self._last_final_risk_snapshot_id,),
            ).fetchall()
            if len(fresh_rows) != 1:
                failures.append("fresh_crypto_risk_snapshot_missing_at_final_check")
            else:
                try:
                    fresh_snapshot_payload = json.loads(fresh_rows[0]["snapshot_json"])
                    if (
                        not isinstance(fresh_snapshot_payload, dict)
                        or int(fresh_rows[0]["authoritative"] or 0) != 1
                        or _hash(fresh_snapshot_payload) != fresh_rows[0]["snapshot_fingerprint"]
                    ):
                        failures.append("fresh_crypto_risk_snapshot_invalid_at_final_check")
                        fresh_snapshot_payload = None
                except (TypeError, ValueError, json.JSONDecodeError):
                    failures.append("fresh_crypto_risk_snapshot_invalid_at_final_check")
        try:
            account = broker.get_account()
            if _enum(_value(account, "status", default="")) != "active" or str(_value(account, "currency", default="")).upper() != "USD":
                failures.append("paper_account_not_active_or_usd")
            if fresh_snapshot_payload is not None:
                expected_account = fresh_snapshot_payload.get("account") or {}
                for name in ("equity", "cash", "non_marginable_buying_power"):
                    current_value = _decimal(_value(account, name, default=None), f"final account {name}")
                    expected_value = _decimal(expected_account.get(name), f"fresh snapshot account {name}")
                    if current_value != expected_value:
                        failures.append(f"final_account_{name}_changed_after_risk_rebuild")
        except Exception as exc:
            failures.append(f"paper_account_unavailable:{type(exc).__name__}")
        try:
            current_quote = broker.get_crypto_latest_quote(str(row["symbol"]))
            bid = _decimal(_value(current_quote, "bid_price", default=None), "final crypto bid", positive=True)
            ask = _decimal(_value(current_quote, "ask_price", default=None), "final crypto ask", positive=True)
            if ask < bid:
                failures.append("final_crypto_quote_crossed")
            spread = (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("10000")
            max_spread = _decimal((config.get("crypto") or {}).get("max_spread_bps"), "crypto max spread")
            if spread > max_spread:
                failures.append("final_crypto_spread_exceeds_policy")
            limit_price = _decimal(row["limit_price"], "crypto intent limit price", positive=True)
            if str(row["side"]).lower() == "buy" and ask > limit_price:
                failures.append("final_crypto_ask_above_limit_price")
            if str(row["side"]).lower() == "sell" and bid < limit_price:
                failures.append("final_crypto_bid_below_limit_price")
            quote_at = _value(current_quote, "timestamp", default=None)
            if quote_at is None or (now - _utc(quote_at, "final crypto quote" )).total_seconds() > float((config.get("crypto") or {}).get("max_price_age_seconds", 300)):
                failures.append("final_crypto_quote_stale")
            if fresh_snapshot_payload is not None:
                market_id = str(fresh_snapshot_payload.get("market_evidence_id") or "")
                market_rows = conn.execute(
                    "SELECT bid_price,ask_price FROM crypto_market_data_evidence WHERE id=?",
                    (market_id,),
                ).fetchall()
                if len(market_rows) != 1:
                    failures.append("fresh_crypto_market_evidence_missing_at_final_check")
                else:
                    if (
                        _decimal(market_rows[0]["bid_price"], "fresh crypto bid", positive=True) != bid
                        or _decimal(market_rows[0]["ask_price"], "fresh crypto ask", positive=True) != ask
                    ):
                        failures.append("final_crypto_quote_changed_after_risk_rebuild")
        except Exception as exc:
            failures.append(f"final_crypto_quote_unavailable:{type(exc).__name__}")
        try:
            open_orders = list(broker.get_open_orders())
            for order in open_orders:
                symbol = str(_value(order, "symbol", default="") or "").upper().replace("-", "/")
                client_id = str(_value(order, "client_order_id", default="") or "")
                status = _enum(_value(order, "status", default=""))
                if symbol == str(row["symbol"]).upper() and client_id != str(row["client_order_id"]) and status not in {"filled", "canceled", "cancelled", "rejected", "expired"}:
                    failures.append("conflicting_crypto_open_order")
            if fresh_snapshot_payload is not None:
                expected_orders = fresh_snapshot_payload.get("open_orders") or []
                current_orders = []
                for order in open_orders:
                    current_orders.append({
                        "broker_order_id": str(_value(order, "id", "order_id", default="") or ""),
                        "client_order_id": str(_value(order, "client_order_id", default="") or ""),
                        "symbol": str(_value(order, "symbol", default="") or "").upper().replace("-", "/"),
                        "side": _enum(_value(order, "side", default="")),
                        "status": _enum(_value(order, "status", default="")),
                        "quantity": _text(_decimal(_value(order, "qty", "quantity", default="0"), "final open-order quantity")),
                        "filled_quantity": _text(_decimal(_value(order, "filled_qty", "filled_quantity", default="0"), "final open-order filled quantity")),
                        "notional": None if _value(order, "notional", default=None) in (None, "") else _text(_decimal(_value(order, "notional"), "final open-order notional")),
                        "limit_price": None if _value(order, "limit_price", default=None) in (None, "") else _text(_decimal(_value(order, "limit_price"), "final open-order limit price")),
                    })
                expected_orders_simple = [
                    {
                        key: item.get(key)
                        for key in (
                            "broker_order_id", "client_order_id", "symbol", "side", "status",
                            "quantity", "filled_quantity", "notional", "limit_price",
                        )
                    }
                    for item in expected_orders
                ]
                if sorted(current_orders, key=lambda item: (item["symbol"], item["client_order_id"], item["broker_order_id"])) != sorted(expected_orders_simple, key=lambda item: (str(item.get("symbol") or ""), str(item.get("client_order_id") or ""), str(item.get("broker_order_id") or ""))):
                    failures.append("final_open_orders_changed_after_risk_rebuild")
        except Exception as exc:
            failures.append(f"crypto_open_orders_unavailable:{type(exc).__name__}")
        try:
            positions = list(broker.get_positions())
            same_symbol_quantity = ZERO
            for position in positions:
                symbol = str(_value(position, "symbol", default="") or "").upper().replace("-", "/")
                if symbol != str(row["symbol"]).upper():
                    continue
                quantity = _decimal(_value(position, "qty", "quantity", default=None), "final crypto position quantity")
                same_symbol_quantity += quantity
            candidate = conn.execute(
                "SELECT action FROM crypto_paper_proposals WHERE id=?", (row["proposal_id"],)
            ).fetchone()
            candidate_action = str(candidate["action"] if candidate is not None else "").lower()
            if str(row["side"]).lower() == "buy" and candidate_action == "entry" and same_symbol_quantity > ZERO:
                failures.append("crypto_entry_position_appeared_after_snapshot")
            if str(row["side"]).lower() == "buy" and candidate_action == "add" and same_symbol_quantity <= ZERO:
                failures.append("crypto_add_position_disappeared_after_snapshot")
            if str(row["side"]).lower() == "sell":
                requested_quantity = _decimal(row["requested_quantity"], "final crypto sell quantity", positive=True)
                if same_symbol_quantity < requested_quantity:
                    failures.append("crypto_sellable_quantity_decreased_after_snapshot")
            if fresh_snapshot_payload is not None:
                expected_positions = fresh_snapshot_payload.get("positions") or []
                current_positions = []
                for position in positions:
                    raw_symbol = str(_value(position, "symbol", default="") or "").upper().replace("-", "/")
                    qty = _decimal(_value(position, "qty", "quantity", default="0"), "final position quantity")
                    market_value = _decimal(_value(position, "market_value", default="0"), "final position market value")
                    current_price = _decimal(_value(position, "current_price", default="0"), "final position current price")
                    raw_average = _value(position, "avg_entry_price", "average_entry_price", "average_price", default=None)
                    current_positions.append({
                        "symbol": raw_symbol,
                        "quantity": _text(qty),
                        "market_value": _text(market_value),
                        "current_price": _text(current_price),
                        "average_entry_price": None if raw_average in (None, "") else _text(_decimal(raw_average, "final position average entry")),
                    })
                expected_positions_simple = [
                    {
                        "symbol": str(item.get("symbol") or "").upper().replace("-", "/"),
                        "quantity": str(item.get("quantity") or ""),
                        "market_value": str(item.get("market_value") or ""),
                        "current_price": str(item.get("current_price") or ""),
                        "average_entry_price": item.get("average_entry_price"),
                    }
                    for item in expected_positions
                ]
                if sorted(current_positions, key=lambda item: item["symbol"]) != sorted(expected_positions_simple, key=lambda item: item["symbol"]):
                    failures.append("final_positions_changed_after_risk_rebuild")
        except Exception as exc:
            failures.append(f"crypto_positions_unavailable:{type(exc).__name__}")
        other_intent = conn.execute(
            """SELECT i.id FROM crypto_paper_intents i
               JOIN crypto_paper_reservations r ON r.intent_id=i.id
               WHERE i.symbol=? AND i.id<>? AND i.state IN ('reserved','submitting','submitted','partially_filled','retryable_pre_submission','unknown','reconciliation_required')
                 AND r.state='active' LIMIT 1""",
            (row["symbol"], row["id"]),
        ).fetchone()
        if other_intent is not None:
            failures.append("conflicting_crypto_durable_intent")
        try:
            metrics = broker.get_loss_metrics()
            if not isinstance(metrics, Mapping) or metrics.get("metrics_version") != "loss_controls_v2" or metrics.get("daily_loss_confidence") != "verified" or metrics.get("weekly_loss_confidence") != "verified":
                failures.append("crypto_loss_evidence_not_verified")
            captured_at = metrics.get("captured_at") if isinstance(metrics, Mapping) else None
            if captured_at is None:
                failures.append("crypto_loss_evidence_timestamp_missing")
            else:
                age = (now - _utc(captured_at, "crypto loss evidence")).total_seconds()
                if age < -1 or age > 300:
                    failures.append("crypto_loss_evidence_stale")
            parsed_loss: dict[str, Decimal] = {}
            for name in ("daily_loss_dollars", "weekly_loss_dollars", "reference_equity"):
                try:
                    parsed_loss[name] = _decimal(metrics.get(name) if isinstance(metrics, Mapping) else None, f"crypto {name}")
                except CryptoPaperLaneError:
                    failures.append(f"crypto_loss_{name}_invalid")
            if len(parsed_loss) == 3 and str(row["side"]).lower() == "buy":
                risk_policy = (config.get("crypto") or {}).get("risk_policy") or {}
                reference = parsed_loss["reference_equity"]
                daily_limit = reference * _decimal(risk_policy.get("daily_account_loss_halt_pct_equity"), "daily account loss threshold") / Decimal("100")
                weekly_limit = reference * _decimal(risk_policy.get("weekly_account_loss_halt_pct_equity"), "weekly account loss threshold") / Decimal("100")
                if parsed_loss["daily_loss_dollars"] >= daily_limit:
                    failures.append("crypto_daily_account_loss_threshold_reached")
                if parsed_loss["weekly_loss_dollars"] >= weekly_limit:
                    failures.append("crypto_weekly_account_loss_threshold_reached")
                local_rows = conn.execute(
                    "SELECT realized_pl_decimal FROM realized_pnl_events WHERE source='crypto_paper_fill' AND trading_day>=? AND trading_day<=? ORDER BY trading_day,id",
                    ((now.date() - timedelta(days=now.weekday())).isoformat(), now.date().isoformat()),
                ).fetchall()
                local_weekly_loss = sum((max(ZERO, -_decimal(item["realized_pl_decimal"], "crypto realized loss")) for item in local_rows), ZERO)
                daily_rows = conn.execute(
                    "SELECT realized_pl_decimal FROM realized_pnl_events WHERE source='crypto_paper_fill' AND trading_day=? ORDER BY id",
                    (now.date().isoformat(),),
                ).fetchall()
                local_daily_loss = sum((max(ZERO, -_decimal(item["realized_pl_decimal"], "crypto realized loss")) for item in daily_rows), ZERO)
                daily_crypto_limit = reference * _decimal(risk_policy.get("daily_crypto_loss_halt_pct_equity"), "daily crypto loss threshold") / Decimal("100")
                weekly_crypto_limit = reference * _decimal(risk_policy.get("weekly_crypto_loss_halt_pct_equity"), "weekly crypto loss threshold") / Decimal("100")
                if local_daily_loss >= daily_crypto_limit:
                    failures.append("crypto_daily_loss_threshold_reached")
                if local_weekly_loss >= weekly_crypto_limit:
                    failures.append("crypto_weekly_loss_threshold_reached")
        except Exception as exc:
            failures.append(f"crypto_loss_evidence_unavailable:{type(exc).__name__}")
        try:
            kill = self.storage.get_control_state("kill_switch_active")
            if kill_switch_active() or str(kill or "").strip().lower() in {"1", "true", "on", "active"}:
                failures.append("kill_switch_active")
        except Exception:
            failures.append("kill_switch_state_unavailable")
        reservation = conn.execute(
            "SELECT state,active_notional,active_stop_risk FROM crypto_paper_reservations WHERE intent_id=?",
            (row["id"],),
        ).fetchone()
        if reservation is None or reservation["state"] != "active":
            failures.append("crypto_paper_reservation_not_active")
        else:
            try:
                if _decimal(reservation["active_notional"], "active crypto reservation notional") != _decimal(row["reserved_notional"], "intent reserved notional"):
                    failures.append("crypto_paper_reservation_notional_changed")
                if _decimal(reservation["active_stop_risk"], "active crypto reservation stop risk") != _decimal(row["reserved_stop_risk"], "intent reserved stop risk"):
                    failures.append("crypto_paper_reservation_stop_risk_changed")
            except CryptoPaperLaneError as exc:
                failures.append(str(exc).replace(" ", "_"))
        proposal = conn.execute(
            "SELECT * FROM crypto_paper_proposals WHERE id=?", (row["proposal_id"],)
        ).fetchone()
        if proposal is None or proposal["status"] not in {"intent_created", "submitting", "retryable_pre_submission"}:
            failures.append("crypto_paper_proposal_not_executable")
        elif _utc(proposal["expires_at"], "crypto paper expiry") <= now:
            failures.append("crypto_paper_proposal_expired")
        if proposal is not None:
            try:
                # Re-open every persisted child authority at the adapter
                # boundary. Hashes alone are not enough: a locally edited
                # proposal must still agree with its strategy, risk, sizing,
                # market and capability evidence before any broker call.
                self._verify_proposal_row(dict(proposal), config, now=now)
            except Exception as exc:
                failures.append(f"crypto_paper_authority_reload_failed:{type(exc).__name__}")
            try:
                proposal_payload = json.loads(proposal["proposal_json"])
                display_payload = json.loads(proposal["display_json"])
                if not isinstance(proposal_payload, dict) or not isinstance(display_payload, dict):
                    raise ValueError
                if _hash(proposal_payload) != proposal["proposal_fingerprint"] or _hash(display_payload) != proposal["display_fingerprint"]:
                    failures.append("crypto_paper_display_or_proposal_fingerprint_changed")
                if proposal_payload.get("display") != display_payload or proposal_payload.get("display_fingerprint") != proposal["display_fingerprint"]:
                    failures.append("crypto_paper_display_binding_changed")
            except (TypeError, ValueError, json.JSONDecodeError):
                failures.append("crypto_paper_display_or_proposal_json_invalid")
            if proposal["config_hash"] != str(config.get("effective_config_hash") or ""):
                failures.append("crypto_paper_configuration_changed")
            if proposal["id"] != row["proposal_id"] or proposal["symbol"] != row["symbol"] or proposal["side"] != row["side"] or proposal["request_basis"] != row["request_basis"]:
                failures.append("crypto_paper_intent_candidate_binding_changed")
            approval = conn.execute("SELECT * FROM crypto_paper_approvals WHERE id=? AND proposal_id=?", (row["approval_id"], row["proposal_id"])).fetchone()
            if approval is None or approval["status"] != "consumed":
                failures.append("crypto_paper_approval_not_consumed_or_bound")
            elif approval["display_fingerprint"] != proposal["display_fingerprint"]:
                failures.append("crypto_paper_approval_display_changed")
        if proposal is not None:
            try:
                stored_versions = json.loads(conn.execute("SELECT formula_versions_json FROM crypto_paper_proposals WHERE id=?", (row["proposal_id"],)).fetchone()["formula_versions_json"])
                if stored_versions != _formula_versions(config):
                    failures.append("crypto_paper_formula_identity_changed")
            except Exception:
                failures.append("crypto_paper_formula_identity_unavailable")
        return sorted(set(failures))

    @staticmethod
    def _expire_intent_locked(conn: Any, row: Mapping[str, Any], *, now: datetime, reason: str) -> dict[str, Any]:
        """Terminalise an unsubmitted expired attempt in one SQLite transaction.

        Expiry is a local authority decision.  It must not be represented as a
        retryable state (which could later turn into a submission), and it must
        release the reservation in the same transaction as the intent and
        approval transitions.
        """

        intent_id = str(row["id"])
        proposal_id = str(row["proposal_id"])
        approval_id = str(row["approval_id"])
        timestamp = now.isoformat()
        conn.execute(
            """UPDATE crypto_paper_intents
               SET state='expired', last_error=?, updated_at=?, terminal_at=?
               WHERE id=? AND broker_invocation_occurred=0""",
            (reason, timestamp, timestamp, intent_id),
        )
        conn.execute(
            "UPDATE crypto_paper_proposals SET status='expired' WHERE id=? AND status NOT IN ('filled','rejected','expired')",
            (proposal_id,),
        )
        conn.execute(
            "UPDATE crypto_paper_approvals SET status='expired' WHERE id=? AND status IN ('active','consumed')",
            (approval_id,),
        )
        conn.execute(
            """UPDATE crypto_paper_reservations
               SET active_notional='0', active_stop_risk='0', state='released',
                   released_at=?, release_reason='expired', updated_at=?
               WHERE intent_id=? AND state='active'""",
            (timestamp, timestamp, intent_id),
        )
        CryptoPaperLaneStore._refresh_reservation_fingerprint_locked(conn, intent_id)
        conn.execute(
            """INSERT OR IGNORE INTO crypto_paper_order_events(
                 id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at
             ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), intent_id, f"{intent_id}:expired", row["state"],
                "expired", "proposal_expired_before_broker", reason, timestamp,
            ),
        )
        refreshed = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
        return {
            **dict(refreshed),
            "state": "expired",
            "broker_invocation_occurred": 0,
            "broker_call": False,
            "last_error": reason,
        }

    def _reload_final_authority(
        self,
        row: Mapping[str, Any],
        config: Mapping[str, Any],
        broker: Any,
        *,
        now: datetime,
    ) -> list[str]:
        """Rebuild capability → market → risk → sizing immediately pre-submit."""

        failures: list[str] = []
        self._last_final_risk_snapshot_id = None
        try:
            proposal_rows = self.storage.fetch_all(
                "SELECT risk_snapshot_id,sizing_decision_id,config_hash FROM crypto_paper_proposals WHERE id=?",
                (row["proposal_id"],),
            )
            if len(proposal_rows) != 1:
                return ["final_crypto_authority_proposal_missing"]
            if str(proposal_rows[0]["config_hash"] or "") != str(config.get("effective_config_hash") or ""):
                return ["final_crypto_configuration_changed"]
            snapshot_rows = self.storage.fetch_all(
                "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=?",
                (proposal_rows[0]["risk_snapshot_id"],),
            )
            if len(snapshot_rows) != 1:
                return ["final_crypto_risk_snapshot_missing"]
            snapshot = json.loads(snapshot_rows[0]["snapshot_json"])
            request_payload = snapshot.get("request")
            if not isinstance(request_payload, Mapping):
                return ["final_crypto_risk_request_missing"]
            request = CryptoSizingRequest(**dict(request_payload))
            run_id = str(snapshot.get("run_id") or row["id"])
            capability = CryptoCapabilityStore(self.storage).capture(
                config, broker, run_id, now=now,
            )
            market = CryptoMarketDataStore(self.storage).capture(
                config, broker, capability, run_id, f"final-{row['id']}", str(row["symbol"]), now=now,
            )
            evaluation = CryptoRiskStore(self.storage).evaluate(
                config,
                broker,
                run_id,
                capability.id,
                market.id,
                request,
                now=now,
                exclude_crypto_intent_id=str(row["id"]),
            )
            self._last_final_risk_snapshot_id = str(evaluation.snapshot_id)
            if not evaluation.risk_eligible or not evaluation.sizing.eligible or not evaluation.sizing.authoritative:
                failures.append("final_crypto_risk_or_sizing_ineligible")
                failures.extend(f"final_crypto_risk:{reason}" for reason in evaluation.reasons)
                return sorted(set(failures))
            persisted = load_verified_crypto_sizing(
                self.storage, proposal_rows[0]["sizing_decision_id"], config,
            )
            fresh = evaluation.sizing
            fields = (
                "symbol", "side", "action", "request_basis", "canonical_quantity",
                "canonical_notional", "canonical_stop_risk", "limit_price", "stop_price",
            )
            for field in fields:
                if str(getattr(persisted, field, None)) != str(getattr(fresh, field, None)):
                    failures.append(f"final_crypto_sizing_changed:{field}")
            if str(evaluation.snapshot_id) == str(persisted.risk_snapshot_id):
                failures.append("final_crypto_risk_snapshot_was_not_rebuilt")
            self.storage.audit(
                str(snapshot.get("run_id") or row["id"]),
                "crypto_final_authority_reloaded",
                {
                    "intent_id": row["id"],
                    "persisted_risk_snapshot_id": persisted.risk_snapshot_id,
                    "fresh_risk_snapshot_id": evaluation.snapshot_id,
                    "fresh_market_evidence_id": market.id,
                    "fresh_capability_snapshot_id": capability.id,
                    "failures": sorted(set(failures)),
                },
            )
        except Exception as exc:
            failures.append(f"final_crypto_authority_reload_failed:{type(exc).__name__}")
        return sorted(set(failures))

    def execute_intent(self, intent_id: str, config: Mapping[str, Any], broker: Any, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        _policy(config)
        submit = getattr(broker, "submit_crypto_order", None)
        adapter_available = callable(submit)
        availability_probe = getattr(broker, "crypto_submission_available", None)
        if availability_probe is None:
            availability_probe = getattr(broker, "is_crypto_submission_available", None)
        if not callable(availability_probe):
            adapter_available = False
        else:
            try:
                adapter_available = adapter_available and availability_probe() is True
            except Exception:
                adapter_available = False
        if not adapter_available:
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
                if row is None:
                    raise CryptoPaperLaneError("crypto paper intent is missing")
                message = "crypto paper broker adapter is unavailable before invocation"
                conn.execute(
                    "UPDATE crypto_paper_intents SET state='retryable_pre_submission',broker_invocation_occurred=0,last_error=?,updated_at=? WHERE id=? AND broker_invocation_occurred=0",
                    (message, current.isoformat(), intent_id),
                )
                refreshed = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
            return {**dict(refreshed), "state": "retryable_pre_submission", "broker_invocation_occurred": 0, "last_error": message, "broker_call": False}
        initial_rows = self.storage.fetch_all("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,))
        if not initial_rows:
            raise CryptoPaperLaneError("crypto paper intent is missing")
        initial = dict(initial_rows[0])
        if int(initial.get("broker_invocation_occurred") or 0) == 1 or initial.get("state") in {
            "unknown", "reconciliation_required", "submitted", "partially_filled", "filled", "cancelled", "rejected", "expired",
        }:
            return initial
        final_authority_failures = self._reload_final_authority(
            initial, config, broker, now=current,
        )
        if final_authority_failures:
            reason = ";".join(final_authority_failures)
            self.storage.execute(
                "UPDATE crypto_paper_intents SET state='retryable_pre_submission',last_error=?,updated_at=? WHERE id=? AND broker_invocation_occurred=0",
                (reason, current.isoformat(), intent_id),
            )
            return {**initial, "state": "retryable_pre_submission", "broker_invocation_occurred": 0, "last_error": reason, "broker_call": False}
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise CryptoPaperLaneError("crypto paper intent is missing")
            row = dict(row)
            if int(row["broker_invocation_occurred"] or 0) == 1 or row["state"] in {"unknown", "reconciliation_required", "submitted", "partially_filled", "filled", "cancelled", "rejected", "expired"}:
                return row
            proposal_state = conn.execute(
                "SELECT status,expires_at FROM crypto_paper_proposals WHERE id=?", (row["proposal_id"],)
            ).fetchone()
            if proposal_state is None:
                raise CryptoPaperLaneError("crypto paper proposal is missing")
            if _utc(proposal_state["expires_at"], "crypto paper proposal expiry") <= current:
                return self._expire_intent_locked(
                    conn, row, now=current, reason="crypto_paper_proposal_expired_before_broker",
                )
            failures = self._final_controls(conn, row, config, broker, now=current)
            if failures:
                if "crypto_paper_proposal_expired" in failures:
                    return self._expire_intent_locked(
                        conn, row, now=current, reason="crypto_paper_proposal_expired_before_broker",
                    )
                conn.execute("UPDATE crypto_paper_intents SET state='retryable_pre_submission',last_error=?,updated_at=? WHERE id=?", (";".join(failures), current.isoformat(), intent_id))
                return {**row, "state": "retryable_pre_submission", "broker_invocation_occurred": 0, "last_error": ";".join(failures), "broker_call": False}
            conn.execute(
                "UPDATE crypto_paper_intents SET state='submitting',broker_invocation_occurred=1,submission_attempt_count=submission_attempt_count+1,first_submission_at=COALESCE(first_submission_at,?),updated_at=? WHERE id=?",
                (current.isoformat(), current.isoformat(), intent_id),
            )
            conn.execute("INSERT INTO crypto_paper_order_events(id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), intent_id, f"{intent_id}:submission:{row['submission_attempt_count'] + 1}", row["state"], "submitting", "broker_invocation_marked", "paper crypto broker invocation may occur", current.isoformat()))
        try:
            basis = {"qty": str(row["requested_quantity"])} if row["request_basis"] == "quantity" else {"notional": str(row["requested_notional"])}
            response = submit(
                str(row["symbol"]), str(row["side"]), basis, "limit", limit_price=str(row["limit_price"]),
                client_order_id=str(row["client_order_id"]), time_in_force=str((_policy(config))["time_in_force"]),
            )
        except BrokerSubmissionNotAttempted as exc:
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE crypto_paper_intents SET state='retryable_pre_submission',broker_invocation_occurred=0,last_error=?,updated_at=? WHERE id=?", (str(exc), current.isoformat(), intent_id))
            return {**row, "state": "retryable_pre_submission", "broker_invocation_occurred": 0, "last_error": str(exc), "broker_call": False}
        except Exception as exc:
            with self.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE crypto_paper_intents SET state='unknown',last_error=?,updated_at=? WHERE id=?", (type(exc).__name__, current.isoformat(), intent_id))
            return {**row, "state": "unknown", "broker_invocation_occurred": 1, "last_error": type(exc).__name__, "broker_call": True}
        broker_order_id = str(_value(response, "id", "order_id", default="") or "")
        status = _enum(_value(response, "status", default="accepted")) or "accepted"
        # A submit response is not fill evidence.  Even if the adapter says
        # ``filled`` or ``partially_filled``, keep the durable order at
        # ``submitted`` until a client-order reconciliation returns verified
        # paper-account evidence and FIFO accounting accepts the fill.
        mapped_state = {
            "rejected": "rejected", "canceled": "cancelled", "cancelled": "cancelled", "expired": "expired",
        }.get(status, "submitted")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE crypto_paper_intents SET state=?,broker_order_id=?,updated_at=?,terminal_at=CASE WHEN ? IN ('filled','rejected','cancelled','expired') THEN ? ELSE terminal_at END WHERE id=?", (mapped_state, broker_order_id or None, current.isoformat(), mapped_state, current.isoformat(), intent_id))
            conn.execute("UPDATE crypto_paper_proposals SET status=? WHERE id=?", (mapped_state, row["proposal_id"]))
            if mapped_state in TERMINAL_INTENT_STATES:
                conn.execute("UPDATE crypto_paper_reservations SET active_notional='0',active_stop_risk='0',state='released',released_at=?,release_reason=?,updated_at=? WHERE intent_id=?", (current.isoformat(), mapped_state, current.isoformat(), intent_id))
                self._refresh_reservation_fingerprint_locked(conn, intent_id)
        return {
            **row, "state": mapped_state, "broker_invocation_occurred": 1,
            "broker_order_id": broker_order_id, "broker_call": True,
            "broker_status": status, "response": response,
        }

    @staticmethod
    def _sum_fill_quantity(conn: Any, intent_id: str) -> Decimal:
        rows = conn.execute(
            "SELECT quantity FROM crypto_paper_fills WHERE intent_id=? ORDER BY occurred_at,id",
            (intent_id,),
        ).fetchall()
        return sum((_decimal(row["quantity"], "crypto persisted fill quantity", minimum=ZERO) for row in rows), ZERO)

    @staticmethod
    def _refresh_reservation_fingerprint_locked(conn: Any, intent_id: str) -> None:
        row = conn.execute(
            "SELECT * FROM crypto_paper_reservations WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            raise CryptoPaperLaneError("crypto paper reservation is missing")
        payload = dict(row)
        conn.execute(
            "UPDATE crypto_paper_reservations SET reservation_fingerprint=? WHERE id=?",
            (_reservation_fingerprint(payload), payload["id"]),
        )

    @staticmethod
    def _refresh_lot_fingerprint_locked(conn: Any, lot_id: str) -> None:
        row = conn.execute(
            "SELECT * FROM crypto_paper_lots WHERE id=?", (lot_id,)
        ).fetchone()
        if row is None:
            raise CryptoPaperLaneError("crypto paper lot is missing")
        payload = dict(row)
        conn.execute(
            "UPDATE crypto_paper_lots SET lot_fingerprint=? WHERE id=?",
            (_crypto_lot_fingerprint(payload), payload["id"]),
        )

    @staticmethod
    def _position_lot_decimal(row: Mapping[str, Any], canonical: str, legacy: str) -> Decimal:
        value = row.get(canonical)
        if value in (None, ""):
            raise CryptoPaperLaneError(
                f"crypto lifecycle exact field is missing: {canonical}"
            )
        return _decimal(value, f"crypto lifecycle {canonical}")

    @classmethod
    def _crypto_lot_geometry_locked(
        cls, conn: Any, symbol: str, *, lifecycle_id: str | None = None
    ) -> tuple[Decimal, Decimal, Decimal, str | None]:
        """Read the shared exact FIFO lots that define a crypto holding."""

        if lifecycle_id:
            rows = conn.execute(
                "SELECT * FROM position_lots WHERE symbol=? AND position_lifecycle_id=? ORDER BY opened_at,id",
                (symbol, lifecycle_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM position_lots WHERE symbol=? ORDER BY opened_at,id",
                (symbol,),
            ).fetchall()
        original = ZERO
        remaining = ZERO
        remaining_cost = ZERO
        original_cost = ZERO
        opened_at: str | None = None
        for raw in rows:
            row = dict(raw)
            lot_original = cls._position_lot_decimal(row, "original_quantity_decimal", "original_quantity")
            lot_remaining = cls._position_lot_decimal(row, "remaining_quantity_decimal", "remaining_quantity")
            lot_cost = cls._position_lot_decimal(row, "unit_cost_decimal", "unit_cost")
            if lot_original <= ZERO or lot_remaining < ZERO or lot_remaining > lot_original or lot_cost <= ZERO:
                raise CryptoPaperLaneError("crypto lifecycle FIFO lot geometry is invalid")
            original += lot_original
            remaining += lot_remaining
            original_cost += lot_original * lot_cost
            remaining_cost += lot_remaining * lot_cost
            if opened_at is None or str(row.get("opened_at") or "") < opened_at:
                opened_at = str(row.get("opened_at") or "")
        average = (remaining_cost / remaining) if remaining > ZERO else (
            original_cost / original if original > ZERO else ZERO
        )
        return original, remaining, average, opened_at

    @classmethod
    def _ensure_crypto_position_lifecycle_locked(
        cls,
        conn: Any,
        *,
        intent: Mapping[str, Any],
        quantity: Decimal,
        price: Decimal,
        occurred_at: datetime,
    ) -> str:
        """Bind a verified crypto fill to one exact shared position lifecycle.

        The lifecycle is created or recovered before the FIFO write so the lot
        and its later consumption carry the same identity atomically.  A
        missing or contradictory basis is rejected; this helper never invents
        a holding from a broker quote or an unverified position.
        """

        symbol = str(intent["symbol"]).upper().replace("-", "/")
        side = str(intent["side"]).lower()
        if side not in {"buy", "sell"}:
            raise CryptoPaperLaneError("crypto lifecycle side is invalid")
        active_rows = conn.execute(
            "SELECT * FROM position_lifecycles WHERE symbol=? AND state='active' ORDER BY id",
            (symbol,),
        ).fetchall()
        if len(active_rows) > 1:
            raise CryptoPaperLaneError("crypto symbol has duplicate active position lifecycles")
        lifecycle: dict[str, Any] | None = dict(active_rows[0]) if active_rows else None

        if side == "sell" and lifecycle is None:
            # Recovery for a pre-lifecycle crypto lot is allowed only when the
            # shared exact FIFO ledger supplies a positive, unambiguous basis.
            lot_rows = conn.execute(
                "SELECT * FROM position_lots WHERE symbol=? AND COALESCE(remaining_quantity_decimal,'0')<>'0' ORDER BY opened_at,id",
                (symbol,),
            ).fetchall()
            if not lot_rows:
                raise CryptoPaperLaneError("crypto sell fill has no verified active FIFO basis")
            lifecycle_ids = {str(row["position_lifecycle_id"]) for row in lot_rows if row["position_lifecycle_id"]}
            if len(lifecycle_ids) > 1:
                raise CryptoPaperLaneError("crypto sell fill has conflicting FIFO lifecycle identities")
            if lifecycle_ids:
                recovered = conn.execute(
                    "SELECT * FROM position_lifecycles WHERE id=?", (next(iter(lifecycle_ids)),)
                ).fetchone()
                if recovered is None or str(recovered["state"]) != "active":
                    raise CryptoPaperLaneError("crypto sell fill references a non-active FIFO lifecycle")
                lifecycle = dict(recovered)
            else:
                original, remaining, average, opened_at = cls._crypto_lot_geometry_locked(conn, symbol)
                if remaining <= ZERO or not opened_at:
                    raise CryptoPaperLaneError("crypto sell fill has no positive recoverable holding")
                lifecycle_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO position_lifecycles(
                       id,symbol,broker_position_id,side,state,opened_at,opening_quantity,current_quantity,
                       average_entry_price,source,created_at,updated_at,
                       opening_quantity_decimal,current_quantity_decimal,average_entry_price_decimal,
                       decimal_provenance,decimal_accounting_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        lifecycle_id, symbol, None, "long", "active", opened_at,
                        float(original), float(remaining), float(average), "crypto_paper_recovered",
                        occurred_at.isoformat(), occurred_at.isoformat(),
                        _text(original), _text(remaining), _text(average),
                        "exact_source_decimal", "fixed_point_fifo_accounting_v1",
                    ),
                )
                conn.execute(
                    "UPDATE position_lots SET position_lifecycle_id=?,updated_at=? WHERE symbol=? AND position_lifecycle_id IS NULL AND COALESCE(remaining_quantity_decimal,'0')<>'0'",
                    (lifecycle_id, occurred_at.isoformat(), symbol),
                )
                conn.execute(
                    "UPDATE crypto_paper_lots SET position_lifecycle_id=? WHERE symbol=? AND position_lifecycle_id IS NULL",
                    (lifecycle_id, symbol),
                )
                for raw_lot in conn.execute(
                    "SELECT id FROM crypto_paper_lots WHERE symbol=? AND position_lifecycle_id=?",
                    (symbol, lifecycle_id),
                ).fetchall():
                    cls._refresh_lot_fingerprint_locked(conn, str(raw_lot["id"]))
                lifecycle = dict(conn.execute("SELECT * FROM position_lifecycles WHERE id=?", (lifecycle_id,)).fetchone())

        if lifecycle is None:
            # A new entry starts a lifecycle.  The exact values are updated
            # again after the FIFO insert, but the identity is already present
            # when the lot is written.
            lifecycle_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO position_lifecycles(
                   id,symbol,broker_position_id,side,state,opened_at,opening_quantity,current_quantity,
                   average_entry_price,source,created_at,updated_at,
                   opening_quantity_decimal,current_quantity_decimal,average_entry_price_decimal,
                   decimal_provenance,decimal_accounting_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lifecycle_id, symbol, None, "long", "active", occurred_at.isoformat(),
                    float(quantity), float(quantity), float(price), "crypto_paper_lane",
                    occurred_at.isoformat(), occurred_at.isoformat(),
                    _text(quantity), _text(quantity), _text(price),
                    "exact_source_decimal", "fixed_point_fifo_accounting_v1",
                ),
            )
            lifecycle = dict(conn.execute("SELECT * FROM position_lifecycles WHERE id=?", (lifecycle_id,)).fetchone())
        elif str(lifecycle.get("side") or "").lower() != "long":
            raise CryptoPaperLaneError("crypto lifecycle is not long-only")

        lifecycle_id = str(lifecycle["id"])
        if side == "sell":
            _original, current_quantity, _average, _opened_at = cls._crypto_lot_geometry_locked(
                conn, symbol, lifecycle_id=lifecycle_id
            )
            if current_quantity < quantity:
                raise CryptoPaperLaneError("crypto sell fill exceeds lifecycle quantity")
        conn.execute(
            "UPDATE crypto_paper_intents SET position_lifecycle_id=?,updated_at=? WHERE id=?",
            (lifecycle_id, occurred_at.isoformat(), intent["id"]),
        )
        return lifecycle_id

    @classmethod
    def _refresh_crypto_position_lifecycle_locked(
        cls, conn: Any, *, lifecycle_id: str, symbol: str, occurred_at: datetime
    ) -> dict[str, Any]:
        lifecycle_row = conn.execute(
            "SELECT * FROM position_lifecycles WHERE id=?", (lifecycle_id,)
        ).fetchone()
        if lifecycle_row is None:
            raise CryptoPaperLaneError("crypto lifecycle disappeared during fill accounting")
        original, remaining, average, _opened_at = cls._crypto_lot_geometry_locked(
            conn, symbol, lifecycle_id=lifecycle_id
        )
        if original <= ZERO:
            raise CryptoPaperLaneError("crypto lifecycle has no FIFO lots")
        state = "active" if remaining > ZERO else "closed"
        closed_at = None if state == "active" else occurred_at.isoformat()
        conn.execute(
            """UPDATE position_lifecycles
               SET state=?,closed_at=?,opening_quantity=?,current_quantity=?,average_entry_price=?,updated_at=?,
                   opening_quantity_decimal=?,current_quantity_decimal=?,average_entry_price_decimal=?,
                   decimal_provenance=?,decimal_accounting_version=?
               WHERE id=?""",
            (
                state, closed_at, float(original), float(remaining), float(average), occurred_at.isoformat(),
                _text(original), _text(remaining), _text(average),
                "exact_source_decimal", "fixed_point_fifo_accounting_v1", lifecycle_id,
            ),
        )
        refreshed = conn.execute("SELECT * FROM position_lifecycles WHERE id=?", (lifecycle_id,)).fetchone()
        return dict(refreshed)

    @staticmethod
    def _evidence_payload(evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Return the immutable, non-secret broker response envelope."""

        payload: dict[str, Any] = {}
        for key, value in sorted(evidence.items()):
            if key in {"raw", "response", "payload"}:
                continue
            if isinstance(value, Decimal):
                payload[key] = _text(value)
            elif isinstance(value, (str, int, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = str(value)
        raw = evidence.get("payload")
        if isinstance(raw, Mapping):
            payload["payload"] = dict(raw)
        return payload

    def _record_order_evidence_locked(
        self,
        conn: Any,
        *,
        intent: Mapping[str, Any],
        evidence: Mapping[str, Any],
        captured_at: datetime,
    ) -> tuple[str, str]:
        payload = self._evidence_payload(evidence)
        fingerprint = _hash(payload)
        existing = conn.execute(
            "SELECT id,intent_id,broker_order_id,client_order_id FROM crypto_paper_order_evidence WHERE payload_fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if existing is not None:
            if str(existing["intent_id"]) != str(intent["id"]):
                raise CryptoPaperLaneError("crypto broker evidence payload is reused across intents")
            return str(existing["id"]), fingerprint
        evidence_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO crypto_paper_order_evidence(
               id,intent_id,broker_order_id,client_order_id,symbol,side,status,
               requested_quantity,requested_notional,filled_quantity,filled_average_price,fees,
               payload,payload_fingerprint,captured_at,verified,verification_error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id, intent["id"], str(evidence.get("broker_order_id") or ""),
                str(evidence.get("client_order_id") or ""), str(evidence.get("symbol") or ""),
                str(evidence.get("side") or ""), str(evidence.get("status") or "unknown"),
                evidence.get("requested_quantity"), evidence.get("requested_notional"),
                str(evidence.get("cumulative_filled_quantity") or "0"), evidence.get("filled_average_price"),
                str(evidence.get("fees") or "0"), json_dumps(payload), fingerprint, captured_at.isoformat(),
                int(bool(evidence.get("verified"))), evidence.get("verification_error"),
            ),
        )
        return evidence_id, fingerprint

    def _link_fill_to_performance_locked(
        self,
        conn: Any,
        *,
        fill_id: str,
        intent: Mapping[str, Any],
        broker_event_key: str,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        evidence_fingerprint: str,
        realized_pl: str | None,
        order_status: str,
        occurred_at: datetime,
    ) -> None:
        """Bind a verified crypto fill to an auditable Performance Lab row.

        A proposal or a paper fill is never treated as an actual outcome by
        inference.  This link is written only after broker identity, account
        identity, cumulative quantity and FIFO accounting have all passed in
        the surrounding transaction.  Entry fills promote the matching shadow
        setup; exits get their own actual-fill setup so an exit cannot rewrite
        the entry evidence.
        """

        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('performance_setups','performance_outcomes')"
            ).fetchall()
        }
        if not {"performance_setups", "performance_outcomes"}.issubset(tables):
            return
        proposal = conn.execute(
            "SELECT run_id,strategy_decision_id,symbol,side,action,display_json FROM crypto_paper_proposals WHERE id=?",
            (intent["proposal_id"],),
        ).fetchone()
        if proposal is None:
            raise CryptoPaperLaneError("crypto performance link proposal is missing")
        run_id = str(proposal["run_id"] or intent.get("id") or "")
        symbol = str(proposal["symbol"] or intent["symbol"]).upper()
        side = str(proposal["side"] or intent["side"]).lower()
        action = str(proposal["action"] or intent.get("action") or "").lower()
        strategy_decision_id = str(proposal["strategy_decision_id"] or "")
        fill_type = "entry" if side == "buy" else "exit"
        setup: Any | None = None
        if fill_type == "entry":
            candidates = conn.execute(
                """SELECT * FROM performance_setups
                   WHERE asset_class='crypto' AND symbol=? AND run_id=?
                   ORDER BY created_at DESC,id DESC""",
                (symbol, run_id),
            ).fetchall()
            for candidate in candidates:
                signal_state = {}
                try:
                    signal_state = json.loads(candidate["signal_state"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    signal_state = {}
                if strategy_decision_id and str(signal_state.get("strategy_decision_id") or "") == strategy_decision_id:
                    setup = candidate
                    break
            if setup is None and candidates:
                setup = candidates[0]

        # A broker order may arrive as several partial fills.  Performance Lab
        # stores the cumulative actual execution for the intent, while the
        # immutable crypto_performance_links table retains one row per fill.
        fill_rows = conn.execute(
            "SELECT quantity,price FROM crypto_paper_fills WHERE intent_id=? ORDER BY received_at,id",
            (intent["id"],),
        ).fetchall()
        cumulative_quantity = sum(
            (_decimal(row["quantity"], "crypto Performance Lab fill quantity", positive=True) for row in fill_rows),
            ZERO,
        )
        cumulative_notional = sum(
            (
                _decimal(row["quantity"], "crypto Performance Lab fill quantity", positive=True)
                * _decimal(row["price"], "crypto Performance Lab fill price", positive=True)
                for row in fill_rows
            ),
            ZERO,
        )
        cumulative_average_price = cumulative_notional / cumulative_quantity
        entry_notional = _text(cumulative_notional)
        entry_price = _text(cumulative_average_price)
        entry_quantity = _text(cumulative_quantity)
        if setup is None:
            setup_id = str(uuid.uuid4())
            setup_type = "entry" if fill_type == "entry" else "exit"
            signal_state = json_dumps({
                "source": "verified_crypto_paper_fill",
                "strategy_decision_id": strategy_decision_id or None,
                "broker_event_key": broker_event_key,
                "evidence_fingerprint": evidence_fingerprint,
            })
            conn.execute(
                """INSERT INTO performance_setups(
                   id,timestamp,run_id,symbol,asset_class,tier,setup_type,action_decision,
                   proposed,proposal_id,not_proposed_reason,signal_state,current_price,
                   proposed_notional,created_at,updated_at,broker_order_id,fill_id,
                   fill_price,fill_qty,order_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    setup_id, occurred_at.isoformat(), run_id, symbol, "crypto",
                    "PAPER_ACTIVE", setup_type, "actual_fill", 1, intent["proposal_id"],
                    None, signal_state, entry_price, entry_notional,
                    occurred_at.isoformat(), occurred_at.isoformat(),
                    str(intent.get("broker_order_id") or "") or None, fill_id,
                    entry_price, entry_quantity, order_status,
                ),
            )
            setup = conn.execute(
                "SELECT * FROM performance_setups WHERE id=?", (setup_id,)
            ).fetchone()
        else:
            setup_id = str(setup["id"])
            conn.execute(
                """UPDATE performance_setups
                   SET proposed=1,proposal_id=COALESCE(proposal_id,?),
                       broker_order_id=COALESCE(broker_order_id,?),fill_id=COALESCE(fill_id,?),
                       fill_price=?,fill_qty=?,
                       order_status=?,updated_at=?
                   WHERE id=?""",
                (
                    intent["proposal_id"], str(intent.get("broker_order_id") or "") or None,
                    fill_id, entry_price, entry_quantity, order_status,
                    occurred_at.isoformat(), setup_id,
                ),
            )

        outcome = conn.execute(
            "SELECT * FROM performance_outcomes WHERE setup_id=?", (setup_id,)
        ).fetchone()
        if outcome is None:
            outcome_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO performance_outcomes(
                   id,setup_id,run_id,symbol,proposal_id,broker_order_id,fill_id,
                   actual_or_shadow,entry_time,entry_price,entry_notional,entry_qty,
                   status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome_id, setup_id, run_id, symbol, intent["proposal_id"],
                    str(intent.get("broker_order_id") or "") or None, fill_id,
                    "actual_fill", occurred_at.isoformat(), entry_price,
                    entry_notional, entry_quantity, "actual_fill",
                    occurred_at.isoformat(), occurred_at.isoformat(),
                ),
            )
        else:
            outcome_id = str(outcome["id"])
            conn.execute(
                """UPDATE performance_outcomes
                   SET actual_or_shadow='actual_fill',proposal_id=COALESCE(proposal_id,?),
                       broker_order_id=COALESCE(broker_order_id,?),fill_id=COALESCE(fill_id,?),
                       entry_time=COALESCE(entry_time,?),entry_price=?,
                       entry_notional=?,entry_qty=?,
                       status='actual_fill',updated_at=?
                   WHERE setup_id=?""",
                (
                    intent["proposal_id"], str(intent.get("broker_order_id") or "") or None,
                    fill_id, occurred_at.isoformat(), entry_price, entry_notional,
                    entry_quantity, occurred_at.isoformat(), setup_id,
                ),
            )

        link_payload = {
            "fill_id": fill_id,
            "intent_id": str(intent["id"]),
            "setup_id": setup_id,
            "outcome_id": outcome_id,
            "broker_order_id": str(intent.get("broker_order_id") or ""),
            "evidence_fingerprint": evidence_fingerprint,
            "side": side,
            "action": action,
            "quantity": _text(quantity),
            "price": _text(price),
            "fees": _text(fees),
            "fill_type": fill_type,
            "position_lifecycle_id": str(intent.get("position_lifecycle_id") or ""),
            "realized_pl": realized_pl,
            "order_status": order_status,
        }
        link_fingerprint = _hash(link_payload)
        existing = conn.execute(
            "SELECT link_fingerprint,evidence_fingerprint FROM crypto_performance_links WHERE fill_id=?",
            (fill_id,),
        ).fetchone()
        if existing is not None:
            if existing["link_fingerprint"] != link_fingerprint or existing["evidence_fingerprint"] != evidence_fingerprint:
                raise CryptoPaperLaneError("conflicting crypto Performance Lab fill link")
            return
        columns = {
            str(item[1])
            for item in conn.execute("PRAGMA table_info(crypto_performance_links)").fetchall()
        }
        if not {"side", "action", "quantity", "price", "fees", "fill_type", "position_lifecycle_id", "order_status"}.issubset(columns):
            raise CryptoPaperLaneError("crypto Performance Lab link schema is incomplete")
        conn.execute(
            """INSERT INTO crypto_performance_links(
               id,fill_id,intent_id,setup_id,outcome_id,broker_order_id,evidence_fingerprint,
               realized_pl,link_fingerprint,created_at,side,action,quantity,price,fees,fill_type,position_lifecycle_id,order_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), fill_id, intent["id"], setup_id, outcome_id,
                str(intent.get("broker_order_id") or ""), evidence_fingerprint,
                realized_pl, link_fingerprint, occurred_at.isoformat(), side, action,
                _text(quantity), _text(price), _text(fees), fill_type,
                str(intent.get("position_lifecycle_id") or "") or None,
                order_status,
            ),
        )

    def _record_verified_fill_locked(
        self,
        conn: Any,
        *,
        intent: Mapping[str, Any],
        broker_event_key: str,
        quantity: Any,
        price: Any,
        fees: Any,
        evidence: Mapping[str, Any],
        config: Mapping[str, Any],
        occurred_at: datetime,
    ) -> dict[str, Any]:
        intent = dict(intent)
        if evidence.get("verified") is not True:
            raise CryptoPaperLaneError("verified broker evidence is required for a crypto fill")
        if int(intent["broker_invocation_occurred"] or 0) != 1:
            raise CryptoPaperLaneError("crypto fill cannot precede a broker invocation")
        broker_order_id = str(evidence.get("broker_order_id") or "")
        client_order_id = str(evidence.get("client_order_id") or "")
        symbol = str(evidence.get("symbol") or "").upper().replace("-", "/")
        side = str(evidence.get("side") or "").lower()
        evidence_status = _enum(evidence.get("status"))
        if evidence_status not in {"partially_filled", "partial_fill", "filled"}:
            raise CryptoPaperLaneError("crypto fill evidence is not a broker fill event")
        if not broker_order_id or not client_order_id:
            raise CryptoPaperLaneError("crypto fill broker order identity is missing")
        if client_order_id != str(intent["client_order_id"]):
            raise CryptoPaperLaneError("crypto fill client order identity differs from intent")
        if intent.get("broker_order_id") and broker_order_id != str(intent["broker_order_id"]):
            raise CryptoPaperLaneError("crypto fill broker order identity differs from intent")
        if symbol != str(intent["symbol"]).upper() or side != str(intent["side"]).lower():
            raise CryptoPaperLaneError("crypto fill symbol or side differs from intent")
        quantity_d = _decimal(quantity, "crypto fill quantity", positive=True)
        price_d = _decimal(price, "crypto fill price", positive=True)
        fees_d = _decimal(fees, "crypto fill fees")
        requested = _decimal(intent["requested_quantity"], "requested crypto quantity", positive=True)
        cumulative = _decimal(evidence.get("cumulative_filled_quantity"), "crypto cumulative fill quantity", positive=True)
        proposal_row = conn.execute(
            "SELECT display_json,config_hash FROM crypto_paper_proposals WHERE id=?",
            (intent["proposal_id"],),
        ).fetchone()
        if proposal_row is None:
            raise CryptoPaperLaneError("crypto fill proposal is missing")
        snapshot_row = conn.execute(
            "SELECT snapshot_json FROM crypto_risk_snapshots WHERE id=(SELECT risk_snapshot_id FROM crypto_paper_proposals WHERE id=?)",
            (intent["proposal_id"],),
        ).fetchone()
        if not evidence.get("paper_account_id_hash") or snapshot_row is None:
            raise CryptoPaperLaneError("crypto fill lacks verified paper-account identity evidence")
        if evidence.get("paper_account_id_hash") and snapshot_row is not None:
            try:
                snapshot = json.loads(snapshot_row["snapshot_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CryptoPaperLaneError("crypto fill risk snapshot is invalid") from exc
            expected_account = str((snapshot.get("account") or {}).get("paper_account_id_hash") or "")
            if expected_account != str(evidence["paper_account_id_hash"]):
                raise CryptoPaperLaneError("crypto fill paper-account identity differs")
        evidence_id, evidence_fingerprint = self._record_order_evidence_locked(
            conn, intent=intent, evidence=evidence, captured_at=occurred_at,
        )
        payload = {
            "broker_event_key": str(broker_event_key),
            "broker_order_id": broker_order_id, "client_order_id": client_order_id,
            "symbol": symbol, "side": side, "quantity": _text(quantity_d),
            "price": _text(price_d), "fees": _text(fees_d),
            "cumulative_filled_quantity": _text(cumulative),
            "evidence_id": evidence_id, "evidence_fingerprint": evidence_fingerprint,
            "broker_payload": self._evidence_payload(evidence),
        }
        payload_fingerprint = _hash(payload)
        existing = conn.execute(
            "SELECT id,payload_fingerprint,quantity,price,fees,evidence_fingerprint FROM crypto_paper_fills WHERE broker_event_key=?",
            (broker_event_key,),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload_fingerprint"] != payload_fingerprint
                or existing["quantity"] != _text(quantity_d)
                or existing["price"] != _text(price_d)
                or existing["fees"] != _text(fees_d)
                or existing["evidence_fingerprint"] != evidence_fingerprint
            ):
                raise CryptoPaperLaneError("conflicting duplicate crypto fill payload")
            return {"status": "duplicate", "fill_id": existing["id"], "state": intent["state"]}
        total = self._sum_fill_quantity(conn, str(intent["id"]))
        expected_cumulative = total + quantity_d
        if cumulative != expected_cumulative:
            raise CryptoPaperLaneError("crypto fill cumulative quantity does not reconcile exactly")
        if expected_cumulative > requested:
            raise CryptoPaperLaneError("crypto fills exceed requested quantity")
        fill_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO crypto_paper_fills(
               id,intent_id,broker_event_key,quantity,price,fees,occurred_at,received_at,
               payload,broker_order_id,client_order_id,evidence_id,evidence_fingerprint,payload_fingerprint
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fill_id, intent["id"], broker_event_key, _text(quantity_d), _text(price_d), _text(fees_d),
                occurred_at.isoformat(), iso_now(), json_dumps(payload), broker_order_id, client_order_id,
                evidence_id, evidence_fingerprint, payload_fingerprint,
            ),
        )
        # Keep the shared fixed-point FIFO ledger in the same transaction. A
        # missing basis or oversell aborts the transaction and leaves the
        # broker evidence available only as a reconciliation failure.
        from .lot_ledger import LotLedger

        lifecycle_id = self._ensure_crypto_position_lifecycle_locked(
            conn,
            intent=intent,
            quantity=quantity_d,
            price=price_d,
            occurred_at=occurred_at,
        )
        intent["position_lifecycle_id"] = lifecycle_id
        ledger_intent = dict(intent)
        ledger_intent["position_lifecycle_id"] = lifecycle_id
        ledger_intent.setdefault("approved_quantity", ledger_intent.get("requested_quantity"))
        try:
            proposal_display = json.loads(proposal_row["display_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            proposal_display = {}
        ledger_intent.setdefault("strategy_version", proposal_display.get("strategy"))
        ledger_intent.setdefault("entry_regime", proposal_display.get("volatility_regime"))
        ledger_intent.setdefault("initial_risk_dollars", ledger_intent.get("reserved_stop_risk"))
        ledger_intent.setdefault("config_hash", proposal_row["config_hash"])
        ledger_intent.setdefault("formula_version", CRYPTO_PAPER_EXECUTION_FORMULA_VERSION)
        LotLedger.apply_fill_in_transaction(
            conn, intent=ledger_intent, broker_event_key=broker_event_key,
            delta_quantity=quantity_d, fill_price=price_d, occurred_at=occurred_at.isoformat(),
            fees=fees_d, adjustments=ZERO, source="crypto_paper_fill", accounting_timezone="UTC",
        )
        if str(intent["side"]).lower() == "sell":
            remaining_to_consume = quantity_d
            lots = conn.execute(
                "SELECT id,remaining_quantity FROM crypto_paper_lots WHERE symbol=? AND position_lifecycle_id=? AND remaining_quantity<>'0' ORDER BY opened_at,id",
                (intent["symbol"], lifecycle_id),
            ).fetchall()
            for lot in lots:
                if remaining_to_consume <= ZERO:
                    break
                lot_remaining = _decimal(lot["remaining_quantity"], "crypto paper lot remaining quantity", positive=True)
                consumed = min(remaining_to_consume, lot_remaining)
                conn.execute("UPDATE crypto_paper_lots SET remaining_quantity=? WHERE id=?", (_text(lot_remaining - consumed), lot["id"]))
                self._refresh_lot_fingerprint_locked(conn, str(lot["id"]))
                remaining_to_consume -= consumed
            if remaining_to_consume != ZERO:
                raise CryptoPaperLaneError("crypto sell fill exceeds verified local lot quantity")
            realized_row = conn.execute(
                """SELECT symbol,quantity_decimal,gross_proceeds_decimal,cost_basis_decimal,
                          fees_decimal,realized_pl_decimal,occurred_at
                   FROM realized_pnl_events WHERE broker_event_key=?""",
                (broker_event_key,),
            ).fetchone()
            if (
                realized_row is None
                or realized_row["cost_basis_decimal"] is None
                or realized_row["realized_pl_decimal"] is None
            ):
                raise CryptoPaperLaneError("crypto sell fill lacks verified realized P&L basis")
            pnl_id = str(uuid.uuid4())
            pnl_created_at = iso_now()
            pnl_payload = {
                "id": pnl_id,
                "broker_event_key": broker_event_key,
                "intent_id": intent["id"],
                "position_lifecycle_id": lifecycle_id,
                "symbol": realized_row["symbol"],
                "quantity": realized_row["quantity_decimal"],
                "gross_proceeds": realized_row["gross_proceeds_decimal"],
                "cost_basis": realized_row["cost_basis_decimal"],
                "fees": realized_row["fees_decimal"],
                "realized_pl": realized_row["realized_pl_decimal"],
                "occurred_at": realized_row["occurred_at"],
                "created_at": pnl_created_at,
                "evidence_fingerprint": evidence_fingerprint,
                "confidence": "verified",
            }
            conn.execute(
                """INSERT INTO crypto_paper_realized_pnl(
                   id,broker_event_key,intent_id,symbol,quantity,gross_proceeds,cost_basis,
                   fees,realized_pl,occurred_at,created_at,evidence_fingerprint,confidence,
                   position_lifecycle_id,pnl_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pnl_id, broker_event_key, intent["id"], realized_row["symbol"],
                    realized_row["quantity_decimal"], realized_row["gross_proceeds_decimal"], realized_row["cost_basis_decimal"],
                    realized_row["fees_decimal"], realized_row["realized_pl_decimal"], realized_row["occurred_at"],
                    pnl_created_at, evidence_fingerprint, "verified", lifecycle_id,
                    _crypto_pnl_fingerprint(pnl_payload),
                ),
            )
        else:
            lot_id = str(uuid.uuid4())
            lot_created_at = iso_now()
            lot_payload = {
                "id": lot_id,
                "symbol": intent["symbol"],
                "source_fill_event_key": broker_event_key,
                "opened_at": occurred_at.isoformat(),
                "original_quantity": _text(quantity_d),
                "remaining_quantity": _text(quantity_d),
                "unit_cost": _text(price_d),
                "fees_allocated": _text(fees_d),
                "created_at": lot_created_at,
                "position_lifecycle_id": lifecycle_id,
            }
            conn.execute(
                """INSERT INTO crypto_paper_lots(
                   id,symbol,source_fill_event_key,opened_at,original_quantity,remaining_quantity,
                   unit_cost,fees_allocated,created_at,position_lifecycle_id,lot_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lot_id, intent["symbol"], broker_event_key, occurred_at.isoformat(),
                    _text(quantity_d), _text(quantity_d), _text(price_d), _text(fees_d),
                    lot_created_at, lifecycle_id, _crypto_lot_fingerprint(lot_payload),
                ),
            )
        self._refresh_crypto_position_lifecycle_locked(
            conn, lifecycle_id=lifecycle_id, symbol=str(intent["symbol"]).upper(), occurred_at=occurred_at,
        )
        new_state = "filled" if expected_cumulative == requested else "partially_filled"
        realized_pl_text: str | None = None
        if str(intent["side"]).lower() == "sell":
            realized_link_row = conn.execute(
                "SELECT realized_pl FROM crypto_paper_realized_pnl WHERE broker_event_key=?",
                (broker_event_key,),
            ).fetchone()
            if realized_link_row is not None:
                realized_pl_text = str(realized_link_row["realized_pl"])
        self._link_fill_to_performance_locked(
            conn,
            fill_id=fill_id,
            intent=intent,
            broker_event_key=broker_event_key,
            quantity=quantity_d,
            price=price_d,
            fees=fees_d,
            evidence_fingerprint=evidence_fingerprint,
            realized_pl=realized_pl_text,
            order_status=new_state,
            occurred_at=occurred_at,
        )
        conn.execute(
            "UPDATE crypto_paper_intents SET state=?,updated_at=?,terminal_at=CASE WHEN ?='filled' THEN ? ELSE terminal_at END WHERE id=?",
            (new_state, occurred_at.isoformat(), new_state, occurred_at.isoformat(), intent["id"]),
        )
        reservation = conn.execute(
            "SELECT initial_notional,initial_stop_risk FROM crypto_paper_reservations WHERE intent_id=?",
            (intent["id"],),
        ).fetchone()
        if reservation is None:
            raise CryptoPaperLaneError("crypto fill reservation is missing")
        if new_state == "filled":
            conn.execute(
                """UPDATE crypto_paper_reservations SET active_notional='0',active_stop_risk='0',
                   state='released',released_at=?,release_reason='filled',updated_at=? WHERE intent_id=?""",
                (occurred_at.isoformat(), occurred_at.isoformat(), intent["id"]),
            )
        else:
            remaining_ratio = (requested - expected_cumulative) / requested
            active_notional = _decimal(reservation["initial_notional"], "initial crypto reservation notional") * remaining_ratio
            active_stop = _decimal(reservation["initial_stop_risk"], "initial crypto reservation stop risk") * remaining_ratio
            conn.execute(
                "UPDATE crypto_paper_reservations SET active_notional=?,active_stop_risk=?,updated_at=? WHERE intent_id=? AND state='active'",
                (_text(active_notional), _text(active_stop), occurred_at.isoformat(), intent["id"]),
            )
            self._refresh_reservation_fingerprint_locked(conn, str(intent["id"]))
        if new_state == "filled":
            self._refresh_reservation_fingerprint_locked(conn, str(intent["id"]))
        conn.execute("UPDATE crypto_paper_proposals SET status=? WHERE id=?", (new_state, intent["proposal_id"]))
        conn.execute(
            """INSERT INTO crypto_paper_order_events(
               id,intent_id,event_key,from_state,to_state,event_type,safe_detail,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), intent["id"], f"{intent['id']}:fill:{broker_event_key}", intent["state"],
                new_state, "verified_fill_recorded", evidence_fingerprint, occurred_at.isoformat(),
            ),
        )
        return {
            "status": "recorded", "fill_id": fill_id, "state": new_state,
            "quantity": _text(quantity_d), "price": _text(price_d), "fees": _text(fees_d),
            "evidence_id": evidence_id, "evidence_fingerprint": evidence_fingerprint,
            "position_lifecycle_id": lifecycle_id,
        }

    def record_verified_fill(
        self,
        intent_id: str,
        broker_event_key: str,
        quantity: Any,
        price: Any,
        fees: Any,
        config: Mapping[str, Any],
        *,
        broker_evidence: Mapping[str, Any],
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        current = (occurred_at or datetime.now(UTC)).astimezone(UTC)
        _policy(config)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            intent = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
            if intent is None:
                raise CryptoPaperLaneError("crypto paper intent is missing")
            result = self._record_verified_fill_locked(
                conn, intent=intent, broker_event_key=str(broker_event_key), quantity=quantity,
                price=price, fees=fees, evidence=broker_evidence, config=config, occurred_at=current,
            )
        # Attribution is a post-commit projection over the now-authoritative
        # lifecycle and FIFO rows.  A fill remains durable if a report writer
        # is temporarily unavailable; the next strategy-performance refresh
        # deterministically retries the same lifecycle fingerprint.
        lifecycle_id = str(result.get("position_lifecycle_id") or "")
        if lifecycle_id:
            try:
                from .profit_attribution import ProfitAttributionEngine

                lifecycle_rows = self.storage.fetch_all(
                    "SELECT * FROM position_lifecycles WHERE id=? AND state='closed'",
                    (lifecycle_id,),
                )
                if lifecycle_rows:
                    ProfitAttributionEngine(self.storage).refresh_lifecycle(lifecycle_rows[0])
            except Exception as exc:
                self.storage.execute(
                    "INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(NULL,?,?,?,?)",
                    (
                        "crypto_attribution_refresh_deferred", "crypto_paper_lane",
                        json_dumps({"lifecycle_id": lifecycle_id, "error_type": type(exc).__name__}), iso_now(),
                    ),
                )
        return result

    def record_fill(
        self,
        intent_id: str,
        broker_event_key: str,
        quantity: Any,
        price: Any,
        fees: Any,
        config: Mapping[str, Any],
        *,
        broker_evidence: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper that refuses caller-synthesized fills."""

        if broker_evidence is None:
            raise CryptoPaperLaneError("verified broker evidence is required for a crypto fill")
        return self.record_verified_fill(
            intent_id, broker_event_key, quantity, price, fees, config,
            broker_evidence=broker_evidence, occurred_at=occurred_at,
        )

    def portfolio_metrics(self, broker: Any, config: Mapping[str, Any] | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        """Return crypto-only realized/unrealized evidence for portfolio reporting.

        The broker's account equity remains the authoritative portfolio equity;
        the crypto values here are an auditable attribution view.  They are
        deliberately separate from NYSE session metrics and fail closed when a
        current quote or a persisted lot is malformed.
        """

        current = (now or datetime.now(UTC)).astimezone(UTC)
        max_age_seconds = float(((config or {}).get("crypto") or {}).get("max_price_age_seconds", 300) or 300)
        realized = ZERO
        fees = ZERO
        unrealized = ZERO
        exposure = ZERO
        by_symbol: dict[str, dict[str, Decimal]] = {}
        by_strategy: dict[str, Decimal] = {}
        with self.storage.connect() as conn:
            fill_rows = conn.execute(
                """SELECT f.quantity,f.fees,i.symbol,p.display_json
                   FROM crypto_paper_fills f
                   JOIN crypto_paper_intents i ON i.id=f.intent_id
                   JOIN crypto_paper_proposals p ON p.id=i.proposal_id"""
            ).fetchall()
            for row in fill_rows:
                fees += _decimal(row["fees"], "crypto fill fees")
                try:
                    display = json.loads(row["display_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    display = {}
                strategy = str(display.get("strategy") or "unknown")
                by_strategy.setdefault(strategy, ZERO)
            realized_rows = conn.execute(
                """SELECT e.symbol,e.realized_pl_decimal,e.fees_decimal,e.confidence,
                          p.display_json
                   FROM realized_pnl_events e
                   LEFT JOIN crypto_paper_fills f ON f.broker_event_key=e.broker_event_key
                   LEFT JOIN crypto_paper_intents i ON i.id=f.intent_id
                   LEFT JOIN crypto_paper_proposals p ON p.id=i.proposal_id
                   WHERE e.source='crypto_paper_fill'"""
            ).fetchall()
            for row in realized_rows:
                if str(row["confidence"] or "") not in {"verified", "reconstructed"}:
                    raise CryptoPaperLaneError("crypto realized P&L confidence is not verified")
                value = row["realized_pl_decimal"]
                if value in (None, ""):
                    raise CryptoPaperLaneError("crypto realized P&L exact evidence is missing")
                realized += _decimal(value, "crypto realized P&L")
                symbol = str(row["symbol"] or "").upper()
                by_symbol.setdefault(symbol, {"realized": ZERO, "unrealized": ZERO, "exposure": ZERO})
                by_symbol[symbol]["realized"] += _decimal(value, "crypto symbol realized P&L")
                try:
                    display = json.loads(row["display_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    display = {}
                strategy = str(display.get("strategy") or "unknown")
                by_strategy[strategy] = by_strategy.get(strategy, ZERO) + _decimal(value, "crypto strategy realized P&L")
            lot_rows = conn.execute(
                """SELECT symbol,remaining_quantity,unit_cost,fees_allocated
                   FROM crypto_paper_lots WHERE remaining_quantity<>'0'"""
            ).fetchall()
        for row in lot_rows:
            symbol = str(row["symbol"] or "").upper()
            quantity = _decimal(row["remaining_quantity"], "crypto remaining quantity", positive=True)
            unit_cost = _decimal(row["unit_cost"], "crypto lot unit cost", positive=True)
            lot_fees = _decimal(row["fees_allocated"], "crypto lot fees")
            try:
                quote = broker.get_crypto_latest_quote(symbol)
                bid = _decimal(_value(quote, "bid_price", default=None), "crypto portfolio bid", positive=True)
                quote_at = _value(quote, "timestamp", default=None)
                if quote_at is None:
                    raise CryptoPaperLaneError("crypto portfolio quote timestamp is missing")
                quote_age = (current - _utc(quote_at, "crypto portfolio quote")).total_seconds()
                if quote_age < -1 or quote_age > max_age_seconds:
                    raise CryptoPaperLaneError("crypto portfolio quote is stale")
            except Exception as exc:
                raise CryptoPaperLaneError(f"crypto portfolio quote unavailable:{type(exc).__name__}") from exc
            value = quantity * bid
            lot_pnl = value - quantity * unit_cost - lot_fees
            unrealized += lot_pnl
            exposure += value
            by_symbol.setdefault(symbol, {"realized": ZERO, "unrealized": ZERO, "exposure": ZERO})
            by_symbol[symbol]["unrealized"] += lot_pnl
            by_symbol[symbol]["exposure"] += value
        account = broker.get_account()
        equity = _decimal(_value(account, "equity", default=None), "crypto portfolio account equity", positive=True)
        return {
            "as_of": current.isoformat(),
            "asset_class": "crypto",
            "broker_equity": _text(equity),
            "crypto_exposure": _text(exposure),
            "crypto_realized_pnl": _text(realized),
            "crypto_unrealized_pnl": _text(unrealized),
            "crypto_fees": _text(fees),
            "portfolio_equity_attribution": _text(equity),
            "by_symbol": {
                symbol: {key: _text(value) for key, value in sorted(values.items())}
                for symbol, values in sorted(by_symbol.items())
            },
            "by_strategy_realized_pnl": {key: _text(value) for key, value in sorted(by_strategy.items())},
            "continuous_market_profile": True,
            "equity_session_metrics_included": False,
        }

    def integrity_report(self) -> dict[str, int]:
        checks = {
            "crypto_paper_orphan_approvals": "SELECT COUNT(*) n FROM crypto_paper_approvals a LEFT JOIN crypto_paper_proposals p ON p.id=a.proposal_id WHERE p.id IS NULL",
            "crypto_paper_duplicate_active_approvals": "SELECT COUNT(*) n FROM (SELECT proposal_id FROM crypto_paper_approvals WHERE status='active' GROUP BY proposal_id HAVING COUNT(*)>1)",
            "crypto_paper_intents_without_reservation": "SELECT COUNT(*) n FROM crypto_paper_intents i LEFT JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE r.id IS NULL",
            "crypto_paper_active_intents_without_active_reservation": "SELECT COUNT(*) n FROM crypto_paper_intents i LEFT JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE i.state IN ('reserved','submitting','submitted','partially_filled','retryable_pre_submission') AND r.state<>'active'",
            "crypto_paper_terminal_intents_with_active_reservation": "SELECT COUNT(*) n FROM crypto_paper_intents i JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE i.state IN ('filled','rejected','cancelled','expired') AND r.state='active'",
            "crypto_paper_duplicate_logical_actions": "SELECT COUNT(*) n FROM (SELECT logical_action_key FROM crypto_paper_intents GROUP BY logical_action_key HAVING COUNT(*)>1)",
            "crypto_paper_duplicate_client_order_ids": "SELECT COUNT(*) n FROM (SELECT client_order_id FROM crypto_paper_intents GROUP BY client_order_id HAVING COUNT(*)>1)",
            "crypto_paper_ambiguous_auto_retry": "SELECT COUNT(*) n FROM crypto_paper_intents WHERE state IN ('unknown','reconciliation_required') AND broker_invocation_occurred=0",
            "crypto_paper_unauthorized_intents": "SELECT COUNT(*) n FROM crypto_paper_intents i JOIN crypto_paper_proposals p ON p.id=i.proposal_id WHERE p.side NOT IN ('buy','sell') OR p.status NOT IN ('intent_created','submitted','partially_filled','filled','expired','rejected')",
            "crypto_paper_reconciliation_required": "SELECT COUNT(*) n FROM crypto_paper_intents WHERE state IN ('unknown','reconciliation_required')",
            "crypto_paper_fills_without_order_evidence": "SELECT COUNT(*) n FROM crypto_paper_fills f LEFT JOIN crypto_paper_order_evidence e ON e.id=f.evidence_id WHERE e.id IS NULL",
            "crypto_paper_unverified_fill_evidence": "SELECT COUNT(*) n FROM crypto_paper_fills f JOIN crypto_paper_order_evidence e ON e.id=f.evidence_id WHERE e.verified<>1",
            "crypto_paper_fills_without_performance_link": "SELECT COUNT(*) n FROM crypto_paper_fills f LEFT JOIN crypto_performance_links l ON l.fill_id=f.id WHERE l.fill_id IS NULL",
            "crypto_paper_performance_link_without_fill": "SELECT COUNT(*) n FROM crypto_performance_links l LEFT JOIN crypto_paper_fills f ON f.id=l.fill_id WHERE f.id IS NULL",
            "crypto_paper_performance_link_evidence_mismatch": "SELECT COUNT(*) n FROM crypto_performance_links l JOIN crypto_paper_fills f ON f.id=l.fill_id WHERE l.evidence_fingerprint<>f.evidence_fingerprint",
            "crypto_paper_fill_without_position_lifecycle": "SELECT COUNT(*) n FROM crypto_paper_fills f JOIN crypto_paper_intents i ON i.id=f.intent_id WHERE i.position_lifecycle_id IS NULL",
            "crypto_paper_lot_without_position_lifecycle": "SELECT COUNT(*) n FROM crypto_paper_lots l JOIN crypto_paper_fills f ON f.broker_event_key=l.source_fill_event_key WHERE l.position_lifecycle_id IS NULL",
            "crypto_paper_realized_pnl_lifecycle_mismatch": "SELECT COUNT(*) n FROM crypto_paper_realized_pnl p JOIN crypto_paper_intents i ON i.id=p.intent_id WHERE p.position_lifecycle_id IS NULL OR i.position_lifecycle_id IS NULL OR p.position_lifecycle_id<>i.position_lifecycle_id",
            "crypto_paper_closed_lifecycle_with_open_fifo_lots": "SELECT COUNT(*) n FROM position_lifecycles l JOIN position_lots p ON p.position_lifecycle_id=l.id WHERE l.source LIKE 'crypto_paper%' AND l.state='closed' AND COALESCE(p.remaining_quantity_decimal,'0')<>'0'",
            "crypto_paper_active_lifecycle_without_open_fifo_lots": "SELECT COUNT(*) n FROM position_lifecycles l WHERE l.source LIKE 'crypto_paper%' AND l.state='active' AND NOT EXISTS (SELECT 1 FROM position_lots p WHERE p.position_lifecycle_id=l.id AND COALESCE(p.remaining_quantity_decimal,'0')<>'0')",
            "crypto_paper_closed_lifecycle_without_attribution": "SELECT COUNT(*) n FROM position_lifecycles l LEFT JOIN profit_attribution_records a ON a.position_lifecycle_id=l.id WHERE l.source LIKE 'crypto_paper%' AND l.state='closed' AND a.id IS NULL",
            "crypto_paper_performance_link_lifecycle_mismatch": "SELECT COUNT(*) n FROM crypto_performance_links l JOIN crypto_paper_intents i ON i.id=l.intent_id WHERE COALESCE(l.position_lifecycle_id,'')<>COALESCE(i.position_lifecycle_id,'')",
            "crypto_paper_reservation_without_intent": "SELECT COUNT(*) n FROM crypto_paper_reservations r LEFT JOIN crypto_paper_intents i ON i.id=r.intent_id WHERE i.id IS NULL",
            "crypto_paper_active_reservation_symbol_mismatch": "SELECT COUNT(*) n FROM crypto_paper_reservations r JOIN crypto_paper_intents i ON i.id=r.intent_id WHERE r.symbol<>i.symbol",
            "crypto_paper_approval_display_mismatch": "SELECT COUNT(*) n FROM crypto_paper_approvals a JOIN crypto_paper_proposals p ON p.id=a.proposal_id WHERE a.raw_message<>('YES CRYPTO '||p.id) OR a.display_fingerprint<>p.display_fingerprint OR a.telegram_message_fingerprint<>p.telegram_display_fingerprint OR a.reply_to_message_id<>p.telegram_message_id",
            "crypto_paper_pending_telegram_binding_missing": "SELECT COUNT(*) n FROM crypto_paper_proposals WHERE status='pending' AND (telegram_message_id IS NULL OR telegram_display_text IS NULL OR telegram_display_fingerprint IS NULL)",
        }
        result: dict[str, int] = {}
        with self.storage.connect() as conn:
            for name, query in checks.items():
                row = conn.execute(query).fetchone()
                result[name] = int(row["n"] or 0) if row is not None and "n" in row.keys() else 0
            fill_rows = conn.execute(
                """SELECT f.*,i.requested_quantity,i.symbol AS intent_symbol,i.side AS intent_side,
                          i.client_order_id AS intent_client_order_id,i.broker_order_id AS intent_broker_order_id,
                          e.status AS evidence_status,e.verified AS evidence_verified
                   FROM crypto_paper_fills f
                   JOIN crypto_paper_intents i ON i.id=f.intent_id
                   LEFT JOIN crypto_paper_order_evidence e ON e.id=f.evidence_id
                   ORDER BY f.intent_id,f.occurred_at,f.id"""
            ).fetchall()
            fills_by_intent: dict[str, Decimal] = {}
            for fill in fill_rows:
                try:
                    quantity = _decimal(fill["quantity"], "crypto integrity fill quantity", positive=True)
                    requested = _decimal(fill["requested_quantity"], "crypto integrity requested quantity", positive=True)
                    fills_by_intent[str(fill["intent_id"])] = fills_by_intent.get(str(fill["intent_id"]), ZERO) + quantity
                    if fills_by_intent[str(fill["intent_id"])] > requested:
                        result["crypto_paper_fills_exceed_quantity"] = result.get("crypto_paper_fills_exceed_quantity", 0) + 1
                except CryptoPaperLaneError:
                    result["crypto_paper_malformed_fill_decimal"] = result.get("crypto_paper_malformed_fill_decimal", 0) + 1
                if str(fill["broker_order_id"] or "") != str(fill["intent_broker_order_id"] or ""):
                    result["crypto_paper_fill_order_identity_mismatch"] = result.get("crypto_paper_fill_order_identity_mismatch", 0) + 1
                if str(fill["client_order_id"] or "") != str(fill["intent_client_order_id"] or ""):
                    result["crypto_paper_fill_client_identity_mismatch"] = result.get("crypto_paper_fill_client_identity_mismatch", 0) + 1
                if str(fill["evidence_status"] or "").lower() not in {"partially_filled", "partial_fill", "filled"}:
                    result["crypto_paper_fill_evidence_status_invalid"] = result.get("crypto_paper_fill_evidence_status_invalid", 0) + 1
                try:
                    payload = json.loads(fill["payload"] or "{}")
                    if _hash(payload) != str(fill["payload_fingerprint"] or ""):
                        result["crypto_paper_fill_payload_fingerprint_mismatch"] = result.get("crypto_paper_fill_payload_fingerprint_mismatch", 0) + 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    result["crypto_paper_fill_payload_invalid"] = result.get("crypto_paper_fill_payload_invalid", 0) + 1
            link_rows = conn.execute(
                """SELECT l.*,f.evidence_fingerprint AS fill_evidence_fingerprint,
                          i.client_order_id AS intent_client_order_id,
                          i.broker_order_id AS intent_broker_order_id,
                          i.position_lifecycle_id AS intent_lifecycle_id
                   FROM crypto_performance_links l
                   JOIN crypto_paper_fills f ON f.id=l.fill_id
                   JOIN crypto_paper_intents i ON i.id=l.intent_id"""
            ).fetchall()
            for link in link_rows:
                if str(link["evidence_fingerprint"]) != str(link["fill_evidence_fingerprint"]):
                    result["crypto_paper_performance_link_evidence_mismatch"] = result.get("crypto_paper_performance_link_evidence_mismatch", 0) + 1
                if str(link["position_lifecycle_id"] or "") != str(link["intent_lifecycle_id"] or ""):
                    result["crypto_paper_performance_link_lifecycle_mismatch"] = result.get("crypto_paper_performance_link_lifecycle_mismatch", 0) + 1
                if link["order_status"]:
                    expected_link_fingerprint = _hash({
                        "fill_id": str(link["fill_id"]),
                        "intent_id": str(link["intent_id"]),
                        "setup_id": str(link["setup_id"] or ""),
                        "outcome_id": str(link["outcome_id"] or ""),
                        "broker_order_id": str(link["broker_order_id"] or ""),
                        "evidence_fingerprint": str(link["evidence_fingerprint"] or ""),
                        "side": str(link["side"] or ""),
                        "action": str(link["action"] or ""),
                        "quantity": str(link["quantity"] or ""),
                        "price": str(link["price"] or ""),
                        "fees": str(link["fees"] or ""),
                        "fill_type": str(link["fill_type"] or ""),
                        "position_lifecycle_id": str(link["position_lifecycle_id"] or ""),
                        "realized_pl": link["realized_pl"],
                        "order_status": str(link["order_status"] or ""),
                    })
                    if expected_link_fingerprint != str(link["link_fingerprint"] or ""):
                        result["crypto_paper_performance_link_fingerprint_mismatch"] = result.get("crypto_paper_performance_link_fingerprint_mismatch", 0) + 1
            proposal_rows = conn.execute(
                "SELECT id,display_json,display_fingerprint,proposal_json,proposal_fingerprint FROM crypto_paper_proposals ORDER BY id"
            ).fetchall()
            for proposal in proposal_rows:
                try:
                    display = json.loads(proposal["display_json"] or "{}")
                    payload = json.loads(proposal["proposal_json"] or "{}")
                    if not isinstance(display, dict) or not isinstance(payload, dict):
                        raise ValueError("proposal authority JSON must be an object")
                    if _hash(display) != str(proposal["display_fingerprint"] or ""):
                        result["crypto_paper_display_fingerprint_mismatch"] = result.get("crypto_paper_display_fingerprint_mismatch", 0) + 1
                    if _hash(payload) != str(proposal["proposal_fingerprint"] or "") or payload.get("display") != display:
                        result["crypto_paper_proposal_fingerprint_mismatch"] = result.get("crypto_paper_proposal_fingerprint_mismatch", 0) + 1
                except (TypeError, ValueError, json.JSONDecodeError):
                    result["crypto_paper_proposal_payload_invalid"] = result.get("crypto_paper_proposal_payload_invalid", 0) + 1
            pending_rows = conn.execute(
                "SELECT id,telegram_message_id,telegram_chat_id,telegram_display_text,telegram_display_fingerprint FROM crypto_paper_proposals WHERE telegram_message_id IS NOT NULL OR telegram_display_text IS NOT NULL OR telegram_display_fingerprint IS NOT NULL"
            ).fetchall()
            for proposal in pending_rows:
                text_value = str(proposal["telegram_display_text"] or "")
                stored_fingerprint = str(proposal["telegram_display_fingerprint"] or "")
                if not text_value or not stored_fingerprint or _sha256(text_value) != stored_fingerprint:
                    result["crypto_paper_telegram_binding_mismatch"] = result.get("crypto_paper_telegram_binding_mismatch", 0) + 1
            reservation_rows = conn.execute(
                "SELECT * FROM crypto_paper_reservations ORDER BY id"
            ).fetchall()
            for reservation in reservation_rows:
                try:
                    initial_notional = _decimal(
                        reservation["initial_notional"], "crypto integrity initial reservation notional", positive=True
                    )
                    active_notional = _decimal(
                        reservation["active_notional"], "crypto integrity active reservation notional"
                    )
                    initial_stop = _decimal(
                        reservation["initial_stop_risk"], "crypto integrity initial reservation stop risk"
                    )
                    active_stop = _decimal(
                        reservation["active_stop_risk"], "crypto integrity active reservation stop risk"
                    )
                    if active_notional < ZERO or active_notional > initial_notional or active_stop < ZERO or active_stop > initial_stop:
                        result["crypto_paper_reservation_geometry_invalid"] = result.get("crypto_paper_reservation_geometry_invalid", 0) + 1
                    if str(reservation["reservation_fingerprint"] or "") != _reservation_fingerprint(dict(reservation)):
                        result["crypto_paper_reservation_fingerprint_mismatch"] = result.get("crypto_paper_reservation_fingerprint_mismatch", 0) + 1
                except CryptoPaperLaneError:
                    result["crypto_paper_malformed_reservation_decimal"] = result.get("crypto_paper_malformed_reservation_decimal", 0) + 1
            lot_rows = conn.execute(
                "SELECT * FROM crypto_paper_lots"
            ).fetchall()
            for lot in lot_rows:
                try:
                    remaining = _decimal(lot["remaining_quantity"], "crypto integrity remaining quantity")
                    original = _decimal(lot["original_quantity"], "crypto integrity original quantity", positive=True)
                    _decimal(lot["unit_cost"], "crypto integrity lot unit cost", positive=True)
                    _decimal(lot["fees_allocated"], "crypto integrity lot fees")
                    if remaining < ZERO or remaining > original:
                        result["crypto_paper_lot_quantity_invalid"] = result.get("crypto_paper_lot_quantity_invalid", 0) + 1
                    if str(lot["lot_fingerprint"] or "") != _crypto_lot_fingerprint(dict(lot)):
                        result["crypto_paper_lot_fingerprint_mismatch"] = result.get("crypto_paper_lot_fingerprint_mismatch", 0) + 1
                except CryptoPaperLaneError:
                    result["crypto_paper_malformed_lot_decimal"] = result.get("crypto_paper_malformed_lot_decimal", 0) + 1
            pnl_rows = conn.execute(
                "SELECT * FROM crypto_paper_realized_pnl"
            ).fetchall()
            for pnl in pnl_rows:
                if str(pnl["pnl_fingerprint"] or "") != _crypto_pnl_fingerprint(dict(pnl)):
                    result["crypto_paper_realized_pnl_fingerprint_mismatch"] = result.get("crypto_paper_realized_pnl_fingerprint_mismatch", 0) + 1
            approval_rows = conn.execute(
                "SELECT * FROM crypto_paper_approvals ORDER BY id"
            ).fetchall()
            for approval in approval_rows:
                expected_approval = _hash({
                    "id": approval["id"],
                    "proposal_id": approval["proposal_id"],
                    "sender_id": approval["sender_id"],
                    "raw_message": approval["raw_message"],
                    "reply_to_message_id": approval["reply_to_message_id"],
                    "parsed_action": approval["parsed_action"],
                    "display_fingerprint": approval["display_fingerprint"],
                    "telegram_message_fingerprint": approval["telegram_message_fingerprint"],
                    "telegram_chat_id": approval["telegram_chat_id"],
                    "approved_at": approval["approved_at"],
                })
                if str(approval["approval_fingerprint"] or "") != expected_approval:
                    result["crypto_paper_approval_fingerprint_mismatch"] = result.get("crypto_paper_approval_fingerprint_mismatch", 0) + 1
        return result


__all__ = [
    "CryptoPaperLaneError",
    "CryptoPaperProposal",
    "CryptoPaperIntent",
    "CryptoPaperLaneStore",
    "apply_crypto_paper_lane_schema",
    "format_crypto_paper_proposal",
]
