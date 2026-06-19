#!/usr/bin/env bash
# safe_commit_push.sh - Safely test, scan, commit, and push changes to GitHub.
set -euo pipefail

# 1. Check for commit message argument
if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "Usage: $0 \"Your commit message\"" >&2
    exit 1
fi
COMMIT_MSG="$1"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Step 1: Running unit tests ==="
if ! "$ROOT/.venv/bin/pytest"; then
    echo "ERROR: Unit tests failed. Aborting commit." >&2
    exit 1
fi

echo "=== Step 2: Verifying docs/SYSTEM_OVERVIEW.md exists ==="
if [ ! -f "$ROOT/docs/SYSTEM_OVERVIEW.md" ]; then
    echo "ERROR: docs/SYSTEM_OVERVIEW.md is missing. Aborting." >&2
    exit 1
fi

echo "=== Step 3: Checking for staged sensitive/ignored files ==="
# Get staged file list
STAGED_FILES=$(git diff --cached --name-only)

if [ -z "$STAGED_FILES" ]; then
    echo "ERROR: No files are staged for commit. Use git add first." >&2
    exit 1
fi

# Block forbidden extensions or paths
for f in $STAGED_FILES; do
    # Check if the file is .env, .db, .xlsx, .csv, .log, or in logs/ or backups/
    if [[ "$f" == *".env"* ]] || [[ "$f" == *".db"* ]] || [[ "$f" == *".xlsx"* ]] || [[ "$f" == *".csv"* ]] || [[ "$f" == *".log"* ]] || [[ "$f" == "logs/"* ]] || [[ "$f" == "data/backups/"* ]]; then
        echo "ERROR: Staged forbidden file detected: $f. Aborting commit." >&2
        exit 1
    fi
done

echo "=== Step 4: Scanning staged files for secrets ==="
# Run a Python script inline to check for secret patterns in staged files
"$ROOT/.venv/bin/python" -c "
import sys, subprocess, re

SENSITIVE_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9]{32,48}'),
    re.compile(r'ALPACA_SECRET_KEY\s*=\s*[\'\"][a-zA-Z0-9]{32,48}[\'\"]'),
    re.compile(r'TELEGRAM_BOT_TOKEN\s*=\s*[\'\"][0-9]+:[a-zA-Z0-9_-]{35}[\'\"]'),
]

staged = subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True).splitlines()
has_secret = False
for f in staged:
    try:
        content = subprocess.check_output(['git', 'show', f':{f}'], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        # File might be deleted or binary
        continue
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(content):
            print(f'ERROR: Suspected secret pattern found in staged file: {f}', file=sys.stderr)
            has_secret = True

if has_secret:
    sys.exit(1)
" || {
    echo "ERROR: Secret scan failed. Aborting commit." >&2
    exit 1
}

echo "=== Step 5: Showing staged files ==="
git status --short

echo "=== Step 6: Committing changes ==="
git commit -m "$COMMIT_MSG"

echo "=== Step 7: Pushing to GitHub ==="
git push origin "$(git branch --show-current)"

echo "=== Safe commit and push completed successfully! ==="
