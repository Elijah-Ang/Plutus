#!/bin/zsh
set -euo pipefail

SOURCE="${0:A:h:h}"
RELEASE_ROOT="${RELEASE_ROOT:-$HOME/TradingAgentReleases}"
PYTHON_BIN="${PYTHON_BIN:-python}"
REQUIRED_PYTHON="3.13.9"
PINNED_PIP="25.3"
AUTHORITY="forward"
TAG=""
RELEASE_ID=""

while (( $# )); do
  case "$1" in
    --authority) AUTHORITY="${2:?missing authority}"; shift 2 ;;
    --tag) TAG="${2:?missing tag}"; shift 2 ;;
    *) [[ -z "$RELEASE_ID" ]] || { print -u2 -- "unexpected argument: $1"; exit 2; }; RELEASE_ID="$1"; shift ;;
  esac
done
[[ "$AUTHORITY" == "forward" || "$AUTHORITY" == "rollback" ]] || { print -u2 -- "authority must be forward or rollback"; exit 2; }
[[ "$AUTHORITY" == "forward" || "$TAG" == immutable-release-* ]] || { print -u2 -- "rollback builds require --tag immutable-release-*"; exit 2; }
[[ "$($PYTHON_BIN -c 'import platform; print(platform.python_version())')" == "$REQUIRED_PYTHON" ]] || {
  print -u2 -- "release builds require Python $REQUIRED_PYTHON"; exit 2
}
DIRTY=$(git -C "$SOURCE" status --porcelain)
[[ -z "$DIRTY" ]] || { print -u2 -- "refusing dirty source worktree"; exit 2; }
COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
BRANCH=$(git -C "$SOURCE" branch --show-current)
REMOTE_MAIN=$(git -C "$SOURCE" ls-remote origin refs/heads/main | awk '{print $1}')
[[ -n "$REMOTE_MAIN" ]] || { print -u2 -- "remote main identity unavailable"; exit 2; }
if [[ "$AUTHORITY" == "forward" && "$COMMIT" != "$REMOTE_MAIN" ]]; then
  print -u2 -- "forward release builds require HEAD to equal the exact current remote main SHA"; exit 2
fi

RELEASE_ID="${RELEASE_ID:-${COMMIT:0:12}}"
[[ -n "$RELEASE_ID" && "$RELEASE_ID" != *[^A-Za-z0-9._-]* ]] || {
  print -u2 -- "release id contains unsafe characters"; exit 2
}
DEST="$RELEASE_ROOT/$RELEASE_ID"
[[ ! -e "$DEST" ]] || { print -u2 -- "release already exists: $DEST"; exit 2; }
ELIGIBILITY=$(mktemp -t plutus-release-eligibility.XXXXXX)
mkdir -p "$RELEASE_ROOT"
LOCK="$DEST.building"
LOCK_HELD=0
STAGING=""
cleanup() {
  rm -f "$ELIGIBILITY"
  if [[ -d "$STAGING" ]]; then
    chmod -R u+w "$STAGING" 2>/dev/null || true
    rm -rf "$STAGING"
  fi
  (( LOCK_HELD == 0 )) || rmdir "$LOCK"
}
trap cleanup EXIT
mkdir "$LOCK" || { print -u2 -- "release build is already reserved: $LOCK"; exit 2; }
LOCK_HELD=1
STAGING=$(mktemp -d "$RELEASE_ROOT/.${RELEASE_ID}.staging.XXXXXX")

cd "$SOURCE"
# Remote CI and authority are checked before spending time on the build. Local
# tests are intentionally supplied later by the fresh artifact interpreter.
"$PYTHON_BIN" scripts/check_release_eligibility.py --skip-tests --repository Elijah-Ang/Plutus > "$ELIGIBILITY" || true
"$PYTHON_BIN" - "$ELIGIBILITY" "$COMMIT" "$AUTHORITY" "$TAG" <<'PY'
import json, sys
r=json.load(open(sys.argv[1], encoding="utf-8")); sha,mode,tag=sys.argv[2:]
if r.get("commit_sha") != sha or not r.get("github_ci", {}).get("passed"):
    raise SystemExit("exact-SHA GitHub CI did not pass")
reach=r.get("release_reachability", {})
if mode == "forward" and reach.get("remote_main_sha") != sha:
    raise SystemExit("forward authority is not exact current main")
if mode == "rollback" and tag not in set(reach.get("verified_release_manifest") or []):
    raise SystemExit("rollback tag lacks verified immutable release authority")
PY

git -C "$SOURCE" archive --format=tar "$COMMIT" | tar -x -C "$STAGING" -f -
"$PYTHON_BIN" "$STAGING/scripts/verify_source_tree.py" \
  --root "$STAGING" --inventory "$STAGING/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus --commit "$COMMIT" --create
"$PYTHON_BIN" "$STAGING/scripts/verify_source_tree.py" \
  --root "$STAGING" --inventory "$STAGING/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus
