"""Deterministic operational-paper sizing from Adaptive Conviction.

The engine consumes canonical safety ceilings and owns proposal/final sizing.
It cannot create approvals, reservations, intents, orders, or broker requests;
those remain in the durable execution workflow.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .formula_versions import (
    ADAPTIVE_SIZING_FORMULA_VERSION,
    ADAPTIVE_SIZING_SCHEMA_VERSION,
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    SIZING_POLICY_VERSION,
    STOP_POLICY_VERSION,
)
from .position_sizing import notional_from_stop_risk
from .utils import iso_now, json_dumps


COMPARISON_DIRECTIONS = frozenset({"INCREASE", "UNCHANGED", "REDUCE", "REJECT"})
FINAL_REVALIDATION_OUTCOMES = frozenset({
    "STAYED_EQUAL", "REDUCED", "BECAME_BLOCKED", "INCREASE_CONSTRAINED_BY_DISPLAYED_CEILING",
})
CANONICAL_CEILING_ORDER = (
    "stage", "stop_risk", "displayed_quantity", "displayed_stop_risk", "deployment_mode_heat", "deployment_mode_gross", "equity", "absolute", "cash_available", "cash_usage", "cash",
    "buying_power", "symbol", "cluster", "portfolio", "allocation", "exploration", "probe",
    "phase3_heat_cap", "gross_exposure_cap", "exploration_heat_cap", "exploration_strategy_cap",
    "exploration_gross_cap", "probe_heat_cap", "probe_gross_cap", "probe_active_count_cap",
)
TRADING_STATE_TABLES = (
    "trade_proposals", "approvals", "risk_reservations", "order_intents", "orders", "fills",
)


def _finite(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _percentage_difference(dollars: float, operational: float) -> float | None:
    if operational <= 0:
        return None
    return dollars / operational * 100.0


def _ordered_ceilings(ceilings: Mapping[str, Any]) -> list[tuple[str, float]]:
    finite = {
        str(name): max(0.0, float(value))
        for name, value in ceilings.items()
        if _finite(value) is not None
    }
    names = [name for name in CANONICAL_CEILING_ORDER if name in finite]
    names.extend(sorted(set(finite) - set(names)))
    return [(name, finite[name]) for name in names]


@dataclass(frozen=True)
class AdaptiveSizingDecision:
    id: str
    stage: str
    created_at: str
    run_id: str | None
    proposal_id: str | None
    candidate_id: str | None
    setup_id: str | None
    approval_id: str | None
    strategy_version: str
    policy_id: str | None
    adaptive_conviction_decision_id: str | None
    action: str
    operational_requested_notional: float
    operational_constrained_notional: float
    operational_quantity: float
    operational_stop_risk_pct: float
    operational_stop_risk_dollars: float
    conviction_stop_risk_pct: float
    conviction_stop_risk_dollars: float
    adaptive_requested_notional: float
    adaptive_constrained_notional: float
    adaptive_quantity: float
    adaptive_constrained_stop_risk_pct: float
    adaptive_constrained_stop_risk_dollars: float
    ceilings: dict[str, float]
    ceiling_path: dict[str, float]
    binding_adaptive_cap: str
    comparison_direction: str
    difference_dollars: float
    difference_pct: float | None
    displayed_adaptive_ceiling: float
    future_activation_notional: float
    final_operational_notional: float
    final_operational_quantity: float
    final_revalidation_outcome: str | None
    proposal_to_approval_drift_dollars: float | None
    proposal_to_approval_drift_pct: float | None
    confidence: float
    missing_inputs: list[str]
    contradictions: list[str]
    hypothetical_portfolio_heat_pct: float | None
    hypothetical_gross_exposure_pct: float | None
    hypothetical_symbol_exposure_pct: float | None
    hypothetical_cluster_exposure_pct: float | None
    would_exceed_hard_limit: bool
    raw_inputs: dict[str, Any]
    evidence_version: str
    formula_version: str
    schema_version: str
    configuration_version: str
    sizing_policy_version: str
    stop_policy_version: str
    config_hash: str | None
    decision_fingerprint: str
    operating_mode: str
    operational_enforced: bool
    report_only: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "decision_id": self.id,
            "stage": self.stage,
            "report_only": False,
            "operating_mode": self.operating_mode,
            "operational_enforced": self.operational_enforced,
            "operational_notional": self.final_operational_notional,
            "operational_quantity": self.final_operational_quantity,
            "adaptive_notional": self.adaptive_constrained_notional,
            "adaptive_quantity": self.adaptive_quantity,
            "comparison_direction": self.comparison_direction,
            "difference_dollars": self.difference_dollars,
            "difference_pct": self.difference_pct,
            "binding_adaptive_cap": self.binding_adaptive_cap,
            "sizing_caps": dict(self.ceilings),
            "stop_risk_pct": self.adaptive_constrained_stop_risk_pct,
            "stop_risk_dollars": self.adaptive_constrained_stop_risk_dollars,
            "displayed_adaptive_ceiling": self.displayed_adaptive_ceiling,
            "final_revalidation_outcome": self.final_revalidation_outcome,
            "confidence": self.confidence,
            "missing_inputs": list(self.missing_inputs),
        }


def apply_adaptive_sizing_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adaptive_sizing_decisions(
          id TEXT PRIMARY KEY,stage TEXT NOT NULL CHECK(stage IN ('proposal','final_revalidation')),created_at TEXT NOT NULL,
          run_id TEXT,proposal_id TEXT,candidate_id TEXT,setup_id TEXT,approval_id TEXT,strategy_version TEXT NOT NULL,
          policy_id TEXT,adaptive_conviction_decision_id TEXT,action TEXT NOT NULL,
          operational_requested_notional REAL NOT NULL,operational_constrained_notional REAL NOT NULL,operational_quantity REAL NOT NULL,
          operational_stop_risk_pct REAL NOT NULL,operational_stop_risk_dollars REAL NOT NULL,
          conviction_stop_risk_pct REAL NOT NULL,conviction_stop_risk_dollars REAL NOT NULL,
          adaptive_requested_notional REAL NOT NULL,adaptive_constrained_notional REAL NOT NULL,adaptive_quantity REAL NOT NULL,
          adaptive_constrained_stop_risk_pct REAL NOT NULL,adaptive_constrained_stop_risk_dollars REAL NOT NULL,
          ceilings_json TEXT NOT NULL,ceiling_path_json TEXT NOT NULL,binding_adaptive_cap TEXT NOT NULL,
          comparison_direction TEXT NOT NULL CHECK(comparison_direction IN ('INCREASE','UNCHANGED','REDUCE','REJECT')),
          difference_dollars REAL NOT NULL,difference_pct REAL,displayed_adaptive_ceiling REAL NOT NULL,
          future_activation_notional REAL NOT NULL,final_revalidation_outcome TEXT,
          proposal_to_approval_drift_dollars REAL,proposal_to_approval_drift_pct REAL,
          confidence REAL NOT NULL,missing_inputs_json TEXT NOT NULL,contradictions_json TEXT NOT NULL,
          hypothetical_portfolio_heat_pct REAL,hypothetical_gross_exposure_pct REAL,
          hypothetical_symbol_exposure_pct REAL,hypothetical_cluster_exposure_pct REAL,
          would_exceed_hard_limit INTEGER NOT NULL CHECK(would_exceed_hard_limit IN (0,1)),raw_inputs_json TEXT NOT NULL,
          evidence_version TEXT NOT NULL,formula_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          configuration_version TEXT NOT NULL,sizing_policy_version TEXT NOT NULL,stop_policy_version TEXT NOT NULL,
          config_hash TEXT,decision_fingerprint TEXT NOT NULL UNIQUE,report_only INTEGER NOT NULL CHECK(report_only=1))"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adaptive_sizing_proposal ON adaptive_sizing_decisions(proposal_id,stage,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adaptive_sizing_evidence ON adaptive_sizing_decisions(stage,comparison_direction,created_at)")
    present = {row[1] for row in conn.execute("PRAGMA table_info(adaptive_sizing_decisions)")}
    for name in ("adaptive_constrained_stop_risk_pct", "adaptive_constrained_stop_risk_dollars"):
        if name not in present:
            conn.execute(f"ALTER TABLE adaptive_sizing_decisions ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adaptive_sizing_operational_decisions(
          id TEXT PRIMARY KEY,stage TEXT NOT NULL CHECK(stage IN ('proposal','final_revalidation')),created_at TEXT NOT NULL,
          run_id TEXT,proposal_id TEXT,candidate_id TEXT,setup_id TEXT,approval_id TEXT,strategy_version TEXT NOT NULL,
          policy_id TEXT,adaptive_conviction_decision_id TEXT,action TEXT NOT NULL,
          operational_requested_notional REAL NOT NULL,operational_constrained_notional REAL NOT NULL,operational_quantity REAL NOT NULL,
          operational_stop_risk_pct REAL NOT NULL,operational_stop_risk_dollars REAL NOT NULL,
          conviction_stop_risk_pct REAL NOT NULL,conviction_stop_risk_dollars REAL NOT NULL,
          adaptive_requested_notional REAL NOT NULL,adaptive_constrained_notional REAL NOT NULL,adaptive_quantity REAL NOT NULL,
          adaptive_constrained_stop_risk_pct REAL NOT NULL,adaptive_constrained_stop_risk_dollars REAL NOT NULL,
          ceilings_json TEXT NOT NULL,ceiling_path_json TEXT NOT NULL,binding_adaptive_cap TEXT NOT NULL,
          comparison_direction TEXT NOT NULL CHECK(comparison_direction IN ('INCREASE','UNCHANGED','REDUCE','REJECT')),
          difference_dollars REAL NOT NULL,difference_pct REAL,displayed_adaptive_ceiling REAL NOT NULL,
          future_activation_notional REAL NOT NULL,final_operational_notional REAL NOT NULL,final_operational_quantity REAL NOT NULL,
          final_revalidation_outcome TEXT,proposal_to_approval_drift_dollars REAL,proposal_to_approval_drift_pct REAL,
          confidence REAL NOT NULL,missing_inputs_json TEXT NOT NULL,contradictions_json TEXT NOT NULL,
          hypothetical_portfolio_heat_pct REAL,hypothetical_gross_exposure_pct REAL,
          hypothetical_symbol_exposure_pct REAL,hypothetical_cluster_exposure_pct REAL,
          would_exceed_hard_limit INTEGER NOT NULL CHECK(would_exceed_hard_limit IN (0,1)),raw_inputs_json TEXT NOT NULL,
          evidence_version TEXT NOT NULL,formula_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          configuration_version TEXT NOT NULL,sizing_policy_version TEXT NOT NULL,stop_policy_version TEXT NOT NULL,
          config_hash TEXT,decision_fingerprint TEXT NOT NULL UNIQUE,
          operating_mode TEXT NOT NULL CHECK(operating_mode='operational_paper'),
          operational_enforced INTEGER NOT NULL CHECK(operational_enforced=1),
          report_only INTEGER NOT NULL CHECK(report_only=0))"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adaptive_sizing_operational_proposal ON adaptive_sizing_operational_decisions(proposal_id,stage,created_at)")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (ADAPTIVE_SIZING_SCHEMA_VERSION, iso_now(), "additive operational-paper adaptive sizing decisions; historical shadow rows preserved"),
        )


class AdaptiveSizingEngine:
    """Convert persisted conviction risk into authoritative paper size using canonical caps."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        cfg = self.config.get("adaptive_sizing", {}) or {}
        if cfg.get("enabled") is not True or cfg.get("mode") != "operational_paper":
            raise ValueError("adaptive sizing must be enabled in operational_paper mode")
        if cfg.get("operational_enforcement") is not True or cfg.get("allow_order_size_change") is not True:
            raise ValueError("adaptive sizing must control paper proposal and approval sizing")
        if cfg.get("formula_version") != ADAPTIVE_SIZING_FORMULA_VERSION or cfg.get("schema_version") != ADAPTIVE_SIZING_SCHEMA_VERSION:
            raise ValueError("adaptive sizing formula or schema version mismatch")
        if self.config.get("mode") != "paper" or self.config.get("live_enabled") is not False:
            raise ValueError("operational adaptive sizing requires paper-only mode")
        if self.config.get("auto_execution_enabled") is not False or self.config.get("auto_execution_mode") != "manual_only":
            raise ValueError("operational adaptive sizing requires manual approval")

    def evaluate(self, inputs: Mapping[str, Any]) -> AdaptiveSizingDecision | None:
        raw = dict(inputs)
        action = str(raw.get("action") or "").lower()
        if str(raw.get("side") or "").lower() != "buy" or action not in {"entry", "add"}:
            return None
        stage = str(raw.get("stage") or "")
        if stage not in {"proposal", "final_revalidation"}:
            raise ValueError("adaptive sizing stage must be proposal or final_revalidation")

        conviction = dict(raw.get("adaptive_conviction") or {})
        operational = dict(raw.get("operational_sizing") or {})
        required = {
            "authoritative_equity": _finite(raw.get("authoritative_equity")),
            "authoritative_cash": _finite(raw.get("authoritative_cash")),
            "authoritative_buying_power": _finite(raw.get("authoritative_buying_power")),
            "entry_price": _finite(raw.get("entry_price")),
            "stop_distance_dollars": _finite(raw.get("stop_distance_dollars")),
            "conviction_recommended_stop_risk_pct": _finite(conviction.get("recommended_stop_risk_pct")),
            "operational_constrained_notional": _finite(operational.get("final_notional")),
        }
        critical_missing = [name for name, value in required.items() if value is None]
        for name in ("current_portfolio_heat_pct", "current_gross_exposure_pct", "current_symbol_exposure_pct", "current_cluster_exposure_pct"):
            if _finite(raw.get(name)) is None:
                critical_missing.append(name)
        reservations = raw.get("active_reservations")
        if not isinstance(reservations, Mapping) or _finite(reservations.get("notional")) is None or _finite(reservations.get("stop_risk")) is None:
            critical_missing.append("active_reservations")
        integrity = raw.get("integrity")
        if not isinstance(integrity, Mapping) or integrity.get("reconciliation_checked") is not True or integrity.get("pending_buy_exposure_unknown") is not False:
            critical_missing.append("authoritative_integrity_and_reservation_state")
        missing = list(critical_missing)
        missing.extend(str(name) for name in conviction.get("missing_inputs", []) if name)
        missing.extend(str(name) for name in raw.get("missing_inputs", []) if name)
        missing = sorted(set(missing))

        equity = max(0.0, required["authoritative_equity"] or 0.0)
        entry_price = max(0.0, required["entry_price"] or 0.0)
        stop_distance = max(0.0, required["stop_distance_dollars"] or 0.0)
        recommended_pct = max(0.0, required["conviction_recommended_stop_risk_pct"] or 0.0)
        operational_constrained = max(0.0, required["operational_constrained_notional"] or 0.0)
        operational_requested = max(0.0, _finite(operational.get("score_adjusted_notional")) or operational_constrained)
        operational_quantity = max(0.0, _finite(operational.get("suggested_shares")) or (operational_constrained / entry_price if entry_price else 0.0))
        operational_stop_dollars = max(0.0, _finite(operational.get("stop_risk_dollars")) or 0.0)
        operational_stop_pct = operational_stop_dollars / equity * 100.0 if equity else 0.0

        conviction_dollars = equity * recommended_pct / 100.0 if equity else 0.0
        adaptive_requested = 0.0
        if equity > 0 and entry_price > 0 and stop_distance > 0 and not {
            "authoritative_equity", "entry_price", "stop_distance_dollars",
            "conviction_recommended_stop_risk_pct",
        }.intersection(missing):
            adaptive_requested = notional_from_stop_risk(conviction_dollars, entry_price, stop_distance)

        ceilings = dict(operational.get("sizing_caps") or {})
        if (self.config.get("position_sizing", {}) or {}).get("use_stage_dollar_cap") is False:
            ceilings.pop("stage", None)
        policy_state = str(raw.get("strategy_policy_state") or "")
        if policy_state == "ACTIVE":
            # The old equal-risk allocation and baseline stop-risk ceilings are
            # comparators, not mature ACTIVE caps. Replace stop risk with the
            # immutable Phase 3 hard envelope; conviction remains at or below it.
            ceilings.pop("allocation", None)
            hard_trade_pct = float(
                ((self.config.get("phase3", {}) or {}).get("risk_profile", {}) or {}).get("max_trade_stop_risk_pct", 0.35)
            )
            if equity > 0 and entry_price > 0 and stop_distance > 0:
                ceilings["stop_risk"] = notional_from_stop_risk(
                    equity * hard_trade_pct / 100.0, entry_price, stop_distance
                )
        ceilings.pop("phase3_heat_cap", None)
        ceilings.pop("gross_exposure_cap", None)
        heat_before = _finite(raw.get("current_portfolio_heat_pct"))
        gross_before = _finite(raw.get("current_gross_exposure_pct"))
        heat_target = _finite(conviction.get("portfolio_heat_target_pct"))
        gross_target = _finite(conviction.get("gross_exposure_target_pct"))
        if equity > 0 and entry_price > 0 and stop_distance > 0 and heat_before is not None and heat_target is not None:
            ceilings["deployment_mode_heat"] = notional_from_stop_risk(
                equity * max(0.0, heat_target - heat_before) / 100.0, entry_price, stop_distance
            )
        if equity > 0 and gross_before is not None and gross_target is not None:
            ceilings["deployment_mode_gross"] = equity * max(0.0, gross_target - gross_before) / 100.0
        if stage == "final_revalidation":
            displayed_quantity = _finite(raw.get("displayed_quantity_ceiling"))
            displayed_stop_risk = _finite(raw.get("displayed_stop_risk_dollars"))
            if displayed_quantity is not None and entry_price > 0:
                ceilings["displayed_quantity"] = max(0.0, displayed_quantity) * entry_price
            if displayed_stop_risk is not None and entry_price > 0 and stop_distance > 0:
                ceilings["displayed_stop_risk"] = notional_from_stop_risk(max(0.0, displayed_stop_risk), entry_price, stop_distance)
        ceiling_path: dict[str, float] = {}
        constrained = adaptive_requested
        binding = "conviction_stop_risk"
        for name, ceiling in _ordered_ceilings(ceilings):
            prior = constrained
            constrained = min(constrained, ceiling)
            ceiling_path[name] = round(constrained, 8)
            if constrained < prior - 1e-9:
                binding = name
        minimum = max(0.0, _finite(operational.get("minimum_executable_notional")) or 0.0)
        blocked_reason = str(operational.get("blocked_reason") or "")
        if critical_missing:
            constrained = 0.0
            binding = "missing_critical_inputs"
        elif blocked_reason or (constrained > 0 and minimum > 0 and constrained < minimum):
            constrained = 0.0
            binding = "operational_block" if blocked_reason else "minimum_executable_notional"
        if missing and adaptive_requested <= 0 and not critical_missing:
            binding = "missing_inputs"
        adaptive_quantity = constrained / entry_price if entry_price > 0 else 0.0
        adaptive_stop_dollars = adaptive_quantity * stop_distance if stop_distance > 0 else 0.0
        adaptive_stop_pct = adaptive_stop_dollars / equity * 100.0 if equity > 0 else 0.0

        difference = constrained - operational_constrained
        tolerance = 1e-8
        if constrained <= tolerance:
            direction = "REJECT"
        elif difference > tolerance:
            direction = "INCREASE"
        elif difference < -tolerance:
            direction = "REDUCE"
        else:
            direction = "UNCHANGED"

        displayed = max(0.0, _finite(raw.get("displayed_adaptive_ceiling")) or (constrained if stage == "proposal" else 0.0))
        proposal_adaptive = _finite(raw.get("proposal_adaptive_notional"))
        final_blocked = bool(raw.get("final_revalidation_blocked")) or constrained <= 0
        future_activation = constrained
        final_outcome = None
        drift_dollars = None
        drift_pct = None
        if stage == "final_revalidation":
            future_activation = min(displayed, constrained, *[value for _name, value in _ordered_ceilings(ceilings)]) if ceilings else min(displayed, constrained)
            if final_blocked:
                final_outcome = "BECAME_BLOCKED"
                future_activation = 0.0
            elif constrained > displayed + tolerance:
                final_outcome = "INCREASE_CONSTRAINED_BY_DISPLAYED_CEILING"
            elif future_activation < displayed - tolerance:
                final_outcome = "REDUCED"
            else:
                final_outcome = "STAYED_EQUAL"
            if proposal_adaptive is not None:
                drift_dollars = constrained - proposal_adaptive
                drift_pct = _percentage_difference(drift_dollars, proposal_adaptive)

        heat_before = _finite(raw.get("current_portfolio_heat_pct"))
        gross_before = _finite(raw.get("current_gross_exposure_pct"))
        symbol_before = _finite(raw.get("current_symbol_exposure_pct"))
        cluster_before = _finite(raw.get("current_cluster_exposure_pct"))
        risk_increment_pct = adaptive_stop_pct if equity else None
        notional_increment_pct = constrained / equity * 100.0 if equity else None
        hypothetical_heat = heat_before + risk_increment_pct if heat_before is not None and risk_increment_pct is not None else None
        hypothetical_gross = gross_before + notional_increment_pct if gross_before is not None and notional_increment_pct is not None else None
        hypothetical_symbol = symbol_before + notional_increment_pct if symbol_before is not None and notional_increment_pct is not None else None
        hypothetical_cluster = cluster_before + notional_increment_pct if cluster_before is not None and notional_increment_pct is not None else None

        hard_limits = dict(raw.get("hard_limits_pct") or {})
        hard_pairs = (
            ("portfolio_heat", None if heat_before is None or equity <= 0 else heat_before + conviction_dollars / equity * 100.0),
            ("gross_exposure", None if gross_before is None or equity <= 0 else gross_before + adaptive_requested / equity * 100.0),
            ("symbol_exposure", None if symbol_before is None or equity <= 0 else symbol_before + adaptive_requested / equity * 100.0),
            ("cluster_exposure", None if cluster_before is None or equity <= 0 else cluster_before + adaptive_requested / equity * 100.0),
        )
        contradictions: list[str] = []
        would_exceed = False
        for name, value in hard_pairs:
            limit = _finite(hard_limits.get(name))
            if value is not None and limit is not None and value > limit + 1e-9:
                would_exceed = True
        if future_activation > displayed + tolerance:
            contradictions.append("future_activation_exceeds_displayed_ceiling")
        if direction == "REJECT" and constrained > tolerance:
            contradictions.append("reject_with_positive_adaptive_size")

        confidence = max(0.0, min(1.0, _finite(conviction.get("confidence")) or 0.0))
        confidence *= max(0.0, 1.0 - min(len(missing), 10) * 0.08)
        persisted_raw = {
            **raw,
            "adaptive_conviction": conviction,
            "operational_sizing": operational,
            "canonical_ceiling_order": list(CANONICAL_CEILING_ORDER),
            "approval_contract": "min(displayed_approved_adaptive_ceiling, approval_recomputed_adaptive_size, current_phase3_capacity, reservation_adjusted_capacity, all_hard_ceilings)",
            "operational_authority": "adaptive sizing controls paper proposal and submitted quantity through Executor",
        }
        fingerprint_payload = {
            "stage": stage,
            "identifiers": {name: raw.get(name) for name in ("run_id", "proposal_id", "candidate_id", "setup_id", "approval_id", "strategy_version", "policy_id")},
            "raw_inputs": persisted_raw,
            "formula_version": ADAPTIVE_SIZING_FORMULA_VERSION,
            "config_hash": self.config.get("effective_config_hash"),
        }
        fingerprint = _fingerprint(fingerprint_payload)
        final_operational = constrained if stage == "proposal" else future_activation
        final_quantity = final_operational / entry_price if entry_price > 0 else 0.0
        return AdaptiveSizingDecision(
            id=fingerprint[:32], stage=stage, created_at=iso_now(), run_id=raw.get("run_id"),
            proposal_id=raw.get("proposal_id"), candidate_id=raw.get("candidate_id"), setup_id=raw.get("setup_id"),
            approval_id=raw.get("approval_id"), strategy_version=str(raw.get("strategy_version") or ""),
            policy_id=raw.get("policy_id"), adaptive_conviction_decision_id=conviction.get("decision_id"), action=action,
            operational_requested_notional=round(operational_requested, 8), operational_constrained_notional=round(operational_constrained, 8),
            operational_quantity=round(operational_quantity, 8), operational_stop_risk_pct=round(operational_stop_pct, 8),
            operational_stop_risk_dollars=round(operational_stop_dollars, 8), conviction_stop_risk_pct=round(recommended_pct, 8),
            conviction_stop_risk_dollars=round(conviction_dollars, 8), adaptive_requested_notional=round(adaptive_requested, 8),
            adaptive_constrained_notional=round(constrained, 8), adaptive_quantity=round(adaptive_quantity, 8),
            adaptive_constrained_stop_risk_pct=round(adaptive_stop_pct, 8),
            adaptive_constrained_stop_risk_dollars=round(adaptive_stop_dollars, 8),
            ceilings={name: round(value, 8) for name, value in _ordered_ceilings(ceilings)}, ceiling_path=ceiling_path,
            binding_adaptive_cap=binding, comparison_direction=direction, difference_dollars=round(difference, 8),
            difference_pct=None if (pct := _percentage_difference(difference, operational_constrained)) is None else round(pct, 8),
            displayed_adaptive_ceiling=round(displayed, 8), future_activation_notional=round(future_activation, 8),
            final_operational_notional=round(final_operational, 8), final_operational_quantity=round(final_quantity, 8),
            final_revalidation_outcome=final_outcome,
            proposal_to_approval_drift_dollars=None if drift_dollars is None else round(drift_dollars, 8),
            proposal_to_approval_drift_pct=None if drift_pct is None else round(drift_pct, 8), confidence=round(confidence, 8),
            missing_inputs=missing, contradictions=sorted(set(contradictions)),
            hypothetical_portfolio_heat_pct=None if hypothetical_heat is None else round(hypothetical_heat, 8),
            hypothetical_gross_exposure_pct=None if hypothetical_gross is None else round(hypothetical_gross, 8),
            hypothetical_symbol_exposure_pct=None if hypothetical_symbol is None else round(hypothetical_symbol, 8),
            hypothetical_cluster_exposure_pct=None if hypothetical_cluster is None else round(hypothetical_cluster, 8),
            would_exceed_hard_limit=would_exceed, raw_inputs=persisted_raw, evidence_version=EVIDENCE_VERSION,
            formula_version=ADAPTIVE_SIZING_FORMULA_VERSION, schema_version=ADAPTIVE_SIZING_SCHEMA_VERSION,
            configuration_version=CONFIGURATION_SCHEMA_VERSION, sizing_policy_version=SIZING_POLICY_VERSION,
            stop_policy_version=STOP_POLICY_VERSION, config_hash=self.config.get("effective_config_hash"),
            decision_fingerprint=fingerprint, operating_mode="operational_paper", operational_enforced=True, report_only=False,
        )

    @staticmethod
    def persist(storage: Any, decision: AdaptiveSizingDecision) -> None:
        values = asdict(decision)
        for name in ("ceilings", "ceiling_path", "missing_inputs", "contradictions", "raw_inputs"):
            values[name + "_json"] = json_dumps(values.pop(name))
        values["would_exceed_hard_limit"] = int(values["would_exceed_hard_limit"])
        values["operational_enforced"] = 1
        values["report_only"] = 0
        columns = list(values)
        storage.execute(
            f"INSERT OR IGNORE INTO adaptive_sizing_operational_decisions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values[name] for name in columns),
        )

    @staticmethod
    def format_report(storage: Any, *, limit: int = 100) -> str:
        rows = storage.fetch_all(
            """SELECT stage,comparison_direction,operational_constrained_notional,adaptive_constrained_notional,final_operational_notional,
                      binding_adaptive_cap,final_revalidation_outcome
               FROM adaptive_sizing_operational_decisions ORDER BY created_at DESC,id DESC LIMIT ?""",
            (int(limit),),
        )
        if not rows:
            return "Adaptive Sizing (operational paper): no persisted operational decisions."
        latest = rows[0]
        directions = Counter(str(row["comparison_direction"]) for row in rows)
        finals = Counter(str(row["final_revalidation_outcome"]) for row in rows if row.get("final_revalidation_outcome"))
        return (
            "Adaptive Sizing (operational paper)\n"
            f"Latest: baseline ${float(latest['operational_constrained_notional']):,.2f}; adaptive operational ${float(latest['final_operational_notional']):,.2f}; "
            f"{latest['comparison_direction']}; binding {latest['binding_adaptive_cap']}.\n"
            f"Last {len(rows)}: comparisons {dict(sorted(directions.items()))}; final drift {dict(sorted(finals.items()))}.\n"
            "Phase 3 hard limits, PROBE limits, displayed approval ceiling, reservations, and final one-way reduction remain authoritative."
        )


