from __future__ import annotations

from app.preflight import PreflightCheck, PreflightResult, run_core_preflight, run_preflight


class FakeStorage:
    last_instance = None

    def __init__(self, *args, **kwargs):
        FakeStorage.last_instance = self
        self.audit_events = []
        self.finished = None
        self.checks = []

    def initialize(self):
        return None

    def start_run(self, mode):
        self.mode = mode
        return "run-test"

    def execute(self, *args, **kwargs):
        return None

    def audit(self, run_id, event_type, detail):
        self.audit_events.append((event_type, detail))

    def finish_run(self, run_id, status, detail=None):
        self.finished = (status, detail)

    def record_check(self, run_id, name, passed, reason, proposal_id=None, stage="preflight"):
        self.checks.append((name, passed, reason, stage))

    def fetch_all(self, *args, **kwargs):
        return []

    def writable(self):
        return True

    def expire_proposals(self):
        return 0


class ClosedBroker:
    def get_account(self):
        return object()

    def is_market_open(self):
        return False


class OpenBroker(ClosedBroker):
    def is_market_open(self):
        return True


class FakeService:
    instances = []

    def __init__(self, config, storage, broker, run_id):
        self.config = config
        self.storage = storage
        self.broker = broker
        self.run_id = run_id
        self.run_cycle_called = False
        self.notify_called = False
        FakeService.instances.append(self)

    def run_dynamic_universe_research_only(self):
        return self.config.get("_research_results", [])

    def notify_premarket_dynamic_universe_status(self, results, trading_skipped_reason):
        self.notify_called = True

    def run_cycle(self, run_dynamic_universe=True):
        self.run_cycle_called = True
        self.run_dynamic_universe = run_dynamic_universe


def _config(research_results=None):
    return {
        "mode": "paper",
        "live_enabled": False,
        "require_market_open": True,
        "storage": {"sqlite_path": "ignored.db"},
        "telegram": {"dynamic_universe_premarket_updates_enabled": True},
        "_research_results": research_results or [],
    }


def _ok_core(*args, **kwargs):
    return PreflightResult(True, (PreflightCheck("core_config", True, "ok"),))


def _closed_trading(*args, **kwargs):
    return PreflightResult(False, (PreflightCheck("market_open", False, "market must be open when required"),))


def _open_trading(*args, **kwargs):
    return PreflightResult(True, (PreflightCheck("market_open", True, "market open"),))


def test_market_closed_daily_deep_research_runs_before_trading_block(monkeypatch):
    from app import main

    FakeService.instances = []
    research_results = [{"status": "completed", "run_type": "daily_deep_research", "promoted": ["AMAT"]}]
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config(research_results))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: ClosedBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_trading_preflight", _closed_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 0

    storage = FakeStorage.last_instance
    service = FakeService.instances[0]
    assert service.run_cycle_called is False
    assert service.notify_called is True
    assert storage.finished[0] == "research_completed_trading_blocked_market_closed"
    assert any(e[0] == "research_completed_trading_blocked_market_closed" for e in storage.audit_events)


def test_market_closed_without_research_due_exits_blocked_without_trading(monkeypatch):
    from app import main

    FakeService.instances = []
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config([]))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: ClosedBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_trading_preflight", _closed_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 2

    service = FakeService.instances[0]
    assert service.run_cycle_called is False
    assert service.notify_called is False
    assert FakeStorage.last_instance.finished == ("blocked", "market_open")


def test_market_open_runs_trading_without_duplicate_dynamic_universe(monkeypatch):
    from app import main

    FakeService.instances = []
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config([{"status": "completed", "run_type": "intraday_light_refresh"}]))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: OpenBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_trading_preflight", _open_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 0

    service = FakeService.instances[0]
    assert service.run_cycle_called is True
    assert service.run_dynamic_universe is False
    assert FakeStorage.last_instance.finished[0] == "completed"


def test_core_preflight_does_not_require_market_open():
    config = _config()
    result = run_core_preflight(config, FakeStorage(), lock_held=True)

    assert result.passed is True
    assert all(c.name != "market_open" for c in result.checks)


def test_legacy_trading_preflight_still_requires_market_open():
    config = _config()
    result = run_preflight(config, FakeStorage(), ClosedBroker(), lock_held=True)

    assert result.passed is False
    assert any(c.name == "market_open" and not c.passed for c in result.checks)
