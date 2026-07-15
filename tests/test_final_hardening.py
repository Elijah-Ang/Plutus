from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.approval_display import record_display
from app.approval_workflow import ApprovalWorkflowStore
from app.broker_interface import BrokerSubmissionNotAttempted
from app.canonical_sizing import canonical_sizing, enforce_ceilings
from app.execution import Executor
from app.exit_blocker import ExitBlockerStore
from app.risk_engine import RiskDecision
from app.storage import Storage


def _proposal(identifier: str = "hardening-proposal") -> dict:
    now = datetime.now(UTC)
    return {
        "id": identifier,
        "proposal_id": identifier,
        "status": "pending",
        "symbol": "ABBV",
        "side": "sell",
        "action": "exit",
        "qty": 0.2,
        "notional": 49.0,
        "latest_price": 245.0,
        "limit_price": 244.18,
        "order_type": "limit",
        "quote_source": "alpaca_quote",
        "quote_bid": 244.8,
        "quote_ask": 245.0,
        "quote_midpoint": 244.9,
        "quote_timestamp": now.isoformat(),
        "quote_spread_bps": 8.2,
        "strategy_version": "rule_based_v2",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "trading_mode": "paper",
    }


def _persist(storage: Storage, proposal: dict) -> None:
    storage.execute(
        """INSERT INTO trade_proposals(
             id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            proposal["id"], proposal["symbol"], proposal["side"], proposal["notional"],
            "pending", proposal["created_at"], proposal["expires_at"],
            proposal["strategy_version"], json.dumps(proposal),
        ),
    )


def test_displayed_terms_are_immutable_and_reply_identity_is_exact(tmp_path):
    storage = Storage(tmp_path / "display.sqlite3")
    storage.initialize()
    proposal = _proposal()
    _persist(storage, proposal)
    display = record_display(storage, proposal["id"], "500")
    assert display["displayed_fingerprint"]

    with pytest.raises(RuntimeError, match="does not target"):
        ApprovalWorkflowStore(storage).accept_approval(
            approval_id="wrong-reply", run_id="run", proposal_id=proposal["id"], sender_id="owner",
            raw_message="yes", parsed_action="approve", telegram_update_id=1,
            reply_to_message_id="501", targeting_method="reply", acknowledgement_status="received",
            approval_received_at=datetime.now(UTC).isoformat(),
        )

    storage.execute("UPDATE trade_proposals SET notional=60 WHERE id=?", (proposal["id"],))
    with pytest.raises(RuntimeError, match="changed"):
        ApprovalWorkflowStore(storage).accept_approval(
            approval_id="mutated", run_id="run", proposal_id=proposal["id"], sender_id="owner",
            raw_message="yes", parsed_action="approve", telegram_update_id=2,
            reply_to_message_id="500", targeting_method="reply", acknowledgement_status="received",
            approval_received_at=datetime.now(UTC).isoformat(),
        )


def test_canonical_quantity_basis_recomputes_notional_and_rejects_contradiction():
    sized = canonical_sizing({
        "request_basis": "quantity", "qty": 2, "latest_price": 100,
        "limit_price": 101, "notional": 202, "stop_price": 95,
    })
    assert sized.quantity == 2
    assert sized.notional == 202
    assert sized.stop_risk == 12
    enforce_ceilings(sized, {
        "approved_quantity_ceiling": 2,
        "approved_notional_ceiling": 202,
        "approved_stop_risk_ceiling": 12,
    }, required=True)
    with pytest.raises(ValueError, match="inconsistent"):
        canonical_sizing({
            "request_basis": "quantity", "qty": 2, "latest_price": 100,
            "limit_price": 101, "notional": 200,
        })


class _PassingRisk:
    config = {}

    def evaluate(self, proposal, context, final=False):
        return RiskDecision(bool(final and context.get("authoritative_risk_snapshot_id")), ())


class _NoAttemptBroker:
    def submit_order(self, *_args, **_kwargs):
        raise BrokerSubmissionNotAttempted("synthetic adapter preflight")


def test_proven_pre_network_failure_is_terminal_not_unknown(tmp_path):
    storage = Storage(tmp_path / "pre-network.sqlite3")
    storage.initialize()
    proposal = _proposal("pre-network")
    _persist(storage, proposal)
    record_display(storage, proposal["id"], "700")
    workflow = ApprovalWorkflowStore(storage).accept_approval(
        approval_id="approval", run_id="run", proposal_id=proposal["id"], sender_id="owner",
        raw_message="yes", parsed_action="approve", telegram_update_id=7,
        reply_to_message_id="700", targeting_method="reply", acknowledgement_status="received",
        approval_received_at=datetime.now(UTC).isoformat(),
    )
    assert storage.consume_approval(proposal["id"], "approval")
    from app.approval_workflow import ApprovalWorkflowState
    ApprovalWorkflowStore(storage).transition(
        workflow["id"], ApprovalWorkflowState.VALIDATING,
        expected_state=ApprovalWorkflowState.TARGET_RESOLVED,
    )
    executable = {**proposal, "status": "approved", "request_basis": "quantity"}
    result = Executor(_NoAttemptBroker(), _PassingRisk(), storage, "run").execute(
        executable, {}, approval_id="approval"
    )
    assert result.status == "rejected", result
    intent = storage.fetch_all("SELECT state,broker_invocation_occurred,risk_snapshot_id FROM order_intents")[0]
    assert intent["state"] == "rejected"
    assert intent["broker_invocation_occurred"] == 0
    assert intent["risk_snapshot_id"]
    assert storage.fetch_all("SELECT state FROM risk_reservations")[0]["state"] == "released"


def test_exit_blocker_persists_action_and_recovery_provenance(tmp_path):
    storage = Storage(tmp_path / "blocker.sqlite3")
    storage.initialize()
    blocker = ExitBlockerStore(storage, "run").observe({
        "active": True,
        "symbol": "ABBV",
        "source_type": "current_position_management_decision",
        "source_id": "decision-1",
        "status": "TIME_STOP_EXIT",
        "position_lifecycle_id": "lifecycle-1",
        "reason": "fresh ABBV TIME_STOP_EXIT decision has priority",
    })
    assert blocker["blocker_state"] == "fresh_decision_awaiting_proposal"
    assert "fresh displayed proposal" in blocker["user_action_required"]
    assert "fresh valid data" in blocker["automatic_recovery"]
    assert ExitBlockerStore(storage, "run").clear_absent() == 1
    row = storage.fetch_all("SELECT state,active,cleared_at FROM exit_blocker_states")[0]
    assert row["state"] == "cleared" and row["active"] == 0 and row["cleared_at"]
