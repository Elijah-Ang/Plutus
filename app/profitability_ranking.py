"""Conservative candidate edge estimation and transparent opportunity ranking.

This module turns one current strategy-performance authority and one exact
long-risk candidate into:

1. an immutable ``TradeEconomicsRecord`` with complete expected costs; and
2. an immutable profitability-quality and ranking decision.

All candidate money, price, quantity, probability, ratio, and score values use
``Decimal``.  The service adapter must convert trusted broker/config values
through their decimal string representation before entering this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .formula_versions import (
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    PROFITABILITY_RANKING_FORMULA_VERSION,
    PROFITABILITY_RANKING_SCHEMA_VERSION,
    PROFITABILITY_VALIDATION_FORMULA_VERSION,
    PROFIT_ATTRIBUTION_FORMULA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
    TRADE_ECONOMICS_FORMULA_VERSION,
)
from .strategy_performance import StrategyPerformanceEngine, StrategyRiskPolicy
from .trade_economics import (
    TradeEconomicsCosts,
    TradeEconomicsError,
    TradeEconomicsInput,
    TradeEconomicsPolicy,
    TradeEconomicsRecord,
    TradeEconomicsStore,
    calculate_trade_economics,
)
from .utils import iso_now


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
TRADING_SECONDS_PER_YEAR = Decimal("252") * Decimal("6.5") * Decimal("3600")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_FORMULAS = {
    "evidence": EVIDENCE_VERSION,
    "strategy_performance": STRATEGY_PERFORMANCE_VERSION,
    "strategy_policy": STRATEGY_POLICY_VERSION,
    "trade_economics": TRADE_ECONOMICS_FORMULA_VERSION,
    "profitability_ranking": PROFITABILITY_RANKING_FORMULA_VERSION,
    "profitability_validation": PROFITABILITY_VALIDATION_FORMULA_VERSION,
    "profit_attribution": PROFIT_ATTRIBUTION_FORMULA_VERSION,
}


class ProfitabilityRankingError(ValueError):
    """Raised when candidate profitability evidence is missing or inconsistent."""


def _decimal(
    value: Any,
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
    allow_trusted_float: bool = False,
) -> Decimal:
    if isinstance(value, bool) or (isinstance(value, float) and not allow_trusted_float):
        raise ProfitabilityRankingError(
            f"{name} must use Decimal, an integer, or a decimal string"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProfitabilityRankingError(f"{name} must be a valid decimal") from exc
    if not result.is_finite():
        raise ProfitabilityRankingError(f"{name} must be finite")
    if positive and result <= ZERO:
        raise ProfitabilityRankingError(f"{name} must be positive")
    if minimum is not None and result < minimum:
        raise ProfitabilityRankingError(f"{name} must be at least {_text(minimum)}")
    if maximum is not None and result > maximum:
        raise ProfitabilityRankingError(f"{name} must be at most {_text(maximum)}")
    return ZERO if result == ZERO else result


def _trusted_decimal(
    value: Any,
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    return _decimal(
        value,
        name,
        minimum=minimum,
        maximum=maximum,
        positive=positive,
        allow_trusted_float=True,
    )


def _text(value: Decimal) -> str:
    return format((ZERO if value == ZERO else value).normalize(), "f")


def _required_text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProfitabilityRankingError(f"{name} is required")
    return result


def _timestamp(value: Any, name: str) -> datetime:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfitabilityRankingError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ProfitabilityRankingError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clamp(value: Decimal, lower: Decimal = ZERO, upper: Decimal = ONE) -> Decimal:
    return max(lower, min(upper, value))


def _sqrt(value: Decimal, name: str) -> Decimal:
    if value < ZERO:
        raise ProfitabilityRankingError(f"{name} cannot be negative")
    try:
        return value.sqrt()
    except InvalidOperation as exc:
        raise ProfitabilityRankingError(f"{name} square root is unavailable") from exc


def _formula_versions(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProfitabilityRankingError("formula_versions must be a mapping")
    result = {
        str(key): _required_text(item, f"formula_versions.{key}")
        for key, item in sorted(value.items())
    }
    for key, expected in REQUIRED_FORMULAS.items():
        if result.get(key) != expected:
            raise ProfitabilityRankingError(
                f"formula_versions.{key} must be {expected}"
            )
    return result


@dataclass(frozen=True)
class ProfitabilityCandidateInput:
    candidate_id: str
    run_id: str
    asset_class: str
    symbol: str
    action: str
    strategy_version: str
    strategy_state: str
    setup_type: str
    market_regime: str
    volatility_regime: str
    liquidity_regime: str
    trend_regime: str
    breadth_regime: str
    estimated_at: str
    quote_at: str
    quantity: Any
    entry_estimate: Any
    stop_price: Any
    bid_price: Any
    ask_price: Any
    average_dollar_volume: Any
    annualized_volatility: Any
    setup_score: Any
    symbol_exposure_pct: Any
    cluster_exposure_pct: Any
    maximum_symbol_exposure_pct: Any
    maximum_cluster_exposure_pct: Any
    performance_snapshot_id: str
    policy_decision_id: str
    configuration_version: str
    config_hash: str
    formula_versions: Mapping[str, str]
    proposal_id: str | None = None
    record_class: str = "shadow_candidate"

    def canonical(self, *, quote_max_age_seconds: Decimal) -> dict[str, Any]:
        asset_class = _required_text(self.asset_class, "asset_class").lower()
        if asset_class not in {"equity", "etf"}:
            raise ProfitabilityRankingError(
                "profitability ranking v1 supports equities and ETFs only"
            )
        action = _required_text(self.action, "action").lower()
        if action not in {"entry", "add", "rotation_entry"}:
            raise ProfitabilityRankingError("candidate action is unsupported")
        record_class = _required_text(
            self.record_class, "record_class"
        ).lower()
        if record_class not in {"shadow_candidate", "proposal_candidate"}:
            raise ProfitabilityRankingError(
                "profitability record_class is unsupported"
            )
        proposal_id = (
            str(self.proposal_id).strip()
            if self.proposal_id is not None
            else None
        )
        if proposal_id == "":
            proposal_id = None
        if record_class == "proposal_candidate" and proposal_id is None:
            raise ProfitabilityRankingError(
                "proposal_id is required for proposal profitability"
            )
        if record_class == "shadow_candidate" and proposal_id is not None:
            raise ProfitabilityRankingError(
                "proposal_id is forbidden for shadow profitability"
            )
        configuration_version = _required_text(
            self.configuration_version, "configuration_version"
        )
        if configuration_version != CONFIGURATION_SCHEMA_VERSION:
            raise ProfitabilityRankingError("configuration_version is not current")
        config_hash = _required_text(self.config_hash, "config_hash").lower()
        if not SHA256.fullmatch(config_hash):
            raise ProfitabilityRankingError("config_hash must be a SHA-256 digest")
        estimated_at = _timestamp(self.estimated_at, "estimated_at")
        quote_at = _timestamp(self.quote_at, "quote_at")
        quote_age = Decimal(str((estimated_at - quote_at).total_seconds()))
        if quote_age < Decimal("-5"):
            raise ProfitabilityRankingError("quote evidence is from the future")
        if quote_age > quote_max_age_seconds:
            raise ProfitabilityRankingError("quote evidence is stale")

        quantity = _decimal(self.quantity, "quantity", positive=True)
        entry = _decimal(self.entry_estimate, "entry_estimate", positive=True)
        stop = _decimal(self.stop_price, "stop_price", positive=True)
        bid = _decimal(self.bid_price, "bid_price", positive=True)
        ask = _decimal(self.ask_price, "ask_price", positive=True)
        if stop >= entry:
            raise ProfitabilityRankingError("stop_price must be below entry_estimate")
        if ask < bid:
            raise ProfitabilityRankingError("ask_price cannot be below bid_price")
        return {
            "candidate_id": _required_text(self.candidate_id, "candidate_id"),
            "run_id": _required_text(self.run_id, "run_id"),
            "proposal_id": proposal_id,
            "record_class": record_class,
            "asset_class": asset_class,
            "symbol": _required_text(self.symbol, "symbol").upper(),
            "action": action,
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
            "estimated_at": estimated_at.isoformat(),
            "quote_at": quote_at.isoformat(),
            "quote_age_seconds": _text(max(ZERO, quote_age)),
            "quantity": _text(quantity),
            "entry_estimate": _text(entry),
            "stop_price": _text(stop),
            "bid_price": _text(bid),
            "ask_price": _text(ask),
            "average_dollar_volume": _text(
                _decimal(
                    self.average_dollar_volume,
                    "average_dollar_volume",
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
            "setup_score": _text(
                _decimal(
                    self.setup_score,
                    "setup_score",
                    minimum=ZERO,
                    maximum=Decimal("100"),
                )
            ),
            "symbol_exposure_pct": _text(
                _decimal(
                    self.symbol_exposure_pct,
                    "symbol_exposure_pct",
                    minimum=ZERO,
                )
            ),
            "cluster_exposure_pct": _text(
                _decimal(
                    self.cluster_exposure_pct,
                    "cluster_exposure_pct",
                    minimum=ZERO,
                )
            ),
            "maximum_symbol_exposure_pct": _text(
                _decimal(
                    self.maximum_symbol_exposure_pct,
                    "maximum_symbol_exposure_pct",
                    positive=True,
                )
            ),
            "maximum_cluster_exposure_pct": _text(
                _decimal(
                    self.maximum_cluster_exposure_pct,
                    "maximum_cluster_exposure_pct",
                    positive=True,
                )
            ),
            "performance_snapshot_id": _required_text(
                self.performance_snapshot_id, "performance_snapshot_id"
            ),
            "policy_decision_id": _required_text(
                self.policy_decision_id, "policy_decision_id"
            ),
            "configuration_version": configuration_version,
            "config_hash": config_hash,
            "formula_versions": _formula_versions(self.formula_versions),
        }


@dataclass(frozen=True)
class CandidateProfitabilityDecision:
    id: str
    economics: TradeEconomicsRecord
    score_context: Mapping[str, str]
    quality_components: Mapping[str, str]
    profitability_quality_score: str
    ranking_score: str
    ranking_key: tuple[str, ...]
    profitability_eligible: bool
    rejection_reasons: tuple[str, ...]
    input_fingerprint: str
    decision_fingerprint: str
    formula_version: str = PROFITABILITY_RANKING_FORMULA_VERSION
    schema_version: str = PROFITABILITY_RANKING_SCHEMA_VERSION

    def summary(self) -> dict[str, Any]:
        return {
            "profitability_decision_id": self.id,
            "trade_economics_id": self.economics.id,
            "trade_economics_record_class": self.economics.candidate[
                "record_class"
            ],
            "trade_economics_input_fingerprint": self.economics.input_fingerprint,
            "trade_economics_record_fingerprint": self.economics.record_fingerprint,
            "profitability_eligible": self.profitability_eligible,
            "profitability_rejection_reasons": list(self.rejection_reasons),
            "profitability_quality_score": self.profitability_quality_score,
            "profitability_quality_components": dict(self.quality_components),
            "profitability_ranking_score": self.ranking_score,
            "profitability_ranking_key": list(self.ranking_key),
            "profitability_metrics": dict(self.economics.metrics),
            "profitability_input_fingerprint": self.input_fingerprint,
            "profitability_decision_fingerprint": self.decision_fingerprint,
            "profitability_formula_version": self.formula_version,
            "profitability_schema_version": self.schema_version,
        }


def _policy_metric(
    metrics: Mapping[str, Any],
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
    required: bool = True,
) -> Decimal | None:
    value = metrics.get(name)
    if value is None and not required:
        return None
    return _trusted_decimal(
        value,
        f"strategy_metrics.{name}",
        minimum=minimum,
        maximum=maximum,
        positive=positive,
    )


def _model_decimal(
    model: Mapping[str, Any],
    name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    return _trusted_decimal(
        model.get(name),
        f"candidate_model.{name}",
        minimum=minimum,
        maximum=maximum,
        positive=positive,
    )


def _score_decision(
    economics: TradeEconomicsRecord,
    context: Mapping[str, Any],
) -> CandidateProfitabilityDecision:
    canonical_context = {
        "setup_score": _text(
            _decimal(
                context.get("setup_score"),
                "score_context.setup_score",
                minimum=ZERO,
                maximum=Decimal("100"),
            )
        ),
        "policy_quality_score": _text(
            _decimal(
                context.get("policy_quality_score"),
                "score_context.policy_quality_score",
                minimum=ZERO,
                maximum=Decimal("100"),
            )
        ),
        "evidence_sample_count": _text(
            _decimal(
                context.get("evidence_sample_count"),
                "score_context.evidence_sample_count",
                minimum=ZERO,
            )
        ),
        "prior_sample_count": _text(
            _decimal(
                context.get("prior_sample_count"),
                "score_context.prior_sample_count",
                positive=True,
            )
        ),
        "positive_regime_ratio": _text(
            _decimal(
                context.get("positive_regime_ratio"),
                "score_context.positive_regime_ratio",
                minimum=ZERO,
                maximum=ONE,
            )
        ),
        "maximum_drawdown_r": _text(
            _decimal(
                context.get("maximum_drawdown_r"),
                "score_context.maximum_drawdown_r",
                minimum=ZERO,
            )
        ),
        "hard_max_drawdown_r": _text(
            _decimal(
                context.get("hard_max_drawdown_r"),
                "score_context.hard_max_drawdown_r",
                positive=True,
            )
        ),
        "diversification_factor": _text(
            _decimal(
                context.get("diversification_factor"),
                "score_context.diversification_factor",
                minimum=ZERO,
                maximum=ONE,
            )
        ),
        "observed_quote_spread_bps": _text(
            _decimal(
                context.get("observed_quote_spread_bps"),
                "score_context.observed_quote_spread_bps",
                minimum=ZERO,
            )
        ),
        "maximum_quote_spread_bps": _text(
            _decimal(
                context.get("maximum_quote_spread_bps"),
                "score_context.maximum_quote_spread_bps",
                positive=True,
            )
        ),
        "target_expected_net_r": _text(
            _decimal(
                context.get("target_expected_net_r"),
                "score_context.target_expected_net_r",
                positive=True,
            )
        ),
        "target_conservative_net_r": _text(
            _decimal(
                context.get("target_conservative_net_r"),
                "score_context.target_conservative_net_r",
                positive=True,
            )
        ),
        "target_expected_r_per_day": _text(
            _decimal(
                context.get("target_expected_r_per_day"),
                "score_context.target_expected_r_per_day",
                positive=True,
            )
        ),
    }
    metrics = economics.metrics
    expected_net_r = Decimal(metrics["expected_net_r"])
    conservative_net_r = Decimal(metrics["conservative_expected_net_r"])
    expected_r_per_day = Decimal(metrics["expected_r_per_day"])
    gross_reward_to_risk = Decimal(metrics["gross_reward_to_risk"])
    marginal = Decimal(metrics["marginal_portfolio_contribution_r"])
    cost_ratio = (
        None
        if metrics["cost_to_gross_edge_ratio"] is None
        else Decimal(metrics["cost_to_gross_edge_ratio"])
    )
    policy_max_cost = Decimal(
        economics.policy["maximum_cost_to_gross_edge_ratio"]
    )
    evidence_count = Decimal(canonical_context["evidence_sample_count"])
    prior_count = Decimal(canonical_context["prior_sample_count"])
    drawdown = Decimal(canonical_context["maximum_drawdown_r"])
    hard_drawdown = Decimal(canonical_context["hard_max_drawdown_r"])

    components = {
        "uncertainty_adjusted_net_expectancy": _clamp(
            conservative_net_r
            / Decimal(canonical_context["target_conservative_net_r"])
        ),
        "expected_net_expectancy": _clamp(
            expected_net_r / Decimal(canonical_context["target_expected_net_r"])
        ),
        "execution_cost_resilience": (
            ZERO
            if cost_ratio is None
            else _clamp(ONE - cost_ratio / policy_max_cost)
        ),
        "holding_period_efficiency": _clamp(
            expected_r_per_day
            / Decimal(canonical_context["target_expected_r_per_day"])
        ),
        "evidence_maturity": evidence_count / (evidence_count + prior_count),
        "strategy_evidence_quality": (
            Decimal(canonical_context["policy_quality_score"]) / Decimal("100")
        ),
        "payoff_asymmetry": _clamp(gross_reward_to_risk / Decimal("3")),
        "regime_breadth": Decimal(canonical_context["positive_regime_ratio"]),
        "drawdown_resilience": _clamp(ONE - drawdown / hard_drawdown),
        "portfolio_diversification": Decimal(
            canonical_context["diversification_factor"]
        ),
    }
    weights = {
        "uncertainty_adjusted_net_expectancy": Decimal("0.25"),
        "expected_net_expectancy": Decimal("0.15"),
        "execution_cost_resilience": Decimal("0.10"),
        "holding_period_efficiency": Decimal("0.10"),
        "evidence_maturity": Decimal("0.10"),
        "strategy_evidence_quality": Decimal("0.10"),
        "payoff_asymmetry": Decimal("0.07"),
        "regime_breadth": Decimal("0.05"),
        "drawdown_resilience": Decimal("0.04"),
        "portfolio_diversification": Decimal("0.04"),
    }
    quality = sum(
        (components[name] * weight for name, weight in weights.items()), ZERO
    ) * Decimal("100")
    rejection_reasons = list(economics.rejection_reasons)
    if (
        Decimal(canonical_context["observed_quote_spread_bps"])
        > Decimal(canonical_context["maximum_quote_spread_bps"])
    ):
        rejection_reasons.append("quote_spread_exceeds_execution_policy")
    eligible = not rejection_reasons
    ranking_score = quality if eligible else ZERO
    ranking_key = (
        "1" if eligible else "0",
        metrics["conservative_expected_net_r"],
        metrics["expected_net_r"],
        metrics["expected_r_per_day"],
        metrics["expected_capital_efficiency"],
        metrics["marginal_portfolio_contribution_r"],
        _text(components["execution_cost_resilience"]),
        _text(quality),
        canonical_context["setup_score"],
    )
    component_payload = {
        name: _text(value * Decimal("100"))
        for name, value in sorted(components.items())
    }
    input_payload = {
        "economics_id": economics.id,
        "economics_record_fingerprint": economics.record_fingerprint,
        "score_context": canonical_context,
        "weights": {name: _text(value) for name, value in sorted(weights.items())},
    }
    input_fingerprint = _fingerprint(input_payload)
    decision_payload = {
        **input_payload,
        "quality_components": component_payload,
        "profitability_quality_score": _text(quality),
        "ranking_score": _text(ranking_score),
        "ranking_key": list(ranking_key),
        "profitability_eligible": eligible,
        "rejection_reasons": rejection_reasons,
        "input_fingerprint": input_fingerprint,
        "formula_version": PROFITABILITY_RANKING_FORMULA_VERSION,
        "schema_version": PROFITABILITY_RANKING_SCHEMA_VERSION,
    }
    decision_fingerprint = _fingerprint(decision_payload)
    return CandidateProfitabilityDecision(
        id=decision_fingerprint[:32],
        economics=economics,
        score_context=canonical_context,
        quality_components=component_payload,
        profitability_quality_score=_text(quality),
        ranking_score=_text(ranking_score),
        ranking_key=ranking_key,
        profitability_eligible=eligible,
        rejection_reasons=tuple(rejection_reasons),
        input_fingerprint=input_fingerprint,
        decision_fingerprint=decision_fingerprint,
    )


def calculate_candidate_profitability(
    candidate: ProfitabilityCandidateInput,
    policy: StrategyRiskPolicy,
    config: Mapping[str, Any],
) -> CandidateProfitabilityDecision:
    """Estimate and rank one exact candidate from current gross-R evidence."""

    profitability = config.get("profitability_engine") or {}
    model = profitability.get("candidate_model") or {}
    quote_max_age = _model_decimal(
        model, "quote_max_age_seconds", positive=True
    )
    data = candidate.canonical(quote_max_age_seconds=quote_max_age)
    if (
        not policy.id
        or policy.id != data["policy_decision_id"]
        or policy.performance_snapshot_id != data["performance_snapshot_id"]
        or policy.strategy_version != data["strategy_version"]
        or policy.state != data["strategy_state"]
        or policy.performance_version != STRATEGY_PERFORMANCE_VERSION
        or policy.policy_version != STRATEGY_POLICY_VERSION
        or policy.evidence_version != EVIDENCE_VERSION
        or policy.configuration_version != CONFIGURATION_SCHEMA_VERSION
        or policy.config_hash != data["config_hash"]
        or policy.enforcement_enabled is not True
    ):
        raise ProfitabilityRankingError(
            "strategy policy authority does not match the candidate"
        )
    if policy.state not in {"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"}:
        raise ProfitabilityRankingError(
            "strategy policy does not authorize candidate profitability"
        )
    metrics = policy.metrics
    gross_count = _policy_metric(metrics, "gross_sample_count", minimum=ZERO)
    win_count = _policy_metric(metrics, "gross_win_count", minimum=ZERO)
    loss_count = _policy_metric(metrics, "gross_loss_count", minimum=ZERO)
    average_win_r = _policy_metric(metrics, "average_gross_win_r", positive=True)
    average_loss_r = _policy_metric(metrics, "average_gross_loss_r")
    if (
        gross_count is None
        or win_count is None
        or loss_count is None
        or average_win_r is None
        or average_loss_r is None
        or average_loss_r >= ZERO
        or win_count + loss_count <= ZERO
        or win_count + loss_count > gross_count
    ):
        raise ProfitabilityRankingError(
            "complete gross win/loss strategy evidence is unavailable"
        )

    neutral_probability = _model_decimal(
        model, "neutral_win_probability", minimum=ZERO, maximum=ONE
    )
    prior_win_samples = _model_decimal(
        model, "prior_win_samples", positive=True
    )
    prior_payoff_samples = _model_decimal(
        model, "prior_payoff_samples", positive=True
    )
    posterior_z = _model_decimal(model, "posterior_z", positive=True)
    # The posterior estimates the probability of a strictly positive gross
    # outcome. Flat outcomes are therefore non-wins rather than disappearing
    # from the denominator.
    nonwin_count = gross_count - win_count
    alpha = win_count + prior_win_samples * neutral_probability
    beta = nonwin_count + prior_win_samples * (ONE - neutral_probability)
    posterior_total = alpha + beta
    posterior_probability = alpha / posterior_total
    posterior_variance = (
        alpha
        * beta
        / (posterior_total * posterior_total * (posterior_total + ONE))
    )
    conservative_probability = max(
        ZERO,
        posterior_probability
        - posterior_z * _sqrt(posterior_variance, "posterior variance"),
    )
    prior_average_win_r = _model_decimal(
        model, "prior_average_win_r", positive=True
    )
    prior_average_loss_r = _model_decimal(
        model, "prior_average_loss_r", positive=True
    )
    shrunk_average_win_r = (
        average_win_r * win_count + prior_average_win_r * prior_payoff_samples
    ) / (win_count + prior_payoff_samples)
    shrunk_average_loss_r = (
        abs(average_loss_r) * loss_count
        + prior_average_loss_r * prior_payoff_samples
    ) / (loss_count + prior_payoff_samples)
    maximum_target_r = _model_decimal(model, "maximum_target_r", positive=True)
    target_r = min(maximum_target_r, shrunk_average_win_r)
    expected_loss_r = min(ONE, shrunk_average_loss_r)

    quantity = Decimal(data["quantity"])
    entry = Decimal(data["entry_estimate"])
    stop = Decimal(data["stop_price"])
    bid = Decimal(data["bid_price"])
    ask = Decimal(data["ask_price"])
    notional = quantity * entry
    downside = quantity * (entry - stop)
    target = entry + (entry - stop) * target_r
    limit_price = max(entry, ask)
    if limit_price >= target:
        raise ProfitabilityRankingError(
            "current ask leaves no valid target asymmetry"
        )
    expected_average_win = quantity * (target - entry)
    expected_average_loss = downside * expected_loss_r
    average_dollar_volume = Decimal(data["average_dollar_volume"])
    participation = notional / average_dollar_volume
    market_impact_bps = _model_decimal(
        model, "market_impact_floor_bps", minimum=ZERO
    ) + _model_decimal(
        model, "market_impact_coefficient_bps", minimum=ZERO
    ) * _sqrt(participation, "market participation")

    spread = quantity * (ask - bid)
    slippage = notional * _model_decimal(
        model, "expected_slippage_bps", minimum=ZERO
    ) / BPS
    historical_shortfall = _policy_metric(
        metrics,
        "median_absolute_implementation_shortfall_bps",
        minimum=ZERO,
        required=False,
    )
    shortfall_bps = (
        historical_shortfall
        if historical_shortfall is not None
        else _model_decimal(
            model, "default_implementation_shortfall_bps", minimum=ZERO
        )
    )
    implementation_shortfall = notional * shortfall_bps / BPS
    market_impact = notional * market_impact_bps / BPS
    adverse_selection = notional * _model_decimal(
        model, "adverse_selection_bps", minimum=ZERO
    ) / BPS
    rejected_or_missed_fill = notional * _model_decimal(
        model, "missed_fill_cost_bps", minimum=ZERO
    ) / BPS
    opportunity = notional * _model_decimal(
        model, "opportunity_cost_bps", minimum=ZERO
    ) / BPS
    annualized_volatility = Decimal(data["annualized_volatility"])
    approval_delay_seconds = _model_decimal(
        model, "approval_delay_seconds", minimum=ZERO
    )
    approval_delay = (
        notional
        * annualized_volatility
        * _sqrt(
            approval_delay_seconds / TRADING_SECONDS_PER_YEAR,
            "approval-delay fraction",
        )
        * _model_decimal(
            model, "approval_delay_volatility_multiplier", minimum=ZERO
        )
    )
    holding_days = _policy_metric(
        metrics, "average_holding_period_days", positive=True, required=False
    )
    if holding_days is None:
        holding_days = _trusted_decimal(
            profitability.get("primary_horizon_sessions"),
            "profitability_engine.primary_horizon_sessions",
            positive=True,
        )
    annualization_days = _model_decimal(
        model, "annualization_days", positive=True
    )
    holding = (
        notional
        * _model_decimal(
            model, "holding_opportunity_rate_annual", minimum=ZERO
        )
        * holding_days
        / annualization_days
    )
    model_uncertainty = notional * _model_decimal(
        model, "model_uncertainty_bps", minimum=ZERO
    ) / BPS
    estimation_uncertainty = notional * _model_decimal(
        model, "estimation_uncertainty_bps", minimum=ZERO
    ) / BPS
    # Section 31 is assessed on the covered sale value. Use the displayed
    # target sale notional rather than entry notional so profitable exits do
    # not understate the regulatory drag.
    sec_fee = quantity * target * _model_decimal(
        model, "sec_sell_fee_rate", minimum=ZERO
    )
    taf_fee = min(
        quantity
        * _model_decimal(model, "finra_taf_per_share", minimum=ZERO),
        _model_decimal(model, "finra_taf_max", minimum=ZERO),
    )
    cat_fee = (
        quantity
        * _model_decimal(model, "cat_fee_per_share_per_side", minimum=ZERO)
        * Decimal("2")
    )
    regulatory = sec_fee + taf_fee + cat_fee
    worst_additional = notional * _model_decimal(
        model, "worst_additional_cost_bps", minimum=ZERO
    ) / BPS
    expected_total_cost = sum(
        (
            spread,
            slippage,
            regulatory,
            market_impact,
            implementation_shortfall,
            adverse_selection,
            rejected_or_missed_fill,
            opportunity,
            approval_delay,
            holding,
            model_uncertainty,
            estimation_uncertainty,
        ),
        ZERO,
    )

    symbol_ratio = Decimal(data["symbol_exposure_pct"]) / Decimal(
        data["maximum_symbol_exposure_pct"]
    )
    cluster_ratio = Decimal(data["cluster_exposure_pct"]) / Decimal(
        data["maximum_cluster_exposure_pct"]
    )
    diversification_factor = _clamp(ONE - max(symbol_ratio, cluster_ratio))
    conservative_gross_r = (
        conservative_probability * target_r
        - (ONE - conservative_probability) * expected_loss_r
    )
    conservative_net_r_before_portfolio = (
        conservative_gross_r - expected_total_cost / downside
    )
    marginal_contribution = (
        conservative_net_r_before_portfolio * diversification_factor
    )

    costs = TradeEconomicsCosts(
        spread=spread,
        slippage=slippage,
        fees=ZERO,
        regulatory=regulatory,
        crypto_transaction=ZERO,
        market_impact=market_impact,
        implementation_shortfall=implementation_shortfall,
        adverse_selection=adverse_selection,
        rejected_or_missed_fill=rejected_or_missed_fill,
        opportunity=opportunity,
        approval_delay=approval_delay,
        holding=holding,
        model_uncertainty=model_uncertainty,
        estimation_uncertainty=estimation_uncertainty,
        worst_reasonable_additional_cost=worst_additional,
    )
    cost_payload = costs.canonical()
    expected_total_cost = sum(
        (
            Decimal(value)
            for name, value in cost_payload.items()
            if name != "worst_reasonable_additional_cost"
        ),
        ZERO,
    )
    maximum_approved_loss = (
        quantity * (limit_price - stop)
        + expected_total_cost
        + worst_additional
    )
    formulas = data["formula_versions"]
    trade_input = TradeEconomicsInput(
        candidate_id=data["candidate_id"],
        run_id=data["run_id"],
        proposal_id=data["proposal_id"],
        record_class=data["record_class"],
        asset_class=data["asset_class"],
        symbol=data["symbol"],
        side="buy",
        action=data["action"],
        request_basis="quantity",
        strategy_version=data["strategy_version"],
        strategy_state=data["strategy_state"],
        setup_type=data["setup_type"],
        market_regime=data["market_regime"],
        volatility_regime=data["volatility_regime"],
        liquidity_regime=data["liquidity_regime"],
        trend_regime=data["trend_regime"],
        breadth_regime=data["breadth_regime"],
        estimated_at=data["estimated_at"],
        quantity=quantity,
        proposed_notional=notional,
        entry_estimate=entry,
        limit_price=limit_price,
        stop_price=stop,
        target_price=target,
        maximum_approved_loss=maximum_approved_loss,
        expected_win_probability=posterior_probability,
        conservative_win_probability=conservative_probability,
        expected_average_win=expected_average_win,
        expected_average_loss=expected_average_loss,
        expected_holding_period_days=holding_days,
        annualization_days=annualization_days,
        marginal_portfolio_contribution_r=marginal_contribution,
        performance_snapshot_id=data["performance_snapshot_id"],
        policy_decision_id=data["policy_decision_id"],
        evidence_version=EVIDENCE_VERSION,
        configuration_version=data["configuration_version"],
        config_hash=data["config_hash"],
        formula_versions=formulas,
        cost_model_version=_required_text(
            model.get("cost_model_version"), "candidate_model.cost_model_version"
        ),
        estimation_model_version=_required_text(
            model.get("estimation_model_version"),
            "candidate_model.estimation_model_version",
        ),
    )
    economics_policy = TradeEconomicsPolicy(
        maximum_cost_to_gross_edge_ratio=_trusted_decimal(
            profitability.get("maximum_cost_to_gross_edge_ratio"),
            "profitability_engine.maximum_cost_to_gross_edge_ratio",
            minimum=ZERO,
        ),
        maximum_break_even_win_probability=_trusted_decimal(
            profitability.get("maximum_break_even_win_probability"),
            "profitability_engine.maximum_break_even_win_probability",
            minimum=ZERO,
            maximum=ONE,
        ),
        minimum_expected_net_r=_trusted_decimal(
            profitability.get("minimum_expected_net_r"),
            "profitability_engine.minimum_expected_net_r",
        ),
        minimum_conservative_net_r=_trusted_decimal(
            profitability.get("minimum_conservative_net_r"),
            "profitability_engine.minimum_conservative_net_r",
        ),
        minimum_marginal_portfolio_contribution_r=ZERO,
    )
    try:
        economics = calculate_trade_economics(
            trade_input, costs, economics_policy
        )
    except TradeEconomicsError as exc:
        raise ProfitabilityRankingError(str(exc)) from exc
    observed_spread_bps = (ask - bid) / ((ask + bid) / Decimal("2")) * BPS
    positive_regime_ratio = _policy_metric(
        metrics, "positive_regime_ratio", minimum=ZERO, maximum=ONE, required=False
    )
    maximum_drawdown_r = _policy_metric(
        metrics, "maximum_drawdown_r", minimum=ZERO, required=False
    )
    score_context = {
        "setup_score": data["setup_score"],
        "policy_quality_score": _text(
            _trusted_decimal(
                policy.quality_score,
                "policy.quality_score",
                minimum=ZERO,
                maximum=Decimal("100"),
            )
        ),
        "evidence_sample_count": _text(gross_count),
        "prior_sample_count": _text(prior_win_samples),
        "positive_regime_ratio": _text(
            positive_regime_ratio if positive_regime_ratio is not None else ZERO
        ),
        "maximum_drawdown_r": _text(
            maximum_drawdown_r if maximum_drawdown_r is not None else _trusted_decimal(
                profitability.get("hard_max_drawdown_r"),
                "profitability_engine.hard_max_drawdown_r",
                positive=True,
            )
        ),
        "hard_max_drawdown_r": _text(
            _trusted_decimal(
                profitability.get("hard_max_drawdown_r"),
                "profitability_engine.hard_max_drawdown_r",
                positive=True,
            )
        ),
        "diversification_factor": _text(diversification_factor),
        "observed_quote_spread_bps": _text(observed_spread_bps),
        "maximum_quote_spread_bps": _text(
            _trusted_decimal(
                (config.get("quotes") or {}).get("max_spread_bps"),
                "quotes.max_spread_bps",
                positive=True,
            )
        ),
        "target_expected_net_r": _text(
            _trusted_decimal(
                profitability.get("target_expectancy_r"),
                "profitability_engine.target_expectancy_r",
                positive=True,
            )
        ),
        "target_conservative_net_r": "0.15",
        "target_expected_r_per_day": "0.02",
    }
    return _score_decision(economics, score_context)


def apply_profitability_ranking_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_profitability_decisions(
          id TEXT PRIMARY KEY,
          trade_economics_id TEXT NOT NULL UNIQUE,
          candidate_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          strategy_version TEXT NOT NULL,
          performance_snapshot_id TEXT NOT NULL,
          policy_decision_id TEXT NOT NULL,
          profitability_eligible INTEGER NOT NULL
            CHECK(profitability_eligible IN (0,1)),
          rejection_reasons_json TEXT NOT NULL,
          profitability_quality_score TEXT NOT NULL,
          quality_components_json TEXT NOT NULL,
          ranking_score TEXT NOT NULL,
          ranking_key_json TEXT NOT NULL,
          score_context_json TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          formula_versions_json TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL,
          decision_fingerprint TEXT NOT NULL UNIQUE,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_profitability_decisions_run_rank
          ON candidate_profitability_decisions(
            run_id,profitability_eligible,profitability_quality_score,symbol);
        CREATE INDEX IF NOT EXISTS idx_profitability_decisions_candidate
          ON candidate_profitability_decisions(candidate_id,created_at);
        """
    )
    if record_migration:
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail)
               VALUES(?,?,?)""",
            (
                PROFITABILITY_RANKING_SCHEMA_VERSION,
                iso_now(),
                "immutable uncertainty-adjusted candidate profitability quality and ranking decisions",
            ),
        )


def _decision_columns(decision: CandidateProfitabilityDecision) -> dict[str, Any]:
    candidate = decision.economics.candidate
    return {
        "id": decision.id,
        "trade_economics_id": decision.economics.id,
        "candidate_id": candidate["candidate_id"],
        "run_id": candidate["run_id"],
        "symbol": candidate["symbol"],
        "strategy_version": candidate["strategy_version"],
        "performance_snapshot_id": candidate["performance_snapshot_id"],
        "policy_decision_id": candidate["policy_decision_id"],
        "profitability_eligible": int(decision.profitability_eligible),
        "rejection_reasons_json": _canonical_json(
            list(decision.rejection_reasons)
        ),
        "profitability_quality_score": decision.profitability_quality_score,
        "quality_components_json": _canonical_json(
            dict(decision.quality_components)
        ),
        "ranking_score": decision.ranking_score,
        "ranking_key_json": _canonical_json(list(decision.ranking_key)),
        "score_context_json": _canonical_json(dict(decision.score_context)),
        "config_hash": candidate["config_hash"],
        "formula_versions_json": _canonical_json(candidate["formula_versions"]),
        "input_fingerprint": decision.input_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "formula_version": decision.formula_version,
        "schema_version": decision.schema_version,
    }


class CandidateProfitabilityStore:
    """Persist and reload exact profitability decisions without mutable updates."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def _persist_in_connection(
        self,
        conn: sqlite3.Connection,
        decision: CandidateProfitabilityDecision,
    ) -> str:
        TradeEconomicsStore(self.storage)._persist_in_connection(
            conn, decision.economics
        )
        values = _decision_columns(decision)
        values["created_at"] = iso_now()
        columns = tuple(values)
        conn.execute(
            f"""INSERT OR IGNORE INTO candidate_profitability_decisions(
                   {",".join(columns)}) VALUES({",".join("?" for _ in columns)})""",
            tuple(values[name] for name in columns),
        )
        row = conn.execute(
            "SELECT * FROM candidate_profitability_decisions WHERE id=?",
            (decision.id,),
        ).fetchone()
        if row is None:
            raise ProfitabilityRankingError(
                "candidate profitability persistence failed"
            )
        self._verify_row(dict(row), decision)
        return decision.id

    def persist(
        self,
        decision: CandidateProfitabilityDecision,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        if conn is not None:
            if not conn.in_transaction:
                raise ProfitabilityRankingError(
                    "external profitability persistence requires an active transaction"
                )
            return self._persist_in_connection(conn, decision)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._persist_in_connection(conn, decision)

    @staticmethod
    def _verify_row(
        row: Mapping[str, Any], expected: CandidateProfitabilityDecision
    ) -> None:
        columns = _decision_columns(expected)
        for name, value in columns.items():
            if row.get(name) != value:
                raise ProfitabilityRankingError(
                    f"persisted profitability decision is inconsistent: {name}"
                )

    def load_verified(self, decision_id: str) -> CandidateProfitabilityDecision:
        rows = self.storage.fetch_all(
            "SELECT * FROM candidate_profitability_decisions WHERE id=?",
            (_required_text(decision_id, "decision_id"),),
        )
        if not rows:
            raise ProfitabilityRankingError(
                "candidate profitability decision is missing"
            )
        row = rows[0]
        economics = TradeEconomicsStore(self.storage).load_verified(
            str(row["trade_economics_id"])
        )
        try:
            context = json.loads(row["score_context_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfitabilityRankingError(
                "persisted profitability score context is invalid"
            ) from exc
        recomputed = _score_decision(economics, context)
        self._verify_row(row, recomputed)
        return recomputed


class CandidateProfitabilityEngine:
    """Load exact durable strategy authority, calculate, and persist a decision."""

    def __init__(self, storage: Any, config: Mapping[str, Any]) -> None:
        self.storage = storage
        self.config = config

    def evaluate(
        self, candidate: ProfitabilityCandidateInput
    ) -> CandidateProfitabilityDecision:
        policy = StrategyPerformanceEngine(
            self.storage, dict(self.config)
        ).policy_by_id(candidate.policy_decision_id)
        if policy is None:
            raise ProfitabilityRankingError(
                "current strategy policy authority is unavailable"
            )
        decision = calculate_candidate_profitability(
            candidate, policy, self.config
        )
        CandidateProfitabilityStore(self.storage).persist(decision)
        return decision


__all__ = [
    "CandidateProfitabilityDecision",
    "CandidateProfitabilityEngine",
    "CandidateProfitabilityStore",
    "ProfitabilityCandidateInput",
    "ProfitabilityRankingError",
    "apply_profitability_ranking_schema",
    "calculate_candidate_profitability",
]