"$PYTHON_BIN" -m venv --copies "$STAGING/.venv"
[[ "$("$STAGING/.venv/bin/python" -c 'import platform; print(platform.python_version())')" == "$REQUIRED_PYTHON" ]] || {
  print -u2 -- "fresh release environment has the wrong Python"; exit 2
}
"$STAGING/.venv/bin/python" -m pip install "pip==$PINNED_PIP"
"$STAGING/.venv/bin/python" -m pip install --require-hashes --requirement "$STAGING/requirements-hashes.lock"
"$STAGING/.venv/bin/python" "$STAGING/scripts/build_isolated_wheel.py" \
  --repository-root "$SOURCE" --staging-root "$STAGING" \
  --inventory "$STAGING/tracked-source-inventory.json" --commit "$COMMIT" \
  --python "$STAGING/.venv/bin/python" --workspace-parent "$RELEASE_ROOT" \
  --repository Elijah-Ang/Plutus
WHEEL_PATH=$(
  "$STAGING/.venv/bin/python" -c \
  'import json,pathlib,sys; e=json.load(open(sys.argv[1],encoding="utf-8")); print(pathlib.Path(sys.argv[2],"release-wheel",e["wheel_filename"]))' \
  "$STAGING/wheel-build-evidence.json" "$STAGING"
)
[[ -f "$WHEEL_PATH" ]] || { print -u2 -- "verified release wheel is missing"; exit 2; }
"$STAGING/.venv/bin/python" -m pip install --no-deps --no-index "$WHEEL_PATH"
"$STAGING/.venv/bin/python" "$STAGING/scripts/verify_source_tree.py" \
  --root "$STAGING" --inventory "$STAGING/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus

cd "$STAGING"
export TRADING_AGENT_PROJECT_ROOT="$STAGING"
"$STAGING/.venv/bin/python" scripts/run_artifact_tests.py --output artifact-test-results.json
"$STAGING/.venv/bin/python" scripts/verify_source_tree.py \
  --root "$STAGING" --inventory "$STAGING/tracked-source-inventory.json" \
  --repository Elijah-Ang/Plutus
"$STAGING/.venv/bin/python" -m pip freeze --all | LC_ALL=C sort > "$STAGING/dependency-inventory.txt"

LOCK_HASH=$(shasum -a 256 "$STAGING/requirements.lock" | awk '{print $1}')
HASH_LOCK_HASH=$(shasum -a 256 "$STAGING/requirements-hashes.lock" | awk '{print $1}')
INVENTORY_HASH=$(shasum -a 256 "$STAGING/dependency-inventory.txt" | awk '{print $1}')
TEST_RESULTS_HASH=$(shasum -a 256 "$STAGING/artifact-test-results.json" | awk '{print $1}')
SOURCE_INVENTORY_HASH=$(shasum -a 256 "$STAGING/tracked-source-inventory.json" | awk '{print $1}')
WHEEL_EVIDENCE_HASH=$(shasum -a 256 "$STAGING/wheel-build-evidence.json" | awk '{print $1}')
SOURCE_TREE_SHA=$("$STAGING/.venv/bin/python" -c 'import json; print(json.load(open("tracked-source-inventory.json"))["git_tree_sha"])')
SOURCE_INVENTORY_DIGEST=$("$STAGING/.venv/bin/python" -c 'import json; print(json.load(open("tracked-source-inventory.json"))["inventory_digest"])')
WHEEL_SHA=$("$STAGING/.venv/bin/python" -c 'import json; print(json.load(open("wheel-build-evidence.json"))["wheel_sha256"])')
WHEEL_NAME=$("$STAGING/.venv/bin/python" -c 'import json; print(json.load(open("wheel-build-evidence.json"))["distribution_name"])')
WHEEL_VERSION=$("$STAGING/.venv/bin/python" -c 'import json; print(json.load(open("wheel-build-evidence.json"))["distribution_version"])')
WHEEL_FILE=$("$STAGING/.venv/bin/python" -c 'import json; print(json.load(open("wheel-build-evidence.json"))["wheel_filename"])')
CI_RUN_ID=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["github_ci"]["run_id"])' "$ELIGIBILITY")
CI_WORKFLOW=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["github_ci"]["workflow_name"])' "$ELIGIBILITY")
AUTHORITY_EVIDENCE=$(SOURCE_TREE_SHA="$SOURCE_TREE_SHA" SOURCE_INVENTORY_DIGEST="$SOURCE_INVENTORY_DIGEST" \
"$PYTHON_BIN" - "$ELIGIBILITY" "$AUTHORITY" "$TAG" <<'PY'
import json,os,sys
r=json.load(open(sys.argv[1])); mode,tag=sys.argv[2:]
reach=r.get("release_reachability", {})
value={"mode":mode,"remote_main_sha":reach.get("remote_main_sha"),
       "source_tree_sha":os.environ["SOURCE_TREE_SHA"],
       "tracked_source_inventory_digest":os.environ["SOURCE_INVENTORY_DIGEST"]}
if mode=="rollback":
    value.update(tag_name=tag,attestation=(reach.get("release_attestation_evidence") or {}).get(tag))
print(json.dumps(value,sort_keys=True,separators=(",",":")))
PY
)

