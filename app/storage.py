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
    "trade_proposals": "id TEXT PRIMARY KEY, run_id TEXT, signal_id TEXT, symbol TEXT, side TEXT, notional REAL, status TEXT, created_at TEXT, expires_at TEXT, strategy_version TEXT, payload TEXT",
    "approvals": "id TEXT PRIMARY KEY, run_id TEXT, proposal_id TEXT, sender_id TEXT, raw_message TEXT, parsed_action TEXT, authorized INTEGER, status TEXT, created_at TEXT, consumed_at TEXT, UNIQUE(proposal_id, status) ON CONFLICT ABORT",
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
    "market_memory": "id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, price REAL, prev_price REAL, price_change REAL, price_change_pct REAL, session_start_price REAL, session_change REAL, volatility REAL, signal TEXT, score REAL, classification TEXT, reason TEXT, proposal_allowed INTEGER, gpt_called INTEGER, created_at TEXT",
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
