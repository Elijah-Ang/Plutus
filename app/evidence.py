"""Evidence classification shared by promotion and operational allocation.

The labels are deliberately descriptive.  A fixed-horizon signal result is not
an executable portfolio return merely because it has matured in the database.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

SIGNAL_OUTCOME = "signal_outcome"
SHADOW_OUTCOME = "shadow_outcome"
EXECUTABLE_PORTFOLIO_RETURN = "executable_portfolio_return"
ACTUAL_PAPER_TRADE_RETURN = "actual_paper_trade_return"

OPERATIONAL_EVIDENCE_TYPES = frozenset({EXECUTABLE_PORTFOLIO_RETURN, ACTUAL_PAPER_TRADE_RETURN})


def _provenance(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def classify_evidence_type(
    execution_type: Any,
    source_table: Any,
    provenance: Any = None,
) -> str:
    execution = str(execution_type or "").strip().lower()
    source = str(source_table or "").strip().lower()
    provenance_map = _provenance(provenance)

    if execution == "executable_portfolio_return" and source in {
        "strategy_portfolio_returns",
        "executable_portfolio_returns",
    }:
        return EXECUTABLE_PORTFOLIO_RETURN
    if execution == "actual_fill" and source in {"performance_setups", "trade_outcomes"}:
        # Require durable linkage to an execution record.  A row with only a
        # price or a matured horizon is not sufficient proof of a paper fill.
        if any(provenance_map.get(key) for key in ("fill_id", "order_id", "trade_id", "proposal_id")):
            return ACTUAL_PAPER_TRADE_RETURN
    if execution == "shadow_hypothetical":
        return SHADOW_OUTCOME
    return SIGNAL_OUTCOME


def is_operational_evidence(row: Mapping[str, Any]) -> bool:
    return classify_evidence_type(
        row.get("execution_type"), row.get("source_table"), row.get("provenance_json")
    ) in OPERATIONAL_EVIDENCE_TYPES
