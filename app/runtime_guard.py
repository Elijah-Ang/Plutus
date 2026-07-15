from __future__ import annotations

import json
import os
from pathlib import Path

from .formula_versions import REQUIRED_SCHEMA_VERSIONS


STATE_ROOT = Path.home() / "Library" / "Application Support" / "TradingAgent"
RELEASE_ROOT = Path.home() / "TradingAgentReleases"
RUNTIME_LINK = Path.home() / "TradingAgentRuntime"
REQUIRED_SCHEMA_VERSION = "runtime_safety_accounting_v1"

# Runtime starts only after the migration ledger and the concrete columns used
# by safety gates agree. Checking only one marker row allowed a partial P1
# execution migration to look deployable.
REQUIRED_RUNTIME_TABLE_COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "runs": {"id", "started_at", "status", "mode"},
    "config_snapshots": {"run_id", "config_json", "effective_config_json", "effective_config_hash"},
    "risk_checks": {"run_id", "proposal_id", "name", "passed", "formula_version", "evidence_version", "config_hash"},
    "orders": {"id", "client_order_id", "status", "quote_bid", "quote_ask", "limit_price"},
    "fills": {"order_id", "qty", "price", "implementation_shortfall_bps"},
    "order_intents": {"id", "logical_action_key", "client_order_id", "reserved_notional", "reserved_stop_risk", "state", "trading_mode", "strategy_registry_snapshot_id", "strategy_sleeve", "sleeve_allocation_id", "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling", "incremental_risk", "rotation_step_id", "approval_authority_fingerprint", "displayed_fingerprint", "execution_path", "risk_snapshot_id", "canonical_quantity", "canonical_notional", "canonical_stop_risk", "broker_invocation_occurred"},
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
    "account_equity_watermarks": {"account_key", "peak_equity", "latest_equity", "drawdown_pct", "source", "updated_at"},
    "phase4_strategy_estimates": {"run_id", "strategy_version", "estimated_at", "state", "estimator_version", "evidence_fingerprint", "payload"},
    "phase4_covariance_snapshots": {"run_id", "calculated_at", "strategy_order_json", "covariance_json", "correlation_json", "observation_counts_json", "method", "fallback_used", "payload"},
    "phase4_strategy_states": {"strategy_version", "state", "reason", "estimate_id", "state_version", "evaluated_at", "payload"},
    "phase4_stress_results": {"allocation_id", "scenario", "assumed_loss", "portfolio_loss", "passed", "stress_version", "payload"},
    "phase4_allocation_decisions": {"run_id", "strategy_weights_json", "allocation_class", "operational_kelly_used", "binding_caps_json", "evidence_versions_json", "strategy_policy_map_json", "strategy_policy_version", "probe_allocation_json", "payload"},
    "trade_proposals": {"performance_snapshot_id", "policy_decision_id", "strategy_state", "permitted_stop_risk_pct", "strategy_policy_version", "strategy_registry_snapshot_id", "strategy_sleeve", "sleeve_allocation_id", "sleeve_notional_ceiling", "sleeve_stop_risk_ceiling", "winner_expansion_decision_id", "pyramiding_milestone_id", "pyramiding_milestone_key", "rotation_group_id", "rotation_step_id", "relationship_type"},
    "position_sizing_decisions": {"performance_snapshot_id", "policy_decision_id", "strategy_state", "permitted_stop_risk_pct", "strategy_policy_version"},
    "cash_snapshots": {"equity", "realized_fifo_pnl", "account_equity_change", "unrealized_change", "external_cash_flow", "accounting_version"},
    "position_lots": {"strategy_version", "entry_proposal_id", "entry_intent_id", "entry_regime", "entry_score", "initial_risk_dollars", "config_hash", "evidence_version", "formula_version"},
    "lot_consumptions": {"broker_event_key", "sell_intent_id", "position_lifecycle_id", "lot_id", "allocated_proceeds", "allocated_cost_basis", "allocated_buy_fees", "allocated_sell_fees", "realized_pnl", "accounting_version"},
    "strategy_trade_records": {"source_key", "strategy_version", "evidence_class", "attribution_status", "r_multiple", "evidence_version", "formula_version"},
    "strategy_performance_snapshots": {"strategy_version", "performance_version", "policy_version", "quality_score", "metrics_json", "input_fingerprint"},
    "strategy_policy_decisions": {"strategy_version", "state", "performance_snapshot_id", "enforcement_enabled", "policy_version", "schema_version", "input_fingerprint", "evidence_version", "configuration_version", "config_hash"},
    "strategy_registry_snapshots": {"run_id", "evaluated_at", "authorized_strategies_json", "rejected_strategies_json", "configuration_version", "config_hash", "evaluation_fingerprint"},
    "strategy_registry_decisions": {"snapshot_id", "run_id", "strategy_version", "authorized", "policy_state", "reasons_json", "evidence_version", "configuration_version", "config_hash", "decision_fingerprint"},
    "position_stop_history": {"position_lifecycle_id", "stop_sequence", "prior_stop", "new_stop", "management_mode", "stop_as_of", "formula_version", "decision_fingerprint"},
    "pyramiding_milestones": {"position_lifecycle_id", "milestone_key", "status", "active_proposal_id", "intent_id", "identity_fingerprint", "retry_count", "generation"},
    "add_risk_decisions": {"proposal_id", "approval_id", "decision_stage", "position_lifecycle_id", "eligible", "pre_add_open_risk_net", "post_add_open_risk_net", "incremental_risk", "consumed_risk", "released_risk", "formula_version", "decision_fingerprint"},
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
    runtime = RUNTIME_LINK.resolve()
    if not runtime.is_relative_to(RELEASE_ROOT.resolve()):
        raise RuntimeGuardError("runtime path must resolve inside TradingAgentReleases")
    cwd = Path.cwd().resolve()
    if cwd != runtime:
        raise RuntimeGuardError("runtime working directory does not match selected immutable release")
    manifest_path = runtime / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeGuardError("release manifest is unavailable or invalid") from exc
    if manifest.get("mode") != "paper":
        raise RuntimeGuardError("release manifest is not paper-only")
    if manifest.get("schema_version") != REQUIRED_SCHEMA_VERSION:
        raise RuntimeGuardError("release schema requirement is not explicit")
    if os.getenv("TRADING_AGENT_RELEASE_ID") != manifest.get("release_id"):
        raise RuntimeGuardError("runtime release ID does not match manifest")
    return manifest
