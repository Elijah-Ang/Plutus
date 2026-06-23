from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .utils import PROJECT_ROOT, iso_now, json_dumps

TABLE_DEFINITIONS: dict[str, str] = {
    "runs": "id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, mode TEXT NOT NULL, detail TEXT",
    "preflight_checks": "id INTEGER PRIMARY KEY, run_id TEXT, name TEXT, passed INTEGER, reason TEXT, checked_at TEXT",
    "market_snapshots": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, price REAL, price_at TEXT, volume REAL, payload TEXT, created_at TEXT",
    "indicators": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, values_json TEXT, created_at TEXT",
    "signals": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, side TEXT, action TEXT, strategy_version TEXT, reason TEXT, confidence REAL, created_at TEXT, expires_at TEXT, payload TEXT",
    "ml_predictions": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, model_version TEXT, prediction TEXT, probability REAL, payload TEXT, created_at TEXT",
    "risk_checks": "id INTEGER PRIMARY KEY, run_id TEXT, proposal_id TEXT, stage TEXT, name TEXT, passed INTEGER, reason TEXT, checked_at TEXT",
    "ai_reviews": "id INTEGER PRIMARY KEY, run_id TEXT, proposal_id TEXT, summary TEXT, risks TEXT, caution_level TEXT, payload TEXT, created_at TEXT",
    "trade_proposals": "id TEXT PRIMARY KEY, run_id TEXT, signal_id TEXT, symbol TEXT, side TEXT, notional REAL, status TEXT, created_at TEXT, expires_at TEXT, strategy_version TEXT, payload TEXT, expiry_notified INTEGER DEFAULT 0, telegram_message_id TEXT, proposal_market_rank INTEGER, proposal_eligible_rank INTEGER, selection_reason TEXT, ai_review_status TEXT, ai_confidence TEXT, ai_caution TEXT, true_score_rank INTEGER, watchlist_order INTEGER, setup_key TEXT, cooldown_applied INTEGER, cooldown_remaining_minutes REAL, cooldown_reason TEXT, revival_reason TEXT, last_proposal_status TEXT, score_delta REAL, volatility_regime_change TEXT, exit_priority_applied INTEGER, exit_trigger_reason TEXT, position_drawdown_pct REAL, average_entry_price REAL, latest_position_price REAL, gpt_exit_explanation_status TEXT, gpt_exit_confidence TEXT, gpt_exit_caution TEXT, final_proposal_message_category TEXT, emergency_exit_score REAL, emergency_exit_triggered INTEGER DEFAULT 0, emergency_exit_trigger_reason TEXT, emergency_exit_hard_trigger TEXT, emergency_exit_mode TEXT, emergency_exit_wait_seconds INTEGER, emergency_exit_user_response TEXT, emergency_exit_auto_execute_due_at TEXT, emergency_exit_auto_execute_attempted_at TEXT, emergency_exit_final_decision TEXT, emergency_exit_block_reason TEXT, current_price REAL, atr_value REAL, adverse_move_atr REAL, minutes_to_close REAL, sleep_mode_active INTEGER DEFAULT 0, suppressed_by_sleep_mode INTEGER DEFAULT 0, sleep_mode_reason TEXT, sleep_mode_suppressed_candidate INTEGER DEFAULT 0, sleep_mode_started_at TEXT, sleep_mode_ended_at TEXT",
    "approvals": "id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT, sender_id TEXT, raw_message TEXT, parsed_action TEXT, authorized INTEGER, status TEXT, created_at TEXT, consumed_at TEXT, reply_to_message_id TEXT, proposal_targeting_method TEXT, acknowledgement_status TEXT, approval_received_at TEXT, acknowledgement_sent_at TEXT, acknowledgement_delay_seconds REAL, final_revalidation_started_at TEXT, final_revalidation_completed_at TEXT, price_refreshed_at TEXT, refreshed_price REAL, refreshed_price_age_seconds REAL, price_move_bps_since_proposal REAL, final_order_decision TEXT, final_block_reason TEXT, UNIQUE(proposal_id, status) ON CONFLICT ABORT",
    "orders": "id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT UNIQUE, broker_order_id TEXT, client_order_id TEXT UNIQUE, symbol TEXT, side TEXT, notional REAL, qty REAL, status TEXT, payload TEXT, created_at TEXT, updated_at TEXT",
    "fills": "id INTEGER PRIMARY KEY, run_id TEXT, order_id TEXT, qty REAL, price REAL, filled_at TEXT, payload TEXT",
    "positions": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, qty REAL, market_value REAL, unrealized_pl REAL, payload TEXT, created_at TEXT",
    "cash_snapshots": "id INTEGER PRIMARY KEY, run_id TEXT, equity REAL, cash REAL, settled_cash REAL, realized_pl REAL, unrealized_pl REAL, created_at TEXT",
    "cashout_reviews": "id INTEGER PRIMARY KEY, run_id TEXT, payload TEXT, created_at TEXT",
    "cashout_suggestions": "id INTEGER PRIMARY KEY, run_id TEXT, suggested_withdrawal REAL, reserve REAL, reinvest REAL, reason TEXT, created_at TEXT",
    "errors": "id INTEGER PRIMARY KEY, run_id TEXT, category TEXT, message TEXT, detail TEXT, created_at TEXT",
    "audit_events": "id INTEGER PRIMARY KEY, run_id TEXT, event_type TEXT, actor TEXT, detail TEXT, created_at TEXT",
    "strategy_versions": "id TEXT PRIMARY KEY, name TEXT, version TEXT, metadata TEXT, created_at TEXT",
    "model_versions": "id TEXT PRIMARY KEY, name TEXT, version TEXT, trained_at TEXT, features TEXT, symbols TEXT, metrics TEXT, path TEXT",
    "config_snapshots": "id INTEGER PRIMARY KEY, run_id TEXT, config_json TEXT, created_at TEXT",
    "daily_summaries": "id INTEGER PRIMARY KEY, date TEXT UNIQUE, mode TEXT, realized_pl REAL, unrealized_pl REAL, equity REAL, payload TEXT, created_at TEXT",
    "market_memory": "id INTEGER PRIMARY KEY, run_id TEXT, market_profile TEXT, symbol TEXT, price REAL, prev_price REAL, price_change REAL, price_change_pct REAL, session_start_price REAL, session_change REAL, volatility REAL, signal TEXT, score REAL, classification TEXT, reason TEXT, proposal_allowed INTEGER, gpt_called INTEGER, created_at TEXT, asset_score REAL, asset_classification TEXT, symbol_rank INTEGER, proposal_generated INTEGER, no_action_reason TEXT, asset_selection_score REAL, trade_decision_score REAL, system_confidence TEXT, gpt_confidence TEXT, gpt_caution TEXT, expiry_minutes INTEGER, expires_at_sgt TEXT, main_risk TEXT, volatility_regime TEXT, volatility_score_contribution REAL, volatility_gate_result TEXT, dedupe_status TEXT, dedupe_reason TEXT, paper_size_adjustment REAL, candidate_suppression_reason TEXT, deferred_ai_review_reason TEXT, true_score_rank INTEGER, watchlist_order INTEGER, setup_key TEXT, cooldown_applied INTEGER, cooldown_remaining_minutes REAL, cooldown_reason TEXT, revival_reason TEXT, last_proposal_status TEXT, score_delta REAL, volatility_regime_change TEXT, exit_priority_applied INTEGER, exit_trigger_reason TEXT, position_drawdown_pct REAL, average_entry_price REAL, latest_position_price REAL, gpt_exit_explanation_status TEXT, gpt_exit_confidence TEXT, gpt_exit_caution TEXT, final_proposal_message_category TEXT, emergency_exit_score REAL, emergency_exit_triggered INTEGER DEFAULT 0, emergency_exit_trigger_reason TEXT, emergency_exit_hard_trigger TEXT, emergency_exit_mode TEXT, emergency_exit_wait_seconds INTEGER, emergency_exit_user_response TEXT, emergency_exit_auto_execute_due_at TEXT, emergency_exit_auto_execute_attempted_at TEXT, emergency_exit_final_decision TEXT, emergency_exit_block_reason TEXT, current_price REAL, atr_value REAL, adverse_move_atr REAL, minutes_to_close REAL, sleep_mode_active INTEGER DEFAULT 0, suppressed_by_sleep_mode INTEGER DEFAULT 0, sleep_mode_reason TEXT, sleep_mode_suppressed_candidate INTEGER DEFAULT 0, sleep_mode_started_at TEXT, sleep_mode_ended_at TEXT",
    "telegram_digests": "id INTEGER PRIMARY KEY, run_id TEXT, window_start TEXT, window_end TEXT, sent_at TEXT, symbols TEXT, summary_text TEXT, status TEXT",
    "control_state": "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT, source TEXT, raw_command_redacted TEXT, telegram_update_id INTEGER, telegram_message_id INTEGER, telegram_message_timestamp INTEGER, processed_at TEXT",
    "trade_setups": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, timestamp TEXT, side TEXT, action TEXT, setup_key TEXT, is_active INTEGER, price REAL, score REAL, asset_score REAL, volatility_regime TEXT, trend_state TEXT, gpt_status TEXT, proposal_eligible INTEGER, proposal_sent INTEGER, block_reason TEXT",
    "shadow_trades": "id TEXT PRIMARY KEY, run_id TEXT, setup_id TEXT, symbol TEXT, side TEXT, would_have_entry_price REAL, would_have_entry_time TEXT, would_have_notional REAL, would_have_shares REAL, would_have_stop_price REAL, would_have_stop_distance_pct REAL, reason_not_executed TEXT, score REAL, volatility_regime TEXT, gpt_confidence TEXT, gpt_caution TEXT, setup_key TEXT, portfolio_state_json TEXT, sleep_mode_active INTEGER, cooldown_state TEXT, selected_actual_trade_this_cycle INTEGER",
    "trade_outcomes": "id TEXT PRIMARY KEY, trade_id TEXT, actual_or_shadow TEXT, symbol TEXT, entry_time TEXT, entry_price REAL, outcome_status TEXT, forward_return_1d REAL, forward_return_5d REAL, forward_return_20d REAL, max_favorable_excursion REAL, max_adverse_excursion REAL, stop_hit INTEGER, target_reached INTEGER, add_on_improved INTEGER, beat_shadow_alternatives INTEGER, updated_at TEXT",
    "position_sizing_decisions": "id TEXT PRIMARY KEY, run_id TEXT, symbol TEXT, timestamp TEXT, portfolio_equity REAL, risk_budget REAL, stop_distance_dollars REAL, risk_based_shares REAL, score_adjusted_notional REAL, vol_adjusted_notional REAL, final_notional REAL, suggested_shares REAL, base_notional REAL, score_multiplier REAL, volatility_multiplier REAL, stop_model_used TEXT",
    "portfolio_exposure_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, total_exposure_pct REAL, total_exposure_dollars REAL, single_symbol_exposure_json TEXT, cluster_exposure_json TEXT",
    "candidate_rankings": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, symbol TEXT, true_score_rank INTEGER, final_candidate_rank INTEGER, setup_quality_score REAL, portfolio_fit_score REAL, diversification_score REAL, sizing_score REAL, reason_selected TEXT, reason_not_selected TEXT",
    "add_on_opportunities": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, symbol TEXT, current_qty REAL, avg_entry_price REAL, current_price REAL, unrealized_gain_pct REAL, proposed_add_notional REAL, proposed_add_shares REAL, score REAL, score_improvement REAL, passed INTEGER, block_reasons TEXT",
    "performance_lab_summaries": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, total_qualified_setups INTEGER, total_shadow_trades INTEGER, total_actual_trades INTEGER",
    "proposal_batches": "id TEXT PRIMARY KEY, run_id TEXT, telegram_message_id TEXT, status TEXT, created_at TEXT, expires_at TEXT, payload TEXT",
    "proposal_batch_candidates": "id TEXT PRIMARY KEY, batch_id TEXT, proposal_id TEXT, telegram_message_id TEXT, candidate_symbol TEXT, candidate_side TEXT, candidate_action TEXT, candidate_status TEXT, rank INTEGER, reason TEXT, created_at TEXT, expires_at TEXT, payload TEXT",
    "candidate_risk_budget_decisions": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, proposal_id TEXT, symbol TEXT, timestamp TEXT, risk_per_trade_pct REAL, open_risk_after_pct REAL, max_open_risk_pct REAL, total_exposure_after_pct REAL, single_symbol_exposure_after_pct REAL, cluster_exposure_after_pct REAL, buying_power REAL, passed INTEGER, block_reason TEXT",
    "candidate_batch_allocations": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, proposal_id TEXT, symbol TEXT, rank INTEGER, raw_suggested_notional REAL, adjusted_suggested_notional REAL, risk_budget_adjusted_notional REAL, final_suggested_notional REAL, final_suggested_shares REAL, cap_reason TEXT, reduction_reason TEXT, created_at TEXT",
    "approval_batch_actions": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, proposal_id TEXT, sender_id TEXT, raw_message TEXT, action TEXT, status TEXT, created_at TEXT, detail TEXT",
    "risk_budget_snapshots": "id TEXT PRIMARY KEY, run_id TEXT, timestamp TEXT, total_exposure_pct REAL, open_risk_pct REAL, daily_realized_loss_pct REAL, max_open_risk_pct REAL, buying_power REAL, payload TEXT",
    "ranked_opportunity_sets": "id TEXT PRIMARY KEY, run_id TEXT, batch_id TEXT, timestamp TEXT, symbol TEXT, rank INTEGER, actionable INTEGER, reason TEXT, score REAL, suggested_notional REAL, suggested_shares REAL, payload TEXT",
}


