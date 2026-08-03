"""Bounded, append-only crypto outcome and evidence sidecar.

This module is intentionally standalone.  It owns only the SQLite table
``crypto_profitability_observations`` and can therefore be integrated into
Plutus without changing the existing trading or production-database code.
All prices, quantities, PnL, returns, risk, costs, probabilities, holding
hours, and correlations are handled as :class:`~decimal.Decimal` values and
are persisted as canonical decimal text.  Python ``float`` values are rejected
at the evidence boundary.

Integration API
---------------

``bootstrap_schema(connection_or_path)`` creates the table, indexes, and the
append-only trigger.  It accepts a ``sqlite3.Connection``, a path-like value,
or an object exposing the existing project's ``connect()`` context manager.

``calculate_shadow_outcome(setup_payload, hourly_bars, *, horizon_hours,
cost_model)`` normalizes a setup and its post-signal hourly bars, then returns
an immutable :class:`CryptoOutcomeObservation`.  ``horizon_hours`` and the
cost model must be explicit; there is no implicit holding period or free-cost
assumption.  Bars whose UTC timestamp is equal to or before the research
timestamp are excluded.  For a long setup, a bar touching both stop and target
is resolved as a stop; short setups use the analogous stop-first rule.  If a
future bar is not available through the requested horizon, the observation is
``status='maturing'`` and ``outcome_class='unavailable'`` with all outcome
amounts left ``None``.

``persist_observation(connection_or_store, observation)`` persists a shadow or
validated actual observation idempotently.  The unique immutable
``input_fingerprint`` is SHA-256 over canonical setup, post-signal bars, and
cost-model evidence.  Replaying the same evidence is a no-op.  Changed
evidence creates a different fingerprint; attempting to mutate an existing
row is rejected by the SQLite trigger and by the Python API.

``derive_aggregate_metrics(observations, *, severe_loss_threshold, prior=None,
verified_snapshot=None)`` excludes maturing/unavailable observations and
returns exact Decimal aggregate metrics.  A posterior is returned only when a
caller supplies an explicit ``BetaPrior``.  Correlation is returned only from
a supplied verified snapshot.  No probability, holding-hour, or correlation
default is inserted when evidence is absent.

``validate_actual_evidence(lineage)`` checks the minimum actual-evidence
lineage: a verified fill, a verified performance link, and a position
lifecycle whose state is ``closed``.  The validator never constructs a fill,
performance link, or lifecycle.  ``build_actual_observation`` is provided for
later integration and requires that lineage before accepting caller-supplied
actual values.

SQLite schema
-------------

The sidecar table is:

``crypto_profitability_observations``

``observation_id TEXT PRIMARY KEY``
    Deterministic ``<evidence_type>:<input_fingerprint>`` identifier.
``input_fingerprint TEXT NOT NULL UNIQUE``
    SHA-256 of the canonical input evidence; immutable and append-only.
``evidence_type TEXT NOT NULL``
    ``shadow`` or ``actual``.
``status TEXT NOT NULL``
    ``completed`` or ``maturing`` (``unavailable`` is represented by a
    maturing row with ``outcome_class='unavailable'``).
``outcome_class TEXT``
    ``stop_hit``, ``target_hit``, ``horizon_close``, or ``unavailable``.
``reason TEXT NOT NULL``
    Deterministic calculation or validation reason.
``symbol, side, strategy_decision_id, strategy_decision_fingerprint``
    Decision identity and normalized setup direction.
``research_timestamp TEXT NOT NULL``
    Canonical UTC timestamp.
``horizon_hours INTEGER NOT NULL``
    Explicit requested elapsed UTC horizon.
``entry_price, stop_price, target_price, quantity TEXT``
    Canonical Decimal text; quantity is nullable when the setup has no size.
``exit_timestamp, exit_price, holding_hours TEXT``
    Canonical observed exit values; nullable for maturing rows.
``gross_return, net_return, gross_pnl, net_pnl, risk_amount,
gross_r_multiple, net_r_multiple TEXT``
    Derived exact Decimal text; nullable when unavailable or when quantity is
    absent for PnL/R values.
``cost_model_version, fee_bps, spread_bps, slippage_bps TEXT NOT NULL``
    Explicit cost model provenance.  ``fee_bps`` is per leg; spread and
    slippage are round-trip bps.  Net return subtracts
    ``(2*fee_bps + spread_bps + slippage_bps) / 10000``.
``bar_count INTEGER NOT NULL, bars_json TEXT NOT NULL``
    Count and canonical post-signal bar evidence.
``setup_json, cost_model_json, input_evidence_json TEXT NOT NULL``
    Canonical replayable evidence payloads.
``actual_lineage_json TEXT``
    Required only for ``evidence_type='actual'`` and never synthesized.
``created_at TEXT NOT NULL``
    UTC persistence timestamp; not part of the input fingerprint.

The table is deliberately append-only.  A SQLite ``BEFORE UPDATE`` trigger
rejects every update so the fingerprint and its evidence cannot be silently
rewritten.  There is no foreign key to the existing Plutus schema and the
bootstrap function never touches any other table.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable, Iterator, Mapping, Sequence


TABLE_NAME = "crypto_profitability_observations"
SCHEMA_VERSION = "crypto_profitability_observations_v1"
CALCULATION_VERSION = "crypto_shadow_outcome_v1"
EVIDENCE_VERSION = "crypto_outcome_evidence_v1"
_BPS = Decimal("10000")
_HOUR_MICROSECONDS = 3_600_000_000
_ONE_HOUR = timedelta(hours=1)


class CryptoOutcomeError(ValueError):
    """Base error for invalid or unverifiable sidecar evidence."""


class ActualEvidenceError(CryptoOutcomeError):
    """Raised when an actual observation lacks verified lineage."""


class ImmutableObservationError(CryptoOutcomeError):
    """Raised when an existing append-only observation would be changed."""


def _as_decimal(value: Any, field: str) -> Decimal:
    """Parse a finite Decimal without allowing binary floating point."""

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must be Decimal text, Decimal, or int; float is not accepted")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} cannot be empty")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"{field} is not valid Decimal text") from exc
    else:
        raise TypeError(f"{field} must be Decimal text, Decimal, or int")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def canonical_decimal_text(value: Any, field: str = "decimal") -> str:
    """Return the canonical fixed-point Decimal text used by this sidecar.

    Exponent notation is expanded, insignificant trailing fractional zeroes
    are removed, and both positive and negative zero become ``"0"``.  This
    preserves the exact Decimal value while making equivalent text fingerprint
    identically.  The function intentionally rejects ``float``.
    """

    value_decimal = _as_decimal(value, field)
    if value_decimal == 0:
        return "0"
    text = format(value_decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _canonicalize(value: Any, *, field: str = "evidence") -> Any:
    """Recursively turn evidence into JSON-safe, deterministic values."""

    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        raise TypeError(f"{field} contains float; use Decimal text")
    if isinstance(value, Decimal):
        return canonical_decimal_text(value, field)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field} mapping keys must be strings")
            normalized[key] = _canonicalize(item, field=f"{field}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item, field=f"{field}[]") for item in value]
    raise TypeError(f"{field} contains unsupported value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize evidence with sorted keys and no insignificant whitespace."""

    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse_utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} cannot be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 timestamp")
    if parsed.tzinfo is None:
        # Naive inputs are interpreted as UTC at this boundary.  The stored
        # canonical value is always explicitly timezone-aware UTC.
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed


def canonical_timestamp(value: Any, field: str = "timestamp") -> str:
    """Return a canonical UTC ISO-8601 timestamp with microsecond precision."""

    return _parse_utc(value, field).isoformat(timespec="microseconds")


