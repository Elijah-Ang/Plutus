from __future__ import annotations

import platform
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .internet import internet_available
from .power import get_power_status
from .utils import kill_switch_active, secret_present


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
        add("core_kill_switch", not kill_switch_active(), "kill switch must not be active")
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
                if config.get("phase3", {}).get("active"):
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
        # OpenAI is optional commentary and never a deterministic trading gate.
        add("openai", True, "optional commentary provider; unavailable AI does not alter trading eligibility")
        add("market_open", not cfg_bool("require_market_open", bool(config.get("require_market_open", True))) or market_open, "market must be open when required")

    return _run_checks(build, recorder)


def run_equity_research_preflight(config: dict[str, Any], storage: Any, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    """Check the dependencies for Dynamic Universe/equity research only.

    The legacy ``research_only`` configuration key remains a fallback so
    older local configurations continue to work.  This lane is the only
    research lane that is allowed to depend on the EODHD credential.
    """

    preflight_cfg = config.get("preflight", {}) or {}
    research_cfg = {
        **(preflight_cfg.get("research_only") or {}),
        **(preflight_cfg.get("equity_research") or {}),
    }

    def cfg_bool(name: str, fallback: bool) -> bool:
        return bool(research_cfg.get(name, fallback))

    def build(add: Callable[[str, bool, str], None]) -> None:
        power = get_power_status()
        require_ac = cfg_bool("require_ac_power", False)
        add("research_power", not require_ac or power.connected is True, power.detail if require_ac else f"warning-only: {power.detail}")
        add("research_internet", not cfg_bool("require_internet", True) or internet_available(), "internet connectivity required for provider research")
        add("research_database", storage.writable(), "SQLite database must be writable")
        dynamic_cfg = config.get("dynamic_universe", {}) or {}
        provider_name = str(
            dynamic_cfg.get("provider")
            or config.get("dynamic_universe_provider")
            or "eodhd"
        ).strip().lower()
        provider_needed = bool(dynamic_cfg.get("enabled", False)) and provider_name == "eodhd"
        provider_cfg = config.get("eodhd", {})
        provider_key_name = str(provider_cfg.get("api_key_secret_name", "TradingAgent.EODHD_API_KEY")).replace("TradingAgent.", "")
        add(
            "research_provider_key",
            not provider_needed
            or secret_present(provider_key_name)
            or secret_present(str(provider_cfg.get("api_key_secret_name", ""))),
            "EODHD key required when Dynamic Universe uses EODHD",
        )
        add("research_market_closed_allowed", cfg_bool("allow_market_closed", True), "research-only tasks may run while market is closed")
        add("research_no_trading_actions", True, "research-only preflight does not permit proposals or broker order actions")

    return _run_checks(build, recorder)


def run_crypto_research_preflight(
    config: dict[str, Any],
    storage: Any,
    broker: Any | None = None,
    recorder: Callable[[PreflightCheck], None] | None = None,
) -> PreflightResult:
    """Check the independent Alpaca-backed 24/7 crypto research lane.

    This preflight deliberately has no EODHD/provider-key check.  Crypto
    market evidence is sourced from Alpaca, so equity-provider failures must
    not disable this lane.
    """

    preflight_cfg = config.get("preflight", {}) or {}
    crypto_preflight_cfg = preflight_cfg.get("crypto_research") or {}
    crypto_cfg = config.get("crypto", {}) or {}
    crypto_enabled = bool(crypto_cfg.get("enabled", False))

    def cfg_bool(name: str, fallback: bool) -> bool:
        return bool(crypto_preflight_cfg.get(name, fallback))

    def build(add: Callable[[str, bool, str], None]) -> None:
        power = get_power_status()
        require_ac = cfg_bool("require_ac_power", False)
        add(
            "crypto_research_power",
            not crypto_enabled or not require_ac or power.connected is True,
            power.detail if require_ac else f"warning-only: {power.detail}",
        )
        add(
            "crypto_research_internet",
            not crypto_enabled or not cfg_bool("require_internet", True) or internet_available(),
            "internet connectivity required for Alpaca crypto research",
        )
        add("crypto_research_database", not crypto_enabled or storage.writable(), "SQLite database must be writable")

        require_broker = cfg_bool("require_broker", True)
        if not crypto_enabled:
            add("crypto_research_broker", True, "crypto research lane is disabled")
        elif broker is None:
            add("crypto_research_broker", not require_broker, "Alpaca broker required for crypto market evidence")
        else:
            try:
                broker.get_account()
                add("crypto_research_broker", True, "Alpaca broker reachable for crypto research")
            except Exception as exc:
                add("crypto_research_broker", not require_broker, f"Alpaca broker unavailable: {type(exc).__name__}")

        add(
            "crypto_research_market_closed_allowed",
            not crypto_enabled or cfg_bool("allow_market_closed", True),
            "crypto research runs continuously and does not require the US equity market to be open",
        )
        add("crypto_research_no_trading_actions", True, "crypto research preflight does not permit broker order actions")

    return _run_checks(build, recorder)


def run_research_preflight(config: dict[str, Any], storage: Any, recorder: Callable[[PreflightCheck], None] | None = None) -> PreflightResult:
    """Backward-compatible alias for the equity research preflight."""

    return run_equity_research_preflight(config, storage, recorder=recorder)


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
