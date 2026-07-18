from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.approval_authority import canonical_json
from app.configuration import ConfigurationError, validate_config
from app.crypto_capabilities import CryptoCapabilityStore
from app.crypto_market_data import CryptoMarketDataStore
from app.crypto_proposals import (
    CryptoProposalError,
    CryptoProposalStore,
    format_crypto_proposal_preview,
)
from app.crypto_research import CryptoResearchEngine
from app.crypto_risk import CryptoRiskError, CryptoRiskStore
from app.crypto_sizing import CryptoSizingRequest
from app.crypto_strategies import CryptoStrategyError, CryptoStrategyStore, SUPPORTED_STRATEGIES
from app.execution import DurableExecutionStore
from app.formula_versions import CRYPTO_PROPOSAL_SCHEMA_VERSION, CRYPTO_STRATEGY_SCHEMA_VERSION
from app.storage import Storage
from app.utils import load_config


NOW = datetime(2026, 7, 18, 4, 0, tzinfo=UTC)
ACCOUNT_ID = "paper-crypto-strategy-account"
ACCOUNT_HASH = hashlib.sha256(ACCOUNT_ID.encode()).hexdigest()


def _asset(symbol: str):
    return SimpleNamespace(
        id=f"asset-{symbol}", asset_class="crypto", exchange="CRYPTO", symbol=symbol,
        status="active", tradable=True, marginable=False, shortable=False,
        easy_to_borrow=False, fractionable=True, min_order_size="0.0001",
        min_trade_increment="0.0001", price_increment="1",
    )


class Broker:
    def __init__(self, *, stale=False, wide=False, positions=None, volatility_step=D("0.0001")):
        self.stale = stale
        self.wide = wide
        self.positions = list(positions or [])
        self.volatility_step = volatility_step
        self.submit_calls = 0
        self.account = SimpleNamespace(
            id=ACCOUNT_ID, status="active", currency="USD", equity="100000", cash="100000",
            non_marginable_buying_power="100000", account_blocked=False, trading_blocked=False,
        )

    def paper_account_identity(self):
        return {
            "verified": True, "mode": "paper", "endpoint_class": "paper",
            "account_status": "active", "account_currency": "USD",
            "account_id_hash": ACCOUNT_HASH,
        }

    def get_account(self):
        return self.account

    def get_crypto_assets(self):
        return [_asset("BTC/USD"), _asset("ETH/USD")]

    def get_crypto_latest_quote(self, symbol):
        timestamp = NOW - timedelta(minutes=10) if self.stale else NOW
        ask = "101" if self.wide else "100.1"
        return SimpleNamespace(bid_price="100", ask_price=ask, bid_size="20", ask_size="20", timestamp=timestamp)

    def get_crypto_latest_trade(self, symbol):
        timestamp = NOW - timedelta(minutes=10) if self.stale else NOW
        return SimpleNamespace(price="100.05", size="1", timestamp=timestamp)

    def get_crypto_latest_orderbook(self, symbol):
        timestamp = NOW - timedelta(minutes=10) if self.stale else NOW
        ask = "101" if self.wide else "100.1"
        return SimpleNamespace(
            bids=[SimpleNamespace(price="100", size="20")],
            asks=[SimpleNamespace(price=ask, size="20")], timestamp=timestamp,
        )

    def get_positions(self):
        return list(self.positions)

    def get_open_orders(self):
        return []

    def get_loss_metrics(self):
        return {
            "daily_loss_dollars": "0", "weekly_loss_dollars": "0",
            "reference_equity": "100000", "daily_loss_confidence": "verified",
            "weekly_loss_confidence": "verified", "provenance": "isolated_current_broker",
            "metrics_version": "loss_controls_v2",
        }

    def get_crypto_historical_bars(self, symbol, timeframe="1Hour", limit=169):
        price = D("100")
        rows = []
        for index in range(50):
            price *= D("1") + (self.volatility_step if index % 2 == 0 else -self.volatility_step)
            rows.append(SimpleNamespace(symbol=symbol, timestamp=NOW - timedelta(hours=49 - index), close=str(price)))
        return rows

    def submit_order(self, *args, **kwargs):
        self.submit_calls += 1
        raise AssertionError("crypto strategy/proposal research must never submit")


def _bars(*, count=168, future=False, volatile=False):
    rows = []
    price = D("80")
    for index in range(count):
        if volatile:
            price *= D("1.08") if index % 2 == 0 else D("0.93")
        else:
            price += D("0.12")
        timestamp = NOW - timedelta(hours=count - 1 - index)
        if future and index == count - 1:
            timestamp = NOW + timedelta(hours=1)
        rows.append({
            "symbol": "BTC/USD", "timestamp": timestamp, "close": str(price),
            "high": str(price + D("0.10")), "low": str(price - D("0.10")),
        })
    # Bind the strategy close to the current market around $100.
    scale = D("100") / D(rows[-1]["close"])
    for row in rows:
        for key in ("close", "high", "low"):
            row[key] = str(D(row[key]) * scale)
    return rows


