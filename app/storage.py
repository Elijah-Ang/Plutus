from __future__ import annotations

import sqlite3
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .utils import PROJECT_ROOT, iso_now, json_dumps
from .runtime_guard import REQUIRED_SCHEMA_VERSION, is_production_path
from .formula_versions import ACCOUNTING_VERSION, EVIDENCE_VERSION, REQUIRED_SCHEMA_VERSIONS, RISK_DECISION_VERSION


# CPython 3.13 on macOS can deadlock two threads when one closes a WAL
# connection while another opens the same database. Serialise only the native
# open/close lifecycle; transactions and SQL execution remain concurrent and
# continue to rely on SQLite's BEGIN IMMEDIATE/WAL guarantees.
_SQLITE_CONNECTION_LIFECYCLE_LOCK = threading.RLock()

TABLE_DEFINITIONS: dict[str, str] = {
    "runtime_metadata": "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL",
    "schema_migrations": "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, detail TEXT",
    "runs": "id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, mode TEXT NOT NULL, detail TEXT",
    "preflight_checks": "id INTEGER PRIMARY KEY, run_id TEXT, name TEXT, passed INTEGER, reason TEXT, checked_at TEXT",
    "market_snapshots": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, price REAL, price_at TEXT, volume REAL, payload TEXT, created_at TEXT",
    "indicators": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, values_json TEXT, created_at TEXT",
    "signals": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, side TEXT, action TEXT, strategy_version TEXT, reason TEXT, confidence REAL, created_at TEXT, expires_at TEXT, payload TEXT",
    "ml_predictions": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, model_version TEXT, prediction TEXT, probability REAL, payload TEXT, created_at TEXT",
    "risk_checks": "id INTEGER PRIMARY KEY, run_id TEXT, proposal_id TEXT, stage TEXT, name TEXT, passed INTEGER, reason TEXT, checked_at TEXT, formula_version TEXT, evidence_version TEXT, config_hash TEXT",
    "ai_reviews": "id INTEGER PRIMARY KEY, run_id TEXT, proposal_id TEXT, summary TEXT, risks TEXT, caution_level TEXT, payload TEXT, created_at TEXT",
    "trade_proposals": "id TEXT PRIMARY KEY, run_id TEXT, signal_id TEXT, symbol TEXT, side TEXT, notional REAL, status TEXT, created_at TEXT, expires_at TEXT, strategy_version TEXT, payload TEXT, expiry_notified INTEGER DEFAULT 0, telegram_message_id TEXT, proposal_market_rank INTEGER, proposal_eligible_rank INTEGER, selection_reason TEXT, ai_review_status TEXT, ai_confidence TEXT, ai_caution TEXT, true_score_rank INTEGER, watchlist_order INTEGER, setup_key TEXT, cooldown_applied INTEGER, cooldown_remaining_minutes REAL, cooldown_reason TEXT, revival_reason TEXT, last_proposal_status TEXT, score_delta REAL, volatility_regime_change TEXT, exit_priority_applied INTEGER, exit_trigger_reason TEXT, position_drawdown_pct REAL, average_entry_price REAL, latest_position_price REAL, gpt_exit_explanation_status TEXT, gpt_exit_confidence TEXT, gpt_exit_caution TEXT, final_proposal_message_category TEXT, emergency_exit_score REAL, emergency_exit_triggered INTEGER DEFAULT 0, emergency_exit_trigger_reason TEXT, emergency_exit_hard_trigger TEXT, emergency_exit_mode TEXT, emergency_exit_wait_seconds INTEGER, emergency_exit_user_response TEXT, emergency_exit_auto_execute_due_at TEXT, emergency_exit_auto_execute_attempted_at TEXT, emergency_exit_final_decision TEXT, emergency_exit_block_reason TEXT, current_price REAL, atr_value REAL, adverse_move_atr REAL, minutes_to_close REAL, sleep_mode_active INTEGER DEFAULT 0, suppressed_by_sleep_mode INTEGER DEFAULT 0, sleep_mode_reason TEXT, sleep_mode_suppressed_candidate INTEGER DEFAULT 0, sleep_mode_started_at TEXT, sleep_mode_ended_at TEXT",
    "approvals": "id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT, sender_id TEXT, raw_message TEXT, parsed_action TEXT, authorized INTEGER, status TEXT, created_at TEXT, consumed_at TEXT, reply_to_message_id TEXT, proposal_targeting_method TEXT, acknowledgement_status TEXT, approval_received_at TEXT, acknowledgement_sent_at TEXT, acknowledgement_delay_seconds REAL, final_revalidation_started_at TEXT, final_revalidation_completed_at TEXT, price_refreshed_at TEXT, refreshed_price REAL, refreshed_price_age_seconds REAL, price_move_bps_since_proposal REAL, final_order_decision TEXT, final_block_reason TEXT, UNIQUE(proposal_id, status) ON CONFLICT ABORT",
    "orders": "id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT UNIQUE, broker_order_id TEXT, client_order_id TEXT UNIQUE, symbol TEXT, side TEXT, notional REAL, qty REAL, status TEXT, payload TEXT, quote_bid REAL, quote_ask REAL, quote_timestamp TEXT, quote_spread_bps REAL, limit_price REAL, implementation_shortfall_bps REAL, created_at TEXT, updated_at TEXT",
    "fills": "id INTEGER PRIMARY KEY, run_id TEXT, order_id TEXT, qty REAL, price REAL, filled_at TEXT, payload TEXT, implementation_shortfall_bps REAL, fill_notified_at TEXT, fill_notification_status TEXT, fill_notification_error TEXT",
    # Phase 0 execution integrity invariants:
    # - the intent and reservation are committed before any broker submission;
    # - logical_action_key and client_order_id are stable across restarts;
    # - UNKNOWN is non-terminal and retains its reservation until reconciliation proves the outcome.
    "order_intents": "id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT, approval_id TEXT, source_id TEXT NOT NULL, source_type TEXT NOT NULL, logical_action_key TEXT NOT NULL UNIQUE, candidate_id TEXT, position_lifecycle_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL, intended_action TEXT NOT NULL, request_basis TEXT NOT NULL CHECK(request_basis IN ('quantity','notional')), approved_quantity_ceiling REAL, approved_notional_ceiling REAL, requested_quantity REAL NOT NULL, requested_notional REAL, filled_quantity REAL NOT NULL DEFAULT 0 CHECK(filled_quantity>=0), average_fill_price REAL, reference_price REAL NOT NULL CHECK(reference_price>0), intended_stop_price REAL, reserved_notional REAL NOT NULL DEFAULT 0 CHECK(reserved_notional>=0), reserved_stop_risk REAL NOT NULL DEFAULT 0 CHECK(reserved_stop_risk>=0), quote_bid REAL, quote_ask REAL, quote_timestamp TEXT, quote_spread_bps REAL, limit_price REAL, implementation_shortfall_bps REAL, client_order_id TEXT NOT NULL UNIQUE, trading_mode TEXT NOT NULL CHECK(trading_mode='paper'), state TEXT NOT NULL, submission_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(submission_attempt_count>=0), broker_order_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, first_submission_at TEXT, last_reconciliation_at TEXT, terminal_at TEXT, last_error_category TEXT, safe_error_summary TEXT, transition_counter INTEGER NOT NULL DEFAULT 0 CHECK(transition_counter>=0), not_found_count INTEGER NOT NULL DEFAULT 0 CHECK(not_found_count>=0), replacement_enabled INTEGER NOT NULL DEFAULT 0 CHECK(replacement_enabled IN (0,1)), parent_intent_id TEXT, relationship_group_id TEXT, relationship_type TEXT, order_role TEXT NOT NULL DEFAULT 'primary', protection_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(protection_confirmed IN (0,1)), strategy_version TEXT, entry_regime TEXT, entry_score REAL, initial_risk_dollars REAL, config_hash TEXT, evidence_version TEXT, formula_version TEXT, CHECK(filled_quantity<=requested_quantity+0.000000001)",
    "order_events": "id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE, from_state TEXT, to_state TEXT NOT NULL, event_type TEXT NOT NULL, broker_event_id TEXT, filled_quantity REAL, fill_price REAL, safe_detail TEXT, created_at TEXT NOT NULL, transition_counter INTEGER NOT NULL",
    "risk_reservations": "id TEXT PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, cluster_name TEXT, initial_notional REAL NOT NULL CHECK(initial_notional>=0), active_notional REAL NOT NULL CHECK(active_notional>=0), initial_stop_risk REAL NOT NULL CHECK(initial_stop_risk>=0), active_stop_risk REAL NOT NULL CHECK(active_stop_risk>=0), state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, released_at TEXT, release_reason TEXT, version INTEGER NOT NULL DEFAULT 0 CHECK(version>=0)",
    "broker_fill_events": "id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, broker_event_key TEXT NOT NULL UNIQUE, broker_order_id TEXT, cumulative_filled_quantity REAL NOT NULL CHECK(cumulative_filled_quantity>=0), delta_quantity REAL NOT NULL CHECK(delta_quantity>=0), fill_price REAL NOT NULL CHECK(fill_price>=0), occurred_at TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT",
    "reconciliation_attempts": "id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, lookup_type TEXT NOT NULL, lookup_value_redacted TEXT NOT NULL, outcome TEXT NOT NULL, broker_status TEXT, safe_detail TEXT, created_at TEXT NOT NULL, UNIQUE(intent_id, lookup_type, outcome, broker_status, created_at)",
    "telegram_updates": "update_id INTEGER PRIMARY KEY, message_id INTEGER, message_timestamp INTEGER, received_at TEXT NOT NULL, processing_state TEXT NOT NULL, processed_at TEXT, approval_id TEXT, safe_message_type TEXT, normalized_action TEXT, target_hint TEXT, sender_authorized INTEGER, retry_count INTEGER NOT NULL DEFAULT 0, last_error_category TEXT",
    "approval_workflows": "id TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE, proposal_id TEXT NOT NULL, telegram_update_id INTEGER UNIQUE, logical_workflow_key TEXT UNIQUE, state TEXT NOT NULL, intent_id TEXT, validation_status TEXT, safe_detail TEXT, claim_owner TEXT, claim_until TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, last_error_category TEXT, update_processed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, terminal_at TEXT, manual_review_reason TEXT, version INTEGER NOT NULL DEFAULT 0",
    "position_lifecycles": "id TEXT PRIMARY KEY, symbol TEXT NOT NULL, broker_position_id TEXT, side TEXT NOT NULL, state TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, opening_quantity REAL NOT NULL, current_quantity REAL NOT NULL, average_entry_price REAL, source TEXT NOT NULL, management_state_archive TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
    # Prospective FIFO accounting.  Historical basis is never inferred: coverage
    # and confidence are recorded independently in pnl_ledger_status.
    "position_lots": "id TEXT PRIMARY KEY, symbol TEXT NOT NULL, position_lifecycle_id TEXT, source_fill_event_key TEXT NOT NULL UNIQUE, opened_at TEXT NOT NULL, original_quantity REAL NOT NULL CHECK(original_quantity>0), remaining_quantity REAL NOT NULL CHECK(remaining_quantity>=0), unit_cost REAL NOT NULL CHECK(unit_cost>=0), fees_allocated REAL NOT NULL DEFAULT 0 CHECK(fees_allocated>=0), source TEXT NOT NULL, provenance TEXT NOT NULL, confidence TEXT NOT NULL, closed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL",
    "realized_pnl_events": "id TEXT PRIMARY KEY, broker_event_key TEXT NOT NULL UNIQUE, intent_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL CHECK(quantity>0), gross_proceeds REAL, cost_basis REAL, fees REAL NOT NULL DEFAULT 0, adjustments REAL NOT NULL DEFAULT 0, realized_pl REAL, remaining_position_quantity REAL, occurred_at TEXT NOT NULL, trading_day TEXT NOT NULL, trading_week TEXT NOT NULL, accounting_timezone TEXT NOT NULL, source TEXT NOT NULL, provenance TEXT NOT NULL, confidence TEXT NOT NULL, created_at TEXT NOT NULL",
    "pnl_ledger_status": "scope TEXT PRIMARY KEY, effective_from TEXT, confidence TEXT NOT NULL, provenance TEXT NOT NULL, last_event_at TEXT, updated_at TEXT NOT NULL",
    "health_heartbeats": "component TEXT PRIMARY KEY, state TEXT NOT NULL, attempted_at TEXT, completed_at TEXT, successful_at TEXT, blocked_reason TEXT, detail TEXT, commit_sha TEXT, updated_at TEXT NOT NULL",
    "positions": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL, market_value REAL, unrealized_pl REAL, payload TEXT, created_at TEXT",
    "cash_snapshots": "id INTEGER PRIMARY KEY, run_id TEXT, equity REAL, cash REAL, settled_cash REAL, realized_pl REAL, unrealized_pl REAL, realized_fifo_pnl REAL, account_equity_change REAL, unrealized_change REAL, external_cash_flow REAL, accounting_version TEXT, accounting_confidence TEXT, created_at TEXT",
    "cashout_reviews": "id INTEGER PRIMARY KEY, run_id TEXT, payload TEXT, created_at TEXT",
    "cashout_suggestions": "id INTEGER PRIMARY KEY, run_id TEXT, suggested_withdrawal REAL, reserve REAL, reinvest REAL, reason TEXT, created_at TEXT",
    "errors": "id INTEGER PRIMARY KEY, run_id TEXT, category TEXT, message TEXT, detail TEXT, created_at TEXT",
    "audit_events": "id INTEGER PRIMARY KEY, run_id TEXT, event_type TEXT, actor TEXT, detail TEXT, created_at TEXT",
    "strategy_versions": "id TEXT PRIMARY KEY, name TEXT, version TEXT, metadata TEXT, created_at TEXT",
    "shadow_insights": "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sleeve TEXT NOT NULL, strategy_version TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='SHADOW_ONLY'), symbol TEXT NOT NULL, observed_at TEXT NOT NULL, direction TEXT NOT NULL, signal TEXT NOT NULL, score REAL NOT NULL, rank INTEGER, entry_price REAL NOT NULL, regime TEXT NOT NULL, regime_version TEXT NOT NULL, feature_version TEXT NOT NULL, universe_version TEXT NOT NULL, eligibility_version TEXT NOT NULL, outcome_engine_version TEXT NOT NULL, input_fingerprint TEXT NOT NULL, feature_snapshot_json TEXT NOT NULL, universe_snapshot_json TEXT NOT NULL, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL",
    "shadow_portfolio_observations": "id TEXT PRIMARY KEY, insight_id TEXT NOT NULL UNIQUE, sleeve TEXT NOT NULL, strategy_version TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL, target_weight REAL NOT NULL, comparison_portfolio TEXT NOT NULL, status TEXT NOT NULL CHECK(status='SHADOW_ONLY'), created_at TEXT NOT NULL",
    "shadow_overlap_observations": "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL, active_sleeves_json TEXT NOT NULL, active_sleeve_count INTEGER NOT NULL, pair_keys_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,symbol)",
    "shadow_promotion_assessments": "id TEXT PRIMARY KEY, sleeve TEXT NOT NULL, strategy_version TEXT NOT NULL, assessed_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status='NOT_ELIGIBLE'), gate_version TEXT NOT NULL, completed_oos_n INTEGER NOT NULL DEFAULT 0, limitations_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(sleeve,strategy_version)",
    "model_versions": "id TEXT PRIMARY KEY, name TEXT, version TEXT, trained_at TEXT, features TEXT, symbols TEXT, metrics TEXT, path TEXT",
    "config_snapshots": "id INTEGER PRIMARY KEY, run_id TEXT, config_json TEXT, effective_config_json TEXT, effective_config_hash TEXT, configuration_schema_version TEXT, created_at TEXT",
    "daily_summaries": "id INTEGER PRIMARY KEY, date TEXT UNIQUE, mode TEXT, realized_pl REAL, unrealized_pl REAL, equity REAL, payload TEXT, created_at TEXT",
    "market_memory": "id INTEGER PRIMARY KEY, run_id TEXT, market_profile TEXT, symbol TEXT, price REAL, prev_price REAL, price_change REAL, price_change_pct REAL, previous_observation_change_pct REAL, session_start_price REAL, session_change REAL, utc_day_first_observation_change REAL, movement_semantics_version TEXT, volatility REAL, signal TEXT, score REAL, classification TEXT, reason TEXT, proposal_allowed INTEGER, gpt_called INTEGER, created_at TEXT, asset_score REAL, asset_classification TEXT, symbol_rank INTEGER, proposal_generated INTEGER, no_action_reason TEXT, asset_selection_score REAL, trade_decision_score REAL, system_confidence TEXT, gpt_confidence TEXT, gpt_caution TEXT, expiry_minutes INTEGER, expires_at_sgt TEXT, main_risk TEXT, volatility_regime TEXT, volatility_score_contribution REAL, volatility_gate_result TEXT, dedupe_status TEXT, dedupe_reason TEXT, paper_size_adjustment REAL, candidate_suppression_reason TEXT, deferred_ai_review_reason TEXT, true_score_rank INTEGER, watchlist_order INTEGER, setup_key TEXT, cooldown_applied INTEGER, cooldown_remaining_minutes REAL, cooldown_reason TEXT, revival_reason TEXT, last_proposal_status TEXT, score_delta REAL, volatility_regime_change TEXT, exit_priority_applied INTEGER, exit_trigger_reason TEXT, position_drawdown_pct REAL, average_entry_price REAL, latest_position_price REAL, gpt_exit_explanation_status TEXT, gpt_exit_confidence TEXT, gpt_exit_caution TEXT, final_proposal_message_category TEXT, emergency_exit_score REAL, emergency_exit_triggered INTEGER DEFAULT 0, emergency_exit_trigger_reason TEXT, emergency_exit_hard_trigger TEXT, emergency_exit_mode TEXT, emergency_exit_wait_seconds INTEGER, emergency_exit_user_response TEXT, emergency_exit_auto_execute_due_at TEXT, emergency_exit_auto_execute_attempted_at TEXT, emergency_exit_final_decision TEXT, emergency_exit_block_reason TEXT, current_price REAL, atr_value REAL, adverse_move_atr REAL, minutes_to_close REAL, sleep_mode_active INTEGER DEFAULT 0, suppressed_by_sleep_mode INTEGER DEFAULT 0, sleep_mode_reason TEXT, sleep_mode_suppressed_candidate INTEGER DEFAULT 0, sleep_mode_started_at TEXT, sleep_mode_ended_at TEXT",
    "telegram_digests": "id INTEGER PRIMARY KEY, run_id TEXT, window_start TEXT, window_end TEXT, sent_at TEXT, symbols TEXT, summary_text TEXT, status TEXT",
    "control_state": "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, source TEXT, raw_command_redacted TEXT, telegram_update_id INTEGER, telegram_message_id INTEGER, telegram_message_timestamp INTEGER, processed_at TEXT",
    "trade_setups": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, timestamp TEXT, side TEXT, action TEXT, setup_key TEXT, is_active INTEGER, price REAL, score REAL, asset_score REAL, volatility_regime TEXT, trend_state TEXT, gpt_status TEXT, proposal_eligible INTEGER, proposal_sent INTEGER, block_reason TEXT",
    "shadow_trades": "id TEXT PRIMARY KEY, run_id TEXT, setup_id TEXT, symbol TEXT, side TEXT, would_have_entry_price REAL, would_have_entry_time TEXT, would_have_notional REAL, would_have_shares REAL, would_have_stop_price REAL, would_have_stop_distance_pct REAL, reason_not_executed TEXT, score REAL, volatility_regime TEXT, gpt_confidence TEXT, gpt_caution TEXT, setup_key TEXT, portfolio_state_json TEXT, sleep_mode_active INTEGER, cooldown_state TEXT, selected_actual_trade_this_cycle INTEGER",
    "trade_outcomes": "id TEXT PRIMARY KEY, trade_id TEXT, actual_or_shadow TEXT, symbol TEXT, entry_time TEXT, entry_price REAL, outcome_status TEXT, forward_return_1d REAL, forward_return_5d REAL, forward_return_20d REAL, max_favorable_excursion REAL, max_adverse_excursion REAL, stop_hit INTEGER, target_reached INTEGER, add_on_improved INTEGER, beat_shadow_alternatives INTEGER, updated_at TEXT, batch_id TEXT, candidate_id TEXT, proposal_id TEXT, order_id TEXT, broker_order_id TEXT, fill_id TEXT, shadow_trade_id TEXT, risk_budget_decision_id TEXT, position_sizing_decision_id TEXT, approval_id TEXT, approval_batch_action_id TEXT, quantity REAL, notional REAL, score REAL, asset_score REAL, trade_score REAL, setup_reason TEXT, source TEXT",
    "position_sizing_decisions": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, timestamp TEXT, portfolio_equity REAL, risk_budget REAL, stop_distance_dollars REAL, risk_based_shares REAL, score_adjusted_notional REAL, vol_adjusted_notional REAL, final_notional REAL, suggested_shares REAL, base_notional REAL, score_multiplier REAL, volatility_multiplier REAL, stop_model_used TEXT, initial_stop_price REAL, initial_risk_per_share REAL, initial_risk_pct REAL, initial_risk_dollars REAL, stop_source TEXT, entry_price_for_r REAL, risk_model_version TEXT, r_multiple_unavailable_reason TEXT, batch_id TEXT, candidate_id TEXT, proposal_id TEXT, order_id TEXT, broker_order_id TEXT, fill_id TEXT",
    "portfolio_exposure_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, total_exposure_pct REAL, total_exposure_dollars REAL, single_symbol_exposure_json TEXT, cluster_exposure_json TEXT",
    "candidate_rankings": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, symbol TEXT, true_score_rank INTEGER, final_candidate_rank INTEGER, setup_quality_score REAL, portfolio_fit_score REAL, diversification_score REAL, sizing_score REAL, reason_selected TEXT, reason_not_selected TEXT",
    "add_on_opportunities": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, symbol TEXT, current_qty REAL, avg_entry_price REAL, current_price REAL, unrealized_gain_pct REAL, proposed_add_notional REAL, proposed_add_shares REAL, score REAL, score_improvement REAL, passed INTEGER, block_reasons TEXT",
    "performance_lab_summaries": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, total_qualified_setups INTEGER, total_shadow_trades INTEGER, total_actual_trades INTEGER",
    "proposal_batches": "id TEXT PRIMARY KEY, run_id TEXT, telegram_message_id TEXT, status TEXT, created_at TEXT, expires_at TEXT, expiry_notified INTEGER DEFAULT 0, payload TEXT",
    "proposal_batch_candidates": "id TEXT PRIMARY KEY, batch_id TEXT, proposal_id TEXT, telegram_message_id TEXT, candidate_symbol TEXT, candidate_side TEXT, candidate_action TEXT, candidate_status TEXT, rank INTEGER, reason TEXT, created_at TEXT, expires_at TEXT, expiry_notified INTEGER DEFAULT 0, payload TEXT",
    "candidate_risk_budget_decisions": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, candidate_id TEXT, proposal_id TEXT, order_id TEXT, broker_order_id TEXT, fill_id TEXT, symbol TEXT, timestamp TEXT, risk_per_trade_pct REAL, open_risk_after_pct REAL, max_open_risk_pct REAL, total_exposure_after_pct REAL, single_symbol_exposure_after_pct REAL, cluster_exposure_after_pct REAL, buying_power REAL, passed INTEGER, block_reason TEXT, cluster_name TEXT, cluster_held_symbols TEXT, cluster_positions_count_after INTEGER, max_cluster_positions INTEGER, max_cluster_exposure_pct REAL",
    "candidate_batch_allocations": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, candidate_id TEXT, proposal_id TEXT, symbol TEXT, rank INTEGER, raw_suggested_notional REAL, adjusted_suggested_notional REAL, risk_budget_adjusted_notional REAL, final_suggested_notional REAL, final_suggested_shares REAL, cap_reason TEXT, reduction_reason TEXT, created_at TEXT",
    "approval_batch_actions": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, proposal_id TEXT, sender_id TEXT, raw_message TEXT, action TEXT, status TEXT, created_at TEXT, detail TEXT",
    "risk_budget_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, total_exposure_pct REAL, open_risk_pct REAL, daily_realized_loss_pct REAL, max_open_risk_pct REAL, buying_power REAL, payload TEXT",
    "risk_snapshots_v2": "id TEXT PRIMARY KEY, run_id TEXT, calculated_at TEXT NOT NULL, source_at TEXT, source_status TEXT NOT NULL, portfolio_equity REAL, filled_gross_exposure REAL, filled_net_exposure REAL, active_reserved_exposure REAL, projected_gross_exposure REAL, held_open_stop_risk REAL, active_reserved_stop_risk REAL, projected_total_open_risk REAL, daily_realized_pl REAL, daily_realized_loss_pct REAL, weekly_realized_pl REAL, weekly_realized_loss_pct REAL, unresolved_unknown_exposure REAL, buying_power REAL, cash REAL, symbol_exposure_json TEXT, cluster_exposure_json TEXT, raw_inputs TEXT",
    "ranked_opportunity_sets": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, candidate_id TEXT, proposal_id TEXT, timestamp TEXT, symbol TEXT, rank INTEGER, actionable INTEGER, reason TEXT, score REAL, suggested_notional REAL, suggested_shares REAL, payload TEXT",
    "position_management_state": "id TEXT PRIMARY KEY, symbol TEXT UNIQUE, position_lifecycle_id TEXT, broker_position_id TEXT, avg_entry_price REAL, quantity REAL, highest_price_since_entry REAL, highest_price_seen_at TEXT, max_unrealized_profit_pct REAL, max_unrealized_profit_seen_at TEXT, profit_protection_active INTEGER DEFAULT 0, profit_protection_activated_at TEXT, take_profit_level_1_hit INTEGER DEFAULT 0, take_profit_level_2_hit INTEGER DEFAULT 0, take_profit_level_3_hit INTEGER DEFAULT 0, trailing_stop_price REAL, initial_stop_price REAL, initial_risk_per_share REAL, initial_risk_pct REAL, initial_risk_dollars REAL, stop_model TEXT, stop_source TEXT, entry_price_for_r REAL, risk_model_version TEXT, r_multiple_unavailable_reason TEXT, last_decision_type TEXT, last_reason TEXT, updated_at TEXT, created_at TEXT",
    "position_management_decisions": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, position_lifecycle_id TEXT, decision_type TEXT, priority INTEGER, action TEXT, reason TEXT, current_price REAL, avg_entry_price REAL, quantity REAL, unrealized_profit_pct REAL, highest_price_since_entry REAL, max_unrealized_profit_pct REAL, pullback_from_peak_pct REAL, drawdown_from_entry_pct REAL, drawdown_from_peak_pct REAL, profit_giveback_ratio REAL, current_r_multiple REAL, trailing_stop_price REAL, suggested_sell_fraction REAL, suggested_add_notional REAL, blocking_reasons TEXT, is_actionable INTEGER, dip_trap_classification TEXT, position_age_days REAL, position_age_cycles INTEGER, exit_review_needed INTEGER DEFAULT 0, created_at TEXT, payload TEXT",
    "exit_review_events": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, review_type TEXT, status TEXT, reason TEXT, drawdown_from_entry_pct REAL, drawdown_from_peak_pct REAL, unrealized_pl_pct REAL, peak_price_since_entry REAL, peak_unrealized_pct REAL, trailing_stop_price REAL, time_stop_status TEXT, position_age_days REAL, position_age_cycles INTEGER, proposal_id TEXT, created_at TEXT, payload TEXT",
    "profit_exit_events": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, event_type TEXT, proposal_id TEXT, proposal_batch_id TEXT, sell_fraction REAL, estimated_shares REAL, estimated_notional REAL, current_gain_pct REAL, peak_gain_pct REAL, giveback_ratio REAL, r_multiple REAL, trailing_stop_price REAL, status TEXT, created_at TEXT, resolved_at TEXT",
    "universe_symbols": "id TEXT PRIMARY KEY, symbol TEXT UNIQUE, provider_symbol TEXT, exchange TEXT, asset_class TEXT, country TEXT, region TEXT, currency TEXT, sector TEXT, cluster TEXT, tier TEXT, state TEXT, universe_lane TEXT, alpaca_compatible INTEGER DEFAULT 0, exclusion_reason TEXT, executable INTEGER DEFAULT 0, observation_only INTEGER DEFAULT 1, score REAL, reason TEXT, source TEXT, provider TEXT, data_quality TEXT, data_confidence TEXT, data_confidence_reason TEXT, data_freshness_status TEXT, last_successful_research_at TEXT, provider_health_status TEXT, promotion_allowed INTEGER DEFAULT 0, demotion_allowed INTEGER DEFAULT 0, stale_after_minutes INTEGER, promotion_freshness_path TEXT, promotion_confidence_adjustment TEXT, promotion_data_limitations TEXT, proposal_block_reason_after_promotion TEXT, fallback_used TEXT, next_review_time TEXT, alpaca_quote_freshness TEXT, alpaca_tradability_result TEXT, intraday_freshness TEXT, eod_freshness TEXT, last_seen_at TEXT, last_promoted_at TEXT, last_demoted_at TEXT, created_at TEXT, updated_at TEXT",
    "universe_research_runs": "id TEXT PRIMARY KEY, run_id TEXT, research_type TEXT, provider TEXT, status TEXT, started_at TEXT, ended_at TEXT, symbols_considered INTEGER, symbols_promoted INTEGER, symbols_demoted INTEGER, detail TEXT",
    "symbol_research_scores": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, provider TEXT, score REAL, liquidity_score REAL, trend_score REAL, intraday_momentum_score REAL, relative_strength_score REAL, volatility_quality_score REAL, screener_mover_score REAL, news_score REAL, sector_theme_score REAL, data_quality_score REAL, data_confidence TEXT, data_confidence_reason TEXT, universe_lane TEXT, block_reason TEXT, created_at TEXT",
    "symbol_news_events": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, provider TEXT, event_time TEXT, headline TEXT, sentiment TEXT, source TEXT, url TEXT, relevance_score REAL, created_at TEXT",
    "symbol_trend_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, trend_score REAL, relative_strength_score REAL, volatility_quality_score REAL, cluster TEXT, created_at TEXT, payload TEXT",
    "symbol_promotion_decisions": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, from_tier TEXT, to_tier TEXT, score REAL, reason TEXT, deterministic_pass INTEGER, gpt_summary_used INTEGER DEFAULT 0, created_at TEXT, payload TEXT",
    "symbol_demotion_decisions": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, from_tier TEXT, to_tier TEXT, score REAL, reason TEXT, created_at TEXT, payload TEXT",
    "universe_membership_history": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, old_tier TEXT, new_tier TEXT, reason TEXT, source TEXT, created_at TEXT",
    "sector_regime_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, sector TEXT, cluster TEXT, score REAL, reason TEXT, created_at TEXT",
    "dynamic_universe_audit": "id TEXT PRIMARY KEY, run_id TEXT, event_type TEXT, symbol TEXT, detail TEXT, created_at TEXT",
    "data_provider_health": "id TEXT PRIMARY KEY, run_id TEXT, provider TEXT, status TEXT, checked_at TEXT, rate_limit_remaining INTEGER, error TEXT, detail TEXT",
    "data_provider_capabilities": "id TEXT PRIMARY KEY, run_id TEXT, provider TEXT, endpoint_name TEXT UNIQUE, available INTEGER DEFAULT 0, plan_limited INTEGER DEFAULT 0, last_success_at TEXT, last_failure_at TEXT, failure_count INTEGER DEFAULT 0, last_status_code INTEGER, last_error_category TEXT, disabled_until TEXT, retry_after TEXT, used_for_scoring INTEGER DEFAULT 0, updated_at TEXT, detail TEXT",
    "data_provider_cache_index": "id TEXT PRIMARY KEY, provider TEXT, endpoint TEXT, cache_key TEXT UNIQUE, symbol TEXT, fetched_at TEXT, expires_at TEXT, status TEXT, payload TEXT",
    "research_candidate_block_reasons": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, score REAL, data_confidence TEXT, block_reason TEXT, liquidity_score REAL, trend_score REAL, intraday_momentum_score REAL, relative_strength_score REAL, volatility_quality_score REAL, screener_mover_score REAL, news_score REAL, sector_theme_score REAL, data_quality_score REAL, universe_lane TEXT, exclusion_reason TEXT, created_at TEXT, payload TEXT",
    "dynamic_universe_stage_semantics": "tier TEXT PRIMARY KEY, stage_order INTEGER, meaning TEXT, data_checked TEXT, research_completed TEXT, data_may_be_missing TEXT, allowed_actions TEXT, blocked_actions TEXT, promotes_to_next_tier TEXT, blocks_from_next_tier TEXT, telegram_trade_proposals_allowed INTEGER DEFAULT 0, orders_possible INTEGER DEFAULT 0, llm_explanations_allowed INTEGER DEFAULT 0, llm_can_affect_decisions INTEGER DEFAULT 0, updated_at TEXT",
    "research_candidate_briefs": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, company_name TEXT, current_tier TEXT, universe_lane TEXT, research_score REAL, rank INTEGER, data_confidence TEXT, latest_price REAL, price_freshness TEXT, liquidity_metrics TEXT, dollar_volume REAL, trend_summary TEXT, intraday_summary TEXT, relative_strength_vs_spy TEXT, sector TEXT, industry TEXT, sector_relative_context TEXT, volatility_risk_summary TEXT, screener_reason TEXT, main_positive_reasons TEXT, main_blockers TEXT, missing_neutral_data TEXT, endpoint_coverage TEXT, before_observation_requirements TEXT, before_paper_tradable_requirements TEXT, allowed_actions TEXT, blocked_actions TEXT, proposal_order_confirmation TEXT, last_pre_market_scan_at TEXT, last_candidate_brief_at TEXT, last_intraday_refresh_at TEXT, last_observation_check_at TEXT, next_expected_check TEXT, current_stage TEXT, next_stage_requirements TEXT, explanation_source TEXT DEFAULT 'deterministic', created_at TEXT, payload TEXT",
    "llm_explanation_cache": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, input_hash TEXT, explanation_json TEXT, status TEXT, error TEXT, call_count INTEGER DEFAULT 0, estimated_cost REAL DEFAULT 0, created_at TEXT, updated_at TEXT",
    "llm_explanation_usage": "id TEXT PRIMARY KEY, run_id TEXT, enabled INTEGER DEFAULT 0, attempted_calls INTEGER DEFAULT 0, successful_calls INTEGER DEFAULT 0, failed_calls INTEGER DEFAULT 0, discarded_invalid INTEGER DEFAULT 0, conflicts_ignored INTEGER DEFAULT 0, total_estimated_cost REAL DEFAULT 0, status TEXT, created_at TEXT, detail TEXT",
    "dynamic_universe_stage_reviews": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, current_tier TEXT, review_type TEXT, decision TEXT, reason TEXT, score REAL, data_confidence TEXT, observation_since TEXT, observation_cycles INTEGER, market_open_refreshes INTEGER, latest_price REAL, price_freshness TEXT, eod_available INTEGER DEFAULT 0, intraday_available INTEGER DEFAULT 0, trend_summary TEXT, intraday_summary TEXT, liquidity_summary TEXT, volatility_summary TEXT, relative_strength_spy TEXT, relative_strength_qqq TEXT, cluster TEXT, cluster_exposure_blocker TEXT, promotion_requirements_met TEXT, promotion_requirements_missing TEXT, demotion_risk_reasons TEXT, demotion_guard_active INTEGER DEFAULT 0, current_stage_reason TEXT, next_stage_blocker TEXT, tradable_status TEXT, proposal_allowed_status TEXT, proposal_block_reason TEXT, last_promotion_review_at TEXT, last_demotion_review_at TEXT, next_promotion_review_at TEXT, next_demotion_review_at TEXT, created_at TEXT, payload TEXT, promotion_freshness_path TEXT, promotion_confidence_adjustment TEXT, promotion_data_limitations TEXT, proposal_block_reason_after_promotion TEXT, fallback_used TEXT, next_review_time TEXT, alpaca_quote_freshness TEXT, alpaca_tradability_result TEXT, intraday_freshness TEXT, eod_freshness TEXT",
    "dynamic_universe_performance": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, tier TEXT, metric TEXT, value REAL, created_at TEXT, payload TEXT",
    "performance_setups": "id TEXT PRIMARY KEY, timestamp TEXT, run_id TEXT, symbol TEXT, asset_class TEXT, tier TEXT, setup_type TEXT, action_decision TEXT, proposed INTEGER DEFAULT 0, proposal_id TEXT, batch_id TEXT, not_proposed_reason TEXT, score REAL, score_components TEXT, signal_state TEXT, entry_signal INTEGER DEFAULT 0, exit_signal INTEGER DEFAULT 0, add_signal INTEGER DEFAULT 0, current_price REAL, price_timestamp TEXT, data_freshness TEXT, trend_metrics TEXT, volatility_metrics TEXT, liquidity_metrics TEXT, relative_strength_metrics TEXT, portfolio_exposure TEXT, cluster_exposure TEXT, risk_budget TEXT, proposed_notional REAL, hypothetical_notional REAL, actual_approved_notional REAL, final_submitted_notional REAL, order_id TEXT, broker_order_id TEXT, fill_id TEXT, order_status TEXT, fill_price REAL, fill_qty REAL, created_at TEXT, updated_at TEXT",
    "performance_blockers": "id TEXT PRIMARY KEY, setup_id TEXT, run_id TEXT, symbol TEXT, blocker TEXT, reason TEXT, severity TEXT, created_at TEXT",
    "performance_outcomes": "id TEXT PRIMARY KEY, setup_id TEXT UNIQUE, run_id TEXT, symbol TEXT, proposal_id TEXT, batch_id TEXT, order_id TEXT, broker_order_id TEXT, fill_id TEXT, actual_or_shadow TEXT, entry_time TEXT, entry_price REAL, entry_notional REAL, entry_qty REAL, status TEXT, actual_proposal_execution_helped INTEGER, add_to_winner_improved_position INTEGER, exit_signal_avoided_loss INTEGER, created_at TEXT, updated_at TEXT",
    "performance_forward_returns": "id TEXT PRIMARY KEY, setup_id TEXT, run_id TEXT, symbol TEXT, horizon_days INTEGER, due_at TEXT, eligible_to_update INTEGER DEFAULT 0, updated_at TEXT, forward_return REAL, max_favorable_excursion REAL, max_adverse_excursion REAL, hypothetical_stop_hit INTEGER, hypothetical_target_hit INTEGER, status TEXT, reason TEXT",
    "performance_counterfactuals": "id TEXT PRIMARY KEY, setup_id TEXT, run_id TEXT, symbol TEXT, counterfactual_type TEXT, would_enter INTEGER DEFAULT 0, would_add INTEGER DEFAULT 0, would_exit INTEGER DEFAULT 0, hypothetical_entry_price REAL, hypothetical_notional REAL, reason TEXT, comparison_status TEXT, created_at TEXT, updated_at TEXT",
    "crypto_research_runs": "id TEXT PRIMARY KEY, run_id TEXT, status TEXT, started_at TEXT, ended_at TEXT, symbols TEXT, provider TEXT, error TEXT, payload TEXT",
    "crypto_research_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, research_run_id TEXT, symbol TEXT, lane TEXT, price REAL, price_timestamp TEXT, data_freshness TEXT, return_1h REAL, return_4h REAL, return_1d REAL, return_7d REAL, return_20d REAL, realized_volatility REAL, atr_like_volatility REAL, trend_metrics TEXT, volume REAL, spread REAL, score REAL, score_components TEXT, risk_metrics TEXT, provider TEXT, created_at TEXT, payload TEXT",
    "crypto_observation_state": "symbol TEXT PRIMARY KEY, lane TEXT, score REAL, status TEXT, last_price REAL, last_price_timestamp TEXT, data_freshness TEXT, last_research_at TEXT, observation_since TEXT, updated_at TEXT, payload TEXT",
    "crypto_counterfactual_outcomes": "id TEXT PRIMARY KEY, run_id TEXT, research_run_id TEXT, setup_id TEXT, symbol TEXT, score REAL, would_propose INTEGER DEFAULT 0, reason TEXT, forward_return_1d REAL, forward_return_7d REAL, status TEXT, created_at TEXT, updated_at TEXT",
    "crypto_paper_watch_candidates": "id TEXT PRIMARY KEY, run_id TEXT, research_run_id TEXT, setup_id TEXT, proposal_id TEXT, symbol TEXT, mode TEXT, status TEXT, score REAL, entry_price REAL, stop_price REAL, take_profit_price REAL, risk_reward_ratio REAL, spread_bps REAL, volatility_regime TEXT, position_notional REAL, max_loss_estimate REAL, blockers TEXT, candidate_metadata TEXT, created_at TEXT, updated_at TEXT",
    "dynamic_universe_schedule_state": "id TEXT PRIMARY KEY, schedule_name TEXT UNIQUE, schedule_type TEXT, due_at TEXT, last_started_at TEXT, last_completed_at TEXT, last_success_at TEXT, last_skipped_at TEXT, last_skip_reason TEXT, missed_count INTEGER DEFAULT 0, catchup_required INTEGER DEFAULT 0, catchup_attempted_at TEXT, catchup_completed_at TEXT, catchup_status TEXT, data_freshness_status TEXT, provider_health_status TEXT, internet_status TEXT, power_status TEXT, battery_pct REAL, stale_after_minutes INTEGER, promotion_allowed INTEGER DEFAULT 0, demotion_allowed INTEGER DEFAULT 0, notes TEXT, created_at TEXT, updated_at TEXT",
}

