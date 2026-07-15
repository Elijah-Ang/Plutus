# Release verification and rollback

A Plutus release is reviewable only as one exact Git commit. Run:

```bash
python -m compileall app tests scripts
pytest -q
python scripts/check_release_eligibility.py
```

The eligibility report is fail-closed. It verifies a clean worktree, configuration hash, schema versions, additive migration idempotence and runtime compatibility, paper-only/manual-only identity, local tests, and the GitHub `CI` run for the exact SHA. Main authority requires exact equality with the current GitHub `main` SHA; ancestry is never sufficient. The alternative authority is an annotated `immutable-release-*` tag whose GitHub Release contains one immutable `release-attestation.json` asset bound to the tag, commit, configuration, schema/formula versions, and exact successful CI run. The verifier never expects a build-generated manifest to exist inside its own source commit.

`build_release.sh` creates a clean virtual environment from `requirements.lock`, records the complete installed dependency inventory, and hashes every release file. It refuses any commit other than the current remote `main` and consumes the exact successful CI identity. `deploy_release.sh` rechecks the file and dependency inventories, exact current `main`, exact GitHub CI run/job, and paper/manual-only capability before it can switch the runtime pointer.

Before a production-paper cutover, make a SQLite backup, run the migration proof against a clone, and verify restoration. Build an immutable release from the reviewed SHA; do not edit the release directory. Confirm the release manifest, runtime symlink, launchd scanner/listener commit freshness, Alpaca paper identity, migration ledger, and durable execution integrity after cutover.

Rollback changes scanner and listener together to the last compatible immutable release. Additive schema may remain. For an exact database rollback, stop both writers and restore the verified pre-migration backup. A code branch, pull request, or local eligibility run must never migrate production, restart either process, or change the runtime pointer.
