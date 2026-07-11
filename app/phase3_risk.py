from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from .execution import DurableExecutionStore
from .evidence import SHADOW_OUTCOME, classify_evidence_type, is_operational_evidence
from .shadow_strategies import STRATEGY_VERSIONS
from .strategy_rule_based import STRATEGY_VERSION
from .utils import iso_now, json_dumps


PHASE3_SCHEMA_VERSION = "phase3_moderate_paper_risk_v1"
PROFILE_VERSION = "moderate_paper_risk_v1"


@dataclass(frozen=True)
class Phase3RiskProfile:
    base_stop_risk_pct: float
    add_stop_risk_pct: float
    max_trade_stop_risk_pct: float
    max_portfolio_heat_pct: float
    favorable_portfolio_heat_pct: float
    defensive_portfolio_heat_pct: float
    normal_gross_exposure_pct: float
    favorable_gross_exposure_pct: float
    hard_gross_exposure_pct: float
    max_symbol_exposure_pct: float
    max_cluster_exposure_pct: float
    daily_loss_throttle_pct: float
    weekly_loss_throttle_pct: float
    drawdown_halt_pct: float
    minimum_average_dollar_volume: float

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Phase3RiskProfile":
        cfg = config.get("phase3", {}).get("risk_profile", {})
        profile = cls(**{name: float(cfg[name]) for name in cls.__dataclass_fields__})
        profile.validate()
        return profile

    def validate(self) -> None:
        if not math.isclose(self.base_stop_risk_pct, 0.20, abs_tol=1e-9):
            raise ValueError("Phase 3 base stop risk must be 0.20%")
        if not 0 < self.add_stop_risk_pct <= 0.15:
            raise ValueError("add stop risk must be within 0.10-0.15%")
        if not self.base_stop_risk_pct <= self.max_trade_stop_risk_pct <= 0.35:
            raise ValueError("maximum trade stop risk exceeds Phase 3 bound")
        if not self.defensive_portfolio_heat_pct <= 0.50 < self.max_portfolio_heat_pct <= 1.25:
            raise ValueError("portfolio heat bounds are invalid")
        if not self.normal_gross_exposure_pct <= 30 < self.favorable_gross_exposure_pct <= 40:
            raise ValueError("gross exposure profile is invalid")
        if self.hard_gross_exposure_pct != 50 or self.max_symbol_exposure_pct > 6 or self.max_cluster_exposure_pct > 15:
            raise ValueError("hard exposure bounds exceed requirements")
        if self.drawdown_halt_pct != 6:
            raise ValueError("new risk must halt at 6% drawdown")