def _elapsed_hours(start: datetime, end: datetime) -> Decimal:
    """Calculate positive elapsed UTC hours without ``timedelta`` floats."""

    delta = end - start
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros < 0:
        raise ValueError("elapsed time cannot be negative")
    return Decimal(micros) / Decimal(_HOUR_MICROSECONDS)


def _pick(payload: Mapping[str, Any], *keys: str, required: bool = True) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    if required:
        raise ValueError(f"missing required field: {keys[0]}")
    return None


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = _as_decimal(value, field)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    result = _as_decimal(value, field)
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or isinstance(value, float) or isinstance(value, Decimal):
        raise TypeError(f"{field} must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must be a positive integer")
        try:
            result = int(text)
        except ValueError as exc:
            raise ValueError(f"{field} must be a positive integer") from exc
    else:
        raise TypeError(f"{field} must be a positive integer")
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


@dataclass(frozen=True, slots=True)
class CryptoCostModel:
    """Explicit cost model for one round trip.

    ``fee_bps`` is charged on each leg.  ``spread_bps`` and ``slippage_bps``
    are already round-trip amounts.  Keeping those units explicit prevents a
    later integration from silently applying a cost twice.
    """

    version: str
    fee_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        version = str(self.version).strip()
        if not version:
            raise ValueError("cost model version is required")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "fee_bps", _nonnegative_decimal(self.fee_bps, "fee_bps"))
        object.__setattr__(self, "spread_bps", _nonnegative_decimal(self.spread_bps, "spread_bps"))
        object.__setattr__(self, "slippage_bps", _nonnegative_decimal(self.slippage_bps, "slippage_bps"))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CryptoCostModel":
        if not isinstance(payload, Mapping):
            raise TypeError("cost_model must be a mapping")
        fee = _pick(payload, "fee_bps", "fees_bps")
        spread = _pick(payload, "spread_bps")
        slippage = _pick(payload, "slippage_bps")
        return cls(
            version=_pick(payload, "version", "cost_model_version", "model_version"),
            fee_bps=fee,
            spread_bps=spread,
            slippage_bps=slippage,
        )

    @property
    def round_trip_bps(self) -> Decimal:
        return (self.fee_bps * Decimal(2)) + self.spread_bps + self.slippage_bps

    @property
    def round_trip_rate(self) -> Decimal:
        return self.round_trip_bps / _BPS

    def to_payload(self) -> dict[str, str]:
        return {
            "version": self.version,
            "fee_bps": canonical_decimal_text(self.fee_bps, "fee_bps"),
            "spread_bps": canonical_decimal_text(self.spread_bps, "spread_bps"),
            "slippage_bps": canonical_decimal_text(self.slippage_bps, "slippage_bps"),
        }


@dataclass(frozen=True, slots=True)
class CryptoHourlyBar:
    """Validated UTC hourly bar used as immutable shadow evidence."""

    timestamp: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    open: Decimal | None = None
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        timestamp = _parse_utc(self.timestamp, "bar.timestamp")
        high = _positive_decimal(self.high, "bar.high")
        low = _positive_decimal(self.low, "bar.low")
        close = _positive_decimal(self.close, "bar.close")
        opening = None if self.open is None else _positive_decimal(self.open, "bar.open")
        volume = None if self.volume is None else _nonnegative_decimal(self.volume, "bar.volume")
        if high < low:
            raise ValueError("bar.high cannot be lower than bar.low")
        if not low <= close <= high:
            raise ValueError("bar.close must lie between bar.low and bar.high")
        if opening is not None and not low <= opening <= high:
            raise ValueError("bar.open must lie between bar.low and bar.high")
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "open", opening)
        object.__setattr__(self, "volume", volume)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CryptoHourlyBar":
        if not isinstance(payload, Mapping):
            raise TypeError("each hourly bar must be a mapping or CryptoHourlyBar")
        return cls(
            timestamp=_pick(payload, "timestamp", "bar_timestamp", "time", "ts", "t"),
            high=_pick(payload, "high", "h"),
            low=_pick(payload, "low", "l"),
            close=_pick(payload, "close", "c"),
            open=_pick(payload, "open", "o", required=False),
            volume=_pick(payload, "volume", "v", required=False),
        )

    def to_payload(self) -> dict[str, str]:
        payload = {
            "timestamp": canonical_timestamp(self.timestamp),
            "high": canonical_decimal_text(self.high, "bar.high"),
            "low": canonical_decimal_text(self.low, "bar.low"),
            "close": canonical_decimal_text(self.close, "bar.close"),
        }
        if self.open is not None:
            payload["open"] = canonical_decimal_text(self.open, "bar.open")
        if self.volume is not None:
            payload["volume"] = canonical_decimal_text(self.volume, "bar.volume")
        return payload


