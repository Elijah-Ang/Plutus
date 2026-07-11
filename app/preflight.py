from __future__ import annotations

import platform
import socket
from dataclasses import dataclass
from datetime import datetime
import os
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
    trading_cfg = config.get("preflight", {}).get("trading", {})

    def cfg_bool(name: str, fallback: bool) -> bool:
        return bool(trading_cfg.get(name, fallback))

    def build(add: Callable[[str, bool, str], None]) -> None:
        power = get_power_status()
        add("power", not cfg_bool("require_ac_power", bool(config.get("require_power", True))) or power.connected is True, power.detail)
        add("internet", not cfg_bool("require_internet", True) or internet_available(), "internet connectivity required")
        if broker is None:
            add("broker", not cfg_bool("require_broker", True), "broker not initialized")
            market_open = False
        else:
            try:
                broker.get_account()
                add("broker", True, "broker reachable")
                if config.get("phase3", {}).get("active") and (os.getenv("TRADING_AGENT_TESTING") != "1" or config.get("phase3", {}).get("force_in_tests") is True):
                    identity = broker.paper_account_identity() if hasattr(broker, "paper_account_identity") else {"verified": False}
                    add("phase3_paper_account_identity", identity.get("verified") is True and identity.get("mode") == "paper", "unambiguous healthy Alpaca paper account required")
                    from .phase3_risk import Phase3Controller
                    controller = Phase3Controller(storage, config, "preflight")
                    healthy, _report = controller.reconciliation_health()
                    add("phase3_reconciliation_health", healthy, "durable intent/reservation reconciliation must be healthy")
                market_open = broker.is_market_open()
            except Exception as exc:
                add("broker", False, f"broker unavailable: {type(exc).__name__}")
                market_open = False
        add("telegram", secret_present("TELEGRAM_BOT_TOKEN") and secret_present("TELEGRAM_ALLOWED_USER_ID"), "Telegram token and authorized user ID required")
        ai_required = config.get("ai", {}).get("enabled", True)
        add("openai", not ai_required or secret_present("OPENAI_API_KEY"), "OpenAI key required when AI review enabled")
        add("market_open", not cfg_bool("require_market_open", bool(config.get("require_market_open", True))) or market_open, "market must be open when required")

    return _run_checks(build, recorder)


def run_research_preflight(config: dict[str, Any], storage: Any, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    research_cfg = config.get("preflight", {}).get("research_only", {})

    def cfg_bool(name: str, fallback: bool) -> bool:
        return bool(research_cfg.get(name, fallback))

    def build(add: Callable[[str, bool, str], None]) -> None:
        power = get_power_status()
        require_ac = cfg_bool("require_ac_power", False)
        add("research_power", not require_ac or power.connected is True, power.detail if require_ac else f"warning-only: {power.detail}")
        add("research_internet", not cfg_bool("require_internet", True) or internet_available(), "internet connectivity required for provider research")
        add("research_database", storage.writable(), "SQLite database must be writable")
        provider_needed = bool(config.get("dynamic_universe", {}).get("enabled", False))
        provider_cfg = config.get("eodhd", {})
        provider_key_name = str(provider_cfg.get("api_key_secret_name", "TradingAgent.EODHD_API_KEY")).replace("TradingAgent.", "")
        add("research_provider_key", not provider_needed or secret_present(provider_key_name) or secret_present(str(provider_cfg.get("api_key_secret_name", ""))), "provider key required when provider calls are needed")
        add("research_market_closed_allowed", cfg_bool("allow_market_closed", True), "research-only tasks may run while market is closed")
        add("research_no_trading_actions", True, "research-only preflight does not permit proposals or broker order actions")

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