P1_EXECUTION_SCHEMA_VERSION = "p1_execution_safety_v1"
RUNTIME_SAFETY_SCHEMA_VERSION = "runtime_safety_accounting_v1"


RUNTIME_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "approvals": {
        "proposal_reference_price": "REAL", "refreshed_bid": "REAL", "refreshed_ask": "REAL",
        "directional_price_move_bps": "REAL", "movement_classification": "TEXT",
        "final_limit_price": "REAL", "directional_validation_reason": "TEXT",
    },
    "risk_checks": {
        "formula_version": "TEXT", "evidence_version": "TEXT", "config_hash": "TEXT",
    },
    "config_snapshots": {
        "effective_config_json": "TEXT", "effective_config_hash": "TEXT", "configuration_schema_version": "TEXT",
    },
    "cash_snapshots": {
        "realized_fifo_pnl": "REAL", "account_equity_change": "REAL", "unrealized_change": "REAL",
        "external_cash_flow": "REAL", "accounting_version": "TEXT", "accounting_confidence": "TEXT",
    },
    "position_sizing_decisions": {
        "stop_policy_version": "TEXT", "sizing_policy_version": "TEXT", "formula_version": "TEXT",
        "sizing_caps_json": "TEXT", "binding_caps_json": "TEXT", "evidence_version": "TEXT", "config_hash": "TEXT",
    },
    "position_lifecycles": {
        "opening_quantity_frozen": "INTEGER NOT NULL DEFAULT 0",
    },
    "trade_proposals": {
        "sizing_caps_json": "TEXT", "formula_versions_json": "TEXT", "evidence_version": "TEXT",
        "strategy_registry_snapshot_id": "TEXT", "strategy_sleeve": "TEXT", "sleeve_allocation_id": "TEXT",
        "sleeve_notional_ceiling": "REAL", "sleeve_stop_risk_ceiling": "REAL",
        "winner_expansion_decision_id": "TEXT", "pyramiding_milestone_id": "TEXT",
        "pyramiding_milestone_key": "TEXT", "management_mode": "TEXT",
        "current_shares": "REAL", "add_shares": "REAL", "current_protective_stop": "REAL",
        "proposed_protective_stop": "REAL", "pre_add_open_risk": "REAL",
        "post_add_open_risk": "REAL", "incremental_risk": "REAL", "risk_released": "REAL",
        "risk_operator_classification": "TEXT", "rotation_group_id": "TEXT",
        "rotation_step_id": "TEXT", "relationship_type": "TEXT",
    },
    "order_intents": {
        "strategy_registry_snapshot_id": "TEXT", "strategy_sleeve": "TEXT", "sleeve_allocation_id": "TEXT",
        "sleeve_notional_ceiling": "REAL", "sleeve_stop_risk_ceiling": "REAL",
        "winner_expansion_decision_id": "TEXT", "pyramiding_milestone_id": "TEXT",
        "pyramiding_milestone_key": "TEXT", "management_mode": "TEXT",
        "pre_add_open_risk": "REAL", "post_add_open_risk": "REAL", "incremental_risk": "REAL",
        "rotation_step_id": "TEXT",
    },
    "risk_reservations": {
        "strategy_version": "TEXT", "strategy_sleeve": "TEXT", "sleeve_allocation_id": "TEXT",
        "sleeve_notional_ceiling": "REAL", "sleeve_stop_risk_ceiling": "REAL",
        "incremental_risk": "REAL",
        "risk_value": "REAL", "risk_unit": "TEXT", "conversion_equity": "REAL",
        "conversion_equity_as_of": "TEXT", "risk_formula_version": "TEXT",
    },
    "position_management_state": {
        "authoritative_protective_stop": "REAL", "protective_stop_as_of": "TEXT",
        "protective_stop_source": "TEXT", "protective_stop_formula_version": "TEXT",
        "protective_stop_sequence": "INTEGER", "management_mode": "TEXT",
        "trend_management_formula_version": "TEXT", "peak_r_multiple": "REAL",
        "last_completed_pyramiding_milestone": "TEXT",
        "entry_fill_id": "TEXT", "entry_order_intent_id": "TEXT",
        "initial_risk_reconstruction_source": "TEXT",
        "initial_risk_formula_version": "TEXT", "initial_risk_evidence_version": "TEXT",
    },
    "strategy_policy_decisions": {
        "evidence_version": "TEXT", "configuration_version": "TEXT", "config_hash": "TEXT",
    },
    "telegram_updates": {
        "message_timestamp": "INTEGER",
    },
}


