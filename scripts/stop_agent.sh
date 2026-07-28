#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
exec "$ROOT/scripts/manage_kill_switch.sh" enable
