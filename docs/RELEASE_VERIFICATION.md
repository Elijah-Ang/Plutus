# Release verification and rollback

A Plutus release is reviewable only as one exact Git commit. Use Python 3.13.9 (the same exact version required by CI and the release builder), then run:

```bash
python -m compileall app tests scripts
pytest -q
python scripts/check_release_eligibility.py
```

The eligibility report is fail-closed. It verifies a clean worktree, configuration hash, schema versions, additive migration idempotence and runtime compatibility, paper-only/manual-only identity, local tests, and the GitHub `CI` run for the exact SHA. Main authority requires exact equality with the current GitHub `main` SHA; ancestry is never sufficient. The alternative authority is an annotated `immutable-release-*` tag whose GitHub Release contains one immutable `release-attestation.json` asset bound to the tag, commit, configuration, schema/formula versions, and exact successful CI run. The verifier never expects a build-generated manifest to exist inside its own source commit.

`build_release.sh` requires Python 3.13.9, installs pinned pip 25.3 plus hash-locked setuptools 80.9.0 and dependencies into a fresh virtual environment, and installs the local package with build isolation disabled. It then runs compileall, configuration validation, a twice-applied fresh migration proof, the targeted safety suites, and full pytest with that environment. Only those successful artifact results set `tests_verified=true`. `deploy_release.sh` recomputes every file hash, `pip freeze`, the effective configuration hash, schema/formula versions, Python version, and the exact successful CI identity before it can switch the runtime pointer.

Ordinary forward deployment is exact-current-main only:

```sh
./scripts/deploy_release.sh --mode forward /Users/elijahang/TradingAgentReleases/<release-id>
```

A rollback is never authorized by ancestry. It requires a previously built immutable artifact whose manifest names one annotated `immutable-release-*` tag and binds the GitHub immutable release ID plus the unique `release-attestation.json` asset ID and SHA-256 digest. Deployment re-fetches the annotated tag, immutable release, asset identity, and digest:

```sh
./scripts/deploy_release.sh --mode rollback /Users/elijahang/TradingAgentReleases/<approved-prior-release-id>
```

The equivalent controlled wrapper is `./scripts/rollback_release.sh /Users/elijahang/TradingAgentReleases/<approved-prior-release-id>`. A lightweight tag, mutable release, duplicate asset, replaced asset, digest mismatch, untagged ancestor, or tag pointing at another commit fails closed.

Before a production-paper cutover, make a SQLite backup, run the migration proof against a clone, and verify restoration. Build an immutable release from the reviewed SHA; do not edit the release directory. Confirm the release manifest, runtime symlink, launchd scanner/listener commit freshness, Alpaca paper identity, migration ledger, and durable execution integrity after cutover.

Rollback changes scanner and listener together to the last compatible immutable release. Additive schema may remain. For an exact database rollback, stop both writers and restore the verified pre-migration backup. A code branch, pull request, or local eligibility run must never migrate production, restart either process, or change the runtime pointer.
