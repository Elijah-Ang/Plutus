from __future__ import annotations

import os
import platform
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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


def run_preflight(config: dict[str, Any], storage: Any, broker: Any | None = None, lock_held: bool = True, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    checks: list[PreflightCheck] = []

    def add(name: str, passed: bool, reason: str) -> None:
        value = PreflightCheck(name, bool(passed), reason)
        checks.append(value)
        if recorder:
            recorder(value)

    kill_switch = PROJECT_ROOT / "config" / "KILL_SWITCH"
    add("kill_switch", not kill_switch.exists(), "kill switch must not exist")
    power = get_power_status()
    add("power", not config.get("require_power", True) or power.connected is True, power.detail)
    add("internet", internet_available(), "internet connectivity required")
    add("config", config.get("mode") in {"paper", "live"}, "configuration loaded and mode valid")
    add("database", storage.writable(), "SQLite database must be writable")
    add("run_lock", lock_held, "starter must hold the run lock")
    mode_ok = config.get("mode") == "paper" or (config.get("live_enabled") is True and config.get("explicit_live_confirmation") is True)
    add("mode", mode_ok, "paper mode or all explicit live gates required")
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
    expired = storage.expire_proposals()
    add("proposal_expiry", True, f"expired {expired} stale proposal(s)")
    add("local_context", True, f"host={socket.gethostname()} os={platform.system()} time={datetime.now().astimezone().isoformat()}")
    return PreflightResult(all(c.passed for c in checks), tuple(checks))
