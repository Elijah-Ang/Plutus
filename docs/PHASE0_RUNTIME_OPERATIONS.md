# Runtime operations

Development stays in `/Users/elijahang/Projects/TradingAgent`. It must use an explicit non-production database path. Production runtime resolves only through `/Users/elijahang/TradingAgentRuntime` into `/Users/elijahang/TradingAgentReleases/<release-id>`.

Build a clean release:

```sh
./scripts/build_release.sh
```

Deploy a built release while both jobs are stopped:

```sh
./scripts/deploy_release.sh /Users/elijahang/TradingAgentReleases/<release-id>
```

Use `--mode forward` explicitly for ordinary exact-current-main deployment. Controlled rollback is `./scripts/deploy_release.sh --mode rollback /Users/elijahang/TradingAgentReleases/<approved-prior-release-id>` and succeeds only for the manifest-bound annotated tag plus immutable GitHub Release/attestation asset ID and digest described in `RELEASE_VERIFICATION.md`.

Apply a production migration only during deployment:

```sh
RELEASE=/Users/elijahang/TradingAgentReleases/<release-id>
TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION=YES_I_AM_DEPLOYING \
  "$RELEASE/.venv/bin/python" "$RELEASE/scripts/migrate_runtime_db.py" \
  --database "$HOME/Library/Application Support/TradingAgent/database/trading_agent.sqlite3" \
  --release-manifest "$RELEASE/release-manifest.json" \
  --allow-production-migration
```

The command fails closed unless both launchd writers are absent and the exact
immutable release manifest binds paper/manual-only controls, successful
artifact tests, CI, schema versions, and source-tree authority. It checks disk
capacity before creating a collision-resistant exclusive backup, verifies
`quick_check`, `integrity_check`, foreign keys, schema, page counts, and every
table row count, then applies and verifies the migration twice. The final JSON
must report `"idempotent": true`; retain its backup path, SHA-256, and both
migration-pass evidence objects with the deployment record.

If compatibility requires restoration: stop both jobs, create a new backup of
the current database, restore the selected verified backup with SQLite backup
tooling, run `PRAGMA quick_check`, `PRAGMA integrity_check`, and
`PRAGMA foreign_key_check`, switch to the compatible release, and then reload
the jobs.

Check the active release with `readlink "$HOME/TradingAgentRuntime"`; inspect schema versions with a read-only SQLite connection; inspect process paths with `launchctl print` and `ps`. Runtime logs and locks are under the Application Support state root.

Operational kill-switch state is also external:
`$HOME/Library/Application Support/TradingAgent/runtime/KILL_SWITCH`. Enable it
with `"$HOME/TradingAgentRuntime/scripts/stop_agent.sh"` and clear it only with
the exact paper-resume confirmation accepted by `start_agent.sh`. The active
release tree must remain byte-for-byte unchanged. Deployment fails closed if a
legacy release-local switch is active but has not been preserved externally.

After both jobs are started, wait for one listener poll and one completed scanner
cycle, then run the immutable-runtime evidence gate:

```sh
"$HOME/TradingAgentRuntime/scripts/check_runtime_freshness.sh" --json
```

Retain the JSON with the deployment record. A zero exit requires both process
identities, both commit-bound database heartbeats, the listener PID/lock, the
owner-controlled runtime symlink, the immutable release authority, and a
read-only database quick check to agree. Source-tree authority proves tracked
release inputs; generated-artifact inventories separately prove the built
environment and test evidence. Neither substitutes for current runtime
identity and heartbeat evidence.
