from datetime import UTC, datetime, timedelta

from app.approval_parser import parse_approval


def pending(**changes):
    value = {"id": "abc", "symbol": "QQQ", "side": "buy", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    value.update(changes)
    return value


def test_accepts_yes_variants():
    for text in ("yes", "approve", "approved", "yes please", "yes buy qqq"):
        assert parse_approval(text, 7, 7, [pending()]).accepted


def test_accepts_no_variants():
    for text in ("no", "reject", "rejected", "no thanks"):
        result = parse_approval(text, 7, 7, [pending()])
        assert result.accepted and result.action == "reject"


def test_plain_yes_rejected_with_multiple_pending():
    assert not parse_approval("yes", 7, 7, [pending(), pending(id="def")]).accepted


def test_unauthorized_user_rejected():
    assert not parse_approval("yes", 8, 7, [pending()]).accepted


def test_expired_rejected():
    assert not parse_approval("yes", 7, 7, [pending(expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())]).accepted


def test_mismatched_symbol_rejected():
    assert not parse_approval("yes buy spy", 7, 7, [pending()]).accepted
