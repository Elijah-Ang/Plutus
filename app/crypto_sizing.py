"""Canonical Decimal sizing for the supervised Alpaca spot-crypto lane.

The functions in this module perform deterministic arithmetic only.  They do
not read caller booleans, create proposals, create approvals, reserve risk, or
submit orders.  A sizing result is authoritative only when it is bound to a
verified :class:`CryptoSizingAuthority` produced by ``crypto_risk`` from
current broker and durable portfolio evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

from .approval_authority import canonical_json
from .crypto_capabilities import CryptoAssetCapability, CryptoCapabilitySnapshot
from .crypto_market_data import CryptoMarketEvidence
from .formula_versions import (
    CRYPTO_SIZING_FORMULA_VERSION,
    CRYPTO_SIZING_SCHEMA_VERSION,
)
from .utils import iso_now, json_dumps


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
PERCENT = Decimal("100")


class CryptoSizingError(ValueError):
    """Raised when a sizing input is malformed rather than merely ineligible."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _decimal_input(
    value: Any,
    label: str,
    *,
    minimum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    # Binary floats are forbidden at the strategy/sizing authority boundary.
    # Broker and YAML values are normalised separately at their trust boundary.
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (Decimal, int, str)):
        raise CryptoSizingError(f"{label} must use Decimal, an integer, or a decimal string")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoSizingError(f"{label} is invalid") from exc
    if not number.is_finite():
        raise CryptoSizingError(f"{label} must be finite")
    if positive and number <= ZERO:
        raise CryptoSizingError(f"{label} must be positive")
    if minimum is not None and number < minimum:
        raise CryptoSizingError(f"{label} must be at least {_text(minimum)}")
    return number