@dataclass(frozen=True, slots=True)
class CryptoSetup:
    """Normalized shadow setup required by :func:`calculate_shadow_outcome`."""

    symbol: str
    strategy_decision_id: str
    strategy_decision_fingerprint: str
    research_timestamp: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    horizon_hours: int
    cost_model: CryptoCostModel
    side: str = "long"
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        decision_id = str(self.strategy_decision_id).strip()
        decision_fp = str(self.strategy_decision_fingerprint).strip()
        if not symbol:
            raise ValueError("symbol is required")
        if not decision_id:
            raise ValueError("strategy_decision_id is required")
        if not decision_fp:
            raise ValueError("strategy_decision_fingerprint is required")
        side = str(self.side).strip().lower()
        if side in {"buy", "long"}:
            side = "long"
        elif side in {"sell", "short"}:
            side = "short"
        else:
            raise ValueError("side must be long/buy or short/sell")
        horizon_hours = _positive_int(self.horizon_hours, "horizon_hours")
        entry = _positive_decimal(self.entry_price, "entry_price")
        stop = _positive_decimal(self.stop_price, "stop_price")
        target = _positive_decimal(self.target_price, "target_price")
        quantity = None if self.quantity is None else _positive_decimal(self.quantity, "quantity")
        if side == "long" and not (stop < entry < target):
            raise ValueError("long setup requires stop_price < entry_price < target_price")
        if side == "short" and not (target < entry < stop):
            raise ValueError("short setup requires target_price < entry_price < stop_price")
        selected_cost_model = self.cost_model
        if isinstance(selected_cost_model, Mapping):
            selected_cost_model = CryptoCostModel.from_payload(selected_cost_model)
        if not isinstance(selected_cost_model, CryptoCostModel):
            raise TypeError("cost_model must be CryptoCostModel or a mapping")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "strategy_decision_id", decision_id)
        object.__setattr__(self, "strategy_decision_fingerprint", decision_fp)
        object.__setattr__(self, "research_timestamp", _parse_utc(self.research_timestamp, "research_timestamp"))
        object.__setattr__(self, "entry_price", entry)
        object.__setattr__(self, "stop_price", stop)
        object.__setattr__(self, "target_price", target)
        object.__setattr__(self, "horizon_hours", horizon_hours)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cost_model", selected_cost_model)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        horizon_hours: int | None = None,
        cost_model: CryptoCostModel | Mapping[str, Any] | None = None,
    ) -> "CryptoSetup":
        if not isinstance(payload, Mapping):
            raise TypeError("setup_payload must be a mapping")
        payload_horizon = _pick(
            payload,
            "horizon_hours",
            "evaluation_horizon_hours",
            "max_holding_hours",
            required=False,
        )
        if horizon_hours is not None and payload_horizon is not None and _positive_int(horizon_hours, "horizon_hours") != _positive_int(payload_horizon, "horizon_hours"):
            raise ValueError("explicit horizon_hours conflicts with setup payload")
        selected_horizon = horizon_hours if horizon_hours is not None else payload_horizon
        if selected_horizon is None:
            raise ValueError("horizon_hours must be explicit; no holding-period default exists")

        payload_cost_model = _pick(payload, "cost_model", required=False)
        if payload_cost_model is None and any(
            key in payload
            for key in ("cost_model_version", "fee_bps", "fees_bps", "spread_bps", "slippage_bps")
        ):
            payload_cost_model = {
                "version": _pick(payload, "cost_model_version", "model_version", required=False),
                "fee_bps": _pick(payload, "fee_bps", "fees_bps", required=False),
                "spread_bps": _pick(payload, "spread_bps", required=False),
                "slippage_bps": _pick(payload, "slippage_bps", required=False),
            }
        explicit_cost_model = None
        if cost_model is not None:
            explicit_cost_model = CryptoCostModel.from_payload(cost_model) if isinstance(cost_model, Mapping) else cost_model
            if not isinstance(explicit_cost_model, CryptoCostModel):
                raise TypeError("cost_model must be CryptoCostModel or a mapping")
        if explicit_cost_model is not None and payload_cost_model is not None:
            selected_cost_model = CryptoCostModel.from_payload(payload_cost_model) if isinstance(payload_cost_model, Mapping) else payload_cost_model
            if not isinstance(selected_cost_model, CryptoCostModel) or selected_cost_model != explicit_cost_model:
                raise ValueError("explicit cost_model conflicts with setup payload")
        elif explicit_cost_model is not None:
            selected_cost_model = explicit_cost_model
        elif payload_cost_model is not None:
            selected_cost_model = payload_cost_model
        else:
            raise ValueError("cost_model must be explicit; no zero-cost default exists")
        if isinstance(selected_cost_model, Mapping):
            selected_cost_model = CryptoCostModel.from_payload(selected_cost_model)
        if not isinstance(selected_cost_model, CryptoCostModel):
            raise TypeError("cost_model must be CryptoCostModel or a mapping")

        decision_payload = _pick(payload, "strategy_decision", "decision", required=False)
        if not isinstance(decision_payload, Mapping):
            decision_payload = {}
        return cls(
            symbol=_pick(payload, "symbol"),
            strategy_decision_id=_pick(payload, "strategy_decision_id", "decision_id", required=False)
            or _pick(decision_payload, "id", "strategy_decision_id"),
            strategy_decision_fingerprint=_pick(
                payload,
                "strategy_decision_fingerprint",
                "decision_fingerprint",
                "strategy_fingerprint",
                required=False,
            )
            or _pick(decision_payload, "fingerprint", "strategy_decision_fingerprint"),
            research_timestamp=_pick(payload, "research_timestamp", "signal_timestamp"),
            entry_price=_pick(payload, "entry_price", "entry"),
            stop_price=_pick(payload, "stop_price", "stop"),
            target_price=_pick(payload, "target_price", "target"),
            horizon_hours=selected_horizon,
            cost_model=selected_cost_model,
            side=_pick(payload, "side", "direction", required=False) or "long",
            quantity=_pick(payload, "quantity", "position_quantity", required=False),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "strategy_decision_id": self.strategy_decision_id,
            "strategy_decision_fingerprint": self.strategy_decision_fingerprint,
            "research_timestamp": canonical_timestamp(self.research_timestamp),
            "entry_price": canonical_decimal_text(self.entry_price, "entry_price"),
            "stop_price": canonical_decimal_text(self.stop_price, "stop_price"),
            "target_price": canonical_decimal_text(self.target_price, "target_price"),
            "horizon_hours": self.horizon_hours,
            "side": self.side,
            "cost_model": self.cost_model.to_payload(),
        }
        if self.quantity is not None:
            payload["quantity"] = canonical_decimal_text(self.quantity, "quantity")
        return payload


