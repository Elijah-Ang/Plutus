from __future__ import annotations

from app.power import PowerStatus
from app.preflight import PreflightCheck, PreflightResult, run_core_preflight, run_preflight, run_research_preflight, run_trading_preflight


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
        self.notify_reason = None
        self.research_calls = []
        FakeService.instances.append(self)

    def cleanup_stale_research_runs(self):
        return 0

    def run_dynamic_universe_research_only(self, **kwargs):
        self.research_calls.append(kwargs)
        return self.config.get("_research_results", [])

    def notify_premarket_dynamic_universe_status(self, results, trading_skipped_reason):
        self.notify_called = True
        self.notify_reason = trading_skipped_reason
        return self.config.get("_notify_result", "suppressed")

    def run_cycle(self, run_dynamic_universe=True):
        self.run_cycle_called = True
        self.run_dynamic_universe = run_dynamic_universe


def _config(research_results=None):
    return {
        "mode": "paper",
        "live_enabled": False,
        "require_market_open": True,
        "storage": {"sqlite_path": "ignored.db"},
        "dynamic_universe": {"enabled": False},
        "preflight": {
            "research_only": {"require_ac_power": False, "require_internet": True, "require_broker": False, "allow_market_closed": True},
            "trading": {"require_ac_power": True, "require_internet": True, "require_broker": True, "require_market_open": True},
        },
        "telegram": {"dynamic_universe_premarket_updates_enabled": True},
        "_research_results": research_results or [],
    }


def _ok_core(*args, **kwargs):
    return PreflightResult(True, (PreflightCheck("core_config", True, "ok"),))


def _ok_research(*args, **kwargs):
    return PreflightResult(True, (PreflightCheck("research_internet", True, "ok"),))


def _closed_trading(*args, **kwargs):
    return PreflightResult(False, (PreflightCheck("market_open", False, "market must be open when required"),))


def _power_trading(*args, **kwargs):
    return PreflightResult(False, (PreflightCheck("power", False, "on battery"),))


def _power_and_closed_trading(*args, **kwargs):
    return PreflightResult(
        False,
        (
            PreflightCheck("power", False, "on battery"),
            PreflightCheck("market_open", False, "market must be open when required"),
        ),
    )


def _broker_trading(*args, **kwargs):
    return PreflightResult(False, (PreflightCheck("broker", False, "broker unavailable"),))


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
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _closed_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 0

    storage = FakeStorage.last_instance
    service = FakeService.instances[0]
    assert service.run_cycle_called is False
    assert service.notify_called is True
    assert service.notify_reason == "market_closed"
    assert storage.finished[0] == "research_completed_trading_blocked_market_closed"
    assert any(e[0] == "research_completed_trading_blocked_market_closed" for e in storage.audit_events)
    detail = [e[1] for e in storage.audit_events if e[0] == "research_completed_trading_blocked_market_closed"][0]
    assert detail["research_status_notification_evaluated"] is True
    assert detail["research_status_notification_result"] == "suppressed"


def test_research_notification_evaluated_when_trading_blocked_by_power(monkeypatch):
    from app import main

    FakeService.instances = []
    research_results = [{"status": "completed", "run_type": "intraday_light_refresh", "run_id": "run-research"}]
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config(research_results))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: OpenBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _power_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 2

    service = FakeService.instances[0]
    storage = FakeStorage.last_instance
    assert service.run_cycle_called is False
    assert service.notify_called is True
    assert service.notify_reason == "power"
    assert storage.finished == ("blocked", "power")
    detail = [e[1] for e in storage.audit_events if e[0] == "research_completed_trading_blocked_market_closed"][0]
    assert detail["trading_skipped_reason"] == "power"
    assert detail["research_completed"] is True
    assert detail["research_skipped_reason"] is None
    assert detail["research_status_notification_evaluated"] is True


def test_research_notification_evaluated_when_trading_blocked_by_power_and_market_open(monkeypatch):
    from app import main

    FakeService.instances = []
    research_results = [{"status": "completed", "run_type": "intraday_light_refresh", "run_id": "run-research"}]
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config(research_results))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: ClosedBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _power_and_closed_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 2

    service = FakeService.instances[0]
    storage = FakeStorage.last_instance
    assert service.run_cycle_called is False
    assert service.notify_called is True
    assert service.notify_reason == "market_closed"
    assert storage.finished == ("blocked", "power; market_open")
    detail = [e[1] for e in storage.audit_events if e[0] == "research_completed_trading_blocked_market_closed"][0]
    assert detail["trading_skipped_reason"] == "power; market_open"
    assert detail["research_status_notification_result"] == "suppressed"


