from app.ai_review import AIReviewer


class BadResponses:
    class Responses:
        def create(self, **kwargs):
            return type("R", (), {"output_text": "not-json"})()
    responses = Responses()


def test_invalid_json_falls_back():
    result = AIReviewer({"model": "test"}, BadResponses()).review({"symbol": "QQQ", "side": "buy", "notional": 5})
    assert "Deterministic fallback" in result["reasoning_notes"]


def test_call_limit_falls_back():
    client = BadResponses()
    reviewer = AIReviewer({"model": "test", "max_calls_per_run": 2}, client)
    r1 = reviewer.review({"symbol": "QQQ", "side": "buy", "notional": 5})
    assert "JSONDecodeError" in r1["risks"][-1]
    r2 = reviewer.review({"symbol": "QQQ", "side": "buy", "notional": 5})
    assert "JSONDecodeError" in r2["risks"][-1]
    r3 = reviewer.review({"symbol": "QQQ", "side": "buy", "notional": 5})
    assert "exceeded call limit" in r3["risks"][-1]
