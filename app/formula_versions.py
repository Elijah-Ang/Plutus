"""Version identifiers for every operational calculation boundary.

These values are persisted with decisions and evidence so a later formula
change cannot silently mix old and new risk or outcome calculations.
"""

CONFIGURATION_SCHEMA_VERSION = "plutus_effective_config_v14_crypto_decimal_sizing_risk_paper"
STOP_POLICY_VERSION = "monotonic_trend_protective_stop_v3"
SIZING_POLICY_VERSION = "adaptive_strategy_sleeve_winner_v5_operational_paper"
RISK_DECISION_VERSION = "risk_engine_position_risk_rotation_sleeve_v5"
PHASE3_DECISION_VERSION = "phase3_registry_sleeves_v5_operational_paper"
PHASE4_ALLOCATION_VERSION = "phase4_multi_strategy_allocation_v7_stop_risk_dollars"
PHASE4_ALLOCATOR_VERSION = "multi_strategy_paper_allocator_v6_explicit_units"
PHASE4_SCHEMA_VERSION = "phase4_multi_strategy_operational_paper_v5_units"
ACCOUNTING_VERSION = "fifo_equity_unrealized_cashflow_v1"
EVIDENCE_VERSION = "phase1_outcome_v2_exit_session"
STRATEGY_PERFORMANCE_VERSION = "strategy_performance_v2_4_validation_attribution"
STRATEGY_POLICY_VERSION = "strategy_policy_v2_4_validation_attribution"
STRATEGY_PERFORMANCE_SCHEMA_VERSION = "strategy_profitability_engine_v1"
TRADE_ECONOMICS_FORMULA_VERSION = "candidate_trade_economics_v1"
TRADE_ECONOMICS_SCHEMA_VERSION = "candidate_trade_economics_schema_v1"
PROFITABILITY_RANKING_FORMULA_VERSION = "candidate_profitability_ranking_v1"
PROFITABILITY_RANKING_SCHEMA_VERSION = "candidate_profitability_decisions_v1"
PROFITABILITY_VALIDATION_FORMULA_VERSION = "profitability_validation_v1"
PROFITABILITY_VALIDATION_SCHEMA_VERSION = "profitability_validation_authority_v1"
PROFIT_ATTRIBUTION_FORMULA_VERSION = "profit_attribution_v1"
PROFIT_ATTRIBUTION_SCHEMA_VERSION = "profit_attribution_records_v1"
CRYPTO_CAPABILITY_FORMULA_VERSION = "alpaca_spot_crypto_capability_v1"
CRYPTO_CAPABILITY_SCHEMA_VERSION = "alpaca_spot_crypto_capability_snapshot_v1"
CRYPTO_MARKET_DATA_FORMULA_VERSION = "alpaca_crypto_market_evidence_v1"
CRYPTO_MARKET_DATA_SCHEMA_VERSION = "alpaca_crypto_market_evidence_schema_v1"
CRYPTO_SIZING_FORMULA_VERSION = "alpaca_crypto_decimal_stop_risk_sizing_v1"
CRYPTO_SIZING_SCHEMA_VERSION = "alpaca_crypto_decimal_sizing_decisions_v1"
CRYPTO_RISK_FORMULA_VERSION = "alpaca_crypto_portfolio_risk_authority_v1"
CRYPTO_RISK_SCHEMA_VERSION = "alpaca_crypto_portfolio_risk_snapshots_v1"
STRATEGY_POLICY_ENFORCEMENT_SCHEMA_VERSION = "strategy_policy_enforcement_v1"
STRATEGY_PROBE_POLICY_SCHEMA_VERSION = "strategy_probe_policy_v1"
ADAPTIVE_CONVICTION_FORMULA_VERSION = "adaptive_conviction_formula_v2_operational_paper"
ADAPTIVE_CONVICTION_SCHEMA_VERSION = "adaptive_conviction_operational_decisions_v2"
ADAPTIVE_SIZING_FORMULA_VERSION = "adaptive_sizing_formula_v2_operational_paper"
ADAPTIVE_SIZING_SCHEMA_VERSION = "adaptive_sizing_operational_decisions_v2"
STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION = "strategy_execution_registry_formula_v1"
STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION = "strategy_execution_registry_v1"
POSITION_RISK_FORMULA_VERSION = "position_open_risk_v1_operational_paper"
TREND_MANAGEMENT_FORMULA_VERSION = "trend_management_v2_final_mode_invariants"
WINNER_EXPANSION_FORMULA_VERSION = "winner_expansion_v1_operational_paper"
WINNER_EXPANSION_SCHEMA_VERSION = "phase4_2c_winner_expansion_v1"
ROTATION_FORMULA_VERSION = "actual_fill_reconciled_capacity_v2_lifecycle_bound"
ROTATION_SCHEMA_VERSION = "exit_first_rotation_v2_lifecycle_bound"
PLUTUS_AUDIT_SCHEMA_VERSION = "plutus_audit_hardening_v1"
FINAL_HARDENING_SCHEMA_VERSION = "plutus_final_hardening_v1"
FINAL_REVIEW_HARDENING_SCHEMA_VERSION = "plutus_final_review_hardening_v2"

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
        TRADE_ECONOMICS_SCHEMA_VERSION,
        PROFITABILITY_RANKING_SCHEMA_VERSION,
        PROFITABILITY_VALIDATION_SCHEMA_VERSION,
        PROFIT_ATTRIBUTION_SCHEMA_VERSION,
        CRYPTO_CAPABILITY_SCHEMA_VERSION,
        CRYPTO_MARKET_DATA_SCHEMA_VERSION,
        CRYPTO_SIZING_SCHEMA_VERSION,
        CRYPTO_RISK_SCHEMA_VERSION,
        STRATEGY_POLICY_ENFORCEMENT_SCHEMA_VERSION,
        STRATEGY_PROBE_POLICY_SCHEMA_VERSION,
        ADAPTIVE_CONVICTION_SCHEMA_VERSION,
        ADAPTIVE_SIZING_SCHEMA_VERSION,
        STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION,
        WINNER_EXPANSION_SCHEMA_VERSION,
        ROTATION_SCHEMA_VERSION,
        PLUTUS_AUDIT_SCHEMA_VERSION,
        FINAL_HARDENING_SCHEMA_VERSION,
        FINAL_REVIEW_HARDENING_SCHEMA_VERSION,
    }
)
