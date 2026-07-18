from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pandas as pd

from app.approval_display import display_envelope
from app.broker_alpaca import AlpacaBroker
from app.configuration import ConfigurationError, validate_config
from app.execution_risk_snapshot import REQUIRED_FORMULA_VERSIONS
from app.quotes import (
    QuoteValidationError,
    quote_failure_reason,
    validated_quote,
)
from app.service import TradingService
from app.storage import Storage
from app.strategy_rule_based import Signal
from app.utils import load_config


class WideQuoteBroker:
    equity_realtime_data_feed = "iex"

    def __init__(self) -> None:
        self.submission_calls = 0
        self.quote_timestamp = datetime.now(UTC)

    def get_latest_quote(self, symbol: str):
        return {
            "bid_price": 237.23,
            "ask_price": 252.07,
            "timestamp": self.quote_timestamp,
            "feed": "iex",
        }

    def is_market_open(self):
        return True

    def get_positions(self):
        return [SimpleNamespace(symbol="ABBV", qty=10.0)]

    def get_open_orders(self):
        return []

    def submit_order(self, *args, **kwargs):
        self.submission_calls += 1
        raise AssertionError("wide quote must block before broker submission")


class ScannerWideQuoteBroker(WideQuoteBroker):
    def get_latest_price(self, symbol: str):
        return SimpleNamespace(price=251.80, timestamp=datetime.now(UTC))

    def get_historical_bars(self, symbol: str, timeframe: str, limit: int):
        return pd.DataFrame(
            {
                "open": [250.0] * limit,
                "high": [253.0] * limit,
                "low": [249.0] * limit,
                "close": [251.8] * limit,
                "volume": [1_000_000.0] * limit,
            }
        )

    def get_positions(self):
        return [
            SimpleNamespace(
                symbol="ABBV",
                qty=10.0,
                avg_entry_price=244.0,
                current_price=251.8,
                market_value=2518.0,
                unrealized_pl=78.0,
            )
        ]

    def get_account(self):
        return SimpleNamespace(
            equity=10_000.0,
            last_equity=10_000.0,
            cash=7_482.0,
            buying_power=7_482.0,
            long_market_value=2_518.0,
            short_market_value=0.0,
        )

    def get_clock(self):
        now = datetime.now(UTC)
        return SimpleNamespace(is_open=True, timestamp=now, next_close=now + timedelta(hours=2))

    def get_loss_metrics(self):
        return {
            "daily_loss_dollars": 0.0,
            "weekly_loss_dollars": 0.0,
            "daily_loss_confidence": "verified",
            "weekly_loss_confidence": "verified",
            "reference_equity": 10_000.0,
        }


class CapturingTelegram:
    allowed_user_id = "owner"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str, chat_id=None):
        self.messages.append(text)
        return {"message_id": len(self.messages)}

    def is_available(self, force: bool = False):
        return True


def quote_config() -> dict:
    return {
        "mode": "paper",
        "live_enabled": False,
        "alpaca": {"equity_realtime_data_feed": "iex"},
        "quotes": {
            "max_age_seconds": 15,
            "max_spread_bps": 50,
            "max_limit_slippage_bps": 25,
            "price_increment_usd": 0.01,
        },
        "telegram": {
            "approval_price_refresh_required": True,
            "approval_max_price_age_seconds": 120,
            "approval_max_price_move_bps": 25,
            "approval_max_price_move_hard_cap_bps": 75,
        },
        "position_sizing": {"enabled": False},
    }


def test_wide_iex_quote_preserves_exact_failure_evidence() -> None:
    broker = WideQuoteBroker()
    with pytest.raises(QuoteValidationError) as exc_info:
        validated_quote(broker, "ABBV", quote_config(), now=broker.quote_timestamp)

    exc = exc_info.value
    assert exc.code == "spread_exceeds_configured_limit"
    assert exc.evidence == {
        "symbol": "ABBV",
        "source": "alpaca_quote",
        "feed": "iex",
        "bid": 237.23,
        "ask": 252.07,
        "timestamp": broker.quote_timestamp.isoformat(),
        "age_seconds": 0.0,
        "spread_bps": pytest.approx(606.5808287134405),
        "max_age_seconds": 15.0,
        "max_spread_bps": 50.0,
    }
    reason = quote_failure_reason("ABBV", exc, quote_config(), stage="approval")
    assert "Alpaca IEX quote spread 606.6 bps" in reason
    assert "50.0 bps safety limit" in reason
    assert "single-exchange feed rather than consolidated NBBO" in reason
    assert "new proposal and new manual approval are required" in reason


