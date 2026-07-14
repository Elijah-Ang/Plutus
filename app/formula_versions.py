"""Version identifiers for every operational calculation boundary.

These values are persisted with decisions and evidence so a later formula
change cannot silently mix old and new risk or outcome calculations.
"""

CONFIGURATION_SCHEMA_VERSION = "plutus_effective_config_v7_release_gate_operational_paper"
STOP_POLICY_VERSION = "validated_atr_or_technical_stop_v2"
SIZING_POLICY_VERSION = "adaptive_minimum_ceiling_notional_v4_operational_paper"
RISK_DECISION_VERSION = "risk_engine_adaptive_ceiling_stop_probe_v4"
PHASE3_DECISION_VERSION = "phase3_adaptive_modes_v4_operational_paper"
PHASE4_ALLOCATION_VERSION = "phase4_evidence_aware_allocation_v5_dimensionally_normalized"
PHASE4_ALLOCATOR_VERSION = "adaptive_paper_allocator_v4_dimensionally_normalized"
PHASE4_SCHEMA_VERSION = "phase4_evidence_aware_operational_paper_v3"
ACCOUNTING_VERSION = "fifo_equity_unrealized_cashflow_v1"
EVIDENCE_VERSION = "phase1_outcome_v2_exit_session"
STRATEGY_PERFORMANCE_VERSION = "strategy_performance_v2_2_probe"
STRATEGY_POLICY_VERSION = "strategy_policy_v2_2_probe"
STRATEGY_PERFORMANCE_SCHEMA_VERSION = "strategy_profitability_engine_v1"
STRATEGY_POLICY_ENFORCEMENT_SCHEMA_VERSION = "strategy_policy_enforcement_v1"
STRATEGY_PROBE_POLICY_SCHEMA_VERSION = "strategy_probe_policy_v1"
ADAPTIVE_CONVICTION_FORMULA_VERSION = "adaptive_conviction_formula_v2_operational_paper"
ADAPTIVE_CONVICTION_SCHEMA_VERSION = "adaptive_conviction_operational_decisions_v2"
ADAPTIVE_SIZING_FORMULA_VERSION = "adaptive_sizing_formula_v2_operational_paper"
ADAPTIVE_SIZING_SCHEMA_VERSION = "adaptive_sizing_operational_decisions_v2"

# Deployment must not start until all of these additive migrations have been
# recorded and their required tables/columns are present.
REQUIRED_SCHEMA_VERSIONS = frozenset(
    {
        "p1_execution_safety_v1",
        "phase1_evidence_validation_v1",
        "phase2_shadow_strategies_v1",
        "phase3_adaptive_operational_paper_risk_v2",
        PHASE4_SCHEMA_VERSION,
        "runtime_safety_accounting_v1",
        STRATEGY_PERFORMANCE_SCHEMA_VERSION,
        STRATEGY_POLICY_ENFORCEMENT_SCHEMA_VERSION,
        STRATEGY_PROBE_POLICY_SCHEMA_VERSION,
        ADAPTIVE_CONVICTION_SCHEMA_VERSION,
        ADAPTIVE_SIZING_SCHEMA_VERSION,
    }
)
