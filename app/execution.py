from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .risk_engine import RiskEngine


@dataclass(frozen=True)
class ExecutionResult:
    submitted: bool
    status: str
    client_order_id: str | None
    broker_response: Any = None
    reason: str = ""


class Executor:
    def __init__(self, broker: Any, risk_engine: RiskEngine, storage: Any | None = None) -> None:
        self.broker = broker
        self.risk_engine = risk_engine
        self.storage = storage

    def execute(self, proposal: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        if proposal.get("status") != "approved" or context.get("approval_valid") is not True:
            return ExecutionResult(False, "blocked", None, reason="validated approval required")
        client_order_id = proposal.get("client_order_id") or f"ta-{uuid.uuid4().hex[:24]}"
        candidate = {**proposal, "client_order_id": client_order_id}
        final_context = {**context, "final_revalidation": True}
        decision = self.risk_engine.evaluate(candidate, final_context, final=True)
        if not decision.passed:
            return ExecutionResult(False, "blocked", client_order_id, reason="; ".join(decision.reasons))
        try:
            response = self.broker.submit_order(
                candidate["symbol"], candidate["side"], {"notional": float(candidate["notional"])},
                candidate.get("order_type", "market"), candidate.get("limit_price"), client_order_id,
            )
            return ExecutionResult(True, str(getattr(response, "status", "submitted")), client_order_id, response)
        except Exception as exc:
            # Never retry: the broker may have accepted an order before transport failed.
            return ExecutionResult(False, "unknown", client_order_id, reason=f"manual review required: {type(exc).__name__}")


def execute_proposal(broker: Any, risk_engine: RiskEngine, proposal: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
    return Executor(broker, risk_engine).execute(proposal, context)