def test_final_abbv_revalidation_blocks_with_evidence_and_zero_broker_calls(tmp_path) -> None:
    storage = Storage(tmp_path / "quote-final.db")
    storage.initialize()
    broker = WideQuoteBroker()
    service = TradingService(quote_config(), storage, broker, "quote-final-run")
    proposal = {
        "id": "abbv-proposal",
        "symbol": "ABBV",
        "side": "sell",
        "action": "exit",
        "qty": 5.0,
        "notional": 1250.0,
        "latest_price": 251.8,
        "price_at": datetime.now(UTC).isoformat(),
    }
    row = {
        "id": "abbv-proposal",
        "symbol": "ABBV",
        "side": "sell",
        "notional": 1250.0,
        "current_price": 251.8,
        "emergency_exit_triggered": 0,
    }

    result, refreshed, refreshed_at, refreshed_iso, age, movement = (
        service._execute_final_revalidation(
            row, proposal, "ABBV", "sell", False, "abbv-approval"
        )
    )

    assert result.submitted is False
    assert result.status == "blocked"
    assert result.intent_id is None
    assert broker.submission_calls == 0
    assert "Alpaca IEX quote spread 606.6 bps" in result.reason
    assert refreshed == 237.23
    assert refreshed_at == broker.quote_timestamp
    assert refreshed_iso == broker.quote_timestamp.isoformat()
    assert age == pytest.approx(0.0, abs=0.1)
    assert movement is not None
    assert proposal["quote_validation_code"] == "spread_exceeds_configured_limit"
    assert proposal["quote_feed"] == "iex"
    assert proposal["quote_bid"] == 237.23
    assert proposal["quote_ask"] == 252.07

    audits = storage.fetch_all(
        "SELECT detail FROM audit_events WHERE event_type='final_quote_validation_blocked'"
    )
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail"])
    assert detail["code"] == "spread_exceeds_configured_limit"
    assert detail["broker_invocation_occurred"] == 0
    assert detail["evidence"]["feed"] == "iex"
    assert detail["evidence"]["spread_bps"] == pytest.approx(606.5808287134405)
    assert storage.fetch_all("SELECT * FROM order_intents") == []
    assert storage.fetch_all("SELECT * FROM risk_reservations") == []
    assert storage.fetch_all("SELECT * FROM orders") == []


def test_wide_quote_blocks_abbv_before_proposal_insert_or_telegram_display(
    tmp_path, monkeypatch
) -> None:
    config = load_config()
    config["market_profiles"] = {
        "abbv_only": {
            "status": "active",
            "broker": "alpaca",
            "watchlist": ["ABBV"],
            "observation_watchlist": [],
            "proposals_enabled": True,
            "execution_enabled": True,
        }
    }
    config["position_management"]["enabled"] = False
    config["trend_management"]["enabled"] = False
    config["risk"]["use_gpt_for_exit_explanations"] = False
    config["crypto"]["enabled"] = False
    storage = Storage(tmp_path / "quote-proposal.db")
    storage.initialize()
    broker = ScannerWideQuoteBroker()
    service = TradingService(config, storage, broker, "quote-proposal-run")
    telegram = CapturingTelegram()
    service.telegram = telegram
    monkeypatch.setattr(service, "_dynamic_universe_scan_symbols", lambda: ([], []))
    monkeypatch.setattr(
        "app.service.evaluate_symbol",
        lambda symbol, *args, **kwargs: Signal(
            "EXIT",
            "sell",
            symbol,
            "TIME_STOP_EXIT: score deteriorated",
            0.95,
            {"volatility_20": 0.20},
        ),
    )

    service.scan()

    assert storage.fetch_all("SELECT * FROM trade_proposals") == []
    assert storage.fetch_all("SELECT * FROM proposal_display_envelopes") == []
    assert not any("approve" in message.lower() for message in telegram.messages)
    memory = storage.fetch_all(
        "SELECT proposal_generated,no_action_reason FROM market_memory WHERE symbol='ABBV'"
    )
    assert len(memory) == 1
    assert memory[0]["proposal_generated"] == 0
    assert "Alpaca IEX quote spread 606.6 bps" in memory[0]["no_action_reason"]
    audits = storage.fetch_all(
        "SELECT detail FROM audit_events WHERE event_type='proposal_quote_validation_blocked'"
    )
    assert len(audits) == 1
    detail = json.loads(audits[0]["detail"])
    assert detail["proposal_inserted"] == 0
    assert detail["telegram_proposal_sent"] == 0


