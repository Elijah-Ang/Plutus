"""Canonical, immutable candidate-level trade economics.

The strategy performance engine estimates whether a strategy has evidence of
positive expectancy.  This module applies that evidence to one exact candidate
and records the complete expected economics before proposal authority can use
it.  Money, prices, quantities, probabilities, and ratios use ``Decimal`` from
input through persistence; binary floats are rejected at this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .formula_versions import (
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
    TRADE_ECONOMICS_FORMULA_VERSION,
    TRADE_ECONOMICS_SCHEMA_VERSION,
)
from .utils import iso_now


_ZERO = Decimal("0")
_ONE = Decimal("1")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_ASSET_CLASSES = frozenset({"equity", "etf", "crypto"})
_ALLOWED_ACTIONS = frozenset({"entry", "add", "rotation_entry"})
_ALLOWED_RECORD_CLASSES = frozenset(
    {
        "research_estimate",
        "shadow_candidate",
        "proposal_candidate",
        "approved_but_blocked",
    }
)
_PROPOSAL_RECORD_CLASSES = frozenset({"proposal_candidate", "approved_but_blocked"})
_REQUIRED_FORMULA_IDENTITIES = {
    "evidence": EVIDENCE_VERSION,
    "strategy_performance": STRATEGY_PERFORMANCE_VERSION,
    "strategy_policy": STRATEGY_POLICY_VERSION,
    "trade_economics": TRADE_ECONOMICS_FORMULA_VERSION,
}


class TradeEconomicsError(ValueError):
    """Raised when candidate economics or its durable authority is invalid."""


def _decimal(
    value: Any,
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TradeEconomicsError(f"{name} must use Decimal, an integer, or a decimal string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TradeEconomicsError(f"{name} must be a valid decimal") from exc
    if not result.is_finite():
        raise TradeEconomicsError(f"{name} must be finite")
    if positive and result <= _ZERO:
        raise TradeEconomicsError(f"{name} must be positive")
    if minimum is not None and result < minimum:
        raise TradeEconomicsError(f"{name} must be at least {_decimal_text(minimum)}")
    if maximum is not None and result > maximum:
        raise TradeEconomicsError(f"{name} must be at most {_decimal_text(maximum)}")
    return _ZERO if result == _ZERO else result


def _decimal_text(value: Decimal) -> str:
    value = _ZERO if value == _ZERO else value
    return format(value.normalize(), "f")


def _required_text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise TradeEconomicsError(f"{name} is required")
    return result


def _utc_timestamp(value: Any, name: str) -> str:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradeEconomicsError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise TradeEconomicsError(f"{name} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TradeEconomicsCosts:
    """Expected dollar drag for one complete trade path.

    ``worst_reasonable_additional_cost`` is a stress increment and is not
    included in expected total cost.  It is added only to the worst-reasonable
    loss check against the maximum approved loss.
    """

    spread: Any
    slippage: Any
    fees: Any
    regulatory: Any
    crypto_transaction: Any
    market_impact: Any
    implementation_shortfall: Any
    adverse_selection: Any
    rejected_or_missed_fill: Any
    opportunity: Any
    approval_delay: Any
    holding: Any
    model_uncertainty: Any
    estimation_uncertainty: Any
    worst_reasonable_additional_cost: Any

    def canonical(self) -> dict[str, str]:
        return {
            field.name: _decimal_text(
                _decimal(getattr(self, field.name), f"costs.{field.name}", minimum=_ZERO)
            )
            for field in fields(self)
        }


@dataclass(frozen=True)
class TradeEconomicsPolicy:
    maximum_cost_to_gross_edge_ratio: Any = Decimal("0.50")
    maximum_break_even_win_probability: Any = Decimal("0.75")
    minimum_expected_net_r: Any = Decimal("0")
    minimum_conservative_net_r: Any = Decimal("0")
    minimum_marginal_portfolio_contribution_r: Any = Decimal("0")

    def canonical(self) -> dict[str, str]:
        maximum_cost = _decimal(
            self.maximum_cost_to_gross_edge_ratio,
            "policy.maximum_cost_to_gross_edge_ratio",
            minimum=_ZERO,
        )
        maximum_break_even = _decimal(
            self.maximum_break_even_win_probability,
            "policy.maximum_break_even_win_probability",
            minimum=_ZERO,
            maximum=_ONE,
        )
        return {
            "maximum_cost_to_gross_edge_ratio": _decimal_text(maximum_cost),
            "maximum_break_even_win_probability": _decimal_text(maximum_break_even),
            "minimum_expected_net_r": _decimal_text(
                _decimal(self.minimum_expected_net_r, "policy.minimum_expected_net_r")
            ),
            "minimum_conservative_net_r": _decimal_text(
                _decimal(
                    self.minimum_conservative_net_r,
                    "policy.minimum_conservative_net_r",
                )
            ),
            "minimum_marginal_portfolio_contribution_r": _decimal_text(
                _decimal(
                    self.minimum_marginal_portfolio_contribution_r,
                    "policy.minimum_marginal_portfolio_contribution_r",
                )
            ),
        }


@dataclass(frozen=True)
class TradeEconomicsInput:
    candidate_id: str
    run_id: str
    proposal_id: str | None
    record_class: str
    asset_class: str
    symbol: str
    side: str
    action: str
    request_basis: str
    strategy_version: str
    strategy_state: str
    setup_type: str
    market_regime: str
    volatility_regime: str
    liquidity_regime: str
    trend_regime: str
    breadth_regime: str
    estimated_at: str
    quantity: Any
    proposed_notional: Any
    entry_estimate: Any
    limit_price: Any
    stop_price: Any
    target_price: Any
    maximum_approved_loss: Any
    expected_win_probability: Any
    conservative_win_probability: Any
    expected_average_win: Any
    expected_average_loss: Any
    expected_holding_period_days: Any
    annualization_days: Any
    marginal_portfolio_contribution_r: Any
    performance_snapshot_id: str
    policy_decision_id: str
    evidence_version: str
    configuration_version: str
    config_hash: str
    formula_versions: Mapping[str, str]
    cost_model_version: str
    estimation_model_version: str

    def canonical(self) -> dict[str, Any]:
        record_class = _required_text(self.record_class, "record_class").lower()
        if record_class not in _ALLOWED_RECORD_CLASSES:
            raise TradeEconomicsError("record_class is unsupported")
        proposal_id = str(self.proposal_id).strip() if self.proposal_id is not None else None
        if proposal_id == "":
            proposal_id = None
        if record_class in _PROPOSAL_RECORD_CLASSES and proposal_id is None:
            raise TradeEconomicsError("proposal_id is required for proposal-linked economics")
        if record_class not in _PROPOSAL_RECORD_CLASSES and proposal_id is not None:
            raise TradeEconomicsError("proposal_id is forbidden for non-proposal economics")

        asset_class = _required_text(self.asset_class, "asset_class").lower()
        if asset_class not in _ALLOWED_ASSET_CLASSES:
            raise TradeEconomicsError("asset_class is unsupported")
        side = _required_text(self.side, "side").lower()
        if side != "buy":
            raise TradeEconomicsError("candidate trade economics v1 supports long-only BUY risk increases")
        action = _required_text(self.action, "action").lower()
        if action not in _ALLOWED_ACTIONS:
            raise TradeEconomicsError("action is unsupported")
        request_basis = _required_text(self.request_basis, "request_basis").lower()
        if request_basis not in {"quantity", "notional"}:
            raise TradeEconomicsError("request_basis must be quantity or notional")

        config_hash = _required_text(self.config_hash, "config_hash").lower()
        if not _SHA256.fullmatch(config_hash):
            raise TradeEconomicsError("config_hash must be a SHA-256 digest")
        configuration_version = _required_text(
            self.configuration_version, "configuration_version"
        )
        if configuration_version != CONFIGURATION_SCHEMA_VERSION:
            raise TradeEconomicsError("configuration_version is not current")
        evidence_version = _required_text(self.evidence_version, "evidence_version")
        if evidence_version != EVIDENCE_VERSION:
            raise TradeEconomicsError("evidence_version is not current")
        if not isinstance(self.formula_versions, Mapping):
            raise TradeEconomicsError("formula_versions must be a mapping")
        formula_versions = {
            str(key): _required_text(value, f"formula_versions.{key}")
            for key, value in sorted(self.formula_versions.items())
        }
        for key, expected in _REQUIRED_FORMULA_IDENTITIES.items():
            if formula_versions.get(key) != expected:
                raise TradeEconomicsError(
                    f"formula_versions.{key} must be {expected}"
                )

        decimal_fields = {
            "quantity": _decimal(self.quantity, "quantity", positive=True),
            "proposed_notional": _decimal(
                self.proposed_notional, "proposed_notional", positive=True
            ),
            "entry_estimate": _decimal(
                self.entry_estimate, "entry_estimate", positive=True
            ),
            "limit_price": _decimal(self.limit_price, "limit_price", positive=True),
            "stop_price": _decimal(self.stop_price, "stop_price", positive=True),
            "target_price": _decimal(self.target_price, "target_price", positive=True),
            "maximum_approved_loss": _decimal(
                self.maximum_approved_loss, "maximum_approved_loss", positive=True
            ),
            "expected_win_probability": _decimal(
                self.expected_win_probability,
                "expected_win_probability",
                minimum=_ZERO,
                maximum=_ONE,
            ),
            "conservative_win_probability": _decimal(
                self.conservative_win_probability,
                "conservative_win_probability",
                minimum=_ZERO,
                maximum=_ONE,
            ),
            "expected_average_win": _decimal(
                self.expected_average_win, "expected_average_win", positive=True
            ),
            "expected_average_loss": _decimal(
                self.expected_average_loss, "expected_average_loss", positive=True
            ),
            "expected_holding_period_days": _decimal(
                self.expected_holding_period_days,
                "expected_holding_period_days",
                positive=True,
            ),
            "annualization_days": _decimal(
                self.annualization_days, "annualization_days", positive=True
            ),
            "marginal_portfolio_contribution_r": _decimal(
                self.marginal_portfolio_contribution_r,
                "marginal_portfolio_contribution_r",
            ),
        }
        if decimal_fields["conservative_win_probability"] > decimal_fields[
            "expected_win_probability"
        ]:
            raise TradeEconomicsError(
                "conservative_win_probability cannot exceed expected_win_probability"
            )

        return {
            "candidate_id": _required_text(self.candidate_id, "candidate_id"),
            "run_id": _required_text(self.run_id, "run_id"),
            "proposal_id": proposal_id,
            "record_class": record_class,
            "asset_class": asset_class,
            "symbol": _required_text(self.symbol, "symbol").upper(),
            "side": side,
            "action": action,
            "request_basis": request_basis,
            "strategy_version": _required_text(
                self.strategy_version, "strategy_version"
            ),
            "strategy_state": _required_text(
                self.strategy_state, "strategy_state"
            ).upper(),
            "setup_type": _required_text(self.setup_type, "setup_type"),
            "market_regime": _required_text(self.market_regime, "market_regime"),
            "volatility_regime": _required_text(
                self.volatility_regime, "volatility_regime"
            ),
            "liquidity_regime": _required_text(
                self.liquidity_regime, "liquidity_regime"
            ),
            "trend_regime": _required_text(self.trend_regime, "trend_regime"),
            "breadth_regime": _required_text(self.breadth_regime, "breadth_regime"),
            "estimated_at": _utc_timestamp(self.estimated_at, "estimated_at"),
            **{
                name: _decimal_text(value)
                for name, value in decimal_fields.items()
            },
            "performance_snapshot_id": _required_text(
                self.performance_snapshot_id, "performance_snapshot_id"
            ),
            "policy_decision_id": _required_text(
                self.policy_decision_id, "policy_decision_id"
            ),
            "evidence_version": evidence_version,
            "configuration_version": configuration_version,
            "config_hash": config_hash,
            "formula_versions": formula_versions,
            "cost_model_version": _required_text(
                self.cost_model_version, "cost_model_version"
            ),
            "estimation_model_version": _required_text(
                self.estimation_model_version, "estimation_model_version"
            ),
        }


@dataclass(frozen=True)
class TradeEconomicsRecord:
    id: str
    candidate: Mapping[str, Any]
    costs: Mapping[str, str]
    policy: Mapping[str, str]
    metrics: Mapping[str, Any]
    profitability_eligible: bool
    rejection_reasons: tuple[str, ...]
    input_fingerprint: str
    record_fingerprint: str
    formula_version: str = TRADE_ECONOMICS_FORMULA_VERSION
    schema_version: str = TRADE_ECONOMICS_SCHEMA_VERSION

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "candidate": dict(self.candidate),
            "costs": dict(self.costs),
            "policy": dict(self.policy),
            "metrics": dict(self.metrics),
            "profitability_eligible": self.profitability_eligible,
            "rejection_reasons": list(self.rejection_reasons),
            "input_fingerprint": self.input_fingerprint,
            "formula_version": self.formula_version,
            "schema_version": self.schema_version,
        }


def calculate_trade_economics(
    candidate: TradeEconomicsInput,
    costs: TradeEconomicsCosts,
    policy: TradeEconomicsPolicy | None = None,
) -> TradeEconomicsRecord:
    """Calculate complete expected economics for one exact long-risk candidate."""

    candidate_payload = candidate.canonical()
    cost_payload = costs.canonical()
    policy_payload = (policy or TradeEconomicsPolicy()).canonical()

    quantity = Decimal(candidate_payload["quantity"])
    proposed_notional = Decimal(candidate_payload["proposed_notional"])
    entry = Decimal(candidate_payload["entry_estimate"])
    limit_price = Decimal(candidate_payload["limit_price"])
    stop = Decimal(candidate_payload["stop_price"])
    target = Decimal(candidate_payload["target_price"])
    maximum_loss = Decimal(candidate_payload["maximum_approved_loss"])
    win_probability = Decimal(candidate_payload["expected_win_probability"])
    conservative_probability = Decimal(
        candidate_payload["conservative_win_probability"]
    )
    average_win = Decimal(candidate_payload["expected_average_win"])
    average_loss = Decimal(candidate_payload["expected_average_loss"])
    holding_days = Decimal(candidate_payload["expected_holding_period_days"])
    annualization_days = Decimal(candidate_payload["annualization_days"])
    marginal_contribution = Decimal(
        candidate_payload["marginal_portfolio_contribution_r"]
    )

    if stop >= entry:
        raise TradeEconomicsError("stop_price must be below entry_estimate")
    if target <= entry:
        raise TradeEconomicsError("target_price must be above entry_estimate")
    if limit_price < entry:
        raise TradeEconomicsError(
            "limit_price must not improve the BUY beyond the entry estimate"
        )
    if limit_price >= target:
        raise TradeEconomicsError("limit_price must remain below target_price")
    calculated_notional = quantity * entry
    if calculated_notional != proposed_notional:
        raise TradeEconomicsError(
            "proposed_notional must exactly equal quantity multiplied by entry_estimate"
        )

    gross_upside = quantity * (target - entry)
    expected_downside = quantity * (entry - stop)
    if average_win > gross_upside:
        raise TradeEconomicsError(
            "expected_average_win cannot exceed displayed gross upside"
        )
    if average_loss > expected_downside:
        raise TradeEconomicsError(
            "expected_average_loss cannot exceed displayed stop downside"
        )

    expected_cost_names = tuple(
        name
        for name in cost_payload
        if name != "worst_reasonable_additional_cost"
    )
    execution_cost_names = (
        "spread",
        "slippage",
        "fees",
        "regulatory",
        "crypto_transaction",
        "market_impact",
        "implementation_shortfall",
        "adverse_selection",
        "rejected_or_missed_fill",
        "approval_delay",
    )
    uncertainty_cost_names = ("model_uncertainty", "estimation_uncertainty")
    expected_total_cost = sum(
        (Decimal(cost_payload[name]) for name in expected_cost_names), _ZERO
    )
    expected_execution_cost = sum(
        (Decimal(cost_payload[name]) for name in execution_cost_names), _ZERO
    )
    expected_uncertainty_cost = sum(
        (Decimal(cost_payload[name]) for name in uncertainty_cost_names), _ZERO
    )
    expected_holding_and_opportunity_cost = (
        Decimal(cost_payload["holding"]) + Decimal(cost_payload["opportunity"])
    )

    loss_probability = _ONE - win_probability
    conservative_loss_probability = _ONE - conservative_probability
    expected_gross_profit = (
        win_probability * average_win - loss_probability * average_loss
    )
    conservative_gross_profit = (
        conservative_probability * average_win
        - conservative_loss_probability * average_loss
    )
    expected_net_profit = expected_gross_profit - expected_total_cost
    conservative_net_profit = conservative_gross_profit - expected_total_cost
    expected_net_r = expected_net_profit / expected_downside
    conservative_net_r = conservative_net_profit / expected_downside
    break_even_probability = (average_loss + expected_total_cost) / (
        average_win + average_loss
    )
    gross_reward_to_risk = gross_upside / expected_downside
    capital_efficiency = expected_net_profit / proposed_notional
    capital_efficiency_per_day = capital_efficiency / holding_days
    annualized_capital_efficiency = (
        capital_efficiency_per_day * annualization_days
    )
    expected_profit_per_day = expected_net_profit / holding_days
    expected_r_per_day = expected_net_r / holding_days
    cost_to_gross_edge_ratio = (
        expected_total_cost / expected_gross_profit
        if expected_gross_profit > _ZERO
        else None
    )
    worst_reasonable_loss = (
        quantity * (limit_price - stop)
        + expected_total_cost
        + Decimal(cost_payload["worst_reasonable_additional_cost"])
    )
    if worst_reasonable_loss > maximum_loss:
        raise TradeEconomicsError(
            "worst reasonable loss exceeds maximum approved loss"
        )
    maximum_loss_headroom = maximum_loss - worst_reasonable_loss

    maximum_cost_ratio = Decimal(
        policy_payload["maximum_cost_to_gross_edge_ratio"]
    )
    maximum_break_even = Decimal(
        policy_payload["maximum_break_even_win_probability"]
    )
    minimum_expected_net_r = Decimal(policy_payload["minimum_expected_net_r"])
    minimum_conservative_net_r = Decimal(
        policy_payload["minimum_conservative_net_r"]
    )
    minimum_marginal = Decimal(
        policy_payload["minimum_marginal_portfolio_contribution_r"]
    )
    rejection_reasons: list[str] = []
    if expected_gross_profit <= _ZERO:
        rejection_reasons.append("nonpositive_expected_gross_edge")
    if expected_net_profit <= _ZERO or expected_net_r <= minimum_expected_net_r:
        rejection_reasons.append("expected_net_edge_nonpositive_or_below_policy")
    if (
        conservative_net_profit <= _ZERO
        or conservative_net_r <= minimum_conservative_net_r
    ):
        rejection_reasons.append(
            "uncertainty_adjusted_net_edge_nonpositive_or_below_policy"
        )
    if break_even_probability > maximum_break_even:
        rejection_reasons.append("break_even_win_probability_exceeds_policy")
    if (
        cost_to_gross_edge_ratio is None
        or cost_to_gross_edge_ratio > maximum_cost_ratio
    ):
        rejection_reasons.append("cost_consumes_excessive_gross_edge")
    if marginal_contribution < minimum_marginal:
        rejection_reasons.append("marginal_portfolio_contribution_below_policy")

    def text(value: Decimal | None) -> str | None:
        return None if value is None else _decimal_text(value)

    metrics: dict[str, Any] = {
        "proposed_quantity": text(quantity),
        "proposed_notional": text(proposed_notional),
        "entry_estimate": text(entry),
        "limit_price": text(limit_price),
        "stop_price": text(stop),
        "target_price": text(target),
        "maximum_approved_loss": text(maximum_loss),
        "expected_gross_upside": text(gross_upside),
        "expected_downside": text(expected_downside),
        "gross_reward_to_risk": text(gross_reward_to_risk),
        "expected_win_probability": text(win_probability),
        "conservative_win_probability": text(conservative_probability),
        "expected_average_win": text(average_win),
        "expected_average_loss": text(average_loss),
        "expected_gross_profit": text(expected_gross_profit),
        "conservative_gross_profit": text(conservative_gross_profit),
        "expected_execution_cost": text(expected_execution_cost),
        "expected_holding_and_opportunity_cost": text(
            expected_holding_and_opportunity_cost
        ),
        "expected_uncertainty_cost": text(expected_uncertainty_cost),
        "expected_total_cost": text(expected_total_cost),
        "expected_net_profit": text(expected_net_profit),
        "conservative_expected_net_profit": text(conservative_net_profit),
        "expected_net_r": text(expected_net_r),
        "conservative_expected_net_r": text(conservative_net_r),
        "break_even_win_probability_after_costs": text(break_even_probability),
        "expected_capital_efficiency": text(capital_efficiency),
        "expected_capital_efficiency_per_day": text(capital_efficiency_per_day),
        "expected_annualized_capital_efficiency": text(
            annualized_capital_efficiency
        ),
        "expected_profit_per_day": text(expected_profit_per_day),
        "expected_r_per_day": text(expected_r_per_day),
        "cost_to_gross_edge_ratio": text(cost_to_gross_edge_ratio),
        "marginal_portfolio_contribution_r": text(marginal_contribution),
        "worst_reasonable_execution_price": text(limit_price),
        "worst_reasonable_loss": text(worst_reasonable_loss),
        "maximum_loss_headroom": text(maximum_loss_headroom),
        "expected_holding_period_days": text(holding_days),
        "annualization_days": text(annualization_days),
    }
    input_payload = {
        "candidate": candidate_payload,
        "costs": cost_payload,
        "policy": policy_payload,
    }
    input_fingerprint = _fingerprint(input_payload)
    record_payload = {
        **input_payload,
        "metrics": metrics,
        "profitability_eligible": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
        "input_fingerprint": input_fingerprint,
        "formula_version": TRADE_ECONOMICS_FORMULA_VERSION,
        "schema_version": TRADE_ECONOMICS_SCHEMA_VERSION,
    }
    record_fingerprint = _fingerprint(record_payload)
    return TradeEconomicsRecord(
        id=record_fingerprint[:32],
        candidate=candidate_payload,
        costs=cost_payload,
        policy=policy_payload,
        metrics=metrics,
        profitability_eligible=not rejection_reasons,
        rejection_reasons=tuple(rejection_reasons),
        input_fingerprint=input_fingerprint,
        record_fingerprint=record_fingerprint,
    )


def apply_trade_economics_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    """Install immutable candidate economics without rewriting prior records."""

    proposal_columns = {
        row[1] for row in conn.execute('PRAGMA table_info("trade_proposals")')
    }
    if proposal_columns and "trade_economics_id" not in proposal_columns:
        conn.execute("ALTER TABLE trade_proposals ADD COLUMN trade_economics_id TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trade_economics_records(
          id TEXT PRIMARY KEY,
          candidate_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          proposal_id TEXT,
          record_class TEXT NOT NULL,
          asset_class TEXT NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side='buy'),
          action TEXT NOT NULL,
          request_basis TEXT NOT NULL CHECK(request_basis IN ('quantity','notional')),
          strategy_version TEXT NOT NULL,
          strategy_state TEXT NOT NULL,
          setup_type TEXT NOT NULL,
          market_regime TEXT NOT NULL,
          volatility_regime TEXT NOT NULL,
          liquidity_regime TEXT NOT NULL,
          trend_regime TEXT NOT NULL,
          breadth_regime TEXT NOT NULL,
          estimated_at TEXT NOT NULL,
          proposed_quantity TEXT NOT NULL,
          proposed_notional TEXT NOT NULL,
          entry_estimate TEXT NOT NULL,
          limit_price TEXT NOT NULL,
          stop_price TEXT NOT NULL,
          target_price TEXT NOT NULL,
          expected_gross_upside TEXT NOT NULL,
          expected_downside TEXT NOT NULL,
          gross_reward_to_risk TEXT NOT NULL,
          expected_win_probability TEXT NOT NULL,
          conservative_win_probability TEXT NOT NULL,
          expected_average_win TEXT NOT NULL,
          expected_average_loss TEXT NOT NULL,
          expected_gross_profit TEXT NOT NULL,
          expected_execution_cost TEXT NOT NULL,
          expected_holding_and_opportunity_cost TEXT NOT NULL,
          expected_uncertainty_cost TEXT NOT NULL,
          expected_net_profit TEXT NOT NULL,
          conservative_expected_net_profit TEXT NOT NULL,
          expected_net_r TEXT NOT NULL,
          conservative_expected_net_r TEXT NOT NULL,
          break_even_win_probability TEXT NOT NULL,
          expected_total_cost TEXT NOT NULL,
          cost_to_gross_edge_ratio TEXT,
          expected_capital_efficiency TEXT NOT NULL,
          expected_annualized_capital_efficiency TEXT NOT NULL,
          expected_profit_per_day TEXT NOT NULL,
          expected_r_per_day TEXT NOT NULL,
          expected_holding_period_days TEXT NOT NULL,
          worst_reasonable_execution_price TEXT NOT NULL,
          worst_reasonable_loss TEXT NOT NULL,
          maximum_approved_loss TEXT NOT NULL,
          maximum_loss_headroom TEXT NOT NULL,
          marginal_portfolio_contribution_r TEXT NOT NULL,
          profitability_eligible INTEGER NOT NULL
            CHECK(profitability_eligible IN (0,1)),
          rejection_reasons_json TEXT NOT NULL,
          performance_snapshot_id TEXT NOT NULL,
          policy_decision_id TEXT NOT NULL,
          evidence_version TEXT NOT NULL,
          configuration_version TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          formula_versions_json TEXT NOT NULL,
          cost_model_version TEXT NOT NULL,
          estimation_model_version TEXT NOT NULL,
          input_json TEXT NOT NULL,
          costs_json TEXT NOT NULL,
          policy_json TEXT NOT NULL,
          economics_json TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL,
          record_fingerprint TEXT NOT NULL UNIQUE,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_trade_economics_candidate
          ON trade_economics_records(candidate_id,estimated_at);
        CREATE INDEX IF NOT EXISTS idx_trade_economics_proposal
          ON trade_economics_records(proposal_id);
        CREATE INDEX IF NOT EXISTS idx_trade_economics_strategy
          ON trade_economics_records(strategy_version,profitability_eligible,estimated_at);
        """
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                TRADE_ECONOMICS_SCHEMA_VERSION,
                iso_now(),
                "immutable Decimal candidate economics with strategy, policy, configuration, cost, and uncertainty provenance",
            ),
        )


