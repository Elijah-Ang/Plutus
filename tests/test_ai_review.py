from app.ai_review import AIReviewer


class BadResponses:
    class Responses:
        def create(self, **kwargs):
            return type("R", (), {"output_text": "not-json"})()
    responses = Responses()


def test_invalid_json_falls_back():
    result = AIReviewer({"model": "test"}, BadResponses()).review({"symbol": "QQQ", "side": "buy", "notional": 5})
    assert "Deterministic fallback" in result["reasoning_notes"]
