#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h}"
DB="$ROOT/data/trading_agent.db"
DEST="$ROOT/data/backups"
mkdir -p "$DEST"
if [[ ! -f "$DB" ]]; then echo "Database does not exist yet."; exit 0; fi
/usr/bin/sqlite3 "$DB" ".backup '$DEST/trading_agent_$(date +%F_%H%M%S).db'"
find "$DEST" -type f -name '*.db' -mtime +31 -delete
LATEST="$(find "$DEST" -type f -name '*.db' -print | sort | tail -1)"
if [[ "$(date +%u)" == "7" && -n "$LATEST" ]]; then
  tar -czf "$DEST/weekly_$(date +%G-W%V).tar.gz" -C "$ROOT" "${LATEST#$ROOT/}" data/exports
fi
if [[ "$(date +%d)" == "01" && -n "$LATEST" ]]; then
  tar -czf "$DEST/monthly_$(date +%Y-%m).tar.gz" -C "$ROOT" "${LATEST#$ROOT/}" data/exports
fi
echo "Local SQLite backup complete; Sunday and first-of-month archive rules were evaluated."
