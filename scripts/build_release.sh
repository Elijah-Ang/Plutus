#!/bin/zsh
set -euo pipefail

SOURCE="${0:A:h:h}"
RELEASE_ROOT="$HOME/TradingAgentReleases"
DIRTY=$(git -C "$SOURCE" status --porcelain)
[[ -z "$DIRTY" ]] || { print -u2 -- "refusing dirty source worktree"; exit 2; }
COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
BRANCH=$(git -C "$SOURCE" branch --show-current)
REMOTE_MAIN=$(git -C "$SOURCE" ls-remote origin refs/heads/main | awk '{print $1}')
[[ -n "$REMOTE_MAIN" && "$COMMIT" == "$REMOTE_MAIN" ]] || {
  print -u2 -- "release builds require HEAD to equal the current remote main SHA"; exit 2
}
RELEASE_ID="${1:-${COMMIT:0:12}}"
DEST="$RELEASE_ROOT/$RELEASE_ID"
[[ ! -e "$DEST" ]] || { print -u2 -- "release already exists: $DEST"; exit 2; }

ELIGIBILITY=$(mktemp -t plutus-release-eligibility.XXXXXX)
mkdir -p "$RELEASE_ROOT"
mkdir "$DEST"
STAGING="$DEST"
cleanup() { rm -f "$ELIGIBILITY"; [[ -d "$STAGING" ]] && rm -rf "$STAGING"; }
trap cleanup EXIT

cd "$SOURCE"
python3 scripts/check_release_eligibility.py --repository Elijah-Ang/Plutus > "$ELIGIBILITY"
python3 - "$ELIGIBILITY" "$COMMIT" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
if not report.get("release_eligible") or report.get("commit_sha") != sys.argv[2]:
    raise SystemExit("exact-SHA release eligibility did not pass")
if report.get("release_reachability", {}).get("remote_main_sha") != sys.argv[2]:
    raise SystemExit("eligibility authority is not the exact remote main SHA")
PY

git -C "$SOURCE" archive --format=tar "$COMMIT" | tar -x -C "$STAGING" -f -
python3 -m venv "$STAGING/.venv"
"$STAGING/.venv/bin/python" -m pip install --upgrade pip
"$STAGING/.venv/bin/python" -m pip install --requirement "$STAGING/requirements.lock"
"$STAGING/.venv/bin/python" -m pip install --no-deps "$STAGING"

LOCK_HASH=$(shasum -a 256 "$STAGING/requirements.lock" | awk '{print $1}')
CI_RUN_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["github_ci"]["run_id"])' "$ELIGIBILITY")
CI_WORKFLOW=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["github_ci"]["workflow_name"])' "$ELIGIBILITY")
"$STAGING/.venv/bin/python" -m pip freeze --all | LC_ALL=C sort > "$STAGING/dependency-inventory.txt"
INVENTORY_HASH=$(shasum -a 256 "$STAGING/dependency-inventory.txt" | awk '{print $1}')

cd "$STAGING"
RELEASE_ID="$RELEASE_ID" COMMIT="$COMMIT" BRANCH="$BRANCH" LOCK_HASH="$LOCK_HASH" \
INVENTORY_HASH="$INVENTORY_HASH" CI_RUN_ID="$CI_RUN_ID" CI_WORKFLOW="$CI_WORKFLOW" \
"$STAGING/.venv/bin/python" - <<'PY'
import json, os, platform
from datetime import UTC, datetime
from pathlib import Path
from app.formula_versions import REQUIRED_SCHEMA_VERSIONS
from scripts.check_release_eligibility import RELEASE_FORMULA_VERSIONS
from app.utils import load_config
from app.runtime_guard import REQUIRED_SCHEMA_VERSION
config = load_config()
manifest = {
  "release_id": os.environ["RELEASE_ID"], "release_commit": os.environ["COMMIT"],
  "schema_version": REQUIRED_SCHEMA_VERSION, "required_schema_versions": sorted(REQUIRED_SCHEMA_VERSIONS),
  "formula_versions": RELEASE_FORMULA_VERSIONS,
  "configuration_hash": config.get("effective_config_hash"), "mode": "paper",
  "manual_approval_only": True, "live_capability": False,
  "authority_model": "exact_github_main_or_annotated_tag_with_release_attestation_asset",
  "ci": {"workflow_name": os.environ["CI_WORKFLOW"], "run_id": os.environ["CI_RUN_ID"], "head_sha": os.environ["COMMIT"]},
  "built_at_utc": datetime.now(UTC).isoformat(), "python_version": platform.python_version(),
  "requirements_lock_sha256": os.environ["LOCK_HASH"],
  "dependency_inventory_sha256": os.environ["INVENTORY_HASH"],
  "source_branch": os.environ["BRANCH"], "tests_verified": True,
}
Path("release-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

rm -rf "$STAGING/.git" "$STAGING/data" "$STAGING/logs" "$STAGING/scratch"
find . -type f ! -name 'release-file-inventory.sha256' -print0 \
  | LC_ALL=C sort -z | xargs -0 shasum -a 256 > "$STAGING/release-file-inventory.sha256"
chmod 755 "$STAGING/scripts/run_once.sh" "$STAGING/scripts/run_telegram_listener.sh"
find "$STAGING" -type d -exec chmod a-w {} +
find "$STAGING" -type f -exec chmod a-w {} +
STAGING=""
print -- "$DEST"
