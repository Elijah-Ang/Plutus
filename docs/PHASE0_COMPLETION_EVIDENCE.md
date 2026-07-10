# Phase 0 completion evidence

Date: 2026-07-10

Branch: `codex/phase0-execution-integrity`

Original audited baseline: `cddfe4c6a656a78dcdba5015a752bfab59c8dc7e`

This evidence is offline and paper-only. It does not authorize Phase 1, live trading, automatic ordinary entries, or changes to current risk limits.

## Crash-boundary traceability

All rows are executable tests in `tests/test_phase0_crash_boundaries.py`. Each test starts with a temporary database, uses a fake broker, recreates the storage/service object after the injected dead-process boundary, and asserts broker calls, intent/reservation state, and recovery behavior.

| Case | Test | Injected boundary | Expected and actual result |
|---:|---|---|---|
| 1 | `test_crash_01_failure_before_intent_persistence` | `before_intent_persistence` hook | PASS: retryable workflow; 0 intents; 0 reservations; 0 broker calls |
| 2 | `test_crash_02_after_intent_commit_before_broker_call` | after atomic intent/reservation commit | PASS: 1 intent/reservation; stable ID; restart submits once |
| 3 | `test_crash_03_immediately_before_broker_invocation` | final hook immediately adjacent to `broker.submit_order` | PASS: persisted `SUBMITTING`; 0 calls before crash; recovery is explicitly lookup-only/proven-no-submit |
| 4 | `test_crash_04_broker_accepts_then_ambiguous_timeout` | fake broker records order then raises timeout | PASS: `UNKNOWN`; stable ID; one call; reservation retained; replay does not submit |
| 5 | `test_crash_05_broker_success_before_local_success_update` | after fake response, before local transition | PASS: restart lookup finds one broker order; no second submission; local `SUBMITTED` |
| 6 | `test_crash_06_approval_accepted_before_intent_creation` | committed `APPROVED_PENDING_INTENT` only | PASS: restart creates exactly one intent/reservation and moves to `SUBMISSION_PENDING` |
| 7 | `test_crash_07_intent_created_before_workflow_completion` | intent committed, workflow link marker removed | PASS: restart links existing intent; no duplicate |
| 8 | `test_crash_08_partial_fill_precedes_submitted_status` | partial fill before stale submitted snapshot | PASS: quantity remains monotonic and state remains `PARTIALLY_FILLED` |
| 9 | `test_crash_09_duplicate_partial_fill_event` | same broker execution replayed | PASS: one fill event, one lot, one quantity delta |
| 10 | `test_crash_10_final_fill_after_partial_notification` | final fill after partial notification marked sent | PASS: final state; reservation released; final notification pending independently |
| 11 | `test_crash_11_cancellation_during_partial_fill` | cancel after cumulative partial fill | PASS: filled quantity preserved; only remaining reservation released |
| 12 | `test_crash_12_database_lock_during_approval_processing` | deterministic `BEGIN IMMEDIATE` owner | PASS: first attempt rolls back; retry produces one visible workflow |
| 13 | `test_crash_13_database_lock_during_state_transition` | deterministic state-ledger writer lock | PASS: state/event unchanged; retry makes one transition |
| 14 | `test_crash_14_restart_with_unknown_intent_is_lookup_only` | restart after ambiguous submission | PASS: one lookup; zero resubmissions; reservation retained |
| 15 | `test_crash_15_restart_with_accepted_approval_without_decision` | accepted workflow without terminal decision | PASS: recovered once; repeated sweep idempotent |
| 16 | `test_crash_16_restart_with_stale_listener_lock` | dead recorded PID beyond grace | PASS: classified stale/reclaimable; no live owner displacement |
| 17 | `test_crash_17_sequential_yes_all_exhausts_capacity` | first candidate reserves before second | PASS: second blocked; total reservation remains within ceiling |
| 18 | `test_crash_18_emergency_ambiguous_timeout_matches_ordinary` | emergency fake timeout | PASS: same `UNKNOWN`, stable-ID, no-resubmit semantics |
| 19 | `test_crash_19_close_and_reopen_creates_new_lifecycle` | zero position between two holdings | PASS: historical lifecycle closed; new lifecycle identity |
| 20 | `test_crash_20_never_submitted_local_order_is_not_reconciled` | local blocked order in reconciliation tables | PASS: 0 lookups; 0 submissions |

## Controlled concurrency matrix

`tests/test_phase0_concurrency.py` uses barriers/events, explicit SQLite lock ownership, WAL snapshots, and bounded joins rather than sleep-based races.