class Storage:
    def __init__(self, path: str | Path = PROJECT_ROOT / "data" / "trading_agent.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            for table, columns in TABLE_DEFINITIONS.items():
                conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns})')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_status ON trade_proposals(status, expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_proposal ON risk_checks(proposal_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id)")
            
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

    def writable(self) -> bool:
        try:
            self.initialize()
            with self.connect() as conn:
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS writable_probe (value INTEGER)")
            return True
        except sqlite3.Error:
            return False

    def start_run(self, mode: str) -> str:
        run_id = str(uuid.uuid4())
        self.execute("INSERT INTO runs VALUES (?, ?, NULL, ?, ?, NULL)", (run_id, iso_now(), "running", mode))
        return run_id

    def finish_run(self, run_id: str, status: str, detail: str = "") -> None:
        self.execute("UPDATE runs SET ended_at=?, status=?, detail=? WHERE id=?", (iso_now(), status, detail, run_id))

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.connect() as conn:
            return conn.execute(sql, params)

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def record_check(self, run_id: str, name: str, passed: bool, reason: str, proposal_id: str | None = None, stage: str = "risk") -> None:
        if proposal_id is None and stage == "preflight":
            self.execute("INSERT INTO preflight_checks(run_id,name,passed,reason,checked_at) VALUES(?,?,?,?,?)", (run_id, name, int(passed), reason, iso_now()))
        else:
            self.execute("INSERT INTO risk_checks(run_id,proposal_id,stage,name,passed,reason,checked_at) VALUES(?,?,?,?,?,?,?)", (run_id, proposal_id, stage, name, int(passed), reason, iso_now()))

    def audit(self, run_id: str | None, event_type: str, detail: Any, actor: str = "system") -> None:
        self.execute("INSERT INTO audit_events(run_id,event_type,actor,detail,created_at) VALUES(?,?,?,?,?)", (run_id, event_type, actor, json_dumps(detail), iso_now()))

    def active_proposals(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM trade_proposals WHERE status='pending' AND expires_at>? ORDER BY created_at", (now_iso or iso_now(),))

    def expire_proposals(self, now_iso: str | None = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute("UPDATE trade_proposals SET status='expired' WHERE status='pending' AND expires_at<=?", (now_iso or iso_now(),))
            return cursor.rowcount

    def consume_approval(self, proposal_id: str, approval_id: str) -> bool:
        with self.connect() as conn:
            proposal = conn.execute("SELECT status FROM trade_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not proposal or proposal["status"] != "pending":
                return False
            prior = conn.execute("SELECT 1 FROM approvals WHERE proposal_id=? AND consumed_at IS NOT NULL", (proposal_id,)).fetchone()
            if prior:
                return False
            now = iso_now()
            updated = conn.execute("UPDATE trade_proposals SET status='approved' WHERE id=? AND status='pending'", (proposal_id,)).rowcount
            if updated:
                conn.execute("UPDATE approvals SET consumed_at=?, status='consumed' WHERE id=? AND consumed_at IS NULL", (now, approval_id))
            return bool(updated)

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
