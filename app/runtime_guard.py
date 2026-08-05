from __future__ import annotations

import json
import os
import platform
import re
import stat
from pathlib import Path

STATE_ROOT = Path.home() / "Library" / "Application Support" / "TradingAgent"
RELEASE_ROOT = Path.home() / "TradingAgentReleases"
RUNTIME_LINK = Path.home() / "TradingAgentRuntime"
REQUIRED_SCHEMA_VERSION = "runtime_safety_accounting_v1"
REQUIRED_PYTHON_VERSION = "3.13.9"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PAPER_AUTHORITY_MODES = frozenset({"manual_only", "autonomous_paper"})


def paper_authority_mode(manifest: dict) -> str | None:
    """Return the manifest's bounded paper authority, or fail closed."""

    mode = str(manifest.get("paper_authority_mode") or "").strip()
    if not mode and manifest.get("manual_approval_only") is True:
        mode = "manual_only"
    if mode not in PAPER_AUTHORITY_MODES:
        return None
    if manifest.get("manual_approval_only") is not (mode == "manual_only"):
        return None
    autonomous_flag = manifest.get("autonomous_execution_enabled")
    if autonomous_flag is not None and autonomous_flag is not (mode == "autonomous_paper"):
        return None
    return mode


def config_paper_authority_mode(config: dict) -> str | None:
    """Return the configured bounded paper authority, or fail closed."""

    if config.get("mode") != "paper" or config.get("live_enabled") is not False:
        return None
    auto_enabled = config.get("auto_execution_enabled")
    mode = config.get("auto_execution_mode")
    if auto_enabled is False and mode == "manual_only":
        return "manual_only"
    if auto_enabled is True and mode == "autonomous_paper":
        return "autonomous_paper"
    return None

# These generated-evidence identities are part of the release authority.  A
# manifest that predates the isolated-wheel and final-inventory gates must not
# be accepted as a current production runtime merely because it says
# ``tests_verified``.
REQUIRED_ARTIFACT_EVIDENCE_HASHES = (
    "requirements_lock_sha256",
    "requirements_hash_lock_sha256",
    "dependency_inventory_sha256",
    "artifact_test_results_sha256",
    "tracked_source_inventory_sha256",
    "wheel_build_evidence_sha256",
    "release_wheel_sha256",
    "release_file_inventory_sha256",
)