def apply_phase3_schema(conn: Any, *, record_migration: bool = True) -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS phase3_strategy_states(
      strategy_version TEXT PRIMARY KEY, sleeve TEXT NOT NULL, state TEXT NOT NULL,
      reason TEXT NOT NULL, completed_oos_n INTEGER NOT NULL, qualifying_regimes INTEGER NOT NULL,
      mean_cost_adjusted_return REAL, health_status TEXT NOT NULL, state_version TEXT NOT NULL,
      evaluated_at TEXT NOT NULL, activated_at TEXT, suspended_at TEXT, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS phase3_risk_decisions(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, strategy_version TEXT NOT NULL,
      decision_time TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
      equity REAL, account_drawdown_pct REAL, base_stop_risk_pct REAL, scaled_stop_risk_pct REAL,
      stop_price REAL, stop_distance REAL, risk_budget REAL, requested_notional REAL,
      portfolio_heat_before_pct REAL, portfolio_heat_after_pct REAL, gross_exposure_after_pct REAL,
      symbol_exposure_after_pct REAL, cluster_exposure_after_pct REAL, regime TEXT,
      regime_multiplier REAL, drawdown_multiplier REAL, allocation_multiplier REAL,
      profile_version TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS phase3_strategy_allocations(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
      allocation_weight REAL NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
      profile_version TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(run_id,strategy_version));
    CREATE TABLE IF NOT EXISTS account_equity_watermarks(
      account_key TEXT PRIMARY KEY, peak_equity REAL NOT NULL, latest_equity REAL NOT NULL,
      drawdown_pct REAL NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS phase3_activation_events(
      id TEXT PRIMARY KEY, release_commit TEXT NOT NULL, activated_at TEXT NOT NULL,
      status TEXT NOT NULL, paper_identity_json TEXT NOT NULL, account_json TEXT NOT NULL,
      integrity_json TEXT NOT NULL, strategy_states_json TEXT NOT NULL, profile_version TEXT NOT NULL);
    """
    for statement in sql.split(";"):
        if statement.strip(): conn.execute(statement)
    if record_migration:
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
                     (PHASE3_SCHEMA_VERSION, iso_now(), "additive Phase 3 strategy states, allocations, risk decisions, and equity watermark"))


def regime_multiplier(regime: str) -> float:
    value = str(regime).lower()
    if "extreme" in value or "panic" in value or "defensive" in value or "downtrend" in value:
        return 0.50
    if "high" in value or "elevated" in value or "mixed" in value or "uncertain" in value:
        return 0.75
    if "favorable" in value or ("uptrend" in value and "normal" in value):
        return 1.0
    return 0.75


def drawdown_multiplier(drawdown_pct: float) -> float:
    if drawdown_pct >= 6: return 0.0
    if drawdown_pct >= 4: return 0.50
    if drawdown_pct >= 2: return 0.75
    return 1.0


class Phase3Controller:
    def __init__(self, storage: Any, config: Mapping[str, Any], run_id: str) -> None:
        self.storage, self.config, self.run_id = storage, config, run_id
        self.profile = Phase3RiskProfile.from_config(config)

    def update_equity(self, equity: float, account_key: str = "alpaca-paper") -> float:
        if not math.isfinite(equity) or equity <= 0: raise ValueError("authoritative positive equity required")
        rows = self.storage.fetch_all("SELECT peak_equity FROM account_equity_watermarks WHERE account_key=?", (account_key,))
        historical = self.storage.fetch_all("SELECT MAX(equity) peak FROM cash_snapshots WHERE equity IS NOT NULL")
        historical_peak = float(historical[0]["peak"]) if historical and historical[0].get("peak") is not None else equity
        peak = max(equity, historical_peak, float(rows[0]["peak_equity"]) if rows else equity)
        drawdown = max(0.0, (peak - equity) / peak * 100.0)
        self.storage.execute("""INSERT INTO account_equity_watermarks(account_key,peak_equity,latest_equity,drawdown_pct,source,updated_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET peak_equity=excluded.peak_equity,
          latest_equity=excluded.latest_equity,drawdown_pct=excluded.drawdown_pct,source=excluded.source,updated_at=excluded.updated_at""",
          (account_key, peak, equity, drawdown, "authoritative_alpaca_paper_account", iso_now()))
        return drawdown

    def reconciliation_health(self) -> tuple[bool, dict[str, int]]:
        report = DurableExecutionStore(self.storage).integrity_report()
        critical = ("terminal_intents_with_active_reservations", "active_intents_missing_reservations",
                    "fills_exceeding_quantity", "stale_unknown_intents", "stale_partial_fills",
                    "broker_relevant_missing_identity")
        return not any(report.get(key, 0) for key in critical), report

    def refresh_strategy_states(self) -> dict[str, str]:
        healthy, report = self.reconciliation_health()
        now = iso_now(); states: dict[str, str] = {}
        for sleeve, version in STRATEGY_VERSIONS.items():
            rows = self.storage.fetch_all("""SELECT ro.regime,ro.execution_type,ro.source_table,ro.provenance_json,
              r.cost_adjusted_return,ro.split_label
              FROM research_opportunities ro JOIN research_outcomes r ON r.opportunity_id=ro.id
              WHERE ro.strategy_version=? AND r.horizon_sessions=20 AND r.status='completed'""", (version,))
            oos = [r for r in rows if r.get("split_label") == "out_of_sample" and r.get("cost_adjusted_return") is not None and classify_evidence_type(
                r.get("execution_type"), r.get("source_table"), r.get("provenance_json")
            ) == SHADOW_OUTCOME]
            regimes = {str(r.get("regime")) for r in oos if r.get("regime")}
            mean = sum(float(r["cost_adjusted_return"]) for r in oos) / len(oos) if oos else None
            minimum = int(self.config.get("phase3", {}).get("promotion", {}).get("minimum_completed_oos", 100))
            qualifies = healthy and len(oos) >= minimum and len(regimes) >= 2 and mean is not None and mean > 0
            if not healthy: state, reason = "SUSPENDED", "reconciliation health failed"
            elif qualifies: state, reason = "ACTIVE", "positive cost-aware OOS evidence across multiple regimes"
            elif oos and mean is not None and mean <= 0: state, reason = "SUSPENDED", "non-positive cost-aware OOS expectancy"
            else: state, reason = "THROTTLED", "promotion evidence incomplete"
            states[version] = state
            self.storage.execute("""INSERT INTO phase3_strategy_states(strategy_version,sleeve,state,reason,completed_oos_n,
              qualifying_regimes,mean_cost_adjusted_return,health_status,state_version,evaluated_at,activated_at,suspended_at,payload)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_version) DO UPDATE SET state=excluded.state,reason=excluded.reason,
              completed_oos_n=excluded.completed_oos_n,qualifying_regimes=excluded.qualifying_regimes,
              mean_cost_adjusted_return=excluded.mean_cost_adjusted_return,health_status=excluded.health_status,
              evaluated_at=excluded.evaluated_at,activated_at=CASE WHEN excluded.state='ACTIVE' THEN COALESCE(phase3_strategy_states.activated_at,excluded.evaluated_at) ELSE phase3_strategy_states.activated_at END,
              suspended_at=CASE WHEN excluded.state='SUSPENDED' THEN excluded.evaluated_at ELSE phase3_strategy_states.suspended_at END,payload=excluded.payload""",
              (version,sleeve,state,reason,len(oos),len(regimes),mean,"healthy" if healthy else "unhealthy",
               "phase3_evidence_health_state_v1",now,now if state=="ACTIVE" else None,now if state=="SUSPENDED" else None,json_dumps({"integrity":report})))
        # The executable strategy uses the same evidence classification. This
        # prevents Phase 3 from treating a missing executable return history as
        # an implicit allocation while preserving all shadow sleeve states.
        executable_rows = self.storage.fetch_all("""SELECT ro.regime,ro.execution_type,ro.source_table,ro.provenance_json,
          ro.split_label,r.cost_adjusted_return
          FROM research_opportunities ro JOIN research_outcomes r ON r.opportunity_id=ro.id
          WHERE ro.strategy_version=? AND r.horizon_sessions=20 AND r.status='completed'""", (STRATEGY_VERSION,))
        executable_oos = [r for r in executable_rows if r.get("split_label") == "out_of_sample" and r.get("cost_adjusted_return") is not None and is_operational_evidence(r)]
        executable_regimes = {str(r.get("regime")) for r in executable_oos if r.get("regime")}
        executable_mean = sum(float(r["cost_adjusted_return"]) for r in executable_oos) / len(executable_oos) if executable_oos else None
        minimum = int(self.config.get("phase3", {}).get("promotion", {}).get("minimum_completed_oos", 100))
        executable_qualifies = healthy and len(executable_oos) >= minimum and len(executable_regimes) >= 2 and executable_mean is not None and executable_mean > 0
        if not healthy:
            executable_state, executable_reason = "SUSPENDED", "reconciliation health failed"
        elif executable_qualifies:
            executable_state, executable_reason = "ACTIVE", "positive executable paper-return evidence across multiple regimes"
        elif executable_oos and executable_mean is not None and executable_mean <= 0:
            executable_state, executable_reason = "SUSPENDED", "non-positive executable paper-return expectancy"
        else:
            executable_state, executable_reason = "THROTTLED", "executable strategy evidence incomplete"
        states[STRATEGY_VERSION] = executable_state
        self.storage.execute("""INSERT INTO phase3_strategy_states(strategy_version,sleeve,state,reason,completed_oos_n,
          qualifying_regimes,mean_cost_adjusted_return,health_status,state_version,evaluated_at,activated_at,suspended_at,payload)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(strategy_version) DO UPDATE SET state=excluded.state,reason=excluded.reason,
          completed_oos_n=excluded.completed_oos_n,qualifying_regimes=excluded.qualifying_regimes,
          mean_cost_adjusted_return=excluded.mean_cost_adjusted_return,health_status=excluded.health_status,
          evaluated_at=excluded.evaluated_at,payload=excluded.payload""",
          (STRATEGY_VERSION,"executable",executable_state,executable_reason,len(executable_oos),len(executable_regimes),executable_mean,
           "healthy" if healthy else "unhealthy","phase3_evidence_health_state_v2",now,now if executable_state=="ACTIVE" else None,
           now if executable_state=="SUSPENDED" else None,json_dumps({"integrity":report,"evidence_type":"operational"})))
        # Shadow strategy states are persisted for research and promotion
        # evidence, but never become executable Phase 3 allocations.
        eligible = [STRATEGY_VERSION] if states.get(STRATEGY_VERSION) == "ACTIVE" else []
        for version in eligible:
            weight = 1.0 / len(eligible)
            identifier = hashlib.sha256(f"{self.run_id}|{version}|{PROFILE_VERSION}".encode()).hexdigest()[:32]
            self.storage.execute("INSERT OR IGNORE INTO phase3_strategy_allocations VALUES(?,?,?,?,?,?,?,?)",
                                 (identifier,self.run_id,version,weight,"ACTIVE","equal risk across eligible evidence-approved strategies",PROFILE_VERSION,now))
        return states

    def allocation(self, strategy_version: str, states: Mapping[str, str]) -> float:
        return 1.0 if strategy_version == STRATEGY_VERSION and states.get(STRATEGY_VERSION) == "ACTIVE" else 0.0
