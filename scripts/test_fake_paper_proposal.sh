#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
export TRADING_AGENT_LOCK_HELD=1
exec "$ROOT/.venv/bin/python" scripts/test_fake_paper_proposal.py
