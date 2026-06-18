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
