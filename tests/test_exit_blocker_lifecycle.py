from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.service import TradingService
from app.storage import Storage
from app.strategy_rule_based import Signal


class ExitBroker:
    def __init__(self, positions=None, orders=None):
        self.positions = list(positions or [])
        self.orders = list(orders or [])

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders


def make_service(tmp_path, *, positions=None, orders=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    storage = Storage(tmp_path / "exit-blocker.db")
    storage.initialize()
    broker = ExitBroker(positions=positions, orders=orders)
    service = TradingService({"mode": "paper", "live_enabled": False}, storage, broker, "exit-test-run")
    return service, storage, broker


def position(symbol="ABBV"):
    return SimpleNamespace(symbol=symbol, qty=10.0, avg_entry_price=100.0, current_price=110.0)


def insert_proposal(storage, *, proposal_id="sell-1", symbol="ABBV", status="pending", expires_at=None):
    now = datetime.now(UTC)
    storage.execute(
        """INSERT INTO trade_proposals(
           id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (proposal_id, "old-or-current-run", symbol, "sell", 10.0, status,
         now.isoformat(), expires_at or (now + timedelta(minutes=15)).isoformat(), "rule_based_v2", "{}"),
    )


def test_fresh_current_cycle_exit_is_the_only_proposalless_blocker(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    now = datetime.now(UTC)
    result = {
        "symbol": "ABBV",
        "has_position": True,
        "signal": Signal("EXIT", "sell", "ABBV", "time stop", 0.9, {}),
        "signal_id": "sig-1",
        "position_management_decision_id": "pm-1",
        "position_lifecycle_id": "life-1",
        "position_management_decision": {
            "decision_type": "TIME_STOP_EXIT", "action": "sell", "is_actionable": True,
        },
    }

    blocker = service._exit_blocker_context([], positions=broker.positions, current_cycle_exits=[result], validation_at=now)

    assert blocker["active"] is True
    assert blocker["source_type"] == "current_position_management_decision"
    assert blocker["source_id"] == "pm-1"
    assert blocker["symbol"] == "ABBV"
    assert blocker["status"] == "TIME_STOP_EXIT"
    assert blocker["position_lifecycle_id"] == "life-1"
    assert "fresh ABBV TIME_STOP_EXIT decision" in blocker["reason"]


def test_digest_revalidates_only_the_same_cycle_fresh_decision(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    now = datetime.now(UTC)
    result = {
        "symbol": "ABBV",
        "has_position": True,
        "signal": Signal("EXIT", "sell", "ABBV", "time stop", 0.9, {}),
        "signal_id": "sig-digest",
        "position_management_decision_id": "pm-digest",
        "position_lifecycle_id": "life-digest",
        "position_management_decision": {
            "decision_type": "TIME_STOP_EXIT", "action": "sell", "is_actionable": True,
        },
        "cycle_created_at": now.isoformat(),
        "expiry": now + timedelta(minutes=5),
    }
    blocker = service._exit_blocker_context(
        [], positions=broker.positions, current_cycle_exits=[result], validation_at=now,
    )
    service._current_cycle_exit_blocker = {**blocker, "run_id": service.run_id}

    digest_blocker = service._digest_exit_blocker_context(
        [], broker.positions, now + timedelta(seconds=30),
    )

    assert digest_blocker["active"] is True
    assert digest_blocker["source_type"] == "current_position_management_decision"
    assert digest_blocker["source_id"] == "pm-digest"
    assert service._exit_blocker_display_reason(digest_blocker) == "fresh ABBV TIME_STOP_EXIT decision has priority"

    service.run_id = "next-run"
    assert service._digest_exit_blocker_context([], broker.positions, now + timedelta(minutes=1))["active"] is False


def test_digest_clears_same_cycle_decision_when_position_disappears(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    now = datetime.now(UTC)
    service._current_cycle_exit_blocker = {
        "active": True,
        "source_type": "current_position_management_decision",
        "source_id": "pm-missing",
        "symbol": "ABBV",
        "status": "TIME_STOP_EXIT",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "latest_validation_at": now.isoformat(),
        "position_lifecycle_id": "life-missing",
        "reason": "fresh ABBV TIME_STOP_EXIT decision has priority",
        "run_id": service.run_id,
    }

    digest_blocker = service._digest_exit_blocker_context([], [], now + timedelta(seconds=30))

    assert digest_blocker["active"] is False
    audits = storage.fetch_all("SELECT detail FROM audit_events WHERE event_type='exit_blocker_ignored_stale'")
    assert any("position no longer exists at digest validation" in row["detail"] for row in audits)


def test_prior_run_position_management_decision_is_not_current(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    storage.execute(
        """INSERT INTO position_management_decisions(
           id,run_id,symbol,decision_type,priority,action,reason,quantity,is_actionable,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("old-pm", "old-run", "ABBV", "TIME_STOP_EXIT", 1, "sell", "old", 10.0, 1,
         (datetime.now(UTC) - timedelta(hours=5)).isoformat()),
    )

    blocker = service._exit_blocker_context([], positions=broker.positions)

    assert blocker["active"] is False
    assert blocker["source_id"] is None


def test_active_proposal_requires_current_position_and_exposes_provenance(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    expiry = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    insert_proposal(storage, expires_at=expiry)

    blocker = service._exit_blocker_context([], positions=broker.positions)

    assert blocker["active"] is True
    assert blocker["source_type"] == "active_sell_proposal"
    assert blocker["source_id"] == "sell-1"
    assert blocker["status"] == "pending"
    assert blocker["expires_at"] == expiry
    assert "ABBV exit proposal pending" in service._exit_blocker_display_reason(blocker)
    assert "until" in service._exit_blocker_display_reason(blocker)

    broker.positions = []
    cleared = service._exit_blocker_context([], positions=broker.positions)
    assert cleared["active"] is False
    assert cleared["stale_reason"] == "position no longer exists"


def test_terminal_and_expired_proposals_do_not_block(tmp_path):
    for status in ("rejected", "superseded", "filled", "expired"):
        service, storage, broker = make_service(tmp_path / status, positions=[position()])
        insert_proposal(storage, proposal_id=f"{status}-sell", status=status)
        blocker = service._exit_blocker_context([], positions=broker.positions)
        assert blocker["active"] is False

    service, storage, broker = make_service(tmp_path / "expired-pending", positions=[position()])
    insert_proposal(storage, proposal_id="expired-pending", status="pending", expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    blocker = service._exit_blocker_context([], positions=broker.positions)
    assert blocker["active"] is False
    assert blocker["stale"] is True


def test_open_broker_sell_order_remains_blocking_without_wall_clock_expiry(tmp_path):
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    service, storage, broker = make_service(
        tmp_path,
        orders=[SimpleNamespace(id="broker-sell-1", symbol="ABBV", side="sell", status="open", created_at=old)],
    )

    blocker = service._exit_blocker_context(broker.orders, positions=[])

    assert blocker["active"] is True
    assert blocker["source_type"] == "broker_open_order"
    assert blocker["source_id"] == "broker-sell-1"
    assert service._exit_blocker_display_reason(blocker) == "ABBV sell order remains open"

    broker.orders = [SimpleNamespace(id="broker-sell-1", symbol="ABBV", side="sell", status="canceled", created_at=old)]
    cleared = service._exit_blocker_context(broker.orders, positions=[])
    assert cleared["active"] is False


def test_unresolved_sell_intent_and_reservation_block_until_terminal(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    now = datetime.now(UTC).isoformat()
    storage.execute(
        """INSERT INTO order_intents(
           id,source_id,source_type,logical_action_key,symbol,side,intended_action,request_basis,
           requested_quantity,reference_price,reserved_notional,reserved_stop_risk,client_order_id,
           trading_mode,state,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("intent-1", "proposal-1", "telegram", "sell:ABBV:1", "ABBV", "sell", "exit", "quantity",
         10.0, 100.0, 1000.0, 10.0, "client-sell-1", "paper", "submitted", now, now),
    )
    storage.execute(
        """INSERT INTO risk_reservations(
           id,intent_id,symbol,initial_notional,active_notional,initial_stop_risk,active_stop_risk,
           state,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("reservation-1", "intent-1", "ABBV", 1000.0, 1000.0, 10.0, 10.0, "active", now, now),
    )

    blocker = service._exit_blocker_context([], positions=broker.positions)
    assert blocker["active"] is True
    assert blocker["source_type"] == "active_sell_reservation"
    assert blocker["source_id"] == "reservation-1"

    storage.execute("UPDATE risk_reservations SET state='released',active_notional=0,active_stop_risk=0 WHERE id=?", ("reservation-1",))
    storage.execute("UPDATE order_intents SET state='cancelled' WHERE id=?", ("intent-1",))
    assert service._exit_blocker_context([], positions=broker.positions)["active"] is False


def test_sell_batch_candidate_requires_current_position_and_batch_freshness(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=10)).isoformat()
    storage.execute(
        "INSERT INTO proposal_batches(id,run_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?)",
        ("batch-1", "run", "pending", now.isoformat(), expiry, "{}"),
    )
    storage.execute(
        """INSERT INTO proposal_batch_candidates(
           id,batch_id,proposal_id,candidate_symbol,candidate_side,candidate_action,candidate_status,rank,created_at,expires_at,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("candidate-1", "batch-1", "missing-proposal", "ABBV", "sell", "EXIT", "pending", 1, now.isoformat(), expiry, "{}"),
    )

    blocker = service._exit_blocker_context([], positions=broker.positions)
    assert blocker["active"] is True
    assert blocker["source_type"] == "active_sell_batch_candidate"
    assert blocker["source_id"] == "candidate-1"

    broker.positions = []
    assert service._exit_blocker_context([], positions=broker.positions)["active"] is False


def test_stale_market_memory_wording_is_audited_but_not_displayed(tmp_path):
    service, storage, broker = make_service(tmp_path)
    row = {
        "id": 7,
        "symbol": "IWM",
        "score": 90.0,
        "signal": "ENTRY",
        "no_action_reason": "new buy blocked because an actionable exit has priority",
        "created_at": datetime.now(UTC).isoformat(),
    }

    status = service._digest_market_memory_status("IWM", row, set(), {})

    assert status["event"] != "exit_blocked"
    audits = storage.fetch_all("SELECT detail FROM audit_events WHERE event_type='exit_blocker_ignored_stale'")
    assert audits
    assert json.loads(audits[-1]["detail"])["source_type"] == "market_memory_exit_flag"


def test_digest_names_current_blocker_source_symbol_and_status(tmp_path):
    service, storage, broker = make_service(tmp_path, positions=[position()])
    expiry = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    insert_proposal(storage, expires_at=expiry)
    blocker = service._exit_blocker_context([], positions=broker.positions)

    status = service._digest_market_memory_status(
        "IWM",
        {
            "symbol": "IWM", "score": 90.0, "signal": "ENTRY",
            "no_action_reason": "suppressed due to exit priority",
            "exit_priority_applied": 1,
        },
        set(), {}, current_exit_blocker=blocker,
    )

    assert status["event"] == "exit_blocked"
    assert "ABBV exit proposal pending" in status["status"]
    assert "until" in status["status"]
