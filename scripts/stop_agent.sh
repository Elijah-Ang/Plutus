#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
touch "$ROOT/config/KILL_SWITCH"
echo "Kill switch enabled. Existing process will stop at its next gate."
