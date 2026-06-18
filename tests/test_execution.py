from app.execution import Executor
from app.risk_engine import RiskDecision


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


def test_execution_requires_final_revalidation(safe_config, proposal, context):
    broker = Broker()
    risk = SpyRisk()
    proposal["status"] = "approved"
    context["approval_valid"] = True
    result = Executor(broker, risk).execute(proposal, context)
    assert result.submitted
    assert risk.called_final
    assert broker.called


def test_execution_rejects_pending_proposal(safe_config, proposal, context):
    broker = Broker()
    context["approval_valid"] = True
    assert not Executor(broker, SpyRisk()).execute(proposal, context).submitted
    assert not broker.called
