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
from .formula_versions import EVIDENCE_VERSION, PHASE4_ALLOCATION_VERSION
from .shadow_strategies import STRATEGY_VERSIONS
from .strategy_rule_based import STRATEGY_VERSION
from .utils import iso_now, json_dumps


PHASE4_SCHEMA_VERSION = "phase4_adaptive_paper_allocation_v1"
ALLOCATOR_VERSION = "adaptive_paper_allocator_v1"
ESTIMATOR_VERSION = "shrunk_oos_estimator_v1"
COVARIANCE_VERSION = "ledoit_wolf_style_shrinkage_v1"
EXECUTABLE_STRATEGIES = (STRATEGY_VERSION,)
STRATEGIES = (*EXECUTABLE_STRATEGIES, *tuple(sorted(STRATEGY_VERSIONS.values())))


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
    def __init__(self, storage: Any, config: Mapping[str, Any], run_id: str) -> None:
        self.storage, self.config, self.run_id = storage, config, run_id
        self.cfg = config.get("phase4", {})
        self._validate()

    def _validate(self) -> None:
        if self.cfg.get("mode") != "ACTIVE_ADAPTIVE_PAPER": raise ValueError("Phase 4 mode must be ACTIVE_ADAPTIVE_PAPER")
        fraction = float(self.cfg.get("fractional_kelly", 0))
        if not 0 < fraction <= 0.25: raise ValueError("fractional Kelly must be positive and no greater than one quarter")
        if self.cfg.get("full_kelly_allowed") is not False: raise ValueError("full Kelly is forbidden")
        if self.cfg.get("llm_trading_decisions") is not False: raise ValueError("LLM trading decisions are forbidden")
        if self.cfg.get("operational_kelly_enabled") is not False: raise ValueError("operational Kelly must remain disabled")
        if self.cfg.get("operational_allocation_mode") != "deterministic_equal_risk":
            raise ValueError("operational allocation must be deterministic equal risk")

    def _rows(self, strategy: str) -> list[dict[str, Any]]:
        rows = self.storage.fetch_all("""SELECT ro.id,ro.regime,ro.split_label,ro.execution_type,ro.source_table,
          ro.provenance_json,r.exit_session,r.cost_adjusted_return,r.gross_return,r.cost_bps,r.calculated_at
          FROM research_opportunities ro JOIN research_outcomes r ON r.opportunity_id=ro.id
          WHERE ro.strategy_version=? AND ro.split_label='out_of_sample' AND r.horizon_sessions=20
            AND r.status='completed' AND r.cost_adjusted_return IS NOT NULL
            AND r.calculation_version=?
            ORDER BY r.exit_session,ro.id""", (strategy, EVIDENCE_VERSION))
        # Executable strategies may use only synchronized executable evidence.
        # Shadow strategies use shadow outcomes for research state transitions;
        # neither population is allowed to cross into the other.
        allowed = OPERATIONAL_EVIDENCE_TYPES if strategy in EXECUTABLE_STRATEGIES else {SHADOW_OUTCOME}
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
        rows = self._rows(strategy); values = [float(r["cost_adjusted_return"]) for r in rows]
        regimes = {str(r.get("regime")) for r in rows if r.get("regime")}
        fp = _fingerprint(rows)
        minimum = int(self.cfg.get("minimum_oos_samples", 100)); min_regimes = int(self.cfg.get("minimum_regimes", 2))
        if not values:
            return StrategyEstimate(strategy,0,0,None,None,None,None,None,1.0,0.0,1.0,"EXPLORATION",
                                    "insufficient evidence: no completed OOS evidence; bounded exploration permitted",
                                    "insufficient"),rows,fp
        mean = statistics.fmean(values); sd = statistics.stdev(values) if len(values)>1 else 0.0
        se = sd / math.sqrt(len(values)) if len(values)>1 else None
        prior_strength = float(self.cfg.get("shrinkage_prior_samples", 100))
        shrunk = mean * len(values) / (len(values)+prior_strength)
        conservative = shrunk - float(self.cfg.get("confidence_z",1.96)) * (se or abs(mean) or 1.0)
        wins = sum(v>0 for v in values); calibrated_p = (wins+10.0)/(len(values)+20.0)
        recent = values[-max(5,min(20,len(values)//3 or 1)):]
        earlier = values[:-len(recent)]
        deterioration = max(0.0,(statistics.fmean(earlier)-statistics.fmean(recent))/(sd or 1.0)) if earlier else 0.0
        completeness = min(1.0,len(values)/minimum); regime_quality=min(1.0,len(regimes)/min_regimes)
        cost_quality = 1.0 if all(r.get("cost_bps") is not None for r in rows) else 0.5
        quality = completeness*regime_quality*cost_quality
        uncertainty = min(1.0,(se or 1.0)/(abs(shrunk)+1e-9))
        stale = self._is_stale(rows)
        if stale: state,reason,evidence_class="SUSPENDED","stale OOS evidence","stale"
        elif deterioration >= float(self.cfg.get("deterioration_suspend_z",2.0)): state,reason,evidence_class="SUSPENDED","statistically material recent deterioration","deteriorating"
        elif mean <= 0 or calibrated_p <= 0.5: state,reason,evidence_class="SUSPENDED","negative cost-adjusted evidence","negative"
        elif len(values)<minimum or len(regimes)<min_regimes: state,reason,evidence_class="EXPLORATION","insufficient OOS sample or regime coverage; bounded exploration permitted","insufficient"
        elif conservative<=0: state,reason,evidence_class="THROTTLED","positive point estimate but uncertainty is too high for adaptive allocation","insufficient"
        else: state,reason,evidence_class="ACTIVE","conservative OOS evidence passed","qualified"
        return StrategyEstimate(strategy,len(values),len(regimes),mean,shrunk,conservative,calibrated_p,se,uncertainty,quality,deterioration,state,reason,evidence_class),rows,fp

    def covariance(self, evidence: Mapping[str,list[dict[str,Any]]]) -> tuple[np.ndarray,bool,dict[str,int]]:
        n=len(STRATEGIES); matrix=np.zeros((n,n)); counts={s:len(evidence[s]) for s in STRATEGIES}; fallback=False
        maps={s:{str(r.get("exit_session")):float(r["cost_adjusted_return"]) for r in evidence[s] if r.get("exit_session")} for s in STRATEGIES}
        default_var=float(self.cfg.get("fallback_annual_variance",0.04))
        for i,a in enumerate(STRATEGIES):
            av=list(maps[a].values()); matrix[i,i]=float(np.var(av,ddof=1)) if len(av)>=2 else default_var; fallback |= len(av)<2
            for j in range(i):
                b=STRATEGIES[j]; common=sorted(set(maps[a])&set(maps[b]))
                if len(common)>=5: cov=float(np.cov([maps[a][d] for d in common],[maps[b][d] for d in common],ddof=1)[0,1])
                else:
                    cov=0.5*math.sqrt(matrix[i,i]*matrix[j,j]); fallback=True
                matrix[i,j]=matrix[j,i]=cov
        target=np.diag(np.diag(matrix)); shrink=float(self.cfg.get("covariance_shrinkage",0.5))
        matrix=(1-shrink)*matrix+shrink*target
        return matrix,fallback,counts

    def run(
        self,
        *,
        regime: str,
        drawdown_pct: float,
        portfolio_snapshot: Mapping[str, Any] | None = None,
        strategy_policy_map: Mapping[str, Any] | None = None,
    ) -> dict[str,Any]:
        healthy=not any(DurableExecutionStore(self.storage).integrity_report().values())
        estimates={}; evidence={}; fps=[]; now=iso_now()
        portfolio_snapshot = dict(portfolio_snapshot or {})
        heat_before = portfolio_snapshot.get("heat_before_pct")
        gross_before = portfolio_snapshot.get("gross_exposure_before_pct")
        symbol_before = portfolio_snapshot.get("symbol_exposure_before") or {}
        cluster_before = portfolio_snapshot.get("cluster_exposure_before") or {}
        pending_risk = portfolio_snapshot.get("pending_risk")
        reserved_risk = portfolio_snapshot.get("reserved_risk")
        for strategy in STRATEGIES:
            estimate,rows,fp=self.estimate(strategy); evidence[strategy]=rows; fps.append(fp)
            if not healthy: estimate=StrategyEstimate(**{**asdict(estimate),"state":"SUSPENDED","reason":"durable integrity health failed"})
            estimates[strategy]=estimate
            eid=_fingerprint([self.run_id,strategy,fp])[:32]
            self.storage.execute("INSERT OR REPLACE INTO phase4_strategy_estimates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (eid,self.run_id,strategy,now,estimate.sample_n,estimate.regime_n,estimate.mean_return,estimate.shrunk_mean_return,
               estimate.conservative_expected_return,estimate.calibrated_positive_probability,estimate.standard_error,
               estimate.uncertainty,estimate.data_quality,estimate.deterioration_score,estimate.state,estimate.reason,
               ESTIMATOR_VERSION,fp,json_dumps({"cost_adjusted":True,"score_sizing":False,
                                                 "evidence_class":estimate.evidence_class,
                                                 "state_version":"phase4_strategy_state_v2"}),))
            self._persist_state(estimate,eid,now)
        policy_authoritative = strategy_policy_map is not None

        def policy_value(strategy: str, name: str, default: Any = None) -> Any:
            policy = (strategy_policy_map or {}).get(strategy)
            if policy is None:
                return default
            if isinstance(policy, Mapping):
                return policy.get(name, default)
            return getattr(policy, name, default)

        operational_states: dict[str, str] = {}
        operational_reasons: dict[str, str] = {}
        for strategy in STRATEGIES:
            if strategy not in EXECUTABLE_STRATEGIES:
                operational_states[strategy] = "RESEARCH_ONLY"
                operational_reasons[strategy] = "shadow/research strategy cannot receive executable allocation"
            elif not policy_authoritative:
                operational_states[strategy] = estimates[strategy].state
                operational_reasons[strategy] = estimates[strategy].reason
            elif policy_value(strategy, "state") in {"RESEARCH_ONLY", "EXPLORATION", "THROTTLED", "ACTIVE", "SUSPENDED"}:
                operational_states[strategy] = str(policy_value(strategy, "state"))
                operational_reasons[strategy] = str(policy_value(strategy, "reason", "persisted strategy policy"))
            else:
                operational_states[strategy] = "SUSPENDED"
                operational_reasons[strategy] = "latest strategy performance policy unavailable or invalid"
        try:
            from .phase3_risk import Phase3RiskProfile
            from .strategy_performance import state_risk_policy
            phase3_profile = Phase3RiskProfile.from_config(self.config)
        except (KeyError, TypeError, ValueError):
            phase3_profile = None
        cov,fallback,counts=self.covariance(evidence); diag=np.sqrt(np.maximum(np.diag(cov),1e-12)); corr=cov/np.outer(diag,diag)
        cov_id=_fingerprint([self.run_id,counts,cov.tolist()])[:32]
        self.storage.execute("INSERT OR REPLACE INTO phase4_covariance_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (cov_id,self.run_id,now,json_dumps(STRATEGIES),json_dumps(cov.tolist()),json_dumps(corr.tolist()),json_dumps(counts),
           COVARIANCE_VERSION,int(fallback),min(e.data_quality for e in estimates.values()),json_dumps({"overlap_penalty":True,"sector_fallback_correlation":0.5})))
        weights=np.zeros(len(STRATEGIES)); fraction=float(self.cfg["fractional_kelly"]); max_weight=float(self.cfg.get("max_strategy_weight",0.35))
        kelly_diagnostics: dict[str, float] = {}
        for i,s in enumerate(STRATEGIES):
            e=estimates[s]
            if e.state!="ACTIVE" or e.conservative_expected_return is None: continue
            kelly=max(0.0,e.conservative_expected_return/max(cov[i,i],1e-12))*fraction
            kelly_diagnostics[s] = min(max_weight,kelly)*e.data_quality*(1-e.uncertainty)
        # Kelly/covariance remain diagnostics.  Operational allocation is
        # deterministic and only executable strategies can receive it.
        active_executable = [s for s in EXECUTABLE_STRATEGIES if operational_states[s] == "ACTIVE"]
        if active_executable:
            equal_weight = min(max_weight, 1.0 / len(active_executable))
            for strategy in active_executable:
                weights[STRATEGIES.index(strategy)] = equal_weight
        total=float(weights.sum()); max_invested=float(self.cfg.get("max_allocated_risk_fraction",0.75))
        if total>max_invested: weights*=max_invested/total
        port_var=float(weights@cov@weights); port_vol=math.sqrt(max(0.0,port_var)); mu=np.array([estimates[s].conservative_expected_return or 0.0 for s in STRATEGIES]); expected=float(weights@mu)
        marginal=(cov@weights)/port_vol if port_vol>0 else np.zeros(len(weights)); component=weights*marginal
        stress=self._stress(weights); stress_loss=max(stress.values()) if stress else 0.0
        stress_cap=float(self.cfg.get("max_stress_loss",0.05))
        if stress_loss > stress_cap and stress_loss > 0:
            weights *= stress_cap / stress_loss
            port_var=float(weights@cov@weights); port_vol=math.sqrt(max(0.0,port_var)); expected=float(weights@mu)
            marginal=(cov@weights)/port_vol if port_vol>0 else np.zeros(len(weights)); component=weights*marginal
            stress=self._stress(weights); stress_loss=max(stress.values()) if stress else 0.0
        expected_shortfall=2.063*port_vol

        exploration_heat_cap=float(self.cfg.get("exploration_heat_pct",0.25))
        exploration_per_strategy=float(self.cfg.get("exploration_stop_risk_pct",0.05))
        exploration_max_per_strategy=float(self.cfg.get("max_exploration_stop_risk_pct",0.10))
        exploration_heat=0.0; exploration_weights: dict[str,float] = {}
        for strategy in EXECUTABLE_STRATEGIES:
            if operational_states[strategy] != "EXPLORATION" or not healthy:
                continue
            remaining=max(0.0, exploration_heat_cap-exploration_heat)
            risk=min(exploration_per_strategy, exploration_max_per_strategy, remaining)
            if risk <= 0:
                continue
            exploration_weights[strategy]=risk
            exploration_heat += risk
        cash=max(0.0,1.0-float(weights.sum()))
        if float(weights.sum()) > 0:
            decision="ALLOCATE_ADAPTIVELY"; reason="qualified strategies sized below fractional Kelly and hard limits"
        elif exploration_weights:
            decision="ALLOCATE_EXPLORATION"; reason="healthy immature strategies receive bounded manual-approved paper exploration"
        else:
            decision="PRESERVE_CASH"; reason="no strategy has reliable positive OOS evidence or safe exploration eligibility"
        strategy_policies: dict[str,dict[str,Any]] = {}
        for i, strategy in enumerate(STRATEGIES):
            estimate=estimates[strategy]
            if strategy in exploration_weights:
                strategy_policies[strategy]={"mode":"exploration","state":operational_states[strategy],"stop_risk_pct":exploration_weights[strategy],
                                             "max_stop_risk_pct":exploration_max_per_strategy,"gross_exposure_cap_pct":float(self.cfg.get("exploration_gross_exposure_pct",7.5)),
                                             "kelly_used":False,"kelly_diagnostic_only":True,"score_sizing_used":False,"manual_approval_required":True,
                                             "allocation_class":"exploration","evidence_version":EVIDENCE_VERSION}
            elif strategy in EXECUTABLE_STRATEGIES and operational_states[strategy]=="ACTIVE" and weights[i]>0:
                strategy_policies[strategy]={"mode":"adaptive","state":operational_states[strategy],"allocation_weight":float(weights[i]),
                                             "kelly_used":False,"kelly_diagnostic_only":True,"score_sizing_used":False,"manual_approval_required":True,
                                             "allocation_class":"adaptive","evidence_version":EVIDENCE_VERSION}
            elif strategy not in EXECUTABLE_STRATEGIES:
                strategy_policies[strategy]={"mode":"research_only","state":"RESEARCH_ONLY","operationally_executable":False,
                                             "kelly_used":False,"kelly_diagnostic_only":True,"score_sizing_used":False,"manual_approval_required":False,
                                             "allocation_class":"unallocated","evidence_version":EVIDENCE_VERSION,
                                             "reason":"shadow/research strategy cannot receive executable allocation"}
            else:
                strategy_policies[strategy]={"mode":"blocked","state":operational_states[strategy],"reason":operational_reasons[strategy],"kelly_used":False,"kelly_diagnostic_only":True,"score_sizing_used":False,"manual_approval_required":True,
                                             "allocation_class":"unallocated","evidence_version":EVIDENCE_VERSION}
            strategy_policies[strategy].update({
                "performance_snapshot_id": policy_value(strategy, "performance_snapshot_id"),
                "policy_decision_id": policy_value(strategy, "id"),
                "quality_score": policy_value(strategy, "quality_score"),
                "policy_version": policy_value(strategy, "policy_version"),
                "binding_policy_reason": operational_reasons[strategy],
                "policy_authoritative": policy_authoritative,
            })
            if phase3_profile is not None and strategy in EXECUTABLE_STRATEGIES:
                permitted, multiplier, _risk_reason = state_risk_policy(
                    operational_states[strategy],
                    initial_stop_risk_pct=phase3_profile.base_stop_risk_pct,
                    add_stop_risk_pct=phase3_profile.add_stop_risk_pct,
                    exploration_stop_risk_pct=float(self.cfg.get("exploration_stop_risk_pct", 0.05)),
                    is_add=False,
                )
                strategy_policies[strategy].update({
                    "strategy_risk_multiplier": multiplier,
                    "permitted_stop_risk_pct": permitted,
                })
            else:
                strategy_policies[strategy].update({"strategy_risk_multiplier": 0.0, "permitted_stop_risk_pct": 0.0})
        fingerprint=_fingerprint(fps); aid=_fingerprint([self.run_id,weights.tolist(),cash,fingerprint])[:32]
        allocation_class = "adaptive" if float(weights.sum()) > 0 else "exploration" if exploration_weights else "unallocated"
        unallocated_risk_pct = max(0.0, 1.0 - float(weights.sum()) - float(exploration_heat) / 100.0)
        binding_caps = {
            "fractional_kelly_ceiling": fraction,
            "max_strategy_weight": max_weight,
            "max_allocated_risk_fraction": max_invested,
            "max_stress_loss": stress_cap,
            "exploration_heat_pct": exploration_heat_cap,
            "exploration_gross_exposure_pct": float(self.cfg.get("exploration_gross_exposure_pct", 7.5)),
        }
        payload={"covariance_id":cov_id,"phase3_limits_authoritative":True,"full_kelly":False,"llm_decisions":False,"covariance_fallback":fallback,
                 "operational_kelly_enabled":False,"operational_allocation_mode":"deterministic_equal_risk",
                 "kelly_diagnostics":kelly_diagnostics,
                 "exploration_heat_pct":exploration_heat,"exploration_heat_cap_pct":exploration_heat_cap,"exploration_weights":exploration_weights,
                 "exploration_gross_exposure_cap_pct":float(self.cfg.get("exploration_gross_exposure_pct",7.5)),"strategy_policies":strategy_policies,
                 "allocation_class":allocation_class,"unallocated_risk_pct":unallocated_risk_pct,
                 "evidence_versions":{strategy:EVIDENCE_VERSION for strategy in STRATEGIES},"formula_version":PHASE4_ALLOCATION_VERSION,
                 "config_hash":self.config.get("effective_config_hash"), "strategy_policy_map":strategy_policies,
                 "strategy_policy_version": next((policy_value(s, "policy_version") for s in STRATEGIES if policy_value(s, "policy_version")), None),
                 "policy_authoritative": policy_authoritative}
        phase4_placeholders = ",".join("?" for _ in range(42))
        self.storage.execute(
            f"""INSERT OR REPLACE INTO phase4_allocation_decisions(
               id,run_id,decided_at,mode,allocator_version,strategy_weights_json,cash_weight,fractional_kelly_ceiling,
               expected_portfolio_return,portfolio_volatility,expected_shortfall,stress_loss,marginal_risk_json,component_risk_json,
               regime,drawdown_pct,uncertainty_penalty,data_quality,decision,reason,allocation_class,operational_kelly_used,
               kelly_diagnostic_json,adaptive_allocation_json,exploration_allocation_json,unallocated_risk_pct,
               heat_before_pct,heat_after_pct,gross_exposure_before_pct,gross_exposure_after_pct,
               symbol_exposure_before_json,symbol_exposure_after_json,cluster_exposure_before_json,cluster_exposure_after_json,
               pending_risk,reserved_risk,binding_caps_json,evidence_versions_json,evidence_fingerprint,formula_version,config_hash,payload)
             VALUES({phase4_placeholders})""",
            (aid,self.run_id,now,"ACTIVE_ADAPTIVE_PAPER",ALLOCATOR_VERSION,json_dumps(dict(zip(STRATEGIES,weights.tolist()))),cash,fraction,
             expected,port_vol,expected_shortfall,stress_loss,json_dumps(dict(zip(STRATEGIES,marginal.tolist()))),json_dumps(dict(zip(STRATEGIES,component.tolist()))),
             regime,drawdown_pct,statistics.fmean(e.uncertainty for e in estimates.values()),statistics.fmean(e.data_quality for e in estimates.values()),
             decision,reason,allocation_class,0,json_dumps(kelly_diagnostics),json_dumps({s:float(weights[i]) for i,s in enumerate(STRATEGIES) if weights[i] > 0}),
             json_dumps(exploration_weights),unallocated_risk_pct,heat_before,heat_before,gross_before,gross_before,
             json_dumps(symbol_before),json_dumps(symbol_before),json_dumps(cluster_before),json_dumps(cluster_before),pending_risk,reserved_risk,
             json_dumps(binding_caps),json_dumps({strategy:EVIDENCE_VERSION for strategy in STRATEGIES}),fingerprint,PHASE4_ALLOCATION_VERSION,
             self.config.get("effective_config_hash"),json_dumps(payload)))
        self.storage.execute(
            "UPDATE phase4_allocation_decisions SET strategy_policy_map_json=?,strategy_policy_version=? WHERE id=?",
            (json_dumps(strategy_policies), payload.get("strategy_policy_version"), aid),
        )
        for scenario,loss in stress.items():
            sid=_fingerprint([aid,scenario])[:32]
            self.storage.execute("INSERT OR REPLACE INTO phase4_stress_results VALUES(?,?,?,?,?,?,?,?)",
                                 (sid,aid,scenario,loss,loss,int(loss<=float(self.cfg.get("max_stress_loss",0.05))),"phase4_stress_v1",json_dumps({"deterministic":True})))
        return {"allocation_id":aid,"weights":dict(zip(STRATEGIES,weights.tolist())),"exploration_weights":exploration_weights,
                "exploration_heat_pct":exploration_heat,"cash_weight":cash,"decision":decision,"reason":reason,"estimates":estimates,
                "strategy_policies":strategy_policies,"kelly_diagnostics":kelly_diagnostics,
                "operational_strategies":list(EXECUTABLE_STRATEGIES),"healthy":healthy,
                "allocation_class":allocation_class,"operational_kelly_used":False,
                "unallocated_risk_pct":unallocated_risk_pct,"binding_caps":binding_caps,
                "evidence_versions":{strategy:EVIDENCE_VERSION for strategy in STRATEGIES},
                "formula_version":PHASE4_ALLOCATION_VERSION, "strategy_policy_map":strategy_policies,
                "strategy_policy_version": payload.get("strategy_policy_version"), "policy_authoritative": policy_authoritative}

    def _persist_state(self,e:StrategyEstimate,eid:str,now:str)->None:
        old=self.storage.fetch_all("SELECT state FROM phase4_strategy_states WHERE strategy_version=?",(e.strategy_version,)); previous=old[0]["state"] if old else None
        recovered=now if previous in {"THROTTLED","SUSPENDED"} and e.state=="ACTIVE" else None
        self.storage.execute("""INSERT INTO phase4_strategy_states VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(strategy_version) DO UPDATE SET state=excluded.state,reason=excluded.reason,estimate_id=excluded.estimate_id,
          evaluated_at=excluded.evaluated_at,activated_at=COALESCE(phase4_strategy_states.activated_at,excluded.activated_at),
          throttled_at=excluded.throttled_at,suspended_at=excluded.suspended_at,recovered_at=COALESCE(excluded.recovered_at,phase4_strategy_states.recovered_at),payload=excluded.payload""",
          (e.strategy_version,e.state,e.reason,eid,"phase4_strategy_state_v2",now,now if e.state=="ACTIVE" else None,
           now if e.state=="THROTTLED" else None,now if e.state=="SUSPENDED" else None,recovered,
           json_dumps({"deterministic":True,"evidence_class":e.evidence_class,"state_version":"phase4_strategy_state_v2"})))

    def _stress(self,w:np.ndarray)->dict[str,float]:
        invested=float(w.sum())
        return {"spy_down_3":invested*.03,"spy_down_5":invested*.05,"sector_down_7":float(w.max(initial=0))*.07,
                "volatility_doubles":invested*.04,"two_atr_gap":invested*.06,"correlations_to_one":invested*.08,
                "largest_position_down_15":float(w.max(initial=0))*.15}