def test_research_notification_evaluated_when_trading_blocked_by_broker(monkeypatch):
    from app import main

    FakeService.instances = []
    research_results = [{"status": "completed", "run_type": "intraday_light_refresh", "run_id": "run-research"}]
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config(research_results))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: ClosedBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _broker_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 2

    service = FakeService.instances[0]
    storage = FakeStorage.last_instance
    assert service.run_cycle_called is False
    assert service.notify_called is True
    assert service.notify_reason == "broker"
    assert storage.finished == ("blocked", "broker")


def test_research_incomplete_does_not_emit_completed_notification(monkeypatch):
    from app import main

    FakeService.instances = []
    research_results = [{"status": "skipped", "run_type": "intraday_light_refresh", "reason": "not_due"}]
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config(research_results))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: ClosedBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _power_and_closed_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 2

    service = FakeService.instances[0]
    assert service.run_cycle_called is False
    assert service.notify_called is False
    assert not any(e[0] == "research_completed_trading_blocked_market_closed" for e in FakeStorage.last_instance.audit_events)


def test_market_closed_without_research_due_exits_blocked_without_trading(monkeypatch):
    from app import main

    FakeService.instances = []
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config([]))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: ClosedBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
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
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _open_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 0

    service = FakeService.instances[0]
    assert service.run_cycle_called is True
    assert service.run_dynamic_universe is False
    assert service.research_calls[0]["run_types"] == ["intraday_light_refresh", "event_triggered_refresh"]
    assert "daily_deep_research" in service.research_calls[0]["skip_run_types"]
    assert FakeStorage.last_instance.finished[0] == "completed"


def test_market_open_research_timeout_does_not_block_cycle_completion(monkeypatch):
    from app import main

    FakeService.instances = []
    monkeypatch.setattr(main, "load_config", lambda config_path=None: _config([{"status": "timeout", "run_type": "dynamic_universe", "reason": "research_wall_clock_timeout"}]))
    monkeypatch.setattr(main, "Storage", FakeStorage)
    monkeypatch.setattr(main, "AlpacaBroker", lambda config: OpenBroker())
    monkeypatch.setattr(main, "TradingService", FakeService)
    monkeypatch.setattr(main, "run_core_preflight", _ok_core)
    monkeypatch.setattr(main, "run_research_preflight", _ok_research)
    monkeypatch.setattr(main, "run_trading_preflight", _open_trading)
    monkeypatch.setattr(main, "configure_logging", lambda: type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None, "exception": lambda *a, **k: None})())

    assert main.run_once() == 0

    service = FakeService.instances[0]
    assert service.run_cycle_called is True
    assert service.research_calls
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


def test_research_only_market_closed_can_run_without_ac_when_config_allows(monkeypatch):
    config = _config()
    config["dynamic_universe"]["enabled"] = False
    monkeypatch.setattr("app.preflight.get_power_status", lambda: PowerStatus(False, "battery", "on battery", 45.0))
    monkeypatch.setattr("app.preflight.internet_available", lambda: True)

    result = run_research_preflight(config, FakeStorage())

    assert result.passed is True
    assert any(c.name == "research_power" and c.passed and "warning-only" in c.reason for c in result.checks)
    assert all(c.name != "broker" for c in result.checks)
    assert all(c.name != "market_open" for c in result.checks)


def test_trading_path_still_requires_ac_power_and_market_open(monkeypatch):
    config = _config()
    monkeypatch.setattr("app.preflight.get_power_status", lambda: PowerStatus(False, "battery", "on battery", 45.0))
    monkeypatch.setattr("app.preflight.internet_available", lambda: True)
    monkeypatch.setattr("app.preflight.secret_present", lambda name: True)

    result = run_trading_preflight(config, FakeStorage(), ClosedBroker())

    assert result.passed is False
    assert any(c.name == "power" and not c.passed for c in result.checks)
    assert any(c.name == "market_open" and not c.passed for c in result.checks)


def test_research_only_preflight_does_not_call_broker_or_order_functions(monkeypatch):
    config = _config()
    monkeypatch.setattr("app.preflight.get_power_status", lambda: PowerStatus(False, "battery", "on battery", 45.0))
    monkeypatch.setattr("app.preflight.internet_available", lambda: True)

    result = run_research_preflight(config, FakeStorage())

    assert result.passed is True
    assert any(c.name == "research_no_trading_actions" and c.passed for c in result.checks)
