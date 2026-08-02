#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import yaml


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from app.run_lock import LockInspection, inspect_lock  # noqa: E402


DEFAULT_STATE_ROOT = Path.home() / "Library" / "Application Support" / "TradingAgent"
DEFAULT_RUNTIME_LINK = Path.home() / "TradingAgentRuntime"
DEFAULT_SCANNER_STALE_SECONDS = 900.0
DEFAULT_LISTENER_STALE_SECONDS = 120.0
REQUIRED_PYTHON = "3.13.9"
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
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
ALLOWED_SCANNER_STATES = {"healthy", "blocked"}


class FreshnessError(RuntimeError):
    pass


def runtime_state_root() -> Path:
    configured = os.getenv("TRADING_AGENT_STATE_ROOT")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else DEFAULT_STATE_ROOT.resolve()
    )


def check_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FreshnessError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FreshnessError(f"{label} is malformed") from exc
    if parsed.tzinfo is None:
        raise FreshnessError(f"{label} lacks a timezone")
    return parsed.astimezone(UTC)


def _age_seconds(value: Any, label: str, now: datetime) -> float:
    parsed = _parse_time(value, label)
    age = (now - parsed).total_seconds()
    if age < -5:
        raise FreshnessError(f"{label} is in the future")
    return max(0.0, age)


