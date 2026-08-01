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
from .crypto_market_data import CryptoMarketDataStore
from .crypto_risk import CryptoRiskStore
from .crypto_sizing import load_verified_crypto_sizing
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
        if max_notional > Decimal("5"):
            failures.append("crypto_lane_order_notional_exceeds_stage_ceiling")
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
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,released_at TEXT,release_reason TEXT
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
          occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_lots(
          id TEXT PRIMARY KEY,symbol TEXT NOT NULL,source_fill_event_key TEXT NOT NULL UNIQUE,
          opened_at TEXT NOT NULL,original_quantity TEXT NOT NULL,remaining_quantity TEXT NOT NULL,
          unit_cost TEXT NOT NULL,fees_allocated TEXT NOT NULL DEFAULT '0',created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_paper_realized_pnl(
          id TEXT PRIMARY KEY,broker_event_key TEXT NOT NULL UNIQUE,intent_id TEXT NOT NULL,
          symbol TEXT NOT NULL,quantity TEXT NOT NULL,gross_proceeds TEXT NOT NULL,
          cost_basis TEXT NOT NULL,fees TEXT NOT NULL,realized_pl TEXT NOT NULL,
          occurred_at TEXT NOT NULL,created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_proposals_status ON crypto_paper_proposals(status,expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_paper_intents_state ON crypto_paper_intents(state,updated_at)")
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

    def bind_telegram_message(self, proposal_id: str, message_id: str, config: Mapping[str, Any], *, now: datetime | None = None) -> CryptoPaperProposal:
        if not str(message_id or "").strip():
            raise CryptoPaperLaneError("Telegram message identity is required")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM crypto_paper_proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise CryptoPaperLaneError("crypto paper proposal is missing")
            if row["status"] != "pending":
                raise CryptoPaperLaneError("crypto paper proposal is no longer display-bindable")
            if row["telegram_message_id"] not in (None, str(message_id)):
                raise CryptoPaperLaneError("crypto paper proposal Telegram identity changed")
            conn.execute("UPDATE crypto_paper_proposals SET telegram_message_id=? WHERE id=?", (str(message_id), proposal_id))
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
        stored_message = self._load_proposal_row(proposal.id).get("telegram_message_id")
        if stored_message in (None, ""):
            raise CryptoPaperLaneError("crypto paper proposal has no bound Telegram display message")
        if stored_message is not None and str(reply_to_message_id) != str(stored_message):
            raise CryptoPaperLaneError("crypto paper approval reply target does not match displayed Telegram message")
        expected_message = f"YES CRYPTO {proposal.id}"
        if " ".join(str(raw_message or expected_message).strip().upper().split()) != expected_message.upper():
            raise CryptoPaperLaneError("crypto paper approval command is not exact")
        approval_id = str(uuid.uuid4())
        body = {
            "id": approval_id, "proposal_id": proposal.id, "sender_id": str(sender_id),
            "raw_message": str(raw_message or f"YES CRYPTO {proposal.id}"),
            "reply_to_message_id": reply_to_message_id, "parsed_action": "approve",
            "display_fingerprint": proposal.display_fingerprint, "approved_at": current.isoformat(),
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
                   approved_at,display_fingerprint,approval_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (approval_id, proposal.id, str(sender_id), body["raw_message"], reply_to_message_id, "approve", "active", current.isoformat(), proposal.display_fingerprint, fingerprint),
            )
            conn.execute("UPDATE crypto_paper_proposals SET status='approved' WHERE id=?", (proposal.id,))
        return {**body, "approval_fingerprint": fingerprint, "status": "active"}

    def _active_reservation_totals(self, conn: Any, symbol: str) -> tuple[Decimal, Decimal]:
        row = conn.execute(
            "SELECT COALESCE(SUM(CAST(active_notional AS REAL)),0) AS n, COALESCE(SUM(CAST(active_stop_risk AS REAL)),0) AS r FROM crypto_paper_reservations WHERE state='active' AND symbol=?",
            (symbol,),
        ).fetchone()
        return Decimal(str(row["n"] or 0)), Decimal(str(row["r"] or 0))

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
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (reservation_id, intent_id, row["symbol"], row["notional"], row["notional"], row["stop_risk"], row["stop_risk"], "active", current.isoformat(), current.isoformat()),
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
        for name in ("power", "internet", "database"):
            probe = self.control_providers.get(name)
            if probe is not None:
                try:
                    if probe() is not True:
                        failures.append(f"{name}_health_not_verified")
                except Exception as exc:
                    failures.append(f"{name}_health_probe_failed:{type(exc).__name__}")
        if (config.get("telegram") or {}).get("crypto_execution_health_required") is True:
            probe = self.control_providers.get("telegram")
            if probe is None:
                failures.append("telegram_health_probe_missing")
            else:
                try:
                    if probe() is not True:
                        failures.append("telegram_health_not_verified")
                except Exception as exc:
                    failures.append(f"telegram_health_probe_failed:{type(exc).__name__}")
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
        try:
            account = broker.get_account()
            if _enum(_value(account, "status", default="")) != "active" or str(_value(account, "currency", default="")).upper() != "USD":
                failures.append("paper_account_not_active_or_usd")
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
            for name in ("daily_loss_dollars", "weekly_loss_dollars", "reference_equity"):
                try:
                    _decimal(metrics.get(name) if isinstance(metrics, Mapping) else None, f"crypto {name}")
                except CryptoPaperLaneError:
                    failures.append(f"crypto_loss_{name}_invalid")
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

    def execute_intent(self, intent_id: str, config: Mapping[str, Any], broker: Any, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        _policy(config)
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
            submit = getattr(broker, "submit_crypto_order", None)
            if submit is None:
                raise BrokerSubmissionNotAttempted("crypto paper broker adapter is unavailable before invocation")
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
        mapped_state = "submitted" if status not in {"filled", "partially_filled", "rejected", "canceled", "cancelled", "expired"} else {"filled": "filled", "partially_filled": "partially_filled", "rejected": "rejected", "canceled": "cancelled", "cancelled": "cancelled", "expired": "expired"}[status]
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE crypto_paper_intents SET state=?,broker_order_id=?,updated_at=?,terminal_at=CASE WHEN ? IN ('filled','rejected','cancelled','expired') THEN ? ELSE terminal_at END WHERE id=?", (mapped_state, broker_order_id or None, current.isoformat(), mapped_state, current.isoformat(), intent_id))
            conn.execute("UPDATE crypto_paper_proposals SET status=? WHERE id=?", (mapped_state, row["proposal_id"]))
            if mapped_state in TERMINAL_INTENT_STATES:
                conn.execute("UPDATE crypto_paper_reservations SET active_notional='0',active_stop_risk='0',state='released',released_at=?,release_reason=?,updated_at=? WHERE intent_id=?", (current.isoformat(), mapped_state, current.isoformat(), intent_id))
        return {**row, "state": mapped_state, "broker_invocation_occurred": 1, "broker_order_id": broker_order_id, "broker_call": True, "response": response}

    def record_fill(self, intent_id: str, broker_event_key: str, quantity: Any, price: Any, fees: Any, config: Mapping[str, Any], *, occurred_at: datetime | None = None) -> dict[str, Any]:
        current = (occurred_at or datetime.now(UTC)).astimezone(UTC)
        quantity_d = _decimal(quantity, "crypto fill quantity", positive=True)
        price_d = _decimal(price, "crypto fill price", positive=True)
        fees_d = _decimal(fees, "crypto fill fees")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            intent = conn.execute("SELECT * FROM crypto_paper_intents WHERE id=?", (intent_id,)).fetchone()
            if intent is None:
                raise CryptoPaperLaneError("crypto paper intent is missing")
            existing = conn.execute("SELECT id FROM crypto_paper_fills WHERE broker_event_key=?", (broker_event_key,)).fetchone()
            if existing:
                return {"status": "duplicate", "fill_id": existing["id"]}
            total = conn.execute("SELECT COALESCE(SUM(CAST(quantity AS REAL)),0) n FROM crypto_paper_fills WHERE intent_id=?", (intent_id,)).fetchone()["n"]
            requested = _decimal(intent["requested_quantity"], "requested crypto quantity", positive=True)
            if Decimal(str(total or 0)) + quantity_d > requested + Decimal("0.000000001"):
                raise CryptoPaperLaneError("crypto fills exceed requested quantity")
            fill_id = str(uuid.uuid4())
            conn.execute("INSERT INTO crypto_paper_fills(id,intent_id,broker_event_key,quantity,price,fees,occurred_at,received_at,payload) VALUES(?,?,?,?,?,?,?,?,?)", (fill_id, intent_id, broker_event_key, _text(quantity_d), _text(price_d), _text(fees_d), current.isoformat(), iso_now(), json_dumps({"quantity": _text(quantity_d), "price": _text(price_d), "fees": _text(fees_d)})))
            # Keep the shared fixed-point FIFO ledger in the same transaction.
            # Crypto remains an isolated execution lane, but its verified fills
            # must still contribute to durable loss controls and portfolio
            # accounting rather than becoming an unreported side ledger.
            from .lot_ledger import LotLedger

            ledger_intent = dict(intent)
            # The isolated crypto intent schema intentionally does not carry
            # equity-only lifecycle columns; provide explicit nulls for the
            # shared FIFO adapter instead of letting a missing SQLite key
            # become an accounting failure.
            ledger_intent.setdefault("position_lifecycle_id", None)
            ledger_intent.setdefault("approved_quantity", ledger_intent.get("requested_quantity"))
            proposal_row = conn.execute(
                "SELECT display_json,config_hash FROM crypto_paper_proposals WHERE id=?",
                (intent["proposal_id"],),
            ).fetchone()
            if proposal_row is not None:
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
                conn,
                intent=ledger_intent,
                broker_event_key=broker_event_key,
                delta_quantity=quantity_d,
                fill_price=price_d,
                occurred_at=current.isoformat(),
                fees=fees_d,
                adjustments=ZERO,
                source="crypto_paper_fill",
                accounting_timezone="UTC",
            )
            if str(intent["side"]).lower() == "sell":
                remaining_to_consume = quantity_d
                lots = conn.execute(
                    "SELECT id,remaining_quantity FROM crypto_paper_lots WHERE symbol=? AND CAST(remaining_quantity AS REAL)>0 ORDER BY opened_at,id",
                    (intent["symbol"],),
                ).fetchall()
                for lot in lots:
                    if remaining_to_consume <= ZERO:
                        break
                    lot_remaining = _decimal(lot["remaining_quantity"], "crypto paper lot remaining quantity", positive=True)
                    consumed = min(remaining_to_consume, lot_remaining)
                    conn.execute(
                        "UPDATE crypto_paper_lots SET remaining_quantity=? WHERE id=?",
                        (_text(lot_remaining - consumed), lot["id"]),
                    )
                    remaining_to_consume -= consumed
                realized_row = conn.execute(
                    """SELECT symbol,quantity,gross_proceeds,cost_basis,fees,realized_pl,occurred_at
                       FROM realized_pnl_events WHERE broker_event_key=?""",
                    (broker_event_key,),
                ).fetchone()
                if realized_row is not None and realized_row["cost_basis"] is not None and realized_row["realized_pl"] is not None:
                    conn.execute(
                        """INSERT OR IGNORE INTO crypto_paper_realized_pnl(
                           id,broker_event_key,intent_id,symbol,quantity,gross_proceeds,cost_basis,
                           fees,realized_pl,occurred_at,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()), broker_event_key, intent_id, realized_row["symbol"],
                            realized_row["quantity"], realized_row["gross_proceeds"], realized_row["cost_basis"],
                            realized_row["fees"], realized_row["realized_pl"], realized_row["occurred_at"], iso_now(),
                        ),
                    )
            conn.execute("INSERT OR IGNORE INTO crypto_paper_lots(id,symbol,source_fill_event_key,opened_at,original_quantity,remaining_quantity,unit_cost,fees_allocated,created_at) SELECT ?,symbol,?,?,?, ?,?,?,? FROM crypto_paper_intents WHERE id=? AND side='buy'", (str(uuid.uuid4()), broker_event_key, current.isoformat(), _text(quantity_d), _text(quantity_d), _text(price_d), _text(fees_d), current.isoformat(), intent_id))
            filled = Decimal(str(total or 0)) + quantity_d
            new_state = "filled" if filled >= requested - Decimal("0.000000001") else "partially_filled"
            conn.execute("UPDATE crypto_paper_intents SET state=?,updated_at=?,terminal_at=CASE WHEN ?='filled' THEN ? ELSE terminal_at END WHERE id=?", (new_state, current.isoformat(), new_state, current.isoformat(), intent_id))
            if new_state == "filled":
                conn.execute("UPDATE crypto_paper_reservations SET active_notional='0',active_stop_risk='0',state='released',released_at=?,release_reason='filled',updated_at=? WHERE intent_id=?", (current.isoformat(), current.isoformat(), intent_id))
            return {"status": "recorded", "fill_id": fill_id, "state": new_state, "quantity": _text(quantity_d), "price": _text(price_d), "fees": _text(fees_d)}

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
                """SELECT e.symbol,e.realized_pl,e.realized_pl_decimal,e.fees,e.fees_decimal,e.confidence,
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
                value = row["realized_pl_decimal"] or row["realized_pl"]
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
            "crypto_paper_duplicate_active_approvals": "SELECT COUNT(*) n FROM crypto_paper_approvals WHERE status='active' GROUP BY proposal_id HAVING COUNT(*)>1",
            "crypto_paper_intents_without_reservation": "SELECT COUNT(*) n FROM crypto_paper_intents i LEFT JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE r.id IS NULL",
            "crypto_paper_active_intents_without_active_reservation": "SELECT COUNT(*) n FROM crypto_paper_intents i LEFT JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE i.state IN ('reserved','submitting','submitted','partially_filled','retryable_pre_submission') AND r.state<>'active'",
            "crypto_paper_terminal_intents_with_active_reservation": "SELECT COUNT(*) n FROM crypto_paper_intents i JOIN crypto_paper_reservations r ON r.intent_id=i.id WHERE i.state IN ('filled','rejected','cancelled','expired') AND r.state='active'",
            "crypto_paper_duplicate_logical_actions": "SELECT COUNT(*) n FROM (SELECT logical_action_key FROM crypto_paper_intents GROUP BY logical_action_key HAVING COUNT(*)>1)",
            "crypto_paper_duplicate_client_order_ids": "SELECT COUNT(*) n FROM (SELECT client_order_id FROM crypto_paper_intents GROUP BY client_order_id HAVING COUNT(*)>1)",
            "crypto_paper_fills_exceed_quantity": "SELECT COUNT(*) n FROM crypto_paper_intents i WHERE (SELECT COALESCE(SUM(CAST(quantity AS REAL)),0) FROM crypto_paper_fills f WHERE f.intent_id=i.id)>CAST(i.requested_quantity AS REAL)+0.000000001",
            "crypto_paper_ambiguous_auto_retry": "SELECT COUNT(*) n FROM crypto_paper_intents WHERE state IN ('unknown','reconciliation_required') AND broker_invocation_occurred=0",
            "crypto_paper_unauthorized_intents": "SELECT COUNT(*) n FROM crypto_paper_intents i JOIN crypto_paper_proposals p ON p.id=i.proposal_id WHERE p.side NOT IN ('buy','sell') OR p.status NOT IN ('intent_created','submitted','partially_filled','filled','expired','rejected')",
        }
        result: dict[str, int] = {}
        with self.storage.connect() as conn:
            for name, query in checks.items():
                row = conn.execute(query).fetchone()
                result[name] = int(row["n"] or 0) if row is not None and "n" in row.keys() else 0
        return result


__all__ = [
    "CryptoPaperLaneError",
    "CryptoPaperProposal",
    "CryptoPaperIntent",
    "CryptoPaperLaneStore",
    "apply_crypto_paper_lane_schema",
    "format_crypto_paper_proposal",
]
