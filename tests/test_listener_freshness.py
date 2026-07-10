import json
import uuid
import pytest
import os
from datetime import datetime, UTC, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.storage import Storage
from app.service import TradingService
from app.utils import (
    get_git_commit,
    is_git_clean,
    record_process_identity,
    check_listener_freshness,
    BOOT_COMMIT
)

class MockTelegramBot:
    def __init__(self):
        self.messages = []
        self.updates = []
    def send_message(self, text, chat_id=None):
        self.messages.append(text)
        return {"message_id": 123}
    def is_authorized(self, sender_id):
        return True
    def is_available(self, force=False):
        return True
    def get_updates(self, timeout=0, offset=None):
        updates = list(self.updates)
        self.updates.clear()
        return updates

class MockClock:
    def __init__(self):
        self.timestamp = datetime.now(UTC)
        self.next_close = self.timestamp + timedelta(hours=2)

class MockBroker:
    def __init__(self):
        self.submitted_orders = []
    def is_market_open(self):
        return True
    def get_latest_price(self, symbol):
        return type("T", (), {"price": 100.0, "timestamp": datetime.now(UTC)})()
    def get_historical_bars(self, symbol, timeframe, limit=250):
        import pandas as pd
        return pd.DataFrame({"close": [100.0] * limit, "volume": [1000.0] * limit})
    def get_account(self):
        return type("A", (), {
            "buying_power": 1000000.0,
            "equity": 1000000.0,
            "last_equity": 1000000.0,
            "cash": 1000000.0,
            "long_market_value": 0.0,
            "short_market_value": 0.0
        })()
    def get_clock(self):
        return MockClock()
    def get_positions(self):
        return []
    def get_open_orders(self):
        return []

@pytest.fixture
def temp_storage(tmp_path):
    db_file = tmp_path / "test_trading.db"
    storage = Storage(db_file)
    storage.initialize()
    return storage

@pytest.fixture
def base_config():
    return {
        "mode": "paper",
        "live_enabled": False,
        "storage": {"sqlite_path": "data/trading_agent.db"},
        "telegram": {
            "telegram_approval_listener_enabled": True,
            "telegram_approval_listener_mode": "approval_only",
            "telegram_approval_poll_interval_seconds": 30,
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 60,
            "approval_max_price_move_bps": 25,
            "chat_id": "123",
            "allowed_user_id": "456"
        },
        "watchlists": {
            "us_equities": ["AAPL", "QQQ"]
        },
        "position_sizing": {
            "enabled": True,
            "us_equities": {
                "base_notional": 5.0,
                "max_portfolio_risk_pct": 2.0,
                "risk_multiplier_band_90": 1.2
            }
        },
        "risk": {
            "max_trade_notional_paper": 10.0,
            "max_trade_notional_live": 10.0,
            "max_trades_per_day": 5,
            "max_open_positions": 5,
            "allow_margin": False,
            "allow_shorting": False,
            "allowed_order_types": ["market"],
            "max_price_age_seconds": 120,
            "min_historical_bars": 50,
            "max_price_gap_pct": 15,
            "stop_if_daily_loss_exceeds": 5,
            "stop_if_weekly_loss_exceeds": 10
        }
    }

def test_git_utils():
    commit = get_git_commit()
    assert commit is not None
    assert isinstance(commit, str)
    clean = is_git_clean()
    assert isinstance(clean, bool)

