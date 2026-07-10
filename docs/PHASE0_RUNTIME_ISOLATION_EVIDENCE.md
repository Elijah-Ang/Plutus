# Phase 0 runtime isolation evidence

Date: 2026-07-10

## Incident containment

The installed scanner and listener previously executed directly from `/Users/elijahang/Projects/TradingAgent`. The mutable checkout allowed the scheduled scanner to load development schema code and apply additive Phase 0 changes to the paper-production database.

Both launchd jobs were stopped before isolation work. The listener process was verified absent before the database move and plist replacement.

The affected schema was additive: workflow completion fields, prospective FIFO accounting tables/indexes, and related migration metadata. No destructive rollback was selected because integrity was `ok`, the schema is additive, and no valid pre-Phase-0 backup was found in the searched backup locations. A rollback requires restoring a verified pre-migration backup, not deleting additive tables.

## Backup and state move

Production state root: `/Users/elijahang/Library/Application Support/TradingAgent/`.

- Backup timestamps: `20260710T121256Z` and `20260710T121347Z`.
- Source and backup size: 548,716,544 bytes.
- Integrity: `ok` before and after backup/placement.
- Journal mode: WAL.
- Tables: 89.
- Schema SHA-256: `0f37be41453ddd3e88c9cdb6aeb4e5d0c26a67bf7a3550ac872f22706bd51e05`.
- Pre-move schema versions: `phase0_execution_integrity_v1`, `phase0_execution_integrity_v2_completion`.
- New location: `database/trading_agent.sqlite3` under the state root.
- Original repository database was moved to the protected backup directory and is no longer present under the mutable checkout.

Files and directories under the state root use owner-only modes: directories `0700`, databases, backups, plists, and metadata `0600`.

## Selected remediation

Runtime now requires an immutable release beneath `/Users/elijahang/TradingAgentReleases/`, selected through `/Users/elijahang/TradingAgentRuntime`. Mutable state is externalized into database, logs, locks, backups, release, and runtime directories below Application Support.

Ordinary scanner/listener startup verifies a release manifest, paper mode, explicit production database path, runtime sentinel, and required schema version. It does not call schema initialization. Missing schema reports: `Database migration required. Trading remains blocked.`

The deployment-only migration command requires both `--allow-production-migration` and `TRADINGAGENT_ALLOW_PRODUCTION_DB_MIGRATION=YES_I_AM_DEPLOYING`; it creates and verifies a backup before writing schema or the production-paper sentinel.

No database restore was performed as part of this containment.

## Completed isolated release

The selected runtime release is `971c3d373ce9` at
`/Users/elijahang/TradingAgentReleases/971c3d373ce9`; its manifest declares
paper mode and runtime-isolation schema `phase0_execution_integrity_v3_runtime_isolation`.
`/Users/elijahang/TradingAgentRuntime` points to that release. The previous
release `43cda459bd1e` is retained as the code rollback target.

The explicit production migration created
`backups/explicit-pre-migration-20260710T123015Z.sqlite3`, then produced an
integrity-checked database with 90 tables, schema SHA-256
`1029677bef2fe79dcf4c02f61bccacdc855000febeba7923a6578dc1571c2237`, all
three Phase 0 schema versions, and the `production-paper` runtime sentinel.

The scanner launchd job now uses the runtime symlink for both program and
working directory and writes launchd/log/lock state only below Application
Support. The listener plist was also converted but is deliberately unloaded:
it will not consume pending updates during this release validation.
