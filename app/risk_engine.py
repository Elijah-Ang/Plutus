from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from .formula_versions import EVIDENCE_VERSION, RISK_DECISION_VERSION, STRATEGY_POLICY_VERSION
from .loss_controls import LOSS_METRICS_VERSION
from .position_sizing import effective_notional_policy, validate_stop_evidence


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
        is_entry = str(proposal.get("action", "entry")) in {"entry", "add"}
        is_add = str(proposal.get("action", "entry")) == "add" or bool(proposal.get("is_add", False))

        def check(name: str, passed: bool, reason: str) -> None:
            item = RiskCheck(name, bool(passed), reason)
            checks.append(item)
            if self.recorder:
                self.recorder(item)

        check("mode_gate", mode == "paper" and self.config.get("live_enabled") is not True, "this build supports paper mode only")
        check("kill_switch", not context.get("kill_switch", False), "kill switch must be off")
        check("power", context.get("power_connected") is True, "AC power must be confirmed")
        check("internet", context.get("internet_available") is True, "internet must be available")
        check("database", context.get("database_writable") is True, "database must be writable")
        check("broker", context.get("broker_available") is True, "broker must be reachable")
        check("telegram", not is_entry or context.get("telegram_available") is True, "Telegram must be configured for entry execution")
        check("market_open", context.get("market_open") is True, "market must be open")
        check("live_execution_capability", mode == "paper" and self.config.get("live_enabled") is False, "live execution is disabled")
        if final and context.get("autonomous_entry_requested") is True and is_entry:
            check("autonomous_entry_capability", False, "ordinary autonomous entries are disabled")
        if final and context.get("autonomous_exit_requested") is True and not is_entry:
            check("autonomous_exit_capability", False, "ordinary autonomous exits are disabled")

        price = proposal.get("latest_price")
        check("valid_price", isinstance(price, (int, float)) and price > 0, "latest price must be positive")
        price_at = _dt(proposal.get("price_at"))
        age = (now - price_at).total_seconds() if price_at else float("inf")
        is_dynamic = proposal.get("approved_dynamic_paper_tradable") is True and proposal.get("universe_source") == "dynamic"
        if is_dynamic and final:
            check("fresh_price", -5 <= age <= self.risk.get("max_price_age_seconds", 120), "dynamic symbol failed final Alpaca price freshness check")
        else:
            check("fresh_price", -5 <= age <= self.risk.get("max_price_age_seconds", 120), "price timestamp must be fresh")
        check("historical_data", not is_entry or int(proposal.get("historical_bars", 0)) >= self.risk.get("min_historical_bars", 50), "sufficient history required for entries")
        check("volume", not is_entry or (proposal.get("volume") is not None and proposal.get("volume", 0) >= 0), "volume must be present for entries")
        check("price_gap", not is_entry or abs(float(proposal.get("price_gap_pct", 0))) <= self.risk.get("max_price_gap_pct", 15), "suspicious entry price gap blocked")
        if is_entry:
            stop_evidence = validate_stop_evidence(
                entry_price=price,
                stop_price=proposal.get("stop_price"),
                stop_distance_dollars=proposal.get("stop_distance_dollars"),
                atr_value=proposal.get("atr_value"),
                technical_stop_price=proposal.get("technical_stop_price"),
                stop_model_used=proposal.get("stop_model_used") or proposal.get("stop_model"),
                stop_validation_status=proposal.get("stop_validation_status"),
            )
            check("validated_stop_evidence", stop_evidence["valid"], stop_evidence["reason"])

        positions = int(context.get("open_positions", 0))
        risk_budgeted_mode = self.config.get("portfolio_execution_mode") == "risk_budgeted"

        max_pos = self.config.get("portfolio_behavior", {}).get("max_open_positions", 3)
        if risk_budgeted_mode or max_pos is None:
            check("max_positions", True, "risk-budgeted mode uses exposure and open-risk limits instead of fixed open-position count")
        else:
            check("max_positions", not is_entry or (is_add or positions < max_pos), "open-position limit")

        max_buys_day = self.config.get("portfolio_behavior", {}).get("max_new_buy_orders_per_day", 3)
        if risk_budgeted_mode or max_buys_day is None:
            check("max_buy_trades_today", True, "risk-budgeted mode uses daily loss and portfolio risk instead of fixed daily buy count")
        else:
            check("max_buy_trades_today", not is_entry or int(context.get("buy_trades_today", 0)) < max_buys_day, "daily buy order limit")

        # Portfolio Exposure caps
        max_total_exposure = self.config.get("portfolio_behavior", {}).get("max_total_portfolio_exposure_pct", 6.0)
        projected_total = context.get("proposed_total_exposure_pct")
        check("portfolio_total_exposure", not is_entry or (isinstance(projected_total, (int, float)) and projected_total <= max_total_exposure), "total portfolio exposure cap")

        max_single_exposure = self.config.get("portfolio_behavior", {}).get("max_single_symbol_exposure_pct", 2.5)
        projected_symbol = context.get("proposed_symbol_exposure_pct")
        check("portfolio_single_symbol_exposure", not is_entry or (isinstance(projected_symbol, (int, float)) and projected_symbol <= max_single_exposure), "single symbol exposure cap")

        max_cluster_pos = self.config.get("portfolio_optimizer", {}).get("max_same_cluster_positions", 2)
        projected_cluster_count = context.get("proposed_cluster_positions_count")
        check(
            "portfolio_cluster_positions_limit",
            risk_budgeted_mode or not is_entry or (isinstance(projected_cluster_count, (int, float)) and projected_cluster_count <= max_cluster_pos),
            "risk-budgeted mode treats position counts as targets; cluster exposure remains authoritative",
        )

        max_cluster_exposure = self.config.get("portfolio_optimizer", {}).get("max_same_cluster_exposure_pct", 5.0)
        projected_cluster = context.get("proposed_cluster_exposure_pct")
        check("portfolio_cluster_exposure_limit", not is_entry or (isinstance(projected_cluster, (int, float)) and projected_cluster <= max_cluster_exposure), "same cluster exposure limit")

        # Warning / Exit pending controls
        block_if_exit_pending = self.config.get("portfolio_behavior", {}).get("block_new_buy_if_exit_pending", True)
        if block_if_exit_pending and is_entry and context.get("exit_pending", False):
            exit_reason = context.get("exit_pending_reason") or "an exit is pending"
            check("block_new_buy_if_exit_pending", False, f"new buy blocked because {exit_reason}")
        check(
            "pending_buy_exposure_known",
            not is_entry or context.get("pending_buy_exposure_unknown") is not True,
            context.get("pending_buy_exposure_unknown_reason") or "pending buy exposure must be complete before a new entry",
        )

        block_if_emergency_exit_score_above = self.config.get("portfolio_behavior", {}).get("block_new_buy_if_emergency_exit_score_above", 40)
        if is_entry and context.get("max_emergency_exit_score", 0.0) > block_if_emergency_exit_score_above:
            check("block_new_buy_if_emergency_exit_score_above", False, f"new buy blocked because max emergency exit score is {context.get('max_emergency_exit_score', 0.0):.1f} (> {block_if_emergency_exit_score_above})")

        sizing_cfg = self.config.get("position_sizing", {})
        sizing_enabled = sizing_cfg.get("enabled", True)
        limit = self.risk.get("max_trade_notional_live" if mode == "live" else "max_trade_notional_paper", 5)
        minimum_notional = None
        if sizing_enabled:
            try:
                policy = effective_notional_policy(
                    self.config,
                    float(context.get("portfolio_equity") or 100000.0),
                    is_add=is_add,
                )
                limit = policy.maximum_allowed_notional_usd
                minimum_notional = policy.minimum_executable_notional_usd
            except (TypeError, ValueError):
                # Minimal synthetic test/snapshot configs may predate the
                # explicit policy. Production config validation rejects this.
                if sizing_cfg.get("mode", "fixed") == "risk_portfolio":
                    equity = float(context.get("portfolio_equity") or 100000.0)
                    pct = float(sizing_cfg.get("max_trade_notional_pct_equity", 0.25))
                    stage = sizing_cfg.get("stage", "adaptive_operational_paper")
                    stage_cap = float("inf") if sizing_cfg.get("use_stage_dollar_cap") is False else float((sizing_cfg.get("stage_max_initial_notional_usd", {}) or {}).get(stage) or float("inf"))
                    limit = min(equity * pct / 100.0, stage_cap, float(limit))
                else:
                    limit = float(limit)
                try:
                    minimum_notional = float(sizing_cfg.get("minimum_executable_notional_usd", 0.0))
                except (TypeError, ValueError):
                    minimum_notional = None
        else:
            # An explicitly disabled sizing mode retains its legacy fixed
            # notional contract; the validated production configuration keeps
            # sizing enabled and therefore always supplies this gate.
            minimum_notional = 0.0

        notional = proposal.get("notional")
        exit_quantity = proposal.get("qty")
        check("notional", (not is_entry and isinstance(exit_quantity, (int, float)) and float(exit_quantity) > 0) or (isinstance(notional, (int, float)) and 0 < notional <= limit), "entry notional or exit quantity must be positive and within policy")
        if is_entry:
            check(
                "minimum_notional",
                minimum_notional is not None and isinstance(notional, (int, float)) and float(notional) >= float(minimum_notional),
                "entry notional must meet the executable minimum",
            )
            sizing_caps = proposal.get("sizing_caps") or {}
            if isinstance(sizing_caps, dict):
                for cap_name, cap_value in sizing_caps.items():
                    if isinstance(cap_value, (int, float)) and cap_value == cap_value and cap_value != float("inf"):
                        check(f"sizing_ceiling_{cap_name}", isinstance(notional, (int, float)) and float(notional) <= float(cap_value) + 1e-9, f"notional exceeds {cap_name} ceiling")
        check("duplicate_order", not context.get("duplicate_order", False), "duplicate order is forbidden")
        check("duplicate_position", not (is_entry and not is_add and context.get("same_symbol_position", False)), "duplicate symbol position is forbidden")

        # Explicit Guardrails
        allow_add = self.risk.get("allow_add_to_existing_position", False) or self.config.get("portfolio_behavior", {}).get("allow_add_to_existing_position", False)
        if not allow_add and is_entry and context.get("same_symbol_position", False):
            check("allow_add_to_existing_position", False, "adding to existing position is disabled")

        block_any_pos = self.risk.get("block_new_buys_when_any_position_open", True)
        if block_any_pos and not risk_budgeted_mode and is_entry and not is_add and positions > 0:
            check("block_new_buys_when_any_position_open", False, "new buys blocked when any position is open")

        block_buy_today = self.risk.get("block_new_buys_after_buy_order_submitted_today", True)
        if block_buy_today and not risk_budgeted_mode and is_entry and not is_add and context.get("buy_trades_today", 0) > 0:
            check("block_new_buys_after_buy_order_submitted_today", False, "new buys blocked since a buy order was already submitted today")

        block_same_rebuy = self.risk.get("block_same_symbol_rebuy_while_position_open", True)
        if block_same_rebuy and is_entry and not is_add and context.get("same_symbol_position", False):
            check("block_same_symbol_rebuy_while_position_open", False, "same symbol rebuy is blocked while position is open")
        uses_margin = context.get("uses_margin")
        check("margin_state_known", isinstance(uses_margin, bool), "margin-use state must be authoritative")
        check("margin", uses_margin is False or self.risk.get("allow_margin", False), "margin use must be disabled")
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
            elif proposal.get("approved_dynamic_paper_tradable") is True and proposal.get("universe_source") == "dynamic":
                approved_profile_key = proposal.get("approved_market_profile")
                approved_profile = profiles.get(approved_profile_key) if approved_profile_key else None
                profile_name = approved_profile_key or "dynamic_paper_tradable"
                if not approved_profile:
                    check("active_profile", False, "dynamic symbol missing active scanner profile at final validation")
                else:
                    check("active_profile", approved_profile.get("status") == "active", f"profile {profile_name} is not active")

                sym_info = context.get("universe_symbol_info")
                active_dynamic = context.get("active_dynamic_paper_tradable_symbols")

                if sym_info is not None or active_dynamic is not None:
                    tier = sym_info.get("tier") if sym_info else None
                    alpaca_compatible = sym_info.get("alpaca_compatible") == 1 if sym_info else False
                    universe_lane = sym_info.get("universe_lane") if sym_info else None
                    
                    if not sym_info:
                        check("approved_universe", False, "dynamic symbol missing active scanner profile at final validation")
                    elif tier != "paper_tradable":
                        check("approved_universe", False, "dynamic symbol no longer paper-tradable at final validation")
                    elif universe_lane == "global_research_only":
                        check("approved_universe", False, "global research-only symbol cannot pass final validation")
                    elif universe_lane == "excluded_or_low_quality":
                        check("approved_universe", False, "unsupported/OTC-like symbol cannot pass final validation")
                    elif not alpaca_compatible:
                        check("approved_universe", False, "dynamic symbol failed final Alpaca compatibility check")
                    elif active_dynamic is not None and symbol not in active_dynamic:
                        check("approved_universe", False, "dynamic symbol no longer paper-tradable at final validation")
                    else:
                        check("approved_universe", True, f"symbol {symbol} is approved dynamic paper-tradable")
                else:
                    check("approved_universe", True, f"symbol {symbol} is approved dynamic paper-tradable")

                check("profile_execution_enabled", bool(approved_profile and approved_profile.get("execution_enabled", False) is True), f"execution disabled for profile {profile_name}")
                check("profile_proposals_enabled", bool(approved_profile and approved_profile.get("proposals_enabled", False) is True), f"proposals disabled for profile {profile_name}")
                check("profile_broker_alpaca", bool(approved_profile and approved_profile.get("broker") == "alpaca"), "broker must be alpaca for execution")
                check("asset_class", proposal.get("asset_class", "equity") == "equity", "only equities are allowed")
                check("options_blocked", proposal.get("asset_class") != "option", "options are blocked")
                check("crypto_blocked", proposal.get("asset_class") != "crypto" and not symbol.endswith("USD"), "crypto is blocked")
                check("forex_futures_blocked", proposal.get("asset_class") not in {"forex", "future"}, "forex and futures are blocked")
                leveraged_inverse = {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SDOW", "UDOW", "TNA"}
                check("leveraged_inverse_blocked", symbol not in leveraged_inverse, "leveraged/inverse ETFs are blocked")
            else:
                sym_info = context.get("universe_symbol_info")
                if sym_info:
                    tier = sym_info.get("tier")
                    universe_lane = sym_info.get("universe_lane")
                    if tier == "research_candidate" or universe_lane == "global_research_only":
                        check("approved_universe", False, "research candidate cannot pass final validation" if tier == "research_candidate" else "global research-only symbol cannot pass final validation")
                    elif tier == "observation" or sym_info.get("observation_only") == 1:
                        check("approved_universe", False, "observation-only symbol cannot pass final validation")
                    elif universe_lane == "excluded_or_low_quality" or sym_info.get("alpaca_compatible") == 0:
                        check("approved_universe", False, "unsupported/OTC-like symbol cannot pass final validation")
                    else:
                        check("approved_universe", False, "symbol not found in static or dynamic paper-tradable profiles")
                else:
                    check("approved_universe", False, "symbol not found in static or dynamic paper-tradable profiles")
        daily_loss_pct = context.get("daily_loss_pct")
        weekly_loss_pct = context.get("weekly_loss_pct")
        daily_loss_dollars = context.get("daily_loss_dollars")
        weekly_loss_dollars = context.get("weekly_loss_dollars")
        daily_confidence = context.get("daily_loss_confidence")
        weekly_confidence = context.get("weekly_loss_confidence")
        metrics_version = context.get("loss_metrics_version")
        daily_known = (
            isinstance(daily_loss_pct, (int, float)) and daily_confidence in {"verified", "reconstructed"}
        )
        weekly_known = (
            isinstance(weekly_loss_pct, (int, float)) and weekly_confidence in {"verified", "reconstructed"}
        )
        loss_information_safe = metrics_version == LOSS_METRICS_VERSION and daily_known and weekly_known
        check(
            "realized_loss_information",
            not is_entry or loss_information_safe,
            "versioned reliable daily and weekly loss evidence is required for new entries",
        )
        check("loss_metrics_version", not is_entry or metrics_version == LOSS_METRICS_VERSION, "loss metrics must be explicit and versioned")
        check("daily_loss_known", not is_entry or daily_known, "daily loss percentage must come from an authoritative source")
        check("weekly_loss_known", not is_entry or weekly_known, "weekly loss percentage must come from an authoritative source")
        daily_pct_limit = self.risk.get("stop_if_daily_loss_pct_exceeds", 0.75)
        weekly_pct_limit = self.risk.get("stop_if_weekly_loss_pct_exceeds", 1.50)
        daily_dollar_limit = self.risk.get("stop_if_daily_loss_dollars_exceeds")
        weekly_dollar_limit = self.risk.get("stop_if_weekly_loss_dollars_exceeds")
        check("daily_loss", not is_entry or (daily_known and daily_pct_limit is not None and float(daily_loss_pct) < float(daily_pct_limit)), "daily percentage loss limit")
        check("weekly_loss", not is_entry or (weekly_known and weekly_pct_limit is not None and float(weekly_loss_pct) < float(weekly_pct_limit)), "weekly percentage loss limit")
        if daily_dollar_limit is not None:
            check("daily_loss_dollars", not is_entry or (isinstance(daily_loss_dollars, (int, float)) and float(daily_loss_dollars) < float(daily_dollar_limit)), "daily dollar loss limit")
        if weekly_dollar_limit is not None:
            check("weekly_loss_dollars", not is_entry or (isinstance(weekly_loss_dollars, (int, float)) and float(weekly_loss_dollars) < float(weekly_dollar_limit)), "weekly dollar loss limit")

        created = _dt(proposal.get("created_at"))
        expires = _dt(proposal.get("expires_at"))
        check("signal_time", not is_entry or (created is not None and created <= now and expires is not None and expires > now), "entry signal/proposal must be current")
        # Production injects the current persisted registry authorization on
        # every evaluation.  The old configuration list remains only as a
        # compatibility fallback for isolated historical fixtures.
        runtime_authorized = self.config.get("runtime_authorized_strategy_versions")
        approved = (
            runtime_authorized
            if runtime_authorized is not None
            else self.config.get("approved_strategy_versions", ["rule_based_v1", "rule_based_v2"])
        )
        check(
            "strategy",
            not is_entry or proposal.get("strategy_version") in approved,
            "current strategy execution registry authorization required",
        )
        if is_entry and proposal.get("phase4_mode") == "exploration":
            check("phase4_exploration_manual_approval", not final or context.get("approval_valid") is True,
                  "Phase 4 exploration requires explicit manual approval")
            check("phase4_exploration_score_sizing", proposal.get("score_multiplier", 1.0) == 1.0,
                  "Phase 4 exploration cannot use score-based sizing")
        if is_entry and (proposal.get("phase4_mode") == "probe" or proposal.get("strategy_state") == "PROBE"):
            phase4 = self.config.get("phase4", {}) or {}
            equity = context.get("portfolio_equity")
            check("phase4_probe_state", proposal.get("strategy_state") == "PROBE", "PROBE requires an explicit persisted PROBE strategy state")
            check("phase4_probe_policy_version", proposal.get("strategy_policy_version") == STRATEGY_POLICY_VERSION, "PROBE requires the current strategy policy version")
            check("phase4_probe_entry_only", not is_add and str(proposal.get("action", "entry")) == "entry", "PROBE permits new entries only; adds are blocked")
            score = proposal.get("score")
            check("phase4_probe_setup_score", isinstance(score, (int, float)) and float(score) >= float(phase4.get("probe_min_setup_score", 85)), "PROBE setup trade score must be at least 85")
            check("phase4_probe_score_sizing", proposal.get("score_multiplier", 1.0) == 1.0, "PROBE cannot use score-based sizing")
            check("phase4_probe_manual_only", phase4.get("require_manual_approval") is True and self.config.get("auto_execution_enabled", False) is False, "PROBE requires manual Telegram approval and cannot execute autonomously")
            check("phase4_probe_manual_approval", not final or context.get("approval_valid") is True, "PROBE requires explicit manual Telegram approval")
            check("phase4_probe_active_count", isinstance(context.get("probe_projected_count"), int) and int(context["probe_projected_count"]) <= int(phase4.get("probe_max_active_count", 1)), "PROBE active position or reserved-intent limit")
            check("phase4_probe_heat", isinstance(equity, (int, float)) and float(equity) > 0 and isinstance(context.get("probe_projected_stop_risk"), (int, float)) and float(context["probe_projected_stop_risk"]) <= float(equity) * float(phase4.get("probe_portfolio_heat_pct", 0.10)) / 100.0 + 1e-9, "PROBE portfolio heat cap")
            check("phase4_probe_gross", isinstance(equity, (int, float)) and float(equity) > 0 and isinstance(context.get("probe_projected_gross_notional"), (int, float)) and float(context["probe_projected_gross_notional"]) <= float(equity) * float(phase4.get("probe_gross_exposure_pct", 2.5)) / 100.0 + 1e-9, "PROBE gross exposure cap")
            adv = proposal.get("average_dollar_volume")
            minimum_adv = float((self.config.get("phase3", {}).get("risk_profile", {}) or {}).get("minimum_average_dollar_volume", 10_000_000.0))
            check("phase4_probe_liquidity", isinstance(adv, (int, float)) and float(adv) >= minimum_adv, "PROBE requires the existing Phase 3 liquidity floor")
        check("reason", not is_entry or bool(proposal.get("reason")), "entry strategy reason required")
        check("side", str(proposal.get("side", "")).lower() in {"buy", "sell"}, "side must be buy or sell")
        check("order_type", proposal.get("order_type", "market") in self.risk.get("allowed_order_types", ["market", "limit"]), "allowed order type required")
        protective_exit = str(context.get("execution_path") or proposal.get("execution_path") or "") == "protective_paper_exit"
        if final and not protective_exit:
            quote_complete = all(proposal.get(key) is not None for key in ("quote_bid", "quote_ask", "quote_timestamp", "quote_spread_bps", "limit_price"))
            check("authoritative_quote", quote_complete, "fresh authoritative quote is required for normal orders")
            check("bounded_limit_order", proposal.get("order_type") == "limit", "normal orders must use bounded marketable-limit orders")
        buying_power = context.get("buying_power")
        check("buying_power", (not is_entry) or (notional is not None and isinstance(buying_power, (int, float)) and float(buying_power) >= float(notional)), "sufficient buying power required for entries")
        check("client_order_id", bool(proposal.get("client_order_id")) if final else True, "unique client order ID required at final validation")
        if final:
            check("final_revalidation", context.get("final_revalidation") is True, "final revalidation marker required")
            check("approval", context.get("approval_valid") is True, "valid unused approval required")
        return RiskDecision(all(c.passed for c in checks), tuple(checks))

    def check_trade(self, proposal: dict[str, Any], context: dict[str, Any], final: bool = False) -> RiskDecision:
        return self.evaluate(proposal, context, final)
