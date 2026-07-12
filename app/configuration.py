from __future__ import annotations

import hashlib
import json
import math
import warnings
from typing import Any

from .formula_versions import (
    ACCOUNTING_VERSION,
    CONFIGURATION_SCHEMA_VERSION,
    EVIDENCE_VERSION,
    PHASE3_DECISION_VERSION,
    PHASE4_ALLOCATION_VERSION,
    RISK_DECISION_VERSION,
    SIZING_POLICY_VERSION,
    STOP_POLICY_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
)


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

STRICT_TOP_LEVEL_KEYS = {
    "configuration_schema_version", "strict_unknown_keys", "effective_config_hash", "mode", "live_enabled", "explicit_live_confirmation",
    "phase2_shadow_strategies", "phase3", "phase4", "profitability_engine", "execution_capabilities", "broker",
    "require_power", "require_market_open", "preflight", "watchlist", "approved_strategy_versions", "strategies", "formula_versions", "crypto",
    "market_profiles", "proposal_expiry_default_minutes", "proposal_expiry_min_minutes", "proposal_expiry_max_minutes",
    "proposal_expiry_high_volatility_minutes", "proposal_expiry_low_volatility_minutes", "proposal_expiry_notify_on_expiry",
    "proposal_expiry_high_volatility_threshold", "proposal_expiry_low_volatility_threshold", "portfolio_execution_mode",
    "proposal_mode", "auto_execution_enabled", "auto_execution_mode", "risk_budget", "data_providers", "alpaca", "eodhd", "news_providers", "dynamic_universe",
    "dynamic_universe_resilience", "position_management", "telegram", "ai", "ml_shadow_enabled", "risk",
    "portfolio_behavior", "position_sizing", "portfolio_optimizer", "add_to_position", "cash_management", "storage",
    "digest", "emergency_exit", "quotes",
}

STRICT_SECTION_KEYS = {
    "execution_capabilities": {"live_execution_enabled", "autonomous_entries_enabled", "autonomous_exits_enabled", "protective_paper_exit_enabled"},
    "quotes": {"max_age_seconds", "max_spread_bps", "max_limit_slippage_bps", "price_increment_usd"},
    "risk": {
        "max_trade_notional_paper", "max_trade_notional_live", "max_trades_per_day", "max_open_positions",
        "allow_add_to_existing_position", "block_new_buys_when_any_position_open", "block_new_buys_after_buy_order_submitted_today",
        "block_same_symbol_rebuy_while_position_open", "allow_margin", "allow_shorting", "allow_options", "allow_crypto",
        "allow_fractional", "signal_expiry_minutes", "stop_if_daily_loss_pct_exceeds", "stop_if_weekly_loss_pct_exceeds",
        "stop_if_daily_loss_dollars_exceeds", "stop_if_weekly_loss_dollars_exceeds", "require_final_revalidation", "allowed_order_types",
        "max_price_age_seconds", "min_historical_bars", "max_price_gap_pct", "max_new_buy_proposals_per_cycle", "max_pending_buy_proposals",
        "allow_multiple_exit_proposals", "use_gpt_for_exit_explanations", "exit_gpt_max_wait_seconds",
    },
    "position_sizing": {
        "enabled", "mode", "stage", "use_stage_dollar_cap", "stage_max_initial_notional_usd", "stage_max_add_notional_usd",
        "risk_per_trade_pct", "max_trade_notional_pct_equity", "max_position_notional_pct_equity", "max_total_portfolio_exposure_pct",
        "max_cluster_exposure_pct", "min_cash_reserve_pct", "max_cash_usage_pct", "max_margin_usage_pct",
        "default_paper_notional_usd", "default_add_notional_usd", "minimum_executable_notional_usd", "absolute_max_notional_usd",
        "add_size_multiplier", "stop_model", "score_multiplier", "volatility_multiplier",
    },
    "phase2_shadow_strategies": {
        "enabled", "mode", "schema_version", "outcome_engine_version", "promotion_enabled", "proposals_enabled",
        "approvals_enabled", "telegram_approval_messages_enabled", "risk_reservations_enabled", "order_intents_enabled", "broker_calls_enabled",
    },
    "phase3": {
        "enabled", "active", "mode", "profile_version", "require_manual_approval", "require_paper_account_identity",
        "require_healthy_reconciliation", "allow_score_based_sizing", "allow_kelly_sizing", "allow_leverage", "promotion", "risk_profile",
    },
    "phase4": {
        "enabled", "active", "mode", "allocator_version", "fractional_kelly", "operational_kelly_enabled", "operational_allocation_mode",
        "full_kelly_allowed", "llm_trading_decisions", "uncalibrated_score_sizing", "minimum_oos_samples", "minimum_regimes",
        "shrinkage_prior_samples", "confidence_z", "covariance_shrinkage", "fallback_annual_variance", "deterioration_suspend_z",
        "evidence_stale_after_days", "max_strategy_weight", "max_allocated_risk_fraction", "max_stress_loss", "exploration_heat_pct",
        "exploration_stop_risk_pct", "max_exploration_stop_risk_pct", "exploration_gross_exposure_pct", "preserve_cash_on_unreliable_evidence",
        "require_manual_approval", "phase3_hard_limits_authoritative",
    },
    "risk_budget": {
        "risk_per_trade_pct", "max_open_risk_pct", "max_daily_realized_loss_pct", "max_total_portfolio_exposure_pct",
        "max_single_symbol_exposure_pct", "max_cluster_exposure_pct", "max_adds_only_if_profitable", "block_averaging_down",
    },
    "formula_versions": {"stop_policy", "sizing_policy", "risk_decision", "accounting", "evidence", "strategy_performance", "strategy_policy"},
    "profitability_engine": {
        "enabled", "enforcement_enabled", "performance_version", "policy_version", "schema_version", "primary_horizon_sessions",
        "minimum_completed_samples", "minimum_regimes", "evidence_stale_after_days", "maturity_research_only_max",
        "maturity_exploration_max", "maturity_throttled_max", "score_exploration_threshold", "score_throttled_threshold",
        "score_active_threshold", "hard_max_drawdown_r", "hard_max_losing_streak", "hard_max_divergence_r",
        "target_expectancy_r", "target_profit_factor", "target_drawdown_r", "target_losing_streak", "target_shortfall_bps", "target_divergence_r",
    },
}

