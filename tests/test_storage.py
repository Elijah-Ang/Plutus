from datetime import UTC, datetime, timedelta

from app.storage import Storage, TABLE_DEFINITIONS


def test_schema_and_duplicate_approval(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    tables = {row["name"] for row in storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLE_DEFINITIONS) <= tables
    run = storage.start_run("paper")
    now = datetime.now(UTC)
    storage.execute("INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)", ("p", run, "QQQ", "buy", 5, "pending", now.isoformat(), (now + timedelta(minutes=5)).isoformat(), "rule_based_v1"))
    storage.execute("INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("a", run, "p", "7", "yes", "approve", 1, "accepted", now.isoformat()))
    assert storage.consume_approval("p", "a")
    assert not storage.consume_approval("p", "a")


def test_active_and_historical_proposals_distinguish_stale_rows(tmp_path):
    storage = Storage(tmp_path / "state.db")
    storage.initialize()
    run = storage.start_run("paper")
    now = datetime.now(UTC)
    storage.execute("INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)", ("active", run, "SPY", "buy", 5, "pending", now.isoformat(), (now + timedelta(minutes=5)).isoformat(), "rule_based_v1"))
    storage.execute("INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)", ("expired-pending", run, "DIA", "buy", 5, "pending", now.isoformat(), (now - timedelta(minutes=1)).isoformat(), "rule_based_v1"))
    storage.execute("INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)", ("approved-expired", run, "IWM", "buy", 5, "approved", now.isoformat(), (now - timedelta(minutes=1)).isoformat(), "rule_based_v1"))
    storage.execute("INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)", ("submitted", run, "QQQ", "buy", 5, "submitted", now.isoformat(), (now + timedelta(minutes=5)).isoformat(), "rule_based_v1"))

    assert [row["id"] for row in storage.active_proposals(now.isoformat())] == ["active"]
    assert {row["id"] for row in storage.historical_proposals(now.isoformat())} == {"expired-pending", "approved-expired", "submitted"}


def test_consume_approval_rejects_expired_and_non_pending_rows(tmp_path):
    storage = Storage(tmp_path / "consume.db")
    storage.initialize()
    run = storage.start_run("paper")
    now = datetime.now(UTC)
    rows = [
        ("expired", "pending", now - timedelta(minutes=1)),
        ("filled", "filled", now + timedelta(minutes=5)),
        ("submitted", "submitted", now + timedelta(minutes=5)),
        ("active", "pending", now + timedelta(minutes=5)),
    ]
    for pid, status, expiry in rows:
        storage.execute("INSERT INTO trade_proposals(id,run_id,symbol,side,notional,status,created_at,expires_at,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)", (pid, run, "SPY", "buy", 5, status, now.isoformat(), expiry.isoformat(), "rule_based_v1"))
        storage.execute("INSERT INTO approvals(id,run_id,proposal_id,sender_id,raw_message,parsed_action,authorized,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (f"a-{pid}", run, pid, "7", "yes", "approve", 1, "accepted", now.isoformat()))

    assert storage.consume_approval("expired", "a-expired") is False
    assert storage.consume_approval("filled", "a-filled") is False
    assert storage.consume_approval("submitted", "a-submitted") is False
    assert storage.consume_approval("active", "a-active") is True