def _storage(tmp_path):
    storage = Storage(tmp_path / "crypto-strategy-proposal.sqlite3")
    storage.initialize()
    return storage


def _config():
    return deepcopy(load_config())


def _market(storage, config, broker, *, research_run="research"):
    capability = CryptoCapabilityStore(storage).capture(config, broker, "run", now=NOW)
    storage.execute(
        """INSERT OR IGNORE INTO crypto_research_runs(
          id,run_id,status,started_at,symbols,provider,capability_snapshot_id,
          capability_snapshot_fingerprint,capability_authoritative,payload
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            research_run, "run", "running", NOW.isoformat(), '["BTC/USD"]', "alpaca",
            capability.id, capability.snapshot_fingerprint, int(capability.authoritative), '{}',
        ),
    )
    market = CryptoMarketDataStore(storage).capture(
        config, broker, capability, "run", research_run, "BTC/USD", now=NOW
    )
    return capability, market


def _strategy(storage, config, broker, *, bars=None, research_run="research"):
    capability, market = _market(storage, config, broker, research_run=research_run)
    decision = CryptoStrategyStore(storage).evaluate(
        config, "run", research_run, market.id, bars or _bars(), now=NOW
    )
    return capability, market, decision


def _risk(storage, config, broker, capability, market, decision, *, source_fingerprint=None):
    request = CryptoSizingRequest(
        source_type="crypto_strategy_decision", source_id=decision.id,
        source_fingerprint=source_fingerprint or decision.decision_fingerprint,
        symbol="BTC/USD", side="buy", action="entry", request_basis="notional",
        requested_stop_risk_dollars=D("1"), stop_price=decision.stop_price,
    )
    return CryptoRiskStore(storage).evaluate(
        config, broker, "run", capability.id, market.id, request, now=NOW
    )


def _complete(tmp_path, *, positions=None):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker(positions=positions)
    capability, market, strategy = _strategy(storage, config, broker)
    risk = _risk(storage, config, broker, capability, market, strategy)
    preview = CryptoProposalStore(storage).create_preview(config, strategy.id, risk.decision_id, now=NOW)
    return storage, config, broker, strategy, risk, preview


def test_default_config_keeps_strategy_and_proposal_stage_dormant():
    config = _config()
    assert validate_config(config) == []
    assert config["crypto"]["strategy_policy"]["lifecycle"] == "RESEARCH_ONLY"
    assert config["crypto"]["proposal_policy"]["manual_approval_enabled"] is False
    assert config["crypto"]["proposal_policy"]["execution_enabled"] is False


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("strategy_policy", "lifecycle", "PROBE"),
        ("proposal_policy", "create_trade_proposals", True),
        ("proposal_policy", "send_telegram", True),
        ("proposal_policy", "manual_approval_enabled", True),
        ("proposal_policy", "execution_enabled", True),
    ],
)
def test_configuration_rejects_crypto_authority_enablement_in_this_stage(section, key, value):
    config = _config()
    config["crypto"][section][key] = value
    with pytest.raises(ConfigurationError):
        validate_config(config)


def test_four_crypto_strategies_are_evaluated_point_in_time(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    _, _, decision = _strategy(storage, config, Broker())
    assert tuple(item["strategy"] for item in decision.payload["evaluations"]) == SUPPORTED_STRATEGIES
    assert decision.selected_strategy in SUPPORTED_STRATEGIES
    assert decision.signal_eligible is True
    assert decision.lifecycle == "RESEARCH_ONLY"
    assert decision.proposal_authorized is False
    assert decision.execution_authorized is False


def test_hourly_crypto_research_persists_strategy_decision_without_proposal(tmp_path):
    storage = _storage(tmp_path)
    config = _config()

    class ResearchBroker(Broker):
        def get_crypto_historical_bars(self, symbol, timeframe="1Hour", limit=500):
            rows = _bars(count=max(168, limit))
            for row in rows:
                row["symbol"] = symbol
                row["volume"] = "1000"
            return rows

    broker = ResearchBroker()
    result = CryptoResearchEngine(config, storage, broker=broker, run_id="run").run_research(
        symbols=["BTC/USD"], now=NOW
    )[0]
    assert result.strategy_decision_id
    assert result.selected_strategy in SUPPORTED_STRATEGIES
    assert result.strategy_lifecycle == "RESEARCH_ONLY"
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_strategy_decisions")[0]["n"] == 1
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_proposal_previews")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM trade_proposals")[0]["n"] == 0
    assert broker.submit_calls == 0


def test_future_bar_fails_closed_without_strategy_row(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    _, market = _market(storage, config, Broker())
    with pytest.raises(CryptoStrategyError, match="future information"):
        CryptoStrategyStore(storage).evaluate(config, "run", "research", market.id, _bars(future=True), now=NOW)
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_strategy_decisions")[0]["n"] == 0


def test_insufficient_strategy_history_fails_closed(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    _, market = _market(storage, config, Broker())
    with pytest.raises(CryptoStrategyError, match="history is insufficient"):
        CryptoStrategyStore(storage).evaluate(config, "run", "research", market.id, _bars(count=100), now=NOW)


def test_previously_fresh_market_evidence_cannot_be_reused_after_it_stales(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    _, market = _market(storage, config, Broker())
    with pytest.raises(CryptoStrategyError, match="no longer fresh"):
        CryptoStrategyStore(storage).evaluate(
            config, "run", "research", market.id, _bars(), now=NOW + timedelta(minutes=10)
        )
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_strategy_decisions")[0]["n"] == 0


def test_extreme_strategy_volatility_blocks_signal(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    _, _, decision = _strategy(storage, config, Broker(), bars=_bars(volatile=True))
    assert decision.signal_eligible is False
    assert "crypto_strategy_volatility_above_policy" in decision.blockers


def test_strategy_loader_recomputes_persisted_bars_not_only_local_digest(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    _, _, decision = _strategy(storage, config, Broker())
    row = storage.fetch_all("SELECT decision_json FROM crypto_strategy_decisions WHERE id=?", (decision.id,))[0]
    payload = json.loads(row["decision_json"])
    payload["input"]["bars"][0]["close"] = "1"
    payload["input"]["bar_fingerprint"] = hashlib.sha256(canonical_json(payload["input"]["bars"]).encode()).hexdigest()
    payload["input_fingerprint"] = hashlib.sha256(canonical_json(payload["input"]).encode()).hexdigest()
    local = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    storage.execute(
        "UPDATE crypto_strategy_decisions SET decision_json=?,decision_fingerprint=?,input_fingerprint=? WHERE id=?",
        (canonical_json(payload), local, payload["input_fingerprint"], decision.id),
    )
    with pytest.raises(CryptoStrategyError):
        CryptoStrategyStore(storage).load_verified(decision.id, config)


def test_preview_binds_strategy_risk_sizing_and_complete_display(tmp_path):
    storage, config, broker, strategy, risk, preview = _complete(tmp_path)
    assert preview.strategy_decision_id == strategy.id
    assert preview.risk_decision_id == risk.decision_id
    assert preview.sizing_decision_id == risk.sizing.id
    assert preview.manual_approval_eligible is False
    assert preview.execution_authorized is False
    required = {
        "symbol", "strategy", "action", "request_basis", "current_bid", "current_ask",
        "spread_bps", "annualized_volatility", "stop_price", "expected_reward_usd",
        "maximum_loss_usd", "expected_execution_cost_usd", "current_crypto_exposure_usd",
        "projected_crypto_exposure_usd", "current_total_portfolio_exposure_usd",
        "projected_total_portfolio_exposure_usd", "expires_at", "would_be_approval_command",
        "paper_only_warning",
    }
    assert required <= preview.display.keys()
    assert preview.display["approval_command_enabled"] is False
    gross_reward = D(preview.display["expected_reward_usd"])
    maximum_loss = D(preview.display["maximum_loss_usd"])
    assert gross_reward / maximum_loss == D(preview.display["expected_reward_r"])
    assert D(preview.display["expected_reward_r"]) >= D("1.75")
    assert D(preview.display["expected_net_reward_after_estimated_cost_usd"]) > 0
    assert broker.submit_calls == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM trade_proposals")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM approvals")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM order_intents")[0]["n"] == 0
    assert storage.fetch_all("SELECT COUNT(*) n FROM risk_reservations")[0]["n"] == 0


def test_preview_formatter_contains_exact_disabled_approval_surface(tmp_path):
    *_, preview = _complete(tmp_path)
    message = format_crypto_proposal_preview(preview)
    for text in (
        "BTC/USD", preview.strategy, "spread", "Maximum loss", "expected execution cost",
        "Crypto exposure", "Total exposure", "Would-be command", "DISABLED", "PAPER ONLY",
    ):
        assert text in message


def test_preview_exposure_math_uses_authoritative_portfolio_snapshot(tmp_path):
    position = SimpleNamespace(
        symbol="ETHUSD", asset_class="crypto", qty="0.01", market_value="20",
        current_price="2000", side="long",
    )
    *_, preview = _complete(tmp_path, positions=[position])
    current_crypto = D(preview.display["current_crypto_exposure_usd"])
    projected_crypto = D(preview.display["projected_crypto_exposure_usd"])
    current_total = D(preview.display["current_total_portfolio_exposure_usd"])
    projected_total = D(preview.display["projected_total_portfolio_exposure_usd"])
    notional = D(preview.display["notional_usd"])
    assert current_crypto == current_total == D("20")
    assert projected_crypto == current_crypto + notional
    assert projected_total == current_total + notional


def test_wrong_strategy_source_binding_cannot_create_preview(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    capability, market, strategy = _strategy(storage, config, broker)
    risk = _risk(storage, config, broker, capability, market, strategy, source_fingerprint="f" * 64)
    with pytest.raises(CryptoProposalError, match="does not bind the exact strategy"):
        CryptoProposalStore(storage).create_preview(config, strategy.id, risk.decision_id, now=NOW)
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_proposal_previews")[0]["n"] == 0


def test_mismatched_strategy_and_risk_run_cannot_create_preview(tmp_path):
    storage = _storage(tmp_path)
    config = _config()
    broker = Broker()
    capability, market, strategy = _strategy(storage, config, broker)
    request = CryptoSizingRequest(
        source_type="crypto_strategy_decision", source_id=strategy.id,
        source_fingerprint=strategy.decision_fingerprint, symbol="BTC/USD", side="buy",
        action="entry", request_basis="notional", requested_stop_risk_dollars=D("1"),
        stop_price=strategy.stop_price,
    )
    risk = CryptoRiskStore(storage).evaluate(
        config, broker, "different-run", capability.id, market.id, request, now=NOW
    )
    with pytest.raises(CryptoProposalError, match="run identities differ"):
        CryptoProposalStore(storage).create_preview(config, strategy.id, risk.decision_id, now=NOW)


@pytest.mark.parametrize("broker", [Broker(stale=True), Broker(wide=True)])
def test_stale_or_wide_market_cannot_reach_strategy_or_preview(tmp_path, broker):
    storage = _storage(tmp_path)
    config = _config()
    _, market = _market(storage, config, broker)
    assert market.execution_eligible is False
    with pytest.raises(CryptoStrategyError, match="execution-eligible"):
        CryptoStrategyStore(storage).evaluate(config, "run", "research", market.id, _bars(), now=NOW)
    assert storage.fetch_all("SELECT COUNT(*) n FROM crypto_proposal_previews")[0]["n"] == 0
    assert broker.submit_calls == 0


def test_preview_loader_rejects_tamper_even_with_regenerated_local_digests(tmp_path):
    storage, config, _, _, _, preview = _complete(tmp_path)
    row = storage.fetch_all("SELECT proposal_json FROM crypto_proposal_previews WHERE id=?", (preview.id,))[0]
    payload = json.loads(row["proposal_json"])
    payload["display"]["maximum_loss_usd"] = "0.01"
    payload["display_fingerprint"] = hashlib.sha256(canonical_json(payload["display"]).encode()).hexdigest()
    proposal_fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    storage.execute(
        "UPDATE crypto_proposal_previews SET display_json=?,display_fingerprint=?,proposal_json=?,proposal_fingerprint=? WHERE id=?",
        (canonical_json(payload["display"]), payload["display_fingerprint"], canonical_json(payload), proposal_fingerprint, preview.id),
    )
    with pytest.raises(CryptoProposalError, match="recomputation mismatch"):
        CryptoProposalStore(storage).load_verified(preview.id, config, now=NOW)


def test_current_config_identity_change_invalidates_preview(tmp_path):
    storage, config, _, _, _, preview = _complete(tmp_path)
    changed = deepcopy(config)
    changed["effective_config_hash"] = "b" * 64
    with pytest.raises((CryptoProposalError, CryptoStrategyError, RuntimeError, CryptoRiskError)):
        CryptoProposalStore(storage).load_verified(preview.id, changed, now=NOW)


def test_crypto_strategy_proposal_migrations_are_idempotent_and_integrity_clean(tmp_path):
    storage, _, _, _, _, _ = _complete(tmp_path)
    storage.apply_explicit_migrations()
    first = storage.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    storage.apply_explicit_migrations()
    second = storage.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert first == second
    versions = {row["version"] for row in second}
    assert {CRYPTO_STRATEGY_SCHEMA_VERSION, CRYPTO_PROPOSAL_SCHEMA_VERSION} <= versions
    report = DurableExecutionStore(storage).integrity_report()
    assert all(value == 0 for value in report.values()), report