@dataclass(frozen=True, slots=True)
class CryptoOutcomeObservation:
    """Immutable normalized observation returned by shadow or actual APIs."""

    observation_id: str
    input_fingerprint: str
    evidence_type: str
    status: str
    outcome_class: str
    reason: str
    setup: CryptoSetup
    bars: tuple[CryptoHourlyBar, ...]
    exit_timestamp: datetime | None
    exit_price: Decimal | None
    holding_hours: Decimal | None
    gross_return: Decimal | None
    net_return: Decimal | None
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    risk_amount: Decimal | None
    gross_r_multiple: Decimal | None
    net_r_multiple: Decimal | None
    actual_lineage_json: str | None = None
    input_evidence_json: str = ""

    def __post_init__(self) -> None:
        evidence_type = str(self.evidence_type).strip().lower()
        if evidence_type not in {"shadow", "actual"}:
            raise ValueError("evidence_type must be shadow or actual")
        status = str(self.status).strip().lower()
        if status not in {"completed", "maturing"}:
            raise ValueError("status must be completed or maturing")
        outcome_class = str(self.outcome_class).strip().lower()
        if status == "maturing" and outcome_class != "unavailable":
            raise ValueError("maturing observations must have outcome_class='unavailable'")
        if status == "completed" and outcome_class == "unavailable":
            raise ValueError("completed observations cannot be unavailable")
        if evidence_type == "actual" and not self.actual_lineage_json:
            raise ActualEvidenceError("actual observations require verified lineage")
        if evidence_type == "shadow" and self.actual_lineage_json is not None:
            raise ValueError("shadow observations cannot contain actual lineage")
        if status == "maturing":
            for name in (
                "exit_timestamp",
                "exit_price",
                "holding_hours",
                "gross_return",
                "net_return",
                "gross_pnl",
                "net_pnl",
                "gross_r_multiple",
                "net_r_multiple",
            ):
                if getattr(self, name) is not None:
                    raise ValueError(f"maturing observation cannot have {name}")
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "outcome_class", outcome_class)
        object.__setattr__(self, "observation_id", str(self.observation_id))
        object.__setattr__(self, "input_fingerprint", str(self.input_fingerprint))
        object.__setattr__(self, "exit_timestamp", None if self.exit_timestamp is None else _parse_utc(self.exit_timestamp, "exit_timestamp"))
        for name in (
            "exit_price",
            "holding_hours",
            "gross_return",
            "net_return",
            "gross_pnl",
            "net_pnl",
            "risk_amount",
            "gross_r_multiple",
            "net_r_multiple",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _as_decimal(value, name))
        if not self.input_evidence_json:
            raise ValueError("input_evidence_json is required")
        try:
            parsed_evidence = json.loads(self.input_evidence_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("input_evidence_json must be canonical JSON") from exc
        canonical_evidence = canonical_json(parsed_evidence)
        if canonical_evidence != self.input_evidence_json:
            raise ValueError("input_evidence_json must be canonical JSON")
        expected_fingerprint = hashlib.sha256(canonical_evidence.encode("utf-8")).hexdigest()
        if str(self.input_fingerprint) != expected_fingerprint:
            raise ImmutableObservationError("input_fingerprint does not match canonical input evidence")

    @property
    def available(self) -> bool:
        return self.status == "completed" and self.net_return is not None

    def to_record(self) -> dict[str, Any]:
        """Return the database-shaped record with exact Decimal text."""

        return {
            "observation_id": self.observation_id,
            "input_fingerprint": self.input_fingerprint,
            "evidence_type": self.evidence_type,
            "status": self.status,
            "outcome_class": self.outcome_class,
            "reason": self.reason,
            "symbol": self.setup.symbol,
            "side": self.setup.side,
            "strategy_decision_id": self.setup.strategy_decision_id,
            "strategy_decision_fingerprint": self.setup.strategy_decision_fingerprint,
            "research_timestamp": canonical_timestamp(self.setup.research_timestamp),
            "horizon_hours": self.setup.horizon_hours,
            "entry_price": canonical_decimal_text(self.setup.entry_price, "entry_price"),
            "stop_price": canonical_decimal_text(self.setup.stop_price, "stop_price"),
            "target_price": canonical_decimal_text(self.setup.target_price, "target_price"),
            "quantity": None if self.setup.quantity is None else canonical_decimal_text(self.setup.quantity, "quantity"),
            "exit_timestamp": None if self.exit_timestamp is None else canonical_timestamp(self.exit_timestamp),
            "exit_price": None if self.exit_price is None else canonical_decimal_text(self.exit_price, "exit_price"),
            "holding_hours": None if self.holding_hours is None else canonical_decimal_text(self.holding_hours, "holding_hours"),
            "gross_return": None if self.gross_return is None else canonical_decimal_text(self.gross_return, "gross_return"),
            "net_return": None if self.net_return is None else canonical_decimal_text(self.net_return, "net_return"),
            "gross_pnl": None if self.gross_pnl is None else canonical_decimal_text(self.gross_pnl, "gross_pnl"),
            "net_pnl": None if self.net_pnl is None else canonical_decimal_text(self.net_pnl, "net_pnl"),
            "risk_amount": None if self.risk_amount is None else canonical_decimal_text(self.risk_amount, "risk_amount"),
            "gross_r_multiple": None if self.gross_r_multiple is None else canonical_decimal_text(self.gross_r_multiple, "gross_r_multiple"),
            "net_r_multiple": None if self.net_r_multiple is None else canonical_decimal_text(self.net_r_multiple, "net_r_multiple"),
            "cost_model_version": self.setup.cost_model.version,
            "fee_bps": canonical_decimal_text(self.setup.cost_model.fee_bps, "fee_bps"),
            "spread_bps": canonical_decimal_text(self.setup.cost_model.spread_bps, "spread_bps"),
            "slippage_bps": canonical_decimal_text(self.setup.cost_model.slippage_bps, "slippage_bps"),
            "bar_count": len(self.bars),
            "bars_json": canonical_json([bar.to_payload() for bar in self.bars]),
            "setup_json": canonical_json(self.setup.to_payload()),
            "cost_model_json": canonical_json(self.setup.cost_model.to_payload()),
            "input_evidence_json": self.input_evidence_json,
            "actual_lineage_json": self.actual_lineage_json,
        }


@dataclass(frozen=True, slots=True)
class PersistResult:
    """Result of an idempotent append operation."""

    observation_id: str
    input_fingerprint: str
    inserted: bool


@dataclass(frozen=True, slots=True)
class ActualEvidenceValidation:
    """Non-fabricating result of actual lineage validation."""

    valid: bool
    missing: tuple[str, ...]
    reason: str
    canonical_lineage_json: str | None


@dataclass(frozen=True, slots=True)
class BetaPrior:
    """Explicit Beta prior; no prior is assumed by aggregate metrics."""

    alpha: Decimal
    beta: Decimal

    def __post_init__(self) -> None:
        alpha = _positive_decimal(self.alpha, "prior.alpha")
        beta = _positive_decimal(self.beta, "prior.beta")
        object.__setattr__(self, "alpha", alpha)
        object.__setattr__(self, "beta", beta)

    @classmethod
    def from_value(cls, value: "BetaPrior | Mapping[str, Any]") -> "BetaPrior":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("prior must be BetaPrior or a mapping")
        return cls(alpha=_pick(value, "alpha"), beta=_pick(value, "beta"))


@dataclass(frozen=True, slots=True)
class VerifiedCorrelationSnapshot:
    """Explicit verified correlation evidence accepted by aggregate metrics."""

    snapshot_id: str
    snapshot_fingerprint: str
    correlation: Decimal

    def __post_init__(self) -> None:
        snapshot_id = str(self.snapshot_id).strip()
        fingerprint = str(self.snapshot_fingerprint).strip()
        if not snapshot_id or not fingerprint:
            raise ValueError("verified correlation snapshot needs id and fingerprint")
        correlation = _as_decimal(self.correlation, "correlation")
        if correlation < Decimal("-1") or correlation > Decimal("1"):
            raise ValueError("correlation must be between -1 and 1")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "snapshot_fingerprint", fingerprint)
        object.__setattr__(self, "correlation", correlation)


def _normalize_bars(signal_timestamp: datetime, hourly_bars: Iterable[Mapping[str, Any] | CryptoHourlyBar]) -> tuple[CryptoHourlyBar, ...]:
    if isinstance(hourly_bars, (str, bytes, Mapping)):
        raise TypeError("hourly_bars must be an iterable of bars")
    normalized: list[CryptoHourlyBar] = []
    for payload in hourly_bars:
        bar = payload if isinstance(payload, CryptoHourlyBar) else CryptoHourlyBar.from_mapping(payload)
        if bar.timestamp > signal_timestamp:
            normalized.append(bar)
    normalized.sort(key=lambda item: item.timestamp)
    for prior, current in zip(normalized, normalized[1:]):
        if current.timestamp == prior.timestamp:
            raise ValueError("duplicate post-signal hourly bar timestamp")
    return tuple(normalized)


def _input_evidence(setup: CryptoSetup, bars: Sequence[CryptoHourlyBar]) -> dict[str, Any]:
    return {
        "calculation_version": CALCULATION_VERSION,
        "evidence_version": EVIDENCE_VERSION,
        "setup": setup.to_payload(),
        "bars": [bar.to_payload() for bar in bars],
    }


def compute_input_fingerprint(
    setup_payload: Mapping[str, Any] | CryptoSetup,
    hourly_bars: Iterable[Mapping[str, Any] | CryptoHourlyBar],
    *,
    horizon_hours: int | None = None,
    cost_model: CryptoCostModel | Mapping[str, Any] | None = None,
) -> str:
    """Compute the immutable fingerprint for normalized shadow evidence."""

    setup = setup_payload if isinstance(setup_payload, CryptoSetup) else CryptoSetup.from_payload(
        setup_payload, horizon_hours=horizon_hours, cost_model=cost_model
    )
    bars = _normalize_bars(setup.research_timestamp, hourly_bars)
    return _sha256_json(_input_evidence(setup, bars))


def _return_for_side(side: str, entry: Decimal, exit_price: Decimal) -> Decimal:
    if side == "long":
        return (exit_price - entry) / entry
    return (entry - exit_price) / entry


