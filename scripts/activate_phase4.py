from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.broker_alpaca import AlpacaBroker  # noqa: E402
from app.execution import DurableExecutionStore  # noqa: E402
from app.phase3_risk import Phase3Controller  # noqa: E402
from app.phase4_allocator import (  # noqa: E402
    ALLOCATOR_VERSION,
    PHASE4_SCHEMA_VERSION,
    AdaptiveAllocator,
)
from app.storage import Storage  # noqa: E402
from app.utils import load_config  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="Explicit active adaptive paper allocation gate"
    )
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--release-manifest", type=Path, required=True)
    p.add_argument("--allow-phase4-activation", action="store_true")
    a = p.parse_args()
    if (
        not a.allow_phase4_activation
        or os.getenv("TRADINGAGENT_ALLOW_PHASE4_ACTIVATION")
        != "YES_ACTIVE_ADAPTIVE_PAPER"
    ):
        raise SystemExit("explicit Phase 4 paper activation authorization required")
    manifest = json.loads(a.release_manifest.read_text())
    if (
        manifest.get("mode") != "paper"
        or manifest.get("schema_version") != PHASE4_SCHEMA_VERSION
    ):
        raise SystemExit("release is not a Phase 4 paper release")
    cfg = load_config()
    if (
        cfg.get("phase4", {}).get("mode") != "ACTIVE_ADAPTIVE_PAPER"
        or cfg.get("live_enabled") is not False
    ):
        raise SystemExit("active adaptive paper configuration required")
    s = Storage(a.database)
    s.require_runtime_schema(production=True)
    b = AlpacaBroker(cfg)
    identity = b.paper_account_identity()
    account = b.get_account()
    orders = b.get_open_orders()
    if identity.get("verified") is not True or orders:
        raise SystemExit("paper identity ambiguous or broker has open orders")
    report = DurableExecutionStore(s).integrity_report()
    if any(report.values()):
        raise SystemExit("durable integrity is unhealthy")
    equity = float(account.equity)
    cash = float(account.cash)
    long_value = float(getattr(account, "long_market_value", 0) or 0)
    short_value = float(getattr(account, "short_market_value", 0) or 0)
    if cash < 0 or short_value < 0 or long_value > equity + 0.01:
        raise SystemExit("no-leverage activation check failed")
    drawdown = Phase3Controller(s, cfg, "phase4-activation").update_equity(equity)
    if drawdown >= 6:
        raise SystemExit("Phase 3 drawdown halt is active")
    now = datetime.now(UTC).isoformat()
    result = AdaptiveAllocator(s, cfg, "phase4-activation").run(
        regime="activation_uncertain",
        drawdown_pct=drawdown,
        as_of=now,
        portfolio_snapshot={
            "portfolio_equity": equity,
            "as_of": now,
            "equity_as_of": now,
        },
    )
    s.execute(
        "INSERT INTO phase4_activation_events VALUES(?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            manifest["release_commit"],
            now,
            "ACTIVE_ADAPTIVE_PAPER",
            result["allocation_id"],
            json.dumps(identity, sort_keys=True, default=str),
            json.dumps(
                {
                    "equity": equity,
                    "cash": cash,
                    "open_orders": 0,
                    "drawdown_pct": drawdown,
                },
                sort_keys=True,
            ),
            json.dumps(report, sort_keys=True),
            ALLOCATOR_VERSION,
        ),
    )
    print(
        json.dumps(
            {
                "status": "ACTIVE_ADAPTIVE_PAPER",
                "release_commit": manifest["release_commit"],
                "allocation_id": result["allocation_id"],
                "decision": result["decision"],
                "cash_weight": result["cash_weight"],
                "weights": result["weights"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