def evidence_report(conn: Any) -> dict[str, Any]:
    """Aggregate naturally collected decisions using a caller-owned read-only connection."""
    rows = [dict(row) for row in conn.execute("SELECT * FROM adaptive_sizing_operational_decisions ORDER BY created_at,id")]
    historical_shadow_count = int(conn.execute("SELECT COUNT(*) FROM adaptive_sizing_decisions").fetchone()[0])
    complete = [row for row in rows if not json.loads(row["missing_inputs_json"] or "[]")]
    conviction_ids = [row["adaptive_conviction_decision_id"] for row in rows if row.get("adaptive_conviction_decision_id")]
    conviction: dict[str, dict[str, Any]] = {}
    if conviction_ids:
        placeholders = ",".join("?" for _ in conviction_ids)
        conviction = {
            row["id"]: dict(row)
            for row in conn.execute(
                f"SELECT id,deployment_mode,opportunity_class FROM adaptive_conviction_operational_decisions WHERE id IN ({placeholders})",
                tuple(conviction_ids),
            )
        }
    differences = sorted(abs(float(row["difference_dollars"] or 0.0)) for row in rows)
    drift = [float(row["proposal_to_approval_drift_dollars"]) for row in rows if row["proposal_to_approval_drift_dollars"] is not None]
    missing = Counter()
    contradictions = Counter()
    for row in rows:
        missing.update(json.loads(row["missing_inputs_json"] or "[]"))
        contradictions.update(json.loads(row["contradictions_json"] or "[]"))
    exposure_keys = (
        "hypothetical_portfolio_heat_pct", "hypothetical_gross_exposure_pct",
        "hypothetical_symbol_exposure_pct", "hypothetical_cluster_exposure_pct",
    )
    return {
        "report_only": False,
        "operating_mode": "operational_paper",
        "historical_shadow_decisions": historical_shadow_count,
        "total_decisions": len(rows),
        "complete_counts": dict(Counter(row["stage"] for row in complete)),
        "deployment_modes": dict(Counter(conviction.get(row["adaptive_conviction_decision_id"], {}).get("deployment_mode", "UNKNOWN") for row in rows)),
        "opportunity_classes": dict(Counter(conviction.get(row["adaptive_conviction_decision_id"], {}).get("opportunity_class", "UNKNOWN") for row in rows)),
        "comparison_directions": dict(Counter(row["comparison_direction"] for row in rows)),
        "median_absolute_size_difference": statistics.median(differences) if differences else 0.0,
        "maximum_absolute_size_difference": max(differences, default=0.0),
        "binding_cap_frequency": dict(Counter(row["binding_adaptive_cap"] for row in rows)),
        "proposal_to_approval_drift": {
            "count": len(drift), "median_dollars": statistics.median(drift) if drift else 0.0,
            "maximum_absolute_dollars": max((abs(value) for value in drift), default=0.0),
            "outcomes": dict(Counter(row["final_revalidation_outcome"] for row in rows if row["final_revalidation_outcome"])),
        },
        "missing_input_frequency": dict(missing),
        "contradictory_classifications": dict(contradictions),
        "hypothetical_exposure": {
            key: {"median": statistics.median(values) if values else None, "maximum": max(values, default=None)}
            for key in exposure_keys
            for values in [[float(row[key]) for row in rows if row[key] is not None]]
        },
        "recommendations_exceeding_hard_limit": sum(int(row["would_exceed_hard_limit"] or 0) for row in rows),
        "trading_state_mutations": 0,
    }


def trading_state_counts(conn: Any, tables: Sequence[str] = TRADING_STATE_TABLES) -> dict[str, int]:
    return {table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in tables}


__all__ = [
    "AdaptiveSizingDecision", "AdaptiveSizingEngine", "CANONICAL_CEILING_ORDER", "COMPARISON_DIRECTIONS",
    "FINAL_REVALIDATION_OUTCOMES", "apply_adaptive_sizing_schema", "evidence_report", "trading_state_counts",
]