| Case | Test | Result |
|---:|---|---|
| 1 | `test_concurrency_01_two_handlers_process_same_telegram_update` | One workflow identity; both handlers observe it |
| 2 | `test_concurrency_02_two_handlers_accept_same_proposal` | One accepted workflow; competing approval conflicts |
| 3 | `test_concurrency_03_two_workers_create_same_intent` | One intent, reservation, client ID |
| 4 | `test_concurrency_04_two_recovery_workers_claim_same_workflow` | One lease owner |
| 5 | `test_concurrency_05_two_workers_transition_same_state` | One transition/event winner |
| 6 | `test_concurrency_06_reconciliation_and_stream_deduplicate_fill` | One broker fill event and quantity delta |
| 7 | `test_concurrency_07_scanner_listener_atomic_capacity_reservation` | One reservation winner; exposure ceiling preserved |
| 8 | `test_concurrency_08_overlapping_yes_all_logical_actions_win_once` | One intent per logical candidate |
| 9 | `test_concurrency_09_write_lock_timeout_is_explicit_and_retryable` | Explicit lock error, no partial state, clean retry |
| 10 | `test_concurrency_10_migration_writer_does_not_block_wal_reader_snapshot` | Reader snapshot remains coherent |
| 11 | `test_concurrency_11_health_reads_during_snapshot_write` | Reader sees old or committed state, never partial state |
| 12 | `test_concurrency_12_reservation_release_races_late_fill_without_negative_value` | No negative/lost reservation or quantity regression |

## Migration and restoration proof

The production source was opened as `file:...?...mode=ro` and cloned through `sqlite3.Connection.backup`. No application row values were printed. On the migrated clone:

- source and clone size: 546,369,536 bytes;
- backup: 1.865 seconds;
- migration: 0.0082 seconds;
- repeated startup: 0.0020 seconds and schema-identical;
- disk growth: 0 bytes for the already-additive source shape;
- schema: 89 tables, 27 indexes, 1 trigger, WAL;
- migration versions: `phase0_execution_integrity_v1` plus `phase0_execution_integrity_v2_completion` on the migrated clone;
- source and post-migration `PRAGMA integrity_check`: `ok`;
- all fourteen Phase 0 read-only integrity counters: zero after the legacy-approval manual-review backfill;
- restoration through SQLite backup: schema hash and all table counts exactly match the pre-migration snapshot;
- production source connection mode: read-only, with source size/mtime unchanged during the proof.

## Release-gate verdict

The prior production-database boundary incident is contained: the mutable checkout no longer contains the production database, normal runtime startup cannot migrate it, and the installed jobs point only at an immutable release plus the external state root. The release migration was explicit, backed up, manifest-bound, and integrity-checked.

All Phase 0 code and release blockers identified in the re-audit are closed: crash-3 now has its literal final broker-boundary hook; every crash case executes shared durable-invariant assertions; lower late cumulative fill reports preserve the event while marking prospective P&L `partially_reconstructed`; and the compatibility matrix exercises original/new schema combinations and restoration. `process_telegram()` invokes recovery on every listener poll; the listener remains intentionally stopped during this validation and cannot consume pending updates.

The remaining operational acceptance item is one controlled regular-market scanner cycle on this exact release. It is not a reason to start Phase 1, enable live trading, or consume Telegram updates.

Executable synthetic proofs are in `tests/test_phase0_migration_proof.py`: repeat migration, transaction interruption rollback, old-query/new-code compatibility, clear pre-migration failure, WAL reader/writer behavior, and restoration.

Code rollback and database rollback are separate. Code may be reverted normally because the schema is additive. Exact database rollback requires a pre-migration SQLite backup and a maintenance window: stop application writers, restore with SQLite backup or atomically replace the database plus WAL/SHM handling, run `PRAGMA integrity_check`, and restart only after verification. Reverting code while leaving additive tables is forward-compatible but is not an exact database rollback.

## Accounting and safety policy

Prospective executions use FIFO lots. Trading-day and Monday-based trading-week boundaries use `America/New_York`. Realized P&L excludes unrealized P&L and includes recorded fees/adjustments. Historical basis is never invented; confidence is `verified`, `reconstructed`, `partially_reconstructed`, or `unavailable`. New risk-increasing entries fail closed for unavailable/stale/nonverified realized-loss information unless the existing reliable and stricter absolute account-loss controls are present. Risk-reducing exits are not blocked solely by missing realized P&L. Existing absolute daily and weekly limits are unchanged; percentage values remain display-only.

## Security and lock proof

Tests establish synthetic credentials before application imports and install process-wide socket, `requests`, and `urllib` blockers. Environment-first secret retrieval is centralized with an injectable Keychain fallback that is disabled in tests. Redaction covers Telegram URLs, headers, query credentials, database URL userinfo, nested exceptions, request exceptions, structured dictionaries, JSON, object repr, retries, validation/broker errors, multiple registered secrets, and false-positive control text.

Listener tests cover live owner, wrong command, dead PID, PID reuse/start-token mismatch, malformed/absent metadata, repository and commit differences, rapid restart, two concurrent starts, interrupted stale cleanup, and a real temporary process. A live valid owner is never displaced solely because repository metadata or commit differs.
