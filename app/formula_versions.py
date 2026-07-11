"""Version identifiers for every operational calculation boundary.

These values are persisted with decisions and evidence so a later formula
change cannot silently mix old and new risk or outcome calculations.
"""

CONFIGURATION_SCHEMA_VERSION = "plutus_effective_config_v2"
STOP_POLICY_VERSION = "validated_atr_or_technical_stop_v2"
SIZING_POLICY_VERSION = "canonical_minimum_ceiling_notional_v2"
RISK_DECISION_VERSION = "risk_engine_ceiling_and_stop_v2"
PHASE3_DECISION_VERSION = "phase3_risk_audit_v2"
PHASE4_ALLOCATION_VERSION = "phase4_allocation_audit_v2"
ACCOUNTING_VERSION = "fifo_equity_unrealized_cashflow_v1"
EVIDENCE_VERSION = "phase1_outcome_v2_exit_session"

# Deployment must not start until all of these additive migrations have been
# recorded and their required tables/columns are present.
REQUIRED_SCHEMA_VERSIONS = frozenset(
    {
        "p1_execution_safety_v1",
        "phase1_evidence_validation_v1",
        "phase2_shadow_strategies_v1",
        "phase3_moderate_paper_risk_v1",
        "phase4_adaptive_paper_allocation_v1",
        "runtime_safety_accounting_v1",
    }
)