# Runtime starts only after the migration ledger and the concrete columns used
# by safety gates agree. Checking only one marker row allowed a partial P1
# execution migration to look deployable.
REQUIRED_RUNTIME_TABLE_COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "runs": {"id", "started_at", "status", "mode"},
    "config_snapshots": {"run_id", "config_json", "effective_config_json", "effective_config_hash"},
    "risk_checks": {"run_id", "proposal_id", "name", "passed", "formula_version", "evidence_version", "config_hash"},
    "orders": {"id", "client_order_id", "status", "quote_bid", "quote_ask", "limit_price"},
    "fills": {"order_id", "qty", "price", "implementation_shortfall_bps", "qty_decimal", "price_decimal", "decimal_provenance", "decimal_accounting_version"},
    "order_intents": {"id", "logical_action_key", "client_order_id", "reserved_notional", "reserved_stop_risk", "state", "trading_mode", "strategy_registry_snapshot_id", "strategy_sleeve", "sleeve_allocation_id", "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling", "incremental_risk", "rotation_step_id", "approval_authority_fingerprint", "displayed_fingerprint", "execution_path", "risk_snapshot_id", "canonical_quantity", "canonical_notional", "canonical_stop_risk", "broker_invocation_occurred", "filled_quantity_decimal", "average_fill_price_decimal", "decimal_provenance", "decimal_accounting_version"},
    "broker_fill_events": {"intent_id", "broker_event_key", "cumulative_filled_quantity_decimal", "delta_quantity_decimal", "fill_price_decimal", "fees_decimal", "adjustments_decimal", "decimal_provenance", "decimal_accounting_version"},
    "broker_fill_evidence": {"intent_id", "broker_event_key", "broker_order_id", "client_order_id", "symbol", "side", "remote_status", "payload", "payload_fingerprint", "evidence_source", "captured_at"},
    "crypto_profitability_observations": {
        "observation_id", "input_fingerprint", "evidence_type", "status", "outcome_class",
        "symbol", "strategy_decision_id", "strategy_decision_fingerprint", "research_timestamp",
        "horizon_hours", "entry_price", "stop_price", "target_price", "cost_model_version",
        "fee_bps", "spread_bps", "slippage_bps", "bars_json", "input_evidence_json",
    },
    "crypto_profitability_decisions": {
        "decision_id", "symbol", "strategy_version", "strategy_decision_id",
        "strategy_decision_fingerprint", "config_hash", "sample_count",
        "unavailable_count", "win_probability", "severe_loss_rate", "uncertainty",
        "mean_net_return", "average_holding_hours", "correlation_to_portfolio",
        "minimum_samples", "validation_family_id", "validation_decision_id",
        "validation_decision_fingerprint", "walk_forward_status",
        "validation_sample_count", "validation_fold_count", "validation_lower_net_r",
        "validation_fdr_q_value", "validation_reason", "validation_status",
        "rejection_reasons_json",
        "observation_fingerprints_json", "input_fingerprint", "decision_fingerprint",
        "formula_version", "schema_version", "metrics_json",
    },
    "risk_reservations": {"intent_id", "active_notional", "active_stop_risk", "state", "strategy_version", "strategy_sleeve", "sleeve_allocation_id", "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling", "incremental_risk", "risk_value", "risk_unit", "conversion_equity", "conversion_equity_as_of", "risk_formula_version"},
    "approvals": {"proposal_id", "proposal_reference_price", "refreshed_bid", "refreshed_ask", "directional_price_move_bps", "movement_classification", "final_limit_price", "directional_validation_reason", "authority_envelope_json", "authority_fingerprint", "display_envelope_id", "displayed_fingerprint", "approval_source_type", "execution_path"},
    "proposal_display_envelopes": {"proposal_id", "proposal_version", "telegram_message_id", "displayed_envelope_json", "displayed_fingerprint"},
    "execution_risk_snapshots": {
        "proposal_id", "approval_id", "account_id_hash", "trading_mode", "snapshot_fingerprint",
        "expires_at", "authoritative", "snapshot_version", "position_fingerprint",
        "open_order_fingerprint", "durable_state_json", "durable_state_fingerprint",
        "risk_context_json", "risk_context_fingerprint", "execution_candidate_json",
        "execution_candidate_fingerprint", "market_open", "control_evidence_json",
        "control_evidence_fingerprint",
    },
    "exit_blocker_states": {"symbol", "generation", "state", "source_type", "source_id", "active", "recovery_classification", "detail_json"},
    "reconciliation_attempts": {"intent_id", "outcome", "created_at"},
    "telegram_updates": {"update_id", "message_id", "message_timestamp", "received_at", "processing_state"},
    "research_outcomes": {"opportunity_id", "horizon_sessions", "status", "exit_session", "calculation_version", "outcome_class"},
    "phase3_risk_decisions": {"run_id", "requested_notional", "binding_caps_json", "evidence_version", "formula_version", "performance_snapshot_id", "policy_decision_id", "strategy_state", "permitted_stop_risk_pct"},
    "phase3_strategy_allocations": {"run_id", "strategy_version", "allocation_weight", "state", "reason", "profile_version", "created_at"},
    "phase3_strategy_states": {"strategy_version", "sleeve", "state", "reason", "completed_oos_n", "qualifying_regimes", "health_status", "state_version", "evaluated_at", "payload"},
    "account_equity_watermarks": {"account_key", "peak_equity", "latest_equity", "drawdown_pct", "source", "updated_at", "peak_equity_decimal", "latest_equity_decimal", "drawdown_pct_decimal", "decimal_provenance", "decimal_accounting_version"},
    "phase4_strategy_estimates": {"run_id", "strategy_version", "estimated_at", "state", "estimator_version", "evidence_fingerprint", "payload"},
    "phase4_covariance_snapshots": {"run_id", "calculated_at", "strategy_order_json", "covariance_json", "correlation_json", "observation_counts_json", "method", "fallback_used", "payload"},
    "phase4_strategy_states": {"strategy_version", "state", "reason", "estimate_id", "state_version", "evaluated_at", "payload"},
    "phase4_stress_results": {"allocation_id", "scenario", "assumed_loss", "portfolio_loss", "passed", "stress_version", "payload"},
    "phase4_allocation_decisions": {"run_id", "strategy_weights_json", "allocation_class", "operational_kelly_used", "binding_caps_json", "evidence_versions_json", "strategy_policy_map_json", "strategy_policy_version", "probe_allocation_json", "payload"},
    "trade_proposals": {"performance_snapshot_id", "policy_decision_id", "strategy_state", "permitted_stop_risk_pct", "strategy_policy_version", "trade_economics_id", "strategy_registry_snapshot_id", "strategy_sleeve", "sleeve_allocation_id", "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling", "winner_expansion_decision_id", "pyramiding_milestone_id", "pyramiding_milestone_key", "rotation_group_id", "rotation_step_id", "relationship_type"},
    "position_sizing_decisions": {"performance_snapshot_id", "policy_decision_id", "strategy_state", "permitted_stop_risk_pct", "strategy_policy_version"},
    "cash_snapshots": {"equity", "realized_fifo_pnl", "account_equity_change", "unrealized_change", "external_cash_flow", "accounting_version", "equity_decimal", "cash_decimal", "settled_cash_decimal", "realized_fifo_pnl_decimal", "unrealized_pl_decimal", "account_equity_change_decimal", "unrealized_change_decimal", "external_cash_flow_decimal", "decimal_provenance", "decimal_accounting_version"},
    "position_lifecycles": {"symbol", "side", "state", "opened_at", "opening_quantity", "current_quantity", "average_entry_price", "source", "opening_quantity_frozen", "opening_quantity_decimal", "current_quantity_decimal", "average_entry_price_decimal", "decimal_provenance", "decimal_accounting_version"},
    "position_lots": {"strategy_version", "entry_proposal_id", "entry_intent_id", "entry_regime", "entry_score", "initial_risk_dollars", "config_hash", "evidence_version", "formula_version", "original_quantity_decimal", "remaining_quantity_decimal", "unit_cost_decimal", "fees_allocated_decimal", "initial_risk_dollars_decimal", "decimal_provenance", "decimal_accounting_version"},
    "realized_pnl_events": {"quantity_decimal", "gross_proceeds_decimal", "cost_basis_decimal", "fees_decimal", "adjustments_decimal", "realized_pl_decimal", "remaining_position_quantity_decimal", "decimal_provenance", "decimal_accounting_version"},
    "lot_consumptions": {"broker_event_key", "sell_intent_id", "position_lifecycle_id", "lot_id", "allocated_proceeds", "allocated_cost_basis", "allocated_buy_fees", "allocated_sell_fees", "allocated_adjustments", "realized_pnl", "accounting_version", "quantity_decimal", "allocated_proceeds_decimal", "allocated_cost_basis_decimal", "allocated_buy_fees_decimal", "allocated_sell_fees_decimal", "allocated_adjustments_decimal", "realized_pnl_decimal", "decimal_provenance", "decimal_accounting_version"},
    "strategy_trade_records": {"source_key", "strategy_version", "evidence_class", "attribution_status", "r_multiple", "evidence_version", "formula_version", "profit_attribution_id"},
    "strategy_performance_snapshots": {"strategy_version", "performance_version", "policy_version", "quality_score", "metrics_json", "input_fingerprint", "validation_family_id", "validation_decision_id", "validation_status", "validation_fingerprint"},
    "strategy_policy_decisions": {"strategy_version", "state", "performance_snapshot_id", "enforcement_enabled", "policy_version", "schema_version", "input_fingerprint", "evidence_version", "configuration_version", "config_hash", "validation_family_id", "validation_decision_id", "validation_status", "validation_fingerprint"},
    "trade_economics_records": {
        "candidate_id", "proposal_id", "record_class", "asset_class", "symbol",
        "strategy_version", "strategy_state", "market_regime",
        "volatility_regime", "liquidity_regime", "trend_regime",
        "breadth_regime", "proposed_quantity", "proposed_notional",
        "entry_estimate", "limit_price", "stop_price", "target_price",
        "expected_gross_upside", "expected_downside",
        "gross_reward_to_risk", "expected_win_probability",
        "conservative_win_probability", "expected_average_win",
        "expected_average_loss", "expected_gross_profit",
        "expected_execution_cost", "expected_uncertainty_cost",
        "expected_net_profit",
        "conservative_expected_net_profit", "expected_net_r",
        "conservative_expected_net_r", "break_even_win_probability",
        "expected_total_cost", "expected_capital_efficiency",
        "expected_annualized_capital_efficiency",
        "expected_holding_period_days", "worst_reasonable_loss",
        "maximum_approved_loss", "profitability_eligible",
        "performance_snapshot_id", "policy_decision_id", "config_hash",
        "formula_versions_json", "input_fingerprint", "record_fingerprint",
        "formula_version", "schema_version",
    },
    "candidate_profitability_decisions": {
        "trade_economics_id", "candidate_id", "run_id", "symbol",
        "strategy_version", "performance_snapshot_id", "policy_decision_id",
        "profitability_eligible", "rejection_reasons_json",
        "profitability_quality_score", "quality_components_json",
        "ranking_score", "ranking_key_json", "score_context_json",
        "config_hash", "formula_versions_json", "input_fingerprint",
        "decision_fingerprint", "formula_version", "schema_version",
    },
    "profitability_validation_families": {
        "family_key", "as_of", "hypotheses_json", "observations_json",
        "policy_json", "configuration_version", "config_hash",
        "evidence_version", "formula_versions_json", "input_fingerprint",
        "family_fingerprint", "formula_version", "schema_version",
    },
    "profitability_validation_decisions": {
        "family_id", "hypothesis_id", "strategy_version",
        "stability_group", "status", "sample_count", "fold_count",
        "bootstrap_lower_net_r", "bootstrap_p_value", "fdr_q_value",
        "fdr_accepted", "positive_fold_ratio",
        "parameter_stability_ratio", "metrics_json", "input_fingerprint",
        "decision_fingerprint", "formula_version", "schema_version",
    },
    "profitability_validation_folds": {
        "decision_id", "fold", "train_start", "train_end", "test_start",
        "test_end", "train_ids_json", "test_ids_json",
        "purged_train_count", "embargo_group_count", "test_mean_r",
        "fold_fingerprint",
    },
    "profit_attribution_records": {
        "position_lifecycle_id", "symbol", "strategy_version",
        "evidence_class", "status", "confidence", "opened_at", "closed_at",
        "quantity", "initial_risk_dollars", "actual_cost_basis",
        "realized_gross_pnl", "realized_fee_drag", "realized_net_pnl",
        "realized_adjustments",
        "actual_r_multiple", "expected_gross_profit",
        "expected_execution_cost", "expected_net_profit",
        "expected_vs_realized_variance", "market_outcome_variance",
        "execution_cost_variance", "reconciliation_residual",
        "input_json", "components_json", "input_fingerprint",
        "record_fingerprint", "accounting_version", "evidence_version",
        "formula_version", "schema_version",
    },
    "crypto_capability_snapshots": {
        "id", "run_id", "provider", "broker", "trading_mode",
        "paper_account_id_hash", "market_profile", "data_feed", "asset_count",
        "official_contract_version", "official_contract_fingerprint",
        "config_hash", "formula_version", "schema_version", "captured_at",
        "expires_at", "authoritative", "failure_reasons_json", "evidence_json",
        "input_fingerprint", "snapshot_fingerprint",
    },
    "crypto_asset_capabilities": {
        "snapshot_id", "symbol", "asset_id", "asset_class", "exchange",
        "status", "tradable", "fractionable", "marginable", "shortable",
        "easy_to_borrow", "min_order_size", "min_trade_increment",
        "price_increment", "base_asset", "quote_currency", "authoritative",
        "failure_reasons_json", "asset_json", "asset_fingerprint",
    },
    "crypto_market_data_evidence": {
        "id", "run_id", "research_run_id", "capability_snapshot_id",
        "capability_snapshot_fingerprint", "symbol", "provider", "data_feed",
        "bid_price", "ask_price", "bid_size", "ask_size", "quote_timestamp",
        "quote_age_seconds", "trade_price", "trade_size", "trade_timestamp",
        "trade_age_seconds", "orderbook_bid_price", "orderbook_ask_price",
        "orderbook_bid_size", "orderbook_ask_size", "orderbook_timestamp",
        "orderbook_age_seconds", "spread_bps", "top_of_book_notional",
        "authoritative", "execution_eligible", "failure_reasons_json", "warnings_json",
        "config_hash", "formula_version", "schema_version", "captured_at",
        "evidence_json", "evidence_fingerprint",
    },
    "crypto_risk_snapshots": {
        "id", "run_id", "request_fingerprint", "capability_snapshot_id",
        "capability_snapshot_fingerprint", "market_evidence_id",
        "market_evidence_fingerprint", "symbol", "paper_account_id_hash",
        "account_json", "account_fingerprint", "positions_json",
        "positions_fingerprint", "open_orders_json", "open_orders_fingerprint",
        "durable_state_json", "durable_state_fingerprint", "loss_evidence_json",
        "loss_evidence_fingerprint", "volatility_evidence_json",
        "volatility_evidence_fingerprint", "aggregate_json",
        "derived_authority_json", "authoritative", "failure_reasons_json",
        "config_hash", "formula_version", "schema_version", "captured_at",
        "expires_at", "snapshot_json", "snapshot_fingerprint",
    },
    "crypto_sizing_decisions": {
        "id", "run_id", "request_fingerprint", "risk_snapshot_id",
        "risk_snapshot_fingerprint", "capability_snapshot_id",
        "capability_snapshot_fingerprint", "market_evidence_id",
        "market_evidence_fingerprint", "symbol", "side", "action",
        "request_basis", "limit_price", "stop_price", "stop_execution_price",
        "canonical_quantity", "canonical_notional", "canonical_stop_risk",
        "gross_stop_risk", "estimated_fees", "estimated_stop_slippage",
        "minimum_order_size", "quantity_increment", "price_increment",
        "eligible", "authoritative", "execution_authorized", "blockers_json",
        "binding_caps_json", "config_hash", "formula_version", "schema_version",
        "created_at", "decision_json", "decision_fingerprint",
    },
    "crypto_risk_decisions": {
        "id", "run_id", "snapshot_id", "snapshot_fingerprint",
        "sizing_decision_id", "sizing_fingerprint", "risk_eligible",
        "execution_authorized", "checks_json", "reasons_json", "config_hash",
        "formula_version", "schema_version", "created_at", "decision_json",
        "decision_fingerprint",
    },
    "crypto_strategy_decisions": {
        "id", "run_id", "research_run_id", "market_evidence_id",
        "market_evidence_fingerprint", "symbol", "selected_strategy", "action",
        "lifecycle", "signal_eligible", "proposal_authorized",
        "execution_authorized", "stop_price", "target_price",
        "expected_reward_r", "blockers_json", "input_fingerprint", "config_hash",
        "formula_version", "schema_version", "as_of", "created_at",
        "decision_json", "decision_fingerprint",
    },
    "crypto_proposal_previews": {
        "id", "run_id", "strategy_decision_id", "strategy_decision_fingerprint",
        "risk_decision_id", "risk_decision_fingerprint", "risk_snapshot_id",
        "risk_snapshot_fingerprint", "sizing_decision_id",
        "sizing_decision_fingerprint", "capability_snapshot_id",
        "capability_snapshot_fingerprint", "market_evidence_id",
        "market_evidence_fingerprint", "symbol", "strategy", "action",
        "request_basis", "status", "manual_approval_eligible",
        "execution_authorized", "created_at", "expires_at", "display_json",
        "display_fingerprint", "proposal_json", "proposal_fingerprint",
        "config_hash", "formula_version", "schema_version",
    },
    "crypto_paper_proposals": {
        "id", "run_id", "strategy_decision_id", "strategy_decision_fingerprint",
        "risk_decision_id", "risk_decision_fingerprint", "risk_snapshot_id",
        "risk_snapshot_fingerprint", "sizing_decision_id", "sizing_decision_fingerprint",
        "capability_snapshot_id", "capability_snapshot_fingerprint", "market_evidence_id",
        "market_evidence_fingerprint", "symbol", "side", "action", "request_basis",
        "quantity", "notional", "limit_price", "stop_price", "stop_risk", "status",
        "created_at", "expires_at", "config_hash", "formula_versions_json",
        "schema_version", "display_json", "display_fingerprint", "proposal_json",
        "proposal_fingerprint", "telegram_message_id", "telegram_chat_id",
        "telegram_display_text", "telegram_display_fingerprint", "telegram_bound_at",
    },
    "crypto_paper_approvals": {
        "id", "proposal_id", "sender_id", "raw_message", "reply_to_message_id",
        "parsed_action", "status", "approved_at", "consumed_at", "display_fingerprint",
        "telegram_chat_id", "telegram_message_fingerprint", "approval_fingerprint",
    },
    "crypto_paper_reservations": {
        "id", "intent_id", "symbol", "initial_notional", "active_notional",
        "initial_stop_risk", "active_stop_risk", "state", "created_at", "updated_at",
        "released_at", "release_reason", "reservation_fingerprint",
    },
    "crypto_paper_intents": {
        "id", "proposal_id", "approval_id", "logical_action_key", "client_order_id",
        "symbol", "side", "request_basis", "requested_quantity", "requested_notional",
        "limit_price", "stop_price", "reserved_notional", "reserved_stop_risk", "state",
        "broker_invocation_occurred", "submission_attempt_count", "broker_order_id",
        "last_error", "created_at", "updated_at", "first_submission_at", "terminal_at", "position_lifecycle_id",
    },
    "crypto_paper_fills": {
        "id", "intent_id", "broker_event_key", "quantity", "price", "fees", "occurred_at",
        "received_at", "payload", "broker_order_id", "client_order_id", "evidence_id",
        "evidence_fingerprint", "payload_fingerprint",
    },
    "crypto_paper_order_events": {
        "id", "intent_id", "event_key", "from_state", "to_state", "event_type", "safe_detail", "created_at",
    },
    "crypto_paper_lots": {
        "id", "symbol", "source_fill_event_key", "opened_at", "original_quantity",
        "remaining_quantity", "unit_cost", "fees_allocated", "created_at", "position_lifecycle_id", "lot_fingerprint",
    },
    "crypto_paper_realized_pnl": {
        "id", "broker_event_key", "intent_id", "symbol", "quantity", "gross_proceeds",
        "cost_basis", "fees", "realized_pl", "occurred_at", "created_at", "evidence_fingerprint", "confidence",
        "position_lifecycle_id", "pnl_fingerprint",
    },
    "crypto_paper_order_evidence": {
        "id", "intent_id", "broker_order_id", "client_order_id", "symbol", "side", "status",
        "requested_quantity", "requested_notional", "filled_quantity", "filled_average_price",
        "fees", "payload", "payload_fingerprint", "captured_at", "verified", "verification_error",
    },
    "crypto_paper_reconciliation_events": {
        "id", "intent_id", "event_type", "broker_order_id", "client_order_id", "payload",
        "payload_fingerprint", "created_at",
    },
    "crypto_paper_rejections": {
        "id", "proposal_id", "sender_id", "telegram_chat_id", "raw_message", "reply_to_message_id",
        "telegram_message_fingerprint", "rejection_fingerprint", "rejected_at",
    },
    "crypto_paper_telegram_outbox": {
        "id", "proposal_id", "chat_id", "rendered_text", "display_fingerprint",
        "status", "attempts", "telegram_message_id", "telegram_chat_id", "error",
        "created_at", "updated_at", "sent_at",
    },
    "crypto_paper_position_management": {
        "id", "symbol", "quantity", "average_entry_price", "peak_price", "stop_price",
        "profit_target_price", "time_stop_at", "thesis_fingerprint", "last_action",
        "last_proposal_id", "updated_at", "created_at",
    },
    "crypto_performance_links": {
        "id", "fill_id", "intent_id", "setup_id", "outcome_id", "broker_order_id",
        "evidence_fingerprint", "realized_pl", "link_fingerprint", "created_at", "side",
        "action", "quantity", "price", "fees", "fill_type", "position_lifecycle_id", "order_status",
    },
    "performance_setups": {
        "id", "run_id", "symbol", "asset_class", "setup_type", "action_decision", "proposed",
        "proposal_id", "broker_order_id", "fill_id", "order_status", "fill_price", "fill_qty",
    },
    "performance_outcomes": {
        "id", "setup_id", "run_id", "symbol", "proposal_id", "broker_order_id", "fill_id",
        "actual_or_shadow", "entry_time", "entry_price", "entry_notional", "entry_qty", "status",
    },
    "cross_asset_allocation_plans": {
        "id", "run_id", "as_of", "expires_at", "portfolio_snapshot_id",
        "portfolio_snapshot_fingerprint", "candidate_set_json",
        "candidate_set_fingerprint", "policy_json", "policy_fingerprint",
        "plan_json", "plan_fingerprint", "execution_authorized",
        "config_hash", "formula_version", "schema_version", "created_at",
    },
    "crypto_research_runs": {
        "capability_snapshot_id", "capability_snapshot_fingerprint",
        "capability_authoritative",
    },
    "crypto_research_snapshots": {
        "capability_snapshot_id", "capability_snapshot_fingerprint",
        "capability_authoritative", "market_evidence_id", "market_evidence_fingerprint",
        "market_evidence_authoritative", "market_execution_eligible",
    },
    "strategy_registry_snapshots": {"run_id", "evaluated_at", "authorized_strategies_json", "rejected_strategies_json", "configuration_version", "config_hash", "evaluation_fingerprint"},
    "strategy_registry_decisions": {"snapshot_id", "run_id", "strategy_version", "authorized", "policy_state", "reasons_json", "evidence_version", "configuration_version", "config_hash", "decision_fingerprint"},
    "position_stop_history": {"position_lifecycle_id", "stop_sequence", "prior_stop", "new_stop", "management_mode", "stop_as_of", "formula_version", "decision_fingerprint"},
    "pyramiding_milestones": {"position_lifecycle_id", "milestone_key", "status", "active_proposal_id", "intent_id", "identity_fingerprint", "retry_count", "generation"},
    "add_risk_decisions": {"proposal_id", "approval_id", "decision_stage", "position_lifecycle_id", "eligible", "pre_add_open_risk_net", "post_add_open_risk_net", "incremental_risk", "incremental_risk_decimal", "consumed_risk", "released_risk", "formula_version", "decision_fingerprint"},
    "trend_management_decisions": {"position_lifecycle_id", "management_mode", "prior_stop", "protective_stop", "stop_monotonic", "formula_version", "decision_fingerprint"},
    "position_management_state": {"position_lifecycle_id", "authoritative_protective_stop", "protective_stop_as_of", "protective_stop_source", "protective_stop_formula_version", "protective_stop_sequence", "management_mode", "trend_management_formula_version", "peak_r_multiple", "last_completed_pyramiding_milestone", "entry_fill_id", "entry_order_intent_id", "initial_risk_reconstruction_source", "initial_risk_formula_version", "initial_risk_evidence_version"},
    "rotation_groups": {"run_id", "state", "expires_at", "actual_released_notional", "actual_released_risk", "reconciliation_fingerprint", "registry_snapshot_id", "allocation_id", "origin_run_id", "revalidation_run_id", "revalidation_registry_snapshot_id", "revalidation_allocation_id", "revalidated_at", "decision_fingerprint", "position_lifecycle_id", "exit_proposal_fingerprint", "contingent_candidate_fingerprint", "displayed_approval_fingerprint", "workflow_structure_fingerprint"},
    "rotation_steps": {"group_id", "sequence", "role", "proposal_id", "intent_id", "state", "filled_quantity", "filled_notional", "released_risk"},
    "rotation_contingent_entries": {"group_id", "candidate_key", "strategy_version", "displayed_max_quantity", "displayed_max_notional", "displayed_max_stop_risk", "state", "final_quantity", "final_notional", "final_stop_risk"},
    "rotation_events": {"group_id", "event_key", "event_type", "from_state", "to_state", "notification_claimed_at", "notification_sent_at"},
    "rotation_group_approvals": {
        "group_id", "approval_id", "sender_id", "ceiling_fingerprint", "status", "consumed_at",
        "display_envelope_id", "display_fingerprint", "workflow_fingerprint", "telegram_message_id",
    },
    "rotation_group_display_envelopes": {
        "group_id", "telegram_message_id", "displayed_at", "expires_at", "envelope_json",
        "display_fingerprint", "workflow_fingerprint",
    },
    "take_profit_milestones": {"position_lifecycle_id", "take_profit_level", "target_quantity", "cumulative_filled_quantity", "completed_fraction", "status", "formula_version"},
    "take_profit_milestone_actions": {"milestone_id", "proposal_id", "order_intent_id", "requested_quantity", "cumulative_filled_quantity", "completed_fraction", "status"},
    "take_profit_milestone_fill_links": {"milestone_id", "action_id", "broker_fill_event_id", "broker_event_key", "delta_quantity", "cumulative_intent_quantity"},
    "adaptive_conviction_operational_decisions": {"proposal_id", "decision_stage", "approval_id", "strategy_version", "deployment_mode", "opportunity_class", "recommended_stop_risk_pct", "operational_stop_risk_pct", "position_target", "binding_cap", "raw_inputs_json", "formula_version", "configuration_schema_version", "config_hash", "decision_fingerprint", "operating_mode", "operational_enforced", "report_only"},
    "adaptive_sizing_operational_decisions": {"stage", "proposal_id", "approval_id", "strategy_version", "policy_id", "adaptive_conviction_decision_id", "operational_constrained_notional", "adaptive_requested_notional", "adaptive_constrained_notional", "adaptive_quantity", "adaptive_constrained_stop_risk_pct", "adaptive_constrained_stop_risk_dollars", "ceilings_json", "ceiling_path_json", "binding_adaptive_cap", "comparison_direction", "displayed_adaptive_ceiling", "future_activation_notional", "final_operational_notional", "final_operational_quantity", "final_revalidation_outcome", "missing_inputs_json", "raw_inputs_json", "evidence_version", "formula_version", "schema_version", "configuration_version", "decision_fingerprint", "operating_mode", "operational_enforced", "report_only"},
}


