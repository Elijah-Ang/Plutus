from __future__ import annotations

import json
import os
from typing import Any

from .utils import get_secret, redact

REQUIRED_FIELDS = {
    "summary", "risks", "telegram_message", "caution_level", "should_block_for_reasoning_only", "reasoning_notes",
    "gpt_confidence", "gpt_caution", "main_risk", "supports_system_score", "reason"
}


def deterministic_review(proposal: dict[str, Any], warning: str | None = None) -> dict[str, Any]:
    symbol = proposal.get("symbol", "UNKNOWN")
    side = str(proposal.get("side", "review")).upper()
    notional = float(proposal.get("notional", 0))
    risks = ["Market prices can move before approval", "Paper fills may differ from live fills"]
    if warning:
        risks.append(warning)
    return {
        "summary": f"Paper proposal: {side} {symbol} for ${notional:.2f}. Deterministic risk checks control execution.",
        "risks": risks,
        "telegram_message": f"PAPER ONLY — {side} {symbol}, ${notional:.2f}. Reply with an unambiguous approval or rejection before expiry.",
        "caution_level": "high" if warning else "medium",
        "should_block_for_reasoning_only": False,
        "reasoning_notes": "Deterministic fallback; no AI decision was used.",
        "gpt_confidence": "Not called",
        "gpt_caution": "Low",
        "main_risk": warning or "No AI risk evaluation was performed.",
        "supports_system_score": "yes",
        "reason": "AI review throttled or unavailable; using system defaults.",
    }


def validate_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not REQUIRED_FIELDS.issubset(value):
        raise ValueError("AI response missing required fields")
    if not isinstance(value["risks"], list) or value["caution_level"] not in {"low", "medium", "high"}:
        raise ValueError("AI response has invalid field types")
    if value["gpt_confidence"] not in {"High", "Medium", "Low", "Not called"}:
        raise ValueError("Invalid gpt_confidence value")
    if value["gpt_caution"] not in {"Low", "Medium", "High"}:
        raise ValueError("Invalid gpt_caution value")
    return value


class AIReviewer:
    def __init__(self, config: dict[str, Any], client: Any | None = None) -> None:
        self.config = config
        self.client = client
        self.calls_made = 0

    def review(self, proposal: dict[str, Any]) -> dict[str, Any]:
        max_calls = self.config.get("max_calls_per_run", 5)
        if self.calls_made >= max_calls:
            return deterministic_review(proposal, f"AI review blocked: exceeded call limit of {max_calls}")
        self.calls_made += 1
        safe = redact(proposal)
        try:
            if self.client is None:
                from openai import OpenAI
                api_key = get_secret("OPENAI_API_KEY")
                if not api_key:
                    raise RuntimeError("OpenAI key unavailable")
                self.client = OpenAI(api_key=api_key, timeout=20, max_retries=1)
            prompt = (
                "You summarize a supervised trade proposal; never decide or bypass rules. "
                "Return strict JSON with the following fields: "
                "1. summary: short plain-English review "
                "2. risks: list of 2-4 strings "
                "3. telegram_message: a standard message "
                "4. caution_level: low/medium/high "
                "5. should_block_for_reasoning_only: boolean "
                "6. reasoning_notes: string notes "
                "7. gpt_confidence: High / Medium / Low "
                "8. gpt_caution: Low / Medium / High "
                "9. main_risk: one short risk sentence "
                "10. supports_system_score: yes/no "
                "11. reason: short explanation. "
                "Data: " + json.dumps(safe, default=str)
            )
            response = self.client.responses.create(
                model=self.config.get("model", "gpt-5.4-mini"),
                reasoning={"effort": self.config.get("reasoning_effort_default", "low")},
                input=prompt,
            )
            value = json.loads(response.output_text)
            return validate_review(value)
        except Exception as exc:
            return deterministic_review(proposal, f"AI review unavailable: {type(exc).__name__}")


def review_proposal(proposal: dict[str, Any], config: dict[str, Any], client: Any | None = None) -> dict[str, Any]:
    return AIReviewer(config, client).review(proposal)
