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
if git diff --cached --quiet; then
    echo "ERROR: No files are staged for commit. Use git add first." >&2
    exit 1
fi

"$ROOT/.venv/bin/python" - <<'PY'
import subprocess, sys

blocked_prefixes = ("logs/", "data/", ".venv/", ".pytest_cache/", "__pycache__/", "trading_agent.egg-info/")
blocked_suffixes = (".env", ".db", ".db-wal", ".db-shm", ".xlsx", ".xls", ".csv", ".log", ".joblib", ".pkl", ".pyc")
staged = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"]).decode().split("\0")
bad = []
for path in filter(None, staged):
    lower = path.lower()
    if path == ".env" or path.startswith(blocked_prefixes) or lower.endswith(blocked_suffixes) or "telegram" in lower and "update" in lower and lower.endswith(".json"):
        # Tracked .gitkeep files are never expected in an ordinary change; fail closed.
        bad.append(path)
if bad:
    for path in bad:
        print(f"ERROR: Staged forbidden runtime/sensitive file: {path}", file=sys.stderr)
    raise SystemExit(1)
PY

echo "=== Step 4: Scanning staged files for secrets ==="
# Run a Python script inline to check for secret patterns in staged files
"$ROOT/.venv/bin/python" -c "
import sys, subprocess, re

SENSITIVE_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9_-]{20,}'),
    re.compile(r'[0-9]{6,}:[a-zA-Z0-9_-]{20,}'),
    re.compile(r'PK[A-Z0-9]{15,}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'(?:ALPACA_SECRET_KEY|OPENAI_API_KEY|TELEGRAM_BOT_TOKEN)\s*[=:]\s*[\'\"]?(?!replace_with_|\[REDACTED)[^\s\'\"]{12,}', re.I),
]

staged = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '-z']).decode().split('\0')
has_secret = False
for f in filter(None, staged):
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