def _read_json_file(path: Path, *, owner_only: bool = False) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FreshnessError(f"{path.name} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise FreshnessError(f"{path.name} must be a regular non-symlink file")
    if metadata.st_uid != os.geteuid():
        raise FreshnessError(f"{path.name} is not owned by the runtime user")
    if owner_only and metadata.st_mode & 0o077:
        raise FreshnessError(f"{path.name} permissions are not owner-only")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FreshnessError(f"{path.name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FreshnessError(f"{path.name} must contain a JSON object")
    return value


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FreshnessError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise FreshnessError(f"{label} must be a positive finite number")
    return number


def _load_thresholds(release_root: Path) -> tuple[float, float]:
    config_path = release_root / "config" / "config.yaml"
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FreshnessError("release health configuration is unavailable") from exc
    if not isinstance(value, dict):
        raise FreshnessError("release configuration is not a mapping")
    health = value.get("health") or {}
    if not isinstance(health, dict):
        raise FreshnessError("release health configuration is not a mapping")
    return (
        _positive_number(
            health.get("scanner_stale_seconds", DEFAULT_SCANNER_STALE_SECONDS),
            "scanner stale threshold",
        ),
        _positive_number(
            health.get("listener_stale_seconds", DEFAULT_LISTENER_STALE_SECONDS),
            "listener stale threshold",
        ),
    )


def _load_runtime_authority(runtime_link: Path) -> tuple[Path, dict[str, Any]]:
    try:
        link_metadata = runtime_link.lstat()
    except OSError as exc:
        raise FreshnessError("active runtime pointer is unavailable") from exc
    if not stat.S_ISLNK(link_metadata.st_mode):
        raise FreshnessError("active runtime pointer must be a symbolic link")
    if link_metadata.st_uid != os.geteuid():
        raise FreshnessError("active runtime pointer is not owned by the runtime user")
    try:
        release_root = runtime_link.resolve(strict=True)
    except OSError as exc:
        raise FreshnessError("active runtime pointer is broken") from exc
    if not release_root.is_dir():
        raise FreshnessError("active runtime pointer does not resolve to a directory")

    manifest = _read_json_file(release_root / "release-manifest.json")
    commit = str(manifest.get("release_commit") or "")
    release_id = str(manifest.get("release_id") or "")
    tree_sha = str(manifest.get("git_tree_sha") or "")
    source_digest = str(manifest.get("tracked_source_inventory_digest") or "")
    authority = (
        manifest.get("release_authority")
        if isinstance(manifest.get("release_authority"), dict)
        else {}
    )
    ci = manifest.get("ci") if isinstance(manifest.get("ci"), dict) else {}
    run_id = ci.get("run_id")
    formulas = manifest.get("formula_versions")

    if not release_id:
        raise FreshnessError("release manifest has no release ID")
    if release_root.name != release_id:
        raise FreshnessError("release directory does not match the manifest release ID")
    if not SHA1_RE.fullmatch(commit):
        raise FreshnessError("release manifest commit is malformed")
    if manifest.get("mode") != "paper":
        raise FreshnessError("active release is not paper-only")
    if manifest.get("manual_approval_only") is not True:
        raise FreshnessError("active release does not require manual approval")
    if manifest.get("live_capability") is not False:
        raise FreshnessError("active release permits live capability")
    if manifest.get("tests_verified") is not True:
        raise FreshnessError("active release artifact tests are not verified")
    if manifest.get("python_version") != REQUIRED_PYTHON:
        raise FreshnessError("active release Python identity is incompatible")
    if platform.python_version() != REQUIRED_PYTHON:
        raise FreshnessError("freshness check is not using the release Python")
    for field in REQUIRED_ARTIFACT_EVIDENCE_HASHES:
        if not SHA256_RE.fullmatch(str(manifest.get(field) or "").lower()):
            raise FreshnessError(f"active release artifact evidence hash is missing or invalid: {field}")
    if not str(manifest.get("release_wheel_filename") or "").strip():
        raise FreshnessError("active release wheel filename evidence is missing")
    if not str(manifest.get("distribution_name") or "").strip() or not str(manifest.get("distribution_version") or "").strip():
        raise FreshnessError("active release distribution identity evidence is missing")
    if not str(manifest.get("schema_version") or ""):
        raise FreshnessError("active release schema identity is missing")
    if not manifest.get("required_schema_versions"):
        raise FreshnessError("active release required schema identities are missing")
    if not isinstance(formulas, dict) or not formulas:
        raise FreshnessError("active release formula identities are missing")
    if not SHA256_RE.fullmatch(str(manifest.get("configuration_hash") or "")):
        raise FreshnessError("active release configuration identity is malformed")
    if (
        not SHA1_RE.fullmatch(tree_sha)
        or authority.get("source_tree_sha") != tree_sha
        or not SHA256_RE.fullmatch(source_digest)
        or authority.get("tracked_source_inventory_digest") != source_digest
        or authority.get("mode") not in {"forward", "rollback"}
    ):
        raise FreshnessError("active release source-tree authority is invalid")
    if (
        ci.get("workflow_name") != "CI"
        or ci.get("head_sha") != commit
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
    ):
        raise FreshnessError("active release CI identity is invalid")
    return release_root, manifest


def _load_identity(
    path: Path,
    *,
    role: str,
    release_root: Path,
    commit: str,
    now: datetime,
) -> tuple[dict[str, Any], float]:
    identity = _read_json_file(path, owner_only=True)
    if identity.get("role") != role:
        raise FreshnessError(f"{role} identity role is invalid")
    if not isinstance(identity.get("run_id"), str) or not identity["run_id"].strip():
        raise FreshnessError(f"{role} identity run ID is missing")
    pid = identity.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise FreshnessError(f"{role} identity PID is invalid")
    project_root = identity.get("project_root")
    if (
        not isinstance(project_root, str)
        or not project_root.strip()
        or not Path(project_root).is_absolute()
    ):
        raise FreshnessError(f"{role} identity project root is invalid")
    try:
        identity_root = Path(project_root).resolve(strict=True)
    except OSError as exc:
        raise FreshnessError(f"{role} identity project root is unavailable") from exc
    if identity_root != release_root:
        raise FreshnessError(f"{role} identity project root does not match the active release")
    if identity.get("commit") != commit:
        raise FreshnessError(f"{role} identity commit does not match the active release")
    if identity.get("git_clean") is not True:
        raise FreshnessError(f"{role} identity does not attest a clean release")
    age = _age_seconds(identity.get("start_time"), f"{role} identity start time", now)
    return identity, age


def _read_heartbeats(database: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    try:
        metadata = database.lstat()
    except OSError as exc:
        raise FreshnessError("runtime database is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or database.is_symlink():
        raise FreshnessError("runtime database must be a regular non-symlink file")
    if metadata.st_uid != os.geteuid():
        raise FreshnessError("runtime database is not owned by the runtime user")
    database = database.resolve()
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            quick_check = str(connection.execute("PRAGMA quick_check(1)").fetchone()[0])
            rows = connection.execute(
                """SELECT component,state,attempted_at,completed_at,successful_at,
                          blocked_reason,detail,commit_sha,updated_at
                   FROM health_heartbeats
                   WHERE component IN ('scanner','listener_poll')"""
            ).fetchall()
    except sqlite3.Error as exc:
        raise FreshnessError("runtime database heartbeat probe failed") from exc
    if quick_check != "ok":
        raise FreshnessError("runtime database quick_check failed")
    return quick_check, {str(row["component"]): dict(row) for row in rows}


def _validate_heartbeat(
    row: dict[str, Any] | None,
    *,
    component: str,
    allowed_states: set[str],
    max_age_seconds: float,
    now: datetime,
    commit: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    if row is None:
        raise FreshnessError(f"{component} heartbeat is missing")
    state = str(row.get("state") or "")
    if state not in allowed_states:
        raise FreshnessError(f"{component} heartbeat state is {state or 'missing'}")
    age = _age_seconds(
        row.get("completed_at") or row.get("updated_at"),
        f"{component} heartbeat completion time",
        now,
    )
    if age > max_age_seconds:
        raise FreshnessError(f"{component} heartbeat is stale ({age:.1f}s)")
    heartbeat_commit = str(row.get("commit_sha") or "")
    if heartbeat_commit != commit:
        raise FreshnessError(f"{component} heartbeat commit does not match the active release")
    try:
        detail = json.loads(str(row.get("detail") or "{}"))
    except ValueError as exc:
        raise FreshnessError(f"{component} heartbeat detail is invalid") from exc
    if not isinstance(detail, dict):
        raise FreshnessError(f"{component} heartbeat detail is invalid")
    if run_id is not None and detail.get("run_id") != run_id:
        raise FreshnessError(f"{component} heartbeat run ID does not match its identity")
    return {"state": state, "age_seconds": age, "commit": heartbeat_commit}


def _read_lock_value(lockdir: Path, name: str) -> str:
    path = lockdir / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FreshnessError(f"listener lock {name} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
    ):
        raise FreshnessError(f"listener lock {name} is not a trusted regular file")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise FreshnessError(f"listener lock {name} is unavailable") from exc


def _validate_listener_lock(
    lockdir: Path,
    *,
    listener_pid: int,
    release_root: Path,
    commit: str,
    lock_probe: Callable[..., LockInspection],
) -> dict[str, Any]:
    inspection = lock_probe(
        lockdir,
        expected_commands=("run_telegram_listener.sh", "--mode listener"),
        expected_repository=release_root,
        expected_commit=commit,
    )
    if inspection.state != "active" or "mismatch" in inspection.reason.lower():
        raise FreshnessError(
            f"listener lock is not authoritative: {inspection.state} ({inspection.reason})"
        )
    if inspection.pid != listener_pid:
        raise FreshnessError("listener lock PID does not match listener identity")
    if _read_lock_value(lockdir, "repository_path") != str(release_root):
        raise FreshnessError("listener lock repository does not match the active release")
    if _read_lock_value(lockdir, "commit") != commit:
        raise FreshnessError("listener lock commit does not match the active release")
    return {
        "state": inspection.state,
        "pid": inspection.pid,
        "age_seconds": inspection.age_seconds,
    }


def evaluate_runtime_freshness(
    *,
    state_root: Path,
    runtime_link: Path,
    database: Path | None = None,
    now: datetime | None = None,
    pid_probe: Callable[[int], bool] = check_pid_running,
    lock_probe: Callable[..., LockInspection] = inspect_lock,
) -> dict[str, Any]:
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    report: dict[str, Any] = {
        "ok": False,
        "checked_at": checked_at.isoformat(),
        "state_root": str(state_root),
        "runtime_pointer": str(runtime_link),
        "components": {},
        "errors": [],
    }

    def fail(component: str, error: Exception) -> None:
        message = str(error)
        report["components"][component] = {"ok": False, "error": message}
        report["errors"].append(f"{component}: {message}")

    try:
        release_root, manifest = _load_runtime_authority(runtime_link)
        commit = str(manifest["release_commit"])
        scanner_stale, listener_stale = _load_thresholds(release_root)
        report["release_root"] = str(release_root)
        report["release_id"] = str(manifest["release_id"])
        report["release_commit"] = commit
        report["components"]["runtime_authority"] = {
            "ok": True,
            "release_id": manifest["release_id"],
            "commit": commit,
            "git_tree_sha": manifest["git_tree_sha"],
        }
    except FreshnessError as exc:
        fail("runtime_authority", exc)
        return report

    runtime_dir = state_root / "runtime"
    scanner_identity: dict[str, Any] | None = None
    listener_identity: dict[str, Any] | None = None
    try:
        scanner_identity, scanner_identity_age = _load_identity(
            runtime_dir / "scanner_identity.json",
            role="scanner",
            release_root=release_root,
            commit=commit,
            now=checked_at,
        )
        if scanner_identity_age > scanner_stale:
            raise FreshnessError(
                f"scanner identity is stale ({scanner_identity_age:.1f}s)"
            )
        report["components"]["scanner_identity"] = {
            "ok": True,
            "run_id": scanner_identity["run_id"],
            "age_seconds": scanner_identity_age,
        }
    except FreshnessError as exc:
        fail("scanner_identity", exc)

    try:
        listener_identity, listener_identity_age = _load_identity(
            runtime_dir / "telegram_listener_identity.json",
            role="telegram_listener",
            release_root=release_root,
            commit=commit,
            now=checked_at,
        )
        if not pid_probe(int(listener_identity["pid"])):
            raise FreshnessError("telegram listener identity PID is not running")
        report["components"]["listener_identity"] = {
            "ok": True,
            "run_id": listener_identity["run_id"],
            "pid": listener_identity["pid"],
            "age_seconds": listener_identity_age,
        }
    except FreshnessError as exc:
        fail("listener_identity", exc)

    heartbeat_database = database or state_root / "database" / "trading_agent.sqlite3"
    try:
        quick_check, heartbeats = _read_heartbeats(heartbeat_database)
        report["components"]["database"] = {"ok": True, "quick_check": quick_check}
    except FreshnessError as exc:
        fail("database", exc)
        heartbeats = {}

    if scanner_identity is not None:
        try:
            result = _validate_heartbeat(
                heartbeats.get("scanner"),
                component="scanner",
                allowed_states=ALLOWED_SCANNER_STATES,
                max_age_seconds=scanner_stale,
                now=checked_at,
                commit=commit,
                run_id=str(scanner_identity["run_id"]),
            )
            report["components"]["scanner_heartbeat"] = {"ok": True, **result}
        except FreshnessError as exc:
            fail("scanner_heartbeat", exc)
    else:
        fail("scanner_heartbeat", FreshnessError("scanner identity is unavailable"))

    if listener_identity is not None:
        try:
            result = _validate_heartbeat(
                heartbeats.get("listener_poll"),
                component="listener",
                allowed_states={"healthy"},
                max_age_seconds=listener_stale,
                now=checked_at,
                commit=commit,
                run_id=str(listener_identity["run_id"]),
            )
            report["components"]["listener_heartbeat"] = {"ok": True, **result}
        except FreshnessError as exc:
            fail("listener_heartbeat", exc)
    else:
        fail("listener_heartbeat", FreshnessError("listener identity is unavailable"))

    if listener_identity is not None:
        try:
            result = _validate_listener_lock(
                state_root / "locks" / "listener.lockdir",
                listener_pid=int(listener_identity["pid"]),
                release_root=release_root,
                commit=commit,
                lock_probe=lock_probe,
            )
            report["components"]["listener_lock"] = {"ok": True, **result}
        except FreshnessError as exc:
            fail("listener_lock", exc)
    else:
        fail("listener_lock", FreshnessError("listener identity is unavailable"))

    report["ok"] = not report["errors"]
    return report


def _print_human(report: dict[str, Any]) -> None:
    print("=== Runtime Freshness Report ===")
    print(f"Runtime State Root: {report['state_root']}")
    print(f"Runtime Pointer: {report['runtime_pointer']}")
    if report.get("release_root"):
        print(f"Expected Runtime Root: {report['release_root']}")
        print(f"Expected Runtime Commit: {report['release_commit']}")
    for name, component in report["components"].items():
        if component.get("ok"):
            print(f"PASS {name}: {json.dumps(component, sort_keys=True)}")
        else:
            print(f"FAIL {name}: {component['error']}")
    print("--------------------------------")
    if report["ok"]:
        print("PASS scanner and listener match the active immutable release and are fresh")
    else:
        print("FAIL runtime freshness gate is closed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed scanner/listener immutable-runtime freshness check"
    )
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--runtime-link", type=Path, default=DEFAULT_RUNTIME_LINK)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate_runtime_freshness(
        state_root=(args.state_root or runtime_state_root()).expanduser().resolve(),
        runtime_link=args.runtime_link.expanduser(),
        database=args.database.expanduser() if args.database else None,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
