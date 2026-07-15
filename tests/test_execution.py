from datetime import UTC, datetime, timedelta

from app.approval_workflow import ApprovalWorkflowState, ApprovalWorkflowStore
from app.execution import Executor
from app.risk_engine import RiskDecision
from app.storage import Storage


class Broker:
    called = False
    def submit_order(self, *args, **kwargs):
        self.called = True
        return type("Order", (), {"status": "submitted"})()


class SpyRisk:
    called_final = False
    def evaluate(self, proposal, context, final=False):
        self.called_final = final and context.get("final_revalidation") is True and bool(proposal.get("client_order_id"))
        return RiskDecision(self.called_final, ())


def authorize(storage, proposal, approval_id="approval-1"):
    now = datetime.now(UTC)
    proposal_id = str(proposal.get("proposal_id") or proposal.get("id"))
    storage.execute(
        """INSERT INTO trade_proposals(id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (proposal_id, proposal["symbol"], proposal["side"], proposal.get("notional"), "pending",
         proposal.get("created_at", now.isoformat()), proposal.get("expires_at"),
         proposal.get("strategy_version", "rule_based_v2"), "{}"),
    )
    store = ApprovalWorkflowStore(storage)
    workflow = store.accept_approval(
        approval_id=approval_id, run_id="run", proposal_id=proposal_id,
        sender_id="owner", raw_message="approve", parsed_action="approve",
        telegram_update_id=None, reply_to_message_id=None, targeting_method="test",
        acknowledgement_status="received", approval_received_at=now.isoformat(),
    )
    assert storage.consume_approval(proposal_id, approval_id)
    store.transition(workflow["id"], ApprovalWorkflowState.VALIDATING,
                     expected_state=ApprovalWorkflowState.TARGET_RESOLVED)
    return {**proposal, "id": proposal_id, "proposal_id": proposal_id, "status": "approved"}


def test_execution_requires_final_revalidation(safe_config, proposal, context, tmp_path):
    broker = Broker()
    risk = SpyRisk()
    context["approval_valid"] = True
    storage = Storage(tmp_path / "execution.db")
    storage.initialize()
    proposal = authorize(storage, {**proposal, "status": "approved"}, "approval-1")
    result = Executor(broker, risk, storage, "run").execute(proposal, context, approval_id="approval-1")
    assert result.submitted
    assert risk.called_final
    assert broker.called


def test_execution_rejects_pending_proposal(safe_config, proposal, context):
    broker = Broker()
    context["approval_valid"] = True
    assert not Executor(broker, SpyRisk()).execute(proposal, context).submitted
    assert not broker.called


def test_missing_approval_blocks_even_when_proposal_looks_approved(safe_config, proposal, context, tmp_path):
    broker = Broker()
    storage = Storage(tmp_path / "missing-approval.db")
    storage.initialize()
    context["approval_valid"] = True
    proposal = authorize(storage, {**proposal, "status": "approved"})
    result = Executor(broker, SpyRisk(), storage, "run").execute(proposal, context)
    assert not result.submitted
    assert "approval_id" in result.reason
    assert not broker.called


def test_wrong_proposal_approval_pairing_blocks(safe_config, proposal, context, tmp_path):
    broker = Broker()
    storage = Storage(tmp_path / "wrong-pair.db")
    storage.initialize()
    context["approval_valid"] = True
    first = authorize(storage, {**proposal, "id": "proposal-a", "proposal_id": "proposal-a", "status": "approved"}, "approval-a")
    other = {**proposal, "id": "proposal-b", "proposal_id": "proposal-b", "status": "approved"}
    # Persist the second approval through the same durable workflow contract.
    authorize(storage, other, "approval-b")
    result = Executor(broker, SpyRisk(), storage, "run").execute(first, context, approval_id="approval-b")
    assert not result.submitted
    assert "linked" in result.reason
    assert not broker.called


def test_expired_approval_blocks(safe_config, proposal, context, tmp_path):
    broker = Broker()
    storage = Storage(tmp_path / "expired-approval.db")
    storage.initialize()
    context["approval_valid"] = True
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    expired = authorize(storage, {**proposal, "status": "approved"})
    storage.execute("UPDATE trade_proposals SET expires_at=? WHERE id=?", (expired_at, expired["proposal_id"]))
    expired["expires_at"] = expired_at
    result = Executor(broker, SpyRisk(), storage, "run").execute(expired, context, approval_id="approval-1")
    assert not result.submitted
    assert "expired" in result.reason
    assert not broker.called


def test_recovery_reuses_linked_intent_without_second_broker_call(safe_config, proposal, context, tmp_path):
    broker = Broker()
    storage = Storage(tmp_path / "recovery.db")
    storage.initialize()
    context["approval_valid"] = True
    approved = authorize(storage, {**proposal, "status": "approved"})
    first = Executor(broker, SpyRisk(), storage, "run").execute(approved, context, approval_id="approval-1")
    second = Executor(broker, SpyRisk(), storage, "restart").execute(approved, context)
    assert first.submitted and second.status == "submitted"
    assert broker.called