def _outcome_amounts(
    setup: CryptoSetup,
    exit_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    gross_return = _return_for_side(setup.side, setup.entry_price, exit_price)
    net_return = gross_return - setup.cost_model.round_trip_rate
    if setup.quantity is None:
        return gross_return, net_return, None, None, None, None, None
    if setup.side == "long":
        gross_pnl = (exit_price - setup.entry_price) * setup.quantity
    else:
        gross_pnl = (setup.entry_price - exit_price) * setup.quantity
    net_pnl = net_return * setup.entry_price * setup.quantity
    risk_amount = abs(setup.entry_price - setup.stop_price) * setup.quantity
    if risk_amount == 0:
        return gross_return, net_return, gross_pnl, net_pnl, risk_amount, None, None
    return (
        gross_return,
        net_return,
        gross_pnl,
        net_pnl,
        risk_amount,
        gross_pnl / risk_amount,
        net_pnl / risk_amount,
    )


def _make_shadow_observation(
    setup: CryptoSetup,
    bars: tuple[CryptoHourlyBar, ...],
    *,
    status: str,
    outcome_class: str,
    reason: str,
    exit_bar: CryptoHourlyBar | None = None,
    exit_price: Decimal | None = None,
) -> CryptoOutcomeObservation:
    evidence = _input_evidence(setup, bars)
    evidence_json = canonical_json(evidence)
    fingerprint = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    if status == "maturing":
        amounts: tuple[Decimal | None, ...] = (None, None, None, None, None, None, None)
        exit_timestamp = None
        holding_hours = None
    else:
        if exit_bar is None or exit_price is None:
            raise ValueError("completed shadow observation requires an exit bar and price")
        amounts = _outcome_amounts(setup, exit_price)
        exit_timestamp = exit_bar.timestamp
        holding_hours = _elapsed_hours(setup.research_timestamp, exit_bar.timestamp)
    return CryptoOutcomeObservation(
        observation_id=f"shadow:{fingerprint}",
        input_fingerprint=fingerprint,
        evidence_type="shadow",
        status=status,
        outcome_class=outcome_class,
        reason=reason,
        setup=setup,
        bars=bars,
        exit_timestamp=exit_timestamp,
        exit_price=exit_price,
        holding_hours=holding_hours,
        gross_return=amounts[0],
        net_return=amounts[1],
        gross_pnl=amounts[2],
        net_pnl=amounts[3],
        risk_amount=amounts[4],
        gross_r_multiple=amounts[5],
        net_r_multiple=amounts[6],
        input_evidence_json=evidence_json,
    )


def calculate_shadow_outcome(
    setup_payload: Mapping[str, Any] | CryptoSetup,
    hourly_bars: Iterable[Mapping[str, Any] | CryptoHourlyBar],
    *,
    horizon_hours: int | None = None,
    cost_model: CryptoCostModel | Mapping[str, Any] | None = None,
) -> CryptoOutcomeObservation:
    """Calculate a deterministic long/short crypto shadow outcome.

    The bar timestamp is treated as the observation time of that hourly bar.
    A stop or target is actionable only at or before the explicit elapsed UTC
    horizon.  The first bar at or after the horizon supplies the fixed-horizon
    close when no barrier was hit.  A gap larger than one UTC hour before the
    horizon is considered missing future evidence; the result remains
    maturing rather than guessing a zero return.
    """

    setup = setup_payload if isinstance(setup_payload, CryptoSetup) else CryptoSetup.from_payload(
        setup_payload, horizon_hours=horizon_hours, cost_model=cost_model
    )
    bars = _normalize_bars(setup.research_timestamp, hourly_bars)
    horizon = Decimal(setup.horizon_hours)
    last_timestamp = setup.research_timestamp

    for bar in bars:
        elapsed = _elapsed_hours(setup.research_timestamp, bar.timestamp)
        gap = bar.timestamp - last_timestamp
        if gap > _ONE_HOUR:
            return _make_shadow_observation(
                setup,
                bars,
                status="maturing",
                outcome_class="unavailable",
                reason="missing_hourly_bar_gap",
            )
        last_timestamp = bar.timestamp

        if elapsed <= horizon:
            if setup.side == "long":
                stop_hit = bar.low <= setup.stop_price
                target_hit = bar.high >= setup.target_price
            else:
                stop_hit = bar.high >= setup.stop_price
                target_hit = bar.low <= setup.target_price
            if stop_hit:
                return _make_shadow_observation(
                    setup,
                    bars,
                    status="completed",
                    outcome_class="stop_hit",
                    reason="stop_hit_stop_first",
                    exit_bar=bar,
                    exit_price=setup.stop_price,
                )
            if target_hit:
                return _make_shadow_observation(
                    setup,
                    bars,
                    status="completed",
                    outcome_class="target_hit",
                    reason="target_hit",
                    exit_bar=bar,
                    exit_price=setup.target_price,
                )

        if elapsed >= horizon:
            return _make_shadow_observation(
                setup,
                bars,
                status="completed",
                outcome_class="horizon_close",
                reason="horizon_observed",
                exit_bar=bar,
                exit_price=bar.close,
            )

    reason = "no_post_signal_bars" if not bars else "missing_future_bars"
    return _make_shadow_observation(
        setup,
        bars,
        status="maturing",
        outcome_class="unavailable",
        reason=reason,
    )


def _first_nested(payload: Mapping[str, Any], nested_key: str, *keys: str) -> Any:
    direct = _pick(payload, *keys, required=False)
    if direct is not None:
        return direct
    nested = payload.get(nested_key)
    if isinstance(nested, Mapping):
        return _pick(nested, *keys, "id", required=False)
    if nested is not None and not isinstance(nested, (list, tuple, Mapping)):
        return nested
    return None


def _verified_flag(value: Any) -> bool:
    return value is True


def validate_actual_evidence(lineage: Mapping[str, Any] | None) -> ActualEvidenceValidation:
    """Validate, but never create, fill/performance/lifecycle lineage."""

    if not isinstance(lineage, Mapping):
        return ActualEvidenceValidation(False, ("lineage",), "actual lineage must be a mapping", None)
    fill = lineage.get("fill") if isinstance(lineage.get("fill"), Mapping) else {}
    performance = lineage.get("performance_link") if isinstance(lineage.get("performance_link"), Mapping) else {}
    lifecycle = lineage.get("lifecycle") if isinstance(lineage.get("lifecycle"), Mapping) else {}

    fill_id = _pick(lineage, "fill_id", required=False) or _pick(fill, "id", "fill_id", required=False)
    fill_verified = _pick(lineage, "fill_verified", required=False)
    if fill_verified is None:
        fill_verified = _pick(fill, "verified", required=False)
    if fill_verified is None and str(_pick(lineage, "fill_verification_status", required=False) or "").lower() == "verified":
        fill_verified = True

    performance_id = _pick(lineage, "performance_link_id", "performance_id", required=False) or _pick(
        performance, "id", "performance_link_id", "performance_id", required=False
    )
    performance_verified = _pick(lineage, "performance_link_verified", required=False)
    if performance_verified is None:
        performance_verified = _pick(performance, "verified", "linked", required=False)
    if performance_verified is None and str(_pick(lineage, "performance_link_status", required=False) or "").lower() == "verified":
        performance_verified = True

    lifecycle_id = _pick(lineage, "position_lifecycle_id", "closed_lifecycle_id", required=False) or _pick(
        lifecycle, "id", "position_lifecycle_id", required=False
    )
    lifecycle_state = _pick(lineage, "lifecycle_state", "position_lifecycle_state", required=False) or _pick(
        lifecycle, "state", "status", required=False
    )
    if lifecycle_state is None and (
        _pick(lineage, "lifecycle_closed", required=False) is True
        or _pick(lifecycle, "closed", required=False) is True
    ):
        lifecycle_state = "closed"

    missing: list[str] = []
    if not fill_id or not _verified_flag(fill_verified):
        missing.append("verified_fill")
    if not performance_id or not _verified_flag(performance_verified):
        missing.append("verified_performance_link")
    if not lifecycle_id or str(lifecycle_state or "").strip().lower() != "closed":
        missing.append("closed_position_lifecycle")

    canonical = canonical_json(lineage)
    if missing:
        return ActualEvidenceValidation(False, tuple(missing), ";".join(missing), canonical)
    return ActualEvidenceValidation(True, (), "verified_actual_lineage", canonical)


def validate_actual_lineage(lineage: Mapping[str, Any] | None) -> ActualEvidenceValidation:
    """Alias kept for integrators that call the evidence object lineage."""

    return validate_actual_evidence(lineage)


def _actual_setup(payload: Mapping[str, Any]) -> CryptoSetup:
    return CryptoSetup.from_payload(payload)


def build_actual_observation(
    payload: Mapping[str, Any],
    *,
    lineage: Mapping[str, Any] | None = None,
) -> CryptoOutcomeObservation:
    """Normalize caller-supplied actual values after lineage validation.

    This function does not look up or synthesize any broker fill, performance
    link, or lifecycle.  The payload must contain the observed actual return
    fields, and the caller must pass the verified lineage explicitly.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("actual observation payload must be a mapping")
    selected_lineage = lineage if lineage is not None else _pick(payload, "actual_lineage", "lineage", required=False)
    validation = validate_actual_evidence(selected_lineage)
    if not validation.valid:
        raise ActualEvidenceError(f"actual evidence is not verifiable: {validation.reason}")
    setup_payload = _pick(payload, "setup", "setup_payload", required=False)
    setup = CryptoSetup.from_payload(setup_payload) if isinstance(setup_payload, Mapping) else _actual_setup(payload)
    status = str(_pick(payload, "status", required=False) or "completed").strip().lower()
    if status != "completed":
        raise ValueError("actual observations require a completed closed lifecycle")
    outcome_class = str(_pick(payload, "outcome_class", "result_class", required=False) or "actual_closed").strip().lower()
    exit_price = _positive_decimal(_pick(payload, "exit_price", "actual_exit_price"), "exit_price")
    exit_timestamp = _parse_utc(_pick(payload, "exit_timestamp", "actual_exit_timestamp"), "exit_timestamp")
    holding_hours = _as_decimal(_pick(payload, "holding_hours"), "holding_hours")
    if holding_hours < 0:
        raise ValueError("holding_hours cannot be negative")
    gross_return = _as_decimal(_pick(payload, "gross_return"), "gross_return")
    net_return = _as_decimal(_pick(payload, "net_return"), "net_return")
    gross_pnl = _pick(payload, "gross_pnl", required=False)
    net_pnl = _pick(payload, "net_pnl", required=False)
    risk_amount = _pick(payload, "risk_amount", required=False)
    gross_r = _pick(payload, "gross_r_multiple", required=False)
    net_r = _pick(payload, "net_r_multiple", required=False)
    input_evidence = _pick(payload, "input_evidence", required=False)
    if input_evidence is None:
        actual_payload: dict[str, Any] = {
            "setup": setup.to_payload(),
            "exit_timestamp": canonical_timestamp(exit_timestamp),
            "exit_price": canonical_decimal_text(exit_price, "exit_price"),
            "holding_hours": canonical_decimal_text(holding_hours, "holding_hours"),
            "gross_return": canonical_decimal_text(gross_return, "gross_return"),
            "net_return": canonical_decimal_text(net_return, "net_return"),
            "outcome_class": outcome_class,
        }
        for key, value, field in (
            ("gross_pnl", gross_pnl, "gross_pnl"),
            ("net_pnl", net_pnl, "net_pnl"),
            ("risk_amount", risk_amount, "risk_amount"),
            ("gross_r_multiple", gross_r, "gross_r_multiple"),
            ("net_r_multiple", net_r, "net_r_multiple"),
        ):
            if value is not None:
                actual_payload[key] = canonical_decimal_text(value, field)
        input_evidence = {"actual_payload": actual_payload, "lineage": json.loads(validation.canonical_lineage_json or "{}")}
    if not isinstance(input_evidence, Mapping):
        raise TypeError("input_evidence must be a mapping when supplied")
    evidence_json = canonical_json(input_evidence)
    calculated_fingerprint = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    supplied_fingerprint = _pick(payload, "input_fingerprint", required=False)
    if supplied_fingerprint is not None and str(supplied_fingerprint) != calculated_fingerprint:
        raise ImmutableObservationError("input_fingerprint does not match canonical actual evidence")
    fingerprint = calculated_fingerprint
    return CryptoOutcomeObservation(
        observation_id=str(_pick(payload, "observation_id", required=False) or f"actual:{fingerprint}"),
        input_fingerprint=fingerprint,
        evidence_type="actual",
        status=status,
        outcome_class=outcome_class,
        reason=str(_pick(payload, "reason", required=False) or "actual_closed_verified"),
        setup=setup,
        bars=tuple(),
        exit_timestamp=exit_timestamp,
        exit_price=exit_price,
        holding_hours=holding_hours,
        gross_return=gross_return,
        net_return=net_return,
        gross_pnl=None if gross_pnl is None else _as_decimal(gross_pnl, "gross_pnl"),
        net_pnl=None if net_pnl is None else _as_decimal(net_pnl, "net_pnl"),
        risk_amount=None if risk_amount is None else _as_decimal(risk_amount, "risk_amount"),
        gross_r_multiple=None if gross_r is None else _as_decimal(gross_r, "gross_r_multiple"),
        net_r_multiple=None if net_r is None else _as_decimal(net_r, "net_r_multiple"),
        actual_lineage_json=validation.canonical_lineage_json,
        input_evidence_json=evidence_json,
    )


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    if isinstance(row, sqlite3.Row):
        return row[key]
    tuple_indexes = {"observation_id": 0, "input_fingerprint": 1, "input_evidence_json": 2}
    if key in tuple_indexes and isinstance(row, tuple):
        return row[tuple_indexes[key]]
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _coerce_observation(observation: CryptoOutcomeObservation | Mapping[str, Any]) -> CryptoOutcomeObservation:
    if isinstance(observation, CryptoOutcomeObservation):
        if observation.evidence_type == "actual":
            try:
                lineage = json.loads(observation.actual_lineage_json or "")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ActualEvidenceError("actual lineage is not valid JSON") from exc
            validation = validate_actual_evidence(lineage)
            if not validation.valid:
                raise ActualEvidenceError(f"actual evidence is not verifiable: {validation.reason}")
        return observation
    if not isinstance(observation, Mapping):
        raise TypeError("observation must be CryptoOutcomeObservation or a mapping")
    evidence_type = str(_pick(observation, "evidence_type", "source", required=False) or "shadow").lower()
    if evidence_type == "actual":
        return build_actual_observation(observation)
    if evidence_type != "shadow":
        raise ValueError("evidence_type must be shadow or actual")
    setup_payload = _pick(observation, "setup", "setup_payload", required=False)
    if isinstance(setup_payload, CryptoSetup):
        setup = setup_payload
    elif isinstance(setup_payload, Mapping):
        setup = CryptoSetup.from_payload(setup_payload)
    else:
        setup = CryptoSetup.from_payload(observation)
    bars_payload = _pick(observation, "bars", "hourly_bars", required=False) or []
    bars = _normalize_bars(setup.research_timestamp, bars_payload)
    status = str(_pick(observation, "status", required=False) or "maturing").lower()
    if status == "maturing":
        return _make_shadow_observation(setup, bars, status="maturing", outcome_class="unavailable", reason=str(_pick(observation, "reason", required=False) or "missing_future_bars"))
    exit_price = _positive_decimal(_pick(observation, "exit_price"), "exit_price")
    exit_timestamp = _parse_utc(_pick(observation, "exit_timestamp"), "exit_timestamp")
    exit_bar = CryptoHourlyBar(timestamp=exit_timestamp, high=exit_price, low=exit_price, close=exit_price)
    return _make_shadow_observation(
        setup,
        bars,
        status="completed",
        outcome_class=str(_pick(observation, "outcome_class", required=False) or "horizon_close"),
        reason=str(_pick(observation, "reason", required=False) or "recalculated"),
        exit_bar=exit_bar,
        exit_price=exit_price,
    )


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME}(
  observation_id TEXT PRIMARY KEY,
  input_fingerprint TEXT NOT NULL UNIQUE,
  evidence_type TEXT NOT NULL CHECK(evidence_type IN ('shadow','actual')),
  status TEXT NOT NULL CHECK(status IN ('completed','maturing')),
  outcome_class TEXT NOT NULL,
  reason TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('long','short')),
  strategy_decision_id TEXT NOT NULL,
  strategy_decision_fingerprint TEXT NOT NULL,
  research_timestamp TEXT NOT NULL,
  horizon_hours INTEGER NOT NULL CHECK(horizon_hours > 0),
  entry_price TEXT NOT NULL,
  stop_price TEXT NOT NULL,
  target_price TEXT NOT NULL,
  quantity TEXT,
  exit_timestamp TEXT,
  exit_price TEXT,
  holding_hours TEXT,
  gross_return TEXT,
  net_return TEXT,
  gross_pnl TEXT,
  net_pnl TEXT,
  risk_amount TEXT,
  gross_r_multiple TEXT,
  net_r_multiple TEXT,
  cost_model_version TEXT NOT NULL,
  fee_bps TEXT NOT NULL,
  spread_bps TEXT NOT NULL,
  slippage_bps TEXT NOT NULL,
  bar_count INTEGER NOT NULL CHECK(bar_count >= 0),
  bars_json TEXT NOT NULL,
  setup_json TEXT NOT NULL,
  cost_model_json TEXT NOT NULL,
  input_evidence_json TEXT NOT NULL,
  actual_lineage_json TEXT,
  created_at TEXT NOT NULL,
  CHECK((evidence_type='shadow' AND actual_lineage_json IS NULL) OR
        (evidence_type='actual' AND actual_lineage_json IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_crypto_profitability_observations_decision
  ON {TABLE_NAME}(strategy_decision_id, research_timestamp);
CREATE INDEX IF NOT EXISTS idx_crypto_profitability_observations_status
  ON {TABLE_NAME}(status, evidence_type);
CREATE TRIGGER IF NOT EXISTS trg_crypto_profitability_observations_append_only
  BEFORE UPDATE ON {TABLE_NAME}
BEGIN
  SELECT RAISE(ABORT, 'crypto profitability observations are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_crypto_profitability_observations_append_only_delete
  BEFORE DELETE ON {TABLE_NAME}
BEGIN
  SELECT RAISE(ABORT, 'crypto profitability observations are append-only');
END;
"""


def _bootstrap_connection(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA_SQL)
    required = {
        row[1] for row in connection.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    }
    expected = {
        "observation_id",
        "input_fingerprint",
        "evidence_type",
        "status",
        "strategy_decision_id",
        "research_timestamp",
        "entry_price",
        "stop_price",
        "target_price",
        "input_evidence_json",
    }
    missing = expected - required
    if missing:
        raise RuntimeError(f"{TABLE_NAME} schema is incompatible; missing columns: {sorted(missing)}")


@contextmanager
def _connection_scope(target: Any) -> Iterator[sqlite3.Connection]:
    if isinstance(target, CryptoOutcomeStore):
        yield target.connection
        return
    if isinstance(target, sqlite3.Connection):
        yield target
        return
    if hasattr(target, "connect") and not isinstance(target, (str, bytes, os.PathLike)):
        with target.connect() as connection:
            yield connection
        return
    path = os.fspath(target)
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def bootstrap_schema(connection_or_path: Any) -> None:
    """Create the isolated crypto outcome table and append-only protections."""

    with _connection_scope(connection_or_path) as connection:
        _bootstrap_connection(connection)
        connection.commit()


def apply_crypto_outcomes_schema(
    connection: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    """Install the sidecar in an existing Plutus migration transaction."""

    _bootstrap_connection(connection)
    if record_migration:
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                SCHEMA_VERSION,
                canonical_timestamp(datetime.now(UTC)),
                "append-only Decimal crypto shadow and verified actual outcome evidence",
            ),
        )