def _ensure_columns(conn: sqlite3.Connection, additions: dict[str, dict[str, str]]) -> None:
    for table, columns in additions.items():
        present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not present:
            # Some additive tables are created by their versioned migration
            # later in initialize/apply_explicit_migrations. Never fabricate a
            # partial table merely to add provenance columns.
            continue
        for name, column_type in columns.items():
            if name not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {column_type}')


def apply_p1_execution_schema(conn: sqlite3.Connection, *, record_migration: bool = True) -> None:
    """Add quote/limit/fill-shortfall fields without rewriting order history."""
    additions = {
        "orders": {
            "quote_bid": "REAL", "quote_ask": "REAL", "quote_timestamp": "TEXT",
            "quote_spread_bps": "REAL", "limit_price": "REAL", "implementation_shortfall_bps": "REAL",
        },
        "fills": {"implementation_shortfall_bps": "REAL"},
        "order_intents": {
            "quote_bid": "REAL", "quote_ask": "REAL", "quote_timestamp": "TEXT",
            "quote_spread_bps": "REAL", "limit_price": "REAL", "implementation_shortfall_bps": "REAL",
        },
    }
    for table, columns in additions.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, column_type in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (P1_EXECUTION_SCHEMA_VERSION, iso_now(), "quote validation, bounded limit orders, and implementation-shortfall persistence"),
        )


