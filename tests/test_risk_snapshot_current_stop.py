from __future__ import annotations

import pytest

from app.risk_snapshot import RiskSnapshotBuilder
from app.storage import Storage


def _storage(tmp_path) -> Storage:
    storage = Storage(tmp_path / "risk-current-stop.sqlite3")
    storage.initialize()
    return storage


def _position_state(storage: Storage, *, initial: float, trailing: float | None, authoritative: float | None) -> None:
    storage.execute(
        """INSERT INTO position_management_state(
             id,symbol,initial_stop_price,trailing_stop_price,authoritative_protective_stop,
             created_at,updated_at) VALUES('pm','SPY',?,?,?,?,?)""",
        (initial, trailing, authoritative, "2026-07-14T00:00:00+00:00", "2026-07-14T00:00:00+00:00"),
    )


def test_held_risk_uses_current_mark_and_tightest_durable_stop(tmp_path) -> None:
    storage = _storage(tmp_path)
    _position_state(storage, initial=90.0, trailing=102.0, authoritative=104.0)

    snapshot = RiskSnapshotBuilder(storage).build(
        [{"symbol": "SPY", "qty": 10, "avg_entry_price": 100.0, "current_price": 110.0, "market_value": 1100.0}],
        {"equity": 10_000.0, "cash": 8_900.0, "buying_power": 8_900.0},
    )

    assert snapshot.held_open_stop_risk == pytest.approx(60.0)


def test_tightening_stop_releases_current_open_risk(tmp_path) -> None:
    storage = _storage(tmp_path)
    _position_state(storage, initial=90.0, trailing=102.0, authoritative=104.0)
    positions = [{"symbol": "SPY", "qty": 10, "avg_entry_price": 100.0, "current_price": 110.0, "market_value": 1100.0}]
    account = {"equity": 10_000.0, "cash": 8_900.0, "buying_power": 8_900.0}

    before = RiskSnapshotBuilder(storage).build(positions, account)
    storage.execute(
        "UPDATE position_management_state SET authoritative_protective_stop=108.0 WHERE symbol='SPY'"
    )
    after = RiskSnapshotBuilder(storage).build(positions, account)

    assert before.held_open_stop_risk == pytest.approx(60.0)
    assert after.held_open_stop_risk == pytest.approx(20.0)
    assert before.held_open_stop_risk - after.held_open_stop_risk == pytest.approx(40.0)


def test_missing_current_mark_or_unsupported_short_fails_closed(tmp_path) -> None:
    storage = _storage(tmp_path)
    _position_state(storage, initial=90.0, trailing=None, authoritative=95.0)
    account = {"equity": 10_000.0, "cash": 10_000.0, "buying_power": 10_000.0}

    missing = RiskSnapshotBuilder(storage).build(
        [{"symbol": "SPY", "qty": 10, "avg_entry_price": 100.0}], account
    )
    short = RiskSnapshotBuilder(storage).build(
        [{"symbol": "SPY", "qty": -2, "current_price": 100.0, "market_value": -200.0}], account
    )

    assert missing.held_open_stop_risk is None
    assert short.held_open_stop_risk is None
