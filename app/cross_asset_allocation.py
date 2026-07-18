"""Deterministic, immutable cross-asset portfolio allocation advice.

This boundary compares exact profitability evidence on a common risk, capital,
time, uncertainty, and portfolio-contribution basis.  It is deliberately not
order authority: plans cannot create proposals, approvals, intents,
reservations, or broker requests.  The distinction lets equity and crypto
research share one portfolio model while crypto remains research-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from .configuration import effective_config_hash
from .formula_versions import (
    CROSS_ASSET_ALLOCATION_FORMULA_VERSION,
    CROSS_ASSET_ALLOCATION_SCHEMA_VERSION,
)
from .utils import iso_now


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ASSET_CLASSES = frozenset({"equity", "etf", "crypto"})
EXECUTION_LANES = frozenset({"operational_paper", "research_only"})
ACTIONS = frozenset({"entry", "add", "rotation_entry"})
SOURCE_TYPES = frozenset(
    {"candidate_profitability_decision", "crypto_profitability_research"}
)


class CrossAssetAllocationError(ValueError):
    """Raised when allocation evidence is incomplete or inconsistent."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Decimal) -> str:
    return format((ZERO if value == ZERO else value).normalize(), "f")


def _required_text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise CrossAssetAllocationError(f"{name} is required")
    return result


def _hash(value: Any, name: str) -> str:
    result = _required_text(value, name).lower()
    if not SHA256.fullmatch(result):
        raise CrossAssetAllocationError(f"{name} must be a SHA-256 digest")
    return result


