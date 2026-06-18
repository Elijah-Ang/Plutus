from datetime import UTC, datetime, timezone, timedelta
from pathlib import Path

from app.utils import format_sgt, format_expiry, format_proposal_message, translate_reason
from app.approval_parser import parse_approval


def test_sgt_formatting():
    # Test that UTC time is converted to SGT (UTC+8) and formatted correctly
    dt_utc = datetime(2026, 6, 18, 18, 6, 20, tzinfo=UTC)
    formatted = format_sgt(dt_utc)
    # 2026-06-18 18:06 UTC -> 2026-06-19 02:06 SGT
    assert "Jun 19, 2026" in formatted
    assert "2:06 AM" in formatted
    assert "SGT" in formatted


def test_relative_expiry():
    # Test relative time string when soon
    now = datetime(2026, 6, 18, 18, 0, 0, tzinfo=UTC)
    expiry = datetime(2026, 6, 18, 18, 10, 0, tzinfo=UTC)
    formatted = format_expiry(expiry, now=now)
    assert "(about 10 minutes)" in formatted


def test_proposal_message_template():
    proposal = {
        "symbol": "SPY",
        "side": "buy",
        "notional": 1.0,
        "expires_at": "2026-06-18T18:06:20.493310+00:00"
    }
    config = {"mode": "paper", "live_enabled": False}
    message = format_proposal_message(proposal, config)
    
    # Verify required structural content
    assert "Paper trading only" in message
    assert "Buy SPY" in message
    assert "$1" in message
    assert "Reply yes to approve, or no to reject." in message
    assert "Jun 19, 2026" in message
    assert "2:06 AM" in message
    assert "SGT" in message
    # Ensure no raw UTC timestamps are present
    assert "2026-06-18T18:06" not in message
    assert "Z" not in message
    assert "+00:00" not in message


def test_fake_test_proposal_message():
    proposal = {
        "symbol": "TEST",
        "side": "buy",
        "notional": 5.0,
        "expires_at": "2026-06-18T18:06:20.493310+00:00"
    }
    config = {"mode": "paper", "live_enabled": False}
    message = format_proposal_message(proposal, config, is_fake_test=True)
    assert "Fake paper test proposal" in message
    assert "No Alpaca order will be placed" in message
    assert "Reply yes to approve the test, or no to reject it." in message


def test_parser_rejection_regex_and_prefix_matching():
    def pending(pid, **changes):
        value = {"id": pid, "symbol": "SPY", "side": "buy", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
        value.update(changes)
        return value

    proposals = [pending("5e165d49-2c16-4631-9c82-4374e71f3a2c"), pending("8fa0dcfe-9b94-467d-b90d-3c25e56b1149")]
    
    # Test prefix-based approval
    res = parse_approval("yes 5e165d49", 7, 7, proposals)
    assert res.accepted
    assert res.proposal_id == "5e165d49-2c16-4631-9c82-4374e71f3a2c"
    
    # Test prefix-based rejection
    res2 = parse_approval("no 8fa0dcfe", 7, 7, proposals)
    assert res2.accepted
    assert res2.action == "reject"
    assert res2.proposal_id == "8fa0dcfe-9b94-467d-b90d-3c25e56b1149"


def test_parser_unclear_message_ignored():
    def pending(pid):
        return {"id": pid, "symbol": "SPY", "side": "buy", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
        
    res = parse_approval("maybe", 7, 7, [pending("abc")])
    assert not res.accepted
    assert res.action == "unclear"


def test_master_overview_documentation_exists_and_has_required_sections():
    overview_path = Path(__file__).resolve().parents[1] / "docs" / "SYSTEM_OVERVIEW.md"
    assert overview_path.exists(), "SYSTEM_OVERVIEW.md does not exist"
    
    content = overview_path.read_text(encoding="utf-8")
    
    # Assert no local links exist
    assert "file:///Users/" not in content, "SYSTEM_OVERVIEW.md contains local absolute file links"
    
    # Assert update rule exists
    assert "Update Rule:" in content, "SYSTEM_OVERVIEW.md is missing the update rule notice"
    
    # Assert stale-state warning exists
    assert "Stale-State Warning:" in content, "SYSTEM_OVERVIEW.md is missing the stale-state warning"
    
    # Assert Mermaid sections exist
    assert "```mermaid" in content, "SYSTEM_OVERVIEW.md is missing Mermaid codeblocks"
    
    required_sections = [
        "Project Purpose",
        "Current Safety Status",
        "High-Level Architecture",
        "Folder Structure",
        "Main Config Files",
        "Main App Modules",
        "Scripts and What They Do",
        "Telegram Approval Flow",
        "Alpaca Paper Broker Flow",
        "OpenAI / AI Review Flow",
        "Risk Engine and Safety Gates",
        "Database / SQLite Tables",
        "Excel Reporting Flow",
        "Testing Strategy",
        "Launchd / Scheduling Status",
        "Live Trading Gates",
        "Current Known State",
        "Recent Milestones Completed",
        "How to Update This Document",
        "Mermaid Diagram of System Connections",
        "Mermaid Flowchart of Trade Proposal → Approval → Execution",
        "Mermaid Flowchart of Safety Blocks",
        "Change Log"
    ]
    
    for section in required_sections:
        assert section in content, f"SYSTEM_OVERVIEW.md is missing section: {section}"

