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

Apply a production migration only during deployment:

```sh
TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION=YES_I_AM_DEPLOYING \
  ./scripts/migrate_runtime_db.py \
  --database "$HOME/Library/Application Support/TradingAgent/database/trading_agent.sqlite3" \
  --release-manifest /Users/elijahang/TradingAgentRuntime/release-manifest.json \
  --allow-production-migration
```

The command writes a verified pre-migration backup under `Application Support/TradingAgent/backups/`. If compatibility requires restoration: stop both jobs, create a new backup of the current database, restore the selected verified backup with SQLite backup tooling, run `PRAGMA integrity_check`, switch to the compatible release, and then reload the jobs.

Check the active release with `readlink "$HOME/TradingAgentRuntime"`; inspect schema versions with a read-only SQLite connection; inspect process paths with `launchctl print` and `ps`. Runtime logs and locks are under the Application Support state root.
