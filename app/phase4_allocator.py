from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

import numpy as np

from .execution import DurableExecutionStore
from .evidence import OPERATIONAL_EVIDENCE_TYPES, SHADOW_OUTCOME, classify_evidence_type
from .formula_versions import EVIDENCE_VERSION, PHASE4_ALLOCATION_VERSION, PHASE4_ALLOCATOR_VERSION, PHASE4_SCHEMA_VERSION
from .shadow_strategies import STRATEGY_VERSIONS
from .strategy_execution_registry import StrategyExecutionRegistry, StrategyRegistryEvaluation
from .strategy_rule_based import STRATEGY_VERSION
from .utils import iso_now, json_dumps


ALLOCATOR_VERSION = PHASE4_ALLOCATOR_VERSION
ESTIMATOR_VERSION = "shrunk_oos_estimator_v1"
COVARIANCE_VERSION = "ledoit_wolf_style_shrinkage_v1"
# Compatibility monitoring universe.  Execution authority no longer comes from
# this tuple; configured builds obtain it exclusively from
# StrategyExecutionRegistry.  The alias remains because Phase 2/legacy reports
# and tests import it as the complete research universe.
STRATEGIES = (STRATEGY_VERSION, *tuple(sorted(STRATEGY_VERSIONS.values())))


def operational_risk_budget_multiplier(allocation_weight: float, max_strategy_weight: float) -> float:
    """Convert Phase 4 sleeve fractions into a unitless risk multiplier.

    Both inputs are portfolio allocation fractions in ``[0, 1]``; neither is
    a stop-risk percentage. Normalising by the configured strategy maximum
    yields a multiplier in ``[0, 1]``. Adaptive Conviction is therefore the
    only component that can expand the resulting strategy risk, and its Phase
    3 hard ceiling remains 0.35% of equity.
    """
    try:
        weight = float(allocation_weight)
        maximum = float(max_strategy_weight)
    except (TypeError, ValueError) as exc:
        raise ValueError("Phase 4 allocation fractions must be finite numbers") from exc
    if not math.isfinite(weight) or not math.isfinite(maximum) or maximum <= 0 or maximum > 1:
        raise ValueError("Phase 4 allocation fractions must use the [0, 1] fraction scale")
    return max(0.0, min(1.0, weight / maximum))


def candidate_allocation_rank(inputs: Mapping[str, Any]) -> dict[str, float]:
    """Deterministic evidence-aware candidate rank; never an order quantity."""
    def unit(value: Any, *, scale: float = 1.0, default: float = 0.5) -> float:
        try:
            number = float(value) / scale
        except (TypeError, ValueError):
            return default
        return max(0.0, min(1.0, number)) if math.isfinite(number) else default

    setup = unit(inputs.get("setup_score"), scale=100.0)
    evidence = unit(inputs.get("evidence_quality"), scale=100.0)
    adaptive_conviction = unit(
        inputs.get("adaptive_conviction_score", inputs.get("conviction_score", inputs.get("setup_score"))),
        scale=100.0,
    )
    regime = {
        "favorable": 1.0, "normal": 0.80, "too quiet": 0.55,
        "elevated": 0.40, "high": 0.15, "extreme": 0.0,
    }.get(str(inputs.get("regime") or "").lower(), 0.50)
    fill_rate = unit(inputs.get("execution_fill_rate"), default=0.50)
    shortfall = unit(inputs.get("execution_shortfall_bps"), scale=50.0, default=0.50)
    spread = unit(inputs.get("spread_bps"), scale=50.0, default=0.50)
    liquidity = unit(inputs.get("average_dollar_volume"), scale=10_000_000.0, default=0.50)
    execution = statistics.fmean((fill_rate, 1.0 - shortfall, 1.0 - spread, liquidity))
    conservative_return = inputs.get("conservative_expected_return")
    try:
        expected_value = 0.5 + 0.5 * math.tanh(float(conservative_return) * 10.0)
    except (TypeError, ValueError):
        expected_value = 0.50
    uncertainty = unit(inputs.get("uncertainty"), default=1.0)
    deterioration = unit(inputs.get("deterioration_score"), default=0.0)
    symbol_exposure = unit(inputs.get("symbol_exposure_pct"), scale=6.0, default=1.0)
    cluster_exposure = unit(inputs.get("cluster_exposure_pct"), scale=15.0, default=1.0)
    correlation = unit(inputs.get("correlation_score"), default=0.0)
    marginal_risk = unit(inputs.get("marginal_portfolio_risk"), default=0.0)
    diversification = 1.0 - max(symbol_exposure, cluster_exposure, correlation, marginal_risk)
    risk_consumption = unit(inputs.get("stop_risk_pct"), scale=0.35, default=1.0)
    stop_quality = unit(inputs.get("stop_quality"), scale=100.0, default=0.50)
    reward_to_risk = unit(inputs.get("reward_to_risk"), scale=3.0, default=0.50)
    risk_efficiency = statistics.fmean((1.0 - 0.50 * risk_consumption, stop_quality, reward_to_risk))
    conviction = statistics.fmean((adaptive_conviction, setup, evidence, regime, execution))
    score = 100.0 * (
        0.30 * conviction + 0.20 * expected_value + 0.20 * diversification
        + 0.15 * execution + 0.15 * risk_efficiency
    )
    score -= 15.0 * uncertainty + 30.0 * deterioration
    return {
        "ranking_score": round(max(0.0, score), 8),
        "conviction_score": round(conviction * 100.0, 8),
        "expected_value_score": round(expected_value * 100.0, 8),
        "diversification_score": round(diversification * 100.0, 8),
        "regime_alignment_score": round(regime * 100.0, 8),
        "execution_quality_score": round(execution * 100.0, 8),
        "risk_efficiency_score": round(risk_efficiency * 100.0, 8),
    }


