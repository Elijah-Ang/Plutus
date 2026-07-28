#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
if [[ "${1:-}" != "CONFIRM PAPER RESUME" || "$#" -ne 1 ]]; then
  print -u2 -- 'usage: start_agent.sh "CONFIRM PAPER RESUME"'
  print -u2 -- "this resumes paper/manual-only operation; it does not load or restart launchd jobs"
  exit 2
fi
exec "$ROOT/scripts/manage_kill_switch.sh" disable --confirm "$1"
