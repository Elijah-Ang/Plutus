from datetime import UTC, datetime, timedelta

from app.risk_engine import RiskEngine


def test_safe_paper_proposal_passes(safe_config, proposal, context):
    assert RiskEngine(safe_config).evaluate(proposal, context).passed


def test_blocks_live_by_default(safe_config, proposal, context):
    safe_config["mode"] = "live"
    assert not RiskEngine(safe_config).evaluate(proposal, context).passed


def test_blocks_unplugged(safe_config, proposal, context):
    context["power_connected"] = False
    assert not RiskEngine(safe_config).evaluate(proposal, context).passed


def test_blocks_expired(safe_config, proposal, context):
    proposal["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert not RiskEngine(safe_config).evaluate(proposal, context).passed


def test_final_revalidation_is_required(safe_config, proposal, context):
    proposal["client_order_id"] = "unique"
    context["approval_valid"] = True
    assert not RiskEngine(safe_config).evaluate(proposal, context, final=True).passed
