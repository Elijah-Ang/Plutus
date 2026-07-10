from __future__ import annotations

import warnings
from typing import Any


class ConfigurationError(ValueError):
    pass


DEPRECATED_KEYS = {
    "scan_interval_minutes": "launchd cadence is authoritative; migrate to runtime_orchestration.scanner_cadence_seconds",
    "execution_limits": "execution limits are superseded by risk_budget and portfolio_behavior",
    "paper_auto_min_asset_score": "automatic ordinary entry execution is unsupported",
    "paper_auto_min_trade_score": "automatic ordinary entry execution is unsupported",
    "paper_auto_max_notional": "automatic ordinary entry execution is unsupported",
    "paper_auto_max_trades_per_day": "automatic ordinary entry execution is unsupported",
    "paper_auto_require_no_open_orders": "automatic ordinary entry execution is unsupported",
    "paper_auto_require_final_revalidation": "automatic ordinary entry execution is unsupported",
    "paper_auto_notify_after_execution": "automatic ordinary entry execution is unsupported",
}
_WARNED_DEPRECATIONS: set[str] = set()


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate safety semantics while retaining compatible non-critical keys."""
    errors: list[str] = []
    emitted: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    mode = config.get("mode", "paper")
    require(mode in {"paper", "live"}, "mode must be paper or live")
    require(mode == "paper", "this build is paper-only; mode=live is contradictory")
    require(config.get("live_enabled") is False, "live_enabled must be false in this build")
    require(config.get("auto_execution_enabled", False) is False, "auto_execution_enabled must remain false")
    require(config.get("auto_execution_mode", "manual_only") == "manual_only", "auto_execution_mode must be manual_only")

    crypto = config.get("crypto", {}) or {}
    require(crypto.get("live_enabled", False) is False, "crypto.live_enabled must be false")
    require(crypto.get("paper_trading_enabled", False) is False, "crypto.paper_trading_enabled must remain false in Phase 0")
    require(crypto.get("proposals_enabled", False) is False, "crypto.proposals_enabled must remain false in Phase 0")
    require(crypto.get("mode", "research_only") == "research_only", "crypto.mode must remain research_only in Phase 0")

    expiry_default = _number(config, "proposal_expiry_default_minutes", errors, minimum=1, maximum=1440)
    expiry_min = _number(config, "proposal_expiry_min_minutes", errors, minimum=1, maximum=1440)
    expiry_max = _number(config, "proposal_expiry_max_minutes", errors, minimum=1, maximum=1440)
    if None not in {expiry_default, expiry_min, expiry_max}:
        require(expiry_min <= expiry_default <= expiry_max, "proposal expiry must satisfy min <= default <= max")

    for section, suffix, minimum, maximum in (
        (config.get("alpaca", {}).get("timeouts", {}), "_seconds", 0.1, 300),
        (config.get("runtime_orchestration", {}) or config.get("dynamic_universe", {}).get("runtime_orchestration", {}), "_seconds", 1, 86400),
    ):
        for key, value in section.items():
            if key.endswith(suffix):
                _bounded(value, f"{key}", errors, minimum, maximum)
    telegram = config.get("telegram", {}) or {}
    if "telegram_approval_poll_interval_seconds" in telegram:
        _bounded(telegram["telegram_approval_poll_interval_seconds"], "telegram approval poll interval", errors, 1, 300)

    risk_budget = config.get("risk_budget", {}) or {}
    risk_per_trade = _bounded(risk_budget.get("risk_per_trade_pct"), "risk_budget.risk_per_trade_pct", errors, 0, 100, optional=True)
    open_risk = _bounded(risk_budget.get("max_open_risk_pct"), "risk_budget.max_open_risk_pct", errors, 0, 100, optional=True)
    total = _bounded(risk_budget.get("max_total_portfolio_exposure_pct"), "risk_budget.max_total_portfolio_exposure_pct", errors, 0, 100, optional=True)
    symbol = _bounded(risk_budget.get("max_single_symbol_exposure_pct"), "risk_budget.max_single_symbol_exposure_pct", errors, 0, 100, optional=True)
    cluster = _bounded(risk_budget.get("max_cluster_exposure_pct"), "risk_budget.max_cluster_exposure_pct", errors, 0, 100, optional=True)
    if risk_per_trade is not None and open_risk is not None:
        require(risk_per_trade <= open_risk, "per-trade risk cannot exceed total open-risk limit")
    if None not in {symbol, cluster, total}:
        require(symbol <= cluster <= total, "exposure limits must satisfy symbol <= cluster <= total")

    profiles = config.get("market_profiles", {}) or {}
    for name, profile in profiles.items():
        require(isinstance(profile, dict), f"market_profiles.{name} must be a mapping")
        if not isinstance(profile, dict):
            continue
        watchlist = profile.get("watchlist", [])
        require(isinstance(watchlist, list) and all(isinstance(item, str) and item.strip() for item in watchlist), f"market_profiles.{name}.watchlist must contain symbols")
        if isinstance(watchlist, list):
            normalized = [str(item).upper() for item in watchlist]
            require(len(normalized) == len(set(normalized)), f"market_profiles.{name}.watchlist contains duplicates")

    for key, message in DEPRECATED_KEYS.items():
        if key in config:
            text = f"deprecated configuration key {key}: {message}"
            if key not in _WARNED_DEPRECATIONS:
                warnings.warn(text, DeprecationWarning, stacklevel=2)
                _WARNED_DEPRECATIONS.add(key)
            emitted.append(text)

    if errors:
        raise ConfigurationError("; ".join(errors))
    return emitted


def _number(config: dict[str, Any], key: str, errors: list[str], minimum: float, maximum: float) -> float | None:
    return _bounded(config.get(key), key, errors, minimum, maximum, optional=False)


def _bounded(value: Any, label: str, errors: list[str], minimum: float, maximum: float, optional: bool = False) -> float | None:
    if value is None:
        if not optional:
            errors.append(f"{label} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} must be numeric")
        return None
    numeric = float(value)
    if not minimum <= numeric <= maximum:
        errors.append(f"{label} must be between {minimum} and {maximum}")
    return numeric
