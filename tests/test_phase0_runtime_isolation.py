from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.runtime_guard import REQUIRED_SCHEMA_VERSION, RuntimeGuardError, runtime_database_path
from app.storage import Storage


def test_tests_reject_production_state_paths(monkeypatch):
    monkeypatch.setenv("TRADING_AGENT_TESTING", "1")
    with pytest.raises(RuntimeError, match="production-paper"):
        Storage(Path.home() / "Library/Application Support/TradingAgent/database/trading_agent.sqlite3")


def test_development_requires_explicit_nonproduction_database(monkeypatch):
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)
    monkeypatch.delenv("TRADING_AGENT_DATABASE_PATH", raising=False)
    with pytest.raises(RuntimeGuardError, match="explicit database path"):
        runtime_database_path({"storage": {"sqlite_path": "ignored.db"}})


def test_runtime_templates_never_reference_development_checkout():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "launchd").glob("*.plist"):
        text = path.read_text(encoding="utf-8")
        assert "/Users/elijahang/Projects/TradingAgent" not in text
        assert "/Users/elijahang/TradingAgentRuntime" in text
        assert "Application Support/TradingAgent" in text


def test_explicit_migration_is_required_for_runtime_schema(tmp_path):
    db = Storage(tmp_path / "db.sqlite3")
    db.initialize()
    assert REQUIRED_SCHEMA_VERSION not in db.schema_versions()
    with pytest.raises(RuntimeError, match="Database migration required"):
        db.require_runtime_schema()
    db.apply_explicit_migrations()
    assert REQUIRED_SCHEMA_VERSION in db.schema_versions()
    db.require_runtime_schema()


def test_runtime_scripts_keep_locks_and_logs_outside_release_tree():
    root = Path(__file__).resolve().parents[1]
    for name in ("run_once.sh", "run_telegram_listener.sh"):
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "Library/Application Support/TradingAgent" in text
        assert 'RUNTIME="$ROOT/logs/runtime"' not in text
        assert 'TRADING_AGENT_ENV_FILE="$STATE_ROOT/runtime/production.env"' in text


def test_production_environment_must_be_external_and_owner_only(tmp_path, monkeypatch):
    from app.main import _load_runtime_environment

    state_root = tmp_path / "state"
    runtime = state_root / "runtime"
    runtime.mkdir(parents=True)
    env_file = runtime / "production.env"
    env_file.write_text("TRADING_AGENT_RUNTIME_TEST_ENV=loaded\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.delenv("TRADING_AGENT_TESTING", raising=False)
    monkeypatch.setenv("TRADING_AGENT_RUNTIME", "production-paper")
    monkeypatch.setenv("TRADING_AGENT_STATE_ROOT", str(state_root))
    monkeypatch.setenv("TRADING_AGENT_ENV_FILE", str(env_file))
    _load_runtime_environment()
    assert os.environ["TRADING_AGENT_RUNTIME_TEST_ENV"] == "loaded"

    env_file.chmod(0o644)
    with pytest.raises(RuntimeGuardError, match="owner-only"):
        _load_runtime_environment()
