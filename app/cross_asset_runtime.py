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
    cold_start_source_fingerprint,
)
from .crypto_capabilities import CryptoCapabilityStore
from .crypto_market_data import CryptoMarketDataStore
from .crypto_strategies import CryptoStrategyStore
from .crypto_outcomes import VerifiedCorrelationSnapshot
from .crypto_profitability import CryptoProfitabilityStore
from .profitability_validation import policy_from_config
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


def _enum_text(value: Any) -> str:
    """Normalize SDK enum values and plain strings at an authority boundary."""

    return str(getattr(value, "value", value) or "").strip().lower()


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
            raw_price = _raw(position, "current_price", "currentPrice", default=None)
            if raw_price not in (None, ""):
                price = _decimal(raw_price, f"position {symbol} current price", positive=True)
                derived_value = quantity * price
                if raw_value not in (None, ""):
                    reported_value = _decimal(raw_value, f"position {symbol} market value", positive=True)
                    tolerance = max(Decimal("0.01"), derived_value * Decimal("0.00000001"))
                    if abs(reported_value - derived_value) > tolerance:
                        raise CrossAssetRuntimeError(
                            f"position {symbol} market value does not reconcile to quantity and current price"
                        )
                market_value = derived_value
            elif raw_value in (None, ""):
                raise CrossAssetRuntimeError(
                    f"position {symbol} needs either current price or market value evidence"
                )
            else:
                # A broker position without a current price is still usable for
                # an exposure snapshot only when its reported value is present;
                # it is deliberately not silently multiplied from a stale or
                # missing price.
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

    def _loss_snapshot(self, equity: Decimal, now: datetime) -> tuple[Decimal, Decimal, bool]:
        try:
            metrics = self.broker.get_loss_metrics()
            if not isinstance(metrics, Mapping):
                raise CrossAssetRuntimeError("loss metrics shape is invalid")
            daily = _decimal(metrics.get("daily_loss_dollars"), "daily loss")
            weekly = _decimal(metrics.get("weekly_loss_dollars"), "weekly loss")
            reference = _decimal(metrics.get("reference_equity"), "loss reference equity", positive=True)
            captured_at = _timestamp(metrics.get("captured_at"), "loss metrics")
            age = Decimal(str((now - captured_at).total_seconds()))
            max_age = Decimal(str((self.config.get("cross_asset_allocation") or {}).get("portfolio_snapshot_ttl_seconds", 300)))
            fresh = (
                metrics.get("daily_loss_confidence") == "verified"
                and metrics.get("weekly_loss_confidence") == "verified"
                and metrics.get("metrics_version") == "loss_controls_v2"
                and reference > ZERO
                and age >= Decimal("-5")
                and age <= max_age
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

    def _authoritative_position_stop_heat(
        self,
        held: Mapping[str, Mapping[str, Any]],
    ) -> tuple[Decimal, dict[str, Decimal], dict[str, Decimal]]:
        """Return open stop risk from the exact FIFO lot ledger.

        Market value is exposure, not stop risk.  A position is therefore not
        allowed into the advisory allocator unless its broker quantity
        reconciles exactly to open FIFO lots carrying canonical initial stop
        risk.  The remaining fraction of each lot retains the corresponding
        fraction of its original stop-risk budget; this is conservative after
        a partial fill or reduction and never reconstructs risk from floats.
        """

        total = ZERO
        by_asset = {"equity": ZERO, "crypto": ZERO}
        by_strategy: dict[str, Decimal] = {}
        for symbol, item in held.items():
            rows = self.storage.fetch_all(
                "SELECT * FROM position_lots WHERE symbol=? ORDER BY opened_at,id",
                (symbol,),
            )
            if not rows:
                raise CrossAssetRuntimeError(
                    f"position {symbol} has no authoritative FIFO lot evidence"
                )
            remaining_total = ZERO
            symbol_risk = ZERO
            for raw in rows:
                row = dict(raw)
                legacy_remaining = row.get("remaining_quantity")
                exact_remaining_raw = row.get("remaining_quantity_decimal")
                if exact_remaining_raw in (None, ""):
                    # Closed legacy rows do not affect the current position;
                    # an open row without its exact projection is unsafe.
                    try:
                        legacy_open = legacy_remaining not in (None, "") and Decimal(str(legacy_remaining)) > ZERO
                    except (InvalidOperation, TypeError, ValueError):
                        legacy_open = True
                    if legacy_open:
                        raise CrossAssetRuntimeError(
                            f"position {symbol} FIFO remaining quantity lacks exact evidence"
                        )
                    continue
                remaining = _decimal(
                    exact_remaining_raw,
                    f"position {symbol} lot remaining quantity",
                )
                original = _decimal(
                    row.get("original_quantity_decimal"),
                    f"position {symbol} lot original quantity",
                    positive=True,
                )
                if remaining > original:
                    raise CrossAssetRuntimeError(
                        f"position {symbol} FIFO lot quantity geometry is invalid"
                    )
                if remaining == ZERO:
                    continue
                risk_raw = row.get("initial_risk_dollars_decimal")
                if risk_raw in (None, ""):
                    raise CrossAssetRuntimeError(
                        f"position {symbol} open FIFO lot lacks exact stop-risk evidence"
                    )
                initial_risk = _decimal(
                    risk_raw,
                    f"position {symbol} lot initial stop risk",
                )
                lot_risk = initial_risk * remaining / original
                remaining_total += remaining
                symbol_risk += lot_risk
                strategy = str(
                    row.get("strategy_version")
                    or item.get("strategy_version")
                    or "unclassified"
                )
                by_strategy[strategy] = by_strategy.get(strategy, ZERO) + lot_risk
            if remaining_total != item["quantity"]:
                raise CrossAssetRuntimeError(
                    f"position {symbol} quantity does not reconcile to exact FIFO lots"
                )
            asset = str(item["asset_class"])
            by_asset[asset] += symbol_risk
            total += symbol_risk
        return total, by_asset, by_strategy

    def _authoritative_active_reservations(
        self,
    ) -> tuple[
        Decimal,
        Decimal,
        dict[str, Decimal],
        dict[str, Decimal],
        dict[str, Decimal],
        dict[str, Decimal],
        dict[str, Decimal],
    ]:
        """Load active generic and crypto reservations from exact Decimal text.

        Cross-asset capacity must include capital and stop-risk already claimed
        by approved intents even before a broker position exists.  The legacy
        REAL columns are never used when an active reservation lacks its exact
        projection; such a row fails closed instead of widening capacity.
        """

        gross = ZERO
        heat = ZERO
        symbol_exposure: dict[str, Decimal] = {}
        cluster_exposure: dict[str, Decimal] = {}
        asset_exposure = {"equity": ZERO, "crypto": ZERO}
        asset_heat = {"equity": ZERO, "crypto": ZERO}
        strategy_heat: dict[str, Decimal] = {}

        generic_rows = self.storage.fetch_all(
            """
            SELECT rr.*
            FROM risk_reservations rr
            WHERE rr.state='active'
            ORDER BY rr.created_at,rr.id
            """
        )
        for row in generic_rows:
            notional_raw = row.get("active_notional_decimal")
            stop_raw = row.get("active_stop_risk_decimal")
            if notional_raw in (None, "") or stop_raw in (None, ""):
                raise CrossAssetRuntimeError("active generic reservation lacks exact Decimal authority")
            notional = _decimal(notional_raw, "active generic reservation notional")
            stop_risk = _decimal(stop_raw, "active generic reservation stop risk")
            symbol = _symbol(row.get("symbol"))
            if not symbol:
                raise CrossAssetRuntimeError("active generic reservation has no symbol")
            crypto = symbol in self._crypto_symbols()
            asset = "crypto" if crypto else "equity"
            cluster = str(row.get("cluster_name") or self._cluster(symbol, crypto=crypto)).strip().lower()
            strategy = str(row.get("strategy_version") or "unclassified")
            gross += notional
            heat += stop_risk
            symbol_exposure[symbol] = symbol_exposure.get(symbol, ZERO) + notional
            cluster_exposure[cluster] = cluster_exposure.get(cluster, ZERO) + notional
            asset_exposure[asset] += notional
            asset_heat[asset] += stop_risk
            strategy_heat[strategy] = strategy_heat.get(strategy, ZERO) + stop_risk

        crypto_rows = self.storage.fetch_all(
            """
            SELECT r.*,i.symbol,i.side,i.proposal_id,p.display_json,p.action
            FROM crypto_paper_reservations r
            JOIN crypto_paper_intents i ON i.id=r.intent_id
            JOIN crypto_paper_proposals p ON p.id=i.proposal_id
            WHERE r.state='active'
            ORDER BY r.created_at,r.id
            """
        )
        for row in crypto_rows:
            notional = _decimal(
                row.get("active_notional"),
                "active crypto reservation notional",
            )
            stop_risk = _decimal(
                row.get("active_stop_risk"),
                "active crypto reservation stop risk",
            )
            symbol = _symbol(row.get("symbol"))
            if not symbol:
                raise CrossAssetRuntimeError("active crypto reservation has no symbol")
            try:
                display = json.loads(row.get("display_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CrossAssetRuntimeError("active crypto reservation display evidence is invalid") from exc
            strategy = str(display.get("strategy") or "crypto_unclassified") if isinstance(display, Mapping) else "crypto_unclassified"
            cluster = self._cluster(symbol, crypto=True)
            gross += notional
            heat += stop_risk
            symbol_exposure[symbol] = symbol_exposure.get(symbol, ZERO) + notional
            cluster_exposure[cluster] = cluster_exposure.get(cluster, ZERO) + notional
            asset_exposure["crypto"] += notional
            asset_heat["crypto"] += stop_risk
            strategy_heat[strategy] = strategy_heat.get(strategy, ZERO) + stop_risk

        return gross, heat, symbol_exposure, cluster_exposure, asset_exposure, asset_heat, strategy_heat

    def _portfolio_snapshot(
        self,
        account: Any,
        positions: Sequence[Any],
        orders: Sequence[Any],
        held: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> CrossAssetPortfolioSnapshot:
        status = _enum_text(_raw(account, "status", default=""))
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
        daily_loss_pct, weekly_loss_pct, loss_fresh = self._loss_snapshot(equity, now)
        gross = sum((item["market_value"] for item in held.values()), ZERO)
        stop_heat, asset_heat, strategy_heat = self._authoritative_position_stop_heat(held)
        (
            reserved_gross,
            reserved_heat,
            reserved_symbol_exposure,
            reserved_cluster_exposure,
            reserved_asset_exposure,
            reserved_asset_heat,
            reserved_strategy_heat,
        ) = self._authoritative_active_reservations()
        gross += reserved_gross
        stop_heat += reserved_heat
        for asset, value in reserved_asset_heat.items():
            asset_heat[asset] = asset_heat.get(asset, ZERO) + value
        for strategy, value in reserved_strategy_heat.items():
            strategy_heat[strategy] = strategy_heat.get(strategy, ZERO) + value
        counts = {"equity": 0, "crypto": 0}
        asset_exposure = {"equity": ZERO, "crypto": ZERO}
        symbol_exposure: dict[str, Decimal] = {}
        cluster_exposure: dict[str, Decimal] = {}
        for item in held.values():
            asset = str(item["asset_class"])
            value = item["market_value"]
            counts[asset] += 1
            asset_exposure[asset] += value
            symbol_exposure[item["symbol"]] = value
            cluster_exposure[item["cluster"]] = cluster_exposure.get(item["cluster"], ZERO) + value
        for symbol, value in reserved_symbol_exposure.items():
            symbol_exposure[symbol] = symbol_exposure.get(symbol, ZERO) + value
        for cluster, value in reserved_cluster_exposure.items():
            cluster_exposure[cluster] = cluster_exposure.get(cluster, ZERO) + value
        for asset, value in reserved_asset_exposure.items():
            asset_exposure[asset] += value
        for cluster in ("crypto_major", "equity_unclassified"):
            cluster_exposure.setdefault(cluster, ZERO)
        strategy_heat.setdefault("unclassified", ZERO)
        health = {
            "database": False,
            "internet": False,
            "power": False,
            "broker": False,
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
            required_methods = (
                "get_account", "get_positions", "get_open_orders", "get_order",
                "get_order_by_client_order_id", "submit_order",
            )
            health["broker"] = all(callable(getattr(self.broker, name, None)) for name in required_methods)
            submission_probe = getattr(self.broker, "submission_available", None)
            if callable(submission_probe):
                health["broker"] = bool(health["broker"] and submission_probe() is True)
            crypto_cfg = self.config.get("crypto") or {}
            if crypto_cfg.get("enabled") and crypto_cfg.get("paper_trading_enabled"):
                submission_probe = getattr(self.broker, "crypto_submission_available", None)
                cancellation_probe = getattr(self.broker, "crypto_cancellation_available", None)
                health["broker"] = bool(
                    health["broker"]
                    and callable(submission_probe)
                    and submission_probe() is True
                    and callable(cancellation_probe)
                    and cancellation_probe() is True
                )
        except Exception:
            health["broker"] = False
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
        authoritative = bool(
            loss_fresh
            and health["database"]
            and health["internet"]
            and health["power"]
            and health["broker"]
            and not health["kill_switch"]
        )
        snapshot_payload = {
            "run_id": self.run_id,
            "as_of": now.isoformat(),
            "symbols": sorted(symbol_exposure),
            "gross": _text(gross),
            "stop_heat": _text(stop_heat),
            "account_hash": account_hash,
            "loss_fresh": loss_fresh,
            "health": health,
        }
        return CrossAssetPortfolioSnapshot(
            snapshot_id=_hash(snapshot_payload)[:32],
            snapshot_fingerprint=_hash(snapshot_payload),
            authoritative=authoritative,
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
        profitability_cfg = crypto_cfg.get("profitability_policy") or {}
        try:
            minimum_samples = int(profitability_cfg["minimum_samples"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CrossAssetRuntimeError("crypto profitability minimum sample policy is unavailable") from exc
        severe_loss_threshold = profitability_cfg.get("severe_loss_threshold")
        minimum_mean_net_return = profitability_cfg.get("minimum_mean_net_return")
        require_verified_correlation = profitability_cfg.get("require_verified_correlation") is True
        fee_bps = _decimal(sizing_cfg.get("conservative_taker_fee_bps_per_side"), "crypto fee bps")
        slippage_bps = _decimal(sizing_cfg.get("stop_execution_slippage_bps"), "crypto slippage bps")
        max_order = _decimal(sizing_cfg.get("maximum_order_notional_usd"), "crypto maximum order", positive=True)
        current_crypto_exposure = Decimal(portfolio.asset_class_exposure["crypto"])
        max_crypto_exposure = equity * _decimal(allocation_cfg.get("maximum_crypto_exposure_pct"), "cross asset crypto exposure") / HUNDRED
        cold_start = self._crypto_cold_start_authority()
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
                correlation_snapshot = self._crypto_correlation_snapshot(
                    strategy.selected_strategy, now,
                    maximum_age_seconds=profitability_cfg.get("correlation_snapshot_max_age_seconds"),
                )
                profitability = CryptoProfitabilityStore(self.storage).build_for_strategy(
                    symbol=market.symbol,
                    strategy_version=str(strategy.selected_strategy or ""),
                    strategy_decision_id=str(strategy.id),
                    strategy_decision_fingerprint=str(strategy.decision_fingerprint),
                    config_hash=str(self.config.get("effective_config_hash") or ""),
                    minimum_samples=minimum_samples,
                    severe_loss_threshold=severe_loss_threshold,
                    minimum_mean_net_return=minimum_mean_net_return,
                    require_verified_correlation=require_verified_correlation,
                    correlation_snapshot=correlation_snapshot,
                    now=now,
                    validation_policy=policy_from_config(self.config),
                    validation_configuration=self.config,
                )
                use_cold_start = cold_start is not None and not profitability.eligible
                if not profitability.eligible and not use_cold_start:
                    self.storage.audit(
                        self.run_id,
                        "crypto_profitability_candidate_excluded",
                        {
                            "symbol": market.symbol,
                            "strategy_version": strategy.selected_strategy,
                            "profitability_decision_id": profitability.decision_id,
                            "reasons": list(profitability.rejection_reasons),
                            "sample_count": profitability.sample_count,
                        },
                    )
                    continue
                if use_cold_start:
                    prior_probability = _decimal(
                        profitability_cfg.get("cold_start_prior_win_probability"),
                        "crypto cold-start prior win probability",
                        minimum=ZERO,
                        maximum=ONE,
                    )
                    prior_uncertainty = _decimal(
                        profitability_cfg.get("cold_start_prior_uncertainty"),
                        "crypto cold-start prior uncertainty",
                        minimum=ZERO,
                        maximum=ONE,
                    )
                    prior_correlation = _decimal(
                        profitability_cfg.get("cold_start_prior_correlation"),
                        "crypto cold-start prior correlation",
                        minimum=Decimal("-1"),
                        maximum=ONE,
                    )
                    probability = prior_probability
                    uncertainty = prior_uncertainty
                    correlation = prior_correlation
                    holding_hours = Decimal("24")
                    source_type = "candidate_cold_start_discovery"
                    source_id = str(strategy.id)
                    source_fingerprint = cold_start_source_fingerprint(
                        source_id=source_id,
                        strategy_version=str(strategy.selected_strategy),
                        symbol=strategy.symbol,
                        config_hash=str(self.config.get("effective_config_hash") or ""),
                    )
                    discovery_cap = cold_start["notional_cap"]
                else:
                    probability = _decimal(
                        profitability.win_probability,
                        f"{market.symbol} verified win probability",
                        positive=True,
                    )
                    uncertainty = _decimal(
                        profitability.uncertainty,
                        f"{market.symbol} verified uncertainty",
                    )
                    correlation = _decimal(
                        profitability.correlation_to_portfolio,
                        f"{market.symbol} portfolio correlation",
                    )
                    holding_hours = _decimal(
                        profitability.average_holding_hours,
                        f"{market.symbol} holding hours",
                        positive=True,
                    )
                    source_type = "candidate_profitability_decision"
                    source_id = str(profitability.decision_id)
                    source_fingerprint = profitability.decision_fingerprint
                    discovery_cap = max_order
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
                proposed = min(
                    max_order,
                    discovery_cap,
                    exposure_budget if exposure_budget > ZERO else max_order,
                    risk_budget / loss_fraction if loss_fraction > ZERO else ZERO,
                )
                proposed = proposed.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                if proposed <= ZERO:
                    continue
                quantity = proposed / entry
                downside = quantity * (entry - stop)
                costs = quantity * spread_cost + proposed * (fee_bps * Decimal("2") + slippage_bps * Decimal("2")) / BPS
                conservative_probability = max(ZERO, probability - uncertainty)
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
                result.append(CrossAssetCandidate(
                    candidate_id=f"crypto:{market.symbol}:{strategy.id}:{action}",
                    source_type=source_type, source_id=source_id,
                    source_fingerprint=source_fingerprint, source_authoritative=True,
                    run_id=self.run_id, asset_class="crypto", symbol=market.symbol,
                    cluster="crypto_major", strategy_version=str(strategy.selected_strategy),
                    strategy_state="ACTIVE" if strategy.lifecycle == "PAPER_ACTIVE" else "RESEARCH_ONLY",
                    action=action, execution_lane="supervised_paper", evidence_as_of=market.captured_at,
                    proposed_notional=_text(proposed), economic_risk_dollars=_text(downside),
                    stop_risk_dollars=_text(downside + costs), expected_net_profit=_text(expected_net_profit),
                    expected_net_r=_text(expected_r), conservative_expected_net_r=_text(conservative_r),
                    expected_capital_efficiency=_text(expected_net_profit / proposed),
                    expected_r_per_day=_text(
                        expected_r / max(
                            Decimal("0.000000000001"),
                            holding_hours / Decimal("24"),
                        )
                    ),
                    marginal_portfolio_contribution_r=_text(marginal),
                    probability_positive_return=_text(probability),
                    probability_severe_loss=_text(
                        _decimal(profitability.severe_loss_rate, f"{market.symbol} severe-loss rate")
                    ),
                    uncertainty=_text(uncertainty), cost_to_gross_edge_ratio=_text(cost_ratio),
                    expected_holding_days=_text(
                        holding_hours / Decimal("24")
                    ), annualized_volatility=_text(volatility),
                    liquidity_notional=_text(liquidity),
                    correlation_to_portfolio=_text(correlation),
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

    def _crypto_cold_start_authority(self) -> dict[str, Any] | None:
        """Return the current bounded discovery cap, or no discovery authority."""

        policy = (self.config.get("crypto") or {}).get("profitability_policy") or {}
        if policy.get("cold_start_enabled") is not True:
            return None
        try:
            maximum_trades = int(policy["cold_start_trade_count"])
            tiers = policy["cold_start_notional_tiers"]
            first_count = int(tiers["first_trade_count"])
            second_count = int(tiers["second_trade_count"])
            first_cap = _decimal(tiers["first_notional_usd"], "crypto cold-start first cap", positive=True)
            second_cap = _decimal(tiers["second_notional_usd"], "crypto cold-start second cap", positive=True)
            final_cap = _decimal(tiers["final_notional_usd"], "crypto cold-start final cap", positive=True)
            rows = self.storage.fetch_all(
                """SELECT net_pnl,net_return
                   FROM crypto_profitability_observations
                   WHERE evidence_type='actual' AND status='completed'
                   ORDER BY exit_timestamp,observation_id"""
            )
            count = len(rows)
            accumulated = ZERO
            for row in rows:
                raw = row.get("net_pnl") if row.get("net_pnl") not in (None, "") else row.get("net_return")
                accumulated += _decimal(raw, "crypto cold-start accumulated evidence")
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            self.storage.audit(
                self.run_id,
                "crypto_cold_start_blocked",
                {"reason": "cold-start policy or evidence is invalid", "error": type(exc).__name__},
            )
            return None
        if (
            maximum_trades <= 0
            or not (0 < first_count < second_count < maximum_trades)
            or count >= maximum_trades
            or accumulated < ZERO
        ):
            return None
        if count < first_count:
            cap = first_cap
        elif count < second_count:
            cap = second_cap
        else:
            cap = final_cap
        return {
            "completed_trades": count,
            "accumulated_evidence": accumulated,
            "notional_cap": cap,
            "maximum_trades": maximum_trades,
        }

    def _crypto_correlation_snapshot(
        self,
        strategy_version: str | None,
        now: datetime,
        *,
        maximum_age_seconds: Any,
    ) -> VerifiedCorrelationSnapshot | None:
        """Return only a current, non-fallback covariance snapshot.

        The phase-4 table stores the strategy order and correlation matrix as
        JSON evidence.  A crypto strategy absent from that order has no
        portfolio-correlation authority; returning ``None`` is intentional.
        """

        if not strategy_version or maximum_age_seconds in (None, ""):
            return None
        try:
            max_age = Decimal(str(maximum_age_seconds))
        except (InvalidOperation, TypeError, ValueError):
            return None
        rows = self.storage.fetch_all(
            "SELECT * FROM phase4_covariance_snapshots WHERE fallback_used=0 ORDER BY calculated_at DESC"
        )
        for row in rows:
            try:
                captured = _timestamp(row["calculated_at"], "covariance snapshot")
                if (now - captured).total_seconds() > float(max_age):
                    continue
                order = json.loads(row["strategy_order_json"])
                matrix = json.loads(row["correlation_json"])
                if not isinstance(order, list) or not isinstance(matrix, list) or strategy_version not in order:
                    continue
                index = order.index(strategy_version)
                values = [
                    _decimal(value, "covariance correlation",)
                    for position, value in enumerate(matrix[index])
                    if position != index
                ]
                if not values:
                    continue
                correlation = sum(values, ZERO) / Decimal(len(values))
                evidence = {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "calculated_at": row["calculated_at"],
                    "strategy_order_json": row["strategy_order_json"],
                    "correlation_json": row["correlation_json"],
                    "method": row["method"],
                    "fallback_used": row["fallback_used"],
                    "data_quality": row["data_quality"],
                }
                return VerifiedCorrelationSnapshot(
                    snapshot_id=str(row["id"]),
                    snapshot_fingerprint=_hash(evidence),
                    correlation=correlation,
                )
            except (KeyError, TypeError, ValueError, InvalidOperation, IndexError, json.JSONDecodeError):
                continue
        return None


def value_fraction(value: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= ZERO:
        return ZERO
    return value / denominator


__all__ = ["CrossAssetRuntimeCoordinator", "CrossAssetRuntimeError"]
