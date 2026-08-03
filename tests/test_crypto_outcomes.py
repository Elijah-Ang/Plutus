from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.crypto_outcomes import (
    ActualEvidenceError,
    BetaPrior,
    CryptoCostModel,
    CryptoOutcomeStore,
    VerifiedCorrelationSnapshot,
    build_actual_observation,
    calculate_shadow_outcome,
    derive_aggregate_metrics,
    validate_actual_evidence,
)


ZERO_COST = CryptoCostModel("explicit-zero-v1", "0", "0", "0")
TEST_COST = CryptoCostModel("explicit-cost-v1", "10", "2", "3")


def _setup(
    *,
    signal: str = "2026-01-01T00:00:00Z",
    horizon: int = 3,
    cost: CryptoCostModel = TEST_COST,
    quantity: str | None = "2",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "BTC/USD",
        "strategy_decision_id": "decision-1",
        "strategy_decision_fingerprint": "decision-fingerprint-1",
        "research_timestamp": signal,
        "entry_price": "100.00",
        "stop_price": "95.00",
        "target_price": "110.00",
        "horizon_hours": horizon,
        "quantity": quantity,
        "side": "long",
        "cost_model": cost.to_payload(),
    }
    payload.update(overrides)
    return payload


def _bar(timestamp: datetime, *, high: str = "104", low: str = "99", close: str = "101") -> dict[str, str]:
    return {
        "timestamp": timestamp.isoformat(),
        "high": high,
        "low": low,
        "close": close,
    }


def test_only_post_signal_bars_are_used_and_elapsed_hours_are_utc_24_7() -> None:
    signal_utc = datetime(2026, 1, 1, tzinfo=UTC)
    setup = _setup(signal="2026-01-01T08:00:00+08:00", horizon=24, cost=ZERO_COST)
    bars = [
        _bar(signal_utc - timedelta(hours=1), high="120", low="80", close="100"),
        _bar(signal_utc, high="120", low="80", close="100"),
    ]
    bars.extend(
        _bar(signal_utc + timedelta(hours=index), close="101")
        for index in range(1, 25)
    )

    observation = calculate_shadow_outcome(setup, bars)

    assert observation.status == "completed"
    assert observation.outcome_class == "horizon_close"
    assert observation.holding_hours == Decimal("24")
    assert len(observation.bars) == 24
    assert observation.bars[0].timestamp == signal_utc + timedelta(hours=1)
    assert observation.exit_timestamp == signal_utc + timedelta(hours=24)


def test_same_bar_stop_and_target_resolves_stop_first_and_applies_explicit_costs() -> None:
    signal = datetime(2026, 1, 1, tzinfo=UTC)
    setup = _setup(horizon=2, cost=TEST_COST)
    observation = calculate_shadow_outcome(
        setup,
        [
            _bar(signal + timedelta(hours=1), high="120", low="90", close="100"),
        ],
    )

    assert observation.outcome_class == "stop_hit"
    assert observation.reason == "stop_hit_stop_first"
    assert observation.exit_price == Decimal("95")
    assert observation.holding_hours == Decimal("1")
    assert observation.gross_return == Decimal("-0.05")
    # 2*10 fee bps + 2 spread bps + 3 slippage bps = 25 bps.
    assert observation.net_return == Decimal("-0.0525")
    assert observation.net_pnl == Decimal("-10.5")
    assert observation.risk_amount == Decimal("10")
    assert observation.net_r_multiple == Decimal("-1.05")


def test_schema_decimal_text_fingerprint_and_idempotent_persistence(tmp_path) -> None:
    database = tmp_path / "crypto-outcomes.sqlite3"
    setup = _setup(horizon=1, cost=TEST_COST)
    signal = datetime(2026, 1, 1, tzinfo=UTC)
    observation = calculate_shadow_outcome(
        setup,
        [_bar(signal + timedelta(hours=1), high="111", low="99", close="110")],
    )

    with CryptoOutcomeStore(database) as store:
        first = store.persist(observation)
        second = store.persist(observation)
        row = store.connection.execute(
            "SELECT entry_price,stop_price,quantity,fee_bps,spread_bps,slippage_bps,input_fingerprint "
            "FROM crypto_profitability_observations"
        ).fetchone()

        assert first.inserted is True
        assert second.inserted is False
        assert row["entry_price"] == "100"
        assert row["stop_price"] == "95"
        assert row["quantity"] == "2"
        assert row["fee_bps"] == "10"
        assert row["spread_bps"] == "2"
        assert row["slippage_bps"] == "3"
        assert row["input_fingerprint"] == observation.input_fingerprint
        assert store.connection.execute(
            "SELECT COUNT(*) FROM crypto_profitability_observations"
        ).fetchone()[0] == 1

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.connection.execute(
                "UPDATE crypto_profitability_observations SET input_fingerprint=? WHERE observation_id=?",
                ("changed", observation.observation_id),
            )
        store.connection.rollback()

        changed_cost = CryptoCostModel("explicit-cost-v2", "10", "2", "4")
        changed = calculate_shadow_outcome(
            _setup(horizon=1, cost=changed_cost),
            [_bar(signal + timedelta(hours=1), high="111", low="99", close="110")],
        )
        assert changed.input_fingerprint != observation.input_fingerprint
        assert store.persist(changed).inserted is True