@pytest.mark.parametrize("feed", ["iex", "sip"])
def test_alpaca_broker_requests_the_configured_realtime_feed(feed: str) -> None:
    broker = AlpacaBroker(
        {
            "mode": "paper",
            "live_enabled": False,
            "alpaca": {"equity_realtime_data_feed": feed},
        },
        "paper-key",
        "paper-secret",
    )
    captured = {}

    def latest_quote(request):
        captured["quote_feed"] = request.feed.value
        return {
            "ABBV": SimpleNamespace(
                bid_price=99.99,
                ask_price=100.01,
                bid_size=10,
                ask_size=20,
                bid_exchange="V",
                ask_exchange="V",
                timestamp=datetime.now(UTC),
            )
        }

    def latest_trade(request):
        captured["trade_feed"] = request.feed.value
        return {"ABBV": SimpleNamespace(price=100.0, timestamp=datetime.now(UTC))}

    broker.data.get_stock_latest_quote = latest_quote
    broker.data.get_stock_latest_trade = latest_trade

    quote = broker.get_latest_quote("ABBV")
    broker.get_latest_price("ABBV")

    assert captured == {"quote_feed": feed, "trade_feed": feed}
    assert quote["feed"] == feed


def test_quote_feed_identity_is_bound_to_config_and_display() -> None:
    now = datetime.now(UTC)

    class WrongFeedBroker:
        equity_realtime_data_feed = "sip"

        def get_latest_quote(self, symbol):
            return {
                "bid_price": 99.99,
                "ask_price": 100.01,
                "timestamp": now,
                "feed": "sip",
            }

    with pytest.raises(QuoteValidationError, match="feed identity") as exc_info:
        validated_quote(WrongFeedBroker(), "ABBV", quote_config(), now=now)
    assert exc_info.value.code == "quote_feed_mismatch"

    proposal = {
        "id": "display-quote",
        "symbol": "ABBV",
        "side": "sell",
        "action": "exit",
        "qty": 2.0,
        "notional": 200.0,
        "latest_price": 100.0,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "config_hash": "a" * 64,
        "formula_versions": dict(REQUIRED_FORMULA_VERSIONS),
        "quote_source": "alpaca_quote",
        "quote_feed": "iex",
        "quote_bid": 99.99,
        "quote_ask": 100.01,
        "quote_timestamp": now.isoformat(),
        "quote_spread_bps": 2.0,
        "limit_price": 99.74,
        "status": "pending",
    }
    envelope = display_envelope(
        proposal, telegram_message_id="101", proposal_version=1
    )
    assert envelope["display_quote"] == {
        "source": "alpaca_quote",
        "feed": "iex",
        "bid": 99.99,
        "ask": 100.01,
        "timestamp": now.isoformat(),
        "spread_bps": 2.0,
        "limit_price": 99.74,
    }


def test_configuration_requires_an_explicit_supported_realtime_feed() -> None:
    config = load_config()
    assert config["alpaca"]["equity_realtime_data_feed"] == "iex"

    missing = copy.deepcopy(config)
    missing["alpaca"].pop("equity_realtime_data_feed")
    with pytest.raises(ConfigurationError, match="must explicitly select"):
        validate_config(missing)

    unsupported = copy.deepcopy(config)
    unsupported["alpaca"]["equity_realtime_data_feed"] = "implicit_best"
    with pytest.raises(ConfigurationError, match="must explicitly select"):
        validate_config(unsupported)

    with pytest.raises(RuntimeError, match="must be explicitly iex or sip"):
        AlpacaBroker(
            {"mode": "paper", "live_enabled": False},
            "paper-key",
            "paper-secret",
        )
