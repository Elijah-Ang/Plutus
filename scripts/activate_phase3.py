from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.broker_alpaca import AlpacaBroker
from app.phase3_risk import PHASE3_SCHEMA_VERSION, PROFILE_VERSION, Phase3Controller
from app.storage import Storage
from app.utils import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit paper-only Phase 3 activation gate")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--allow-phase3-activation", action="store_true")
    args = parser.parse_args()
    if not args.allow_phase3_activation or os.getenv("TRADINGAGENT_ALLOW_PHASE3_ACTIVATION") != "YES_ACTIVE_PAPER_ONLY":
        raise SystemExit("explicit Phase 3 paper activation authorization required")
    manifest = json.loads(args.release_manifest.read_text())
    if manifest.get("mode") != "paper" or manifest.get("schema_version") != PHASE3_SCHEMA_VERSION:
        raise SystemExit("release is not a Phase 3 paper release")
    config = load_config()
    if not config.get("phase3", {}).get("active") or config.get("mode") != "paper" or config.get("live_enabled") is not False:
        raise SystemExit("active paper-only Phase 3 configuration required")
    storage = Storage(args.database)
    storage.require_runtime_schema(production=True)
    broker = AlpacaBroker(config)
    identity = broker.paper_account_identity()
    account = broker.get_account(); positions = broker.get_positions(); open_orders = broker.get_open_orders()
    if identity.get("verified") is not True or open_orders:
        raise SystemExit("paper identity is ambiguous or broker has open orders")
    equity = float(account.equity); cash = float(account.cash)
    long_value = float(getattr(account, "long_market_value", 0) or 0); short_value = float(getattr(account, "short_market_value", 0) or 0)
    if cash < 0 or short_value < 0 or long_value > equity + 0.01:
        raise SystemExit("no-leverage activation check failed")
    controller = Phase3Controller(storage, config, "phase3-activation")
    healthy, report = controller.reconciliation_health()
    if not healthy:
        raise SystemExit("durable reconciliation health failed")
    drawdown = controller.update_equity(equity)
    if drawdown >= controller.profile.drawdown_halt_pct:
        raise SystemExit("account drawdown is at or above Phase 3 halt")
    states = controller.refresh_strategy_states()
    now = datetime.now(UTC).isoformat()
    storage.execute("INSERT INTO phase3_activation_events VALUES(?,?,?,?,?,?,?,?,?)", (
        str(uuid.uuid4()), manifest["release_commit"], now, "ACTIVE_PAPER",
        json.dumps(identity, sort_keys=True, default=str),
        json.dumps({"equity": equity, "cash": cash, "positions": len(positions), "open_orders": 0, "drawdown_pct": drawdown}, sort_keys=True),
        json.dumps(report, sort_keys=True), json.dumps(states, sort_keys=True), PROFILE_VERSION,
    ))
    print(json.dumps({"status": "ACTIVE_PAPER", "release_commit": manifest["release_commit"], "drawdown_pct": drawdown,
                      "strategy_states": states, "profile": PROFILE_VERSION}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