def test_record_process_identity(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr("app.utils.PROJECT_ROOT", tmp_path)
    
    run_id = "run-test-1"
    role = "test_listener"
    
    identity = record_process_identity(role, run_id)
    assert identity["role"] == role
    assert identity["run_id"] == run_id
    assert identity["pid"] == os.getpid()
    
    json_path = tmp_path / "logs" / "runtime" / f"{role}_identity.json"
    assert json_path.exists()
    
    with json_path.open("r") as f:
        data = json.load(f)
    assert data["role"] == role
    assert data["run_id"] == run_id

def test_check_listener_freshness(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr("app.utils.PROJECT_ROOT", tmp_path)
    state_root = tmp_path / "external-state"
    monkeypatch.setenv("TRADING_AGENT_STATE_ROOT", str(state_root))
    
    res = check_listener_freshness()
    assert res["running"] is False
    
    identity = {
        "role": "telegram_listener",
        "run_id": "test-run",
        "pid": os.getpid(),
        "start_time": datetime.now(UTC).isoformat(),
        "project_root": str(tmp_path),
        "commit": "test-commit-hash",
        "git_clean": True
    }
    
    runtime_dir = state_root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    json_path = runtime_dir / "telegram_listener_identity.json"
    with json_path.open("w") as f:
        json.dump(identity, f)
        
    monkeypatch.setattr("app.utils.get_git_commit", lambda: "test-commit-hash")
    res = check_listener_freshness()
    assert res["running"] is True
    assert res["fresh"] is True
    assert res["mismatch"] is False
    
    monkeypatch.setattr("app.utils.get_git_commit", lambda: "different-commit-hash")
    res = check_listener_freshness()
    assert res["running"] is True
    assert res["fresh"] is False
    assert res["mismatch"] is True
    assert "stale" in res["message"]


def test_release_manifest_is_commit_source_without_git(tmp_path, monkeypatch):
    import app.utils as utils

    (tmp_path / "release-manifest.json").write_text(
        json.dumps({"release_id": "release-1", "release_commit": "a" * 40}),
        encoding="utf-8",
    )
    monkeypatch.setattr(utils, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(utils.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no git")))

    assert utils.get_git_commit() == "a" * 40
    assert utils.is_git_clean() is True


def test_runtime_freshness_script_reads_external_state(tmp_path, monkeypatch, capsys):
    import importlib.util

    release = tmp_path / "release"
    release.mkdir()
    commit = "b" * 40
    (release / "release-manifest.json").write_text(
        json.dumps({"release_id": "release-2", "release_commit": commit}),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    runtime = state_root / "runtime"
    runtime.mkdir(parents=True)
    identity = {
        "pid": os.getpid(),
        "commit": commit,
        "start_time": datetime.now(UTC).isoformat(),
        "project_root": str(release),
    }
    (runtime / "telegram_listener_identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (runtime / "scanner_identity.json").write_text(json.dumps(identity), encoding="utf-8")

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "check_runtime_freshness.py"
    spec = importlib.util.spec_from_file_location("runtime_freshness_test_module", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT_ROOT", release)
    monkeypatch.setattr(module, "DEFAULT_STATE_ROOT", state_root)
    monkeypatch.delenv("TRADING_AGENT_STATE_ROOT", raising=False)

    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert f"Runtime State Root: {state_root}" in output
    assert "Listener status: FRESH" in output

def test_stale_listener_blocks_approvals(temp_storage, base_config, monkeypatch):
    monkeypatch.setattr("app.utils.BOOT_COMMIT", "boot-commit-abc")
    monkeypatch.setattr("app.utils.get_git_commit", lambda: "new-commit-def")
    
    run_id = temp_storage.start_run("listener")
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, run_id)
    service.telegram = MockTelegramBot()
    
    now = datetime.now(UTC)
    proposal_id = str(uuid.uuid4())
    temp_storage.execute(
        "INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)",
        (proposal_id, run_id, "AAPL", "buy", 5.0, "pending", now.isoformat(), (now + timedelta(minutes=5)).isoformat(), "rule_based_v1")
    )
    
    approval_id = str(uuid.uuid4())
    blocked = service._check_stale_listener_block("AAPL", approval_id)
    assert blocked is True
    
    assert len(service.telegram.messages) == 1
    assert "stale code" in service.telegram.messages[0]
    
    events = temp_storage.fetch_all("SELECT * FROM audit_events WHERE event_type='listener_stale_code_blocked_approval'")
    assert len(events) == 1
    assert json.loads(events[0]["detail"])["boot_commit"] == "boot-commit-abc"
    assert json.loads(events[0]["detail"])["current_commit"] == "new-commit-def"

def test_fresh_listener_allows_approvals(temp_storage, base_config, monkeypatch):
    monkeypatch.setattr("app.utils.BOOT_COMMIT", "matching-commit")
    monkeypatch.setattr("app.utils.get_git_commit", lambda: "matching-commit")
    
    run_id = temp_storage.start_run("listener")
    broker = MockBroker()
    service = TradingService(base_config, temp_storage, broker, run_id)
    service.telegram = MockTelegramBot()
    
    approval_id = str(uuid.uuid4())
    blocked = service._check_stale_listener_block("AAPL", approval_id)
    assert blocked is False
    assert len(service.telegram.messages) == 0
