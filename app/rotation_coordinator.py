from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .formula_versions import (
    CONFIGURATION_SCHEMA_VERSION,
    PHASE4_ALLOCATION_VERSION,
    PHASE4_SCHEMA_VERSION,
    ROTATION_FORMULA_VERSION,
    ROTATION_SCHEMA_VERSION,
    STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION,
    STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION,
)
from .utils import iso_now, json_dumps


def _authorized_strategy_ids(value: Any, *, label: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{label} authorized strategy set is empty or invalid")
    result: set[str] = set()
    for item in value:
        if isinstance(item, Mapping):
            identifier = item.get("strategy_version") or item.get("id") or item.get("name")
        else:
            identifier = item
        identifier = str(identifier or "").strip()
        if not identifier or identifier in result:
            raise RuntimeError(f"{label} authorized strategy identity is invalid or duplicated")
        result.add(identifier)
    return result


class RotationState(StrEnum):
    PENDING_GROUP_APPROVAL = "pending_group_approval"
    APPROVED_EXIT_PENDING = "approved_exit_pending"
    EXIT_SUBMITTED = "exit_submitted"
    EXIT_PARTIALLY_FILLED = "exit_partially_filled"
    EXIT_FILLED = "exit_filled"
    RECONCILIATION_PENDING = "reconciliation_pending"
    RECONCILED = "reconciled"
    ENTRY_REVALIDATING = "entry_revalidating"
    ENTRY_RESERVED = "entry_reserved"
    ENTRY_SUBMITTED = "entry_submitted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXIT_FAILED = "exit_failed"
    ENTRY_BLOCKED = "entry_blocked"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    RotationState.COMPLETED,
    RotationState.REJECTED,
    RotationState.EXPIRED,
    RotationState.EXIT_FAILED,
    RotationState.ENTRY_BLOCKED,
    RotationState.CANCELLED,
}


ALLOWED_TRANSITIONS: dict[RotationState, set[RotationState]] = {
    RotationState.PENDING_GROUP_APPROVAL: {
        RotationState.APPROVED_EXIT_PENDING,
        RotationState.REJECTED,
        RotationState.EXPIRED,
        RotationState.CANCELLED,
    },
    RotationState.APPROVED_EXIT_PENDING: {
        RotationState.EXIT_SUBMITTED,
        RotationState.EXIT_FAILED,
        RotationState.EXPIRED,
        RotationState.CANCELLED,
    },
    RotationState.EXIT_SUBMITTED: {
        RotationState.EXIT_PARTIALLY_FILLED,
        RotationState.EXIT_FILLED,
        RotationState.EXIT_FAILED,
        RotationState.EXPIRED,
    },
    RotationState.EXIT_PARTIALLY_FILLED: {
        RotationState.EXIT_PARTIALLY_FILLED,
        RotationState.EXIT_FILLED,
        RotationState.RECONCILIATION_PENDING,
        RotationState.EXIT_FAILED,
        RotationState.EXPIRED,
    },
    RotationState.EXIT_FILLED: {RotationState.RECONCILIATION_PENDING},
    RotationState.RECONCILIATION_PENDING: {
        RotationState.RECONCILED,
        RotationState.ENTRY_BLOCKED,
    },
    RotationState.RECONCILED: {
        RotationState.ENTRY_REVALIDATING,
        RotationState.ENTRY_BLOCKED,
        RotationState.EXPIRED,
    },
    RotationState.ENTRY_REVALIDATING: {
        RotationState.RECONCILED,
        RotationState.ENTRY_RESERVED,
        RotationState.ENTRY_BLOCKED,
        RotationState.EXPIRED,
    },
    RotationState.ENTRY_RESERVED: {
        RotationState.ENTRY_SUBMITTED,
        RotationState.ENTRY_BLOCKED,
    },
    RotationState.ENTRY_SUBMITTED: {RotationState.COMPLETED, RotationState.ENTRY_BLOCKED},
}


@dataclass(frozen=True)
class RotationApprovalResult:
    accepted: bool
    group_id: str | None
    action: str
    reason: str