def _costs_from_payload(payload: Mapping[str, Any]) -> TradeEconomicsCosts:
    return TradeEconomicsCosts(
        **{field.name: payload[field.name] for field in fields(TradeEconomicsCosts)}
    )


def _policy_from_payload(payload: Mapping[str, Any]) -> TradeEconomicsPolicy:
    return TradeEconomicsPolicy(
        **{field.name: payload[field.name] for field in fields(TradeEconomicsPolicy)}
    )


def _candidate_from_payload(payload: Mapping[str, Any]) -> TradeEconomicsInput:
    return TradeEconomicsInput(**dict(payload))


def _record_columns(record: TradeEconomicsRecord) -> dict[str, Any]:
    candidate = record.candidate
    metrics = record.metrics
    return {
        "id": record.id,
        "candidate_id": candidate["candidate_id"],
        "run_id": candidate["run_id"],
        "proposal_id": candidate["proposal_id"],
        "record_class": candidate["record_class"],
        "asset_class": candidate["asset_class"],
        "symbol": candidate["symbol"],
        "side": candidate["side"],
        "action": candidate["action"],
        "request_basis": candidate["request_basis"],
        "strategy_version": candidate["strategy_version"],
        "strategy_state": candidate["strategy_state"],
        "setup_type": candidate["setup_type"],
        "market_regime": candidate["market_regime"],
        "volatility_regime": candidate["volatility_regime"],
        "liquidity_regime": candidate["liquidity_regime"],
        "trend_regime": candidate["trend_regime"],
        "breadth_regime": candidate["breadth_regime"],
        "estimated_at": candidate["estimated_at"],
        "proposed_quantity": metrics["proposed_quantity"],
        "proposed_notional": metrics["proposed_notional"],
        "entry_estimate": metrics["entry_estimate"],
        "limit_price": metrics["limit_price"],
        "stop_price": metrics["stop_price"],
        "target_price": metrics["target_price"],
        "expected_gross_upside": metrics["expected_gross_upside"],
        "expected_downside": metrics["expected_downside"],
        "gross_reward_to_risk": metrics["gross_reward_to_risk"],
        "expected_win_probability": metrics["expected_win_probability"],
        "conservative_win_probability": metrics["conservative_win_probability"],
        "expected_average_win": metrics["expected_average_win"],
        "expected_average_loss": metrics["expected_average_loss"],
        "expected_gross_profit": metrics["expected_gross_profit"],
        "expected_execution_cost": metrics["expected_execution_cost"],
        "expected_holding_and_opportunity_cost": metrics[
            "expected_holding_and_opportunity_cost"
        ],
        "expected_uncertainty_cost": metrics["expected_uncertainty_cost"],
        "expected_net_profit": metrics["expected_net_profit"],
        "conservative_expected_net_profit": metrics[
            "conservative_expected_net_profit"
        ],
        "expected_net_r": metrics["expected_net_r"],
        "conservative_expected_net_r": metrics["conservative_expected_net_r"],
        "break_even_win_probability": metrics[
            "break_even_win_probability_after_costs"
        ],
        "expected_total_cost": metrics["expected_total_cost"],
        "cost_to_gross_edge_ratio": metrics["cost_to_gross_edge_ratio"],
        "expected_capital_efficiency": metrics["expected_capital_efficiency"],
        "expected_annualized_capital_efficiency": metrics[
            "expected_annualized_capital_efficiency"
        ],
        "expected_profit_per_day": metrics["expected_profit_per_day"],
        "expected_r_per_day": metrics["expected_r_per_day"],
        "expected_holding_period_days": metrics["expected_holding_period_days"],
        "worst_reasonable_execution_price": metrics[
            "worst_reasonable_execution_price"
        ],
        "worst_reasonable_loss": metrics["worst_reasonable_loss"],
        "maximum_approved_loss": metrics["maximum_approved_loss"],
        "maximum_loss_headroom": metrics["maximum_loss_headroom"],
        "marginal_portfolio_contribution_r": metrics[
            "marginal_portfolio_contribution_r"
        ],
        "profitability_eligible": int(record.profitability_eligible),
        "rejection_reasons_json": _canonical_json(list(record.rejection_reasons)),
        "performance_snapshot_id": candidate["performance_snapshot_id"],
        "policy_decision_id": candidate["policy_decision_id"],
        "evidence_version": candidate["evidence_version"],
        "configuration_version": candidate["configuration_version"],
        "config_hash": candidate["config_hash"],
        "formula_versions_json": _canonical_json(candidate["formula_versions"]),
        "cost_model_version": candidate["cost_model_version"],
        "estimation_model_version": candidate["estimation_model_version"],
        "input_json": _canonical_json(candidate),
        "costs_json": _canonical_json(record.costs),
        "policy_json": _canonical_json(record.policy),
        "economics_json": _canonical_json(record.metrics),
        "input_fingerprint": record.input_fingerprint,
        "record_fingerprint": record.record_fingerprint,
        "formula_version": record.formula_version,
        "schema_version": record.schema_version,
    }


