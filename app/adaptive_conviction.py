"""Deterministic, report-only adaptive-conviction diagnostics.

This module never sizes a proposal, approves an action, creates a reservation,
or talks to a broker.  It produces replayable recommendations for a possible
future sizing system while canonical Phase 3/4 sizing remains authoritative.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .formula_versions import (
    ADAPTIVE_CONVICTION_FORMULA_VERSION,
    ADAPTIVE_CONVICTION_SCHEMA_VERSION,
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
)
from .utils import iso_now, json_dumps


DEPLOYMENT_MODES: dict[str, dict[str, float]] = {
    "DEFENSIVE": {"trade_risk_cap_pct": 0.15, "portfolio_heat_target_pct": 0.50, "gross_exposure_target_pct": 20.0},
    "NORMAL": {"trade_risk_cap_pct": 0.20, "portfolio_heat_target_pct": 1.25, "gross_exposure_target_pct": 30.0},
    "OPPORTUNISTIC": {"trade_risk_cap_pct": 0.30, "portfolio_heat_target_pct": 1.50, "gross_exposure_target_pct": 40.0},
    "AGGRESSIVE": {"trade_risk_cap_pct": 0.35, "portfolio_heat_target_pct": 1.75, "gross_exposure_target_pct": 50.0},
}
OPPORTUNITY_CLASSES = ("REJECTED", "STANDARD", "STRONG", "HIGH_CONVICTION", "EXCEPTIONAL")
OPPORTUNITY_MULTIPLIERS = {"REJECTED": 0.0, "STANDARD": 0.85, "STRONG": 1.0, "HIGH_CONVICTION": 1.15, "EXCEPTIONAL": 1.25}
EXECUTABLE_POLICY_STATES = frozenset({"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"})
BINDING_CAP_ORDER = (
    "formula_request", "deployment_mode_trade_risk", "hard_trade_risk", "portfolio_heat",
    "gross_exposure", "symbol_exposure", "cluster_exposure",
)


def _finite(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    number = _finite(value)
    return low if number is None else max(low, min(high, number))


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class AdaptiveConvictionDecision:
    id: str
    created_at: str
    run_id: str | None
    proposal_id: str | None
    candidate_id: str | None
    setup_id: str | None
    strategy_version: str
    policy_decision_id: str | None
    performance_snapshot_id: str | None
    deployment_mode: str
    opportunity_class: str
    base_strategy_risk_pct: float
    opportunity_multiplier: float
    regime_multiplier: float
    account_health_multiplier: float
    execution_quality_multiplier: float
    diversification_multiplier: float
    requested_stop_risk_pct: float
    recommended_stop_risk_pct: float
    operational_stop_risk_pct: float
    per_trade_ceiling_pct: float
    portfolio_heat_target_pct: float
    gross_exposure_target_pct: float
    heat_ceiling_pct: float
    gross_ceiling_pct: float
    symbol_ceiling_pct: float
    cluster_ceiling_pct: float
    binding_cap: str
    confidence: float
    data_quality: float
    reason: str
    raw_inputs: dict[str, Any]
    evidence_version: str
    formula_version: str
    configuration_schema_version: str
    config_hash: str | None
    decision_fingerprint: str
    report_only: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "decision_id": self.id,
            "report_only": True,
            "deployment_mode": self.deployment_mode,
            "opportunity_class": self.opportunity_class,
            "recommended_stop_risk_pct": self.recommended_stop_risk_pct,
            "operational_stop_risk_pct": self.operational_stop_risk_pct,
            "binding_cap": self.binding_cap,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def apply_adaptive_conviction_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adaptive_conviction_decisions(
          id TEXT PRIMARY KEY,created_at TEXT NOT NULL,run_id TEXT,proposal_id TEXT,candidate_id TEXT,setup_id TEXT,
          strategy_version TEXT NOT NULL,policy_decision_id TEXT,performance_snapshot_id TEXT,
          deployment_mode TEXT NOT NULL,opportunity_class TEXT NOT NULL,base_strategy_risk_pct REAL NOT NULL,
          opportunity_multiplier REAL NOT NULL,regime_multiplier REAL NOT NULL,account_health_multiplier REAL NOT NULL,
          execution_quality_multiplier REAL NOT NULL,diversification_multiplier REAL NOT NULL,
          requested_stop_risk_pct REAL NOT NULL,recommended_stop_risk_pct REAL NOT NULL,operational_stop_risk_pct REAL NOT NULL,
          per_trade_ceiling_pct REAL NOT NULL,portfolio_heat_target_pct REAL NOT NULL,gross_exposure_target_pct REAL NOT NULL,
          heat_ceiling_pct REAL NOT NULL,gross_ceiling_pct REAL NOT NULL,symbol_ceiling_pct REAL NOT NULL,cluster_ceiling_pct REAL NOT NULL,
          binding_cap TEXT NOT NULL,confidence REAL NOT NULL,data_quality REAL NOT NULL,reason TEXT NOT NULL,raw_inputs_json TEXT NOT NULL,
          evidence_version TEXT NOT NULL,formula_version TEXT NOT NULL,configuration_schema_version TEXT NOT NULL,config_hash TEXT,
          decision_fingerprint TEXT NOT NULL UNIQUE,report_only INTEGER NOT NULL CHECK(report_only=1))"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adaptive_conviction_proposal ON adaptive_conviction_decisions(proposal_id,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adaptive_conviction_distribution ON adaptive_conviction_decisions(deployment_mode,opportunity_class,created_at)")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (ADAPTIVE_CONVICTION_SCHEMA_VERSION, iso_now(), "additive report-only adaptive-conviction decisions"),
        )


class AdaptiveConvictionEngine:
    """Independent deterministic classifier and bounded diagnostic-risk engine."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.cfg = dict(config.get("adaptive_conviction", {}) or {})
        self._validate()

    def _validate(self) -> None:
        if self.cfg.get("enabled") is not True or self.cfg.get("report_only") is not True:
            raise ValueError("adaptive conviction must be enabled and report-only")
        if self.cfg.get("formula_version") != ADAPTIVE_CONVICTION_FORMULA_VERSION:
            raise ValueError("adaptive conviction formula version mismatch")
        if self.cfg.get("schema_version") != ADAPTIVE_CONVICTION_SCHEMA_VERSION:
            raise ValueError("adaptive conviction schema version mismatch")
        if self.cfg.get("kelly_operational") is not False or self.cfg.get("covariance_operational") is not False:
            raise ValueError("Kelly and covariance must remain diagnostic only")
        if float(self.cfg.get("hard_trade_risk_ceiling_pct", -1)) != 0.35:
            raise ValueError("adaptive conviction hard trade-risk ceiling must be 0.35%")

    @staticmethod
    def _regime_alignment(inputs: Mapping[str, Any]) -> tuple[float | None, bool]:
        explicit = _finite(inputs.get("regime_alignment"))
        if explicit is not None:
            return _clamp(explicit), True
        regime = str(inputs.get("market_regime") or "").lower()
        mapped = {
            "favorable": 1.0, "low": 0.85, "normal": 0.75, "elevated": 0.45,
            "high": 0.25, "extreme": 0.0, "defensive": 0.20,
        }.get(regime)
        return mapped, mapped is not None

    def _derived(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        missing: list[str] = []
        setup_score = _finite(inputs.get("setup_score"))
        evidence_quality = _finite(inputs.get("evidence_quality"))
        evidence_calibrated = inputs.get("evidence_calibrated") is True and evidence_quality is not None
        regime_alignment, regime_known = self._regime_alignment(inputs)
        drawdown = _finite(inputs.get("account_drawdown_pct"))
        daily_loss = _finite(inputs.get("daily_realized_loss_pct"))
        weekly_loss = _finite(inputs.get("weekly_realized_loss_pct"))
        execution_quality = _finite(inputs.get("execution_quality"))
        if execution_quality is None:
            fill_rate = _finite(inputs.get("execution_fill_rate"))
            shortfall_bps = _finite(inputs.get("execution_shortfall_bps"))
            if inputs.get("execution_evidence_calibrated") is True and fill_rate is not None and shortfall_bps is not None:
                execution_quality = min(_clamp(fill_rate), 1.0 - _clamp(max(0.0, shortfall_bps) / 50.0))
        symbol_exposure = _finite(inputs.get("symbol_exposure_pct"))
        cluster_exposure = _finite(inputs.get("cluster_exposure_pct"))
        correlation = _finite(inputs.get("correlation_score"))
        average_dollar_volume = _finite(inputs.get("average_dollar_volume"))
        spread_bps = _finite(inputs.get("quote_spread_bps"))
        reward_to_risk = _finite(inputs.get("reward_to_risk"))
        stop_geometry_quality = _finite(inputs.get("stop_geometry_quality"))
        if stop_geometry_quality is None and inputs.get("stop_valid") is True:
            stop_distance_pct = _finite(inputs.get("stop_distance_pct"))
            if stop_distance_pct is not None:
                stop_geometry_quality = 1.0 if 1.0 <= stop_distance_pct <= 4.0 else (0.75 if 0.5 <= stop_distance_pct <= 6.0 else (0.50 if stop_distance_pct <= 8.0 else 0.0))
        current_heat = _finite(inputs.get("current_portfolio_heat_pct"))
        current_gross = _finite(inputs.get("current_gross_exposure_pct"))

        for name, value in (
            ("setup_score", setup_score), ("evidence_quality", evidence_quality), ("regime_alignment", regime_alignment),
            ("account_drawdown_pct", drawdown), ("daily_realized_loss_pct", daily_loss), ("weekly_realized_loss_pct", weekly_loss),
            ("execution_quality", execution_quality), ("symbol_exposure_pct", symbol_exposure),
            ("cluster_exposure_pct", cluster_exposure), ("correlation_score", correlation),
            ("average_dollar_volume", average_dollar_volume), ("quote_spread_bps", spread_bps),
            ("reward_to_risk", reward_to_risk), ("stop_geometry_quality", stop_geometry_quality),
            ("current_portfolio_heat_pct", current_heat), ("current_gross_exposure_pct", current_gross),
        ):
            if value is None:
                missing.append(name)
        if not evidence_calibrated:
            missing.append("evidence_calibration")
        for name in ("execution_integrity_ok", "reconciliation_ok", "deterioration_detected"):
            if not isinstance(inputs.get(name), bool):
                missing.append(name)

        account_known = drawdown is not None and daily_loss is not None and weekly_loss is not None
        if not account_known:
            account_score = 0.50
        else:
            account_score = min(
                1.0 - _clamp(drawdown / 6.0),
                1.0 - _clamp(daily_loss / 0.75),
                1.0 - _clamp(weekly_loss / 1.50),
            )
        execution_score = _clamp(execution_quality) if execution_quality is not None else 0.50
        diversification_parts = []
        if symbol_exposure is not None:
            diversification_parts.append(1.0 - _clamp(symbol_exposure / float(self.cfg["maximum_symbol_exposure_pct"])))
        if cluster_exposure is not None:
            diversification_parts.append(1.0 - _clamp(cluster_exposure / float(self.cfg["maximum_cluster_exposure_pct"])))
        if correlation is not None:
            diversification_parts.append(1.0 - _clamp(correlation))
        diversification_score = min(diversification_parts) if diversification_parts else 0.50
        liquidity_score = 0.0 if average_dollar_volume is None else _clamp(average_dollar_volume / (5.0 * float(self.cfg["minimum_liquidity_dollars"])))
        spread_score = 0.0 if spread_bps is None else 1.0 - _clamp(spread_bps / float(self.cfg["maximum_quote_spread_bps"]))
        stop_score = _clamp(stop_geometry_quality) if stop_geometry_quality is not None else (0.70 if inputs.get("stop_valid") is True else 0.0)
        setup_quality = 0.0 if setup_score is None else _clamp(setup_score / 100.0)
        evidence_score = _clamp(evidence_quality) if evidence_calibrated else 0.50
        regime_score = _clamp(regime_alignment) if regime_known else 0.50

        integrity_known = isinstance(inputs.get("execution_integrity_ok"), bool) and isinstance(inputs.get("reconciliation_ok"), bool)
        integrity_ok = inputs.get("execution_integrity_ok") is True and inputs.get("reconciliation_ok") is True
        integrity_failed = inputs.get("execution_integrity_ok") is False or inputs.get("reconciliation_ok") is False
        no_deterioration = inputs.get("deterioration_detected") is False
        core_failures = []
        if inputs.get("strategy_authorized") is not True or str(inputs.get("strategy_policy_state")) not in EXECUTABLE_POLICY_STATES:
            core_failures.append("strategy_not_authorized")
        if inputs.get("stop_valid") is not True:
            core_failures.append("invalid_stop")
        if inputs.get("market_data_fresh") is not True:
            core_failures.append("stale_market_data")
        if average_dollar_volume is None or average_dollar_volume < float(self.cfg["minimum_liquidity_dollars"]):
            core_failures.append("liquidity_unavailable_or_below_floor")
        if spread_bps is not None and spread_bps > float(self.cfg["maximum_quote_spread_bps"]):
            core_failures.append("spread_unavailable_or_too_wide")
        if integrity_failed:
            core_failures.append("execution_or_reconciliation_integrity_failed")
        if inputs.get("risk_checks_passed") is not True:
            core_failures.append("phase3_or_entry_risk_checks_failed")

        independent = {
            "setup": setup_quality,
            "evidence": evidence_score if evidence_calibrated else 0.0,
            "regime": regime_score if regime_known else 0.0,
            "stop_geometry": stop_score,
            "liquidity": liquidity_score,
            "spread": spread_score,
            "diversification": diversification_score,
            "account_health": account_score if account_known else 0.0,
            "execution": execution_score if execution_quality is not None else 0.0,
        }
        data_quality_values = [value for value in independent.values() if value > 0]
        data_quality = sum(data_quality_values) / len(independent) if independent else 0.0
        confidence = _clamp(data_quality * (1.0 - min(len(missing), 10) * 0.04))
        return {
            "missing": missing, "setup_score": setup_score, "evidence_calibrated": evidence_calibrated,
            "evidence_score": evidence_score, "regime_score": regime_score, "regime_known": regime_known,
            "account_score": account_score, "account_known": account_known, "execution_score": execution_score,
            "diversification_score": diversification_score, "liquidity_score": liquidity_score,
            "spread_score": spread_score, "stop_score": stop_score, "reward_to_risk": reward_to_risk,
            "current_heat": current_heat, "current_gross": current_gross, "symbol_exposure": symbol_exposure,
            "cluster_exposure": cluster_exposure, "integrity_ok": integrity_ok, "integrity_known": integrity_known,
            "no_deterioration": no_deterioration, "core_failures": core_failures,
            "independent": independent, "data_quality": _clamp(data_quality), "confidence": confidence,
        }

    @staticmethod
    def _opportunity_class(derived: Mapping[str, Any]) -> tuple[str, list[str]]:
        if derived["core_failures"]:
            return "REJECTED", list(derived["core_failures"])
        scores = derived["independent"]
        setup = float(derived.get("setup_score") or 0.0)
        reward = float(derived.get("reward_to_risk") or 0.0)
        calibrated = bool(derived["evidence_calibrated"])
        expansion_safe = calibrated and derived["account_known"] and derived["regime_known"] and derived["no_deterioration"]
        at_90 = sum(value >= 0.90 for name, value in scores.items() if name != "setup")
        at_75 = sum(value >= 0.75 for name, value in scores.items() if name != "setup")
        at_60 = sum(value >= 0.60 for name, value in scores.items() if name != "setup")
        if expansion_safe and setup >= 95 and reward >= 2.5 and at_90 >= 7 and derived["integrity_ok"]:
            return "EXCEPTIONAL", ["setup>=95", "reward_to_risk>=2.5", "seven_independent_signals>=0.90"]
        if expansion_safe and setup >= 90 and reward >= 2.0 and at_75 >= 7 and derived["integrity_ok"]:
            return "HIGH_CONVICTION", ["setup>=90", "reward_to_risk>=2.0", "seven_independent_signals>=0.75"]
        if expansion_safe and setup >= 85 and reward >= 1.5 and at_60 >= 6 and derived["integrity_ok"]:
            return "STRONG", ["setup>=85", "reward_to_risk>=1.5", "six_independent_signals>=0.60"]
        return "STANDARD", ["core_entry_checks_passed", "higher_class_independent_agreement_not_met"]

    @staticmethod
    def _mode(opportunity_class: str, derived: Mapping[str, Any]) -> tuple[str, list[str]]:
        if opportunity_class == "REJECTED":
            return "DEFENSIVE", ["entry_disqualified"]
        if derived["account_score"] < 0.50 or not derived["integrity_ok"] or not derived["no_deterioration"]:
            return "DEFENSIVE", ["account_integrity_or_deterioration_requires_defense"]
        complete_expansion = not any(
            name in derived["missing"]
            for name in ("evidence_quality", "regime_alignment", "account_drawdown_pct", "daily_realized_loss_pct", "weekly_realized_loss_pct", "execution_quality", "correlation_score")
        )
        if opportunity_class == "EXCEPTIONAL" and complete_expansion and derived["account_score"] >= 0.90 and derived["execution_score"] >= 0.90 and derived["diversification_score"] >= 0.90:
            return "AGGRESSIVE", ["exceptional_multisignal_evidence_and_capacity"]
        if opportunity_class in {"HIGH_CONVICTION", "EXCEPTIONAL"} and complete_expansion and derived["account_score"] >= 0.75 and derived["execution_score"] >= 0.75 and derived["diversification_score"] >= 0.75:
            return "OPPORTUNISTIC", ["high_conviction_multisignal_evidence_and_capacity"]
        return "NORMAL", ["baseline_diagnostic_mode"]

    def evaluate(self, inputs: Mapping[str, Any]) -> AdaptiveConvictionDecision | None:
        raw = dict(inputs)
        if str(raw.get("action") or "entry").lower() != "entry" or str(raw.get("side") or "buy").lower() != "buy":
            return None
        derived = self._derived(raw)
        opportunity_class, class_reasons = self._opportunity_class(derived)
        deployment_mode, mode_reasons = self._mode(opportunity_class, derived)
        mode = DEPLOYMENT_MODES[deployment_mode]

        base = max(0.0, min(float(self.cfg["hard_trade_risk_ceiling_pct"]), float(raw.get("base_strategy_risk_pct") or self.cfg["base_strategy_risk_pct"])))
        opportunity_multiplier = OPPORTUNITY_MULTIPLIERS[opportunity_class]
        regime_multiplier = 1.0 if not derived["regime_known"] else 0.75 + 0.50 * derived["regime_score"]
        account_multiplier = 0.75 + 0.40 * derived["account_score"] if derived["account_known"] else 0.85
        execution_multiplier = 1.0 if "execution_quality" in derived["missing"] else 0.75 + 0.50 * derived["execution_score"]
        diversification_multiplier = 0.65 + 0.55 * derived["diversification_score"]
        requested = base * opportunity_multiplier * regime_multiplier * account_multiplier * execution_multiplier * diversification_multiplier

        hard_cap = float(self.cfg["hard_trade_risk_ceiling_pct"])
        current_heat = derived["current_heat"]
        heat_ceiling = 0.0 if current_heat is not None and current_heat >= mode["portfolio_heat_target_pct"] else (
            mode["trade_risk_cap_pct"] if current_heat is None else min(mode["trade_risk_cap_pct"], mode["portfolio_heat_target_pct"] - current_heat)
        )

        def capacity_ceiling(current: float | None, target: float) -> float:
            if current is None:
                return mode["trade_risk_cap_pct"] * 0.75
            return mode["trade_risk_cap_pct"] * _clamp((target - current) / max(target, 1e-12))

        gross_ceiling = capacity_ceiling(derived["current_gross"], mode["gross_exposure_target_pct"])
        symbol_ceiling = capacity_ceiling(derived["symbol_exposure"], float(self.cfg["maximum_symbol_exposure_pct"]))
        cluster_ceiling = capacity_ceiling(derived["cluster_exposure"], float(self.cfg["maximum_cluster_exposure_pct"]))
        caps = {
            "formula_request": max(0.0, requested),
            "deployment_mode_trade_risk": mode["trade_risk_cap_pct"],
            "hard_trade_risk": hard_cap,
            "portfolio_heat": max(0.0, heat_ceiling),
            "gross_exposure": max(0.0, gross_ceiling),
            "symbol_exposure": max(0.0, symbol_ceiling),
            "cluster_exposure": max(0.0, cluster_ceiling),
        }
        binding_cap = min(BINDING_CAP_ORDER, key=lambda name: (caps[name], BINDING_CAP_ORDER.index(name)))
        recommended = 0.0 if opportunity_class == "REJECTED" else caps[binding_cap]
        operational = max(0.0, float(raw.get("operational_stop_risk_pct") or 0.0))
        diagnostic_context = {
            "kelly": {"label": "diagnostic_only", "operational": False, "value": raw.get("kelly_diagnostic")},
            "covariance": {"label": "diagnostic_only", "operational": False, "value": raw.get("covariance_diagnostic")},
        }
        persisted_raw = {
            **raw,
            "derived": derived,
            "caps_pct": caps,
            "class_reasons": class_reasons,
            "mode_reasons": mode_reasons,
            "diagnostic_context": diagnostic_context,
            "operational_authority": "canonical sizing, Phase 3 limits, final one-way reduction, and stage cap",
        }
        reason_parts = [f"{opportunity_class}/{deployment_mode}", *class_reasons, *mode_reasons, f"binding={binding_cap}"]
        if derived["missing"]:
            reason_parts.append("missing=" + ",".join(sorted(derived["missing"])))
        fingerprint_payload = {
            "identifiers": {name: raw.get(name) for name in ("run_id", "proposal_id", "candidate_id", "setup_id", "strategy_version", "policy_decision_id", "performance_snapshot_id")},
            "inputs": persisted_raw,
            "formula_version": ADAPTIVE_CONVICTION_FORMULA_VERSION,
            "config_hash": self.config.get("effective_config_hash"),
        }
        fingerprint = _fingerprint(fingerprint_payload)
        return AdaptiveConvictionDecision(
            id=fingerprint[:32], created_at=iso_now(), run_id=raw.get("run_id"), proposal_id=raw.get("proposal_id"),
            candidate_id=raw.get("candidate_id"), setup_id=raw.get("setup_id"), strategy_version=str(raw.get("strategy_version") or ""),
            policy_decision_id=raw.get("policy_decision_id"), performance_snapshot_id=raw.get("performance_snapshot_id"),
            deployment_mode=deployment_mode, opportunity_class=opportunity_class, base_strategy_risk_pct=round(base, 8),
            opportunity_multiplier=round(opportunity_multiplier, 8), regime_multiplier=round(regime_multiplier, 8),
            account_health_multiplier=round(account_multiplier, 8), execution_quality_multiplier=round(execution_multiplier, 8),
            diversification_multiplier=round(diversification_multiplier, 8), requested_stop_risk_pct=round(requested, 8),
            recommended_stop_risk_pct=round(recommended, 8), operational_stop_risk_pct=round(operational, 8),
            per_trade_ceiling_pct=mode["trade_risk_cap_pct"], portfolio_heat_target_pct=mode["portfolio_heat_target_pct"],
            gross_exposure_target_pct=mode["gross_exposure_target_pct"], heat_ceiling_pct=round(heat_ceiling, 8),
            gross_ceiling_pct=round(gross_ceiling, 8), symbol_ceiling_pct=round(symbol_ceiling, 8),
            cluster_ceiling_pct=round(cluster_ceiling, 8), binding_cap=binding_cap,
            confidence=round(derived["confidence"], 8), data_quality=round(derived["data_quality"], 8),
            reason="; ".join(reason_parts), raw_inputs=persisted_raw, evidence_version=EVIDENCE_VERSION,
            formula_version=ADAPTIVE_CONVICTION_FORMULA_VERSION, configuration_schema_version=CONFIGURATION_SCHEMA_VERSION,
            config_hash=self.config.get("effective_config_hash"), decision_fingerprint=fingerprint, report_only=True,
        )

    @staticmethod
    def persist(storage: Any, decision: AdaptiveConvictionDecision) -> None:
        values = asdict(decision)
        values["raw_inputs_json"] = json_dumps(values.pop("raw_inputs"))
        values["report_only"] = 1
        columns = list(values)
        storage.execute(
            f"INSERT OR IGNORE INTO adaptive_conviction_decisions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values[name] for name in columns),
        )

    def replay(self, records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        decisions = [decision for record in records if (decision := self.evaluate(record)) is not None]
        modes = Counter(decision.deployment_mode for decision in decisions)
        classes = Counter(decision.opportunity_class for decision in decisions)
        bindings = Counter(decision.binding_cap for decision in decisions)
        disqualifications: Counter[str] = Counter()
        missing: Counter[str] = Counter()
        contradictions = []
        examples = []
        for decision in decisions:
            for reason in decision.raw_inputs["derived"]["core_failures"]:
                disqualifications[reason] += 1
            for name in decision.raw_inputs["derived"]["missing"]:
                missing[name] += 1
            if decision.deployment_mode == "AGGRESSIVE" and decision.opportunity_class != "EXCEPTIONAL":
                contradictions.append(decision.id)
            if decision.deployment_mode == "OPPORTUNISTIC" and decision.opportunity_class not in {"HIGH_CONVICTION", "EXCEPTIONAL"}:
                contradictions.append(decision.id)
            if decision.deployment_mode in {"OPPORTUNISTIC", "AGGRESSIVE"}:
                examples.append(decision.summary())
        recommended = sorted(item.recommended_stop_risk_pct for item in decisions)
        operational = sorted(item.operational_stop_risk_pct for item in decisions)

        def distribution(values: list[float]) -> dict[str, float]:
            if not values:
                return {"minimum_pct": 0.0, "median_pct": 0.0, "average_pct": 0.0, "maximum_pct": 0.0}
            return {
                "minimum_pct": min(values), "median_pct": values[len(values) // 2],
                "average_pct": round(sum(values) / len(values), 8), "maximum_pct": max(values),
            }
        return {
            "report_only": True,
            "records_evaluated": len(decisions),
            "deployment_modes": dict(sorted(modes.items())),
            "opportunity_classes": dict(sorted(classes.items())),
            "recommended_vs_operational": {
                "recommended": distribution(recommended), "operational": distribution(operational),
                "recommended_above_operational": sum(item.recommended_stop_risk_pct > item.operational_stop_risk_pct for item in decisions),
                "recommended_equal_operational": sum(item.recommended_stop_risk_pct == item.operational_stop_risk_pct for item in decisions),
                "recommended_below_operational": sum(item.recommended_stop_risk_pct < item.operational_stop_risk_pct for item in decisions),
            },
            "binding_caps": dict(sorted(bindings.items())),
            "expansion_examples": examples[:10],
            "disqualification_reasons": dict(sorted(disqualifications.items())),
            "missing_data_frequency": dict(sorted(missing.items())),
            "contradictory_classifications": contradictions,
            "trading_state_mutations": 0,
        }

    @staticmethod
    def format_report(storage: Any) -> str:
        rows = storage.fetch_all(
            "SELECT deployment_mode,opportunity_class,recommended_stop_risk_pct,operational_stop_risk_pct,binding_cap FROM adaptive_conviction_decisions ORDER BY created_at DESC,id DESC LIMIT 100"
        )
        if not rows:
            return "Adaptive Conviction (report-only): no persisted entry diagnostics."
        modes = Counter(str(row["deployment_mode"]) for row in rows)
        classes = Counter(str(row["opportunity_class"]) for row in rows)
        latest = rows[0]
        return (
            "Adaptive Conviction (report-only)\n"
            f"Latest: {latest['deployment_mode']} / {latest['opportunity_class']} | recommended {float(latest['recommended_stop_risk_pct']):.4f}% vs operational {float(latest['operational_stop_risk_pct']):.4f}% | binding {latest['binding_cap']}\n"
            f"Last {len(rows)}: modes {dict(sorted(modes.items()))}; classes {dict(sorted(classes.items()))}.\n"
            "Diagnostic only; canonical sizing, Phase 3 limits, final one-way reduction, and the $250 stage cap remain authoritative."
        )


__all__ = [
    "AdaptiveConvictionDecision", "AdaptiveConvictionEngine", "DEPLOYMENT_MODES", "OPPORTUNITY_CLASSES",
    "OPPORTUNITY_MULTIPLIERS", "BINDING_CAP_ORDER", "apply_adaptive_conviction_schema",
]