def _finite_nonnegative(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0.0 else default


def _candidate_identity(candidate: Mapping[str, Any]) -> str:
    explicit = candidate.get("candidate_id") or candidate.get("id")
    if explicit:
        return f"{candidate.get('strategy_version') or ''}|{explicit}"
    return "|".join(
        str(candidate.get(name) or "")
        for name in ("strategy_version", "symbol", "action", "setup_id", "milestone")
    )


def allocate_candidates_to_sleeves(
    candidates: Sequence[Mapping[str, Any]],
    sleeves: Mapping[str, Mapping[str, Any]],
    *,
    global_available_risk: float | None = None,
    precision: int = 8,
) -> dict[str, Any]:
    """Allocate ranked entry/add candidates inside pre-authorized sleeves.

    The function is deterministic and side-effect free.  Exits bypass entry
    risk competition.  Entry and ADD risk can consume only the lesser of the
    strategy's remaining sleeve and the global remaining budget.  It returns
    exact replay inputs rather than an order quantity or execution decision.
    """
    normalized_sleeves = {
        str(strategy): {
            **dict(sleeve),
            "remaining_risk": round(
                _finite_nonnegative(sleeve.get("remaining_risk"), 0.0) or 0.0,
                precision,
            ),
        }
        for strategy, sleeve in sorted(sleeves.items(), key=lambda item: str(item[0]))
    }
    sleeve_capacity = round(
        sum(float(sleeve["remaining_risk"]) for sleeve in normalized_sleeves.values()),
        precision,
    )
    requested_global = _finite_nonnegative(global_available_risk, sleeve_capacity)
    global_remaining = round(min(sleeve_capacity, requested_global or 0.0), precision)

    ranked: list[tuple[float, str, dict[str, Any], dict[str, float]]] = []
    exits: list[tuple[str, dict[str, Any]]] = []
    for raw in candidates:
        candidate = dict(raw)
        identity = _candidate_identity(candidate)
        action = str(candidate.get("action") or "entry").lower()
        side = str(candidate.get("side") or "buy").lower()
        if action in {"exit", "reduce", "sell"} or side == "sell":
            exits.append((identity, candidate))
            continue
        rank = candidate_allocation_rank(candidate)
        ranked.append((-rank["ranking_score"], identity, candidate, rank))
    ranked.sort(key=lambda item: (item[0], item[1]))
    exits.sort(key=lambda item: item[0])

    decisions: list[dict[str, Any]] = []
    for identity, candidate in exits:
        decisions.append({
            "candidate_id": identity,
            "strategy_version": str(candidate.get("strategy_version") or ""),
            "symbol": str(candidate.get("symbol") or ""),
            "action": str(candidate.get("action") or "exit").lower(),
            "decision": "EXIT_BYPASS",
            "requested_risk": 0.0,
            "allocated_risk": 0.0,
            "ranking_score": None,
            "reason": "exits do not compete for entry risk sleeves",
        })

    allocated_by_strategy = {strategy: 0.0 for strategy in normalized_sleeves}
    for _negative_score, identity, candidate, rank in ranked:
        strategy = str(candidate.get("strategy_version") or "")
        requested = next(
            (
                _finite_nonnegative(candidate.get(name))
                for name in (
                    "requested_stop_risk",
                    "requested_risk",
                    "stop_risk_dollars",
                    "stop_risk_pct",
                )
                if _finite_nonnegative(candidate.get(name)) is not None
            ),
            None,
        )
        sleeve = normalized_sleeves.get(strategy)
        reason = "allocated within strategy sleeve and global risk budget"
        allocated = 0.0
        decision = "REJECT"
        if sleeve is None:
            reason = "strategy has no authorized risk sleeve"
        elif requested is None or requested <= 0.0:
            reason = "positive finite requested stop risk is required"
        elif sleeve["remaining_risk"] <= 0.0:
            reason = "strategy sleeve is exhausted"
        elif global_remaining <= 0.0:
            reason = "global Phase 3 available risk is exhausted"
        else:
            allocated = round(
                min(float(requested), float(sleeve["remaining_risk"]), global_remaining),
                precision,
            )
            minimum = _finite_nonnegative(candidate.get("minimum_stop_risk"), 0.0) or 0.0
            if allocated + 10 ** (-(precision + 1)) < minimum:
                allocated = 0.0
                reason = "remaining capacity is below the candidate minimum stop risk"
            elif allocated > 0.0:
                decision = "ALLOCATE" if allocated >= float(requested) - 10 ** (-precision) else "ALLOCATE_PARTIAL"
                if decision == "ALLOCATE_PARTIAL":
                    reason = "candidate was reduced to remaining sleeve or global risk capacity"
                sleeve["remaining_risk"] = round(float(sleeve["remaining_risk"]) - allocated, precision)
                global_remaining = round(global_remaining - allocated, precision)
                allocated_by_strategy[strategy] = round(allocated_by_strategy[strategy] + allocated, precision)
        decisions.append({
            "candidate_id": identity,
            "strategy_version": strategy,
            "symbol": str(candidate.get("symbol") or ""),
            "action": str(candidate.get("action") or "entry").lower(),
            "decision": decision,
            "requested_risk": requested,
            "allocated_risk": allocated,
            "ranking_score": rank["ranking_score"],
            "rank_components": rank,
            "reason": reason,
        })

    allocated_total = round(sum(allocated_by_strategy.values()), precision)
    starting_global = round(global_remaining + allocated_total, precision)
    replay_inputs = {
        "candidates": [dict(candidate) for candidate in candidates],
        "sleeves": {strategy: dict(sleeve) for strategy, sleeve in sorted(sleeves.items())},
        "global_available_risk": requested_global,
        "precision": precision,
    }
    return {
        "decisions": decisions,
        "allocated_by_strategy": allocated_by_strategy,
        "allocated_risk": allocated_total,
        "global_budget": starting_global,
        "global_remaining_risk": global_remaining,
        "sleeves_after": normalized_sleeves,
        "reconciliation_residual": round(starting_global - allocated_total - global_remaining, precision),
        "raw_replay_inputs": replay_inputs,
        "fingerprint": _fingerprint({"inputs": replay_inputs, "decisions": decisions}),
    }


@dataclass(frozen=True)
class StrategyEstimate:
    strategy_version: str
    sample_n: int
    regime_n: int
    mean_return: float | None
    shrunk_mean_return: float | None
    conservative_expected_return: float | None
    calibrated_positive_probability: float | None
    standard_error: float | None
    uncertainty: float
    data_quality: float
    deterioration_score: float
    state: str
    reason: str
    evidence_class: str


def apply_phase4_schema(conn: Any, *, record_migration: bool = True) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS phase4_strategy_estimates(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
      estimated_at TEXT NOT NULL, sample_n INTEGER NOT NULL, regime_n INTEGER NOT NULL,
      mean_return REAL, shrunk_mean_return REAL, conservative_expected_return REAL,
      calibrated_positive_probability REAL, standard_error REAL, uncertainty REAL NOT NULL,
      data_quality REAL NOT NULL, deterioration_score REAL NOT NULL, state TEXT NOT NULL,
      reason TEXT NOT NULL, estimator_version TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
      payload TEXT NOT NULL, UNIQUE(run_id,strategy_version));
    CREATE TABLE IF NOT EXISTS phase4_covariance_snapshots(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, calculated_at TEXT NOT NULL,
      strategy_order_json TEXT NOT NULL, covariance_json TEXT NOT NULL,
      correlation_json TEXT NOT NULL, observation_counts_json TEXT NOT NULL,
      method TEXT NOT NULL, fallback_used INTEGER NOT NULL, data_quality REAL NOT NULL,
      payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS phase4_allocation_decisions(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, decided_at TEXT NOT NULL,
      mode TEXT NOT NULL CHECK(mode='ACTIVE_ADAPTIVE_PAPER'), allocator_version TEXT NOT NULL,
      strategy_weights_json TEXT NOT NULL, cash_weight REAL NOT NULL,
      fractional_kelly_ceiling REAL NOT NULL, expected_portfolio_return REAL,
      portfolio_volatility REAL, expected_shortfall REAL, stress_loss REAL,
      marginal_risk_json TEXT NOT NULL, component_risk_json TEXT NOT NULL,
      regime TEXT NOT NULL, drawdown_pct REAL NOT NULL, uncertainty_penalty REAL NOT NULL,
      data_quality REAL NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
      allocation_class TEXT NOT NULL DEFAULT 'unallocated', operational_kelly_used INTEGER NOT NULL DEFAULT 0,
      kelly_diagnostic_json TEXT, adaptive_allocation_json TEXT, exploration_allocation_json TEXT,
      unallocated_risk_pct REAL NOT NULL DEFAULT 0,
      heat_before_pct REAL, heat_after_pct REAL, gross_exposure_before_pct REAL, gross_exposure_after_pct REAL,
      symbol_exposure_before_json TEXT, symbol_exposure_after_json TEXT,
      cluster_exposure_before_json TEXT, cluster_exposure_after_json TEXT,
      pending_risk REAL, reserved_risk REAL, binding_caps_json TEXT, evidence_versions_json TEXT,
      evidence_fingerprint TEXT NOT NULL, formula_version TEXT, config_hash TEXT, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS phase4_stress_results(
      id TEXT PRIMARY KEY, allocation_id TEXT NOT NULL, scenario TEXT NOT NULL,
      assumed_loss REAL NOT NULL, portfolio_loss REAL NOT NULL, passed INTEGER NOT NULL,
      stress_version TEXT NOT NULL, payload TEXT NOT NULL,
      UNIQUE(allocation_id,scenario));
    CREATE TABLE IF NOT EXISTS phase4_strategy_states(
      strategy_version TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
      estimate_id TEXT NOT NULL, state_version TEXT NOT NULL, evaluated_at TEXT NOT NULL,
      activated_at TEXT, throttled_at TEXT, suspended_at TEXT, recovered_at TEXT,
      payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS phase4_activation_events(
      id TEXT PRIMARY KEY, release_commit TEXT NOT NULL, activated_at TEXT NOT NULL,
      status TEXT NOT NULL, allocation_id TEXT NOT NULL, paper_identity_json TEXT NOT NULL,
      account_json TEXT NOT NULL, integrity_json TEXT NOT NULL, profile_version TEXT NOT NULL);
    """
    for statement in sql.split(";"):
        if statement.strip(): conn.execute(statement)
    additions = {
        "allocation_class": "TEXT DEFAULT 'unallocated'", "operational_kelly_used": "INTEGER NOT NULL DEFAULT 0",
        "kelly_diagnostic_json": "TEXT", "adaptive_allocation_json": "TEXT", "exploration_allocation_json": "TEXT",
        "unallocated_risk_pct": "REAL NOT NULL DEFAULT 0", "heat_before_pct": "REAL", "heat_after_pct": "REAL",
        "gross_exposure_before_pct": "REAL", "gross_exposure_after_pct": "REAL",
        "symbol_exposure_before_json": "TEXT", "symbol_exposure_after_json": "TEXT",
        "cluster_exposure_before_json": "TEXT", "cluster_exposure_after_json": "TEXT",
        "pending_risk": "REAL", "reserved_risk": "REAL", "binding_caps_json": "TEXT", "evidence_versions_json": "TEXT",
        "formula_version": "TEXT", "config_hash": "TEXT", "strategy_policy_map_json": "TEXT", "strategy_policy_version": "TEXT",
        "probe_allocation_json": "TEXT",
    }
    present = {row[1] for row in conn.execute("PRAGMA table_info(phase4_allocation_decisions)")}
    for name, definition in additions.items():
        if name not in present:
            conn.execute(f"ALTER TABLE phase4_allocation_decisions ADD COLUMN {name} {definition}")
    if record_migration:
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
                     (PHASE4_SCHEMA_VERSION, iso_now(), "additive Phase 4 estimates, covariance, allocations, stress, states, and activation"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class AdaptiveAllocator:
    def __init__(
        self,
        storage: Any,
        config: Mapping[str, Any],
        run_id: str,
        *,
        available_implementations: Mapping[str, str] | None = None,
        registry: StrategyExecutionRegistry | None = None,
    ) -> None:
        self.storage, self.config, self.run_id = storage, config, run_id
        self.cfg = config.get("phase4", {})
        self.available_implementations = dict(available_implementations or {})
        self.registry = registry
        self._operational_strategy_set: frozenset[str] = frozenset()
        self._last_covariance_payload: dict[str, Any] = {}
        self._validate()

    def _validate(self) -> None:
        if self.cfg.get("mode") != "ACTIVE_ADAPTIVE_PAPER":
            raise ValueError("Phase 4 mode must be ACTIVE_ADAPTIVE_PAPER")
        fraction = float(self.cfg.get("fractional_kelly", 0))
        if not 0 < fraction <= 0.25:
            raise ValueError("fractional Kelly must be positive and no greater than one quarter")
        if self.cfg.get("full_kelly_allowed") is not False:
            raise ValueError("full Kelly is forbidden")
        if self.cfg.get("llm_trading_decisions") is not False:
            raise ValueError("LLM trading decisions are forbidden")
        if self.cfg.get("operational_kelly_enabled") is not False:
            raise ValueError("operational Kelly must remain disabled")
        if self.cfg.get("operational_allocation_mode") != "bounded_evidence_aware":
            raise ValueError("operational allocation must be bounded evidence-aware")
        if self.cfg.get("allocator_version") != ALLOCATOR_VERSION:
            raise ValueError(f"allocator version must be {ALLOCATOR_VERSION}")

    def _strategy_order(self, policies: Mapping[str, Any] | None) -> tuple[str, ...]:
        registry_cfg = self.config.get("strategy_execution_registry")
        # Compatibility for isolated pre-registry callers.  Production passes
        # an explicit (possibly empty) policy map and therefore always takes
        # the fail-closed registry path.
        if not isinstance(registry_cfg, Mapping) or policies is None:
            return STRATEGIES
        entries = registry_cfg.get("entries", {})
        configured = set(entries) if isinstance(entries, Mapping) else set()
        configured.update(str(key) for key in (policies or {}))
        configured.update(STRATEGIES)
        return tuple(sorted(configured))

    def _registry_evaluation(
        self,
        policies: Mapping[str, Any] | None,
        *,
        as_of: str,
    ) -> tuple[StrategyRegistryEvaluation | None, tuple[str, ...], dict[str, str], dict[str, Any]]:
        registry_cfg = self.config.get("strategy_execution_registry")
        if not isinstance(registry_cfg, Mapping) or policies is None:
            rejected = {strategy: "shadow/research strategy cannot receive executable allocation" for strategy in STRATEGIES if strategy != STRATEGY_VERSION}
            payload = {
                "mode": "legacy_compatibility_without_registry_config",
                "authorized_versions": [STRATEGY_VERSION],
                "rejected": rejected,
            }
            payload["fingerprint"] = _fingerprint(payload)
            return None, (STRATEGY_VERSION,), rejected, payload

        inventory = dict(self.available_implementations)
        configured_inventory = self.config.get("strategy_implementation_inventory", {})
        if isinstance(configured_inventory, Mapping):
            inventory.update({str(key): str(value) for key, value in configured_inventory.items()})
        # The imported rule implementation is the only built-in executable in
        # this module.  Its configured identifier/version can therefore be
        # proven locally; all additional implementations require an explicit
        # runtime inventory passed by the caller.
        entries = registry_cfg.get("entries", {})
        rule_entry = entries.get(STRATEGY_VERSION, {}) if isinstance(entries, Mapping) else {}
        if isinstance(rule_entry, Mapping) and rule_entry.get("implementation_available") is True:
            identifier = rule_entry.get("implementation_id")
            version = rule_entry.get("implementation_version")
            if identifier and version:
                inventory.setdefault(str(identifier), str(version))
        engine = self.registry or StrategyExecutionRegistry(
            self.config, available_implementations=inventory
        )
        evaluation = engine.evaluate(policies or {}, as_of=as_of)
        rejected = {decision.strategy_version: decision.reason for decision in evaluation.rejected}
        return evaluation, evaluation.authorized_versions, rejected, evaluation.as_dict()

    def _rows(self, strategy: str) -> list[dict[str, Any]]:
        rows = self.storage.fetch_all("""SELECT ro.id,ro.regime,ro.split_label,ro.execution_type,ro.source_table,
          ro.provenance_json,r.exit_session,r.cost_adjusted_return,r.gross_return,r.cost_bps,r.calculated_at
          FROM research_opportunities ro JOIN research_outcomes r ON r.opportunity_id=ro.id
          WHERE ro.strategy_version=? AND ro.split_label='out_of_sample' AND r.horizon_sessions=20
            AND r.status='completed' AND r.cost_adjusted_return IS NOT NULL
            AND r.calculation_version=?
            ORDER BY r.exit_session,ro.id""", (strategy, EVIDENCE_VERSION))
        allowed = OPERATIONAL_EVIDENCE_TYPES if strategy in self._operational_strategy_set else {SHADOW_OUTCOME}
        return [row for row in rows if classify_evidence_type(
            row.get("execution_type"), row.get("source_table"), row.get("provenance_json")
        ) in allowed]

    def _is_stale(self, rows: Sequence[Mapping[str, Any]]) -> bool:
        if not rows:
            return False
        latest = max((str(row.get("calculated_at") or "") for row in rows), default="")
        if not latest:
            return False
        try:
            timestamp = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
            age_days = (datetime.now(UTC) - timestamp).total_seconds() / 86400.0
            return age_days > float(self.cfg.get("evidence_stale_after_days", 90))
        except (TypeError, ValueError, OverflowError):
            return True

    def estimate(self, strategy: str) -> tuple[StrategyEstimate, list[dict[str, Any]], str]:
        rows = self._rows(strategy)
        values = [float(row["cost_adjusted_return"]) for row in rows if math.isfinite(float(row["cost_adjusted_return"]))]
        regimes = {str(row.get("regime")) for row in rows if row.get("regime")}
        fp = _fingerprint(rows)
        minimum = int(self.cfg.get("minimum_oos_samples", 100))
        min_regimes = int(self.cfg.get("minimum_regimes", 2))
        if not values:
            return StrategyEstimate(
                strategy, 0, 0, None, None, None, None, None, 1.0, 0.0, 1.0,
                "EXPLORATION", "insufficient evidence: no completed OOS evidence; bounded exploration permitted",
                "insufficient",
            ), rows, fp
        mean = statistics.fmean(values)
        sd = statistics.stdev(values) if len(values) > 1 else 0.0
        se = sd / math.sqrt(len(values)) if len(values) > 1 else None
        prior_strength = float(self.cfg.get("shrinkage_prior_samples", 100))
        shrunk = mean * len(values) / (len(values) + prior_strength)
        conservative = shrunk - float(self.cfg.get("confidence_z", 1.96)) * (se or abs(mean) or 1.0)
        wins = sum(value > 0 for value in values)
        calibrated_p = (wins + 10.0) / (len(values) + 20.0)
        recent = values[-max(5, min(20, len(values) // 3 or 1)):]
        earlier = values[:-len(recent)]
        deterioration = max(0.0, (statistics.fmean(earlier) - statistics.fmean(recent)) / (sd or 1.0)) if earlier else 0.0
        quality = min(1.0, len(values) / minimum) * min(1.0, len(regimes) / min_regimes)
        quality *= 1.0 if all(row.get("cost_bps") is not None for row in rows) else 0.5
        uncertainty = min(1.0, (se or 1.0) / (abs(shrunk) + 1e-9))
        if self._is_stale(rows):
            state, reason, evidence_class = "SUSPENDED", "stale OOS evidence", "stale"
        elif deterioration >= float(self.cfg.get("deterioration_suspend_z", 2.0)):
            state, reason, evidence_class = "SUSPENDED", "statistically material recent deterioration", "deteriorating"
        elif mean <= 0 or calibrated_p <= 0.5:
            state, reason, evidence_class = "SUSPENDED", "negative cost-adjusted evidence", "negative"
        elif len(values) < minimum or len(regimes) < min_regimes:
            state, reason, evidence_class = "EXPLORATION", "insufficient OOS sample or regime coverage; bounded exploration permitted", "insufficient"
        elif conservative <= 0:
            state, reason, evidence_class = "THROTTLED", "positive point estimate but uncertainty is too high for adaptive allocation", "insufficient"
        else:
            state, reason, evidence_class = "ACTIVE", "conservative OOS evidence passed", "qualified"
        return StrategyEstimate(
            strategy, len(values), len(regimes), mean, shrunk, conservative, calibrated_p, se,
            uncertainty, quality, deterioration, state, reason, evidence_class,
        ), rows, fp

    def covariance(
        self,
        evidence: Mapping[str, list[dict[str, Any]]],
        strategy_order: Sequence[str] | None = None,
    ) -> tuple[np.ndarray, bool, dict[str, int]]:
        order = tuple(strategy_order if strategy_order is not None else sorted(evidence))
        if len(order) != len(set(order)):
            raise ValueError("covariance strategy order must be unique")
        n = len(order)
        matrix = np.zeros((n, n), dtype=float)
        counts: dict[str, int] = {}
        normalized: dict[str, dict[str, float]] = {}
        raw_rows: dict[str, list[dict[str, Any]]] = {}
        invalid_rows: dict[str, list[dict[str, Any]]] = {}
        fallback_reasons: list[str] = []
        default_var = _finite_nonnegative(self.cfg.get("fallback_annual_variance"), 0.04) or 0.04
        fallback_corr = _finite_nonnegative(self.cfg.get("covariance_fallback_correlation"), 0.5)
        fallback_corr = min(0.95, fallback_corr if fallback_corr is not None else 0.5)

        for strategy in order:
            by_date: dict[str, list[float]] = {}
            raw_rows[strategy], invalid_rows[strategy] = [], []
            for row in sorted(evidence.get(strategy, []), key=lambda item: (str(item.get("exit_session") or ""), str(item.get("id") or ""))):
                date = str(row.get("exit_session") or "")
                try:
                    signed = float(row.get("cost_adjusted_return"))
                except (TypeError, ValueError):
                    signed = math.nan
                normalized_row = {"id": row.get("id"), "exit_session": date, "cost_adjusted_return": row.get("cost_adjusted_return")}
                if not date or not math.isfinite(signed):
                    invalid_rows[strategy].append(normalized_row)
                    continue
                raw_rows[strategy].append({**normalized_row, "cost_adjusted_return": signed})
                by_date.setdefault(date, []).append(signed)
            normalized[strategy] = {date: statistics.fmean(values) for date, values in sorted(by_date.items())}
            counts[strategy] = len(normalized[strategy])
            if invalid_rows[strategy]:
                fallback_reasons.append(f"{strategy}:invalid_or_unaligned_return_rows")

        pairwise: dict[str, list[dict[str, Any]]] = {}
        for i, first in enumerate(order):
            values = list(normalized[first].values())
            if len(values) >= 2:
                variance = float(np.var(values, ddof=1))
                matrix[i, i] = variance if math.isfinite(variance) and variance > 0 else default_var
                if matrix[i, i] == default_var:
                    fallback_reasons.append(f"{first}:invalid_variance")
            else:
                matrix[i, i] = default_var
                fallback_reasons.append(f"{first}:insufficient_variance_observations")
            for j in range(i):
                second = order[j]
                common = sorted(set(normalized[first]) & set(normalized[second]))
                aligned = [
                    {"exit_session": date, first: normalized[first][date], second: normalized[second][date]}
                    for date in common
                ]
                pairwise[f"{second}|{first}"] = aligned
                if len(common) >= 5:
                    covariance = float(np.cov(
                        [normalized[first][date] for date in common],
                        [normalized[second][date] for date in common],
                        ddof=1,
                    )[0, 1])
                else:
                    covariance = fallback_corr * math.sqrt(matrix[i, i] * matrix[j, j])
                    fallback_reasons.append(f"{second}|{first}:conservative_missing_covariance")
                if not math.isfinite(covariance):
                    covariance = fallback_corr * math.sqrt(matrix[i, i] * matrix[j, j])
                    fallback_reasons.append(f"{second}|{first}:non_finite_covariance")
                matrix[i, j] = matrix[j, i] = covariance

        shrink = _finite_nonnegative(self.cfg.get("covariance_shrinkage"), 0.5)
        shrink = min(1.0, shrink if shrink is not None else 0.5)
        if n:
            matrix = (1.0 - shrink) * matrix + shrink * np.diag(np.diag(matrix))
            matrix = (matrix + matrix.T) / 2.0
        finite_before = bool(np.isfinite(matrix).all())
        if not finite_before or matrix.shape != (n, n):
            matrix = np.full((n, n), fallback_corr * default_var, dtype=float)
            np.fill_diagonal(matrix, default_var)
            fallback_reasons.append("matrix_dimension_or_finite_validation_failed")
        eigenvalues_before = np.linalg.eigvalsh(matrix).tolist() if n else []
        if n and min(eigenvalues_before) < -1e-12:
            eigenvalues, vectors = np.linalg.eigh(matrix)
            matrix = vectors @ np.diag(np.maximum(eigenvalues, 1e-12)) @ vectors.T
            matrix = (matrix + matrix.T) / 2.0
            fallback_reasons.append("deterministic_psd_projection")
        eigenvalues_after = np.linalg.eigvalsh(matrix).tolist() if n else []
        self._last_covariance_payload = {
            "strategy_order": list(order),
            "dimensions": [n, n],
            "matrix_finite": bool(np.isfinite(matrix).all()),
            "matrix_symmetric": bool(np.allclose(matrix, matrix.T, atol=1e-12)),
            "matrix_psd": not eigenvalues_after or min(eigenvalues_after) >= -1e-12,
            "eigenvalues_before": eigenvalues_before,
            "eigenvalues_after": eigenvalues_after,
            "shrinkage": shrink,
            "fallback_correlation": fallback_corr,
            "fallback_reasons": sorted(set(fallback_reasons)),
            "raw_strategy_returns": raw_rows,
            "invalid_rows": invalid_rows,
            "aligned_pairwise_inputs": pairwise,
            "observation_counts": counts,
            "evidence_version": EVIDENCE_VERSION,
        }
        return matrix, bool(fallback_reasons), counts

    def _phase3_available_risk(
        self,
        snapshot: Mapping[str, Any],
        drawdown_pct: float,
        phase3_profile: Any,
    ) -> tuple[float, str, dict[str, Any]]:
        explicit = _finite_nonnegative(snapshot.get("phase3_available_risk"))
        explicit_pct = _finite_nonnegative(snapshot.get("phase3_available_risk_pct"))
        if explicit is not None:
            return explicit, str(snapshot.get("phase3_available_risk_unit") or "stop_risk_dollars"), {"source": "phase3_available_risk"}
        if explicit_pct is not None:
            return explicit_pct, "pct_equity", {"source": "phase3_available_risk_pct"}
        maximum = float(getattr(phase3_profile, "max_portfolio_heat_pct", 0.0) or 0.0)
        heat = _finite_nonnegative(snapshot.get("heat_before_pct"), 0.0) or 0.0
        halt = float(getattr(phase3_profile, "drawdown_halt_pct", 6.0) or 6.0)
        multiplier = 0.0 if drawdown_pct >= halt else 0.50 if drawdown_pct >= 4.0 else 0.75 if drawdown_pct >= 2.0 else 1.0
        # This is the total risk capacity to divide into strategy targets.
        # Current held/reserved consumption is subtracted exactly once inside
        # _build_sleeves. Returning a remaining envelope here and subtracting
        # strategy consumption again would double count existing heat.
        capacity = maximum * multiplier
        return capacity, "pct_equity", {
            "source": "phase3_total_heat_capacity_fallback",
            "max_portfolio_heat_pct": maximum,
            "heat_before_pct": heat,
            "drawdown_multiplier": multiplier,
            "global_remaining_after_consumption_pct": max(0.0, capacity - heat),
        }

    def _build_sleeves(
        self,
        authorized: Sequence[str],
        states: Mapping[str, str],
        weights: Mapping[str, float],
        exploration: Mapping[str, float],
        probe: Mapping[str, float],
        snapshot: Mapping[str, Any],
        available_risk: float,
        risk_unit: str,
        *,
        precision: int = 8,
    ) -> tuple[dict[str, dict[str, Any]], float, float]:
        equity = _finite_nonnegative(snapshot.get("portfolio_equity"))

        def pct_cap(value: float) -> float:
            if risk_unit == "pct_equity":
                return value
            return equity * value / 100.0 if equity is not None else 0.0

        desired: dict[str, float] = {}
        caps: dict[str, float] = {}
        for strategy in sorted(authorized):
            state = states[strategy]
            if state == "PROBE":
                caps[strategy] = pct_cap(float(probe.get(strategy, 0.0)))
                desired[strategy] = caps[strategy]
            elif state == "EXPLORATION":
                caps[strategy] = pct_cap(float(exploration.get(strategy, 0.0)))
                desired[strategy] = caps[strategy]
            elif state in {"ACTIVE", "THROTTLED"}:
                caps[strategy] = available_risk * (
                    float(self.cfg.get("max_strategy_weight", 0.35))
                    if state == "ACTIVE" else float(self.cfg.get("throttled_max_strategy_weight", float(self.cfg.get("max_strategy_weight", 0.35)) * 0.5))
                )
                desired[strategy] = min(caps[strategy], available_risk * max(0.0, float(weights.get(strategy, 0.0))))
            else:
                caps[strategy] = desired[strategy] = 0.0
        desired_total = sum(desired.values())
        scale = min(1.0, available_risk / desired_total) if desired_total > 0 else 0.0
        allocated = {strategy: round(value * scale, precision) for strategy, value in desired.items()}
        allocated_total = round(sum(allocated.values()), precision)
        budget = round(available_risk, precision)
        if allocated_total > budget and allocated:
            last = sorted(allocated)[-1]
            allocated[last] = round(max(0.0, allocated[last] - (allocated_total - budget)), precision)
            allocated_total = round(sum(allocated.values()), precision)
        unallocated = round(max(0.0, budget - allocated_total), precision)
        residual = round(budget - allocated_total - unallocated, precision)
        consumption: dict[str, float] = {strategy: 0.0 for strategy in authorized}
        total_map = snapshot.get("strategy_risk_by_strategy")
        if isinstance(total_map, Mapping):
            for strategy in consumption:
                consumption[strategy] = _finite_nonnegative(total_map.get(strategy), 0.0) or 0.0
        else:
            for key in ("held_risk_by_strategy", "pending_risk_by_strategy", "reserved_risk_by_strategy"):
                values = snapshot.get(key, {})
                if isinstance(values, Mapping):
                    for strategy in consumption:
                        consumption[strategy] += _finite_nonnegative(values.get(strategy), 0.0) or 0.0
        sleeves: dict[str, dict[str, Any]] = {}
        notional_consumption = snapshot.get("strategy_notional_by_strategy", {})
        gross_capacity = 0.0
        if equity is not None:
            try:
                gross_capacity = equity * float(snapshot.get("phase3_gross_exposure_capacity_pct") or 0.0) / 100.0
            except (TypeError, ValueError):
                gross_capacity = 0.0
        for strategy in sorted(authorized):
            assigned = allocated[strategy]
            consumed = round(consumption[strategy], precision)
            target_share = round(assigned / budget, precision) if budget else 0.0
            assigned_notional = round(gross_capacity * target_share, precision)
            consumed_notional = round(
                _finite_nonnegative(
                    notional_consumption.get(strategy) if isinstance(notional_consumption, Mapping) else 0.0,
                    0.0,
                ) or 0.0,
                precision,
            )
            remaining = round(max(0.0, assigned - consumed), precision)
            overconsumed = round(max(0.0, consumed - assigned), precision)
            sleeves[strategy] = {
                "strategy_version": strategy,
                "state": states[strategy],
                "risk_unit": risk_unit,
                "target_weight": target_share,
                "state_cap_risk": round(caps[strategy], precision),
                "allocated_risk": assigned,
                "consumed_risk": consumed,
                "remaining_risk": remaining,
                "overconsumed_risk": overconsumed,
                "risk_reconciliation_residual": round(assigned + overconsumed - consumed - remaining, precision),
                "allocated_notional": assigned_notional,
                "consumed_notional": consumed_notional,
                "remaining_notional": round(max(0.0, assigned_notional - consumed_notional), precision),
                "overconsumed_notional": round(max(0.0, consumed_notional - assigned_notional), precision),
            }
        sleeve_residual = round(sum(abs(float(row["risk_reconciliation_residual"])) for row in sleeves.values()), precision)
        return sleeves, unallocated, round(residual + sleeve_residual, precision)

    def run(
        self,
        *,
        regime: str,
        drawdown_pct: float,
        portfolio_snapshot: Mapping[str, Any] | None = None,
        strategy_policy_map: Mapping[str, Any] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        healthy = not any(DurableExecutionStore(self.storage).integrity_report().values())
        now = iso_now()
        snapshot = dict(portfolio_snapshot or {})
        evaluation_time = str(as_of or snapshot.get("as_of") or now)
        registry_eval, authorized_order, registry_rejections, registry_payload = self._registry_evaluation(
            strategy_policy_map, as_of=evaluation_time,
        )
        if not healthy:
            authorized_order = ()
            registry_rejections = {**registry_rejections, "__integrity__": "durable integrity health failed"}
        if snapshot.get("strategy_attribution_complete") is False:
            authorized_order = ()
            registry_rejections = {
                **registry_rejections,
                "__strategy_attribution__": str(
                    snapshot.get("strategy_attribution_reason")
                    or "held strategy attribution is incomplete"
                ),
            }
        self._operational_strategy_set = frozenset(authorized_order)
        strategy_order = self._strategy_order(strategy_policy_map)

        def policy_value(strategy: str, name: str, default: Any = None) -> Any:
            policy = (strategy_policy_map or {}).get(strategy)
            if policy is None:
                return default
            return policy.get(name, default) if isinstance(policy, Mapping) else getattr(policy, name, default)

        estimates: dict[str, StrategyEstimate] = {}
        evidence: dict[str, list[dict[str, Any]]] = {}
        estimate_ids: dict[str, str] = {}
        evidence_fingerprints: dict[str, str] = {}
        for strategy in strategy_order:
            estimate, rows, evidence_fp = self.estimate(strategy)
            evidence[strategy], evidence_fingerprints[strategy] = rows, evidence_fp
            if not healthy:
                estimate = StrategyEstimate(**{**asdict(estimate), "state": "SUSPENDED", "reason": "durable integrity health failed"})
            estimates[strategy] = estimate
            estimate_id = _fingerprint([self.run_id, strategy, evidence_fp])[:32]
            estimate_ids[strategy] = estimate_id
            self.storage.execute(
                "INSERT OR REPLACE INTO phase4_strategy_estimates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (estimate_id, self.run_id, strategy, now, estimate.sample_n, estimate.regime_n, estimate.mean_return,
                 estimate.shrunk_mean_return, estimate.conservative_expected_return,
                 estimate.calibrated_positive_probability, estimate.standard_error, estimate.uncertainty,
                 estimate.data_quality, estimate.deterioration_score, estimate.state, estimate.reason,
                 ESTIMATOR_VERSION, evidence_fp, json_dumps({
                     "cost_adjusted": True, "score_sizing": False, "evidence_class": estimate.evidence_class,
                     "state_version": "phase4_strategy_state_v3_probe",
                 })),
            )
            self._persist_state(estimate, estimate_id, now)

        operational_states: dict[str, str] = {}
        operational_reasons: dict[str, str] = {}
        authorized_set = set(authorized_order)
        rejected_decisions = {
            decision.strategy_version: decision
            for decision in (registry_eval.rejected if registry_eval is not None else ())
        }
        policy_authoritative = strategy_policy_map is not None
        for strategy in strategy_order:
            if strategy in authorized_set:
                if registry_eval is not None:
                    decision = next(item for item in registry_eval.authorized if item.strategy_version == strategy)
                    operational_states[strategy] = decision.policy_state
                    operational_reasons[strategy] = decision.reason
                elif policy_authoritative and policy_value(strategy, "state") in {"RESEARCH_ONLY", "PROBE", "EXPLORATION", "THROTTLED", "ACTIVE", "SUSPENDED"}:
                    operational_states[strategy] = str(policy_value(strategy, "state"))
                    operational_reasons[strategy] = str(policy_value(strategy, "reason", "persisted strategy policy"))
                elif policy_authoritative:
                    operational_states[strategy] = "SUSPENDED"
                    operational_reasons[strategy] = "latest strategy performance policy unavailable or invalid"
                else:
                    operational_states[strategy] = estimates[strategy].state
                    operational_reasons[strategy] = estimates[strategy].reason
            elif strategy in rejected_decisions:
                rejected = rejected_decisions[strategy]
                state = rejected.policy_state
                if not rejected.implementation_available or not rejected.execution_eligible or not rejected.paper_eligible:
                    operational_states[strategy] = "RESEARCH_ONLY"
                else:
                    operational_states[strategy] = state if state in {"RESEARCH_ONLY", "SUSPENDED"} else "SUSPENDED"
                operational_reasons[strategy] = registry_rejections[strategy]
            else:
                operational_states[strategy] = "RESEARCH_ONLY"
                operational_reasons[strategy] = registry_rejections.get(strategy, "strategy is not present in the executable registry")
        for strategy in authorized_order:
            if operational_states[strategy] != estimates[strategy].state or operational_reasons[strategy] != estimates[strategy].reason:
                self._persist_state(
                    StrategyEstimate(**{**asdict(estimates[strategy]), "state": operational_states[strategy], "reason": operational_reasons[strategy]}),
                    estimate_ids[strategy], now,
                )

        current_regime_metrics: dict[str, dict[str, Any]] = {}
        target_regime = str(regime or "").strip().lower()
        minimum_regime_samples = int((self.config.get("profitability_engine", {}) or {}).get("minimum_samples_per_regime", 10))
        for strategy, rows in evidence.items():
            values = [float(row["cost_adjusted_return"]) for row in rows if str(row.get("regime") or "").strip().lower() == target_regime]
            reliable = len(values) >= minimum_regime_samples
            mean = statistics.fmean(values) if values else None
            se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None
            current_regime_metrics[strategy] = {
                "regime": regime, "sample_n": len(values), "reliable": reliable, "mean_return": mean,
                "conservative_expected_return": mean - float(self.cfg.get("confidence_z", 1.96)) * float(se or 0.0) if reliable and mean is not None else None,
            }

        try:
            from .phase3_risk import Phase3RiskProfile
            from .strategy_performance import state_risk_policy
            phase3_profile = Phase3RiskProfile.from_config(self.config)
        except (KeyError, TypeError, ValueError):
            phase3_profile = None
            state_risk_policy = None

        allocation_evidence = {strategy: evidence.get(strategy, []) for strategy in authorized_order}
        covariance, fallback, counts = self.covariance(allocation_evidence, authorized_order)
        diagonal = np.sqrt(np.maximum(np.diag(covariance), 1e-12)) if len(authorized_order) else np.array([])
        correlation = covariance / np.outer(diagonal, diagonal) if len(authorized_order) else np.empty((0, 0))
        if correlation.size and not np.isfinite(correlation).all():
            correlation = np.eye(len(authorized_order))
            fallback = True
            self._last_covariance_payload["fallback_reasons"] = sorted(set(self._last_covariance_payload["fallback_reasons"] + ["non_finite_correlation_replaced"]))
        covariance_payload = {**self._last_covariance_payload, "covariance": covariance.tolist(), "correlation": correlation.tolist()}
        covariance_id = _fingerprint({"run_id": self.run_id, "inputs": covariance_payload})[:32]
        self.storage.execute(
            "INSERT OR REPLACE INTO phase4_covariance_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (covariance_id, self.run_id, now, json_dumps(authorized_order), json_dumps(covariance.tolist()),
             json_dumps(correlation.tolist()), json_dumps(counts), COVARIANCE_VERSION, int(fallback),
             min((estimates[strategy].data_quality for strategy in authorized_order), default=0.0),
             json_dumps(covariance_payload)),
        )

        fraction = float(self.cfg["fractional_kelly"])
        max_weight = float(self.cfg.get("max_strategy_weight", 0.35))
        weights_vector = np.zeros(len(authorized_order))
        kelly_diagnostics: dict[str, float] = {}
        drawdown_penalty = max(0.0, 1.0 - max(0.0, float(drawdown_pct)) / 6.0)
        weighted_states = {"ACTIVE", "THROTTLED"}
        for index, strategy in enumerate(authorized_order):
            state = operational_states[strategy]
            if state not in weighted_states:
                continue
            estimate = estimates[strategy]
            state_cap = max_weight if state == "ACTIVE" else float(self.cfg.get("throttled_max_strategy_weight", max_weight * 0.5))
            ceiling = 0.0
            if estimate.conservative_expected_return is not None and estimate.conservative_expected_return > 0:
                kelly = estimate.conservative_expected_return / max(covariance[index, index], 1e-12) * fraction
                ceiling = min(state_cap, max(0.0, kelly)) * estimate.data_quality * (1.0 - estimate.uncertainty)
            policy_quality = _finite_nonnegative(policy_value(strategy, "quality_score"))
            quality, uncertainty = estimate.data_quality, estimate.uncertainty
            if ceiling <= 0 and estimate.conservative_expected_return is None and policy_authoritative and policy_quality is not None:
                quality = min(1.0, policy_quality / 100.0)
                uncertainty = max(0.50, 1.0 - quality)
                ceiling = state_cap * 0.50
            kelly_diagnostics[strategy] = max(0.0, ceiling)
            if ceiling <= 0 or (estimate.deterioration_score > 0 and not policy_authoritative):
                continue
            peers = [peer for peer in range(len(authorized_order)) if peer != index and operational_states[authorized_order[peer]] in weighted_states]
            overlap = max((float(correlation[index, peer]) for peer in peers), default=0.0)
            overlap_penalty = max(0.35, 1.0 - max(0.0, overlap))
            deterioration_penalty = 1.0 if estimate.sample_n == 0 else max(0.0, 1.0 - min(1.0, estimate.deterioration_score))
            regime_metric = current_regime_metrics[strategy]
            regime_return = regime_metric.get("conservative_expected_return")
            regime_penalty = 0.50 if regime_metric["reliable"] and float(regime_return or 0.0) <= 0 else min(1.25, 1.0 + float(regime_return or 0.0) * 5.0) if regime_metric["reliable"] else 0.75
            execution_quality = min(1.0, (_finite_nonnegative(policy_value(strategy, "execution_quality_score"), 100.0) or 0.0) / 100.0)
            evidence_weight = state_cap * quality * (1.0 - uncertainty) * overlap_penalty * deterioration_penalty * regime_penalty * execution_quality * drawdown_penalty
            weights_vector[index] = min(state_cap, ceiling, max(0.0, evidence_weight))
        max_invested = float(self.cfg.get("max_allocated_risk_fraction", 0.75))
        if weights_vector.sum() > max_invested:
            weights_vector *= max_invested / float(weights_vector.sum())
        stress = self._stress(weights_vector)
        stress_cap = float(self.cfg.get("max_stress_loss", 0.05))
        stress_loss = max(stress.values(), default=0.0)
        if stress_loss > stress_cap and stress_loss > 0:
            weights_vector *= stress_cap / stress_loss
            stress = self._stress(weights_vector)
            stress_loss = max(stress.values(), default=0.0)
        portfolio_variance = float(weights_vector @ covariance @ weights_vector) if len(authorized_order) else 0.0
        portfolio_volatility = math.sqrt(max(0.0, portfolio_variance))
        expected_returns = np.array([estimates[strategy].conservative_expected_return or 0.0 for strategy in authorized_order])
        expected_return = float(weights_vector @ expected_returns) if len(authorized_order) else 0.0
        marginal = covariance @ weights_vector / portfolio_volatility if portfolio_volatility > 0 else np.zeros(len(authorized_order))
        component = weights_vector * marginal
        expected_shortfall = 2.063 * portfolio_volatility
        allocation_weights = dict(zip(authorized_order, weights_vector.tolist()))
        full_weights = {strategy: float(allocation_weights.get(strategy, 0.0)) for strategy in strategy_order}

        exploration_heat_cap = float(self.cfg.get("exploration_heat_pct", 0.25))
        exploration_per_strategy = float(self.cfg.get("exploration_stop_risk_pct", 0.05))
        exploration_max = float(self.cfg.get("max_exploration_stop_risk_pct", 0.10))
        exploration_weights: dict[str, float] = {}
        exploration_heat = 0.0
        for strategy in authorized_order:
            if operational_states[strategy] == "EXPLORATION" and healthy:
                risk = min(exploration_per_strategy, exploration_max, max(0.0, exploration_heat_cap - exploration_heat))
                if risk > 0:
                    exploration_weights[strategy] = risk
                    exploration_heat += risk
        probe_heat_cap = float(self.cfg.get("probe_portfolio_heat_pct", 0.10))
        probe_per_strategy = float(self.cfg.get("probe_stop_risk_pct", 0.03))
        probe_weights: dict[str, float] = {}
        probe_heat = 0.0
        for strategy in authorized_order:
            if operational_states[strategy] == "PROBE" and healthy:
                risk = min(probe_per_strategy, max(0.0, probe_heat_cap - probe_heat))
                if risk > 0:
                    probe_weights[strategy] = risk
                    probe_heat += risk

        available_risk, risk_unit, available_risk_inputs = self._phase3_available_risk(snapshot, drawdown_pct, phase3_profile)
        sleeves, unallocated_available_risk, reconciliation_residual = self._build_sleeves(
            authorized_order, operational_states, allocation_weights, exploration_weights, probe_weights,
            snapshot, available_risk, risk_unit,
        )
        if weights_vector.sum() > 0:
            decision, reason, allocation_class = "ALLOCATE_ADAPTIVELY", "qualified strategies sized below fractional Kelly and Phase 3 risk", "adaptive"
        elif probe_weights:
            decision, reason, allocation_class = "ALLOCATE_PROBE", "authorized PROBE strategies receive bounded manual-approved paper risk", "probe"
        elif exploration_weights:
            decision, reason, allocation_class = "ALLOCATE_EXPLORATION", "authorized immature strategies receive bounded manual-approved paper exploration", "exploration"
        else:
            decision, reason, allocation_class = "PRESERVE_CASH", "no registry-authorized strategy has allocatable risk", "unallocated"
        cash = max(0.0, 1.0 - float(weights_vector.sum()))

        strategy_policies: dict[str, dict[str, Any]] = {}
        for strategy in strategy_order:
            state = operational_states[strategy]
            estimate = estimates[strategy]
            if strategy in probe_weights:
                emitted = {"mode": "probe", "state": "PROBE", "stop_risk_pct": probe_weights[strategy], "portfolio_heat_cap_pct": probe_heat_cap,
                           "gross_exposure_cap_pct": float(self.cfg.get("probe_gross_exposure_pct", 2.5)), "max_active_count": int(self.cfg.get("probe_max_active_count", 1)),
                           "minimum_setup_score": float(self.cfg.get("probe_min_setup_score", 85)), "entries_only": True, "adds_allowed": False,
                           "autonomous_execution_allowed": False, "allocation_class": "probe"}
            elif strategy in exploration_weights:
                emitted = {"mode": "exploration", "state": state, "stop_risk_pct": exploration_weights[strategy], "max_stop_risk_pct": exploration_max,
                           "gross_exposure_cap_pct": float(self.cfg.get("exploration_gross_exposure_pct", 7.5)), "allocation_class": "exploration"}
            elif strategy in authorized_set and state in weighted_states and allocation_weights.get(strategy, 0.0) > 0:
                weight = float(allocation_weights[strategy])
                emitted = {"mode": "adaptive" if state == "ACTIVE" else "throttled", "state": state, "allocation_weight": weight,
                           "allocation_weight_unit": "fraction_of_phase4_risk_sleeve", "risk_budget_multiplier": operational_risk_budget_multiplier(weight, max_weight),
                           "risk_budget_multiplier_unit": "unitless_fraction_of_authorized_strategy_stop_risk", "allocation_class": "adaptive" if state == "ACTIVE" else "throttled"}
            elif strategy not in authorized_set:
                emitted = {"mode": "research_only" if state == "RESEARCH_ONLY" else "blocked", "state": state, "operationally_executable": False,
                           "allocation_class": "unallocated", "reason": operational_reasons[strategy]}
            else:
                emitted = {"mode": "blocked", "state": state, "allocation_class": "unallocated", "reason": operational_reasons[strategy]}
            emitted.update({
                "operationally_executable": strategy in authorized_set,
                "kelly_used": False, "kelly_diagnostic_only": True, "score_sizing_used": False,
                "manual_approval_required": strategy in authorized_set, "evidence_version": EVIDENCE_VERSION,
                "performance_snapshot_id": policy_value(strategy, "performance_snapshot_id"), "policy_decision_id": policy_value(strategy, "id"),
                "quality_score": policy_value(strategy, "quality_score"), "policy_version": policy_value(strategy, "policy_version"),
                "binding_policy_reason": operational_reasons[strategy], "policy_authoritative": policy_authoritative,
                "conservative_expected_return": estimate.conservative_expected_return, "uncertainty": estimate.uncertainty,
                "data_quality": estimate.data_quality, "deterioration_score": estimate.deterioration_score,
                "current_regime_performance": current_regime_metrics[strategy], "sleeve": sleeves.get(strategy),
            })
            if phase3_profile is not None and state_risk_policy is not None and strategy in authorized_set:
                permitted, multiplier, _ = state_risk_policy(
                    state, initial_stop_risk_pct=phase3_profile.base_stop_risk_pct,
                    add_stop_risk_pct=phase3_profile.add_stop_risk_pct,
                    exploration_stop_risk_pct=exploration_per_strategy,
                    probe_stop_risk_pct=probe_per_strategy, is_add=False,
                )
                emitted.update({"strategy_risk_multiplier": multiplier, "permitted_stop_risk_pct": permitted})
            else:
                emitted.update({"strategy_risk_multiplier": 0.0, "permitted_stop_risk_pct": 0.0})
            strategy_policies[strategy] = emitted

        unallocated_risk_pct = (
            unallocated_available_risk
            if risk_unit == "pct_equity"
            else (
                unallocated_available_risk / float(snapshot.get("portfolio_equity") or 0.0) * 100.0
                if float(snapshot.get("portfolio_equity") or 0.0) > 0
                else 0.0
            )
        )
        binding_caps = {
            "fractional_kelly_ceiling": fraction, "max_strategy_weight": max_weight,
            "max_allocated_risk_fraction": max_invested, "max_stress_loss": stress_cap,
            "exploration_heat_pct": exploration_heat_cap,
            "exploration_gross_exposure_pct": float(self.cfg.get("exploration_gross_exposure_pct", 7.5)),
            "probe_stop_risk_pct": probe_per_strategy, "probe_portfolio_heat_pct": probe_heat_cap,
            "probe_gross_exposure_pct": float(self.cfg.get("probe_gross_exposure_pct", 2.5)),
            "probe_max_active_count": int(self.cfg.get("probe_max_active_count", 1)),
            "phase3_available_risk": available_risk, "phase3_available_risk_unit": risk_unit,
        }
        raw_replay_inputs = {
            "as_of": evaluation_time, "regime": regime, "drawdown_pct": drawdown_pct,
            "portfolio_snapshot": snapshot, "registry": registry_payload,
            "strategy_order": list(strategy_order), "authorized_strategy_order": list(authorized_order),
            "evidence_fingerprints": evidence_fingerprints, "covariance_inputs": self._last_covariance_payload,
            "available_risk_inputs": available_risk_inputs, "configuration_hash": self.config.get("effective_config_hash"),
            "formula_version": PHASE4_ALLOCATION_VERSION,
        }
        fingerprint = _fingerprint(raw_replay_inputs)
        allocation_id = _fingerprint([self.run_id, fingerprint, full_weights, sleeves])[:32]
        payload = {
            "covariance_id": covariance_id, "phase3_limits_authoritative": True, "full_kelly": False, "llm_decisions": False,
            "covariance_fallback": fallback, "covariance_validation": self._last_covariance_payload,
            "operational_kelly_enabled": False, "operational_allocation_mode": "bounded_evidence_aware",
            "registry_evaluation": registry_payload, "authorized_strategies": list(authorized_order), "rejected_strategies": registry_rejections,
            "strategy_order": list(strategy_order), "kelly_diagnostics": kelly_diagnostics,
            "current_regime_performance": current_regime_metrics, "exploration_heat_pct": exploration_heat,
            "exploration_heat_cap_pct": exploration_heat_cap, "exploration_weights": exploration_weights,
            "probe_heat_pct": probe_heat, "probe_heat_cap_pct": probe_heat_cap, "probe_weights": probe_weights,
            "strategy_policies": strategy_policies, "allocation_class": allocation_class,
            "unallocated_risk_pct": unallocated_risk_pct, "phase3_available_risk": available_risk,
            "phase3_available_risk_unit": risk_unit, "strategy_sleeves": sleeves,
            "unallocated_available_risk": unallocated_available_risk, "risk_reconciliation_residual": reconciliation_residual,
            "risk_reconciliation": {
                "capacity": available_risk,
                "allocated_targets": round(sum(float(row["allocated_risk"]) for row in sleeves.values()), 8),
                "unallocated_target": unallocated_available_risk,
                "consumed": round(sum(float(row["consumed_risk"]) for row in sleeves.values()), 8),
                "remaining": round(sum(float(row["remaining_risk"]) for row in sleeves.values()), 8),
                "overconsumed": round(sum(float(row["overconsumed_risk"]) for row in sleeves.values()), 8),
                "residual": reconciliation_residual,
                "unit": risk_unit,
            },
            "registry_snapshot_id": snapshot.get("strategy_registry_snapshot_id"),
            "evidence_versions": {strategy: EVIDENCE_VERSION for strategy in strategy_order},
            "formula_version": PHASE4_ALLOCATION_VERSION, "config_hash": self.config.get("effective_config_hash"),
            "strategy_policy_map": strategy_policies,
            "strategy_policy_version": next((policy_value(strategy, "policy_version") for strategy in strategy_order if policy_value(strategy, "policy_version")), None),
            "policy_authoritative": policy_authoritative, "raw_replay_inputs": raw_replay_inputs,
        }
        placeholders = ",".join("?" for _ in range(42))
        marginal_map = {strategy: float(marginal[index]) for index, strategy in enumerate(authorized_order)}
        component_map = {strategy: float(component[index]) for index, strategy in enumerate(authorized_order)}
        self.storage.execute(
            f"""INSERT OR REPLACE INTO phase4_allocation_decisions(
               id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,fractional_kelly_ceiling,
               expected_portfolio_return,portfolio_volatility,expected_shortfall,stress_loss,marginal_risk_json,component_risk_json,
               regime,drawdown_pct,uncertainty_penalty,data_quality,decision,reason,allocation_class,operational_kelly_used,
               kelly_diagnostic_json,adaptive_allocation_json,exploration_allocation_json,unallocated_risk_pct,
               heat_before_pct,heat_after_pct,gross_exposure_before_pct,gross_exposure_after_pct,
               symbol_exposure_before_json,symbol_exposure_after_json,cluster_exposure_before_json,cluster_exposure_after_json,
               pending_risk,reserved_risk,binding_caps_json,evidence_versions_json,evidence_fingerprint,formula_version,config_hash,payload)
             VALUES({placeholders})""",
            (allocation_id, self.run_id, now, "ACTIVE_ADAPTIVE_PAPER", ALLOCATOR_VERSION, json_dumps(full_weights), cash, fraction,
             expected_return, portfolio_volatility, expected_shortfall, stress_loss, json_dumps(marginal_map), json_dumps(component_map),
             regime, drawdown_pct, statistics.fmean(estimate.uncertainty for estimate in estimates.values()),
             statistics.fmean(estimate.data_quality for estimate in estimates.values()), decision, reason, allocation_class, 0,
             json_dumps(kelly_diagnostics), json_dumps({strategy: weight for strategy, weight in allocation_weights.items() if weight > 0}),
             json_dumps(exploration_weights), unallocated_risk_pct, snapshot.get("heat_before_pct"), snapshot.get("heat_before_pct"),
             snapshot.get("gross_exposure_before_pct"), snapshot.get("gross_exposure_before_pct"),
             json_dumps(snapshot.get("symbol_exposure_before") or {}), json_dumps(snapshot.get("symbol_exposure_before") or {}),
             json_dumps(snapshot.get("cluster_exposure_before") or {}), json_dumps(snapshot.get("cluster_exposure_before") or {}),
             snapshot.get("pending_risk"), snapshot.get("reserved_risk"), json_dumps(binding_caps),
             json_dumps({strategy: EVIDENCE_VERSION for strategy in strategy_order}), fingerprint, PHASE4_ALLOCATION_VERSION,
             self.config.get("effective_config_hash"), json_dumps(payload)),
        )
        self.storage.execute(
            "UPDATE phase4_allocation_decisions SET strategy_policy_map_json=?,strategy_policy_version=?,probe_allocation_json=? WHERE id=?",
            (json_dumps(strategy_policies), payload.get("strategy_policy_version"), json_dumps(probe_weights), allocation_id),
        )
        for scenario, loss in stress.items():
            stress_id = _fingerprint([allocation_id, scenario])[:32]
            self.storage.execute(
                "INSERT OR REPLACE INTO phase4_stress_results VALUES(?,?,?,?,?,?,?,?)",
                (stress_id, allocation_id, scenario, loss, loss, int(loss <= stress_cap), "phase4_stress_v1", json_dumps({"deterministic": True})),
            )
        return {
            "allocation_id": allocation_id, "weights": full_weights, "exploration_weights": exploration_weights,
            "probe_weights": probe_weights, "probe_heat_pct": probe_heat, "exploration_heat_pct": exploration_heat,
            "cash_weight": cash, "decision": decision, "reason": reason, "estimates": estimates,
            "strategy_policies": strategy_policies, "kelly_diagnostics": kelly_diagnostics,
            "operational_strategies": list(authorized_order), "authorized_strategies": list(authorized_order),
            "rejected_strategies": registry_rejections, "registry_evaluation": registry_payload, "healthy": healthy,
            "allocation_class": allocation_class, "operational_kelly_used": False,
            "unallocated_risk_pct": unallocated_risk_pct, "binding_caps": binding_caps,
            "evidence_versions": {strategy: EVIDENCE_VERSION for strategy in strategy_order},
            "formula_version": PHASE4_ALLOCATION_VERSION, "strategy_policy_map": strategy_policies,
            "strategy_policy_version": payload.get("strategy_policy_version"), "policy_authoritative": policy_authoritative,
            "covariance": covariance_payload, "strategy_sleeves": sleeves, "phase3_available_risk": available_risk,
            "phase3_available_risk_unit": risk_unit, "unallocated_available_risk": unallocated_available_risk,
            "risk_reconciliation_residual": reconciliation_residual, "raw_replay_inputs": raw_replay_inputs,
            "evidence_fingerprint": fingerprint,
        }

    def allocate_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
        sleeves: Mapping[str, Mapping[str, Any]],
        *,
        global_available_risk: float | None = None,
    ) -> dict[str, Any]:
        return allocate_candidates_to_sleeves(
            candidates, sleeves, global_available_risk=global_available_risk,
        )

    def _persist_state(self, estimate: StrategyEstimate, estimate_id: str, now: str) -> None:
        old = self.storage.fetch_all("SELECT state FROM phase4_strategy_states WHERE strategy_version=?", (estimate.strategy_version,))
        previous = old[0]["state"] if old else None
        recovered = now if previous in {"THROTTLED", "SUSPENDED"} and estimate.state == "ACTIVE" else None
        self.storage.execute("""INSERT INTO phase4_strategy_states VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(strategy_version) DO UPDATE SET state=excluded.state,reason=excluded.reason,estimate_id=excluded.estimate_id,
          evaluated_at=excluded.evaluated_at,activated_at=COALESCE(phase4_strategy_states.activated_at,excluded.activated_at),
          throttled_at=excluded.throttled_at,suspended_at=excluded.suspended_at,recovered_at=COALESCE(excluded.recovered_at,phase4_strategy_states.recovered_at),payload=excluded.payload""",
          (estimate.strategy_version, estimate.state, estimate.reason, estimate_id, "phase4_strategy_state_v3_probe", now,
           now if estimate.state == "ACTIVE" else None, now if estimate.state == "THROTTLED" else None,
           now if estimate.state == "SUSPENDED" else None, recovered,
           json_dumps({"deterministic": True, "evidence_class": estimate.evidence_class, "state_version": "phase4_strategy_state_v3_probe"})))

    def _stress(self, weights: np.ndarray) -> dict[str, float]:
        invested = float(weights.sum())
        largest = float(weights.max(initial=0))
        return {
            "spy_down_3": invested * 0.03, "spy_down_5": invested * 0.05,
            "sector_down_7": largest * 0.07, "volatility_doubles": invested * 0.04,
            "two_atr_gap": invested * 0.06, "correlations_to_one": invested * 0.08,
            "largest_position_down_15": largest * 0.15,
        }
