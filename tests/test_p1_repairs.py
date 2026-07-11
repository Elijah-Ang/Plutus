from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.capabilities import (
    AUTO_EXECUTION_SUPPORTED,
    AUTONOMOUS_ENTRIES_SUPPORTED,
    AUTONOMOUS_EXITS_SUPPORTED,
    LIVE_TRADING_SUPPORTED,
    PROTECTIVE_PAPER_EXITS_SUPPORTED,
)
from app.configuration import ConfigurationError, validate_config
from app.position_sizing import effective_notional_policy, notional_from_stop_risk
from app.quotes import bounded_marketable_limit, implementation_shortfall_bps, validate_quote_payload, validated_quote
from app.utils import load_config


def test_stop_risk_conversion_keeps_dollars_and_notional_distinct():
    assert notional_from_stop_risk(5.0, 100.0, 2.0) == 250.0
    with pytest.raises(ValueError):
        notional_from_stop_risk(5.0, 100.0, 0.0)


def test_effective_notional_policy_uses_minimum_of_all_ceilings():
    config = {
        "position_sizing": {
            "minimum_executable_notional_usd": 5.0,
            "default_paper_notional_usd": 50.0,
            "absolute_max_notional_usd": 40.0,
            "stage": "moderate_paper",
            "stage_max_initial_notional_usd": {"moderate_paper": 30.0},
            "max_trade_notional_pct_equity": 1.0,
        }
    }
    policy = effective_notional_policy(config, 10000.0)
    assert policy.maximum_allowed_notional_usd == 30.0
    assert policy.minimum_executable_notional_usd == 5.0


def test_effective_notional_policy_rejects_contradictory_minimum():
    with pytest.raises(ValueError, match="below the executable minimum"):
        effective_notional_policy({"position_sizing": {
            "minimum_executable_notional_usd": 50.0,
            "default_paper_notional_usd": 50.0,
            "absolute_max_notional_usd": 10.0,
            "stage": "moderate_paper",
            "stage_max_initial_notional_usd": {"moderate_paper": 100.0},
            "max_trade_notional_pct_equity": 1.0,
        }}, 10000.0)


class QuoteBroker:
    def __init__(self, quote):
        self.quote = quote

    def get_latest_quote(self, symbol):
        return self.quote


def _quote(age_seconds=0, bid=99.9, ask=100.1):
    return {"bid_price": bid, "ask_price": ask, "timestamp": datetime.now(UTC) - timedelta(seconds=age_seconds)}


def test_quotes_reject_stale_crossed_and_wide_market_data():
    config = {"quotes": {"max_age_seconds": 15, "max_spread_bps": 50}}
    with pytest.raises(ValueError, match="stale"):
        validated_quote(QuoteBroker(_quote(age_seconds=16)), "SPY", config)
    with pytest.raises(ValueError, match="crossed"):
        validated_quote(QuoteBroker(_quote(bid=101, ask=100)), "SPY", config)
    with pytest.raises(ValueError, match="spread"):
        validated_quote(QuoteBroker(_quote(bid=90, ask=110)), "SPY", config)


def test_bounded_limit_and_shortfall_are_side_aware():
    config = {"quotes": {"max_limit_slippage_bps": 25, "price_increment_usd": 0.01}}
    quote = {"bid": 99.9, "ask": 100.1, "midpoint": 100.0}
    assert bounded_marketable_limit(quote, "buy", config) >= quote["ask"]
    assert bounded_marketable_limit(quote, "sell", config) <= quote["bid"]
    assert implementation_shortfall_bps(quote, "buy", 100.1) > 0
    assert implementation_shortfall_bps(quote, "sell", 99.9) > 0


def test_persisted_quote_midpoint_and_limit_are_revalidated():
    config = {"quotes": {"max_age_seconds": 15, "max_spread_bps": 50, "max_limit_slippage_bps": 25, "price_increment_usd": 0.01}}
    quote = _quote()
    validated = validated_quote(QuoteBroker(quote), "SPY", config)
    payload = {
        "quote_source": "alpaca_quote",
        "quote_bid": validated["bid"],
        "quote_ask": validated["ask"],
        "quote_midpoint": validated["midpoint"],
        "quote_timestamp": validated["timestamp"],
        "quote_spread_bps": validated["spread_bps"],
        "limit_price": bounded_marketable_limit(validated, "buy", config),
    }
    validate_quote_payload(payload, "buy", config)
    payload["quote_midpoint"] += 1.0
    with pytest.raises(ValueError, match="malformed"):
        validate_quote_payload(payload, "buy", config)


def test_capabilities_keep_only_protective_paper_exit_path():
    assert LIVE_TRADING_SUPPORTED is False
    assert AUTO_EXECUTION_SUPPORTED is False
    assert AUTONOMOUS_ENTRIES_SUPPORTED is False
    assert AUTONOMOUS_EXITS_SUPPORTED is False
    assert PROTECTIVE_PAPER_EXITS_SUPPORTED is True


def test_effective_config_hash_and_strict_unknown_keys():
    config = load_config()
    assert len(config["effective_config_hash"]) == 64
    config["risk"]["unsafe_unknown"] = 1
    with pytest.raises(ConfigurationError, match="unknown risk configuration key"):
        validate_config(config)