def test_missing_future_bars_are_maturing_and_never_zero_outcomes() -> None:
    signal = datetime(2026, 1, 1, tzinfo=UTC)
    observation = calculate_shadow_outcome(
        _setup(horizon=3, cost=ZERO_COST),
        [_bar(signal + timedelta(hours=1))],
    )

    assert observation.status == "maturing"
    assert observation.outcome_class == "unavailable"
    assert observation.reason == "missing_future_bars"
    assert observation.gross_return is None
    assert observation.net_return is None
    assert observation.holding_hours is None
    assert observation.net_pnl is None

    metrics = derive_aggregate_metrics([observation], severe_loss_threshold="-0.05")
    assert metrics["sample_count"] == 0
    assert metrics["unavailable_count"] == 1
    assert metrics["win_probability"] is None
    assert metrics["holding_hours"] is None
    assert metrics["correlation"] is None


def test_actual_evidence_requires_verified_fill_link_and_closed_lifecycle() -> None:
    incomplete = validate_actual_evidence({"fill_id": "fill-1", "fill_verified": True})
    assert incomplete.valid is False
    assert incomplete.missing == ("verified_performance_link", "closed_position_lifecycle")
    with pytest.raises(ActualEvidenceError):
        build_actual_observation(
            {
                **_setup(horizon=1, cost=ZERO_COST),
                "exit_timestamp": "2026-01-01T01:00:00Z",
                "exit_price": "110",
                "holding_hours": "1",
                "gross_return": "0.1",
                "net_return": "0.1",
            },
            lineage={"fill_id": "fill-1", "fill_verified": True},
        )

    lineage = {
        "fill_id": "fill-1",
        "fill_verified": True,
        "performance_link_id": "performance-1",
        "performance_link_verified": True,
        "position_lifecycle_id": "lifecycle-1",
        "lifecycle_state": "closed",
    }
    valid = validate_actual_evidence(lineage)
    assert valid.valid is True
    actual = build_actual_observation(
        {
            **_setup(horizon=1, cost=ZERO_COST),
            "exit_timestamp": "2026-01-01T01:00:00Z",
            "exit_price": "110",
            "holding_hours": "1",
            "gross_return": "0.1",
            "net_return": "0.1",
            "gross_pnl": "20",
            "net_pnl": "20",
            "risk_amount": "10",
            "net_r_multiple": "2",
        },
        lineage=lineage,
    )
    assert actual.evidence_type == "actual"
    assert actual.actual_lineage_json is not None


def test_aggregate_metrics_exclude_unavailable_and_require_explicit_prior_snapshot() -> None:
    signal = datetime(2026, 1, 1, tzinfo=UTC)
    win = calculate_shadow_outcome(
        _setup(horizon=2, cost=ZERO_COST),
        [
            _bar(signal + timedelta(hours=1)),
            _bar(signal + timedelta(hours=2), high="111", close="110"),
        ],
    )
    loss = calculate_shadow_outcome(
        _setup(horizon=3, cost=ZERO_COST, stop_price="95"),
        [
            _bar(signal + timedelta(hours=1)),
            _bar(signal + timedelta(hours=2)),
            _bar(signal + timedelta(hours=3), high="103", low="94", close="98"),
        ],
    )
    immature = calculate_shadow_outcome(
        _setup(horizon=4, cost=ZERO_COST),
        [_bar(signal + timedelta(hours=1))],
    )

    without_optional_evidence = derive_aggregate_metrics(
        [win, loss, immature], severe_loss_threshold="-0.05"
    )
    assert without_optional_evidence["sample_count"] == 2
    assert without_optional_evidence["win_probability"] == Decimal("0.5")
    assert without_optional_evidence["win_probability_posterior"] is None
    assert without_optional_evidence["severe_loss_rate"] == Decimal("0.5")
    assert without_optional_evidence["holding_hours"] == Decimal("2.5")
    assert without_optional_evidence["correlation"] is None
    assert without_optional_evidence["uncertainty"] is not None

    with_explicit_evidence = derive_aggregate_metrics(
        [win, loss, immature],
        severe_loss_threshold=Decimal("-0.05"),
        prior=BetaPrior("1", "1"),
        verified_snapshot=VerifiedCorrelationSnapshot("snapshot-1", "snapshot-fp-1", "0.25"),
    )
    assert with_explicit_evidence["win_probability_posterior"] == Decimal("0.5")
    assert with_explicit_evidence["correlation"] == Decimal("0.25")
    assert with_explicit_evidence["posterior_uncertainty"] is not None


def test_float_financial_input_is_rejected() -> None:
    payload = _setup(horizon=1, cost=ZERO_COST, entry_price=100.0)
    signal = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(TypeError, match="float"):
        calculate_shadow_outcome(
            payload,
            [_bar(signal + timedelta(hours=1), high="111", low="99", close="110")],
        )