def _persist_on_connection(connection: sqlite3.Connection, observation: CryptoOutcomeObservation) -> PersistResult:
    record = observation.to_record()
    columns = (
        "observation_id", "input_fingerprint", "evidence_type", "status", "outcome_class", "reason",
        "symbol", "side", "strategy_decision_id", "strategy_decision_fingerprint", "research_timestamp",
        "horizon_hours", "entry_price", "stop_price", "target_price", "quantity", "exit_timestamp",
        "exit_price", "holding_hours", "gross_return", "net_return", "gross_pnl", "net_pnl", "risk_amount",
        "gross_r_multiple", "net_r_multiple", "cost_model_version", "fee_bps", "spread_bps", "slippage_bps",
        "bar_count", "bars_json", "setup_json", "cost_model_json", "input_evidence_json", "actual_lineage_json",
        "created_at",
    )
    expected_fingerprint = hashlib.sha256(observation.input_evidence_json.encode("utf-8")).hexdigest()
    if observation.input_fingerprint != expected_fingerprint:
        raise ImmutableObservationError("input_fingerprint does not match canonical input evidence")
    placeholders = ",".join("?" for _ in columns)
    values = [record[column] for column in columns[:-1]] + [canonical_timestamp(datetime.now(UTC))]
    try:
        cursor = connection.execute(
            f"INSERT INTO {TABLE_NAME}({','.join(columns)}) VALUES({placeholders}) "
            "ON CONFLICT(input_fingerprint) DO NOTHING",
            tuple(values),
        )
    except sqlite3.IntegrityError as exc:
        existing = connection.execute(
            f"SELECT observation_id,input_fingerprint,input_evidence_json FROM {TABLE_NAME} WHERE observation_id=?",
            (observation.observation_id,),
        ).fetchone()
        if existing is not None and _row_get(existing, "input_fingerprint") != observation.input_fingerprint:
            raise ImmutableObservationError("observation_id already exists with a different input_fingerprint") from exc
        raise
    inserted = cursor.rowcount == 1
    existing_cursor = connection.execute(
        f"SELECT {','.join(columns[:-1])} FROM {TABLE_NAME} WHERE input_fingerprint=?",
        (observation.input_fingerprint,),
    )
    existing = existing_cursor.fetchone()
    if existing is None:
        raise RuntimeError("idempotent observation insert did not produce a row")
    if isinstance(existing, Mapping):
        existing_values = dict(existing)
    elif isinstance(existing, sqlite3.Row):
        existing_values = {key: existing[key] for key in existing.keys()}
    else:
        existing_values = {
            description[0]: value
            for description, value in zip(existing_cursor.description or (), existing)
        }
    for column in columns[:-1]:
        if existing_values.get(column) != record[column]:
            raise ImmutableObservationError("input_fingerprint evidence mismatch")
    connection.commit()
    return PersistResult(observation.observation_id, observation.input_fingerprint, inserted)


