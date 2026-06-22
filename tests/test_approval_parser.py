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


def test_rejection_rejected_with_multiple_pending():
    result = parse_approval("no", 7, 7, [pending(), pending(id="def")])
    assert not result.accepted
    assert result.reason == "ambiguous plain action with multiple pending proposals"


def test_rejection_rejected_with_no_pending():
    result = parse_approval("no", 7, 7, [])
    assert not result.accepted
    assert result.reason == "exactly one matching pending proposal is required"


def test_approval_rejected_with_no_pending():
    result = parse_approval("yes", 7, 7, [])
    assert not result.accepted
    assert result.reason == "exactly one matching pending proposal is required"


def test_unclear_message_not_accepted():
    result = parse_approval("maybe", 7, 7, [pending()])
    assert not result.accepted
    assert result.action == "unclear"
    assert result.reason == "message is not an unambiguous approval or rejection"


def test_approval_with_specific_id_resolves_multiple_pending():
    proposals = [pending(id="abc"), pending(id="def")]
    result = parse_approval("yes proposal def", 7, 7, proposals)
    assert result.accepted
    assert result.proposal_id == "def"


def test_approval_with_side_and_symbol_resolves_multiple_pending():
    proposals = [pending(id="abc", symbol="QQQ", side="buy"), pending(id="def", symbol="SPY", side="sell")]
    result = parse_approval("yes sell spy", 7, 7, proposals)
    assert result.accepted
    assert result.proposal_id == "def"


def test_approval_fails_if_still_ambiguous():
    proposals = [pending(id="abc", symbol="QQQ", side="buy"), pending(id="def", symbol="QQQ", side="buy")]
    result = parse_approval("yes buy qqq", 7, 7, proposals)
    assert not result.accepted
    assert result.reason == "exactly one matching pending proposal is required"

