from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    passed: bool
    checks: tuple[RiskCheck, ...]

    @property
    def reasons(self) -> list[str]:
        return [c.reason for c in self.checks if not c.passed]


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


class RiskEngine:
    def __init__(self, config: dict[str, Any], recorder: Callable[[RiskCheck], None] | None = None) -> None:
        self.config = config
        self.risk = config.get("risk", {})
        self.recorder = recorder

    def evaluate(self, proposal: dict[str, Any], context: dict[str, Any], final: bool = False) -> RiskDecision:
        mode = self.config.get("mode", "paper")
        now = context.get("now") or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        checks: list[RiskCheck] = []

        def check(name: str, passed: bool, reason: str) -> None:
            item = RiskCheck(name, bool(passed), reason)
            checks.append(item)
            if self.recorder:
                self.recorder(item)

        check("mode_gate", mode == "paper" or (self.config.get("live_enabled") is True and self.config.get("explicit_live_confirmation") is True), "paper mode or all explicit live gates required")
        check("kill_switch", not context.get("kill_switch", False), "kill switch must be off")
        check("power", context.get("power_connected") is True, "AC power must be confirmed")
        check("internet", context.get("internet_available") is True, "internet must be available")
        check("database", context.get("database_writable") is True, "database must be writable")
        check("broker", context.get("broker_available") is True, "broker must be reachable")
        check("telegram", context.get("telegram_available") is True, "Telegram must be configured")
        check("market_open", context.get("market_open") is True, "market must be open")

        price = proposal.get("latest_price")
        check("valid_price", isinstance(price, (int, float)) and price > 0, "latest price must be positive")
        price_at = _dt(proposal.get("price_at"))
        age = (now - price_at).total_seconds() if price_at else float("inf")
        check("fresh_price", -5 <= age <= self.risk.get("max_price_age_seconds", 120), "price timestamp must be fresh")
        check("historical_data", int(proposal.get("historical_bars", 0)) >= self.risk.get("min_historical_bars", 50), "sufficient history required")
        check("volume", proposal.get("volume") is not None and proposal.get("volume", 0) >= 0, "volume must be present")
        check("price_gap", abs(float(proposal.get("price_gap_pct", 0))) <= self.risk.get("max_price_gap_pct", 15), "suspicious price gap blocked")

        positions = int(context.get("open_positions", 0))
        is_entry = str(proposal.get("action", "entry")) == "entry"
        check("max_positions", not is_entry or positions < self.risk.get("max_open_positions", 1), "open-position limit")
        check("max_trades", int(context.get("trades_today", 0)) < self.risk.get("max_trades_per_day", 1), "daily trade limit")
        limit = self.risk.get("max_trade_notional_live" if mode == "live" else "max_trade_notional_paper", 5)
        notional = proposal.get("notional")
        check("notional", isinstance(notional, (int, float)) and 0 < notional <= limit, "notional must be positive and within limit")
        check("duplicate_order", not context.get("duplicate_order", False), "duplicate order is forbidden")
        check("duplicate_position", not (is_entry and context.get("same_symbol_position", False)), "duplicate symbol position is forbidden")
        check("margin", not context.get("uses_margin", False) or self.risk.get("allow_margin", False), "margin use must be disabled")
        check("shorting", str(proposal.get("side", "")).lower() != "sell" or not is_entry or self.risk.get("allow_shorting", False), "short entries are disabled")
        # Profile universe checks
        profiles = self.config.get("market_profiles", {})
        symbol = proposal.get("symbol", "").upper()
        
        if not profiles:
            # Fallback for configuration snapshots / testing that do not define profiles
            watchlist = self.config.get("watchlist", ["SPY", "QQQ"])
            check("approved_universe", symbol in watchlist, f"symbol {symbol} not in watchlist")
            check("asset_class", proposal.get("asset_class", "equity") == "equity", "only equities are allowed")
            check("options_blocked", proposal.get("asset_class") != "option", "options are blocked")
            check("crypto_blocked", proposal.get("asset_class") != "crypto" and not symbol.endswith("USD"), "crypto is blocked")
            check("forex_futures_blocked", proposal.get("asset_class") not in {"forex", "future"}, "forex and futures are blocked")
            leveraged_inverse = {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SDOW", "UDOW", "TNA"}
            check("leveraged_inverse_blocked", symbol not in leveraged_inverse, "leveraged/inverse ETFs are blocked")
        else:
            # Find which profile matches the symbol
            symbol_profile = None
            symbol_profile_key = None
            for p_key, p_val in profiles.items():
                if symbol in p_val.get("watchlist", []) or symbol in p_val.get("observation_watchlist", []):
                    symbol_profile = p_val
                    symbol_profile_key = p_key
                    break
                    
            if symbol_profile:
                # Check if active profile
                is_active_profile = symbol_profile.get("status") == "active"
                check("active_profile", is_active_profile, f"profile {symbol_profile_key} is not active")
                
                # Check watchlist
                check("approved_universe", symbol in symbol_profile.get("watchlist", []), f"symbol {symbol} not in active watchlist")
                
                # Check execution
                check("profile_execution_enabled", symbol_profile.get("execution_enabled", False) is True, f"execution disabled for profile {symbol_profile_key}")
                
                # Check proposals
                check("profile_proposals_enabled", symbol_profile.get("proposals_enabled", False) is True, f"proposals disabled for profile {symbol_profile_key}")
                
                # Check broker
                check("profile_broker_alpaca", symbol_profile.get("broker") == "alpaca", f"broker must be alpaca for execution")
                
                # Alpaca cannot be SGX/HKEX broker/data provider
                is_sgx_or_hkex = symbol.endswith(".SI") or symbol.endswith(".HK")
                if is_sgx_or_hkex:
                    check("sgx_hkex_no_alpaca", symbol_profile.get("broker") != "alpaca", "Alpaca cannot be assigned as SGX/HKEX broker/data provider")
                
                # Blocked asset class checks
                check("asset_class", proposal.get("asset_class", "equity") == "equity", "only equities are allowed")
                check("options_blocked", proposal.get("asset_class") != "option", "options are blocked")
                check("crypto_blocked", proposal.get("asset_class") != "crypto" and not symbol.endswith("USD"), "crypto is blocked")
                check("forex_futures_blocked", proposal.get("asset_class") not in {"forex", "future"}, "forex and futures are blocked")
                leveraged_inverse = {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SDOW", "UDOW", "TNA"}
                check("leveraged_inverse_blocked", symbol not in leveraged_inverse, "leveraged/inverse ETFs are blocked")
            else:
                check("approved_universe", False, f"no matching market profile found for symbol {symbol}")
        check("daily_loss", float(context.get("daily_loss", 0)) < self.risk.get("stop_if_daily_loss_exceeds", 5), "daily loss limit")
        check("weekly_loss", float(context.get("weekly_loss", 0)) < self.risk.get("stop_if_weekly_loss_exceeds", 10), "weekly loss limit")

        created = _dt(proposal.get("created_at"))
        expires = _dt(proposal.get("expires_at"))
        check("signal_time", created is not None and created <= now and expires is not None and expires > now, "signal/proposal must be current")
        check("strategy", proposal.get("strategy_version") in self.config.get("approved_strategy_versions", ["rule_based_v1"]), "approved strategy version required")
        check("reason", bool(proposal.get("reason")), "strategy reason required")
        check("side", str(proposal.get("side", "")).lower() in {"buy", "sell"}, "side must be buy or sell")
        check("order_type", proposal.get("order_type", "market") in self.risk.get("allowed_order_types", ["market", "limit"]), "allowed order type required")
        check("buying_power", (not is_entry) or (notional is not None and float(context.get("buying_power", 0)) >= float(notional)), "sufficient buying power required for entries")
        check("client_order_id", bool(proposal.get("client_order_id")) if final else True, "unique client order ID required at final validation")
        if final:
            check("final_revalidation", context.get("final_revalidation") is True, "final revalidation marker required")
            check("approval", context.get("approval_valid") is True, "valid unused approval required")
        return RiskDecision(all(c.passed for c in checks), tuple(checks))

    def check_trade(self, proposal: dict[str, Any], context: dict[str, Any], final: bool = False) -> RiskDecision:
        return self.evaluate(proposal, context, final)
