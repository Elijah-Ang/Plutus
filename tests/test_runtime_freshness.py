from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.run_lock import LockInspection
from app.storage import Storage
import scripts.check_runtime_freshness as freshness


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)
COMMIT = "a" * 40
TREE = "b" * 40
SOURCE_DIGEST = "c" * 64
CONFIG_HASH = "d" * 64
SCANNER_PID = 41001
LISTENER_PID = 41002


def _write_json(path: Path, value: dict[str, Any], mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _manifest(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "release_id": COMMIT[:12],
        "release_commit": COMMIT,
        "mode": "paper",
        "manual_approval_only": True,
        "live_capability": False,
        "tests_verified": True,
        "python_version": freshness.REQUIRED_PYTHON,
        "schema_version": "runtime-safety-v1",
        "required_schema_versions": ["runtime-safety-v1"],
        "formula_versions": {"risk": "risk-v1"},
        "configuration_hash": CONFIG_HASH,
        "requirements_lock_sha256": "e" * 64,
        "requirements_hash_lock_sha256": "f" * 64,
        "dependency_inventory_sha256": "1" * 64,
        "artifact_test_results_sha256": "2" * 64,
        "tracked_source_inventory_sha256": "3" * 64,
        "wheel_build_evidence_sha256": "4" * 64,
        "release_wheel_sha256": "5" * 64,
        "release_file_inventory_sha256": "6" * 64,
        "release_wheel_filename": "trading-agent-0.1.0-py3-none-any.whl",
        "distribution_name": "trading-agent",
        "distribution_version": "0.1.0",
        "git_tree_sha": TREE,
        "tracked_source_inventory_digest": SOURCE_DIGEST,
        "release_authority": {
            "mode": "forward",
            "source_tree_sha": TREE,
            "tracked_source_inventory_digest": SOURCE_DIGEST,
        },
        "ci": {"workflow_name": "CI", "run_id": 12345, "head_sha": COMMIT},
    }
    value.update(overrides)
    return value


def _identity(
    role: str,
    run_id: str,
    pid: int,
    release: Path,
    **overrides: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role": role,
        "run_id": run_id,
        "pid": pid,
        "start_time": NOW.isoformat(),
        "project_root": str(release),
        "commit": COMMIT,
        "git_clean": True,
    }
    value.update(overrides)
    return value


def _write_heartbeat_database(
    path: Path,
    *,
    scanner_state: str = "healthy",
    scanner_time: datetime = NOW,
    scanner_run_id: str = "scanner-run",
    scanner_commit: str = COMMIT,
    listener_state: str = "healthy",
    listener_time: datetime = NOW,
    listener_commit: str = COMMIT,
    listener_run_id: str = "listener-run",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE health_heartbeats(
                   component TEXT PRIMARY KEY,state TEXT,attempted_at TEXT,
                   completed_at TEXT,successful_at TEXT,blocked_reason TEXT,
                   detail TEXT,commit_sha TEXT,updated_at TEXT)"""
        )
        connection.execute(
            "INSERT INTO health_heartbeats VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "scanner",
                scanner_state,
                scanner_time.isoformat(),
                scanner_time.isoformat(),
                scanner_time.isoformat() if scanner_state == "healthy" else None,
                "market closed" if scanner_state == "blocked" else None,
                json.dumps({"run_id": scanner_run_id}),
                scanner_commit,
                scanner_time.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO health_heartbeats VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "listener_poll",
                listener_state,
                listener_time.isoformat(),
                listener_time.isoformat(),
                listener_time.isoformat() if listener_state == "healthy" else None,
                None,
                json.dumps(
                    {"updates_processed": 0, "run_id": listener_run_id}
                ),
                listener_commit,
                listener_time.isoformat(),
            ),
        )


def _active_lock(
    _path: Path,
    **_kwargs: Any,
) -> LockInspection:
    return LockInspection("active", LISTENER_PID, 5.0, "owner PID is running")


def _fixture(tmp_path: Path) -> dict[str, Path]:
    release = tmp_path / "releases" / COMMIT[:12]
    (release / "config").mkdir(parents=True)
    (release / "config" / "config.yaml").write_text(
        "mode: paper\nhealth:\n  scanner_stale_seconds: 900\n"
        "  listener_stale_seconds: 120\n",
        encoding="utf-8",
    )
    _write_json(release / "release-manifest.json", _manifest())
    runtime_link = tmp_path / "TradingAgentRuntime"
    runtime_link.symlink_to(release, target_is_directory=True)

    state_root = tmp_path / "state"
    _write_json(
        state_root / "runtime" / "scanner_identity.json",
        _identity("scanner", "scanner-run", SCANNER_PID, release),
        0o600,
    )
    _write_json(
        state_root / "runtime" / "telegram_listener_identity.json",
        _identity("telegram_listener", "listener-run", LISTENER_PID, release),
        0o600,
    )
    database = state_root / "database" / "trading_agent.sqlite3"
    _write_heartbeat_database(database)
    lockdir = state_root / "locks" / "listener.lockdir"
    lockdir.mkdir(parents=True)
    (lockdir / "repository_path").write_text(str(release), encoding="utf-8")
    (lockdir / "commit").write_text(COMMIT, encoding="utf-8")
    return {
        "release": release,
        "runtime_link": runtime_link,
        "state_root": state_root,
        "database": database,
    }


def _evaluate(paths: dict[str, Path], **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "state_root": paths["state_root"],
        "runtime_link": paths["runtime_link"],
        "database": paths["database"],
        "now": NOW,
        "pid_probe": lambda _pid: True,
        "lock_probe": _active_lock,
    }
    arguments.update(overrides)
    return freshness.evaluate_runtime_freshness(**arguments)


def _rewrite_identity(
    paths: dict[str, Path],
    name: str,
    role: str,
    run_id: str,
    pid: int,
    **overrides: Any,
) -> Path:
    path = paths["state_root"] / "runtime" / name
    _write_json(
        path,
        _identity(role, run_id, pid, paths["release"], **overrides),
        0o600,
    )
    return path


def test_exact_runtime_authority_and_both_fresh_processes_pass(tmp_path) -> None:
    paths = _fixture(tmp_path)
    report = _evaluate(paths)
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["release_commit"] == COMMIT
    assert report["components"]["database"]["quick_check"] == "ok"


def test_stale_paper_run_recovery_is_terminal_and_audited(tmp_path) -> None:
    storage = Storage(tmp_path / "runtime.db")
    storage.initialize()
    stale_run_id = storage.start_run("paper")
    stale_listener_run_id = storage.start_run("listener")
    current_run_id = storage.start_run("paper")

    recovered = storage.recover_stale_runs(current_run_id, "paper")

    assert recovered == [stale_run_id]
    stale = storage.fetch_all(
        "SELECT status,ended_at,detail FROM runs WHERE id=?", (stale_run_id,)
    )[0]
    assert stale["status"] == "stale_recovered"
    assert stale["ended_at"]
    detail = json.loads(stale["detail"])
    assert detail == {
        "mode": "paper",
        "reason": "runtime_restart_after_previous_process_lost_authority",
        "recovered_by_run_id": current_run_id,
        "started_at": detail["started_at"],
        "trading_ledger_unchanged": True,
    }
    assert (
        storage.fetch_all("SELECT status FROM runs WHERE id=?", (stale_listener_run_id,))[0]["status"]
        == "running"
    )
    assert (
        storage.fetch_all("SELECT status FROM runs WHERE id=?", (current_run_id,))[0]["status"]
        == "running"
    )
    audit = storage.fetch_all(
        """SELECT run_id,event_type FROM audit_events
           WHERE event_type IN ('runtime_run_recovered_after_restart', 'stale_runtime_run_recovered')
           ORDER BY id"""
    )
    assert audit == [
        {"run_id": stale_run_id, "event_type": "runtime_run_recovered_after_restart"},
        {"run_id": current_run_id, "event_type": "stale_runtime_run_recovered"},
    ]

    current_listener_id = storage.start_run("listener")
    assert storage.recover_stale_runs(current_listener_id, "listener") == [
        stale_listener_run_id
    ]
    listener = storage.fetch_all(
        "SELECT status,detail FROM runs WHERE id=?", (stale_listener_run_id,)
    )[0]
    assert listener["status"] == "stale_recovered"
    assert json.loads(listener["detail"])["mode"] == "listener"


def test_missing_scanner_identity_fails_closed(tmp_path) -> None:
    paths = _fixture(tmp_path)
    (paths["state_root"] / "runtime" / "scanner_identity.json").unlink()
    report = _evaluate(paths)
    assert report["ok"] is False
    assert "scanner_identity" in report["components"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"commit": "e" * 40}, "commit"),
        ({"project_root": "/tmp/not-the-release"}, "project root"),
        (
            {"start_time": (NOW - timedelta(seconds=901)).isoformat()},
            "identity is stale",
        ),
    ],
)
def test_scanner_identity_must_match_release_and_be_fresh(
    tmp_path, overrides, message
) -> None:
    paths = _fixture(tmp_path)
    _rewrite_identity(
        paths,
        "scanner_identity.json",
        "scanner",
        "scanner-run",
        SCANNER_PID,
        **overrides,
    )
    report = _evaluate(paths)
    assert report["ok"] is False
    assert message in report["components"]["scanner_identity"]["error"]


@pytest.mark.parametrize(
    ("database_overrides", "message"),
    [
        ({"scanner_time": NOW - timedelta(seconds=901)}, "stale"),
        ({"scanner_run_id": "different-run"}, "run ID"),
        ({"scanner_commit": "e" * 40}, "commit"),
        ({"scanner_state": "failed"}, "state is failed"),
    ],
)
def test_scanner_heartbeat_is_bound_to_identity_release_and_threshold(
    tmp_path, database_overrides, message
) -> None:
    paths = _fixture(tmp_path)
    paths["database"].unlink()
    _write_heartbeat_database(paths["database"], **database_overrides)
    report = _evaluate(paths)
    assert report["ok"] is False
    assert message in report["components"]["scanner_heartbeat"]["error"]


def test_market_closed_blocked_scanner_is_a_fresh_safe_cycle(tmp_path) -> None:
    paths = _fixture(tmp_path)
    paths["database"].unlink()
    _write_heartbeat_database(paths["database"], scanner_state="blocked")
    assert _evaluate(paths)["ok"] is True


@pytest.mark.parametrize(
    ("database_overrides", "message"),
    [
        ({"listener_time": NOW - timedelta(seconds=121)}, "stale"),
        ({"listener_commit": "e" * 40}, "commit"),
        ({"listener_run_id": "different-run"}, "run ID"),
        ({"listener_state": "failed"}, "state is failed"),
    ],
)
def test_listener_heartbeat_must_be_healthy_fresh_and_release_bound(
    tmp_path, database_overrides, message
) -> None:
    paths = _fixture(tmp_path)
    paths["database"].unlink()
    _write_heartbeat_database(paths["database"], **database_overrides)
    report = _evaluate(paths)
    assert report["ok"] is False
    assert message in report["components"]["listener_heartbeat"]["error"]


def test_dead_listener_pid_fails_closed(tmp_path) -> None:
    paths = _fixture(tmp_path)
    report = _evaluate(paths, pid_probe=lambda _pid: False)
    assert report["ok"] is False
    assert "not running" in report["components"]["listener_identity"]["error"]


@pytest.mark.parametrize(
    ("inspection", "message"),
    [
        (
            LockInspection("stale", LISTENER_PID, 500.0, "owner is absent"),
            "not authoritative",
        ),
        (
            LockInspection(
                "active",
                LISTENER_PID,
                5.0,
                "owner PID is running; live owner metadata mismatch: commit",
            ),
            "not authoritative",
        ),
        (
            LockInspection("active", LISTENER_PID + 1, 5.0, "owner PID is running"),
            "PID does not match",
        ),
    ],
)
def test_listener_lock_must_bind_live_process_and_release(
    tmp_path, inspection, message
) -> None:
    paths = _fixture(tmp_path)
    report = _evaluate(paths, lock_probe=lambda *_args, **_kwargs: inspection)
    assert report["ok"] is False
    assert message in report["components"]["listener_lock"]["error"]


def test_identity_symlink_or_permissive_mode_fails_closed(tmp_path) -> None:
    paths = _fixture(tmp_path)
    scanner = paths["state_root"] / "runtime" / "scanner_identity.json"
    scanner.chmod(0o644)
    report = _evaluate(paths)
    assert "permissions" in report["components"]["scanner_identity"]["error"]

    external = tmp_path / "external-identity.json"
    scanner.unlink()
    _write_json(
        external,
        _identity("scanner", "scanner-run", SCANNER_PID, paths["release"]),
        0o600,
    )
    scanner.symlink_to(external)
    report = _evaluate(paths)
    assert "non-symlink" in report["components"]["scanner_identity"]["error"]


def test_runtime_pointer_and_manifest_authority_fail_closed(tmp_path) -> None:
    paths = _fixture(tmp_path)
    release = paths["release"]
    paths["runtime_link"].unlink()
    paths["runtime_link"].mkdir()
    report = _evaluate(paths)
    assert "symbolic link" in report["components"]["runtime_authority"]["error"]

    paths["runtime_link"].rmdir()
    paths["runtime_link"].symlink_to(release, target_is_directory=True)
    _write_json(
        release / "release-manifest.json",
        _manifest(manual_approval_only=False),
    )
    report = _evaluate(paths)
    assert "paper authority" in report["components"]["runtime_authority"]["error"]


def test_runtime_pointer_release_directory_must_match_manifest_id(tmp_path) -> None:
    paths = _fixture(tmp_path)
    manifest_path = paths["release"] / "release-manifest.json"
    _write_json(manifest_path, _manifest(release_id="different-release"))
    report = _evaluate(paths)
    assert "directory" in report["components"]["runtime_authority"]["error"]


def test_runtime_pointer_rejects_release_without_generated_artifact_evidence(tmp_path) -> None:
    paths = _fixture(tmp_path)
    manifest_path = paths["release"] / "release-manifest.json"
    manifest = _manifest(release_file_inventory_sha256=None)
    _write_json(manifest_path, manifest)
    report = _evaluate(paths)
    assert report["ok"] is False
    assert "artifact evidence hash" in report["components"]["runtime_authority"]["error"]


def test_database_probe_is_read_only_and_fails_for_missing_heartbeat_table(tmp_path) -> None:
    paths = _fixture(tmp_path)
    paths["database"].unlink()
    sqlite3.connect(paths["database"]).close()
    before = paths["database"].stat().st_size
    report = _evaluate(paths)
    assert report["ok"] is False
    assert "heartbeat probe failed" in report["components"]["database"]["error"]
    assert paths["database"].stat().st_size == before


def test_runtime_database_symlink_fails_closed(tmp_path) -> None:
    paths = _fixture(tmp_path)
    database = paths["database"]
    external = tmp_path / "external.sqlite3"
    database.replace(external)
    database.symlink_to(external)
    report = _evaluate(paths)
    assert "non-symlink" in report["components"]["database"]["error"]


def test_scanner_storage_heartbeat_persists_exact_boot_commit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.storage.get_git_commit", lambda: COMMIT)
    storage = Storage(tmp_path / "heartbeat.sqlite3")
    storage.initialize()
    run_id = storage.start_run("paper")
    started = storage.fetch_all(
        "SELECT detail,commit_sha FROM health_heartbeats WHERE component='scanner'"
    )[0]
    assert started["commit_sha"] == COMMIT
    assert json.loads(started["detail"])["run_id"] == run_id

    storage.finish_run(run_id, "market_closed_research_not_due", "market_open")
    finished = storage.fetch_all(
        "SELECT state,detail,commit_sha FROM health_heartbeats WHERE component='scanner'"
    )[0]
    assert finished["state"] == "healthy"
    assert finished["commit_sha"] == COMMIT
    assert json.loads(finished["detail"])["run_id"] == run_id


def test_freshness_wrapper_forwards_machine_readable_option() -> None:
    wrapper = (
        Path(__file__).parents[1] / "scripts" / "check_runtime_freshness.sh"
    ).read_text(encoding="utf-8")
    assert '"$ROOT/scripts/check_runtime_freshness.py" "$@"' in wrapper
