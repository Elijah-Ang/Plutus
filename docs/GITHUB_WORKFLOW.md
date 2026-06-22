# GitHub Workflow Guidelines

This document outlines the rules and best practices for contributing to the Plutus (TradingAgent) repository. Following these instructions ensures that no credentials, local databases, logs, or Excel reports are committed to GitHub.

## 1. Tracked vs. Ignored Files

### Never Commit (Ignored)
- `.env` / `.env.*` (sensitive environment files with API keys)
- `data/*.db` / `data/*.db-*` (active SQLite databases and journaling files)
- `data/backups/` (copies of SQLite database files)
- `data/exports/` (Excel reports and CSV exports)
- `data/market_cache/` (cache directories)
- `logs/` (logs from runtime, errors, or audits)
- `.venv/` (Python virtual environments)
- `__pycache__/`, `*.py[cod]` (Python compilation files)
- `config/KILL_SWITCH` (local safety switch file)
- Raw Telegram update/dump files and local diagnostic payloads
- Model files (`.joblib`, `.pkl`) and package/cache artifacts

### Always Safe to Commit
- Core source files under `app/` (e.g. `app/main.py`, `app/service.py`)
- Configuration templates (e.g. `config/config.yaml`, `config/risk_limits.yaml`, `config/strategies.yaml`)
- Shell/Python utility scripts under `scripts/` (e.g. `scripts/run_once.sh`)
- Automated tests under `tests/`
- Documentation files under `docs/` and `README.md`
- Dependency listings (`pyproject.toml`)

## 2. Commit and Push Checklist

Before committing or pushing code:
1. **Check Status**: Run `git status` to ensure only safe files are staged.
2. **Review Diff**: Run `git diff --cached` to verify that no API keys or personal identifiers are written into code.
3. **Run Tests**: Verify all tests pass by running `.venv/bin/pytest`.
4. **Run Secret Scan**: Use `scripts/safe_commit_push.sh` to check staged files for supported secret-value patterns and forbidden runtime paths. This is defense in depth, not a substitute for reviewing `git diff --cached` or a maintained secret scanner.

The helper scans staged blobs only. It does not certify Git history, unstaged files, or arbitrary unknown secret formats. For sensitive releases, also use a maintained scanner such as Gitleaks and review the staged diff manually.

## 3. Handling Suspected Secret Exposure

If you suspect that a secret (such as `OPENAI_API_KEY`, `ALPACA_API_KEY`, or `TELEGRAM_BOT_TOKEN`) has been committed:
1. **Rotate immediately**: Revoke the compromised secret from the provider (OpenAI, Alpaca, or Telegram) and generate a new one.
2. **Remove from history**: Use tools like `git-filter-repo` or `BFG Repo-Cleaner` to purge the secret from all branches and git commit history.
3. **Update local configuration**: Store the fresh secret in the macOS Keychain or in the local `.env` file (which is gitignored).
