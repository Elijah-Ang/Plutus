#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
find "$ROOT/logs/runtime" -type f -mtime +30 -delete
find "$ROOT/logs/errors" -type f -mtime +180 -delete
find "$ROOT/logs/audit" -type f -mtime +30 -exec gzip -f {} \;
echo "Log retention applied; audit logs were archived, not deleted."