@dataclass(frozen=True)
class RevalidatedRotationEntry:
    allowed: bool
    group_id: str
    contingent_entry_id: str
    symbol: str
    final_quantity: float
    final_notional: float
    final_stop_risk: float
    binding_cap: str
    reason: str


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def parse_rotation_approval(
    text: str,
    pending_groups: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> RotationApprovalResult:
    """Parse only explicit rotation commands; a generic yes can never match."""
    normalized = " ".join(str(text or "").strip().lower().split())
    match = re.fullmatch(r"(approve|reject) rotation ([a-f0-9]{6,64})", normalized)
    if not match:
        return RotationApprovalResult(False, None, "unclear", "use APPROVE ROTATION <group-id>")
    action, target = match.groups()
    matches = [row for row in pending_groups if str(row.get("id") or "").lower().startswith(target)]
    if len(matches) != 1:
        return RotationApprovalResult(False, None, action, "rotation group target is missing or ambiguous")
    group = matches[0]
    if RotationState(str(group.get("state"))) != RotationState.PENDING_GROUP_APPROVAL:
        return RotationApprovalResult(False, str(group.get("id")), action, "rotation group is no longer pending")
    if _utc(str(group["expires_at"])) <= (now or datetime.now(UTC)):
        return RotationApprovalResult(False, str(group.get("id")), action, "rotation group expired")
    return RotationApprovalResult(True, str(group.get("id")), action, "explicit rotation group command")


def apply_rotation_schema(conn: Any, *, record_migration: bool = True) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS rotation_groups(
             id TEXT PRIMARY KEY,run_id TEXT,state TEXT NOT NULL,expires_at TEXT NOT NULL,
             approval_id TEXT,approved_at TEXT,estimated_release_notional REAL NOT NULL DEFAULT 0,
             actual_released_notional REAL NOT NULL DEFAULT 0,actual_released_risk REAL NOT NULL DEFAULT 0,
             reconciled_cash REAL,reconciled_buying_power REAL,reconciliation_fingerprint TEXT,
             registry_snapshot_id TEXT,allocation_id TEXT,origin_run_id TEXT,
             revalidation_run_id TEXT,revalidation_registry_snapshot_id TEXT,
             revalidation_allocation_id TEXT,revalidated_at TEXT,terminal_reason TEXT,
             schema_version TEXT NOT NULL,formula_version TEXT NOT NULL,config_hash TEXT,
             decision_fingerprint TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS rotation_steps(
             id TEXT PRIMARY KEY,group_id TEXT NOT NULL,sequence INTEGER NOT NULL,role TEXT NOT NULL,
             proposal_id TEXT,intent_id TEXT,symbol TEXT NOT NULL,side TEXT NOT NULL,state TEXT NOT NULL,
             requested_quantity REAL,filled_quantity REAL NOT NULL DEFAULT 0,filled_notional REAL NOT NULL DEFAULT 0,
             released_risk REAL NOT NULL DEFAULT 0,reason TEXT,payload TEXT NOT NULL,updated_at TEXT NOT NULL,
             UNIQUE(group_id,sequence,role))""",
        """CREATE TABLE IF NOT EXISTS rotation_contingent_entries(
             id TEXT PRIMARY KEY,group_id TEXT NOT NULL,candidate_key TEXT NOT NULL,strategy_version TEXT NOT NULL,
             symbol TEXT NOT NULL,displayed_max_quantity REAL NOT NULL,displayed_max_notional REAL NOT NULL,
             displayed_max_stop_risk REAL NOT NULL,expires_at TEXT NOT NULL,state TEXT NOT NULL,
             final_quantity REAL,final_notional REAL,final_stop_risk REAL,binding_cap TEXT,
             proposal_id TEXT,intent_id TEXT,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
             UNIQUE(group_id,candidate_key))""",
        """CREATE TABLE IF NOT EXISTS rotation_events(
             id TEXT PRIMARY KEY,group_id TEXT NOT NULL,event_key TEXT NOT NULL,event_type TEXT NOT NULL,
             from_state TEXT,to_state TEXT,safe_detail TEXT NOT NULL,created_at TEXT NOT NULL,
             notification_claimed_at TEXT,notification_sent_at TEXT,UNIQUE(group_id,event_key))""",
        """CREATE TABLE IF NOT EXISTS rotation_group_approvals(
             id TEXT PRIMARY KEY,group_id TEXT NOT NULL,approval_id TEXT NOT NULL,sender_id TEXT NOT NULL,
             command TEXT NOT NULL,ceiling_fingerprint TEXT NOT NULL,status TEXT NOT NULL,
             created_at TEXT NOT NULL,consumed_at TEXT,UNIQUE(group_id,approval_id))""",
        """CREATE TABLE IF NOT EXISTS rotation_group_display_envelopes(
             id TEXT PRIMARY KEY,group_id TEXT NOT NULL UNIQUE,telegram_message_id TEXT NOT NULL,
             displayed_at TEXT NOT NULL,expires_at TEXT NOT NULL,envelope_json TEXT NOT NULL,
             display_fingerprint TEXT NOT NULL UNIQUE,workflow_fingerprint TEXT NOT NULL,
             created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_rotation_groups_state ON rotation_groups(state,expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_rotation_events_notify ON rotation_events(notification_sent_at,created_at)",
    )
    for statement in statements:
        conn.execute(statement)
    additions = {
        "position_lifecycle_id": "TEXT",
        "exit_proposal_fingerprint": "TEXT",
        "contingent_candidate_fingerprint": "TEXT",
        "displayed_approval_fingerprint": "TEXT",
        "workflow_structure_fingerprint": "TEXT",
        "origin_run_id": "TEXT",
        "revalidation_run_id": "TEXT",
        "revalidation_registry_snapshot_id": "TEXT",
        "revalidation_allocation_id": "TEXT",
        "revalidated_at": "TEXT",
    }
    present = {row[1] for row in conn.execute("PRAGMA table_info(rotation_groups)")}
    for name, kind in additions.items():
        if name not in present:
            conn.execute(f"ALTER TABLE rotation_groups ADD COLUMN {name} {kind}")
    approval_additions = {
        "display_envelope_id": "TEXT",
        "display_fingerprint": "TEXT",
        "workflow_fingerprint": "TEXT",
        "telegram_message_id": "TEXT",
    }
    approval_present = {row[1] for row in conn.execute("PRAGMA table_info(rotation_group_approvals)")}
    for name, kind in approval_additions.items():
        if name not in approval_present:
            conn.execute(f"ALTER TABLE rotation_group_approvals ADD COLUMN {name} {kind}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rotation_active_lifecycle "
        "ON rotation_groups(position_lifecycle_id,state,expires_at)"
    )
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (ROTATION_SCHEMA_VERSION, iso_now(), "additive exit-first rotation groups, dependencies, approvals and events"),
        )


class RotationCoordinator:
    def __init__(self, storage: Any, *, config_hash: str | None = None) -> None:
        self.storage = storage
        self.config_hash = config_hash

    def _verify_authority_records(
        self,
        group: Mapping[str, Any],
        *,
        registry_snapshot_id: str,
        allocation_id: str,
        evaluation_time: str | datetime,
        allow_later_run: bool = False,
    ) -> str:
        """Verify the immutable registry/allocation pair at an action boundary."""
        snapshot_id = str(registry_snapshot_id or "").strip()
        allocation_key = str(allocation_id or "").strip()
        if not snapshot_id or not allocation_key:
            raise ValueError("rotation requires non-empty registry snapshot and allocation authority IDs")

        strategy_rows = self.entries(str(group["id"]))
        if len(strategy_rows) != 1 or not str(strategy_rows[0].get("strategy_version") or ""):
            raise RuntimeError("rotation contingent strategy authority is missing")
        strategy_version = str(strategy_rows[0]["strategy_version"])
        expected_config_hash = str(group.get("config_hash") or self.config_hash or "")
        evaluated_at = _utc(evaluation_time)

        registry_rows = self.storage.fetch_all(
            """SELECT s.*,d.strategy_version,d.authorized,d.configuration_version AS decision_configuration_version,
                      d.config_hash AS decision_config_hash,d.run_id AS decision_run_id
               FROM strategy_registry_snapshots s
               JOIN strategy_registry_decisions d ON d.snapshot_id=s.id
               WHERE s.id=? AND d.strategy_version=?""",
            (snapshot_id, strategy_version),
        )
        if len(registry_rows) != 1:
            raise RuntimeError("rotation registry snapshot or strategy decision does not exist")
        registry = registry_rows[0]
        authority_run_id = str(registry.get("run_id") or "")
        expected_run_id = str(group.get("origin_run_id") or group.get("run_id") or "")
        if not authority_run_id or str(registry.get("decision_run_id") or "") != authority_run_id:
            raise RuntimeError("rotation registry authority has inconsistent run identity")
        if not allow_later_run and authority_run_id != expected_run_id:
            raise RuntimeError("rotation registry authority belongs to a different run")
        if int(registry.get("authorized") or 0) != 1:
            raise RuntimeError("rotation strategy is not authorized in the referenced registry snapshot")
        try:
            authorized_strategies = json.loads(registry.get("authorized_strategies_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("rotation registry authorized strategy record is invalid") from exc
        if strategy_version not in _authorized_strategy_ids(authorized_strategies, label="rotation registry"):
            raise RuntimeError("rotation strategy is not authorized in the referenced registry snapshot")
        if str(registry.get("config_hash") or "") != expected_config_hash or str(registry.get("decision_config_hash") or "") != expected_config_hash:
            raise RuntimeError("rotation registry configuration hash does not match current configuration")
        if str(registry.get("registry_schema_version") or "") != STRATEGY_EXECUTION_REGISTRY_SCHEMA_VERSION:
            raise RuntimeError("rotation registry schema version mismatch")
        if str(registry.get("registry_formula_version") or "") != STRATEGY_EXECUTION_REGISTRY_FORMULA_VERSION:
            raise RuntimeError("rotation registry formula version mismatch")
        if str(registry.get("configuration_version") or "") != CONFIGURATION_SCHEMA_VERSION:
            raise RuntimeError("rotation registry configuration version mismatch")
        if str(registry.get("decision_configuration_version") or "") != CONFIGURATION_SCHEMA_VERSION:
            raise RuntimeError("rotation strategy decision configuration version mismatch")

        allocation_rows = self.storage.fetch_all(
            "SELECT * FROM phase4_allocation_decisions WHERE id=?",
            (allocation_key,),
        )
        if len(allocation_rows) != 1:
            raise RuntimeError("rotation allocation authority does not exist")
        allocation = allocation_rows[0]
        if str(allocation.get("run_id") or "") != authority_run_id:
            raise RuntimeError("rotation allocation authority belongs to a different run")
        if str(allocation.get("config_hash") or "") != expected_config_hash:
            raise RuntimeError("rotation allocation configuration hash does not match current configuration")
        if not str(allocation.get("decision") or "").startswith("ALLOCATE"):
            raise RuntimeError("rotation allocation is not executable for the referenced strategy")
        if str(allocation.get("formula_version") or "") != PHASE4_ALLOCATION_VERSION:
            raise RuntimeError("rotation allocation formula version mismatch")
        try:
            payload = json.loads(allocation.get("payload") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("rotation allocation payload is invalid") from exc
        if str(payload.get("schema_version") or "") != PHASE4_SCHEMA_VERSION:
            raise RuntimeError("rotation allocation schema version mismatch")
        if str(payload.get("formula_version") or "") != PHASE4_ALLOCATION_VERSION:
            raise RuntimeError("rotation allocation payload formula version mismatch")
        if str(payload.get("config_hash") or "") != expected_config_hash:
            raise RuntimeError("rotation allocation payload configuration hash mismatch")
        if str(payload.get("registry_snapshot_id") or "") != snapshot_id:
            raise RuntimeError("rotation allocation is not linked to the referenced registry snapshot")
        authorized = _authorized_strategy_ids(
            payload.get("authorized_strategies"), label="rotation allocation"
        )
        if strategy_version not in authorized:
            raise RuntimeError("rotation strategy is not authorized in the referenced allocation")

        for label, timestamp in (
            ("registry snapshot", registry.get("evaluated_at")),
            ("allocation", allocation.get("decided_at")),
        ):
            try:
                age = (evaluated_at - _utc(str(timestamp))).total_seconds()
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{label} freshness timestamp is invalid") from exc
            if age < -5.0 or age > 300.0:
                raise RuntimeError(f"{label} authority is stale for the supplied evaluation time")
        return authority_run_id

    def create_group(
        self,
        *,
        run_id: str,
        exit_legs: Sequence[Mapping[str, Any]],
        contingent_entries: Sequence[Mapping[str, Any]],
        expires_at: str | datetime,
        registry_snapshot_id: str | None = None,
        allocation_id: str | None = None,
        evaluation_time: str | datetime,
    ) -> dict[str, Any]:
        if not str(registry_snapshot_id or "").strip() or not str(allocation_id or "").strip():
            raise ValueError("rotation requires non-empty registry_snapshot_id and allocation_id")
        if not exit_legs or len(contingent_entries) != 1:
            raise ValueError("rotation requires valid exits and exactly one contingent entry")
        evaluated_at = _utc(evaluation_time)
        expiry = _utc(expires_at)
        if expiry <= evaluated_at:
            raise ValueError("rotation expiry must be in the future")
        canonical_exits: list[dict[str, Any]] = []
        for leg in exit_legs:
            if str(leg.get("side") or "").lower() != "sell" or float(leg.get("quantity") or 0) <= 0:
                raise ValueError("rotation exits must be genuine positive-quantity sells")
            proposal_id = str(leg.get("proposal_id") or leg.get("id") or "")
            lifecycle_id = str(leg.get("position_lifecycle_id") or "")
            symbol = str(leg.get("symbol") or "").upper()
            if not proposal_id or not lifecycle_id or not symbol:
                raise ValueError("rotation exit requires proposal, symbol, and position lifecycle identity")
            canonical_exits.append({
                "proposal_id": proposal_id,
                "position_lifecycle_id": lifecycle_id,
                "symbol": symbol,
                "quantity": float(leg.get("quantity") or 0),
                "estimated_notional": max(0.0, float(leg.get("estimated_notional") or 0)),
                "estimated_released_risk": max(0.0, float(leg.get("estimated_released_risk") or 0)),
                "reason": str(leg.get("reason") or ""),
                "position_state": str(leg.get("position_state") or ""),
            })
        exit_proposal_ids = [row["proposal_id"] for row in canonical_exits]
        if len(exit_proposal_ids) != len(set(exit_proposal_ids)):
            raise ValueError("rotation exit proposal linkage must be unique")
        lifecycle_ids = {row["position_lifecycle_id"] for row in canonical_exits}
        if len(lifecycle_ids) != 1:
            raise ValueError("one rotation cannot span position lifecycles")
        lifecycle_id = next(iter(lifecycle_ids))
        active_lifecycle = self.storage.fetch_all(
            "SELECT id FROM position_lifecycles WHERE id=? AND state='active'", (lifecycle_id,)
        )
        if len(active_lifecycle) != 1:
            raise ValueError("rotation position lifecycle is not active")
        canonical_entries: list[dict[str, Any]] = []
        for candidate in contingent_entries:
            if str(candidate.get("side") or "buy").lower() != "buy":
                raise ValueError("rotation contingent entries must be buys")
            candidate_key = str(candidate.get("candidate_key") or "")
            proposal_id = str(candidate.get("proposal_id") or "")
            strategy_version = str(candidate.get("strategy_version") or "")
            quantity = float(candidate.get("max_quantity") or candidate.get("quantity") or 0)
            notional = float(candidate.get("max_notional") or candidate.get("notional") or 0)
            risk = float(candidate.get("max_stop_risk") or candidate.get("stop_risk") or 0)
            if not candidate_key or not proposal_id or not strategy_version or quantity <= 0 or notional <= 0 or risk <= 0:
                raise ValueError("rotation candidate requires stable identity and positive displayed ceilings")
            canonical_entries.append({
                "proposal_id": proposal_id,
                "candidate_key": candidate_key,
                "strategy_version": strategy_version,
                "symbol": str(candidate.get("symbol") or "").upper(),
                "max_quantity": quantity,
                "max_notional": notional,
                "max_stop_risk": risk,
                "payload": dict(candidate.get("payload") or {}),
            })
        if canonical_entries[0]["proposal_id"] in set(exit_proposal_ids):
            raise ValueError("rotation proposal cannot be linked as both exit and contingent entry")
        exit_fingerprint = _fingerprint(exit_proposal_ids)
        candidate_fingerprint = _fingerprint(canonical_entries[0]["candidate_key"])
        displayed_fingerprint = _fingerprint({
            "quantity": canonical_entries[0]["max_quantity"],
            "notional": canonical_entries[0]["max_notional"],
            "stop_risk": canonical_entries[0]["max_stop_risk"],
        })
        structure_fingerprint = _fingerprint({
            "roles": ["rotation_exit" for _ in canonical_exits] + ["rotation_entry"],
            "exit_count": len(canonical_exits),
            "entry_count": 1,
        })
        workflow_identity = _fingerprint({
            "exits": canonical_exits,
            "entries": canonical_entries,
            "position_lifecycle_id": lifecycle_id,
            "exit_proposal_fingerprint": exit_fingerprint,
            "contingent_candidate_fingerprint": candidate_fingerprint,
            "displayed_approval_fingerprint": displayed_fingerprint,
            "workflow_structure_fingerprint": structure_fingerprint,
            "registry_snapshot_id": registry_snapshot_id,
            "allocation_id": allocation_id,
            "formula": ROTATION_FORMULA_VERSION,
            "config_hash": self.config_hash,
        })
        fingerprint = _fingerprint({
            "workflow_identity": workflow_identity,
            "run_id": run_id,
            "expires_at": expiry.isoformat(),
            "evaluation_time": evaluated_at.isoformat(),
        })
        group_id = fingerprint[:32]
        now = evaluated_at.isoformat()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            nonterminal = tuple(state.value for state in TERMINAL_STATES)
            placeholders = ",".join("?" for _ in nonterminal)
            existing = conn.execute(
                f"""SELECT * FROM rotation_groups WHERE position_lifecycle_id=? AND expires_at>?
                    AND state NOT IN ({placeholders}) ORDER BY created_at""",
                (lifecycle_id, evaluated_at.isoformat(), *nonterminal),
            ).fetchall()
            for row in existing:
                exact = (
                    row["position_lifecycle_id"] == lifecycle_id
                    and row["exit_proposal_fingerprint"] == exit_fingerprint
                    and row["contingent_candidate_fingerprint"] == candidate_fingerprint
                    and row["displayed_approval_fingerprint"] == displayed_fingerprint
                    and row["workflow_structure_fingerprint"] == structure_fingerprint
                    and row["registry_snapshot_id"] == registry_snapshot_id
                    and row["allocation_id"] == allocation_id
                    and row["config_hash"] == self.config_hash
                    and row["formula_version"] == ROTATION_FORMULA_VERSION
                    and row["schema_version"] == ROTATION_SCHEMA_VERSION
                )
                if exact:
                    return dict(row)
                raise RuntimeError("a conflicting active rotation already owns this position lifecycle")
            conn.execute(
                """INSERT INTO rotation_groups(
                     id,run_id,origin_run_id,state,expires_at,estimated_release_notional,registry_snapshot_id,allocation_id,
                     schema_version,formula_version,config_hash,decision_fingerprint,created_at,updated_at,
                     position_lifecycle_id,exit_proposal_fingerprint,contingent_candidate_fingerprint,
                     displayed_approval_fingerprint,workflow_structure_fingerprint)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (group_id, run_id, run_id, RotationState.PENDING_GROUP_APPROVAL.value, expiry.isoformat(),
                 sum(row["estimated_notional"] for row in canonical_exits), registry_snapshot_id, allocation_id,
                 ROTATION_SCHEMA_VERSION, ROTATION_FORMULA_VERSION, self.config_hash, fingerprint, now, now,
                 lifecycle_id, exit_fingerprint, candidate_fingerprint, displayed_fingerprint,
                 structure_fingerprint),
            )
            for sequence, leg in enumerate(canonical_exits):
                conn.execute(
                    """INSERT INTO rotation_steps(
                         id,group_id,sequence,role,proposal_id,symbol,side,state,requested_quantity,payload,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), group_id, sequence, "rotation_exit", leg["proposal_id"], leg["symbol"],
                     "sell", "pending", leg["quantity"], json_dumps(leg), now),
                )
            for candidate in canonical_entries:
                entry_id = _fingerprint([group_id, candidate["candidate_key"]])[:32]
                conn.execute(
                    """INSERT INTO rotation_contingent_entries(
                         id,group_id,candidate_key,strategy_version,symbol,displayed_max_quantity,
                         displayed_max_notional,displayed_max_stop_risk,expires_at,state,proposal_id,payload,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (entry_id, group_id, candidate["candidate_key"], candidate["strategy_version"], candidate["symbol"],
                     candidate["max_quantity"], candidate["max_notional"], candidate["max_stop_risk"],
                     expiry.isoformat(), "contingent", candidate["proposal_id"], json_dumps(candidate["payload"]), now, now),
                )
            self._event(conn, group_id, "created", "rotation_group_created", None,
                        RotationState.PENDING_GROUP_APPROVAL, {"capital_reserved": False, "estimated_release_only": True})
        return self.get_group(group_id)

    @staticmethod
    def _group_display_envelope(conn: Any, group_id: str, telegram_message_id: str) -> dict[str, Any]:
        group_row = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
        if group_row is None:
            raise KeyError(group_id)
        group = dict(group_row)
        steps = conn.execute(
            "SELECT * FROM rotation_steps WHERE group_id=? AND role='rotation_exit' ORDER BY sequence,id",
            (group_id,),
        ).fetchall()
        entries = conn.execute(
            "SELECT * FROM rotation_contingent_entries WHERE group_id=? ORDER BY candidate_key,id",
            (group_id,),
        ).fetchall()
        if not steps or len(entries) != 1:
            raise RuntimeError("rotation display workflow is incomplete")
        exits: list[dict[str, Any]] = []
        for step in steps:
            try:
                payload = json.loads(step["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("rotation exit display payload is invalid") from exc
            exits.append({
                "sequence": int(step["sequence"]),
                "step_id": str(step["id"]),
                "proposal_id": str(step["proposal_id"] or ""),
                "symbol": str(step["symbol"] or "").upper(),
                "quantity": float(step["requested_quantity"]),
                "position_lifecycle_id": str(payload.get("position_lifecycle_id") or ""),
                "reason": str(step["reason"] or payload.get("reason") or ""),
                "role": str(step["role"]),
            })
        entry = dict(entries[0])
        workflow = {
            "workflow_structure": "ordered_exit_legs_then_fill_reconciliation_then_one_contingent_entry",
            "exits": exits,
            "contingent_entry": {
                "entry_id": str(entry["id"]),
                "candidate_key": str(entry["candidate_key"]),
                "proposal_id": str(entry.get("proposal_id") or ""),
                "symbol": str(entry["symbol"]).upper(),
                "strategy_version": str(entry["strategy_version"]),
                "displayed_max_quantity": float(entry["displayed_max_quantity"]),
                "displayed_max_notional": float(entry["displayed_max_notional"]),
                "displayed_max_stop_risk": float(entry["displayed_max_stop_risk"]),
            },
        }
        return {
            "display_schema_version": "rotation_group_display_v1",
            "telegram_message_id": str(telegram_message_id),
            "rotation_group_id": str(group["id"]),
            "origin_run_id": str(group.get("origin_run_id") or group.get("run_id") or ""),
            "registry_snapshot_id": str(group.get("registry_snapshot_id") or ""),
            "allocation_id": str(group.get("allocation_id") or ""),
            "config_hash": str(group.get("config_hash") or ""),
            "schema_version": str(group.get("schema_version") or ""),
            "formula_version": str(group.get("formula_version") or ""),
            "workflow_structure_fingerprint": str(group.get("workflow_structure_fingerprint") or ""),
            "expires_at": str(group["expires_at"]),
            "workflow": workflow,
        }

    def record_group_display(self, group_id: str, telegram_message_id: str) -> dict[str, Any]:
        """Persist the one immutable grouped approval surface after Telegram succeeds."""
        if not str(telegram_message_id or "").strip():
            raise ValueError("rotation group display requires the Telegram message identity")
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT state FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if group is None or str(group["state"]) != RotationState.PENDING_GROUP_APPROVAL.value:
                raise RuntimeError("only a pending rotation group may be displayed")
            envelope = self._group_display_envelope(conn, group_id, str(telegram_message_id))
            display_fingerprint = _fingerprint(envelope)
            workflow_fingerprint = _fingerprint(envelope["workflow"])
            existing = conn.execute(
                "SELECT * FROM rotation_group_display_envelopes WHERE group_id=?", (group_id,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["telegram_message_id"]) != str(telegram_message_id)
                    or str(existing["display_fingerprint"]) != display_fingerprint
                    or str(existing["workflow_fingerprint"]) != workflow_fingerprint
                ):
                    raise RuntimeError("rotation group was already displayed with different immutable terms")
                return dict(existing)
            display_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO rotation_group_display_envelopes(
                       id,group_id,telegram_message_id,displayed_at,expires_at,envelope_json,
                       display_fingerprint,workflow_fingerprint,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    display_id, group_id, str(telegram_message_id), now, envelope["expires_at"],
                    json_dumps(envelope), display_fingerprint, workflow_fingerprint, now,
                ),
            )
            return dict(conn.execute(
                "SELECT * FROM rotation_group_display_envelopes WHERE id=?", (display_id,)
            ).fetchone())

    def approve(
        self,
        group_id: str,
        *,
        approval_id: str,
        sender_id: str,
        command: str,
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        group = self.get_group(group_id)
        if _utc(group["expires_at"]) <= datetime.now(UTC):
            return self.transition(group_id, RotationState.EXPIRED, reason="group expired before approval")
        entries = self.entries(group_id)
        try:
            self._verify_authority_records(
                group,
                registry_snapshot_id=str(group.get("registry_snapshot_id") or ""),
                allocation_id=str(group.get("allocation_id") or ""),
                evaluation_time=datetime.now(UTC),
            )
        except (RuntimeError, ValueError) as exc:
            self.transition(group_id, RotationState.CANCELLED, reason=f"rotation authority blocked: {exc}")
            raise RuntimeError(f"rotation approval blocked: {exc}") from exc
        ceiling_fingerprint = _fingerprint([
            (row["candidate_key"], row["displayed_max_quantity"], row["displayed_max_notional"], row["displayed_max_stop_risk"])
            for row in entries
        ])
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if not current or current["state"] != RotationState.PENDING_GROUP_APPROVAL.value:
                raise RuntimeError("rotation group is not pending approval")
            display = conn.execute(
                "SELECT * FROM rotation_group_display_envelopes WHERE group_id=?", (group_id,)
            ).fetchone()
            if display is None and os.getenv("TRADING_AGENT_TESTING") == "1":
                synthetic_message_id = str(reply_to_message_id or f"test-rotation:{group_id}")
                envelope = self._group_display_envelope(conn, group_id, synthetic_message_id)
                display_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO rotation_group_display_envelopes(
                           id,group_id,telegram_message_id,displayed_at,expires_at,envelope_json,
                           display_fingerprint,workflow_fingerprint,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        display_id, group_id, synthetic_message_id, now, envelope["expires_at"],
                        json_dumps(envelope), _fingerprint(envelope), _fingerprint(envelope["workflow"]), now,
                    ),
                )
                display = conn.execute(
                    "SELECT * FROM rotation_group_display_envelopes WHERE id=?", (display_id,)
                ).fetchone()
            if display is None:
                raise RuntimeError("rotation group has no immutable displayed approval envelope")
            if str(reply_to_message_id or "") != str(display["telegram_message_id"]):
                if not (os.getenv("TRADING_AGENT_TESTING") == "1" and reply_to_message_id is None):
                    raise RuntimeError("rotation approval reply does not target the displayed group message")
            try:
                displayed_envelope = json.loads(display["envelope_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("rotation group display envelope is invalid") from exc
            current_envelope = self._group_display_envelope(
                conn, group_id, str(display["telegram_message_id"])
            )
            display_fingerprint = _fingerprint(current_envelope)
            workflow_fingerprint = _fingerprint(current_envelope["workflow"])
            if (
                displayed_envelope != current_envelope
                or str(display["display_fingerprint"]) != display_fingerprint
                or str(display["workflow_fingerprint"]) != workflow_fingerprint
                or _utc(str(display["expires_at"])) <= datetime.now(UTC)
            ):
                raise RuntimeError("rotation workflow changed or expired after display")
            conn.execute(
                """INSERT INTO rotation_group_approvals(
                     id,group_id,approval_id,sender_id,command,ceiling_fingerprint,status,created_at,
                     display_envelope_id,display_fingerprint,workflow_fingerprint,telegram_message_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), group_id, approval_id, str(sender_id), command,
                    ceiling_fingerprint, "active", now, display["id"], display_fingerprint,
                    workflow_fingerprint, display["telegram_message_id"],
                ),
            )
            conn.execute(
                "UPDATE rotation_groups SET state=?,approval_id=?,approved_at=?,updated_at=? WHERE id=?",
                (RotationState.APPROVED_EXIT_PENDING.value, approval_id, now, now, group_id),
            )
            self._event(conn, group_id, f"approval:{approval_id}", "group_approved",
                        RotationState.PENDING_GROUP_APPROVAL, RotationState.APPROVED_EXIT_PENDING,
                        {"explicit_group_command": True, "entry_ceiling_fingerprint": ceiling_fingerprint})
        return self.get_group(group_id)

    def reject(self, group_id: str, *, reason: str = "explicit group rejection") -> dict[str, Any]:
        return self.transition(group_id, RotationState.REJECTED, reason=reason)

    def approval_is_current(self, group_id: str) -> bool:
        try:
            with self.storage.connect() as conn:
                row = conn.execute(
                    """SELECT a.*,g.approval_id AS group_approval_id,g.expires_at,
                              d.envelope_json,d.display_fingerprint AS persisted_display_fingerprint,
                              d.workflow_fingerprint AS persisted_workflow_fingerprint,
                              d.telegram_message_id AS persisted_telegram_message_id
                       FROM rotation_group_approvals a
                       JOIN rotation_groups g ON g.id=a.group_id
                       JOIN rotation_group_display_envelopes d ON d.id=a.display_envelope_id
                       WHERE a.group_id=? ORDER BY a.created_at DESC LIMIT 1""",
                    (group_id,),
                ).fetchone()
                if row is None:
                    return False
                current = self._group_display_envelope(
                    conn, group_id, str(row["persisted_telegram_message_id"])
                )
                display_fingerprint = _fingerprint(current)
                workflow_fingerprint = _fingerprint(current["workflow"])
                displayed = json.loads(row["envelope_json"] or "{}")
                return bool(
                    row["approval_id"] == row["group_approval_id"]
                    and displayed == current
                    and row["display_fingerprint"] == display_fingerprint
                    and row["workflow_fingerprint"] == workflow_fingerprint
                    and row["persisted_display_fingerprint"] == display_fingerprint
                    and row["persisted_workflow_fingerprint"] == workflow_fingerprint
                    and row["telegram_message_id"] == row["persisted_telegram_message_id"]
                    and row["status"] in {"active", "exit_submitted"}
                    and row["consumed_at"]
                    and _utc(str(row["expires_at"])) > datetime.now(UTC)
                )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
            return False

    def record_exit_submitted(self, group_id: str, *, step_id: str, intent_id: str) -> dict[str, Any]:
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise KeyError(group_id)
            current = RotationState(group["state"])
            if current in {RotationState.EXIT_SUBMITTED, RotationState.EXIT_PARTIALLY_FILLED}:
                linked = conn.execute(
                    "SELECT intent_id FROM rotation_steps WHERE id=? AND group_id=? AND role='rotation_exit'",
                    (step_id, group_id),
                ).fetchone()
                if not linked:
                    raise RuntimeError("rotation exit step not found")
                if linked["intent_id"] is not None and linked["intent_id"] != intent_id:
                    raise RuntimeError("rotation exit step already belongs to another intent")
                if linked["intent_id"] == intent_id:
                    return dict(group)
                conn.execute(
                    "UPDATE rotation_steps SET intent_id=?,state='submitted',updated_at=? WHERE id=?",
                    (intent_id, now, step_id),
                )
                self._event(conn, group_id, f"exit-submitted:{intent_id}", "exit_submitted",
                            current, current, {"intent_id": intent_id, "additional_exit_leg": True})
                return dict(group)
            self._require_transition(current, RotationState.EXIT_SUBMITTED)
            changed = conn.execute(
                "UPDATE rotation_steps SET intent_id=?,state='submitted',updated_at=? WHERE id=? AND group_id=? AND role='rotation_exit'",
                (intent_id, now, step_id, group_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("rotation exit step not found")
            conn.execute("UPDATE rotation_groups SET state=?,updated_at=? WHERE id=?",
                         (RotationState.EXIT_SUBMITTED.value, now, group_id))
            self._event(conn, group_id, f"exit-submitted:{intent_id}", "exit_submitted", current,
                        RotationState.EXIT_SUBMITTED, {"intent_id": intent_id})
        return self.get_group(group_id)

    def record_exit_fill(
        self,
        group_id: str,
        *,
        intent_id: str,
        cumulative_quantity: float,
        cumulative_notional: float,
        released_risk: float,
        exit_complete: bool,
    ) -> dict[str, Any]:
        values = (float(cumulative_quantity), float(cumulative_notional), float(released_risk))
        if any(not math.isfinite(value) or value < 0 for value in values) or cumulative_quantity <= 0:
            raise ValueError("authoritative cumulative fill values must be finite and non-negative")
        now = iso_now()
        target = RotationState.EXIT_FILLED if exit_complete else RotationState.EXIT_PARTIALLY_FILLED
        event_key = f"exit-fill:{intent_id}:{cumulative_quantity:.12f}:{cumulative_notional:.8f}"
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            step = conn.execute(
                "SELECT * FROM rotation_steps WHERE group_id=? AND intent_id=? AND role='rotation_exit'",
                (group_id, intent_id),
            ).fetchone()
            if not group or not step:
                raise RuntimeError("rotation exit intent linkage is missing")
            if cumulative_quantity + 1e-12 < float(step["filled_quantity"] or 0) or cumulative_notional + 1e-9 < float(step["filled_notional"] or 0):
                raise RuntimeError("cumulative exit fill cannot move backward")
            current = RotationState(group["state"])
            exit_dependency_active = current in {
                RotationState.EXIT_SUBMITTED,
                RotationState.EXIT_PARTIALLY_FILLED,
            }
            if exit_dependency_active:
                self._require_transition(current, target)
            conn.execute(
                """UPDATE rotation_steps SET state=?,filled_quantity=?,filled_notional=?,released_risk=?,updated_at=?
                   WHERE id=?""",
                ("filled" if exit_complete else "partially_filled", cumulative_quantity, cumulative_notional,
                 released_risk, now, step["id"]),
            )
            remaining = conn.execute(
                "SELECT COUNT(*) FROM rotation_steps WHERE group_id=? AND role='rotation_exit' AND state!='filled'",
                (group_id,),
            ).fetchone()[0]
            target = RotationState.EXIT_FILLED if int(remaining) == 0 else RotationState.EXIT_PARTIALLY_FILLED
            if exit_dependency_active:
                self._require_transition(current, target)
            totals = conn.execute(
                "SELECT COALESCE(SUM(filled_notional),0),COALESCE(SUM(released_risk),0) FROM rotation_steps WHERE group_id=? AND role='rotation_exit'",
                (group_id,),
            ).fetchone()
            persisted_state = target if exit_dependency_active else current
            conn.execute(
                "UPDATE rotation_groups SET state=?,actual_released_notional=?,actual_released_risk=?,updated_at=? WHERE id=?",
                (persisted_state.value, float(totals[0]), float(totals[1]), now, group_id),
            )
            event_type = (
                "exit_filled" if target == RotationState.EXIT_FILLED else "exit_partially_filled"
            ) if exit_dependency_active else "late_exit_fill_reconciled"
            self._event(conn, group_id, event_key, event_type,
                        current, persisted_state, {"intent_id": intent_id, "actual_capacity_only": True,
                                                   "additional_capacity_not_reused": not exit_dependency_active,
                                                   "cumulative_quantity": cumulative_quantity,
                                                   "cumulative_notional": cumulative_notional,
                                                   "released_risk": released_risk})
        return self.get_group(group_id)

    def record_exit_terminal(
        self, group_id: str, *, intent_id: str, terminal_state: str
    ) -> dict[str, Any]:
        """Quiesce a terminal exit leg after all known partial fills are accounted.

        A later increase in the linked intent's cumulative fill still makes the
        terminal group recoverable, so late broker evidence can be reconciled
        without reviving or enlarging the dependency decision.
        """
        terminal_state = str(terminal_state).lower()
        if terminal_state not in {"cancelled", "rejected", "expired"}:
            raise ValueError("rotation exit terminal state is unsupported")
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute(
                "SELECT * FROM rotation_groups WHERE id=?", (group_id,)
            ).fetchone()
            step = conn.execute(
                """SELECT * FROM rotation_steps
                   WHERE group_id=? AND intent_id=? AND role='rotation_exit'""",
                (group_id, intent_id),
            ).fetchone()
            if not group or not step:
                raise RuntimeError("rotation exit intent linkage is missing")
            state = f"terminal_{terminal_state}"
            conn.execute(
                "UPDATE rotation_steps SET state=?,reason=?,updated_at=? WHERE id=? AND state!='filled'",
                (state, f"linked intent {terminal_state}", now, step["id"]),
            )
            self._event(
                conn, group_id, f"exit-terminal:{intent_id}:{terminal_state}",
                "exit_terminal_reconciled", RotationState(group["state"]),
                RotationState(group["state"]),
                {"intent_id": intent_id, "terminal_state": terminal_state,
                 "known_partial_fill_accounted": float(step["filled_quantity"] or 0.0)},
            )
        return self.get_group(group_id)

    def begin_reconciliation(self, group_id: str) -> dict[str, Any]:
        return self.transition(group_id, RotationState.RECONCILIATION_PENDING,
                               reason="authoritative exit fill requires account and position reconciliation")

    def record_reconciliation(
        self,
        group_id: str,
        *,
        cash: float,
        buying_power: float,
        snapshot_fingerprint: str,
    ) -> dict[str, Any]:
        if not snapshot_fingerprint or any(not math.isfinite(float(value)) or float(value) < 0 for value in (cash, buying_power)):
            raise ValueError("reconciliation requires finite account capacity and a snapshot fingerprint")
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise KeyError(group_id)
            current = RotationState(group["state"])
            self._require_transition(current, RotationState.RECONCILED)
            if float(group["actual_released_notional"] or 0) <= 0:
                raise RuntimeError("reconciliation cannot authorize capacity without an actual exit fill")
            conn.execute(
                """UPDATE rotation_groups SET state=?,reconciled_cash=?,reconciled_buying_power=?,
                   reconciliation_fingerprint=?,updated_at=? WHERE id=?""",
                (RotationState.RECONCILED.value, float(cash), float(buying_power), snapshot_fingerprint, now, group_id),
            )
            self._event(conn, group_id, f"reconciled:{snapshot_fingerprint}", "reconciliation_complete",
                        current, RotationState.RECONCILED, {"actual_fill_required": True})
        return self.get_group(group_id)

    def revalidate_entry(
        self,
        group_id: str,
        contingent_entry_id: str,
        *,
        candidate_key: str,
        price: float,
        requested_quantity: float,
        stop_risk_per_share: float,
        allocation_notional_cap: float,
        allocation_risk_cap: float,
        other_available_cash: float,
        minimum_notional: float,
        registry_snapshot_id: str,
        allocation_id: str,
        evaluation_time: str | datetime | None = None,
    ) -> RevalidatedRotationEntry:
        numbers = (price, requested_quantity, stop_risk_per_share, allocation_notional_cap,
                   allocation_risk_cap, other_available_cash, minimum_notional)
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in numbers) or price <= 0:
            raise ValueError("rotation entry revalidation inputs must be finite and non-negative")
        group = self.get_group(group_id)
        if RotationState(group["state"]) != RotationState.RECONCILED:
            raise RuntimeError("contingent entry cannot revalidate before fill reconciliation")
        rows = [row for row in self.entries(group_id) if row["id"] == contingent_entry_id]
        if len(rows) != 1:
            raise KeyError(contingent_entry_id)
        entry = rows[0]
        if _utc(entry["expires_at"]) <= datetime.now(UTC):
            self.transition(group_id, RotationState.EXPIRED, reason="contingent entry expired")
            return RevalidatedRotationEntry(False, group_id, contingent_entry_id, entry["symbol"], 0, 0, 0,
                                            "expiry", "contingent entry expired")
        if candidate_key != entry["candidate_key"]:
            self.transition(group_id, RotationState.ENTRY_BLOCKED, reason="contingent candidate changed materially")
            return RevalidatedRotationEntry(False, group_id, contingent_entry_id, entry["symbol"], 0, 0, 0,
                "candidate_identity", "candidate changed; new approval required")
        try:
            authority_run_id = self._verify_authority_records(
                group,
                registry_snapshot_id=registry_snapshot_id,
                allocation_id=allocation_id,
                evaluation_time=evaluation_time or datetime.now(UTC),
                allow_later_run=True,
            )
        except (RuntimeError, ValueError) as exc:
            self.transition(group_id, RotationState.ENTRY_BLOCKED, reason=f"rotation authority blocked: {exc}")
            raise
        displayed_qty = float(entry["displayed_max_quantity"])
        displayed_notional = float(entry["displayed_max_notional"])
        displayed_risk = float(entry["displayed_max_stop_risk"])
        caps = {
            "displayed_quantity": displayed_qty * price,
            "displayed_notional": displayed_notional,
            "actual_exit_release": float(group["actual_released_notional"] or 0),
            "reconciled_cash": float(group["reconciled_cash"] or 0),
            "reconciled_buying_power": float(group["reconciled_buying_power"] or 0),
            "other_available_cash": float(other_available_cash),
            "current_strategy_allocation": float(allocation_notional_cap),
            "requested_quantity": float(requested_quantity) * price,
        }
        binding_cap, final_notional = min(caps.items(), key=lambda item: (item[1], item[0]))
        risk_qty = displayed_qty
        if stop_risk_per_share > 0:
            risk_qty = min(risk_qty, displayed_risk / stop_risk_per_share, allocation_risk_cap / stop_risk_per_share)
        else:
            risk_qty = 0.0
        final_quantity = min(displayed_qty, requested_quantity, final_notional / price, risk_qty)
        final_notional = max(0.0, final_quantity * price)
        final_risk = max(0.0, final_quantity * stop_risk_per_share)
        if final_notional < minimum_notional or final_quantity <= 0:
            self.transition(group_id, RotationState.ENTRY_BLOCKED, reason="actual released capacity leaves no executable entry")
            return RevalidatedRotationEntry(False, group_id, contingent_entry_id, entry["symbol"], 0, 0, 0,
                                            binding_cap, "entry blocked after fresh post-fill allocation")
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT state FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if not current or current["state"] != RotationState.RECONCILED.value:
                raise RuntimeError("rotation group changed during entry revalidation")
            conn.execute(
                """UPDATE rotation_groups SET state=?,revalidation_run_id=?
                   ,revalidation_registry_snapshot_id=?,revalidation_allocation_id=?,revalidated_at=?,updated_at=?
                   WHERE id=?""",
                (RotationState.ENTRY_REVALIDATING.value, authority_run_id, registry_snapshot_id,
                 allocation_id, now, now, group_id),
            )
            conn.execute(
                """UPDATE rotation_contingent_entries SET state='revalidated',final_quantity=?,final_notional=?,
                   final_stop_risk=?,binding_cap=?,updated_at=? WHERE id=? AND state='contingent'""",
                (final_quantity, final_notional, final_risk, binding_cap, now, contingent_entry_id),
            )
            self._event(conn, group_id, f"entry-revalidated:{contingent_entry_id}:{allocation_id}",
                        (
                            "entry_reduced"
                            if final_quantity < displayed_qty - 1e-12
                            or final_notional < displayed_notional - 1e-9
                            or final_risk < displayed_risk - 1e-9
                            else "contingent_entry_revalidated"
                        ), RotationState.RECONCILED,
                        RotationState.ENTRY_REVALIDATING,
                        {"preserve_reduce_or_block_only": True, "binding_cap": binding_cap,
                         "final_quantity": final_quantity, "final_notional": final_notional,
                         "final_stop_risk": final_risk})
        return RevalidatedRotationEntry(True, group_id, contingent_entry_id, entry["symbol"], final_quantity,
                                        final_notional, final_risk, binding_cap,
                                        "fresh post-fill allocation passed without enlargement")

    def record_entry_reserved(self, group_id: str, contingent_entry_id: str, *, intent_id: str) -> dict[str, Any]:
        return self._entry_intent_transition(group_id, contingent_entry_id, intent_id,
                                             RotationState.ENTRY_RESERVED, "entry_reserved")

    def record_entry_submitted(self, group_id: str, contingent_entry_id: str, *, intent_id: str) -> dict[str, Any]:
        return self._entry_intent_transition(group_id, contingent_entry_id, intent_id,
                                             RotationState.ENTRY_SUBMITTED, "entry_submitted")

    def complete(self, group_id: str) -> dict[str, Any]:
        entries = self.entries(group_id)
        intent_ids = [str(row.get("intent_id")) for row in entries if row.get("intent_id")]
        if len(intent_ids) != 1:
            raise RuntimeError("rotation completion requires exactly one linked entry intent")
        rows = self.storage.fetch_all("SELECT state FROM order_intents WHERE id=?", (intent_ids[0],))
        if not rows or rows[0]["state"] != "filled":
            raise RuntimeError("rotation completion requires authoritative entry fill reconciliation")
        return self.transition(group_id, RotationState.COMPLETED, reason="rotation entry fill reconciled")

    def reset_entry_for_revalidation(self, group_id: str) -> dict[str, Any]:
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if not group or group["state"] != RotationState.ENTRY_REVALIDATING.value:
                raise RuntimeError("only an interrupted entry revalidation can be reset")
            linked = conn.execute(
                "SELECT COUNT(*) FROM rotation_contingent_entries WHERE group_id=? AND intent_id IS NOT NULL",
                (group_id,),
            ).fetchone()[0]
            if int(linked):
                raise RuntimeError("linked entry intent must be reconciled, not revalidated")
            conn.execute(
                """UPDATE rotation_contingent_entries SET state='contingent',final_quantity=NULL,
                   final_notional=NULL,final_stop_risk=NULL,binding_cap=NULL,updated_at=? WHERE group_id=?""",
                (now, group_id),
            )
            conn.execute(
                """UPDATE rotation_groups SET state=?,revalidation_run_id=NULL,
                   revalidation_registry_snapshot_id=NULL,revalidation_allocation_id=NULL,
                   revalidated_at=NULL,updated_at=? WHERE id=?""",
                (RotationState.RECONCILED.value, now, group_id),
            )
            self._event(conn, group_id, "entry-revalidation-recovered", "entry_revalidation_recovered",
                        RotationState.ENTRY_REVALIDATING, RotationState.RECONCILED,
                        {"fresh_revalidation_required": True})
        return self.get_group(group_id)

    def fail_exit(self, group_id: str, *, reason: str) -> dict[str, Any]:
        return self.transition(group_id, RotationState.EXIT_FAILED, reason=reason)

    def transition(self, group_id: str, target: RotationState, *, reason: str) -> dict[str, Any]:
        target = RotationState(target)
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                raise KeyError(group_id)
            current = RotationState(group["state"])
            if current == target:
                return dict(group)
            if current in TERMINAL_STATES:
                return dict(group)
            self._require_transition(current, target)
            terminal_reason = reason if target in TERMINAL_STATES else group["terminal_reason"]
            conn.execute("UPDATE rotation_groups SET state=?,terminal_reason=?,updated_at=? WHERE id=?",
                         (target.value, terminal_reason, now, group_id))
            if target in TERMINAL_STATES:
                conn.execute(
                    "UPDATE rotation_contingent_entries SET state='blocked',updated_at=? WHERE group_id=? AND state IN ('contingent','revalidated')",
                    (now, group_id),
                )
                proposal_status = {
                    RotationState.EXPIRED: "expired",
                    RotationState.REJECTED: "rejected",
                }.get(target, "blocked")
                conn.execute(
                    """UPDATE trade_proposals SET status=? WHERE id IN (
                           SELECT proposal_id FROM rotation_contingent_entries WHERE group_id=?
                       ) AND status IN ('pending','approved')""",
                    (proposal_status, group_id),
                )
            self._event(conn, group_id, f"transition:{current.value}:{target.value}:{_fingerprint(reason)[:12]}",
                        target.value, current, target, {"reason": reason})
        return self.get_group(group_id)

    def expire_stale(self, *, now: datetime | None = None) -> int:
        instant = (now or datetime.now(UTC)).isoformat()
        rows = self.storage.fetch_all(
            "SELECT id FROM rotation_groups WHERE expires_at<=? AND state IN (?,?,?,?,?)",
            (instant, RotationState.PENDING_GROUP_APPROVAL.value,
             RotationState.APPROVED_EXIT_PENDING.value, RotationState.EXIT_SUBMITTED.value,
             RotationState.EXIT_PARTIALLY_FILLED.value, RotationState.RECONCILED.value),
        )
        for row in rows:
            self.transition(row["id"], RotationState.EXPIRED, reason="rotation group or candidate expired")
        return len(rows)

    def claim_notification(self, group_id: str, event_key: str) -> bool:
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE rotation_events SET notification_claimed_at=?
                   WHERE group_id=? AND event_key=? AND notification_claimed_at IS NULL AND notification_sent_at IS NULL""",
                (now, group_id, event_key),
            )
            return changed.rowcount == 1

    def mark_notification_sent(self, group_id: str, event_key: str) -> None:
        self.storage.execute(
            "UPDATE rotation_events SET notification_sent_at=? WHERE group_id=? AND event_key=? AND notification_sent_at IS NULL",
            (iso_now(), group_id, event_key),
        )

    def release_notification_claim(self, group_id: str, event_key: str) -> None:
        self.storage.execute(
            """UPDATE rotation_events SET notification_claimed_at=NULL
               WHERE group_id=? AND event_key=? AND notification_sent_at IS NULL""",
            (group_id, event_key),
        )

    def recovery_actions(self) -> list[dict[str, Any]]:
        terminal = tuple(state.value for state in sorted(TERMINAL_STATES, key=lambda value: value.value))
        placeholders = ",".join("?" for _ in terminal)
        rows = self.storage.fetch_all(
            f"""SELECT * FROM rotation_groups g
                WHERE g.state NOT IN ({placeholders})
                    OR EXISTS (
                       SELECT 1 FROM rotation_steps s
                       LEFT JOIN order_intents i ON i.id=s.intent_id
                       WHERE s.group_id=g.id AND s.role='rotation_exit'
                         AND s.intent_id IS NOT NULL
                         AND (
                           s.state NOT IN ('filled','terminal_cancelled','terminal_rejected','terminal_expired')
                           OR COALESCE(i.filled_quantity,0)>COALESCE(s.filled_quantity,0)+0.000000000001
                         )
                   )
                ORDER BY g.created_at,g.id""",
            terminal,
        )
        actions: list[dict[str, Any]] = []
        for row in rows:
            state = RotationState(row["state"])
            if state in {RotationState.EXIT_SUBMITTED, RotationState.EXIT_PARTIALLY_FILLED, RotationState.EXIT_FILLED}:
                action = "reconcile_exit_only"
            elif state == RotationState.RECONCILIATION_PENDING:
                action = "finish_reconciliation"
            elif state in {RotationState.RECONCILED, RotationState.ENTRY_REVALIDATING}:
                action = "revalidate_contingent_entry"
            elif state in {RotationState.ENTRY_RESERVED, RotationState.ENTRY_SUBMITTED}:
                action = "reconcile_entry_only"
            else:
                action = "await_manual_or_exit_action"
            actions.append({"group_id": row["id"], "state": state.value, "action": action,
                            "broker_submission_allowed": False})
        return actions

    def get_group(self, group_id: str) -> dict[str, Any]:
        rows = self.storage.fetch_all("SELECT * FROM rotation_groups WHERE id=?", (group_id,))
        if not rows:
            raise KeyError(group_id)
        return rows[0]

    def entries(self, group_id: str) -> list[dict[str, Any]]:
        return self.storage.fetch_all(
            "SELECT * FROM rotation_contingent_entries WHERE group_id=? ORDER BY id", (group_id,)
        )

    def steps(self, group_id: str) -> list[dict[str, Any]]:
        return self.storage.fetch_all(
            "SELECT * FROM rotation_steps WHERE group_id=? ORDER BY sequence,role,id", (group_id,)
        )

    def _entry_intent_transition(
        self,
        group_id: str,
        contingent_entry_id: str,
        intent_id: str,
        target: RotationState,
        event_type: str,
    ) -> dict[str, Any]:
        now = iso_now()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute("SELECT * FROM rotation_groups WHERE id=?", (group_id,)).fetchone()
            entry = conn.execute(
                "SELECT * FROM rotation_contingent_entries WHERE id=? AND group_id=?",
                (contingent_entry_id, group_id),
            ).fetchone()
            if not group or not entry:
                raise RuntimeError("rotation contingent entry linkage is missing")
            current = RotationState(group["state"])
            if current == target:
                if entry["intent_id"] == intent_id:
                    return dict(group)
                raise RuntimeError("rotation entry state already belongs to another intent")
            self._require_transition(current, target)
            if target == RotationState.ENTRY_RESERVED and entry["state"] != "revalidated":
                raise RuntimeError("entry cannot reserve before post-fill revalidation")
            conn.execute(
                "UPDATE rotation_contingent_entries SET state=?,intent_id=?,updated_at=? WHERE id=?",
                ("reserved" if target == RotationState.ENTRY_RESERVED else "submitted", intent_id, now,
                 contingent_entry_id),
            )
            conn.execute("UPDATE rotation_groups SET state=?,updated_at=? WHERE id=?", (target.value, now, group_id))
            self._event(conn, group_id, f"{event_type}:{intent_id}", event_type, current, target,
                        {"intent_id": intent_id, "capital_reservation_after_reconciliation": True})
        return self.get_group(group_id)

    @staticmethod
    def _require_transition(current: RotationState, target: RotationState) -> None:
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise RuntimeError(f"invalid rotation transition {current.value} -> {target.value}")

    @staticmethod
    def _event(
        conn: Any,
        group_id: str,
        event_key: str,
        event_type: str,
        from_state: RotationState | None,
        to_state: RotationState,
        detail: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO rotation_events(
                 id,group_id,event_key,event_type,from_state,to_state,safe_detail,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), group_id, event_key, event_type,
             from_state.value if from_state is not None else None, to_state.value,
             json_dumps(dict(detail)), iso_now()),
        )


__all__ = [
    "ROTATION_FORMULA_VERSION",
    "ROTATION_SCHEMA_VERSION",
    "RevalidatedRotationEntry",
    "RotationApprovalResult",
    "RotationCoordinator",
    "RotationState",
    "apply_rotation_schema",
    "parse_rotation_approval",
]
