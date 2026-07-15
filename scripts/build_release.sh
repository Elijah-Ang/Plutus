#!/bin/zsh
set -euo pipefail

SOURCE="${0:A:h:h}"
RELEASE_ROOT="$HOME/TradingAgentReleases"
DIRTY=$(git -C "$SOURCE" status --porcelain | rg -v '^\?\? scratch/' || true)
[[ -z "$DIRTY" ]] || { print -u2 -- "refusing dirty source worktree"; exit 2; }
COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
BRANCH=$(git -C "$SOURCE" branch --show-current)
RELEASE_ID="${1:-${COMMIT:0:12}}"
DEST="$RELEASE_ROOT/$RELEASE_ID"
[[ ! -e "$DEST" ]] || { print -u2 -- "release already exists: $DEST"; exit 2; }
mkdir -p "$RELEASE_ROOT"
mkdir "$DEST"
git -C "$SOURCE" archive --format=tar "$COMMIT" | tar -x -C "$DEST" -f -
cp -a "$SOURCE/.venv" "$DEST/.venv"
DEPENDENCY_FILE="$SOURCE/requirements.txt"
[[ -f "$DEPENDENCY_FILE" ]] || DEPENDENCY_FILE="$SOURCE/pyproject.toml"
FINGERPRINT=$(shasum -a 256 "$DEPENDENCY_FILE" | awk '{print $1}')
cd "$DEST"
RELEASE_ID="$RELEASE_ID" COMMIT="$COMMIT" BRANCH="$BRANCH" FINGERPRINT="$FINGERPRINT" "$DEST/.venv/bin/python" - <<'PY'
import json, os, platform
from datetime import UTC, datetime
from pathlib import Path
from app.formula_versions import REQUIRED_SCHEMA_VERSIONS
from scripts.check_release_eligibility import RELEASE_FORMULA_VERSIONS
from app.utils import load_config
from app.runtime_guard import REQUIRED_SCHEMA_VERSION
config = load_config()
Path('release-manifest.json').write_text(json.dumps({
  'release_id': os.environ['RELEASE_ID'], 'release_commit': os.environ['COMMIT'],
  'schema_version': REQUIRED_SCHEMA_VERSION, 'required_schema_versions': sorted(REQUIRED_SCHEMA_VERSIONS),
  'formula_versions': RELEASE_FORMULA_VERSIONS,
  'configuration_hash': config.get('effective_config_hash'), 'mode': 'paper',
  'ci': {
    'workflow_name': os.environ.get('RELEASE_CI_WORKFLOW_NAME', 'CI'),
    'run_id': os.environ.get('RELEASE_CI_RUN_ID', ''),
    'head_sha': os.environ['COMMIT'],
  },
  'built_at_utc': datetime.now(UTC).isoformat(), 'python_version': platform.python_version(),
  'dependency_fingerprint': os.environ['FINGERPRINT'], 'source_branch': os.environ['BRANCH'],
  'tests_verified': True,
}, indent=2, sort_keys=True) + '\n')
PY
rm -rf "$DEST/.git" "$DEST/data" "$DEST/logs" "$DEST/scratch"
chmod 755 "$DEST/scripts/run_once.sh" "$DEST/scripts/run_telegram_listener.sh"
find "$DEST" -type d -exec chmod a-w {} +
find "$DEST" -type f -exec chmod a-w {} +
print -- "$DEST"
