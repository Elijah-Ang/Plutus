# Release verification and rollback

A Plutus release is reviewable only as one exact Git commit. Use Python 3.13.9 (the same exact version required by CI and the release builder), then run:

```bash
python -m compileall app tests scripts
pytest -q
python scripts/check_release_eligibility.py
```

The eligibility report is fail-closed. It verifies a clean worktree, configuration hash, schema versions, additive migration idempotence and runtime compatibility, paper-only/manual-only identity, local tests, and the GitHub `CI` run for the exact SHA. Main authority requires exact equality with the current GitHub `main` SHA; ancestry is never sufficient. The alternative authority is an annotated `immutable-release-*` tag whose GitHub Release contains one immutable `release-attestation.json` asset bound to the tag, commit, configuration, schema/formula versions, and exact successful CI run. The verifier never expects a build-generated manifest to exist inside its own source commit.

`build_release.sh` requires Python 3.13.9 and constructs the release in a temporary staging directory that is atomically promoted only after every gate succeeds. It installs pinned pip 25.3 plus hash-locked setuptools 80.9.0 and dependencies into a fresh virtual environment. Package construction uses a second temporary Git archive outside release staging, verifies that archive against the same authoritative commit tree, builds exactly one wheel without build isolation or dependency resolution, verifies its name and version, and retains its SHA-256 and source-tree binding as generated artifact evidence. Every `app/` member in the wheel is compared byte-for-byte with the authoritative tracked-source inventory, so replacing the wheel and regenerating local evidence cannot bless changed executable package code. Duplicate or unsafe archive paths, non-regular package members, and any payload outside the exact `app/` tree and required dist-info records are rejected. The exact wheel is installed with `--no-deps --no-index`; the temporary build workspace is then removed. `build/`, `dist/`, and project egg-info output remain prohibited in immutable staging.

The release interpreter then proves that `app` imports from the installed wheel rather than source staging, runs compileall, configuration validation, a twice-applied fresh migration proof, the targeted safety suites, and full pytest. Only those successful artifact results set `tests_verified=true`. `deploy_release.sh` recomputes every file hash, the retained wheel evidence, every installed non-bytecode `app/` file, `pip freeze`, the effective configuration hash, schema/formula versions, Python version, and the exact successful CI identity before it can switch the runtime pointer.

Source-tree authority and generated-artifact integrity are deliberately separate. GitHub's commit and Git tree are the independent authority for every tracked source byte; neither the wheel evidence nor a regenerated release-file inventory can bless altered tracked source. The wheel, virtual environment, test results, dependency inventory, manifest, and final file inventory are generated evidence covered by artifact verification and the final inventory. Their integrity proves what was constructed and tested from the authoritative source tree, but does not replace that source authority.

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