def persist_observation(
    connection_or_store: Any,
    observation: CryptoOutcomeObservation | Mapping[str, Any],
) -> PersistResult:
    """Append one observation, validating actual lineage and deduplicating exact evidence."""

    normalized = _coerce_observation(observation)
    with _connection_scope(connection_or_store) as connection:
        _bootstrap_connection(connection)
        return _persist_on_connection(connection, normalized)


def _row_to_metric_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def _sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(40, len(value.as_tuple().digits) + 20)
        return value.sqrt()


def _verified_snapshot_correlation(snapshot: VerifiedCorrelationSnapshot | Mapping[str, Any] | None) -> Decimal | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, VerifiedCorrelationSnapshot):
        return snapshot.correlation
    if not isinstance(snapshot, Mapping):
        raise TypeError("verified_snapshot must be a mapping or VerifiedCorrelationSnapshot")
    if snapshot.get("verified") is not True and str(snapshot.get("verification_status") or "").lower() != "verified":
        return None
    value = _pick(snapshot, "correlation", required=False)
    if value is None:
        return None
    correlation = _as_decimal(value, "correlation")
    if correlation < Decimal("-1") or correlation > Decimal("1"):
        raise ValueError("verified correlation must be between -1 and 1")
    return correlation


def derive_aggregate_metrics(
    observations: Iterable[CryptoOutcomeObservation | Mapping[str, Any]],
    *,
    severe_loss_threshold: Any,
    prior: BetaPrior | Mapping[str, Any] | None = None,
    verified_snapshot: VerifiedCorrelationSnapshot | Mapping[str, Any] | None = None,
    evidence_type: str | None = None,
) -> dict[str, Any]:
    """Derive exact aggregate metrics without filling missing observations.

    ``severe_loss_threshold`` is required and is compared inclusively to
    ``net_return``.  Only completed observations with a non-null net return
    enter the denominator.  ``prior`` and ``verified_snapshot`` are both
    opt-in; without them posterior and correlation remain ``None``.
    """

    threshold = _as_decimal(severe_loss_threshold, "severe_loss_threshold")
    selected_type = None if evidence_type is None else str(evidence_type).strip().lower()
    rows: list[Mapping[str, Any] | CryptoOutcomeObservation] = []
    for observation in observations:
        if isinstance(observation, CryptoOutcomeObservation):
            if selected_type is None or observation.evidence_type == selected_type:
                rows.append(observation)
        else:
            row = _row_to_metric_mapping(observation)
            row_type = str(row.get("evidence_type") or "").lower()
            if selected_type is None or row_type == selected_type:
                rows.append(row)

    completed: list[tuple[Decimal, Decimal | None]] = []
    unavailable_count = 0
    holding_values: list[Decimal] = []
    gross_values: list[Decimal] = []
    net_pnl_values: list[Decimal] = []
    for observation in rows:
        status = observation.status if isinstance(observation, CryptoOutcomeObservation) else str(observation.get("status") or "")
        net_value = observation.net_return if isinstance(observation, CryptoOutcomeObservation) else observation.get("net_return")
        if status != "completed" or net_value is None:
            unavailable_count += 1
            continue
        net_return = _as_decimal(net_value, "net_return")
        holding_value = observation.holding_hours if isinstance(observation, CryptoOutcomeObservation) else observation.get("holding_hours")
        gross_value = observation.gross_return if isinstance(observation, CryptoOutcomeObservation) else observation.get("gross_return")
        pnl_value = observation.net_pnl if isinstance(observation, CryptoOutcomeObservation) else observation.get("net_pnl")
        if holding_value is not None:
            holding_values.append(_as_decimal(holding_value, "holding_hours"))
        if gross_value is not None:
            gross_values.append(_as_decimal(gross_value, "gross_return"))
        if pnl_value is not None:
            net_pnl_values.append(_as_decimal(pnl_value, "net_pnl"))
        completed.append((net_return, holding_values[-1] if holding_value is not None else None))

    sample_count = len(completed)
    wins = sum(1 for net_return, _ in completed if net_return > 0)
    losses = sample_count - wins
    win_probability = None if sample_count == 0 else Decimal(wins) / Decimal(sample_count)
    severe_losses = sum(1 for net_return, _ in completed if net_return <= threshold)
    severe_loss_rate = None if sample_count == 0 else Decimal(severe_losses) / Decimal(sample_count)

    explicit_prior = None if prior is None else BetaPrior.from_value(prior)
    posterior = None
    posterior_uncertainty = None
    if explicit_prior is not None:
        posterior_alpha = explicit_prior.alpha + Decimal(wins)
        posterior_beta = explicit_prior.beta + Decimal(losses)
        posterior_total = posterior_alpha + posterior_beta
        posterior = posterior_alpha / posterior_total
        posterior_variance = (posterior_alpha * posterior_beta) / (
            (posterior_total * posterior_total) * (posterior_total + Decimal(1))
        )
        posterior_uncertainty = _sqrt(posterior_variance)

    observed_uncertainty = None
    if sample_count:
        p = win_probability
        observed_uncertainty = _sqrt((p * (Decimal(1) - p)) / Decimal(sample_count))
    uncertainty = posterior_uncertainty if explicit_prior is not None else observed_uncertainty
    correlation = _verified_snapshot_correlation(verified_snapshot)
    return {
        "sample_count": sample_count,
        "unavailable_count": unavailable_count,
        "win_count": wins,
        "loss_count": losses,
        "win_probability": win_probability,
        "win_probability_posterior": posterior,
        "posterior_win_probability": posterior,
        "uncertainty": uncertainty,
        "win_probability_uncertainty": uncertainty,
        "observed_win_probability_uncertainty": observed_uncertainty,
        "posterior_uncertainty": posterior_uncertainty,
        "severe_loss_threshold": threshold,
        "severe_loss_count": severe_losses,
        "severe_loss_rate": severe_loss_rate,
        "mean_gross_return": _mean(gross_values),
        "mean_net_return": _mean([value for value, _ in completed]),
        "total_net_pnl": None if not net_pnl_values else sum(net_pnl_values, Decimal(0)),
        "holding_hours": _mean(holding_values),
        "average_holding_hours": _mean(holding_values),
        "holding_hours_count": len(holding_values),
        "correlation": correlation,
    }


