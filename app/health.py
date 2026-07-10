from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .run_lock import inspect_lock
from .utils import PROJECT_ROOT, get_git_commit, iso_now, json_dumps


HEALTH_RANK = {"healthy": 0, "blocked": 1, "degraded": 2, "unknown": 3, "stale": 4, "failed": 5}


@dataclass(frozen=True)
class HealthReport:
    state: str
    components: dict[str, dict[str, Any]]
    generated_at: str
    commit: str


def record_heartbeat(
    storage: Any,
    component: str,
    state: str,
    *,
    attempted_at: str | None = None,
    completed_at: str | None = None,
    successful_at: str | None = None,
    blocked_reason: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    if state not in HEALTH_RANK:
        raise ValueError(f"invalid health state: {state}")
    now = iso_now()
    storage.execute(
        """INSERT INTO health_heartbeats(
               component,state,attempted_at,completed_at,successful_at,blocked_reason,detail,commit_sha,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET
           state=excluded.state,attempted_at=COALESCE(excluded.attempted_at,health_heartbeats.attempted_at),
           completed_at=COALESCE(excluded.completed_at,health_heartbeats.completed_at),
           successful_at=COALESCE(excluded.successful_at,health_heartbeats.successful_at),
           blocked_reason=excluded.blocked_reason,detail=excluded.detail,commit_sha=excluded.commit_sha,updated_at=excluded.updated_at""",
        (component, state, attempted_at, completed_at, successful_at, blocked_reason, json_dumps(detail or {}), get_git_commit(), now),
    )


class HealthMonitor:
    def __init__(self, storage: Any, config: dict[str, Any] | None = None, root: Path = PROJECT_ROOT) -> None:
        self.storage = storage
        self.config = config or {}
        self.root = root

    def report(self, now: datetime | None = None) -> HealthReport:
        now = now or datetime.now(UTC)
        thresholds = self.config.get("health", {})
        scanner_stale = float(thresholds.get("scanner_stale_seconds", 900))
        listener_stale = float(thresholds.get("listener_stale_seconds", 120))
        reconcile_stale = float(thresholds.get("reconciliation_stale_seconds", 900))
        components: dict[str, dict[str, Any]] = {}

        try:
            self.storage.fetch_all("SELECT 1")
            components["database"] = {"state": "healthy", "reason": "read probe succeeded"}
        except Exception as exc:
            components["database"] = {"state": "failed", "reason": f"read probe failed: {type(exc).__name__}"}
            return HealthReport("failed", components, now.isoformat(), get_git_commit())

        heartbeats = {row["component"]: row for row in self.storage.fetch_all("SELECT * FROM health_heartbeats")}
        components["scanner"] = self._heartbeat_state(heartbeats.get("scanner"), scanner_stale, now, "scanner has no measured heartbeat")
        components["listener"] = self._heartbeat_state(heartbeats.get("listener_poll"), listener_stale, now, "listener has no measured polling heartbeat")
        components["reconciliation"] = self._heartbeat_state(heartbeats.get("reconciliation"), reconcile_stale, now, "reconciliation has no measured heartbeat")

        unknown = self._count("SELECT COUNT(*) n FROM order_intents WHERE state IN ('unknown','reconciliation_required')")
        components["unknown_orders"] = {
            "state": "degraded" if unknown else "healthy",
            "count": unknown,
            "reason": "unresolved broker outcome" if unknown else "none",
        }
        stale_partial = self._count("SELECT COUNT(*) n FROM order_intents WHERE state='partially_filled' AND (julianday('now')-julianday(updated_at))*86400>300")
        components["partial_fills"] = {"state": "degraded" if stale_partial else "healthy", "count": stale_partial, "reason": "stale partial fill" if stale_partial else "none"}
        recovery = self._count("SELECT COUNT(*) n FROM telegram_updates WHERE processing_state='received'") + self._count("SELECT COUNT(*) n FROM approval_workflows WHERE state='manual_review'")
        components["recovery"] = {"state": "degraded" if recovery else "healthy", "count": recovery, "reason": "unfinished approval/update workflow" if recovery else "none"}
        reservations = self.storage.fetch_all("SELECT COUNT(*) n,MIN(created_at) oldest FROM risk_reservations WHERE state='active'")[0]
        components["reservations"] = {"state": "healthy", "count": int(reservations["n"]), "oldest": reservations.get("oldest"), "reason": "active capacity reservations" if reservations["n"] else "none"}

        components["scanner_lock"] = self._lock_state(self.root / "logs/runtime/agent.lockdir")
        components["listener_lock"] = self._lock_state(self.root / "logs/runtime/listener.lockdir")
        overall = max((item["state"] for item in components.values()), key=lambda state: HEALTH_RANK.get(state, HEALTH_RANK["unknown"]))
        return HealthReport(overall, components, now.isoformat(), get_git_commit())

    def format_status(self) -> str:
        report = self.report()
        lines = [
            f"Status: {report.state.capitalize()}",
            f"Mode: {self.config.get('mode', 'unknown')}",
            f"Live Enabled: {self.config.get('live_enabled', False)}",
            f"Auto Execution: {self.config.get('auto_execution_enabled', False)}",
            f"HEAD Commit: {report.commit[:7] if report.commit else 'unknown'}",
        ]
        labels = {
            "scanner": "Scanner",
            "listener": "Listener polling",
            "reconciliation": "Reconciliation",
            "database": "Database",
            "scanner_lock": "Scanner lock",
            "listener_lock": "Listener lock",
            "unknown_orders": "Unknown orders",
            "partial_fills": "Stale partial fills",
            "recovery": "Pending recovery",
            "reservations": "Active reservations",
        }
        for key, label in labels.items():
            item = report.components.get(key, {"state": "unknown", "reason": "not measured"})
            count = f" ({item['count']})" if "count" in item else ""
            lines.append(f"{label}: {item['state']}{count} — {item.get('reason', '')}")
        return "\n".join(lines) + "\n"

    def _count(self, sql: str) -> int:
        return int(self.storage.fetch_all(sql)[0]["n"])

    def _heartbeat_state(self, row: dict[str, Any] | None, stale_seconds: float, now: datetime, missing_reason: str) -> dict[str, Any]:
        if not row:
            return {"state": "unknown", "reason": missing_reason}
        updated = _parse(row.get("updated_at"))
        age = (now - updated).total_seconds() if updated else float("inf")
        if age > stale_seconds:
            return {"state": "stale", "age_seconds": age, "reason": f"heartbeat older than {stale_seconds:.0f}s"}
        return {"state": row["state"], "age_seconds": age, "reason": row.get("blocked_reason") or "measured heartbeat"}

    def _lock_state(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"state": "healthy", "reason": "no lock present"}
        result = inspect_lock(path)
        state = {"active": "healthy", "stale": "stale", "recent_unknown": "unknown", "missing": "healthy"}[result.state]
        return {"state": state, "age_seconds": result.age_seconds, "reason": result.reason}


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None
