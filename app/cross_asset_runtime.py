"""Runtime adapter for the exact cross-asset advisory allocator.

The allocator itself is deliberately pure and non-authoritative.  This module
only translates already verified broker/research evidence into its Decimal
boundary and persists the resulting advisory plan.  It never creates a
proposal, approval, reservation, intent, or broker request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping, Sequence

from .cross_asset_allocation import (
    CrossAssetAllocationPlan,
    CrossAssetAllocationStore,
    CrossAssetCandidate,
    CrossAssetPortfolioSnapshot,
)
from .crypto_capabilities import CryptoCapabilityStore
from .crypto_market_data import CryptoMarketDataStore
from .crypto_strategies import CryptoStrategyStore
from .internet import internet_available
from .power import get_power_status
from .profitability_ranking import CandidateProfitabilityStore
from .utils import kill_switch_active


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
HUNDRED = Decimal("100")


class CrossAssetRuntimeError(RuntimeError):
    """Raised when current evidence cannot form an authoritative snapshot."""


def _raw(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if not isinstance(value, Mapping) and hasattr(value, name):
            return getattr(value, name)
    return default


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise CrossAssetRuntimeError(f"{label} is missing")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CrossAssetRuntimeError(f"{label} is invalid") from exc
    if not number.is_finite() or (positive and number <= ZERO) or (not positive and number < ZERO):
        raise CrossAssetRuntimeError(f"{label} is outside its safe range")
    return number


def _text(value: Decimal) -> str:
    return "0" if value == ZERO else format(value.normalize(), "f")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CrossAssetRuntimeError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise CrossAssetRuntimeError(f"{label} timestamp has no timezone")
    return parsed.astimezone(UTC)


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "/")


class CrossAssetRuntimeCoordinator:
    """Build and persist one current advisory allocation plan."""

    def __init__(
        self,
        storage: Any,
        config: Mapping[str, Any],
        broker: Any,
        *,
        cluster_resolver: Any | None = None,
        run_id: str,
    ) -> None:
        self.storage = storage
        self.config = config
        self.broker = broker
        self.cluster_resolver = cluster_resolver
        self.run_id = str(run_id)

    def create_plan(
        self,
        *,
        equity_candidates: Sequence[Mapping[str, Any]] = (),
        crypto_results: Sequence[Any] = (),
        positions: Sequence[Any] | None = None,
        orders: Sequence[Any] | None = None,
        account: Any | None = None,
        now: datetime,
    ) -> CrossAssetAllocationPlan:
        current = now.astimezone(UTC)
        current_positions = list(positions if positions is not None else self.broker.get_positions())
        current_orders = list(orders if orders is not None else self.broker.get_open_orders())
        current_account = account if account is not None else self.broker.get_account()
        held = self._held_positions(current_positions)
        portfolio = self._portfolio_snapshot(current_account, current_positions, current_orders, held, current)
        candidates = [
            *self._equity_candidates(equity_candidates, held, current),
            *self._crypto_candidates(crypto_results, held, current_account, current_orders, portfolio, current),
        ]
        return CrossAssetAllocationStore(self.storage, self.config).create(
            run_id=self.run_id,
            candidates=candidates,
            portfolio=portfolio,
            as_of=current.isoformat(),
        )

    def _crypto_symbols(self) -> set[str]:
        return {_symbol(value) for value in (self.config.get("crypto") or {}).get("symbols") or ()}

    def _cluster(self, symbol: str, *, crypto: bool) -> str:
        if crypto:
            return "crypto_major"
        if callable(self.cluster_resolver):
            try:
                value = self.cluster_resolver(symbol)
                if value:
                    return str(value).strip().lower()
            except Exception:
                pass
        return "equity_unclassified"

    def _held_positions(self, positions: Sequence[Any]) -> dict[str, dict[str, Any]]:
        crypto_symbols = self._crypto_symbols()
        held: dict[str, dict[str, Any]] = {}
        for index, position in enumerate(positions):
            symbol = _symbol(_raw(position, "symbol"))
            if not symbol:
                raise CrossAssetRuntimeError(f"broker position {index} has no symbol")
            quantity = _decimal(_raw(position, "qty", "quantity"), f"position {symbol} quantity")
            if quantity <= ZERO:
                continue
            raw_value = _raw(position, "market_value", "marketValue", default=None)
            if raw_value in (None, ""):
                price = _decimal(_raw(position, "current_price", "currentPrice"), f"position {symbol} current price", positive=True)
                market_value = quantity * price
            else:
                market_value = _decimal(raw_value, f"position {symbol} market value", positive=True)
            asset_class_raw = str(_raw(position, "asset_class", "class", default="") or "").lower()
            crypto = symbol in crypto_symbols or asset_class_raw == "crypto"
            asset_class = "crypto" if crypto else "equity"
            if symbol in held:
                raise CrossAssetRuntimeError(f"duplicate held broker position: {symbol}")
            held[symbol] = {
                "symbol": symbol,
                "quantity": quantity,
                "market_value": market_value,
                "asset_class": asset_class,
                "cluster": self._cluster(symbol, crypto=crypto),
                "strategy_version": str(_raw(position, "strategy_version", default="") or "unclassified"),
                "annualized_volatility": _raw(position, "annualized_volatility", default=None),
                "average_entry_price": _raw(
                    position,
                    "avg_entry_price",
                    "average_entry_price",
                    "average_price",
                    default=None,
                ),
            }
        return held

    def _paper_account_hash(self, account: Any) -> str:
        identity = self.broker.paper_account_identity()
        account_id = str(_raw(account, "id", "account_number", default="") or "")
        expected = hashlib.sha256(account_id.encode("utf-8")).hexdigest() if account_id else ""
        if (
            not isinstance(identity, Mapping)
            or identity.get("verified") is not True
            or identity.get("mode") != "paper"
            or identity.get("endpoint_class") != "paper"
            or identity.get("account_id_hash") != expected
        ):
            raise CrossAssetRuntimeError("paper account identity is not verified")
        return expected

    def _loss_snapshot(self, equity: Decimal) -> tuple[Decimal, Decimal, bool]:
        try:
            metrics = self.broker.get_loss_metrics()
            if not isinstance(metrics, Mapping):
                raise CrossAssetRuntimeError("loss metrics shape is invalid")
            daily = _decimal(metrics.get("daily_loss_dollars"), "daily loss")
            weekly = _decimal(metrics.get("weekly_loss_dollars"), "weekly loss")
            reference = _decimal(metrics.get("reference_equity"), "loss reference equity", positive=True)
            fresh = (
                metrics.get("daily_loss_confidence") == "verified"
                and metrics.get("weekly_loss_confidence") == "verified"
                and metrics.get("metrics_version") == "loss_controls_v2"
                and reference > ZERO
            )
            return daily / reference * HUNDRED, weekly / reference * HUNDRED, fresh
        except Exception:
            return ZERO, ZERO, False

    def _drawdown(self, equity: Decimal) -> Decimal:
        peak = equity
        try:
            rows = self.storage.fetch_all(
                "SELECT peak_equity_decimal value FROM account_equity_watermarks WHERE peak_equity_decimal IS NOT NULL"
            )
            for row in rows:
                if row.get("value") not in (None, ""):
                    peak = max(peak, _decimal(row["value"], "peak equity", positive=True))
        except Exception:
            # A missing or malformed exact watermark must not silently widen
            # risk.  The current broker equity remains the conservative floor;
            # the allocator's independent drawdown halt still applies.
            return HUNDRED
        return max(ZERO, (peak - equity) / peak * HUNDRED) if peak > ZERO else HUNDRED

    def _portfolio_snapshot(
        self,
        account: Any,
        positions: Sequence[Any],
        orders: Sequence[Any],
        held: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> CrossAssetPortfolioSnapshot:
        status = str(_raw(account, "status", default="") or "").lower()
        currency = str(_raw(account, "currency", default="") or "").upper()
        equity = _decimal(_raw(account, "equity"), "paper account equity", positive=True)
        cash = _decimal(_raw(account, "cash"), "paper account cash")
        buying_power = _decimal(
            _raw(account, "non_marginable_buying_power", "buying_power", default=None),
            "paper account buying power",
        )
        if status != "active" or currency != "USD" or cash > equity:
            raise CrossAssetRuntimeError("paper account is not active USD cash-only authority")
        account_hash = self._paper_account_hash(account)
        daily_loss_pct, weekly_loss_pct, loss_fresh = self._loss_snapshot(equity)
        gross = sum((item["market_value"] for item in held.values()), ZERO)
        stop_heat = gross
        counts = {"equity": 0, "crypto": 0}
        asset_exposure = {"equity": ZERO, "crypto": ZERO}
        asset_heat = {"equity": ZERO, "crypto": ZERO}
        symbol_exposure: dict[str, Decimal] = {}
        cluster_exposure: dict[str, Decimal] = {}
        strategy_heat: dict[str, Decimal] = {}
        for item in held.values():
            asset = str(item["asset_class"])
            value = item["market_value"]
            counts[asset] += 1
            asset_exposure[asset] += value
            asset_heat[asset] += value
            symbol_exposure[item["symbol"]] = value
            cluster_exposure[item["cluster"]] = cluster_exposure.get(item["cluster"], ZERO) + value
            strategy = item["strategy_version"] or "unclassified"
            strategy_heat[strategy] = strategy_heat.get(strategy, ZERO) + value
        for cluster in ("crypto_major", "equity_unclassified"):
            cluster_exposure.setdefault(cluster, ZERO)
        strategy_heat.setdefault("unclassified", ZERO)
        health = {
            "database": False,
            "internet": False,
            "power": False,
            "broker": True,
        }
        try:
            health["database"] = bool(self.storage.writable())
        except Exception:
            pass
        try:
            health["internet"] = bool(internet_available())
        except Exception:
            pass
        try:
            health["power"] = get_power_status().connected is True
        except Exception:
            pass
        try:
            health["kill_switch"] = bool(kill_switch_active())
        except Exception:
            health["kill_switch"] = True
        # With no open positions, portfolio volatility is exactly zero.  When
        # holdings exist, use a conservative configured sleeve ceiling unless
        # the broker supplied per-position annualized volatility evidence.
        volatility_load = ZERO
        for item in held.values():
            raw_vol = item.get("annualized_volatility")
            if raw_vol not in (None, ""):
                vol = _decimal(raw_vol, f"{item['symbol']} annualized volatility")
            else:
                vol = _decimal(
                    (self.config.get("cross_asset_allocation") or {}).get(
                        "maximum_crypto_annualized_volatility" if item["asset_class"] == "crypto" else "maximum_equity_annualized_volatility"
                    ),
                    f"{item['symbol']} conservative volatility ceiling",
                )
            volatility_load += value_fraction(item["market_value"], equity) * vol
        snapshot_payload = {
            "run_id": self.run_id,
            "as_of": now.isoformat(),
            "symbols": sorted(symbol_exposure),
            "gross": _text(gross),
            "stop_heat": _text(stop_heat),
            "account_hash": account_hash,
        }
        return CrossAssetPortfolioSnapshot(
            snapshot_id=_hash(snapshot_payload)[:32],
            snapshot_fingerprint=_hash(snapshot_payload),
            authoritative=True,
            paper_account_id_hash=account_hash,
            as_of=now.isoformat(),
            equity=_text(equity), cash=_text(cash), buying_power=_text(buying_power),
            gross_exposure=_text(gross), stop_heat=_text(stop_heat),
            daily_loss_pct=_text(max(ZERO, daily_loss_pct)),
            weekly_loss_pct=_text(max(ZERO, weekly_loss_pct)),
            drawdown_pct=_text(self._drawdown(equity)),
            portfolio_annualized_volatility=_text(volatility_load),
            position_count=len(held), asset_class_position_count=counts,
            symbol_exposure={key: _text(value) for key, value in symbol_exposure.items()},
            cluster_exposure={key: _text(value) for key, value in cluster_exposure.items()},
            asset_class_exposure={key: _text(value) for key, value in asset_exposure.items()},
            asset_class_stop_heat={key: _text(value) for key, value in asset_heat.items()},
            strategy_stop_heat={key: _text(value) for key, value in strategy_heat.items()},
            kill_switch_active=bool(health["kill_switch"]),
            loss_evidence_fresh=loss_fresh,
            database_healthy=bool(health["database"]),
            internet_healthy=bool(health["internet"]),
            power_healthy=bool(health["power"]),
            broker_healthy=bool(health["broker"]),
            config_hash=str(self.config.get("effective_config_hash") or ""),
        )

    def _equity_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
        held: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> list[CrossAssetCandidate]:
        result: list[CrossAssetCandidate] = []
        store = CandidateProfitabilityStore(self.storage)
        formulas = self.config.get("formula_versions") or {}
        for raw in candidates:
            decision_id = str(raw.get("profitability_decision_id") or "")
            if not decision_id:
                continue
            try:
                decision = store.load_verified(decision_id)
                source = decision.economics.candidate
                metrics = decision.economics.metrics
                symbol = str(source["symbol"]).upper()
                current_position = symbol in held
                action = str(source.get("action") or "entry").lower()
                if current_position != (action == "add"):
                    continue
                expected_net_profit = _decimal(metrics["expected_net_profit"], f"{symbol} expected net profit")
                expected_downside = _decimal(metrics["expected_downside"], f"{symbol} economic risk", positive=True)
                stop_risk = _decimal(metrics["maximum_approved_loss"], f"{symbol} stop risk", positive=True)
                result.append(CrossAssetCandidate(
                    candidate_id=f"equity:{symbol}:{decision.id}",
                    source_type="candidate_profitability_decision",
                    source_id=decision.id,
                    source_fingerprint=decision.decision_fingerprint,
                    source_authoritative=True,
                    run_id=self.run_id,
                    asset_class=str(source["asset_class"]).lower(), symbol=symbol,
                    cluster=self._cluster(symbol, crypto=False),
                    strategy_version=str(source["strategy_version"]),
                    strategy_state=str(source["strategy_state"]), action=action,
                    execution_lane="operational_paper", evidence_as_of=str(source["estimated_at"]),
                    proposed_notional=_decimal(source["proposed_notional"], f"{symbol} proposed notional", positive=True),
                    economic_risk_dollars=expected_downside, stop_risk_dollars=stop_risk,
                    expected_net_profit=expected_net_profit,
                    expected_net_r=metrics["expected_net_r"],
                    conservative_expected_net_r=metrics["conservative_expected_net_r"],
                    expected_capital_efficiency=metrics["expected_capital_efficiency"],
                    expected_r_per_day=metrics["expected_r_per_day"],
                    marginal_portfolio_contribution_r=metrics["marginal_portfolio_contribution_r"],
                    probability_positive_return=metrics["expected_win_probability"],
                    probability_severe_loss=max(ZERO, ONE - _decimal(metrics["expected_win_probability"], f"{symbol} win probability")),
                    uncertainty=Decimal("0.20"), cost_to_gross_edge_ratio=metrics["cost_to_gross_edge_ratio"] or "1",
                    expected_holding_days=metrics["expected_holding_period_days"],
                    annualized_volatility=source["annualized_volatility"],
                    liquidity_notional=source["average_dollar_volume"], correlation_to_portfolio=Decimal("0.20"),
                    marginal_drawdown_r=max(ZERO, _decimal(metrics.get("maximum_drawdown_r") or "0", f"{symbol} drawdown R")),
                    current_position=current_position, conflict_free=True,
                    profitability_eligible=bool(decision.profitability_eligible),
                    config_hash=str(self.config.get("effective_config_hash") or ""),
                    formula_versions={
                        "trade_economics": formulas["trade_economics"],
                        "profitability_ranking": formulas["profitability_ranking"],
                        "cross_asset_allocation": formulas["cross_asset_allocation"],
                    },
                ))
            except Exception:
                # Missing or stale equity evidence cannot become an advisory
                # candidate.  The ordinary equity pipeline remains unchanged.
                continue
        return result

    def _crypto_candidates(
        self,
        results: Sequence[Any],
        held: Mapping[str, Mapping[str, Any]],
        account: Any,
        orders: Sequence[Any],
        portfolio: CrossAssetPortfolioSnapshot,
        now: datetime,
    ) -> list[CrossAssetCandidate]:
        result: list[CrossAssetCandidate] = []
        formulas = self.config.get("formula_versions") or {}
        equity = _decimal(portfolio.equity, "portfolio equity", positive=True)
        crypto_cfg = self.config.get("crypto") or {}
        sizing_cfg = crypto_cfg.get("sizing_policy") or {}
        allocation_cfg = self.config.get("cross_asset_allocation") or {}
        fee_bps = _decimal(sizing_cfg.get("conservative_taker_fee_bps_per_side"), "crypto fee bps")
        slippage_bps = _decimal(sizing_cfg.get("stop_execution_slippage_bps"), "crypto slippage bps")
        max_order = _decimal(sizing_cfg.get("maximum_order_notional_usd"), "crypto maximum order", positive=True)
        current_crypto_exposure = Decimal(portfolio.asset_class_exposure["crypto"])
        max_crypto_exposure = equity * _decimal(allocation_cfg.get("maximum_crypto_exposure_pct"), "cross asset crypto exposure") / HUNDRED
        for research in results:
            if not getattr(research, "strategy_signal_eligible", False):
                continue
            if not getattr(research, "strategy_decision_id", None) or not getattr(research, "market_evidence_id", None):
                continue
            try:
                capability = CryptoCapabilityStore(self.storage).load_verified(
                    str(research.capability_snapshot_id), self.config, now=now
                )
                market = CryptoMarketDataStore(self.storage).load_verified(
                    str(research.market_evidence_id), self.config
                )
                strategy = CryptoStrategyStore(self.storage).load_verified(
                    str(research.strategy_decision_id), self.config
                )
                if not capability.authoritative or not market.authoritative or not market.execution_eligible:
                    continue
                entry = _decimal(market.ask_price, f"{market.symbol} ask", positive=True)
                bid = _decimal(market.bid_price, f"{market.symbol} bid", positive=True)
                stop = _decimal(strategy.stop_price, f"{market.symbol} stop", positive=True)
                target = _decimal(strategy.target_price, f"{market.symbol} target", positive=True)
                if not stop < entry or not target > entry or bid > entry:
                    continue
                volatility = _decimal(research.realized_volatility, f"{market.symbol} volatility")
                liquidity = _decimal(market.top_of_book_notional, f"{market.symbol} liquidity", positive=True)
                held_position = held.get(market.symbol)
                current_position = held_position is not None
                action = "add" if current_position else "entry"
                winner = False
                if held_position:
                    # The broker position is re-read through the held snapshot
                    # only when its average entry was explicitly retained by the
                    # caller; absent cost basis means no ADD authority.
                    raw_average = held_position.get("average_entry_price")
                    if raw_average not in (None, ""):
                        winner = bid > _decimal(raw_average, f"{market.symbol} average entry", positive=True)
                    if not winner:
                        action = "add"
                spread_cost = (entry - bid)
                loss_fraction = (entry - stop) / entry + (fee_bps * Decimal("2") + slippage_bps * Decimal("2")) / BPS
                risk_budget = equity * _decimal(allocation_cfg.get("maximum_crypto_trade_stop_risk_pct"), "cross asset crypto trade risk") / HUNDRED
                exposure_budget = max(ZERO, max_crypto_exposure - current_crypto_exposure)
                proposed = min(max_order, exposure_budget if exposure_budget > ZERO else max_order, risk_budget / loss_fraction if loss_fraction > ZERO else ZERO)
                proposed = proposed.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                if proposed <= ZERO:
                    continue
                quantity = proposed / entry
                downside = quantity * (entry - stop)
                costs = quantity * spread_cost + proposed * (fee_bps * Decimal("2") + slippage_bps * Decimal("2")) / BPS
                probability = Decimal("0.60")
                uncertainty = Decimal("0.15")
                conservative_probability = probability - uncertainty / Decimal("2")
                expected_gross_profit = probability * quantity * (target - entry) - (ONE - probability) * downside
                expected_net_profit = expected_gross_profit - costs
                conservative_profit = conservative_probability * quantity * (target - entry) - (ONE - conservative_probability) * downside - costs
                expected_r = expected_net_profit / downside
                conservative_r = conservative_profit / downside
                cost_ratio = costs / expected_gross_profit if expected_gross_profit > ZERO else ONE
                # Preserve a negative marginal edge as evidence so the
                # optimizer can reject it with an explicit profitability
                # reason instead of failing candidate canonicalization.
                marginal = (
                    conservative_r * (ONE - Decimal("0.20"))
                    if conservative_r >= ZERO
                    else conservative_r
                )
                conflict_free = not any(
                    _symbol(_raw(order, "symbol")) == market.symbol
                    and str(_raw(order, "side", default="")).lower() == "buy"
                    for order in orders
                )
                profitability_eligible = (
                    expected_net_profit > ZERO
                    and conservative_profit > ZERO
                    and (not current_position or winner)
                )
                source_payload = {
                    "strategy": strategy.decision_fingerprint,
                    "market": market.evidence_fingerprint,
                    "capability": capability.snapshot_fingerprint,
                    "config": self.config.get("effective_config_hash"),
                    "action": action,
                }
                result.append(CrossAssetCandidate(
                    candidate_id=f"crypto:{market.symbol}:{strategy.id}:{action}",
                    source_type="crypto_profitability_research", source_id=strategy.id,
                    source_fingerprint=_hash(source_payload), source_authoritative=True,
                    run_id=self.run_id, asset_class="crypto", symbol=market.symbol,
                    cluster="crypto_major", strategy_version=str(strategy.selected_strategy),
                    strategy_state="ACTIVE" if strategy.lifecycle == "PAPER_ACTIVE" else "RESEARCH_ONLY",
                    action=action, execution_lane="supervised_paper", evidence_as_of=market.captured_at,
                    proposed_notional=_text(proposed), economic_risk_dollars=_text(downside),
                    stop_risk_dollars=_text(downside + costs), expected_net_profit=_text(expected_net_profit),
                    expected_net_r=_text(expected_r), conservative_expected_net_r=_text(conservative_r),
                    expected_capital_efficiency=_text(expected_net_profit / proposed),
                    expected_r_per_day=_text(expected_r / Decimal("3")),
                    marginal_portfolio_contribution_r=_text(marginal),
                    probability_positive_return=_text(probability), probability_severe_loss="0.10",
                    uncertainty=_text(uncertainty), cost_to_gross_edge_ratio=_text(cost_ratio),
                    expected_holding_days="3", annualized_volatility=_text(volatility),
                    liquidity_notional=_text(liquidity), correlation_to_portfolio="0.20",
                    marginal_drawdown_r=_text(max(ZERO, (downside + costs) / equity)),
                    current_position=current_position, conflict_free=conflict_free,
                    profitability_eligible=profitability_eligible,
                    config_hash=str(self.config.get("effective_config_hash") or ""),
                    formula_versions={
                        "trade_economics": formulas["trade_economics"],
                        "profitability_ranking": formulas["profitability_ranking"],
                        "cross_asset_allocation": formulas["cross_asset_allocation"],
                    },
                ))
            except Exception:
                continue
        return result


def value_fraction(value: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= ZERO:
        return ZERO
    return value / denominator


__all__ = ["CrossAssetRuntimeCoordinator", "CrossAssetRuntimeError"]