RELEASE_ID="$RELEASE_ID" COMMIT="$COMMIT" BRANCH="$BRANCH" LOCK_HASH="$LOCK_HASH" \
HASH_LOCK_HASH="$HASH_LOCK_HASH" INVENTORY_HASH="$INVENTORY_HASH" TEST_RESULTS_HASH="$TEST_RESULTS_HASH" \
SOURCE_INVENTORY_HASH="$SOURCE_INVENTORY_HASH" SOURCE_TREE_SHA="$SOURCE_TREE_SHA" \
SOURCE_INVENTORY_DIGEST="$SOURCE_INVENTORY_DIGEST" \
WHEEL_EVIDENCE_HASH="$WHEEL_EVIDENCE_HASH" WHEEL_SHA="$WHEEL_SHA" WHEEL_NAME="$WHEEL_NAME" \
WHEEL_VERSION="$WHEEL_VERSION" WHEEL_FILE="$WHEEL_FILE" \
CI_RUN_ID="$CI_RUN_ID" CI_WORKFLOW="$CI_WORKFLOW" AUTHORITY_EVIDENCE="$AUTHORITY_EVIDENCE" \
"$STAGING/.venv/bin/python" - <<'PY'
import json, os, platform
from datetime import UTC, datetime
from pathlib import Path
from app.formula_versions import REQUIRED_SCHEMA_VERSIONS
from scripts.check_release_eligibility import RELEASE_FORMULA_VERSIONS
from app.utils import load_config
from app.runtime_guard import REQUIRED_SCHEMA_VERSION
tests=json.load(open("artifact-test-results.json",encoding="utf-8"))
if tests.get("tests_verified") is not True: raise SystemExit("artifact tests are not verified")
config=load_config()
manifest={
  "release_id":os.environ["RELEASE_ID"],"release_commit":os.environ["COMMIT"],
  "schema_version":REQUIRED_SCHEMA_VERSION,"required_schema_versions":sorted(REQUIRED_SCHEMA_VERSIONS),
  "formula_versions":RELEASE_FORMULA_VERSIONS,"configuration_hash":config.get("effective_config_hash"),
  "mode":"paper","manual_approval_only":True,"live_capability":False,
  "authority_model":"exact_current_main_or_verified_immutable_release_rollback",
  "release_authority":json.loads(os.environ["AUTHORITY_EVIDENCE"]),
  "ci":{"workflow_name":os.environ["CI_WORKFLOW"],"run_id":os.environ["CI_RUN_ID"],"head_sha":os.environ["COMMIT"]},
  "built_at_utc":datetime.now(UTC).isoformat(),"python_version":platform.python_version(),
  "requirements_lock_sha256":os.environ["LOCK_HASH"],
  "requirements_hash_lock_sha256":os.environ["HASH_LOCK_HASH"],
  "dependency_inventory_sha256":os.environ["INVENTORY_HASH"],
  "artifact_test_results_sha256":os.environ["TEST_RESULTS_HASH"],
  "git_tree_sha":os.environ["SOURCE_TREE_SHA"],
  "tracked_source_inventory_sha256":os.environ["SOURCE_INVENTORY_HASH"],
  "tracked_source_inventory_digest":os.environ["SOURCE_INVENTORY_DIGEST"],
  "wheel_build_evidence_sha256":os.environ["WHEEL_EVIDENCE_HASH"],
  "release_wheel_sha256":os.environ["WHEEL_SHA"],
  "release_wheel_filename":os.environ["WHEEL_FILE"],
  "distribution_name":os.environ["WHEEL_NAME"],
  "distribution_version":os.environ["WHEEL_VERSION"],
  "artifact_test_results":tests["results"],
  "source_branch":os.environ["BRANCH"],"tests_verified":True,
}
Path("release-manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
PY

"$STAGING/.venv/bin/python" scripts/verify_release_artifact.py "$STAGING"
rm -rf "$STAGING/.git" "$STAGING/data" "$STAGING/logs" "$STAGING/scratch"
find . -type f ! -name 'release-file-inventory.sha256' -print0 \
  | LC_ALL=C sort -z | xargs -0 shasum -a 256 > "$STAGING/release-file-inventory.sha256"
chmod 755 "$STAGING/scripts/run_once.sh" "$STAGING/scripts/run_telegram_listener.sh"
find "$STAGING" -type d -exec chmod a-w {} +
find "$STAGING" -type f -exec chmod a-w {} +
[[ ! -e "$DEST" ]] || { print -u2 -- "release destination appeared during construction"; exit 2; }
mv "$STAGING" "$DEST"
STAGING=""
rmdir "$LOCK"
LOCK_HELD=0
print -- "$DEST"