STRICT_NESTED_KEYS = {
    "phase3.promotion": {"minimum_completed_oos", "minimum_regimes", "require_positive_cost_adjusted_expectancy"},
    "phase3.risk_profile": {
        "base_stop_risk_pct", "add_stop_risk_pct", "max_trade_stop_risk_pct", "max_portfolio_heat_pct",
        "favorable_portfolio_heat_pct", "defensive_portfolio_heat_pct", "normal_gross_exposure_pct",
        "favorable_gross_exposure_pct", "hard_gross_exposure_pct", "max_symbol_exposure_pct", "max_cluster_exposure_pct",
        "daily_loss_throttle_pct", "weekly_loss_throttle_pct", "drawdown_halt_pct", "minimum_average_dollar_volume",
    },
    "position_sizing.stop_model": {"method", "atr_multiple", "technical_stop", "max_stop_pct", "min_stop_pct", "require_validated_evidence"},
    "position_management.healthy_pullback_add": {
        "enabled", "minimum_unrealized_profit_pct", "minimum_trade_score", "minimum_score_improvement", "max_emergency_exit_score",
        "max_profit_giveback_ratio", "max_pullback_atr_multiple", "fallback_max_pullback_pct", "require_price_above_avg_entry",
        "require_price_above_ma50", "require_price_above_ma200", "require_no_profit_protection_warning", "require_no_exit_signal",
        "require_normal_or_elevated_volatility_only", "require_telegram_approval",
    },
}


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate safety semantics while retaining compatible non-critical keys."""
    errors: list[str] = []
    emitted: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    if config.get("strict_unknown_keys") is True:
        unknown = sorted(set(config) - STRICT_TOP_LEVEL_KEYS)
        errors.extend(f"unknown top-level configuration key: {key}" for key in unknown)
        for section, allowed in STRICT_SECTION_KEYS.items():
            value = config.get(section)
            if isinstance(value, dict):
                errors.extend(f"unknown {section} configuration key: {key}" for key in sorted(set(value) - allowed))
        for path, allowed in STRICT_NESTED_KEYS.items():
            value: Any = config
            for component in path.split("."):
                value = value.get(component) if isinstance(value, dict) else None
            if isinstance(value, dict):
                errors.extend(f"unknown {path} configuration key: {key}" for key in sorted(set(value) - allowed))
        sizing_source = config.get("position_sizing", {}) or {}
        for required_key in (
            "minimum_executable_notional_usd", "default_paper_notional_usd", "default_add_notional_usd",
            "stage", "stage_max_initial_notional_usd", "stage_max_add_notional_usd", "max_trade_notional_pct_equity",
            "max_position_notional_pct_equity", "max_total_portfolio_exposure_pct", "max_cluster_exposure_pct",
        ):
            if required_key not in sizing_source:
                errors.append(f"position_sizing.{required_key} is required")
        strategies = config.get("strategies", {}) or {}
        allowed_strategy_keys = {"enabled", "maximum_volatility_20d", "require_ma_200", "stop_drawdown_pct"}
        if not isinstance(strategies, dict):
            errors.append("strategies must be a mapping")
        else:
            for strategy_name, strategy_config in strategies.items():
                if strategy_name != "rule_based_v2":
                    errors.append(f"unknown strategies key: {strategy_name}")
                    continue
                if not isinstance(strategy_config, dict):
                    errors.append("strategies.rule_based_v2 must be a mapping")
                    continue
                errors.extend(
                    f"unknown strategies.rule_based_v2 configuration key: {key}"
                    for key in sorted(set(strategy_config) - allowed_strategy_keys)
                )

    require(config.get("configuration_schema_version") == CONFIGURATION_SCHEMA_VERSION,
            f"configuration_schema_version must be {CONFIGURATION_SCHEMA_VERSION}")
    formula_versions = config.get("formula_versions", {}) or {}
    expected_formulas = {
        "stop_policy": STOP_POLICY_VERSION,
        "sizing_policy": SIZING_POLICY_VERSION,
        "risk_decision": RISK_DECISION_VERSION,
        "accounting": ACCOUNTING_VERSION,
        "evidence": EVIDENCE_VERSION,
        "strategy_performance": STRATEGY_PERFORMANCE_VERSION,
        "strategy_policy": STRATEGY_POLICY_VERSION,
    }
    for key, expected in expected_formulas.items():
        require(formula_versions.get(key) == expected, f"formula_versions.{key} must be {expected}")

    profitability = config.get("profitability_engine", {}) or {}
    require(profitability.get("enabled") is True, "profitability_engine.enabled must be true")
    require(profitability.get("enforcement_enabled") is True, "profitability_engine.enforcement_enabled must be true in Build 2")
    require(profitability.get("performance_version") == STRATEGY_PERFORMANCE_VERSION, f"profitability_engine.performance_version must be {STRATEGY_PERFORMANCE_VERSION}")
    require(profitability.get("policy_version") == STRATEGY_POLICY_VERSION, f"profitability_engine.policy_version must be {STRATEGY_POLICY_VERSION}")
    require(profitability.get("schema_version") == STRATEGY_PERFORMANCE_SCHEMA_VERSION, f"profitability_engine.schema_version must be {STRATEGY_PERFORMANCE_SCHEMA_VERSION}")
    require(profitability.get("primary_horizon_sessions") == 20, "profitability_engine.primary_horizon_sessions must be 20")
    if profitability:
        score_thresholds = [profitability.get("score_exploration_threshold"), profitability.get("score_throttled_threshold"), profitability.get("score_active_threshold")]
        if all(value is not None for value in score_thresholds):
            require(score_thresholds == [45, 60, 75], "profitability_engine score thresholds must remain 45, 60, 75")
        maturity_thresholds = [profitability.get("maturity_research_only_max"), profitability.get("maturity_exploration_max"), profitability.get("maturity_throttled_max")]
        if all(value is not None for value in maturity_thresholds):
            require(maturity_thresholds == [19, 49, 99], "profitability_engine maturity ceilings must remain 19, 49, 99")

    mode = config.get("mode", "paper")
    require(mode in {"paper", "live"}, "mode must be paper or live")
    require(mode == "paper", "this build is paper-only; mode=live is contradictory")
    require(config.get("live_enabled") is False, "live_enabled must be false in this build")
    require(config.get("auto_execution_enabled", False) is False, "auto_execution_enabled must remain false")
    require(config.get("auto_execution_mode", "manual_only") == "manual_only", "auto_execution_mode must be manual_only")
    capabilities = config.get("execution_capabilities", {}) or {}
    require(capabilities.get("live_execution_enabled", False) is False, "live execution capability must remain disabled")
    require(capabilities.get("autonomous_entries_enabled", False) is False, "autonomous ordinary entries must remain disabled")
    require(capabilities.get("autonomous_exits_enabled", False) is False, "autonomous ordinary exits must remain disabled")
    require(capabilities.get("protective_paper_exit_enabled", True) is True, "validated protective paper exit capability must remain enabled")
    phase3 = config.get("phase3", {}) or {}
    if phase3.get("active"):
        require(phase3.get("enabled") is True, "active Phase 3 must be enabled")
        require(phase3.get("mode") == "ACTIVE_PAPER", "Phase 3 mode must be ACTIVE_PAPER")
        require(phase3.get("require_manual_approval") is True, "Phase 3 requires manual approval for entries and adds")
        require(phase3.get("allow_score_based_sizing") is False, "uncalibrated score-based sizing is forbidden")
        require(phase3.get("allow_kelly_sizing") is False, "Kelly sizing is Phase 4 and forbidden")
        require(phase3.get("allow_leverage") is False, "Phase 3 leverage is forbidden")
        try:
            from .phase3_risk import Phase3RiskProfile
            Phase3RiskProfile.from_config(config)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid Phase 3 risk profile: {exc}")
    phase4 = config.get("phase4", {}) or {}
    if phase4.get("active"):
        require(phase4.get("enabled") is True, "active Phase 4 must be enabled")
        require(phase4.get("mode") == "ACTIVE_ADAPTIVE_PAPER", "Phase 4 mode must be ACTIVE_ADAPTIVE_PAPER")
        require(phase4.get("full_kelly_allowed") is False, "full Kelly is forbidden")
        require(phase4.get("llm_trading_decisions") is False, "LLM trading decisions are forbidden")
        require(phase4.get("uncalibrated_score_sizing") is False, "uncalibrated score sizing is forbidden")
        require(phase4.get("require_manual_approval") is True, "Phase 4 exploration requires manual approval")
        kelly = _bounded(phase4.get("fractional_kelly"), "phase4.fractional_kelly", errors, 0.01, 0.25)
        require(kelly is not None and kelly <= 0.25, "Phase 4 fractional Kelly cannot exceed one quarter")
        exploration_heat = _bounded(phase4.get("exploration_heat_pct"), "phase4.exploration_heat_pct", errors, 0, 0.25)
        exploration_risk = _bounded(phase4.get("exploration_stop_risk_pct"), "phase4.exploration_stop_risk_pct", errors, 0, 0.10)
        exploration_max = _bounded(phase4.get("max_exploration_stop_risk_pct"), "phase4.max_exploration_stop_risk_pct", errors, 0, 0.10)
        exploration_gross = _bounded(phase4.get("exploration_gross_exposure_pct"), "phase4.exploration_gross_exposure_pct", errors, 0, 7.5)
        require(exploration_heat is not None and exploration_heat <= 0.25, "Phase 4 exploration heat exceeds the 0.25% bound")
        require(exploration_risk is not None and exploration_max is not None and exploration_risk <= exploration_max, "Phase 4 per-strategy exploration stop risk exceeds its maximum")
        require(exploration_gross is not None and exploration_gross <= 7.5, "Phase 4 exploration gross exposure exceeds the 7.5% bound")

    # Safety-critical numeric units are validated recursively. A typo such as
    # a string percentage or a millisecond value in a seconds field must fail
    # before a runtime object or database is opened.
    for section_name in ("risk", "risk_budget", "phase3", "phase4", "profitability_engine", "position_sizing", "portfolio_behavior", "portfolio_optimizer", "quotes", "alpaca", "preflight"):
        _validate_units(config.get(section_name), section_name, errors)

    try:
        from .position_sizing import effective_notional_policy
        equity_hint = float((config.get("position_sizing", {}) or {}).get("validation_equity_usd", 10000.0))
        effective_notional_policy(config, equity_hint)
        effective_notional_policy(config, equity_hint, is_add=True)
    except (TypeError, ValueError) as exc:
        errors.append(f"invalid effective notional policy: {exc}")

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


def effective_config_hash(config: dict[str, Any]) -> str:
    """Hash the validated effective configuration without self-reference."""
    payload = {key: value for key, value in config.items() if key != "effective_config_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def _validate_units(value: Any, path: str, errors: list[str]) -> None:
    """Validate numeric safety units without imposing semantics on text keys."""
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        label = f"{path}.{key}"
        if isinstance(item, dict):
            _validate_units(item, label, errors)
            continue
        unit = None
        minimum = 0.0
        maximum = float("inf")
        if key.endswith("_pct") or key.endswith("_percent"):
            unit, maximum = "percent", 100.0
        elif key.endswith("_bps"):
            unit, maximum = "basis points", 10_000.0
        elif key.endswith("_seconds") or key.endswith("_minutes") or key.endswith("_hours"):
            unit = "time"
        elif "notional" in key or key.endswith("_dollars") or key.endswith("_usd"):
            unit = "USD"
        if unit is None or item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            errors.append(f"{label} must be numeric {unit}")
            continue
        number = float(item)
        if not math.isfinite(number) or number < minimum or number > maximum:
            errors.append(f"{label} has invalid {unit} value")