class TradeEconomicsStore:
    """Persist and reload immutable records against their exact durable authority."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    @staticmethod
    def _verify_authority(
        conn: sqlite3.Connection,
        record: TradeEconomicsRecord,
        *,
        link_proposal: bool,
    ) -> None:
        candidate = record.candidate
        snapshot = conn.execute(
            "SELECT * FROM strategy_performance_snapshots WHERE id=?",
            (candidate["performance_snapshot_id"],),
        ).fetchone()
        if snapshot is None:
            raise TradeEconomicsError("performance snapshot authority is missing")
        snapshot = dict(snapshot)
        if (
            snapshot.get("strategy_version") != candidate["strategy_version"]
            or snapshot.get("performance_version")
            != candidate["formula_versions"]["strategy_performance"]
            or snapshot.get("policy_version")
            != candidate["formula_versions"]["strategy_policy"]
            or snapshot.get("schema_version")
            != STRATEGY_PERFORMANCE_SCHEMA_VERSION
            or not snapshot.get("input_fingerprint")
        ):
            raise TradeEconomicsError("performance snapshot authority is inconsistent")

        policy = conn.execute(
            "SELECT * FROM strategy_policy_decisions WHERE id=?",
            (candidate["policy_decision_id"],),
        ).fetchone()
        if policy is None:
            raise TradeEconomicsError("strategy policy authority is missing")
        policy = dict(policy)
        if (
            policy.get("strategy_version") != candidate["strategy_version"]
            or policy.get("performance_snapshot_id")
            != candidate["performance_snapshot_id"]
            or str(policy.get("state") or "").upper() != candidate["strategy_state"]
            or policy.get("performance_version")
            != candidate["formula_versions"]["strategy_performance"]
            or policy.get("policy_version")
            != candidate["formula_versions"]["strategy_policy"]
            or policy.get("schema_version") != STRATEGY_PERFORMANCE_SCHEMA_VERSION
            or policy.get("evidence_version") != candidate["evidence_version"]
            or policy.get("configuration_version")
            != candidate["configuration_version"]
            or policy.get("config_hash") != candidate["config_hash"]
            or policy.get("input_fingerprint") != snapshot.get("input_fingerprint")
            or policy.get("quality_score") != snapshot.get("quality_score")
            or str(snapshot.get("recommendation_state") or "").upper()
            != candidate["strategy_state"]
            or (
                candidate["record_class"] in _PROPOSAL_RECORD_CLASSES
                and policy.get("enforcement_enabled") != 1
            )
        ):
            raise TradeEconomicsError("strategy policy authority is inconsistent")
        try:
            estimated_at = datetime.fromisoformat(candidate["estimated_at"])
            snapshot_as_of = datetime.fromisoformat(
                _utc_timestamp(snapshot.get("as_of"), "performance snapshot as_of")
            )
            policy_decided_at = datetime.fromisoformat(
                _utc_timestamp(policy.get("decided_at"), "strategy policy decided_at")
            )
        except TradeEconomicsError:
            raise
        if snapshot_as_of > estimated_at or policy_decided_at > estimated_at:
            raise TradeEconomicsError(
                "strategy evidence authority contains future information"
            )

        proposal_id = candidate.get("proposal_id")
        if proposal_id is None:
            return
        proposal = conn.execute(
            """SELECT id,symbol,side,strategy_version,payload,performance_snapshot_id,
                      policy_decision_id,trade_economics_id
               FROM trade_proposals WHERE id=?""",
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise TradeEconomicsError("proposal authority is missing")
        proposal = dict(proposal)
        try:
            payload = json.loads(proposal.get("payload") or "{}")
        except json.JSONDecodeError as exc:
            raise TradeEconomicsError("proposal authority payload is invalid") from exc
        if (
            proposal.get("symbol") != candidate["symbol"]
            or str(proposal.get("side") or "").lower() != candidate["side"]
            or proposal.get("strategy_version") != candidate["strategy_version"]
            or proposal.get("performance_snapshot_id")
            != candidate["performance_snapshot_id"]
            or proposal.get("policy_decision_id") != candidate["policy_decision_id"]
            or payload.get("candidate_id") != candidate["candidate_id"]
            or payload.get("config_hash") != candidate["config_hash"]
            or payload.get("performance_snapshot_id")
            != candidate["performance_snapshot_id"]
            or payload.get("policy_decision_id") != candidate["policy_decision_id"]
            or payload.get("formula_versions") != candidate["formula_versions"]
            or payload.get("trade_economics_input_fingerprint")
            != record.input_fingerprint
        ):
            raise TradeEconomicsError("proposal authority is inconsistent")
        existing = proposal.get("trade_economics_id")
        if existing not in (None, "", record.id):
            raise TradeEconomicsError(
                "proposal is already bound to different trade economics"
            )
        if link_proposal:
            updated = conn.execute(
                """UPDATE trade_proposals SET trade_economics_id=?
                   WHERE id=? AND (trade_economics_id IS NULL OR trade_economics_id=''
                                   OR trade_economics_id=?)""",
                (record.id, proposal_id, record.id),
            )
            if updated.rowcount != 1:
                raise TradeEconomicsError("proposal trade economics binding failed")

    @staticmethod
    def _verified_record_from_row(
        conn: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        verify_authority: bool,
    ) -> TradeEconomicsRecord:
        try:
            candidate_payload = json.loads(row["input_json"])
            costs_payload = json.loads(row["costs_json"])
            policy_payload = json.loads(row["policy_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise TradeEconomicsError("persisted trade economics JSON is invalid") from exc
        recomputed = calculate_trade_economics(
            _candidate_from_payload(candidate_payload),
            _costs_from_payload(costs_payload),
            _policy_from_payload(policy_payload),
        )
        expected = _record_columns(recomputed)
        for name, expected_value in expected.items():
            if name == "created_at":
                continue
            if row.get(name) != expected_value:
                raise TradeEconomicsError(
                    f"persisted trade economics column is inconsistent: {name}"
                )
        if verify_authority:
            TradeEconomicsStore._verify_authority(
                conn, recomputed, link_proposal=False
            )
        return recomputed

    def persist(self, record: TradeEconomicsRecord) -> str:
        values = _record_columns(record)
        values["created_at"] = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._verify_authority(conn, record, link_proposal=False)
            columns = tuple(values)
            placeholders = ",".join("?" for _ in columns)
            conn.execute(
                f"""INSERT OR IGNORE INTO trade_economics_records(
                       {",".join(columns)}) VALUES({placeholders})""",
                tuple(values[name] for name in columns),
            )
            row = conn.execute(
                "SELECT * FROM trade_economics_records WHERE id=?", (record.id,)
            ).fetchone()
            if row is None:
                raise TradeEconomicsError("trade economics persistence failed")
            self._verified_record_from_row(
                conn, dict(row), verify_authority=True
            )
            self._verify_authority(conn, record, link_proposal=True)
        return record.id

    def load_verified(
        self, record_id: str, *, verify_authority: bool = True
    ) -> TradeEconomicsRecord:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT * FROM trade_economics_records WHERE id=?",
                (_required_text(record_id, "record_id"),),
            ).fetchone()
            if row is None:
                raise TradeEconomicsError("trade economics record is missing")
            return self._verified_record_from_row(
                conn, dict(row), verify_authority=verify_authority
            )
