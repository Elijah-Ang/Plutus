from __future__ import annotations

import platform
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .internet import internet_available
from .power import get_power_status
from .utils import PROJECT_ROOT, secret_present


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    checks: tuple[PreflightCheck, ...]


def _run_checks(check_builder: Callable[[Callable[[str, bool, str], None]], None], recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    checks: list[PreflightCheck] = []

    def add(name: str, passed: bool, reason: str) -> None:
        value = PreflightCheck(name, bool(passed), reason)
        checks.append(value)
        if recorder:
            recorder(value)

    check_builder(add)
    return PreflightResult(all(c.passed for c in checks), tuple(checks))


def run_core_preflight(config: dict[str, Any], storage: Any, lock_held: bool = True, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    def build(add: Callable[[str, bool, str], None]) -> None:
        kill_switch = PROJECT_ROOT / "config" / "KILL_SWITCH"
        add("core_kill_switch", not kill_switch.exists(), "kill switch must not exist")
        add("core_config", config.get("mode") in {"paper", "live"}, "configuration loaded and mode valid")
        add("core_database", storage.writable(), "SQLite database must be writable")
        add("core_run_lock", lock_held, "starter must hold the run lock")
        mode_ok = config.get("mode") == "paper" and config.get("live_enabled") is not True
        add("core_mode", mode_ok, "this build supports paper mode only")
        expired = storage.expire_proposals()
        add("core_proposal_expiry", True, f"expired {expired} stale proposal(s)")
        add("core_local_context", True, f"host={socket.gethostname()} os={platform.system()} time={datetime.now().astimezone().isoformat()}")

    return _run_checks(build, recorder)


def run_trading_preflight(config: dict[str, Any], storage: Any, broker: Any | None = None, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    def build(add: Callable[[str, bool, str], None]) -> None:
        power = get_power_status()
        add("power", not config.get("require_power", True) or power.connected is True, power.detail)
        add("internet", internet_available(), "internet connectivity required")
        if broker is None:
            add("broker", False, "broker not initialized")
            market_open = False
        else:
            try:
                broker.get_account()
                add("broker", True, "broker reachable")
                market_open = broker.is_market_open()
            except Exception as exc:
                add("broker", False, f"broker unavailable: {type(exc).__name__}")
                market_open = False
        add("telegram", secret_present("TELEGRAM_BOT_TOKEN") and secret_present("TELEGRAM_ALLOWED_USER_ID"), "Telegram token and authorized user ID required")
        ai_required = config.get("ai", {}).get("enabled", True)
        add("openai", not ai_required or secret_present("OPENAI_API_KEY"), "OpenAI key required when AI review enabled")
        add("market_open", not config.get("require_market_open", True) or market_open, "market must be open when required")

    return _run_checks(build, recorder)


def run_preflight(config: dict[str, Any], storage: Any, broker: Any | None = None, lock_held: bool = True, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    checks: list[PreflightCheck] = []

    def collect(check: PreflightCheck) -> None:
        checks.append(check)
        if recorder:
            recorder(check)

    core = run_core_preflight(config, storage, lock_held=lock_held, recorder=collect)
    trading = run_trading_preflight(config, storage, broker, recorder=collect)
    legacy_checks = []
    legacy_map = {
        "core_kill_switch": "kill_switch",
        "core_config": "config",
        "core_database": "database",
        "core_run_lock": "run_lock",
        "core_mode": "mode",
        "core_proposal_expiry": "proposal_expiry",
        "core_local_context": "local_context",
    }
    for check in checks:
        legacy_checks.append(PreflightCheck(legacy_map.get(check.name, check.name), check.passed, check.reason))
    return PreflightResult(core.passed and trading.passed, tuple(legacy_checks))