class RuntimeGuardError(RuntimeError):
    pass


def is_production_path(path: str | Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(STATE_ROOT.resolve())
    except (OSError, ValueError):
        return False


def runtime_database_path(config: dict) -> Path:
    if os.getenv("TRADING_AGENT_RUNTIME") == "production-paper" and os.getenv("TRADING_AGENT_TESTING") == "1":
        raise RuntimeGuardError("production runtime rejects TRADING_AGENT_TESTING=1")
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        return Path(config["storage"]["sqlite_path"])
    raw = os.getenv("TRADING_AGENT_DATABASE_PATH")
    if not raw:
        raise RuntimeGuardError("explicit database path required; development defaults are forbidden")
    path = Path(raw).resolve()
    if os.getenv("TRADING_AGENT_RUNTIME") == "production-paper":
        if not is_production_path(path):
            raise RuntimeGuardError("production runtime database must be under Application Support")
    elif is_production_path(path):
        raise RuntimeGuardError("development invocation cannot open the production-paper database")
    return path


def validate_production_runtime() -> dict:
    if os.getenv("TRADING_AGENT_TESTING") == "1":
        raise RuntimeGuardError("production runtime rejects TRADING_AGENT_TESTING=1")
    if os.getenv("TRADING_AGENT_RUNTIME") != "production-paper":
        raise RuntimeGuardError("production runtime marker is required")
    try:
        runtime_link_metadata = RUNTIME_LINK.lstat()
    except OSError as exc:
        raise RuntimeGuardError("runtime pointer is unavailable") from exc
    if (
        not stat.S_ISLNK(runtime_link_metadata.st_mode)
        or runtime_link_metadata.st_uid != os.getuid()
    ):
        raise RuntimeGuardError("runtime pointer must be an owner-controlled symlink")
    runtime = RUNTIME_LINK.resolve()
    if not runtime.is_relative_to(RELEASE_ROOT.resolve()):
        raise RuntimeGuardError("runtime path must resolve inside TradingAgentReleases")
    cwd = Path.cwd().resolve()
    if cwd != runtime:
        raise RuntimeGuardError("runtime working directory does not match selected immutable release")
    manifest_path = runtime / "release-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeGuardError("release manifest is unavailable or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeGuardError("release manifest is unavailable or invalid") from exc
    if manifest.get("mode") != "paper":
        raise RuntimeGuardError("release manifest is not paper-only")
    if (
        paper_authority_mode(manifest) is None
        or manifest.get("live_capability") is not False
    ):
        raise RuntimeGuardError("release manifest is not bounded paper-authority and live-disabled")
    if manifest.get("tests_verified") is not True:
        raise RuntimeGuardError("release artifact tests are not verified")
    if (
        manifest.get("python_version") != REQUIRED_PYTHON_VERSION
        or platform.python_version() != REQUIRED_PYTHON_VERSION
    ):
        raise RuntimeGuardError("release Python identity is invalid")
    for field in REQUIRED_ARTIFACT_EVIDENCE_HASHES:
        if not SHA256_RE.fullmatch(str(manifest.get(field) or "").lower()):
            raise RuntimeGuardError(f"release artifact evidence hash is missing or invalid: {field}")
    if not str(manifest.get("release_wheel_filename") or "").strip():
        raise RuntimeGuardError("release wheel filename evidence is missing")
    if not str(manifest.get("distribution_name") or "").strip() or not str(manifest.get("distribution_version") or "").strip():
        raise RuntimeGuardError("release distribution identity evidence is missing")
    if not COMMIT_RE.fullmatch(str(manifest.get("release_commit") or "")):
        raise RuntimeGuardError("release commit identity is invalid")
    if manifest.get("schema_version") != REQUIRED_SCHEMA_VERSION:
        raise RuntimeGuardError("release schema requirement is not explicit")
    if os.getenv("TRADING_AGENT_RELEASE_ID") != manifest.get("release_id"):
        raise RuntimeGuardError("runtime release ID does not match manifest")
    configured_state_root = str(os.getenv("TRADING_AGENT_STATE_ROOT") or "").strip()
    if not configured_state_root:
        raise RuntimeGuardError("external runtime state root is required")
    state_root = Path(configured_state_root).expanduser()
    runtime_state = state_root / "runtime"
    try:
        runtime_state_metadata = runtime_state.lstat()
    except OSError as exc:
        raise RuntimeGuardError("external runtime state directory is unavailable") from exc
    if (
        not state_root.is_absolute()
        or stat.S_ISLNK(runtime_state_metadata.st_mode)
        or not stat.S_ISDIR(runtime_state_metadata.st_mode)
        or runtime_state_metadata.st_uid != os.getuid()
        or stat.S_IMODE(runtime_state_metadata.st_mode) & 0o077
    ):
        raise RuntimeGuardError("external runtime state directory must be owner-only")
    return manifest