class Storage:
    def __init__(self, path: str | Path = PROJECT_ROOT / "data" / "trading_agent.db") -> None:
        self.path = Path(path)
        if os.getenv("TRADING_AGENT_TESTING") == "1" and is_production_path(self.path):
            raise RuntimeError("tests must never open a production-paper database")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def schema_versions(self) -> set[str]:
        try:
            return {row["version"] for row in self.fetch_all("SELECT version FROM schema_migrations")}
        except sqlite3.Error:
            return set()

    def require_runtime_schema(self, *, production: bool = False) -> None:
        versions = self.schema_versions()
        missing_versions = sorted(REQUIRED_SCHEMA_VERSIONS - versions)
        if missing_versions or REQUIRED_SCHEMA_VERSION not in versions:
            missing = ", ".join(missing_versions or [REQUIRED_SCHEMA_VERSION])
            raise RuntimeError(f"Database migration required before runtime start: {missing}")
        from .runtime_guard import REQUIRED_RUNTIME_TABLE_COLUMNS
        missing_columns: list[str] = []
        try:
            with self.connect() as conn:
                for table, required_columns in REQUIRED_RUNTIME_TABLE_COLUMNS.items():
                    present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                    if not present:
                        missing_columns.append(f"{table} (table)")
                    else:
                        missing_columns.extend(f"{table}.{column}" for column in sorted(required_columns - present))
        except sqlite3.Error as exc:
            raise RuntimeError("Database schema inspection failed before runtime start") from exc
        if missing_columns:
            raise RuntimeError("Database schema incomplete before runtime start: " + ", ".join(missing_columns))
        if production:
            rows = self.fetch_all("SELECT value FROM runtime_metadata WHERE key='environment'")
            if not rows or rows[0]["value"] != "production-paper":
                raise RuntimeError("production-paper database sentinel is missing")

    def apply_explicit_migrations(self, *, production_paper: bool = False) -> None:
        """Deployment-only schema mutation. Ordinary runtime must not call this."""
        self.initialize()
        with self.connect() as conn:
            from .research_validation import apply_phase1_schema
            from .shadow_strategies import apply_phase2_schema
            from .phase3_risk import apply_phase3_schema
            from .phase4_allocator import apply_phase4_schema
            from .adaptive_conviction import apply_adaptive_conviction_schema
            from .adaptive_sizing import apply_adaptive_sizing_schema
            from .strategy_execution_registry import apply_strategy_registry_schema
            from .winner_expansion import apply_winner_expansion_schema
            from .rotation_coordinator import apply_rotation_schema
            from .profit_milestones import apply_profit_milestone_schema
            apply_p1_execution_schema(conn)

            apply_phase1_schema(conn)
            apply_phase2_schema(conn)
            apply_phase3_schema(conn)
            apply_phase4_schema(conn)
            apply_adaptive_conviction_schema(conn)
            apply_adaptive_sizing_schema(conn)
            apply_strategy_registry_schema(conn)
            apply_winner_expansion_schema(conn)
            apply_rotation_schema(conn)
            apply_profit_milestone_schema(conn)
            from .strategy_performance import apply_strategy_performance_schema
            apply_strategy_performance_schema(conn)
            _ensure_columns(conn, RUNTIME_ADDITIVE_COLUMNS)
            now = iso_now()
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
                (RUNTIME_SAFETY_SCHEMA_VERSION, now, "effective config, formula versions, and explicit accounting columns"),
            )
            if production_paper:
                existing = conn.execute("SELECT value FROM runtime_metadata WHERE key='environment'").fetchone()
                if existing and existing["value"] != "production-paper":
                    raise RuntimeError("refusing to overwrite a non-production database sentinel")
                conn.execute(
                    "INSERT INTO runtime_metadata(key,value,updated_at) VALUES('environment','production-paper',?) "
                    "ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at",
                    (now,),
                )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
                (REQUIRED_SCHEMA_VERSION, now, "explicit deployment-only runtime isolation migration"),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with _SQLITE_CONNECTION_LIFECYCLE_LOCK:
            conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            with _SQLITE_CONNECTION_LIFECYCLE_LOCK:
                conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            for table, columns in TABLE_DEFINITIONS.items():
                conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
            _ensure_columns(conn, RUNTIME_ADDITIVE_COLUMNS)
            if not is_production_path(self.path):
                from .phase3_risk import apply_phase3_schema
                apply_phase3_schema(conn, record_migration=False)
                from .phase4_allocator import apply_phase4_schema
                apply_phase4_schema(conn, record_migration=False)
                from .adaptive_conviction import apply_adaptive_conviction_schema
                apply_adaptive_conviction_schema(conn, record_migration=False)
                from .adaptive_sizing import apply_adaptive_sizing_schema
                apply_adaptive_sizing_schema(conn, record_migration=False)
                from .strategy_execution_registry import apply_strategy_registry_schema
                apply_strategy_registry_schema(conn)
                from .winner_expansion import apply_winner_expansion_schema
                apply_winner_expansion_schema(conn, record_migration=False)
                from .rotation_coordinator import apply_rotation_schema
                apply_rotation_schema(conn, record_migration=False)
                from .profit_milestones import apply_profit_milestone_schema
                apply_profit_milestone_schema(conn, record_migration=False)
                apply_p1_execution_schema(conn, record_migration=False)
                from .strategy_performance import apply_strategy_performance_schema
                apply_strategy_performance_schema(conn, record_migration=False)
                _ensure_columns(conn, RUNTIME_ADDITIVE_COLUMNS)
            # Establish a prospective accounting boundary once.  Coverage before
            # this instant remains unavailable; repeated startup never advances it.
            now = iso_now()
            conn.execute(
                """INSERT OR IGNORE INTO pnl_ledger_status(
                       scope,effective_from,confidence,provenance,updated_at)
                   VALUES('prospective',?,'verified',
                          'prospective Phase 0 FIFO ledger; pre-boundary history unavailable',?)""",
                (now, now),
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_status ON trade_proposals(status, expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_proposal ON risk_checks(proposal_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_intents_reconcile ON order_intents(state, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_intents_symbol_active ON order_intents(symbol, side, state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_events_intent ON order_events(intent_id, transition_counter)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reservations_active ON risk_reservations(state, symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reconciliation_intent ON reconciliation_attempts(intent_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approval_workflows_state ON approval_workflows(state, updated_at)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_position_lifecycle_one_active ON position_lifecycles(symbol) WHERE state='active'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_position_lots_fifo ON position_lots(symbol,opened_at,id) WHERE remaining_quantity>0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_realized_pnl_day ON realized_pnl_events(trading_day,confidence)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_realized_pnl_week ON realized_pnl_events(trading_week,confidence)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_universe_tier ON universe_symbols(tier, executable, observation_only)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_research_scores_symbol ON symbol_research_scores(symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_research_candidate_briefs_symbol ON research_candidate_briefs(symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dynamic_stage_reviews_symbol ON dynamic_universe_stage_reviews(symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dynamic_schedule_state ON dynamic_universe_schedule_state(schedule_type, catchup_required, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_performance_setups_run_symbol ON performance_setups(run_id, symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_performance_setups_proposal ON performance_setups(proposal_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_performance_blockers_setup ON performance_blockers(setup_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_performance_forward_due ON performance_forward_returns(status, due_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_snapshots_symbol_created ON crypto_research_snapshots(symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_snapshots_run ON crypto_research_snapshots(research_run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_counterfactuals_symbol ON crypto_counterfactual_outcomes(symbol, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_watch_candidates_symbol ON crypto_paper_watch_candidates(symbol, created_at)")
            stage_rows = [
                (
                    "raw_universe",
                    1,
                    "Broad discovery pool. Not tradable and not eligible for proposals.",
                    "Provider or configured source metadata may be present; price/liquidity may be unchecked or insufficient.",
                    "Discovery and symbol intake classification only.",
                    "Price, liquidity, trend, intraday, news, fundamentals, and compatibility data may be missing.",
                    "discover, classify, exclude, score if data is available",
                    "trade proposals, orders, Alpaca scanner execution, LLM decisioning",
                    "passes intake gates, minimum usable price/liquidity data, confidence gate, and research score threshold",
                    "unsupported symbol, excluded lane, missing critical data, low liquidity, low score, stale provider data",
                    0,
                    0,
                    1,
                    0,
                ),
                (
                    "research_candidate",
                    2,
                    "Passed initial pre-market universe scan, filtering, and quantitative scoring. Worth tracking and explaining, but not tradable.",
                    "Symbol lane, compatibility, available EODHD data, local price/liquidity/trend/momentum/relative strength components, confidence, and block reasons.",
                    "Pre-market universe scan plus deterministic quantitative scoring and candidate brief generation.",
                    "Full analyst thesis, fundamentals, fresh news, sector-relative depth, or multi-timeframe chart narrative may be unavailable.",
                    "research, track, report, generate deterministic or explanation-only LLM narrative",
                    "trade proposals, orders, manual promotion, RiskEngine bypass",
                    "subsequent market-open refresh satisfies observation threshold, component checks, confidence, freshness, and clean intake state",
                    "insufficient observation confirmation, stale data, weak score/components, liquidity/trend/relative-strength failure, cluster/intake issues",
                    0,
                    0,
                    1,
                    0,
                ),
                (
                    "observation",
                    3,
                    "Shadow-tracking tier during market hours. Still not tradable and no proposals from this tier.",
                    "Research candidate evidence plus market-open refresh and observation checks.",
                    "Intraday light refresh and observation promotion checks have justified shadow tracking.",
                    "Enough observation cycles, shadow trade evidence, fresh provider data, or clean cluster mapping may still be missing.",
                    "shadow-track, refresh, update scores, monitor promotion requirements",
                    "trade proposals, orders, final trade eligibility, LLM decisioning",
                    "recorded deterministic promotion to dynamic paper-tradable after score, cycles, sessions, shadow tracking, lane, cluster, and confidence requirements pass",
                    "not enough cycles/sessions, no shadow tracking, weak score, stale data, unknown cluster, non-US lane, risk constraints",
                    0,
                    0,
                    1,
                    0,
                ),
                (
                    "paper_tradable",
                    4,
                    "Eligible to be considered for paper trade proposals only after recorded deterministic promotion and normal safety gates.",
                    "Observation history, shadow tracking, score, confidence, lane, cluster, and freshness requirements.",
                    "Deterministic promotion to dynamic paper-tradable recorded.",
                    "Current market data, RiskEngine context, proposal limits, open orders, exposure, and final validation may still block action.",
                    "enter scanner candidate pool, run proposal/risk evaluation",
                    "orders without Telegram approval and final validation; LLM decisioning",
                    "proposal engine selects it and all market-open, risk, cluster, exposure, and proposal rules pass",
                    "market closed, RiskEngine failure, cluster/exposure limit, open order, stale data, proposal limits, weak live setup",
                    1,
                    0,
                    1,
                    0,
                ),
                (
                    "trade_proposal",
                    5,
                    "Paper trade idea sent for Telegram approval. Still not an order.",
                    "Proposal scoring, current market context, RiskEngine checks, and Telegram payload.",
                    "A deterministic proposal was created and sent for approval.",
                    "User approval and final validation remain missing.",
                    "wait for authorized Telegram approval, expire, reject, supersede",
                    "orders before approval and final validation; LLM approval or sizing",
                    "authorized user approval and final revalidation pass",
                    "rejection, expiry, unauthorized response, final validation failure, RiskEngine failure",
                    1,
                    0,
                    1,
                    0,
                ),
                (
                    "approved_order",
                    6,
                    "User approved proposal; final validation is still required before broker submission.",
                    "Telegram approval identity, proposal state, current price/order context, and final validation inputs.",
                    "User approval recorded and consumed.",
                    "Broker acceptance/fill and reconciliation are still missing.",
                    "final validation, broker paper submission if still valid",
                    "live trading, auto-execution, bypassing final validation",
                    "paper broker accepts final validated order submission",
                    "stale approval, price movement, market closed, duplicate/open order, RiskEngine or broker validation failure",
                    0,
                    0,
                    1,
                    0,
                ),
                (
                    "filled_order",
                    7,
                    "Broker paper fill recorded and reconciled.",
                    "Broker order/fill records and reconciliation snapshot.",
                    "Paper fill captured and stored.",
                    "Forward outcome and later position-management results may still be pending.",
                    "reconcile, monitor position, report outcomes",
                    "retroactive LLM decision changes, live trade assumption",
                    "position monitoring and outcome measurement",
                    "broker reconciliation mismatch or missing fill data",
                    0,
                    0,
                    1,
                    0,
                ),
            ]
            conn.executemany(
                """
                INSERT INTO dynamic_universe_stage_semantics(
                    tier,stage_order,meaning,data_checked,research_completed,data_may_be_missing,allowed_actions,
                    blocked_actions,promotes_to_next_tier,blocks_from_next_tier,telegram_trade_proposals_allowed,
                    orders_possible,llm_explanations_allowed,llm_can_affect_decisions,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(tier) DO UPDATE SET
                    stage_order=excluded.stage_order,
                    meaning=excluded.meaning,
                    data_checked=excluded.data_checked,
                    research_completed=excluded.research_completed,
                    data_may_be_missing=excluded.data_may_be_missing,
                    allowed_actions=excluded.allowed_actions,
                    blocked_actions=excluded.blocked_actions,
                    promotes_to_next_tier=excluded.promotes_to_next_tier,
                    blocks_from_next_tier=excluded.blocks_from_next_tier,
                    telegram_trade_proposals_allowed=excluded.telegram_trade_proposals_allowed,
                    orders_possible=excluded.orders_possible,
                    llm_explanations_allowed=excluded.llm_explanations_allowed,
                    llm_can_affect_decisions=excluded.llm_can_affect_decisions,
                    updated_at=excluded.updated_at
                """,
                stage_rows,
            )
            
            # Migration check for existing databases
            cursor = conn.execute("PRAGMA table_info(trade_proposals)")
            cols = [row["name"] for row in cursor.fetchall()]
            if "expiry_notified" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN expiry_notified INTEGER DEFAULT 0")
            if "telegram_message_id" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN telegram_message_id TEXT")
            if "proposal_market_rank" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN proposal_market_rank INTEGER")
            if "proposal_eligible_rank" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN proposal_eligible_rank INTEGER")
            if "selection_reason" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN selection_reason TEXT")
            if "ai_review_status" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN ai_review_status TEXT")
            if "ai_confidence" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN ai_confidence TEXT")
            if "ai_caution" not in cols:
                conn.execute("ALTER TABLE trade_proposals ADD COLUMN ai_caution TEXT")
                
            new_cols = {
                "true_score_rank": "INTEGER",
                "watchlist_order": "INTEGER",
                "setup_key": "TEXT",
                "cooldown_applied": "INTEGER",
                "cooldown_remaining_minutes": "REAL",
                "cooldown_reason": "TEXT",
                "revival_reason": "TEXT",
                "last_proposal_status": "TEXT",
                "score_delta": "REAL",
                "volatility_regime_change": "TEXT",
                "exit_priority_applied": "INTEGER",
                "exit_trigger_reason": "TEXT",
                "position_drawdown_pct": "REAL",
                "average_entry_price": "REAL",
                "latest_position_price": "REAL",
                "gpt_exit_explanation_status": "TEXT",
                "gpt_exit_confidence": "TEXT",
                "gpt_exit_caution": "TEXT",
                "final_proposal_message_category": "TEXT",
                "emergency_exit_score": "REAL",
                "emergency_exit_triggered": "INTEGER DEFAULT 0",
                "emergency_exit_trigger_reason": "TEXT",
                "emergency_exit_hard_trigger": "TEXT",
                "emergency_exit_mode": "TEXT",
                "emergency_exit_wait_seconds": "INTEGER",
                "emergency_exit_user_response": "TEXT",
                "emergency_exit_auto_execute_due_at": "TEXT",
                "emergency_exit_auto_execute_attempted_at": "TEXT",
                "emergency_exit_final_decision": "TEXT",
                "emergency_exit_block_reason": "TEXT",
                "current_price": "REAL",
                "atr_value": "REAL",
                "adverse_move_atr": "REAL",
                "minutes_to_close": "REAL",
                "sleep_mode_active": "INTEGER DEFAULT 0",
                "suppressed_by_sleep_mode": "INTEGER DEFAULT 0",
                "sleep_mode_reason": "TEXT",
                "sleep_mode_suppressed_candidate": "INTEGER DEFAULT 0",
                "sleep_mode_started_at": "TEXT",
                "sleep_mode_ended_at": "TEXT"
            }
            for col_name, col_type in new_cols.items():
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE trade_proposals ADD COLUMN {col_name} {col_type}")
                
            cursor = conn.execute("PRAGMA table_info(approvals)")
            cols = [row["name"] for row in cursor.fetchall()]
            if "reply_to_message_id" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN reply_to_message_id TEXT")
            if "proposal_targeting_method" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN proposal_targeting_method TEXT")
            if "acknowledgement_status" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN acknowledgement_status TEXT")
            if "approval_received_at" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN approval_received_at TEXT")
            if "acknowledgement_sent_at" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN acknowledgement_sent_at TEXT")
            if "acknowledgement_delay_seconds" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN acknowledgement_delay_seconds REAL")
            if "final_revalidation_started_at" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN final_revalidation_started_at TEXT")
            if "final_revalidation_completed_at" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN final_revalidation_completed_at TEXT")
            if "price_refreshed_at" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN price_refreshed_at TEXT")
            if "refreshed_price" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN refreshed_price REAL")
            if "refreshed_price_age_seconds" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN refreshed_price_age_seconds REAL")
            if "price_move_bps_since_proposal" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN price_move_bps_since_proposal REAL")
            if "final_order_decision" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN final_order_decision TEXT")
            if "final_block_reason" not in cols:
                conn.execute("ALTER TABLE approvals ADD COLUMN final_block_reason TEXT")

            cursor = conn.execute("PRAGMA table_info(proposal_batches)")
            batch_cols = [row["name"] for row in cursor.fetchall()]
            if "expiry_notified" not in batch_cols:
                conn.execute("ALTER TABLE proposal_batches ADD COLUMN expiry_notified INTEGER DEFAULT 0")

            cursor = conn.execute("PRAGMA table_info(proposal_batch_candidates)")
            batch_candidate_cols = [row["name"] for row in cursor.fetchall()]
            if "expiry_notified" not in batch_candidate_cols:
                conn.execute("ALTER TABLE proposal_batch_candidates ADD COLUMN expiry_notified INTEGER DEFAULT 0")

            def add_missing_columns(table: str, column_defs: dict[str, str]) -> None:
                existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                for column, definition in column_defs.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

            # The durable Telegram inbox was introduced after some production
            # databases had already created this table. Keep the upgrade
            # additive and idempotent so the listener can ingest updates before
            # advancing the Telegram cursor on those databases.
            add_missing_columns(
                "telegram_updates",
                {
                    "message_timestamp": "INTEGER",
                    "normalized_action": "TEXT",
                    "target_hint": "TEXT",
                    "sender_authorized": "INTEGER",
                },
            )

            add_missing_columns(
                "fills",
                {
                    "fill_notified_at": "TEXT",
                    "fill_notification_status": "TEXT",
                    "fill_notification_error": "TEXT",
                },
            )
            add_missing_columns(
                "approval_workflows",
                {
                    "logical_workflow_key": "TEXT",
                    "validation_status": "TEXT",
                    "safe_detail": "TEXT",
                    "claim_owner": "TEXT",
                    "claim_until": "TEXT",
                    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                    "last_error_category": "TEXT",
                    "update_processed_at": "TEXT",
                },
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_workflows_logical_key "
                "ON approval_workflows(logical_workflow_key) WHERE logical_workflow_key IS NOT NULL"
            )
            # Legacy consumed approvals predate durable intents. They cannot be
            # safely replayed, but they must never remain invisible. The
            # deterministic backfill is additive, idempotent, and explicitly
            # routes them to operator review without broker communication.
            conn.execute(
                """INSERT OR IGNORE INTO approval_workflows(
                       id,approval_id,proposal_id,logical_workflow_key,state,
                       validation_status,safe_detail,created_at,updated_at,terminal_at,
                       manual_review_reason,version)
                   SELECT 'historical-' || a.id,a.id,a.proposal_id,'approval:' || a.id,
                          'manual_review','historical_ambiguity',
                          'legacy consumed approval has no durable order intent',
                          COALESCE(a.consumed_at,a.created_at,?),?, ?,
                          'legacy consumed approval requires operator review; automatic submission forbidden',0
                   FROM approvals a
                   LEFT JOIN order_intents i ON i.approval_id=a.id
                   WHERE a.consumed_at IS NOT NULL AND a.proposal_id IS NOT NULL AND i.id IS NULL""",
                (now, now, now),
            )
            add_missing_columns("position_management_state", {"position_lifecycle_id": "TEXT"})
            add_missing_columns("position_management_decisions", {"position_lifecycle_id": "TEXT"})
            add_missing_columns(
                "market_memory",
                {
                    "previous_observation_change_pct": "REAL",
                    "utc_day_first_observation_change": "REAL",
                    "movement_semantics_version": "TEXT",
                },
            )
            conn.execute(
                """CREATE TRIGGER IF NOT EXISTS trg_market_memory_semantics_v2
                   AFTER INSERT ON market_memory
                   WHEN NEW.movement_semantics_version IS NULL
                   BEGIN
                     UPDATE market_memory SET
                       previous_observation_change_pct=NEW.price_change_pct,
                       utc_day_first_observation_change=NEW.session_change,
                       movement_semantics_version='v2_previous_observation_and_utc_day_first'
                     WHERE id=NEW.id;
                   END"""
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
                ("phase0_execution_integrity_v1", iso_now(), "additive durable intent, reservation, lifecycle, health and semantics schema"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
                (
                    "phase0_execution_integrity_v2_completion",
                    iso_now(),
                    "approval CAS recovery, prospective FIFO PnL, secret redaction, lock identity and completion indexes",
                ),
            )

            add_missing_columns(
                "candidate_risk_budget_decisions",
                {
                    "candidate_id": "TEXT",
                    "order_id": "TEXT",
                    "broker_order_id": "TEXT",
                    "fill_id": "TEXT",
                    "cluster_name": "TEXT",
                    "cluster_held_symbols": "TEXT",
                    "cluster_positions_count_after": "INTEGER",
                    "max_cluster_positions": "INTEGER",
                    "max_cluster_exposure_pct": "REAL",
                },
            )
            add_missing_columns(
                "candidate_batch_allocations",
                {
                    "candidate_id": "TEXT",
                },
            )
            add_missing_columns(
                "ranked_opportunity_sets",
                {
                    "candidate_id": "TEXT",
                    "proposal_id": "TEXT",
                },
            )
            add_missing_columns(
                "position_sizing_decisions",
                {
                    "initial_stop_price": "REAL",
                    "initial_risk_per_share": "REAL",
                    "initial_risk_pct": "REAL",
                    "initial_risk_dollars": "REAL",
                    "stop_source": "TEXT",
                    "entry_price_for_r": "REAL",
                    "risk_model_version": "TEXT",
                    "r_multiple_unavailable_reason": "TEXT",
                    "batch_id": "TEXT",
                    "candidate_id": "TEXT",
                    "proposal_id": "TEXT",
                    "order_id": "TEXT",
                    "broker_order_id": "TEXT",
                    "fill_id": "TEXT",
                },
            )
            add_missing_columns(
                "trade_outcomes",
                {
                    "batch_id": "TEXT",
                    "candidate_id": "TEXT",
                    "proposal_id": "TEXT",
                    "order_id": "TEXT",
                    "broker_order_id": "TEXT",
                    "fill_id": "TEXT",
                    "shadow_trade_id": "TEXT",
                    "risk_budget_decision_id": "TEXT",
                    "position_sizing_decision_id": "TEXT",
                    "approval_id": "TEXT",
                    "approval_batch_action_id": "TEXT",
                    "quantity": "REAL",
                    "notional": "REAL",
                    "score": "REAL",
                    "asset_score": "REAL",
                    "trade_score": "REAL",
                    "setup_reason": "TEXT",
                    "source": "TEXT",
                },
            )
            add_missing_columns(
                "position_management_state",
                {
                    "initial_stop_price": "REAL",
                    "initial_risk_per_share": "REAL",
                    "initial_risk_pct": "REAL",
                    "initial_risk_dollars": "REAL",
                    "stop_model": "TEXT",
                    "stop_source": "TEXT",
                    "entry_price_for_r": "REAL",
                    "risk_model_version": "TEXT",
                    "r_multiple_unavailable_reason": "TEXT",
                },
            )
            add_missing_columns(
                "position_management_decisions",
                {
                    "drawdown_from_entry_pct": "REAL",
                    "drawdown_from_peak_pct": "REAL",
                    "position_age_days": "REAL",
                    "position_age_cycles": "INTEGER",
                    "exit_review_needed": "INTEGER DEFAULT 0",
                },
            )
            add_missing_columns(
                "universe_symbols",
                {
                    "data_confidence": "TEXT",
                    "data_confidence_reason": "TEXT",
                    "data_freshness_status": "TEXT",
                    "last_successful_research_at": "TEXT",
                    "provider_health_status": "TEXT",
                    "promotion_allowed": "INTEGER DEFAULT 0",
                    "demotion_allowed": "INTEGER DEFAULT 0",
                    "stale_after_minutes": "INTEGER",
                    "universe_lane": "TEXT",
                    "alpaca_compatible": "INTEGER DEFAULT 0",
                    "exclusion_reason": "TEXT",
                    "promotion_freshness_path": "TEXT",
                    "promotion_confidence_adjustment": "TEXT",
                    "promotion_data_limitations": "TEXT",
                    "proposal_block_reason_after_promotion": "TEXT",
                    "fallback_used": "TEXT",
                    "next_review_time": "TEXT",
                    "alpaca_quote_freshness": "TEXT",
                    "alpaca_tradability_result": "TEXT",
                    "intraday_freshness": "TEXT",
                    "eod_freshness": "TEXT",
                },
            )
            add_missing_columns(
                "dynamic_universe_stage_reviews",
                {
                    "promotion_freshness_path": "TEXT",
                    "promotion_confidence_adjustment": "TEXT",
                    "promotion_data_limitations": "TEXT",
                    "proposal_block_reason_after_promotion": "TEXT",
                    "fallback_used": "TEXT",
                    "next_review_time": "TEXT",
                    "alpaca_quote_freshness": "TEXT",
                    "alpaca_tradability_result": "TEXT",
                    "intraday_freshness": "TEXT",
                    "eod_freshness": "TEXT",
                },
            )
            add_missing_columns(
                "symbol_research_scores",
                {
                    "data_confidence": "TEXT",
                    "data_confidence_reason": "TEXT",
                    "intraday_momentum_score": "REAL",
                    "screener_mover_score": "REAL",
                    "universe_lane": "TEXT",
                },
            )
            add_missing_columns(
                "research_candidate_block_reasons",
                {
                    "intraday_momentum_score": "REAL",
                    "screener_mover_score": "REAL",
                    "universe_lane": "TEXT",
                    "exclusion_reason": "TEXT",
                },
            )
            add_missing_columns(
                "performance_setups",
                {
                    "batch_id": "TEXT",
                    "actual_approved_notional": "REAL",
                    "final_submitted_notional": "REAL",
                    "order_id": "TEXT",
                    "broker_order_id": "TEXT",
                    "fill_id": "TEXT",
                    "order_status": "TEXT",
                    "fill_price": "REAL",
                    "fill_qty": "REAL",
                    "updated_at": "TEXT",
                },
            )
            add_missing_columns(
                "performance_outcomes",
                {
                    "batch_id": "TEXT",
                    "order_id": "TEXT",
                    "broker_order_id": "TEXT",
                    "fill_id": "TEXT",
                    "actual_proposal_execution_helped": "INTEGER",
                    "add_to_winner_improved_position": "INTEGER",
                    "exit_signal_avoided_loss": "INTEGER",
                },
            )
                
            cursor = conn.execute("PRAGMA table_info(market_memory)")
            cols_mem = [row["name"] for row in cursor.fetchall()]
            if "market_profile" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN market_profile TEXT")
            if "asset_score" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN asset_score REAL")
            if "asset_classification" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN asset_classification TEXT")
            if "symbol_rank" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN symbol_rank INTEGER")
            if "proposal_generated" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN proposal_generated INTEGER")
            if "no_action_reason" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN no_action_reason TEXT")
            if "asset_selection_score" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN asset_selection_score REAL")
            if "trade_decision_score" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN trade_decision_score REAL")
            if "system_confidence" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN system_confidence TEXT")
            if "gpt_confidence" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN gpt_confidence TEXT")
            if "gpt_caution" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN gpt_caution TEXT")
            if "expiry_minutes" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN expiry_minutes INTEGER")
            if "expires_at_sgt" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN expires_at_sgt TEXT")
            if "main_risk" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN main_risk TEXT")
            if "volatility_regime" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN volatility_regime TEXT")
            if "volatility_score_contribution" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN volatility_score_contribution REAL")
            if "volatility_gate_result" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN volatility_gate_result TEXT")
            if "dedupe_status" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN dedupe_status TEXT")
            if "dedupe_reason" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN dedupe_reason TEXT")
            if "paper_size_adjustment" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN paper_size_adjustment REAL")
            if "candidate_suppression_reason" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN candidate_suppression_reason TEXT")
            if "deferred_ai_review_reason" not in cols_mem:
                conn.execute("ALTER TABLE market_memory ADD COLUMN deferred_ai_review_reason TEXT")
            for col_name, col_type in new_cols.items():
                if col_name not in cols_mem:
                    conn.execute(f"ALTER TABLE market_memory ADD COLUMN {col_name} {col_type}")

            # Phase 1 is additive and dormant: ordinary trading code neither
            # reads these tables nor changes behavior based on their contents.
            # Installed production releases never load this development branch.
            from .research_validation import apply_phase1_schema

            apply_phase1_schema(conn)

    def writable(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS writable_probe (value INTEGER)")
            return True
        except sqlite3.Error:
            return False

    def start_run(self, mode: str) -> str:
        run_id = str(uuid.uuid4())
        self.execute("INSERT INTO runs VALUES (?, ?, NULL, ?, ?, NULL)", (run_id, iso_now(), "running", mode))
        if mode == "paper":
            now = iso_now()
            self.execute(
                """INSERT INTO health_heartbeats(component,state,attempted_at,detail,updated_at)
                   VALUES('scanner','unknown',?,?,?) ON CONFLICT(component) DO UPDATE SET
                   state='unknown',attempted_at=excluded.attempted_at,detail=excluded.detail,updated_at=excluded.updated_at""",
                (now, json_dumps({"run_id": run_id}), now),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, detail: str = "") -> None:
        self.execute("UPDATE runs SET ended_at=?, status=?, detail=? WHERE id=?", (iso_now(), status, detail, run_id))
        rows = self.fetch_all("SELECT mode FROM runs WHERE id=?", (run_id,))
        if rows and rows[0]["mode"] == "paper":
            now = iso_now()
            state = "healthy" if status in {"completed", "research_completed_trading_blocked_market_closed"} else ("blocked" if status == "blocked" else "failed")
            self.execute(
                """INSERT INTO health_heartbeats(component,state,completed_at,successful_at,blocked_reason,detail,updated_at)
                   VALUES('scanner',?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET
                   state=excluded.state,completed_at=excluded.completed_at,successful_at=excluded.successful_at,
                   blocked_reason=excluded.blocked_reason,detail=excluded.detail,updated_at=excluded.updated_at""",
                (state, now, now if state == "healthy" else None, detail if state == "blocked" else None, json_dumps({"run_id": run_id, "status": status}), now),
            )

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.connect() as conn:
            return conn.execute(sql, params)

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def record_check(
        self,
        run_id: str,
        name: str,
        passed: bool,
        reason: str,
        proposal_id: str | None = None,
        stage: str = "risk",
        *,
        formula_version: str = RISK_DECISION_VERSION,
        evidence_version: str = EVIDENCE_VERSION,
        config_hash: str | None = None,
    ) -> None:
        if proposal_id is None and stage == "preflight":
            self.execute("INSERT INTO preflight_checks(run_id,name,passed,reason,checked_at) VALUES(?,?,?,?,?)", (run_id, name, int(passed), reason, iso_now()))
        else:
            self.execute(
                "INSERT INTO risk_checks(run_id,proposal_id,stage,name,passed,reason,checked_at,formula_version,evidence_version,config_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, proposal_id, stage, name, int(passed), reason, iso_now(), formula_version, evidence_version, config_hash),
            )

    def audit(self, run_id: str | None, event_type: str, detail: Any, actor: str = "system") -> None:
        self.execute("INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(?,?,?,?,?)", (run_id, event_type, actor, json_dumps(detail), iso_now()))

    def active_proposals(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM trade_proposals WHERE status='pending' AND expires_at>? ORDER BY created_at", (now_iso or iso_now(),))

    def historical_proposals(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM trade_proposals WHERE NOT (status='pending' AND expires_at>?) ORDER BY created_at DESC",
            (now_iso or iso_now(),),
        )

    def expire_proposals(self, now_iso: str | None = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute("UPDATE trade_proposals SET status='expired' WHERE status='pending' AND expires_at<=?", (now_iso or iso_now(),))
            return cursor.rowcount

    def consume_approval(self, proposal_id: str, approval_id: str) -> bool:
        with self.connect() as conn:
            proposal = conn.execute("SELECT status, expires_at FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not proposal or proposal["status"] != "pending":
                return False
            expires_at = proposal["expires_at"]
            if expires_at is not None and expires_at <= iso_now():
                return False
            prior = conn.execute("SELECT 1 FROM approvals WHERE proposal_id=? AND consumed_at IS NOT NULL", (proposal_id,)).fetchone()
            if prior:
                return False
            now = iso_now()
            updated = conn.execute("UPDATE trade_proposals SET status='approved' WHERE id=? AND status='pending'", (proposal_id,)).rowcount
            if updated:
                conn.execute("UPDATE approvals SET consumed_at=?, status='consumed' WHERE id=? AND consumed_at IS NULL", (now, approval_id))
            return bool(updated)

    def ingest_telegram_update(
        self,
        update_id: int,
        *,
        message_id: int | None,
        message_timestamp: int | None,
        safe_message_type: str,
        normalized_action: str | None,
        target_hint: str | None,
        sender_authorized: bool,
    ) -> str:
        """Persist a non-secret business envelope before advancing any cursor."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT processing_state FROM telegram_updates WHERE update_id=?", (update_id,)).fetchone()
            if existing:
                return str(existing["processing_state"])
            conn.execute(
                """INSERT INTO telegram_updates(
                       update_id,message_id,message_timestamp,received_at,processing_state,safe_message_type,
                       normalized_action,target_hint,sender_authorized)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (update_id, message_id, message_timestamp, iso_now(), "received", safe_message_type, normalized_action, target_hint, int(sender_authorized)),
            )
        return "received"

    def complete_telegram_updates(self, update_ids: set[int]) -> None:
        if not update_ids:
            return
        now = iso_now()
        placeholders = ",".join("?" for _ in update_ids)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE telegram_updates SET processing_state='processed',processed_at=?,last_error_category=NULL WHERE update_id IN ({placeholders})",
                (now, *sorted(update_ids)),
            )

    def mark_telegram_update_failed(self, update_id: int, error_category: str) -> None:
        self.execute(
            "UPDATE telegram_updates SET processing_state='received',retry_count=retry_count+1,last_error_category=? WHERE update_id=?",
            (error_category, update_id),
        )

    def get_control_state(self, key: str, default: Any = None) -> Any:
        row = self.fetch_all("SELECT value FROM control_state WHERE key=?", (key,))
        return row[0]["value"] if row else default

    def set_control_state(self, key: str, value: str, updated_by: str, source: str, raw_command_redacted: str, update_id: int | None, message_id: int | None, message_ts: int | None) -> None:
        self.execute(
            """
            INSERT INTO control_state(key, value, updated_at, updated_by, source, raw_command_redacted, telegram_update_id, telegram_message_id, telegram_message_timestamp, processed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by,
                source=excluded.source,
                raw_command_redacted=excluded.raw_command_redacted,
                telegram_update_id=excluded.telegram_update_id,
                telegram_message_id=excluded.telegram_message_id,
                telegram_message_timestamp=excluded.telegram_message_timestamp,
                processed_at=excluded.processed_at
            """,
            (key, value, iso_now(), updated_by, source, raw_command_redacted, update_id, message_id, message_ts, iso_now())
        )

    def link_batch_candidate_records(self, proposal_id: str, batch_id: str, candidate_id: str) -> None:
        """Attach late-created batch candidate IDs to earlier measurement rows."""
        self.execute(
            "UPDATE candidate_risk_budget_decisions SET batch_id=?, candidate_id=?, proposal_id=? WHERE proposal_id IS NULL AND symbol=(SELECT symbol FROM trade_proposals WHERE id=?) AND run_id=(SELECT run_id FROM trade_proposals WHERE id=?)",
            (batch_id, candidate_id, proposal_id, proposal_id, proposal_id),
        )
        self.execute(
            "UPDATE candidate_batch_allocations SET batch_id=?, candidate_id=?, proposal_id=? WHERE proposal_id IS NULL AND symbol=(SELECT symbol FROM trade_proposals WHERE id=?) AND run_id=(SELECT run_id FROM trade_proposals WHERE id=?)",
            (batch_id, candidate_id, proposal_id, proposal_id, proposal_id),
        )
        self.execute(
            "UPDATE ranked_opportunity_sets SET batch_id=?, candidate_id=?, proposal_id=? WHERE proposal_id IS NULL AND symbol=(SELECT symbol FROM trade_proposals WHERE id=?) AND run_id=(SELECT run_id FROM trade_proposals WHERE id=?)",
            (batch_id, candidate_id, proposal_id, proposal_id, proposal_id),
        )
        self.execute(
            "UPDATE position_sizing_decisions SET batch_id=?, candidate_id=?, proposal_id=? WHERE proposal_id IS NULL AND symbol=(SELECT symbol FROM trade_proposals WHERE id=?) AND run_id=(SELECT run_id FROM trade_proposals WHERE id=?)",
            (batch_id, candidate_id, proposal_id, proposal_id, proposal_id),
        )

    def link_executed_order_records(self, order_id: str) -> None:
        rows = self.fetch_all(
            """
            SELECT o.id AS order_id, o.proposal_id, o.broker_order_id, f.id AS fill_id,
                   c.id AS candidate_id, c.batch_id
            FROM orders o
            LEFT JOIN fills f ON f.order_id=o.id
            LEFT JOIN proposal_batch_candidates c ON c.proposal_id=o.proposal_id
            WHERE o.id=?
            """,
            (order_id,),
        )
        if not rows:
            return
        row = rows[0]
        params = (
            row.get("order_id"),
            row.get("broker_order_id"),
            str(row.get("fill_id")) if row.get("fill_id") is not None else None,
            row.get("proposal_id"),
        )
        self.execute(
            "UPDATE candidate_risk_budget_decisions SET order_id=?, broker_order_id=?, fill_id=? WHERE proposal_id=?",
            params,
        )
        self.execute(
            "UPDATE position_sizing_decisions SET order_id=?, broker_order_id=?, fill_id=? WHERE proposal_id=?",
            params,
        )
        self.execute(
            """
            UPDATE performance_setups
            SET order_id=?, broker_order_id=?, fill_id=?, updated_at=?
            WHERE proposal_id=?
            """,
            (row.get("order_id"), row.get("broker_order_id"), str(row.get("fill_id")) if row.get("fill_id") is not None else None, iso_now(), row.get("proposal_id")),
        )
        self.execute(
            """
            UPDATE performance_outcomes
            SET order_id=?, broker_order_id=?, fill_id=?, updated_at=?
            WHERE proposal_id=?
            """,
            (row.get("order_id"), row.get("broker_order_id"), str(row.get("fill_id")) if row.get("fill_id") is not None else None, iso_now(), row.get("proposal_id")),
        )

    def upsert_actual_trade_outcome_for_order(self, order_id: str, source: str = "ranked_batch_approval") -> str | None:
        rows = self.fetch_all(
            """
            SELECT o.*, f.id AS fill_id, f.qty AS fill_qty, f.price AS fill_price, f.filled_at,
                   p.run_id AS proposal_run_id, p.payload AS proposal_payload, p.created_at AS proposal_created_at,
                   p.selection_reason, p.current_price,
                   c.id AS candidate_id, c.batch_id,
                   a.id AS approval_id,
                   aba.id AS approval_batch_action_id,
                   rb.id AS risk_budget_decision_id,
                   ps.id AS position_sizing_decision_id,
                   s.id AS shadow_trade_id
            FROM orders o
            LEFT JOIN fills f ON f.order_id=o.id
            LEFT JOIN trade_proposals p ON p.id=o.proposal_id
            LEFT JOIN proposal_batch_candidates c ON c.proposal_id=o.proposal_id
            LEFT JOIN approvals a ON a.proposal_id=o.proposal_id AND a.status='consumed'
            LEFT JOIN approval_batch_actions aba ON aba.proposal_id=o.proposal_id
            LEFT JOIN candidate_risk_budget_decisions rb ON rb.proposal_id=o.proposal_id
            LEFT JOIN position_sizing_decisions ps ON ps.proposal_id=o.proposal_id
            LEFT JOIN shadow_trades s ON s.symbol=o.symbol AND s.run_id=p.run_id
            WHERE o.id=?
            ORDER BY f.id DESC, a.created_at DESC, aba.created_at DESC
            LIMIT 1
            """,
            (order_id,),
        )
        if not rows:
            return None
        row = rows[0]
        if str(row.get("status") or "").lower() not in {"filled", "partially_filled"}:
            return None
        payload: dict[str, Any] = {}
        if row.get("proposal_payload"):
            try:
                import json

                payload = json.loads(row["proposal_payload"])
            except Exception:
                payload = {}
        entry_time = str(row.get("filled_at") or row.get("updated_at") or row.get("created_at") or iso_now())
        entry_price = row.get("fill_price") or payload.get("latest_price") or row.get("current_price")
        quantity = row.get("fill_qty") or row.get("qty")
        notional = row.get("notional")
        if notional is None and quantity is not None and entry_price is not None:
            notional = float(quantity) * float(entry_price)
        existing = self.fetch_all("SELECT id FROM trade_outcomes WHERE actual_or_shadow='actual' AND order_id=?", (order_id,))
        outcome_id = existing[0]["id"] if existing else str(uuid.uuid4())
        values = (
            outcome_id,
            order_id,
            "actual",
            row.get("symbol"),
            entry_time,
            entry_price,
            "pending_forward_returns",
            0,
            0,
            None,
            None,
            iso_now(),
            row.get("batch_id"),
            row.get("candidate_id"),
            row.get("proposal_id"),
            order_id,
            row.get("broker_order_id"),
            str(row.get("fill_id")) if row.get("fill_id") is not None else None,
            row.get("shadow_trade_id"),
            row.get("risk_budget_decision_id"),
            row.get("position_sizing_decision_id"),
            row.get("approval_id"),
            row.get("approval_batch_action_id"),
            quantity,
            notional,
            payload.get("score"),
            payload.get("asset_score"),
            payload.get("score"),
            row.get("selection_reason") or payload.get("reason"),
            source,
        )
        self.execute(
            """
            INSERT INTO trade_outcomes(
                id, trade_id, actual_or_shadow, symbol, entry_time, entry_price, outcome_status,
                stop_hit, target_reached, add_on_improved, beat_shadow_alternatives, updated_at,
                batch_id, candidate_id, proposal_id, order_id, broker_order_id, fill_id,
                shadow_trade_id, risk_budget_decision_id, position_sizing_decision_id,
                approval_id, approval_batch_action_id, quantity, notional, score, asset_score,
                trade_score, setup_reason, source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                trade_id=excluded.trade_id,
                entry_time=excluded.entry_time,
                entry_price=excluded.entry_price,
                outcome_status=excluded.outcome_status,
                updated_at=excluded.updated_at,
                batch_id=excluded.batch_id,
                candidate_id=excluded.candidate_id,
                proposal_id=excluded.proposal_id,
                order_id=excluded.order_id,
                broker_order_id=excluded.broker_order_id,
                fill_id=excluded.fill_id,
                shadow_trade_id=excluded.shadow_trade_id,
                risk_budget_decision_id=excluded.risk_budget_decision_id,
                position_sizing_decision_id=excluded.position_sizing_decision_id,
                approval_id=excluded.approval_id,
                approval_batch_action_id=excluded.approval_batch_action_id,
                quantity=excluded.quantity,
                notional=excluded.notional,
                score=excluded.score,
                asset_score=excluded.asset_score,
                trade_score=excluded.trade_score,
                setup_reason=excluded.setup_reason,
                source=excluded.source
            """,
            values,
        )
        if row.get("shadow_trade_id"):
            self.execute(
                "UPDATE shadow_trades SET selected_actual_trade_this_cycle=1, reason_not_executed='executed_as_actual' WHERE id=?",
                (row["shadow_trade_id"],),
            )
        return outcome_id
