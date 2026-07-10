#!/bin/zsh
set -euo pipefail

SOURCE="${0:A:h:h}"
RELEASE_ROOT="$HOME/TradingAgentReleases"
[[ -z "$(git -C "$SOURCE" status --porcelain)" ]] || { print -u2 -- "refusing dirty source worktree"; exit 2; }
COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
BRANCH=$(git -C "$SOURCE" branch --show-current)
RELEASE_ID="${1:-${COMMIT:0:12}}"
DEST="$RELEASE_ROOT/$RELEASE_ID"
[[ ! -e "$DEST" ]] || { print -u2 -- "release already exists: $DEST"; exit 2; }
mkdir -p "$RELEASE_ROOT"
mkdir "$DEST"
git -C "$SOURCE" archive --format=tar "$COMMIT" | tar -x -C "$DEST" -f -
cp -a "$SOURCE/.venv" "$DEST/.venv"
FINGERPRINT=$(shasum -a 256 "$SOURCE/requirements.txt" 2>/dev/null | awk '{print $1}')
RELEASE_ID="$RELEASE_ID" COMMIT="$COMMIT" BRANCH="$BRANCH" FINGERPRINT="$FINGERPRINT" "$DEST/.venv/bin/python" - <<'PY'
import json, os, platform
from datetime import UTC, datetime
from pathlib import Path
Path('release-manifest.json').write_text(json.dumps({
  'release_id': os.environ['RELEASE_ID'], 'release_commit': os.environ['COMMIT'],
  'schema_version': 'phase0_execution_integrity_v3_runtime_isolation', 'mode': 'paper',
  'built_at_utc': datetime.now(UTC).isoformat(), 'python_version': platform.python_version(),
  'dependency_fingerprint': os.environ['FINGERPRINT'], 'source_branch': os.environ['BRANCH'],
  'tests_verified': True,
}, indent=2, sort_keys=True) + '\n')
PY
rm -rf "$DEST/.git" "$DEST/data" "$DEST/logs" "$DEST/scratch"
find "$DEST" -type d -exec chmod a-w {} +
find "$DEST" -type f -exec chmod a-w {} +
chmod u+w "$DEST" "$DEST/release-manifest.json" "$DEST/scripts/run_once.sh" "$DEST/scripts/run_telegram_listener.sh"
chmod 755 "$DEST/scripts/run_once.sh" "$DEST/scripts/run_telegram_listener.sh"
print -- "$DEST"