def _trusted_decimal(value: Any, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    """Normalise an already trusted config/broker value into exact arithmetic."""

    if isinstance(value, bool) or value is None:
        raise CryptoSizingError(f"{label} is missing or invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoSizingError(f"{label} is missing or invalid") from exc
    if not number.is_finite() or number < minimum:
        raise CryptoSizingError(f"{label} must be finite and at least {_text(minimum)}")
    return number


def _valid_sha256(value: str) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _round_increment(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    if not value.is_finite() or value < ZERO or not increment.is_finite() or increment <= ZERO:
        raise CryptoSizingError("rounding values must be finite and nonnegative with a positive increment")
    return (value / increment).to_integral_value(rounding=rounding) * increment


def _decimal_places(value: Decimal) -> int:
    return max(0, -value.normalize().as_tuple().exponent)


@dataclass(frozen=True)
class CryptoSizingRequest:
    """Exact strategy-side request before portfolio constraints are applied."""

    source_type: str
    source_id: str
    source_fingerprint: str
    symbol: str
    side: str
    action: str
    request_basis: str
    requested_stop_risk_dollars: Decimal | str | int | None = None
    stop_price: Decimal | str | int | None = None
    requested_exit_quantity: Decimal | str | int | None = None
    close_entire_position: bool = False

    def payload(self) -> dict[str, Any]:
        def value(raw: Any, label: str) -> str | None:
            return None if raw is None else _text(_decimal_input(raw, label, minimum=ZERO))

        return {
            "source_type": str(self.source_type or "").strip(),
            "source_id": str(self.source_id or "").strip(),
            "source_fingerprint": str(self.source_fingerprint or "").strip().lower(),
            "symbol": str(self.symbol or "").strip().upper(),
            "side": str(self.side or "").strip().lower(),
            "action": str(self.action or "").strip().lower(),
            "request_basis": str(self.request_basis or "").strip().lower(),
            "requested_stop_risk_dollars": value(
                self.requested_stop_risk_dollars, "requested_stop_risk_dollars"
            ),
            "stop_price": value(self.stop_price, "stop_price"),
            "requested_exit_quantity": value(
                self.requested_exit_quantity, "requested_exit_quantity"
            ),
            "close_entire_position": self.close_entire_position is True,
        }


@dataclass(frozen=True)
class CryptoSizingAuthority:
    """Portfolio ceilings derived from a verified crypto risk snapshot."""

    risk_snapshot_id: str
    risk_snapshot_fingerprint: str
    paper_account_id_hash: str
    config_hash: str
    hard_notional_ceiling: Decimal
    hard_stop_risk_ceiling: Decimal
    current_position_quantity: Decimal
    pending_sell_quantity: Decimal
    authoritative: bool = True
    failure_reasons: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "risk_snapshot_id": self.risk_snapshot_id,
            "risk_snapshot_fingerprint": self.risk_snapshot_fingerprint,
            "paper_account_id_hash": self.paper_account_id_hash,
            "config_hash": self.config_hash,
            "hard_notional_ceiling": _text(self.hard_notional_ceiling),
            "hard_stop_risk_ceiling": _text(self.hard_stop_risk_ceiling),
            "current_position_quantity": _text(self.current_position_quantity),
            "pending_sell_quantity": _text(self.pending_sell_quantity),
            "authoritative": self.authoritative,
            "failure_reasons": list(self.failure_reasons),
        }


@dataclass(frozen=True)
class CryptoSizingDecision:
    id: str
    run_id: str
    request_fingerprint: str
    risk_snapshot_id: str
    risk_snapshot_fingerprint: str
    capability_snapshot_id: str
    capability_snapshot_fingerprint: str
    market_evidence_id: str
    market_evidence_fingerprint: str
    symbol: str
    side: str
    action: str
    request_basis: str
    limit_price: str | None
    stop_price: str | None
    stop_execution_price: str | None
    canonical_quantity: str | None
    canonical_notional: str | None
    canonical_stop_risk: str | None
    gross_stop_risk: str | None
    estimated_fees: str | None
    estimated_stop_slippage: str | None
    minimum_order_size: str | None
    quantity_increment: str | None
    price_increment: str | None
    eligible: bool
    blockers: tuple[str, ...]
    binding_caps: tuple[str, ...]
    authoritative: bool
    execution_authorized: bool
    config_hash: str
    formula_version: str
    schema_version: str
    created_at: str
    decision_fingerprint: str
    payload: Mapping[str, Any]


def _policy(config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = config.get("crypto") or {}
    policy = cfg.get("sizing_policy") or {}
    failures: list[str] = []
    if policy.get("mode") != "research_only":
        failures.append("sizing_policy_mode_not_research_only")
    if policy.get("formula_version") != CRYPTO_SIZING_FORMULA_VERSION:
        failures.append("sizing_formula_identity_mismatch")
    if policy.get("schema_version") != CRYPTO_SIZING_SCHEMA_VERSION:
        failures.append("sizing_schema_identity_mismatch")
    if str((config.get("formula_versions") or {}).get("crypto_sizing") or "") != CRYPTO_SIZING_FORMULA_VERSION:
        failures.append("configuration_formula_identity_mismatch")

    decimal_names = (
        "minimum_buy_notional_usd",
        "maximum_order_notional_usd",
        "conservative_taker_fee_bps_per_side",
        "stop_execution_slippage_bps",
        "minimum_stop_distance_pct",
        "maximum_stop_distance_pct",
    )
    decimals: dict[str, Decimal] = {}
    for name in decimal_names:
        try:
            decimals[name] = _trusted_decimal(policy.get(name), f"crypto.sizing_policy.{name}")
        except CryptoSizingError:
            failures.append(f"invalid_{name}")
    try:
        quantity_places = int(policy.get("maximum_quantity_decimal_places"))
        notional_places = int(policy.get("maximum_notional_decimal_places"))
    except (TypeError, ValueError):
        quantity_places = notional_places = -1
    if quantity_places != 9:
        failures.append("maximum_quantity_decimal_places_must_be_9")
    if notional_places != 2:
        failures.append("maximum_notional_decimal_places_must_be_2")
    if policy.get("require_quantity_basis_for_sells") is not True:
        failures.append("quantity_basis_for_sells_not_required")
    if policy.get("allow_full_position_dust_exit") is not True:
        failures.append("full_position_dust_exit_not_enabled")
    if decimals:
        if decimals.get("minimum_buy_notional_usd") != Decimal("1"):
            failures.append("minimum_buy_notional_must_be_one_usd")
        if decimals.get("maximum_order_notional_usd", ZERO) > Decimal("5"):
            failures.append("maximum_order_notional_exceeds_stage_ceiling")
        if decimals.get("conservative_taker_fee_bps_per_side") != Decimal("25"):
            failures.append("taker_fee_policy_not_conservative_tier_one")
        minimum_stop = decimals.get("minimum_stop_distance_pct", ZERO)
        maximum_stop = decimals.get("maximum_stop_distance_pct", ZERO)
        if minimum_stop <= ZERO or maximum_stop < minimum_stop or maximum_stop > Decimal("100"):
            failures.append("stop_distance_policy_invalid")
    if failures:
        raise CryptoSizingError("invalid crypto sizing policy: " + ", ".join(sorted(set(failures))))
    return {**policy, **decimals, "quantity_places": quantity_places, "notional_places": notional_places}


def _validate_relationships(
    request: CryptoSizingRequest,
    authority: CryptoSizingAuthority,
    capability: CryptoCapabilitySnapshot,
    market: CryptoMarketEvidence,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], CryptoAssetCapability]:
    payload = request.payload()
    if not payload["source_type"] or not payload["source_id"]:
        raise CryptoSizingError("sizing request requires stable source_type and source_id")
    if not _valid_sha256(payload["source_fingerprint"]):
        raise CryptoSizingError("sizing request requires a SHA-256 source fingerprint")
    if payload["symbol"] not in tuple((config.get("crypto") or {}).get("symbols") or ()):
        raise CryptoSizingError("sizing request symbol is not configured")
    if payload["side"] not in {"buy", "sell"}:
        raise CryptoSizingError("sizing request side must be buy or sell")
    if payload["request_basis"] not in {"quantity", "notional"}:
        raise CryptoSizingError("sizing request basis must be quantity or notional")
    if payload["side"] == "buy" and payload["action"] not in {"entry", "add"}:
        raise CryptoSizingError("a crypto BUY must be an entry or add")
    if payload["side"] == "sell" and payload["action"] not in {"exit", "reduce"}:
        raise CryptoSizingError("a crypto SELL must be an exit or reduction")
    if not capability.authoritative:
        raise CryptoSizingError("crypto capability snapshot is not authoritative")
    asset = capability.asset(payload["symbol"])
    if asset is None or not asset.authoritative:
        raise CryptoSizingError("crypto pair capability is not authoritative")
    if authority.authoritative and (not market.authoritative or not market.execution_eligible):
        raise CryptoSizingError("crypto market evidence is not execution eligible")
    if market.symbol != payload["symbol"]:
        raise CryptoSizingError("crypto market evidence symbol mismatch")
    if market.capability_snapshot_id != capability.id or market.capability_snapshot_fingerprint != capability.snapshot_fingerprint:
        raise CryptoSizingError("crypto market evidence capability binding mismatch")
    config_hash = str(config.get("effective_config_hash") or "").strip()
    if not _valid_sha256(config_hash):
        raise CryptoSizingError("current configuration hash is missing or invalid")
    if capability.config_hash != config_hash or market.config_hash != config_hash or authority.config_hash != config_hash:
        raise CryptoSizingError("crypto sizing configuration identity mismatch")
    if authority.authoritative and authority.paper_account_id_hash != capability.paper_account_id_hash:
        raise CryptoSizingError("crypto sizing paper account identity mismatch")
    if not authority.risk_snapshot_id or not _valid_sha256(authority.risk_snapshot_fingerprint):
        raise CryptoSizingError("crypto sizing risk authority is malformed")
    for label, value in (
        ("hard_notional_ceiling", authority.hard_notional_ceiling),
        ("hard_stop_risk_ceiling", authority.hard_stop_risk_ceiling),
        ("current_position_quantity", authority.current_position_quantity),
        ("pending_sell_quantity", authority.pending_sell_quantity),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
            raise CryptoSizingError(f"{label} must be a finite nonnegative Decimal")
    return payload, asset


def calculate_crypto_sizing(
    *,
    decision_id: str,
    run_id: str,
    request: CryptoSizingRequest,
    authority: CryptoSizingAuthority,
    capability: CryptoCapabilitySnapshot,
    market: CryptoMarketEvidence,
    config: Mapping[str, Any],
    created_at: str,
) -> CryptoSizingDecision:
    """Calculate a risk-reducing canonical size without ever rounding up risk."""

    policy = _policy(config)
    request_payload, asset = _validate_relationships(request, authority, capability, market, config)
    request_fingerprint = _hash(request_payload)
    minimum_order_size = _trusted_decimal(asset.min_order_size, "asset.min_order_size", minimum=Decimal("0.000000001"))
    quantity_increment = _trusted_decimal(
        asset.min_trade_increment, "asset.min_trade_increment", minimum=Decimal("0.000000001")
    )
    price_increment = _trusted_decimal(asset.price_increment, "asset.price_increment", minimum=Decimal("0.000000001"))
    if _decimal_places(quantity_increment) > policy["quantity_places"]:
        raise CryptoSizingError("asset quantity increment exceeds the supported nine decimal places")
    if _decimal_places(price_increment) > policy["quantity_places"]:
        raise CryptoSizingError("asset price increment exceeds the supported precision envelope")

    bid = _trusted_decimal(market.bid_price, "market.bid_price", minimum=Decimal("0.000000001"))
    ask = _trusted_decimal(market.ask_price, "market.ask_price", minimum=Decimal("0.000000001"))
    if ask < bid:
        raise CryptoSizingError("crypto quote is crossed")
    fee_rate = policy["conservative_taker_fee_bps_per_side"] / BPS
    stop_slippage_rate = policy["stop_execution_slippage_bps"] / BPS
    blockers: list[str] = []
    binding_caps: list[str] = []
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    stop_execution_price: Decimal | None = None
    quantity: Decimal | None = None
    notional: Decimal | None = None
    stop_risk: Decimal | None = None
    gross_stop_risk: Decimal | None = None
    fees: Decimal | None = None
    stop_slippage: Decimal | None = None
    if not authority.authoritative:
        blockers.append("crypto_risk_snapshot_not_authoritative")
        blockers.extend(f"risk_evidence:{reason}" for reason in authority.failure_reasons)

    if request_payload["side"] == "buy":
        if request.close_entire_position or request.requested_exit_quantity is not None:
            raise CryptoSizingError("a crypto BUY cannot request an exit quantity")
        requested_risk = _decimal_input(
            request.requested_stop_risk_dollars,
            "requested_stop_risk_dollars",
            positive=True,
        )
        stop_price = _decimal_input(request.stop_price, "stop_price", positive=True)
        limit_price = _round_increment(ask, price_increment, ROUND_CEILING)
        if stop_price >= bid or stop_price >= limit_price:
            blockers.append("protective_stop_not_below_current_market")
        distance_pct = (limit_price - stop_price) / limit_price * PERCENT if stop_price < limit_price else ZERO
        if distance_pct < policy["minimum_stop_distance_pct"]:
            blockers.append("protective_stop_distance_below_policy_minimum")
        if distance_pct > policy["maximum_stop_distance_pct"]:
            blockers.append("protective_stop_distance_above_policy_maximum")
        stop_execution_price = stop_price * (ONE - stop_slippage_rate)
        if stop_execution_price <= ZERO:
            blockers.append("conservative_stop_execution_price_not_positive")

        config_notional_cap = policy["maximum_order_notional_usd"]
        effective_notional_cap = min(config_notional_cap, authority.hard_notional_ceiling)
        effective_risk_cap = min(requested_risk, authority.hard_stop_risk_ceiling)
        if effective_notional_cap <= ZERO:
            blockers.append("no_authoritative_crypto_notional_capacity")
        if effective_risk_cap <= ZERO:
            blockers.append("no_authoritative_crypto_stop_risk_capacity")
        if effective_notional_cap == config_notional_cap:
            binding_caps.append("configured_maximum_order_notional")
        if effective_notional_cap == authority.hard_notional_ceiling:
            binding_caps.append("authoritative_portfolio_notional_capacity")
        if effective_risk_cap == requested_risk:
            binding_caps.append("requested_stop_risk")
        if effective_risk_cap == authority.hard_stop_risk_ceiling:
            binding_caps.append("authoritative_portfolio_stop_risk_capacity")

        if not blockers:
            # Entry fee is charged against the received asset and an exit fee
            # against quote proceeds.  Both are included in the maximum-loss
            # sizing denominator, along with a configured adverse stop fill.
            loss_per_unit = (
                limit_price * (ONE + fee_rate)
                - stop_execution_price * (ONE - fee_rate)
            )
            if loss_per_unit <= ZERO:
                blockers.append("cost_inclusive_stop_loss_per_unit_not_positive")
            else:
                raw_quantity = min(
                    effective_notional_cap / limit_price,
                    effective_risk_cap / loss_per_unit,
                )
                if request_payload["request_basis"] == "quantity":
                    quantity = _round_increment(raw_quantity, quantity_increment, ROUND_FLOOR)
                    notional = quantity * limit_price
                else:
                    notional_quantum = ONE.scaleb(-policy["notional_places"])
                    raw_notional = min(effective_notional_cap, raw_quantity * limit_price)
                    notional = _round_increment(raw_notional, notional_quantum, ROUND_FLOOR)
                    quantity = notional / limit_price if limit_price > ZERO else ZERO
                if quantity <= ZERO or notional <= ZERO:
                    blockers.append("rounded_crypto_size_is_zero")
                if quantity < minimum_order_size:
                    blockers.append("crypto_quantity_below_current_asset_minimum")
                if notional < policy["minimum_buy_notional_usd"]:
                    blockers.append("crypto_buy_notional_below_one_usd")
                if request_payload["request_basis"] == "quantity" and _decimal_places(quantity) > policy["quantity_places"]:
                    blockers.append("crypto_quantity_exceeds_nine_decimal_places")
                if request_payload["request_basis"] == "notional" and _decimal_places(notional) > policy["notional_places"]:
                    blockers.append("crypto_notional_exceeds_two_decimal_places")
                gross_stop_risk = quantity * (limit_price - stop_price)
                fees = quantity * (limit_price + stop_execution_price) * fee_rate
                stop_slippage = quantity * (stop_price - stop_execution_price)
                stop_risk = quantity * loss_per_unit
                if notional > effective_notional_cap:
                    blockers.append("rounded_crypto_notional_exceeds_authority")
                if stop_risk > effective_risk_cap:
                    blockers.append("rounded_crypto_stop_risk_exceeds_authority")
    else:
        if request.requested_stop_risk_dollars is not None or request.stop_price is not None:
            raise CryptoSizingError("a risk-reducing crypto SELL cannot request entry stop risk")
        if request_payload["request_basis"] != "quantity":
            blockers.append("crypto_sell_requires_quantity_basis")
        sellable = authority.current_position_quantity - authority.pending_sell_quantity
        if sellable < ZERO:
            blockers.append("pending_crypto_sells_exceed_current_holding")
            sellable = ZERO
        if request.close_entire_position:
            if request.requested_exit_quantity is not None:
                requested_exit = _decimal_input(
                    request.requested_exit_quantity, "requested_exit_quantity", positive=True
                )
                if requested_exit != sellable:
                    blockers.append("full_exit_quantity_does_not_match_sellable_holding")
            quantity = sellable
            if _decimal_places(quantity) > policy["quantity_places"]:
                blockers.append("full_exit_quantity_exceeds_nine_decimal_places")
        else:
            requested_exit = _decimal_input(
                request.requested_exit_quantity, "requested_exit_quantity", positive=True
            )
            if requested_exit > sellable:
                blockers.append("requested_crypto_sell_exceeds_sellable_holding")
            quantity = _round_increment(min(requested_exit, sellable), quantity_increment, ROUND_FLOOR)
            if quantity < minimum_order_size:
                blockers.append("partial_crypto_sell_below_current_asset_minimum")
        limit_price = _round_increment(bid, price_increment, ROUND_FLOOR)
        if quantity is None or quantity <= ZERO:
            blockers.append("no_sellable_crypto_quantity")
        else:
            notional = quantity * limit_price
            gross_stop_risk = stop_risk = ZERO
            fees = notional * fee_rate
            stop_slippage = ZERO

    blockers = sorted(set(blockers))
    binding_caps = sorted(set(binding_caps))
    eligible = not blockers
    authoritative = eligible and authority.authoritative
    payload = {
        "id": str(decision_id),
        "run_id": str(run_id),
        "request": request_payload,
        "request_fingerprint": request_fingerprint,
        "risk_authority": authority.payload(),
        "capability_snapshot_id": capability.id,
        "capability_snapshot_fingerprint": capability.snapshot_fingerprint,
        "market_evidence_id": market.id,
        "market_evidence_fingerprint": market.evidence_fingerprint,
        "symbol": request_payload["symbol"],
        "side": request_payload["side"],
        "action": request_payload["action"],
        "request_basis": request_payload["request_basis"],
        "limit_price": _text(limit_price),
        "stop_price": _text(stop_price),
        "stop_execution_price": _text(stop_execution_price),
        "canonical_quantity": _text(quantity),
        "canonical_notional": _text(notional),
        "canonical_stop_risk": _text(stop_risk),
        "gross_stop_risk": _text(gross_stop_risk),
        "estimated_fees": _text(fees),
        "estimated_stop_slippage": _text(stop_slippage),
        "minimum_order_size": _text(minimum_order_size),
        "quantity_increment": _text(quantity_increment),
        "price_increment": _text(price_increment),
        "eligible": eligible,
        "blockers": blockers,
        "binding_caps": binding_caps,
        "authoritative": authoritative,
        # Sizing/risk is a research authority in this PR.  It cannot itself
        # authorise a proposal, approval, intent, reservation, or broker call.
        "execution_authorized": False,
        "config_hash": authority.config_hash,
        "formula_version": CRYPTO_SIZING_FORMULA_VERSION,
        "schema_version": CRYPTO_SIZING_SCHEMA_VERSION,
        "created_at": str(created_at),
    }
    fingerprint = _hash(payload)
    return CryptoSizingDecision(
        id=str(decision_id), run_id=str(run_id), request_fingerprint=request_fingerprint,
        risk_snapshot_id=authority.risk_snapshot_id,
        risk_snapshot_fingerprint=authority.risk_snapshot_fingerprint,
        capability_snapshot_id=capability.id,
        capability_snapshot_fingerprint=capability.snapshot_fingerprint,
        market_evidence_id=market.id, market_evidence_fingerprint=market.evidence_fingerprint,
        symbol=request_payload["symbol"], side=request_payload["side"],
        action=request_payload["action"], request_basis=request_payload["request_basis"],
        limit_price=payload["limit_price"], stop_price=payload["stop_price"],
        stop_execution_price=payload["stop_execution_price"],
        canonical_quantity=payload["canonical_quantity"],
        canonical_notional=payload["canonical_notional"],
        canonical_stop_risk=payload["canonical_stop_risk"],
        gross_stop_risk=payload["gross_stop_risk"], estimated_fees=payload["estimated_fees"],
        estimated_stop_slippage=payload["estimated_stop_slippage"],
        minimum_order_size=payload["minimum_order_size"],
        quantity_increment=payload["quantity_increment"], price_increment=payload["price_increment"],
        eligible=eligible, blockers=tuple(blockers), binding_caps=tuple(binding_caps),
        authoritative=authoritative, execution_authorized=False,
        config_hash=authority.config_hash, formula_version=CRYPTO_SIZING_FORMULA_VERSION,
        schema_version=CRYPTO_SIZING_SCHEMA_VERSION, created_at=str(created_at),
        decision_fingerprint=fingerprint, payload=payload,
    )


def apply_crypto_sizing_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_sizing_decisions(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          request_fingerprint TEXT NOT NULL,
          risk_snapshot_id TEXT NOT NULL,
          risk_snapshot_fingerprint TEXT NOT NULL,
          capability_snapshot_id TEXT NOT NULL,
          capability_snapshot_fingerprint TEXT NOT NULL,
          market_evidence_id TEXT NOT NULL,
          market_evidence_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('buy','sell')),
          action TEXT NOT NULL,
          request_basis TEXT NOT NULL CHECK(request_basis IN ('quantity','notional')),
          limit_price TEXT,
          stop_price TEXT,
          stop_execution_price TEXT,
          canonical_quantity TEXT,
          canonical_notional TEXT,
          canonical_stop_risk TEXT,
          gross_stop_risk TEXT,
          estimated_fees TEXT,
          estimated_stop_slippage TEXT,
          minimum_order_size TEXT,
          quantity_increment TEXT,
          price_increment TEXT,
          eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
          authoritative INTEGER NOT NULL CHECK(authoritative IN (0,1)),
          execution_authorized INTEGER NOT NULL CHECK(execution_authorized=0),
          blockers_json TEXT NOT NULL,
          binding_caps_json TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          decision_json TEXT NOT NULL,
          decision_fingerprint TEXT NOT NULL UNIQUE,
          FOREIGN KEY(capability_snapshot_id) REFERENCES crypto_capability_snapshots(id),
          FOREIGN KEY(market_evidence_id) REFERENCES crypto_market_data_evidence(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crypto_sizing_run_symbol ON crypto_sizing_decisions(run_id,symbol,created_at)"
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                CRYPTO_SIZING_SCHEMA_VERSION,
                iso_now(),
                "Decimal crypto quantity/notional precision, cost-inclusive stop risk and conservative rounding authority",
            ),
        )


def insert_crypto_sizing(conn: Any, decision: CryptoSizingDecision) -> None:
    conn.execute(
        """
        INSERT INTO crypto_sizing_decisions(
          id,run_id,request_fingerprint,risk_snapshot_id,risk_snapshot_fingerprint,
          capability_snapshot_id,capability_snapshot_fingerprint,market_evidence_id,
          market_evidence_fingerprint,symbol,side,action,request_basis,limit_price,
          stop_price,stop_execution_price,canonical_quantity,canonical_notional,
          canonical_stop_risk,gross_stop_risk,estimated_fees,estimated_stop_slippage,
          minimum_order_size,quantity_increment,price_increment,eligible,authoritative,
          execution_authorized,blockers_json,binding_caps_json,config_hash,
          formula_version,schema_version,created_at,decision_json,decision_fingerprint
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            decision.id, decision.run_id, decision.request_fingerprint,
            decision.risk_snapshot_id, decision.risk_snapshot_fingerprint,
            decision.capability_snapshot_id, decision.capability_snapshot_fingerprint,
            decision.market_evidence_id, decision.market_evidence_fingerprint,
            decision.symbol, decision.side, decision.action, decision.request_basis,
            decision.limit_price, decision.stop_price, decision.stop_execution_price,
            decision.canonical_quantity, decision.canonical_notional,
            decision.canonical_stop_risk, decision.gross_stop_risk,
            decision.estimated_fees, decision.estimated_stop_slippage,
            decision.minimum_order_size, decision.quantity_increment,
            decision.price_increment, int(decision.eligible), int(decision.authoritative),
            0, json_dumps(decision.blockers), json_dumps(decision.binding_caps),
            decision.config_hash, decision.formula_version, decision.schema_version,
            decision.created_at, json_dumps(decision.payload), decision.decision_fingerprint,
        ),
    )


def load_verified_crypto_sizing(storage: Any, decision_id: str, config: Mapping[str, Any]) -> CryptoSizingDecision:
    rows = storage.fetch_all("SELECT * FROM crypto_sizing_decisions WHERE id=?", (decision_id,))
    if len(rows) != 1:
        raise RuntimeError("crypto sizing decision is missing or duplicated")
    row = dict(rows[0])
    try:
        payload = json.loads(row["decision_json"])
        blockers = json.loads(row["blockers_json"])
        binding_caps = json.loads(row["binding_caps_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("crypto sizing decision JSON is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(blockers, list) or not isinstance(binding_caps, list):
        raise RuntimeError("crypto sizing decision shape is invalid")
    if _hash(payload) != row["decision_fingerprint"]:
        raise RuntimeError("crypto sizing decision fingerprint mismatch")
    request_payload = payload.get("request")
    if not isinstance(request_payload, dict) or _hash(request_payload) != row["request_fingerprint"]:
        raise RuntimeError("crypto sizing request fingerprint mismatch")
    scalar_columns = (
        "id", "run_id", "request_fingerprint", "capability_snapshot_id",
        "capability_snapshot_fingerprint", "market_evidence_id",
        "market_evidence_fingerprint", "symbol", "side", "action", "request_basis",
        "limit_price", "stop_price", "stop_execution_price", "canonical_quantity",
        "canonical_notional", "canonical_stop_risk", "gross_stop_risk",
        "estimated_fees", "estimated_stop_slippage", "minimum_order_size",
        "quantity_increment", "price_increment", "config_hash", "formula_version",
        "schema_version", "created_at",
    )
    for key in scalar_columns:
        expected = payload.get(key)
        if key == "risk_snapshot_id":
            continue
        if row.get(key) != expected:
            raise RuntimeError(f"crypto sizing persisted column mismatch: {key}")
    authority_payload = payload.get("risk_authority")
    if not isinstance(authority_payload, dict):
        raise RuntimeError("crypto sizing risk authority payload is missing")
    if row["risk_snapshot_id"] != authority_payload.get("risk_snapshot_id"):
        raise RuntimeError("crypto sizing risk snapshot identity mismatch")
    if row["risk_snapshot_fingerprint"] != authority_payload.get("risk_snapshot_fingerprint"):
        raise RuntimeError("crypto sizing risk snapshot fingerprint mismatch")
    if bool(row["eligible"]) != payload.get("eligible") or bool(row["authoritative"]) != payload.get("authoritative"):
        raise RuntimeError("crypto sizing authority classification mismatch")
    if bool(row["execution_authorized"]) or payload.get("execution_authorized") is not False:
        raise RuntimeError("crypto sizing decision unexpectedly authorizes execution")
    if blockers != payload.get("blockers") or binding_caps != payload.get("binding_caps"):
        raise RuntimeError("crypto sizing reason binding mismatch")
    config_hash = str(config.get("effective_config_hash") or "")
    if row["config_hash"] != config_hash:
        raise RuntimeError("crypto sizing configuration identity changed")
    _policy(config)
    if row["formula_version"] != CRYPTO_SIZING_FORMULA_VERSION or row["schema_version"] != CRYPTO_SIZING_SCHEMA_VERSION:
        raise RuntimeError("crypto sizing decision version is obsolete")
    capability = storage.fetch_all(
        "SELECT snapshot_fingerprint,config_hash FROM crypto_capability_snapshots WHERE id=?",
        (row["capability_snapshot_id"],),
    )
    market = storage.fetch_all(
        "SELECT evidence_fingerprint,capability_snapshot_id,config_hash FROM crypto_market_data_evidence WHERE id=?",
        (row["market_evidence_id"],),
    )
    risk = storage.fetch_all(
        "SELECT snapshot_fingerprint,config_hash FROM crypto_risk_snapshots WHERE id=?",
        (row["risk_snapshot_id"],),
    )
    if len(capability) != 1 or len(market) != 1 or len(risk) != 1:
        raise RuntimeError("crypto sizing evidence relationship is incomplete")
    if (
        capability[0]["snapshot_fingerprint"] != row["capability_snapshot_fingerprint"]
        or market[0]["evidence_fingerprint"] != row["market_evidence_fingerprint"]
        or market[0]["capability_snapshot_id"] != row["capability_snapshot_id"]
        or risk[0]["snapshot_fingerprint"] != row["risk_snapshot_fingerprint"]
        or any(value["config_hash"] != config_hash for value in (capability[0], market[0], risk[0]))
    ):
        raise RuntimeError("crypto sizing evidence relationship mismatch")
    result = CryptoSizingDecision(
        id=row["id"], run_id=row["run_id"], request_fingerprint=row["request_fingerprint"],
        risk_snapshot_id=row["risk_snapshot_id"], risk_snapshot_fingerprint=row["risk_snapshot_fingerprint"],
        capability_snapshot_id=row["capability_snapshot_id"],
        capability_snapshot_fingerprint=row["capability_snapshot_fingerprint"],
        market_evidence_id=row["market_evidence_id"], market_evidence_fingerprint=row["market_evidence_fingerprint"],
        symbol=row["symbol"], side=row["side"], action=row["action"], request_basis=row["request_basis"],
        limit_price=row["limit_price"], stop_price=row["stop_price"],
        stop_execution_price=row["stop_execution_price"], canonical_quantity=row["canonical_quantity"],
        canonical_notional=row["canonical_notional"], canonical_stop_risk=row["canonical_stop_risk"],
        gross_stop_risk=row["gross_stop_risk"], estimated_fees=row["estimated_fees"],
        estimated_stop_slippage=row["estimated_stop_slippage"], minimum_order_size=row["minimum_order_size"],
        quantity_increment=row["quantity_increment"], price_increment=row["price_increment"],
        eligible=bool(row["eligible"]), blockers=tuple(blockers), binding_caps=tuple(binding_caps),
        authoritative=bool(row["authoritative"]), execution_authorized=False,
        config_hash=row["config_hash"], formula_version=row["formula_version"],
        schema_version=row["schema_version"], created_at=row["created_at"],
        decision_fingerprint=row["decision_fingerprint"], payload=payload,
    )
    # Recompute the complete Decimal result from independently verified source
    # records.  Replacing both decision_json and its local digest cannot bless
    # changed arithmetic or changed relational authority.
    from datetime import datetime
    from .crypto_capabilities import CryptoCapabilityStore
    from .crypto_market_data import CryptoMarketDataStore
    from .crypto_risk import CryptoRiskStore

    created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    risk_payload = CryptoRiskStore(storage).load_verified(row["risk_snapshot_id"], config, now=created)
    capability_snapshot = CryptoCapabilityStore(storage).load_verified(
        row["capability_snapshot_id"], config, now=created
    )
    market_evidence = CryptoMarketDataStore(storage).load_verified(row["market_evidence_id"], config)
    request = CryptoSizingRequest(
        source_type=request_payload.get("source_type"),
        source_id=request_payload.get("source_id"),
        source_fingerprint=request_payload.get("source_fingerprint"),
        symbol=request_payload.get("symbol"),
        side=request_payload.get("side"),
        action=request_payload.get("action"),
        request_basis=request_payload.get("request_basis"),
        requested_stop_risk_dollars=request_payload.get("requested_stop_risk_dollars"),
        stop_price=request_payload.get("stop_price"),
        requested_exit_quantity=request_payload.get("requested_exit_quantity"),
        close_entire_position=request_payload.get("close_entire_position") is True,
    )
    authority_payload = payload["risk_authority"]
    authority = CryptoSizingAuthority(
        risk_snapshot_id=authority_payload.get("risk_snapshot_id"),
        risk_snapshot_fingerprint=authority_payload.get("risk_snapshot_fingerprint"),
        paper_account_id_hash=authority_payload.get("paper_account_id_hash"),
        config_hash=authority_payload.get("config_hash"),
        hard_notional_ceiling=_trusted_decimal(
            authority_payload.get("hard_notional_ceiling"), "risk hard notional ceiling"
        ),
        hard_stop_risk_ceiling=_trusted_decimal(
            authority_payload.get("hard_stop_risk_ceiling"), "risk hard stop-risk ceiling"
        ),
        current_position_quantity=_trusted_decimal(
            authority_payload.get("current_position_quantity"), "risk current position quantity"
        ),
        pending_sell_quantity=_trusted_decimal(
            authority_payload.get("pending_sell_quantity"), "risk pending sell quantity"
        ),
        authoritative=authority_payload.get("authoritative") is True,
        failure_reasons=tuple(authority_payload.get("failure_reasons") or ()),
    )
    expected_authority = {
        "risk_snapshot_id": risk_payload["id"],
        "risk_snapshot_fingerprint": _hash(risk_payload),
        "paper_account_id_hash": risk_payload["paper_account_id_hash"],
        "config_hash": risk_payload["config_hash"],
        "hard_notional_ceiling": risk_payload["derived_authority"]["hard_notional_ceiling"],
        "hard_stop_risk_ceiling": risk_payload["derived_authority"]["hard_stop_risk_ceiling"],
        "current_position_quantity": risk_payload["aggregate"]["position_quantity"],
        "pending_sell_quantity": risk_payload["aggregate"]["pending_symbol_sell_quantity"],
        "authoritative": risk_payload["authoritative"],
        "failure_reasons": risk_payload["failure_reasons"],
    }
    if authority.payload() != expected_authority:
        raise RuntimeError("crypto sizing risk authority derivation mismatch")
    recomputed = calculate_crypto_sizing(
        decision_id=row["id"], run_id=row["run_id"], request=request,
        authority=authority, capability=capability_snapshot, market=market_evidence,
        config=config, created_at=row["created_at"],
    )
    if recomputed.payload != payload or recomputed.decision_fingerprint != row["decision_fingerprint"]:
        raise RuntimeError("crypto sizing independent Decimal recomputation mismatch")
    return result


__all__ = [
    "CryptoSizingAuthority",
    "CryptoSizingDecision",
    "CryptoSizingError",
    "CryptoSizingRequest",
    "apply_crypto_sizing_schema",
    "calculate_crypto_sizing",
    "insert_crypto_sizing",
    "load_verified_crypto_sizing",
]