class CryptoOutcomeStore:
    """Small SQLite repository for the isolated crypto sidecar table."""

    def __init__(self, database: str | os.PathLike[str] | sqlite3.Connection) -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        if self._owns_connection:
            self.connection = sqlite3.connect(os.fspath(database))
        else:
            self.connection = database
        self.connection.row_factory = sqlite3.Row
        bootstrap_schema(self.connection)

    def persist(self, observation: CryptoOutcomeObservation | Mapping[str, Any]) -> PersistResult:
        return persist_observation(self.connection, observation)

    def fetch_observations(
        self,
        *,
        evidence_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if evidence_type is not None:
            clauses.append("evidence_type=?")
            params.append(str(evidence_type).lower())
        if status is not None:
            clauses.append("status=?")
            params.append(str(status).lower())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM {TABLE_NAME}{where} ORDER BY research_timestamp,observation_id",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def derive_metrics(
        self,
        *,
        severe_loss_threshold: Any,
        prior: BetaPrior | Mapping[str, Any] | None = None,
        verified_snapshot: VerifiedCorrelationSnapshot | Mapping[str, Any] | None = None,
        evidence_type: str | None = None,
    ) -> dict[str, Any]:
        return derive_aggregate_metrics(
            self.fetch_observations(evidence_type=evidence_type),
            severe_loss_threshold=severe_loss_threshold,
            prior=prior,
            verified_snapshot=verified_snapshot,
            evidence_type=evidence_type,
        )

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "CryptoOutcomeStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


# Short aliases make the intended integration seam easy to discover while the
# longer names remain the documented API.
calculate_shadow = calculate_shadow_outcome
aggregate_profitability_metrics = derive_aggregate_metrics


__all__ = [
    "ActualEvidenceError",
    "ActualEvidenceValidation",
    "BetaPrior",
    "CALCULATION_VERSION",
    "CryptoCostModel",
    "CryptoHourlyBar",
    "CryptoOutcomeError",
    "CryptoOutcomeObservation",
    "CryptoOutcomeStore",
    "CryptoSetup",
    "EVIDENCE_VERSION",
    "ImmutableObservationError",
    "PersistResult",
    "SCHEMA_VERSION",
    "TABLE_NAME",
    "VerifiedCorrelationSnapshot",
    "aggregate_profitability_metrics",
    "apply_crypto_outcomes_schema",
    "bootstrap_schema",
    "build_actual_observation",
    "calculate_shadow",
    "calculate_shadow_outcome",
    "canonical_decimal_text",
    "canonical_json",
    "canonical_timestamp",
    "compute_input_fingerprint",
    "derive_aggregate_metrics",
    "persist_observation",
    "validate_actual_evidence",
    "validate_actual_lineage",
]