def _decimal(
    value: Any,
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise CrossAssetAllocationError(
            f"{name} must use Decimal, an integer, or a decimal string"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CrossAssetAllocationError(f"{name} must be a valid decimal") from exc
    if not result.is_finite():
        raise CrossAssetAllocationError(f"{name} must be finite")
    if positive and result <= ZERO:
        raise CrossAssetAllocationError(f"{name} must be positive")
    if minimum is not None and result < minimum:
        raise CrossAssetAllocationError(f"{name} must be at least {_text(minimum)}")
    if maximum is not None and result > maximum:
        raise CrossAssetAllocationError(f"{name} must be at most {_text(maximum)}")
    return ZERO if result == ZERO else result


def _trusted_decimal(
    value: Any,
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise CrossAssetAllocationError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CrossAssetAllocationError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise CrossAssetAllocationError(f"{name} must be finite")
    if positive and result <= ZERO:
        raise CrossAssetAllocationError(f"{name} must be positive")
    if minimum is not None and result < minimum:
        raise CrossAssetAllocationError(f"{name} must be at least {_text(minimum)}")
    if maximum is not None and result > maximum:
        raise CrossAssetAllocationError(f"{name} must be at most {_text(maximum)}")
    return ZERO if result == ZERO else result


def _timestamp(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_required_text(value, name).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrossAssetAllocationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CrossAssetAllocationError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise CrossAssetAllocationError(f"{name} must be a boolean")
    return value


def _decimal_map(
    value: Any,
    name: str,
    *,
    normalize_key: Callable[[str], str] | None = None,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise CrossAssetAllocationError(f"{name} must be a mapping")
    result: dict[str, str] = {}
    for key, item in sorted(value.items(), key=lambda item: str(item[0])):
        canonical_key = _required_text(key, f"{name} key")
        if normalize_key is not None:
            canonical_key = normalize_key(canonical_key)
        if canonical_key in result:
            raise CrossAssetAllocationError(
                f"{name} contains duplicate canonical key {canonical_key}"
            )
        result[canonical_key] = _text(
            _decimal(item, f"{name}.{key}", minimum=ZERO)
        )
    return result


def _clamp(value: Decimal, lower: Decimal = ZERO, upper: Decimal = ONE) -> Decimal:
    return max(lower, min(upper, value))


def _ratio(value: Decimal, target: Decimal) -> Decimal:
    if target <= ZERO:
        raise CrossAssetAllocationError("allocation score target must be positive")
    return _clamp(value / target)


@dataclass(frozen=True)
class CrossAssetCandidate:
    candidate_id: str
    source_type: str
    source_id: str
    source_fingerprint: str
    source_authoritative: bool
    run_id: str
    asset_class: str
    symbol: str
    cluster: str
    strategy_version: str
    strategy_state: str
    action: str
    execution_lane: str
    evidence_as_of: str
    proposed_notional: Any
    economic_risk_dollars: Any
    stop_risk_dollars: Any
    expected_net_profit: Any
    expected_net_r: Any
    conservative_expected_net_r: Any
    expected_capital_efficiency: Any
    expected_r_per_day: Any
    marginal_portfolio_contribution_r: Any
    probability_positive_return: Any
    probability_severe_loss: Any
    uncertainty: Any
    cost_to_gross_edge_ratio: Any
    expected_holding_days: Any
    annualized_volatility: Any
    liquidity_notional: Any
    correlation_to_portfolio: Any
    marginal_drawdown_r: Any
    current_position: bool
    conflict_free: bool
    profitability_eligible: bool
    config_hash: str
    formula_versions: Mapping[str, str]

    def canonical(
        self,
        *,
        current_config_hash: str,
        required_formula_versions: Mapping[str, str],
        evaluation_time: datetime,
        maximum_age_seconds: Decimal,
    ) -> dict[str, Any]:
        asset_class = _required_text(self.asset_class, "asset_class").lower()
        if asset_class not in ASSET_CLASSES:
            raise CrossAssetAllocationError("asset_class is unsupported")
        lane = _required_text(self.execution_lane, "execution_lane").lower()
        if lane not in EXECUTION_LANES:
            raise CrossAssetAllocationError("execution_lane is unsupported")
        action = _required_text(self.action, "action").lower()
        if action not in ACTIONS:
            raise CrossAssetAllocationError("action is unsupported")
        if asset_class == "crypto" and lane != "research_only":
            raise CrossAssetAllocationError(
                "crypto must remain research_only at the current capability stage"
            )
        evidence_time = _timestamp(self.evidence_as_of, "evidence_as_of")
        age = Decimal(str((evaluation_time - evidence_time).total_seconds()))
        if age < Decimal("-5"):
            raise CrossAssetAllocationError("candidate evidence is from the future")
        if age > maximum_age_seconds:
            raise CrossAssetAllocationError("candidate evidence is stale")
        config_hash = _hash(self.config_hash, "config_hash")
        if config_hash != current_config_hash:
            raise CrossAssetAllocationError("candidate configuration identity is not current")
        if not isinstance(self.formula_versions, Mapping):
            raise CrossAssetAllocationError("formula_versions must be a mapping")
        formulas = {
            str(key): _required_text(value, f"formula_versions.{key}")
            for key, value in sorted(self.formula_versions.items())
        }
        for key, expected in required_formula_versions.items():
            if formulas.get(key) != expected:
                raise CrossAssetAllocationError(
                    f"formula_versions.{key} must be {expected}"
                )
        source_type = _required_text(self.source_type, "source_type")
        if source_type not in SOURCE_TYPES:
            raise CrossAssetAllocationError("source_type is unsupported")
        canonical = {
            "candidate_id": _required_text(self.candidate_id, "candidate_id"),
            "source_type": source_type,
            "source_id": _required_text(self.source_id, "source_id"),
            "source_fingerprint": _hash(
                self.source_fingerprint, "source_fingerprint"
            ),
            "source_authoritative": _bool(
                self.source_authoritative, "source_authoritative"
            ),
            "run_id": _required_text(self.run_id, "run_id"),
            "asset_class": asset_class,
            "symbol": _required_text(self.symbol, "symbol").upper(),
            "cluster": _required_text(self.cluster, "cluster").lower(),
            "strategy_version": _required_text(
                self.strategy_version, "strategy_version"
            ),
            "strategy_state": _required_text(
                self.strategy_state, "strategy_state"
            ).upper(),
            "action": action,
            "execution_lane": lane,
            "evidence_as_of": evidence_time.isoformat(),
            "evidence_age_seconds": _text(max(ZERO, age)),
            "proposed_notional": _text(
                _decimal(self.proposed_notional, "proposed_notional", positive=True)
            ),
            "economic_risk_dollars": _text(
                _decimal(
                    self.economic_risk_dollars,
                    "economic_risk_dollars",
                    positive=True,
                )
            ),
            "stop_risk_dollars": _text(
                _decimal(self.stop_risk_dollars, "stop_risk_dollars", positive=True)
            ),
            "expected_net_profit": _text(
                _decimal(self.expected_net_profit, "expected_net_profit")
            ),
            "expected_net_r": _text(_decimal(self.expected_net_r, "expected_net_r")),
            "conservative_expected_net_r": _text(
                _decimal(
                    self.conservative_expected_net_r,
                    "conservative_expected_net_r",
                )
            ),
            "expected_capital_efficiency": _text(
                _decimal(
                    self.expected_capital_efficiency,
                    "expected_capital_efficiency",
                )
            ),
            "expected_r_per_day": _text(
                _decimal(self.expected_r_per_day, "expected_r_per_day")
            ),
            "marginal_portfolio_contribution_r": _text(
                _decimal(
                    self.marginal_portfolio_contribution_r,
                    "marginal_portfolio_contribution_r",
                )
            ),
            "probability_positive_return": _text(
                _decimal(
                    self.probability_positive_return,
                    "probability_positive_return",
                    minimum=ZERO,
                    maximum=ONE,
                )
            ),
            "probability_severe_loss": _text(
                _decimal(
                    self.probability_severe_loss,
                    "probability_severe_loss",
                    minimum=ZERO,
                    maximum=ONE,
                )
            ),
            "uncertainty": _text(
                _decimal(self.uncertainty, "uncertainty", minimum=ZERO, maximum=ONE)
            ),
            "cost_to_gross_edge_ratio": _text(
                _decimal(
                    self.cost_to_gross_edge_ratio,
                    "cost_to_gross_edge_ratio",
                    minimum=ZERO,
                )
            ),
            "expected_holding_days": _text(
                _decimal(
                    self.expected_holding_days,
                    "expected_holding_days",
                    positive=True,
                )
            ),
            "annualized_volatility": _text(
                _decimal(
                    self.annualized_volatility,
                    "annualized_volatility",
                    minimum=ZERO,
                    maximum=Decimal("10"),
                )
            ),
            "liquidity_notional": _text(
                _decimal(self.liquidity_notional, "liquidity_notional", positive=True)
            ),
            "correlation_to_portfolio": _text(
                _decimal(
                    self.correlation_to_portfolio,
                    "correlation_to_portfolio",
                    minimum=Decimal("-1"),
                    maximum=ONE,
                )
            ),
            "marginal_drawdown_r": _text(
                _decimal(self.marginal_drawdown_r, "marginal_drawdown_r", minimum=ZERO)
            ),
            "current_position": _bool(self.current_position, "current_position"),
            "conflict_free": _bool(self.conflict_free, "conflict_free"),
            "profitability_eligible": _bool(
                self.profitability_eligible, "profitability_eligible"
            ),
            "config_hash": config_hash,
            "formula_versions": formulas,
        }
        notional = Decimal(canonical["proposed_notional"])
        economic_risk = Decimal(canonical["economic_risk_dollars"])
        stop_risk = Decimal(canonical["stop_risk_dollars"])
        expected_profit = Decimal(canonical["expected_net_profit"])
        expected_r = Decimal(canonical["expected_net_r"])
        capital_efficiency = Decimal(canonical["expected_capital_efficiency"])
        holding_days = Decimal(canonical["expected_holding_days"])
        r_per_day = Decimal(canonical["expected_r_per_day"])
        tolerance = Decimal("0.000000000001")
        if stop_risk < economic_risk:
            raise CrossAssetAllocationError(
                "cost-inclusive stop risk cannot be below modeled economic downside"
            )
        if abs(expected_profit / economic_risk - expected_r) > tolerance:
            raise CrossAssetAllocationError(
                "expected_net_r is inconsistent with expected profit and stop risk"
            )
        if abs(expected_profit / notional - capital_efficiency) > tolerance:
            raise CrossAssetAllocationError(
                "expected_capital_efficiency is inconsistent with expected profit and capital"
            )
        if abs(expected_r / holding_days - r_per_day) > tolerance:
            raise CrossAssetAllocationError(
                "expected_r_per_day is inconsistent with expected net R and holding period"
            )
        if Decimal(canonical["conservative_expected_net_r"]) > expected_r:
            raise CrossAssetAllocationError(
                "conservative expected net R cannot exceed expected net R"
            )
        if Decimal(canonical["marginal_portfolio_contribution_r"]) > Decimal(
            canonical["conservative_expected_net_r"]
        ):
            raise CrossAssetAllocationError(
                "marginal portfolio contribution cannot exceed conservative net R"
            )
        if Decimal(canonical["probability_severe_loss"]) > ONE - Decimal(
            canonical["probability_positive_return"]
        ):
            raise CrossAssetAllocationError(
                "severe-loss probability cannot exceed total nonpositive probability"
            )
        return canonical

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CrossAssetCandidate":
        fields = cls.__dataclass_fields__
        return cls(**{name: value[name] for name in fields})


@dataclass(frozen=True)
class CrossAssetPortfolioSnapshot:
    snapshot_id: str
    snapshot_fingerprint: str
    authoritative: bool
    paper_account_id_hash: str
    as_of: str
    equity: Any
    cash: Any
    buying_power: Any
    gross_exposure: Any
    stop_heat: Any
    daily_loss_pct: Any
    weekly_loss_pct: Any
    drawdown_pct: Any
    portfolio_annualized_volatility: Any
    position_count: int
    asset_class_position_count: Mapping[str, int]
    symbol_exposure: Mapping[str, Any]
    cluster_exposure: Mapping[str, Any]
    asset_class_exposure: Mapping[str, Any]
    asset_class_stop_heat: Mapping[str, Any]
    strategy_stop_heat: Mapping[str, Any]
    kill_switch_active: bool
    loss_evidence_fresh: bool
    database_healthy: bool
    internet_healthy: bool
    power_healthy: bool
    broker_healthy: bool
    config_hash: str

    def canonical(
        self,
        *,
        current_config_hash: str,
        evaluation_time: datetime,
        maximum_age_seconds: Decimal,
    ) -> dict[str, Any]:
        captured = _timestamp(self.as_of, "portfolio.as_of")
        age = Decimal(str((evaluation_time - captured).total_seconds()))
        if age < Decimal("-5"):
            raise CrossAssetAllocationError("portfolio snapshot is from the future")
        if age > maximum_age_seconds:
            raise CrossAssetAllocationError("portfolio snapshot is stale")
        config_hash = _hash(self.config_hash, "portfolio.config_hash")
        if config_hash != current_config_hash:
            raise CrossAssetAllocationError("portfolio configuration identity is not current")
        if isinstance(self.position_count, bool) or not isinstance(self.position_count, int):
            raise CrossAssetAllocationError("portfolio.position_count must be an integer")
        if self.position_count < 0:
            raise CrossAssetAllocationError("portfolio.position_count cannot be negative")
        if not isinstance(self.asset_class_position_count, Mapping):
            raise CrossAssetAllocationError(
                "portfolio.asset_class_position_count must be a mapping"
            )
        asset_counts: dict[str, int] = {}
        for key, value in sorted(self.asset_class_position_count.items()):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CrossAssetAllocationError(
                    f"portfolio.asset_class_position_count.{key} must be a nonnegative integer"
                )
            asset_counts[_required_text(key, "asset class position-count key")] = value
        symbol_exposure = _decimal_map(
            self.symbol_exposure,
            "portfolio.symbol_exposure",
            normalize_key=str.upper,
        )
        cluster_exposure = _decimal_map(
            self.cluster_exposure,
            "portfolio.cluster_exposure",
            normalize_key=str.lower,
        )
        asset_exposure = _decimal_map(
            self.asset_class_exposure, "portfolio.asset_class_exposure"
        )
        asset_heat = _decimal_map(
            self.asset_class_stop_heat, "portfolio.asset_class_stop_heat"
        )
        strategy_heat = _decimal_map(
            self.strategy_stop_heat, "portfolio.strategy_stop_heat"
        )
        required_assets = {"equity", "crypto"}
        if set(asset_counts) != required_assets:
            raise CrossAssetAllocationError(
                "portfolio asset-class position counts must contain exactly equity and crypto"
            )
        if set(asset_exposure) != required_assets or set(asset_heat) != required_assets:
            raise CrossAssetAllocationError(
                "portfolio asset-class evidence must contain exactly equity and crypto"
            )
        gross = _decimal(self.gross_exposure, "portfolio.gross_exposure", minimum=ZERO)
        stop_heat = _decimal(self.stop_heat, "portfolio.stop_heat", minimum=ZERO)
        if sum(asset_counts.values()) != self.position_count:
            raise CrossAssetAllocationError(
                "portfolio position counts do not reconcile"
            )
        if sum(value != "0" for value in symbol_exposure.values()) != self.position_count:
            raise CrossAssetAllocationError(
                "portfolio held-symbol count does not reconcile"
            )
        for values, expected, label in (
            (symbol_exposure, gross, "symbol exposure"),
            (cluster_exposure, gross, "cluster exposure"),
            (asset_exposure, gross, "asset-class exposure"),
            (asset_heat, stop_heat, "asset-class stop heat"),
            (strategy_heat, stop_heat, "strategy stop heat"),
        ):
            if sum((Decimal(value) for value in values.values()), ZERO) != expected:
                raise CrossAssetAllocationError(
                    f"portfolio {label} does not reconcile"
                )
        equity = _decimal(self.equity, "portfolio.equity", positive=True)
        cash = _decimal(self.cash, "portfolio.cash", minimum=ZERO)
        if cash > equity:
            raise CrossAssetAllocationError(
                "portfolio cash cannot exceed equity in the cash-only long portfolio"
            )
        return {
            "snapshot_id": _required_text(self.snapshot_id, "portfolio.snapshot_id"),
            "snapshot_fingerprint": _hash(
                self.snapshot_fingerprint, "portfolio.snapshot_fingerprint"
            ),
            "authoritative": _bool(self.authoritative, "portfolio.authoritative"),
            "paper_account_id_hash": _hash(
                self.paper_account_id_hash, "portfolio.paper_account_id_hash"
            ),
            "as_of": captured.isoformat(),
            "age_seconds": _text(max(ZERO, age)),
            "equity": _text(equity),
            "cash": _text(cash),
            "buying_power": _text(
                _decimal(self.buying_power, "portfolio.buying_power", minimum=ZERO)
            ),
            "gross_exposure": _text(gross),
            "stop_heat": _text(stop_heat),
            "daily_loss_pct": _text(
                _decimal(self.daily_loss_pct, "portfolio.daily_loss_pct", minimum=ZERO)
            ),
            "weekly_loss_pct": _text(
                _decimal(self.weekly_loss_pct, "portfolio.weekly_loss_pct", minimum=ZERO)
            ),
            "drawdown_pct": _text(
                _decimal(self.drawdown_pct, "portfolio.drawdown_pct", minimum=ZERO)
            ),
            "portfolio_annualized_volatility": _text(
                _decimal(
                    self.portfolio_annualized_volatility,
                    "portfolio.portfolio_annualized_volatility",
                    minimum=ZERO,
                    maximum=Decimal("10"),
                )
            ),
            "position_count": self.position_count,
            "asset_class_position_count": asset_counts,
            "symbol_exposure": symbol_exposure,
            "cluster_exposure": cluster_exposure,
            "asset_class_exposure": asset_exposure,
            "asset_class_stop_heat": asset_heat,
            "strategy_stop_heat": strategy_heat,
            "kill_switch_active": _bool(
                self.kill_switch_active, "portfolio.kill_switch_active"
            ),
            "loss_evidence_fresh": _bool(
                self.loss_evidence_fresh, "portfolio.loss_evidence_fresh"
            ),
            "database_healthy": _bool(
                self.database_healthy, "portfolio.database_healthy"
            ),
            "internet_healthy": _bool(
                self.internet_healthy, "portfolio.internet_healthy"
            ),
            "power_healthy": _bool(self.power_healthy, "portfolio.power_healthy"),
            "broker_healthy": _bool(
                self.broker_healthy, "portfolio.broker_healthy"
            ),
            "config_hash": config_hash,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CrossAssetPortfolioSnapshot":
        fields = cls.__dataclass_fields__
        return cls(**{name: value[name] for name in fields})


@dataclass(frozen=True)
class CrossAssetAllocationPlan:
    id: str
    run_id: str
    as_of: str
    expires_at: str
    portfolio_snapshot_id: str
    portfolio_snapshot_fingerprint: str
    candidate_set_fingerprint: str
    policy_fingerprint: str
    plan_fingerprint: str
    decisions: tuple[Mapping[str, Any], ...]
    summary: Mapping[str, Any]
    execution_authorized: bool
    config_hash: str
    formula_version: str = CROSS_ASSET_ALLOCATION_FORMULA_VERSION
    schema_version: str = CROSS_ASSET_ALLOCATION_SCHEMA_VERSION


def _policy(config: Mapping[str, Any]) -> dict[str, Any]:
    if (
        config.get("mode") != "paper"
        or config.get("live_enabled") is not False
        or config.get("auto_execution_enabled") is not False
        or config.get("auto_execution_mode") != "manual_only"
    ):
        raise CrossAssetAllocationError(
            "cross-asset allocation requires paper/manual-only capability controls"
        )
    crypto = config.get("crypto") or {}
    if (
        not isinstance(crypto, Mapping)
        or crypto.get("mode") != "research_only"
        or crypto.get("paper_trading_enabled") is not False
    ):
        raise CrossAssetAllocationError(
            "cross-asset allocation requires the crypto research-only capability"
        )
    raw = config.get("cross_asset_allocation") or {}
    if raw.get("enabled") is not True or raw.get("mode") != "research_advisory":
        raise CrossAssetAllocationError("cross-asset allocation policy is not enabled")
    if raw.get("produce_order_authority") is not False:
        raise CrossAssetAllocationError("cross-asset allocation cannot produce order authority")
    if raw.get("formula_version") != CROSS_ASSET_ALLOCATION_FORMULA_VERSION:
        raise CrossAssetAllocationError("cross-asset allocation formula identity mismatch")
    if raw.get("schema_version") != CROSS_ASSET_ALLOCATION_SCHEMA_VERSION:
        raise CrossAssetAllocationError("cross-asset allocation schema identity mismatch")
    # These are code-level ceilings, not caller-configurable validation hints.
    # A self-consistent alternative config hash must never widen the audited
    # paper/research boundary by bypassing validate_config().
    decimal_fields = {
        "candidate_ttl_seconds": (ONE, Decimal("600")),
        "portfolio_snapshot_ttl_seconds": (ONE, Decimal("600")),
        "plan_ttl_seconds": (ONE, Decimal("600")),
        "maximum_total_gross_exposure_pct": (Decimal("0.01"), Decimal("50")),
        "maximum_stop_heat_pct": (Decimal("0.01"), Decimal("1.75")),
        "maximum_symbol_exposure_pct": (Decimal("0.01"), Decimal("6")),
        "maximum_cluster_exposure_pct": (Decimal("0.01"), Decimal("15")),
        "maximum_equity_exposure_pct": (Decimal("0.01"), Decimal("50")),
        "maximum_crypto_exposure_pct": (Decimal("0.01"), Decimal("1")),
        "maximum_strategy_stop_heat_pct": (Decimal("0.01"), Decimal("0.6125")),
        "maximum_exploration_stop_heat_pct": (Decimal("0.01"), Decimal("0.10")),
        "maximum_crypto_stop_heat_pct": (Decimal("0.01"), Decimal("0.05")),
        "maximum_equity_trade_stop_risk_pct": (Decimal("0.01"), Decimal("0.35")),
        "maximum_crypto_trade_stop_risk_pct": (Decimal("0.001"), Decimal("0.01")),
        "maximum_equity_annualized_volatility": (Decimal("0.01"), Decimal("0.45")),
        "maximum_crypto_annualized_volatility": (Decimal("0.01"), Decimal("1.50")),
        "minimum_cash_reserve_pct": (Decimal("20"), HUNDRED),
        "minimum_executable_notional_usd": (ONE, Decimal("250")),
        "minimum_equity_liquidity_usd": (Decimal("10000000"), Decimal("1000000000000")),
        "minimum_crypto_liquidity_usd": (Decimal("1000"), Decimal("1000000000000")),
        "maximum_cost_to_gross_edge_ratio": (Decimal("0.01"), Decimal("0.50")),
        "daily_loss_halt_pct": (Decimal("0.01"), Decimal("0.75")),
        "weekly_loss_halt_pct": (Decimal("0.01"), Decimal("1.50")),
        "drawdown_throttle_start_pct": (Decimal("0.01"), Decimal("6")),
        "drawdown_halt_pct": (Decimal("0.01"), Decimal("6")),
        "drawdown_throttle_multiplier": (Decimal("0.01"), ONE),
        "maximum_marginal_drawdown_r": (Decimal("0.01"), Decimal("6")),
        "target_conservative_net_r": (Decimal("0.000001"), Decimal("10")),
        "target_expected_net_r": (Decimal("0.000001"), Decimal("10")),
        "target_capital_efficiency": (Decimal("0.000001"), Decimal("10")),
        "target_r_per_day": (Decimal("0.000001"), Decimal("10")),
        "target_marginal_contribution_r": (Decimal("0.000001"), Decimal("10")),
    }
    policy: dict[str, Any] = {
        "enabled": True,
        "mode": "research_advisory",
        "produce_order_authority": False,
        "formula_version": CROSS_ASSET_ALLOCATION_FORMULA_VERSION,
        "schema_version": CROSS_ASSET_ALLOCATION_SCHEMA_VERSION,
    }
    for name, (minimum, maximum) in decimal_fields.items():
        policy[name] = _text(
            _trusted_decimal(
                raw.get(name),
                f"cross_asset_allocation.{name}",
                minimum=minimum,
                maximum=maximum,
                positive=name.startswith("target_") or name.endswith("ttl_seconds"),
            )
        )
    if Decimal(policy["drawdown_throttle_start_pct"]) >= Decimal(
        policy["drawdown_halt_pct"]
    ):
        raise CrossAssetAllocationError(
            "cross-asset drawdown throttle must start below the halt"
        )
    try:
        maximum_positions = int(raw.get("maximum_positions"))
    except (TypeError, ValueError) as exc:
        raise CrossAssetAllocationError(
            "cross_asset_allocation.maximum_positions must be an integer"
        ) from exc
    maximum_positions_raw = raw.get("maximum_positions")
    if (
        isinstance(maximum_positions_raw, bool)
        or _trusted_decimal(
            maximum_positions_raw, "cross_asset_allocation.maximum_positions"
        )
        != Decimal(maximum_positions)
        or maximum_positions < 1
        or maximum_positions > 5
    ):
        raise CrossAssetAllocationError(
            "cross_asset_allocation.maximum_positions must be a positive integer"
        )
    policy["maximum_positions"] = maximum_positions
    for name in ("maximum_equity_positions", "maximum_crypto_positions"):
        raw_value = raw.get(name)
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise CrossAssetAllocationError(
                f"cross_asset_allocation.{name} must be an integer"
            ) from exc
        maximum = 3 if name == "maximum_equity_positions" else 2
        if (
            isinstance(raw_value, bool)
            or _trusted_decimal(raw_value, f"cross_asset_allocation.{name}")
            != Decimal(value)
            or value < 1
            or value > maximum
        ):
            raise CrossAssetAllocationError(
                f"cross_asset_allocation.{name} must be a positive integer"
            )
        policy[name] = value
    weights_raw = raw.get("score_weights") or {}
    expected_weights = {
        "uncertainty_adjusted_net_expectancy",
        "expected_net_expectancy",
        "capital_efficiency",
        "holding_period_efficiency",
        "marginal_portfolio_contribution",
        "probability_positive_return",
        "severe_loss_resilience",
        "execution_cost_resilience",
        "liquidity",
        "diversification",
        "volatility_resilience",
    }
    if set(weights_raw) != expected_weights:
        raise CrossAssetAllocationError("cross-asset allocation score weights are incomplete")
    weights = {
        name: _trusted_decimal(
            weights_raw[name],
            f"cross_asset_allocation.score_weights.{name}",
            minimum=ZERO,
            maximum=ONE,
        )
        for name in sorted(expected_weights)
    }
    if sum(weights.values(), ZERO) != ONE:
        raise CrossAssetAllocationError("cross-asset allocation score weights must sum to 1")
    policy["score_weights"] = {name: _text(value) for name, value in weights.items()}
    return policy


def _candidate_score(candidate: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    conservative = Decimal(candidate["conservative_expected_net_r"])
    expected = Decimal(candidate["expected_net_r"])
    capital = Decimal(candidate["expected_capital_efficiency"])
    per_day = Decimal(candidate["expected_r_per_day"])
    marginal = Decimal(candidate["marginal_portfolio_contribution_r"])
    probability_positive = Decimal(candidate["probability_positive_return"])
    severe_loss = Decimal(candidate["probability_severe_loss"])
    cost_ratio = Decimal(candidate["cost_to_gross_edge_ratio"])
    liquidity = Decimal(candidate["liquidity_notional"])
    correlation = abs(Decimal(candidate["correlation_to_portfolio"]))
    volatility = Decimal(candidate["annualized_volatility"])
    uncertainty = Decimal(candidate["uncertainty"])
    marginal_drawdown = Decimal(candidate["marginal_drawdown_r"])
    liquidity_target = Decimal(
        policy[
            "minimum_crypto_liquidity_usd"
            if candidate["asset_class"] == "crypto"
            else "minimum_equity_liquidity_usd"
        ]
    )
    components = {
        "uncertainty_adjusted_net_expectancy": _ratio(
            max(ZERO, conservative), Decimal(policy["target_conservative_net_r"])
        ),
        "expected_net_expectancy": _ratio(
            max(ZERO, expected), Decimal(policy["target_expected_net_r"])
        ),
        "capital_efficiency": _ratio(
            max(ZERO, capital), Decimal(policy["target_capital_efficiency"])
        ),
        "holding_period_efficiency": _ratio(
            max(ZERO, per_day), Decimal(policy["target_r_per_day"])
        ),
        "marginal_portfolio_contribution": _ratio(
            max(ZERO, marginal), Decimal(policy["target_marginal_contribution_r"])
        ),
        "probability_positive_return": probability_positive,
        "severe_loss_resilience": ONE - severe_loss,
        "execution_cost_resilience": _clamp(
            ONE - cost_ratio / Decimal(policy["maximum_cost_to_gross_edge_ratio"])
        ),
        "liquidity": _ratio(liquidity, liquidity_target),
        "diversification": ONE - correlation,
        "volatility_resilience": _clamp(
            ONE
            - volatility
            / Decimal(
                policy[
                    "maximum_crypto_annualized_volatility"
                    if candidate["asset_class"] == "crypto"
                    else "maximum_equity_annualized_volatility"
                ]
            )
        ),
    }
    weights = {name: Decimal(value) for name, value in policy["score_weights"].items()}
    base = sum((components[name] * weights[name] for name in components), ZERO)
    uncertainty_multiplier = ONE - uncertainty
    drawdown_multiplier = _clamp(
        ONE - marginal_drawdown / Decimal(policy["maximum_marginal_drawdown_r"])
    )
    score = base * uncertainty_multiplier * drawdown_multiplier * HUNDRED
    details = {name: _text(value * HUNDRED) for name, value in sorted(components.items())}
    details["uncertainty_multiplier_pct"] = _text(uncertainty_multiplier * HUNDRED)
    details["marginal_drawdown_multiplier_pct"] = _text(drawdown_multiplier * HUNDRED)
    return _text(score), details


def optimize_cross_asset_allocation(
    *,
    run_id: str,
    candidates: Sequence[CrossAssetCandidate],
    portfolio: CrossAssetPortfolioSnapshot,
    config: Mapping[str, Any],
    as_of: str,
) -> CrossAssetAllocationPlan:
    """Rank and allocate a bounded advisory portfolio using exact evidence."""
    evaluation_time = _timestamp(as_of, "as_of")
    canonical_run_id = _required_text(run_id, "run_id")
    current_config_hash = _hash(config.get("effective_config_hash"), "effective_config_hash")
    if effective_config_hash(dict(config)) != current_config_hash:
        raise CrossAssetAllocationError("effective configuration hash does not match the policy")
    configured_formulas = config.get("formula_versions") or {}
    required_formulas = {
        "trade_economics": _required_text(
            configured_formulas.get("trade_economics"),
            "formula_versions.trade_economics",
        ),
        "profitability_ranking": _required_text(
            configured_formulas.get("profitability_ranking"),
            "formula_versions.profitability_ranking",
        ),
        "cross_asset_allocation": CROSS_ASSET_ALLOCATION_FORMULA_VERSION,
    }
    if configured_formulas.get("cross_asset_allocation") != CROSS_ASSET_ALLOCATION_FORMULA_VERSION:
        raise CrossAssetAllocationError("configured cross-asset formula identity is not current")
    policy = _policy(config)
    portfolio_data = portfolio.canonical(
        current_config_hash=current_config_hash,
        evaluation_time=evaluation_time,
        maximum_age_seconds=Decimal(policy["portfolio_snapshot_ttl_seconds"]),
    )
    if not portfolio_data["authoritative"]:
        raise CrossAssetAllocationError("portfolio snapshot is not authoritative")
    candidate_rows = [
        candidate.canonical(
            current_config_hash=current_config_hash,
            required_formula_versions=required_formulas,
            evaluation_time=evaluation_time,
            maximum_age_seconds=Decimal(policy["candidate_ttl_seconds"]),
        )
        for candidate in candidates
    ]
    candidate_rows.sort(key=lambda row: row["candidate_id"])
    if any(row["run_id"] != canonical_run_id for row in candidate_rows):
        raise CrossAssetAllocationError(
            "candidate run identity does not match the allocation run"
        )
    held_symbols = {
        symbol
        for symbol, exposure in portfolio_data["symbol_exposure"].items()
        if Decimal(exposure) > ZERO
    }
    for row in candidate_rows:
        if row["current_position"] != (row["symbol"] in held_symbols):
            raise CrossAssetAllocationError(
                f"candidate current-position evidence disagrees with portfolio for {row['symbol']}"
            )
        if row["current_position"] != (row["action"] == "add"):
            raise CrossAssetAllocationError(
                f"candidate action disagrees with current-position evidence for {row['symbol']}"
            )
    identities = [row["candidate_id"] for row in candidate_rows]
    if len(identities) != len(set(identities)):
        raise CrossAssetAllocationError("candidate_id values must be unique")
    sources = [(row["source_type"], row["source_id"]) for row in candidate_rows]
    if len(sources) != len(set(sources)):
        raise CrossAssetAllocationError("candidate source authority cannot be reused")

    scored: list[tuple[Decimal, str, dict[str, Any], dict[str, str], tuple[str, ...]]] = []
    for row in candidate_rows:
        reasons: list[str] = []
        if not row["source_authoritative"]:
            reasons.append("source_not_authoritative")
        if not row["profitability_eligible"]:
            reasons.append("profitability_ineligible")
        if not row["conflict_free"]:
            reasons.append("order_or_position_conflict")
        if row["strategy_state"] not in {"ACTIVE", "EXPLORATION"}:
            reasons.append("strategy_not_allocatable")
        if Decimal(row["expected_net_profit"]) <= ZERO:
            reasons.append("nonpositive_expected_net_profit")
        if Decimal(row["expected_net_r"]) <= ZERO:
            reasons.append("nonpositive_expected_net_r")
        if Decimal(row["conservative_expected_net_r"]) <= ZERO:
            reasons.append("nonpositive_conservative_expected_net_r")
        if Decimal(row["marginal_portfolio_contribution_r"]) <= ZERO:
            reasons.append("nonpositive_marginal_portfolio_contribution")
        if Decimal(row["cost_to_gross_edge_ratio"]) > Decimal(
            policy["maximum_cost_to_gross_edge_ratio"]
        ):
            reasons.append("execution_cost_burden_exceeds_policy")
        minimum_liquidity = Decimal(
            policy[
                "minimum_crypto_liquidity_usd"
                if row["asset_class"] == "crypto"
                else "minimum_equity_liquidity_usd"
            ]
        )
        if Decimal(row["liquidity_notional"]) < minimum_liquidity:
            reasons.append("liquidity_below_policy")
        if Decimal(row["marginal_drawdown_r"]) > Decimal(
            policy["maximum_marginal_drawdown_r"]
        ):
            reasons.append("marginal_drawdown_exceeds_policy")
        maximum_volatility = Decimal(
            policy[
                "maximum_crypto_annualized_volatility"
                if row["asset_class"] == "crypto"
                else "maximum_equity_annualized_volatility"
            ]
        )
        if Decimal(row["annualized_volatility"]) > maximum_volatility:
            reasons.append("annualized_volatility_exceeds_policy")
        score, components = _candidate_score(row, policy)
        scored.append((Decimal(score), row["candidate_id"], row, components, tuple(reasons)))
    scored.sort(key=lambda item: (-item[0], item[1]))

    global_reasons: list[str] = []
    if portfolio_data["kill_switch_active"]:
        global_reasons.append("kill_switch_active")
    for key in ("loss_evidence_fresh", "database_healthy", "internet_healthy", "power_healthy", "broker_healthy"):
        if not portfolio_data[key]:
            global_reasons.append(key.replace("_healthy", "_unhealthy") if key.endswith("_healthy") else "loss_evidence_stale")
    if Decimal(portfolio_data["daily_loss_pct"]) >= Decimal(policy["daily_loss_halt_pct"]):
        global_reasons.append("daily_loss_halt")
    if Decimal(portfolio_data["weekly_loss_pct"]) >= Decimal(policy["weekly_loss_halt_pct"]):
        global_reasons.append("weekly_loss_halt")
    if Decimal(portfolio_data["drawdown_pct"]) >= Decimal(policy["drawdown_halt_pct"]):
        global_reasons.append("drawdown_halt")

    equity = Decimal(portfolio_data["equity"])
    cash = Decimal(portfolio_data["cash"])
    buying_power = Decimal(portfolio_data["buying_power"])
    reserve = equity * Decimal(policy["minimum_cash_reserve_pct"]) / HUNDRED
    deployable_cash = max(ZERO, min(buying_power, cash - reserve))
    throttle = (
        Decimal(policy["drawdown_throttle_multiplier"])
        if Decimal(portfolio_data["drawdown_pct"])
        >= Decimal(policy["drawdown_throttle_start_pct"])
        else ONE
    )
    maximum_gross = equity * Decimal(policy["maximum_total_gross_exposure_pct"]) / HUNDRED * throttle
    maximum_heat = equity * Decimal(policy["maximum_stop_heat_pct"]) / HUNDRED * throttle
    gross = Decimal(portfolio_data["gross_exposure"])
    heat = Decimal(portfolio_data["stop_heat"])
    position_count = int(portfolio_data["position_count"])
    asset_position_count = {
        key: int(value)
        for key, value in portfolio_data["asset_class_position_count"].items()
    }
    symbol_exposure = {key: Decimal(value) for key, value in portfolio_data["symbol_exposure"].items()}
    cluster_exposure = {key: Decimal(value) for key, value in portfolio_data["cluster_exposure"].items()}
    asset_exposure = {key: Decimal(value) for key, value in portfolio_data["asset_class_exposure"].items()}
    asset_heat = {key: Decimal(value) for key, value in portfolio_data["asset_class_stop_heat"].items()}
    strategy_heat = {key: Decimal(value) for key, value in portfolio_data["strategy_stop_heat"].items()}
    allocations: list[dict[str, Any]] = []
    planned_symbols: set[str] = set()

    for rank, (score, identity, row, components, candidate_reasons) in enumerate(scored, start=1):
        reasons = list(global_reasons) + list(candidate_reasons)
        requested_notional = Decimal(row["proposed_notional"])
        requested_economic_risk = Decimal(row["economic_risk_dollars"])
        requested_risk = Decimal(row["stop_risk_dollars"])
        risk_per_notional = requested_risk / requested_notional
        economic_risk_per_notional = requested_economic_risk / requested_notional
        asset_key = "equity" if row["asset_class"] in {"equity", "etf"} else "crypto"
        asset_limit_pct = Decimal(
            policy[
                "maximum_crypto_exposure_pct"
                if asset_key == "crypto"
                else "maximum_equity_exposure_pct"
            ]
        )
        caps = {
            "buying_power_and_cash_reserve": deployable_cash,
            "total_gross_exposure": max(ZERO, maximum_gross - gross),
            "symbol_exposure": max(
                ZERO,
                equity * Decimal(policy["maximum_symbol_exposure_pct"]) / HUNDRED
                - symbol_exposure.get(row["symbol"], ZERO),
            ),
            "cluster_exposure": max(
                ZERO,
                equity * Decimal(policy["maximum_cluster_exposure_pct"]) / HUNDRED
                - cluster_exposure.get(row["cluster"], ZERO),
            ),
            "asset_class_exposure": max(
                ZERO,
                equity * asset_limit_pct / HUNDRED - asset_exposure.get(asset_key, ZERO),
            ),
            "total_stop_heat": (
                max(ZERO, maximum_heat - heat) / risk_per_notional
                if risk_per_notional > ZERO
                else ZERO
            ),
            "strategy_stop_heat": (
                max(
                    ZERO,
                    equity * Decimal(policy["maximum_strategy_stop_heat_pct"]) / HUNDRED
                    - strategy_heat.get(row["strategy_version"], ZERO),
                )
                / risk_per_notional
                if risk_per_notional > ZERO
                else ZERO
            ),
            "asset_class_stop_heat": (
                max(
                    ZERO,
                    equity
                    * Decimal(
                        policy["maximum_crypto_stop_heat_pct"]
                        if asset_key == "crypto"
                        else policy["maximum_stop_heat_pct"]
                    )
                    / HUNDRED
                    - asset_heat.get(asset_key, ZERO),
                )
                / risk_per_notional
                if risk_per_notional > ZERO
                else ZERO
            ),
            "trade_stop_risk": (
                equity
                * Decimal(
                    policy["maximum_crypto_trade_stop_risk_pct"]
                    if asset_key == "crypto"
                    else policy["maximum_equity_trade_stop_risk_pct"]
                )
                / HUNDRED
                / risk_per_notional
                if risk_per_notional > ZERO
                else ZERO
            ),
        }
        if row["strategy_state"] == "EXPLORATION" and risk_per_notional > ZERO:
            caps["exploration_stop_heat"] = (
                max(
                    ZERO,
                    equity * Decimal(policy["maximum_exploration_stop_heat_pct"]) / HUNDRED
                    - strategy_heat.get(row["strategy_version"], ZERO),
                )
                / risk_per_notional
            )
        creates_position = not row["current_position"] and row["symbol"] not in planned_symbols
        if creates_position and position_count >= policy["maximum_positions"]:
            reasons.append("maximum_positions_reached")
        asset_position_limit = policy[
            "maximum_crypto_positions"
            if asset_key == "crypto"
            else "maximum_equity_positions"
        ]
        if creates_position and asset_position_count.get(asset_key, 0) >= asset_position_limit:
            reasons.append(f"maximum_{asset_key}_positions_reached")
        allocatable = min([requested_notional, *caps.values()])
        minimum = Decimal(policy["minimum_executable_notional_usd"])
        if allocatable < minimum:
            reasons.append("remaining_capacity_below_minimum")
            allocatable = ZERO
        if reasons:
            allocatable = ZERO
        allocated_risk = allocatable * risk_per_notional
        allocated_economic_risk = allocatable * economic_risk_per_notional
        binding = tuple(sorted(name for name, value in caps.items() if value <= requested_notional))
        if allocatable > ZERO:
            deployable_cash -= allocatable
            gross += allocatable
            heat += allocated_risk
            symbol_exposure[row["symbol"]] = symbol_exposure.get(row["symbol"], ZERO) + allocatable
            cluster_exposure[row["cluster"]] = cluster_exposure.get(row["cluster"], ZERO) + allocatable
            asset_exposure[asset_key] = asset_exposure.get(asset_key, ZERO) + allocatable
            asset_heat[asset_key] = asset_heat.get(asset_key, ZERO) + allocated_risk
            strategy_heat[row["strategy_version"]] = strategy_heat.get(row["strategy_version"], ZERO) + allocated_risk
            if creates_position:
                position_count += 1
                asset_position_count[asset_key] = asset_position_count.get(asset_key, 0) + 1
                planned_symbols.add(row["symbol"])
            decision = (
                "ALLOCATE_RESEARCH_ONLY"
                if row["execution_lane"] == "research_only"
                else "ALLOCATE_ADVISORY"
            )
            if allocatable < requested_notional:
                decision += "_PARTIAL"
        else:
            decision = "REJECT"
        scale = allocatable / requested_notional
        allocations.append(
            {
                "rank": rank,
                "candidate_id": identity,
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "source_fingerprint": row["source_fingerprint"],
                "asset_class": row["asset_class"],
                "symbol": row["symbol"],
                "cluster": row["cluster"],
                "strategy_version": row["strategy_version"],
                "execution_lane": row["execution_lane"],
                "decision": decision,
                "ranking_score": _text(score),
                "ranking_components": components,
                "requested_notional": row["proposed_notional"],
                "allocated_notional": _text(allocatable),
                "requested_stop_risk": row["stop_risk_dollars"],
                "allocated_stop_risk": _text(allocated_risk),
                "requested_economic_risk": row["economic_risk_dollars"],
                "allocated_economic_risk": _text(allocated_economic_risk),
                "allocation_fraction": _text(scale),
                "expected_net_profit_contribution": _text(
                    Decimal(row["expected_net_profit"]) * scale
                ),
                "conservative_expected_profit_contribution": _text(
                    Decimal(row["conservative_expected_net_r"])
                    * allocated_economic_risk
                ),
                "binding_constraints": list(binding),
                "rejection_reasons": sorted(set(reasons)),
                "order_authority": False,
            }
        )

    allocated = [row for row in allocations if Decimal(row["allocated_notional"]) > ZERO]
    expected_profit = sum(
        (Decimal(row["expected_net_profit_contribution"]) for row in allocated), ZERO
    )
    tail_risk = sum(
        (
            Decimal(candidate[2]["probability_severe_loss"])
            * Decimal(decision["allocated_stop_risk"])
            for candidate, decision in zip(scored, allocations, strict=True)
        ),
        ZERO,
    )
    exposure_shares = [value / gross for value in asset_exposure.values() if gross > ZERO]
    concentration = sum((value * value for value in exposure_shares), ZERO)
    volatility_loadings = [
        Decimal(decision["allocated_notional"])
        / equity
        * Decimal(candidate[2]["annualized_volatility"])
        for candidate, decision in zip(scored, allocations, strict=True)
        if Decimal(decision["allocated_notional"]) > ZERO
    ]
    existing_volatility = Decimal(portfolio_data["portfolio_annualized_volatility"])
    existing_covariance_upper = sum(
        (
            existing_volatility
            * Decimal(decision["allocated_notional"])
            / equity
            * Decimal(candidate[2]["annualized_volatility"])
            * abs(Decimal(candidate[2]["correlation_to_portfolio"]))
            for candidate, decision in zip(scored, allocations, strict=True)
            if Decimal(decision["allocated_notional"]) > ZERO
        ),
        ZERO,
    )
    portfolio_variance_upper = (
        existing_volatility * existing_volatility
        + Decimal("2") * existing_covariance_upper
        + sum(volatility_loadings, ZERO) ** 2
    )
    volatility_upper = portfolio_variance_upper.sqrt()
    marginal_drawdown_dollars = sum(
        (
            Decimal(candidate[2]["marginal_drawdown_r"])
            * Decimal(decision["allocated_economic_risk"])
            for candidate, decision in zip(scored, allocations, strict=True)
        ),
        ZERO,
    )
    liquidity_utilizations = [
        Decimal(decision["allocated_notional"])
        / Decimal(candidate[2]["liquidity_notional"])
        for candidate, decision in zip(scored, allocations, strict=True)
        if Decimal(decision["allocated_notional"]) > ZERO
    ]
    summary = {
        "allocation_mode": "research_advisory",
        "candidate_count": len(candidate_rows),
        "allocated_candidate_count": len(allocated),
        "keep_cash": not allocated,
        "global_blockers": sorted(set(global_reasons)),
        "expected_net_profit": _text(expected_profit),
        "expected_net_return_pct_equity": _text(expected_profit / equity * HUNDRED),
        "tail_loss_probability_weighted_stop_risk": _text(tail_risk),
        "expected_marginal_drawdown_dollars": _text(marginal_drawdown_dollars),
        "portfolio_annualized_volatility_upper_bound": _text(volatility_upper),
        "portfolio_variance_upper_bound": _text(portfolio_variance_upper),
        "maximum_liquidity_utilization": _text(
            max(liquidity_utilizations, default=ZERO)
        ),
        "gross_exposure_after": _text(gross),
        "stop_heat_after": _text(heat),
        "cash_deployable_after": _text(deployable_cash),
        "position_count_after": position_count,
        "asset_class_position_count_after": dict(sorted(asset_position_count.items())),
        "asset_class_concentration_hhi": _text(concentration),
        "asset_class_exposure_after": {key: _text(value) for key, value in sorted(asset_exposure.items())},
        "asset_class_stop_heat_after": {key: _text(value) for key, value in sorted(asset_heat.items())},
        "symbol_exposure_after": {key: _text(value) for key, value in sorted(symbol_exposure.items())},
        "cluster_exposure_after": {key: _text(value) for key, value in sorted(cluster_exposure.items())},
        "strategy_stop_heat_after": {key: _text(value) for key, value in sorted(strategy_heat.items())},
        "drawdown_throttle_multiplier": _text(throttle),
        "execution_authorized": False,
        "linear_scaling_assumption": True,
    }
    candidate_set_fingerprint = _fingerprint(candidate_rows)
    policy_fingerprint = _fingerprint(policy)
    def evidence_expiry(timestamp: str, ttl_key: str) -> datetime:
        return _timestamp(timestamp, timestamp) + timedelta(
            microseconds=int(Decimal(policy[ttl_key]) * Decimal("1000000"))
        )

    expiry_candidates = [
        evidence_expiry(evaluation_time.isoformat(), "plan_ttl_seconds"),
        evidence_expiry(
            portfolio_data["as_of"], "portfolio_snapshot_ttl_seconds"
        ),
        *(
            evidence_expiry(row["evidence_as_of"], "candidate_ttl_seconds")
            for row in candidate_rows
        ),
    ]
    expires = min(expiry_candidates)
    plan_payload = {
        "run_id": canonical_run_id,
        "as_of": evaluation_time.isoformat(),
        "expires_at": expires.isoformat(),
        "portfolio_snapshot_id": portfolio_data["snapshot_id"],
        "portfolio_snapshot_fingerprint": portfolio_data["snapshot_fingerprint"],
        "portfolio": portfolio_data,
        "candidate_set_fingerprint": candidate_set_fingerprint,
        "candidates": candidate_rows,
        "policy_fingerprint": policy_fingerprint,
        "policy": policy,
        "decisions": allocations,
        "summary": summary,
        "execution_authorized": False,
        "config_hash": current_config_hash,
        "formula_version": CROSS_ASSET_ALLOCATION_FORMULA_VERSION,
        "schema_version": CROSS_ASSET_ALLOCATION_SCHEMA_VERSION,
    }
    plan_fingerprint = _fingerprint(plan_payload)
    return CrossAssetAllocationPlan(
        id=plan_fingerprint[:32],
        run_id=plan_payload["run_id"],
        as_of=plan_payload["as_of"],
        expires_at=plan_payload["expires_at"],
        portfolio_snapshot_id=portfolio_data["snapshot_id"],
        portfolio_snapshot_fingerprint=portfolio_data["snapshot_fingerprint"],
        candidate_set_fingerprint=candidate_set_fingerprint,
        policy_fingerprint=policy_fingerprint,
        plan_fingerprint=plan_fingerprint,
        decisions=tuple(allocations),
        summary=summary,
        execution_authorized=False,
        config_hash=current_config_hash,
    )


def apply_cross_asset_allocation_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cross_asset_allocation_plans(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          as_of TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          portfolio_snapshot_id TEXT NOT NULL,
          portfolio_snapshot_fingerprint TEXT NOT NULL,
          candidate_set_json TEXT NOT NULL,
          candidate_set_fingerprint TEXT NOT NULL,
          policy_json TEXT NOT NULL,
          policy_fingerprint TEXT NOT NULL,
          plan_json TEXT NOT NULL,
          plan_fingerprint TEXT NOT NULL,
          execution_authorized INTEGER NOT NULL CHECK(execution_authorized=0),
          config_hash TEXT NOT NULL,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cross_asset_plan_run_time
          ON cross_asset_allocation_plans(run_id,as_of);
        CREATE TRIGGER IF NOT EXISTS trg_cross_asset_plan_immutable_update
          BEFORE UPDATE ON cross_asset_allocation_plans
          BEGIN SELECT RAISE(ABORT,'cross-asset allocation plans are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS trg_cross_asset_plan_immutable_delete
          BEFORE DELETE ON cross_asset_allocation_plans
          BEGIN SELECT RAISE(ABORT,'cross-asset allocation plans are immutable'); END;
        """
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                CROSS_ASSET_ALLOCATION_SCHEMA_VERSION,
                iso_now(),
                "immutable research-advisory cross-asset allocation plans",
            ),
        )


class CrossAssetAllocationStore:
    def __init__(self, storage: Any, config: Mapping[str, Any]) -> None:
        self.storage = storage
        self.config = config

    @staticmethod
    def _payload(
        plan: CrossAssetAllocationPlan,
        candidates: Sequence[CrossAssetCandidate],
        portfolio: CrossAssetPortfolioSnapshot,
        config: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        as_of = _timestamp(plan.as_of, "plan.as_of")
        current_hash = _hash(config.get("effective_config_hash"), "effective_config_hash")
        policy = _policy(config)
        formulas = config.get("formula_versions") or {}
        required = {
            "trade_economics": formulas.get("trade_economics"),
            "profitability_ranking": formulas.get("profitability_ranking"),
            "cross_asset_allocation": CROSS_ASSET_ALLOCATION_FORMULA_VERSION,
        }
        portfolio_data = portfolio.canonical(
            current_config_hash=current_hash,
            evaluation_time=as_of,
            maximum_age_seconds=Decimal(policy["portfolio_snapshot_ttl_seconds"]),
        )
        candidate_rows = [
            candidate.canonical(
                current_config_hash=current_hash,
                required_formula_versions=required,
                evaluation_time=as_of,
                maximum_age_seconds=Decimal(policy["candidate_ttl_seconds"]),
            )
            for candidate in candidates
        ]
        candidate_rows.sort(key=lambda row: row["candidate_id"])
        payload = {
            "id": plan.id,
            "run_id": plan.run_id,
            "as_of": plan.as_of,
            "expires_at": plan.expires_at,
            "portfolio_snapshot_id": plan.portfolio_snapshot_id,
            "portfolio_snapshot_fingerprint": plan.portfolio_snapshot_fingerprint,
            "portfolio": portfolio_data,
            "candidate_set_fingerprint": plan.candidate_set_fingerprint,
            "candidates": candidate_rows,
            "policy_fingerprint": plan.policy_fingerprint,
            "policy": policy,
            "decisions": list(plan.decisions),
            "summary": dict(plan.summary),
            "execution_authorized": False,
            "config_hash": plan.config_hash,
            "formula_version": plan.formula_version,
            "schema_version": plan.schema_version,
        }
        return payload, candidate_rows, policy

    def create(
        self,
        *,
        run_id: str,
        candidates: Sequence[CrossAssetCandidate],
        portfolio: CrossAssetPortfolioSnapshot,
        as_of: str,
    ) -> CrossAssetAllocationPlan:
        plan = optimize_cross_asset_allocation(
            run_id=run_id,
            candidates=candidates,
            portfolio=portfolio,
            config=self.config,
            as_of=as_of,
        )
        payload, candidate_rows, policy = self._payload(
            plan, candidates, portfolio, self.config
        )
        if _fingerprint({key: value for key, value in payload.items() if key != "id"}) != plan.plan_fingerprint:
            # The optimizer fingerprints the plan body before adding its derived
            # ID. Reconstruct that exact body explicitly to catch persistence drift.
            optimizer_body = dict(payload)
            optimizer_body.pop("id")
            if _fingerprint(optimizer_body) != plan.plan_fingerprint:
                raise CrossAssetAllocationError("allocation plan persistence payload drifted")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            apply_cross_asset_allocation_schema(conn, record_migration=False)
            conn.execute(
                """INSERT OR IGNORE INTO cross_asset_allocation_plans(
                  id,run_id,as_of,expires_at,portfolio_snapshot_id,
                  portfolio_snapshot_fingerprint,candidate_set_json,
                  candidate_set_fingerprint,policy_json,policy_fingerprint,
                  plan_json,plan_fingerprint,execution_authorized,config_hash,
                  formula_version,schema_version,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.id,
                    plan.run_id,
                    plan.as_of,
                    plan.expires_at,
                    plan.portfolio_snapshot_id,
                    plan.portfolio_snapshot_fingerprint,
                    _canonical_json(candidate_rows),
                    plan.candidate_set_fingerprint,
                    _canonical_json(policy),
                    plan.policy_fingerprint,
                    _canonical_json(payload),
                    plan.plan_fingerprint,
                    0,
                    plan.config_hash,
                    plan.formula_version,
                    plan.schema_version,
                    iso_now(),
                ),
            )
        return self.load_verified(plan.id, now=_timestamp(as_of, "as_of"))

    def load_verified(
        self, plan_id: str, *, now: datetime | None = None
    ) -> CrossAssetAllocationPlan:
        rows = self.storage.fetch_all(
            "SELECT * FROM cross_asset_allocation_plans WHERE id=?",
            (_required_text(plan_id, "plan_id"),),
        )
        if len(rows) != 1:
            raise CrossAssetAllocationError("cross-asset allocation plan is missing or duplicated")
        row = rows[0]
        try:
            candidates_data = json.loads(row["candidate_set_json"])
            policy_data = json.loads(row["policy_json"])
            payload = json.loads(row["plan_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CrossAssetAllocationError("cross-asset allocation JSON is invalid") from exc
        if not isinstance(candidates_data, list) or not isinstance(policy_data, dict) or not isinstance(payload, dict):
            raise CrossAssetAllocationError("cross-asset allocation JSON shape is invalid")
        if _fingerprint(candidates_data) != row["candidate_set_fingerprint"]:
            raise CrossAssetAllocationError("cross-asset candidate-set fingerprint mismatch")
        if _fingerprint(policy_data) != row["policy_fingerprint"]:
            raise CrossAssetAllocationError("cross-asset policy fingerprint mismatch")
        body = dict(payload)
        body.pop("id", None)
        if _fingerprint(body) != row["plan_fingerprint"]:
            raise CrossAssetAllocationError("cross-asset plan fingerprint mismatch")
        if row["id"] != row["plan_fingerprint"][:32] or payload.get("id") != row["id"]:
            raise CrossAssetAllocationError("cross-asset plan identity mismatch")
        scalar = (
            "run_id",
            "as_of",
            "expires_at",
            "portfolio_snapshot_id",
            "portfolio_snapshot_fingerprint",
            "candidate_set_fingerprint",
            "policy_fingerprint",
            "config_hash",
            "formula_version",
            "schema_version",
        )
        for key in scalar:
            if row[key] != payload.get(key):
                raise CrossAssetAllocationError(f"cross-asset persisted column mismatch: {key}")
        if row["execution_authorized"] != 0 or payload.get("execution_authorized") is not False:
            raise CrossAssetAllocationError("cross-asset plan escaped advisory-only authority")
        current_hash = _hash(self.config.get("effective_config_hash"), "effective_config_hash")
        if row["config_hash"] != current_hash:
            raise CrossAssetAllocationError("cross-asset plan configuration identity changed")
        if row["formula_version"] != CROSS_ASSET_ALLOCATION_FORMULA_VERSION or row["schema_version"] != CROSS_ASSET_ALLOCATION_SCHEMA_VERSION:
            raise CrossAssetAllocationError("cross-asset plan version is obsolete")
        if policy_data != _policy(self.config):
            raise CrossAssetAllocationError("cross-asset persisted policy is not current")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current > _timestamp(row["expires_at"], "plan.expires_at"):
            raise CrossAssetAllocationError("cross-asset allocation plan expired")
        portfolio_data = payload.get("portfolio")
        if not isinstance(portfolio_data, dict):
            raise CrossAssetAllocationError("cross-asset portfolio payload is missing")
        candidate_objects = [CrossAssetCandidate.from_mapping(item) for item in candidates_data]
        portfolio_object = CrossAssetPortfolioSnapshot.from_mapping(portfolio_data)
        recomputed = optimize_cross_asset_allocation(
            run_id=row["run_id"],
            candidates=candidate_objects,
            portfolio=portfolio_object,
            config=self.config,
            as_of=row["as_of"],
        )
        if (
            recomputed.plan_fingerprint != row["plan_fingerprint"]
            or list(recomputed.decisions) != payload.get("decisions")
            or dict(recomputed.summary) != payload.get("summary")
        ):
            raise CrossAssetAllocationError("cross-asset plan independent recomputation mismatch")
        return recomputed


__all__ = [
    "CrossAssetAllocationError",
    "CrossAssetAllocationPlan",
    "CrossAssetAllocationStore",
    "CrossAssetCandidate",
    "CrossAssetPortfolioSnapshot",
    "apply_cross_asset_allocation_schema",
    "optimize_cross_asset_allocation",
]
