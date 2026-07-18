# Trading System Transformation Roadmap

Last updated: 2026-07-11
Authoritative implementation branch: `main` (paper-only safety/accounting implementation)
Safety posture: paper-only; live and ordinary automatic execution remain compile-time/runtime blocked.

Implementation commits:

- `513fcf2` — initial long-term roadmap and invariant ledger.
- `8d7d1f5` — Phase 0 execution-integrity foundation, offline tests, and additive schema.
- `4b6a444` — Phase 0 completion-gate implementation and audit repairs.
- `971c3d3` — immutable release/runtime-state cutover and explicit migration gate.
- `581ddff` — verified Phase 0 development tip used as the Phase 1 branch base.
- Phase 1 implementation/evidence commits are recorded in branch history; the gate remains partial until sufficient OOS data exists.

Ledger commit resolution: every Phase 0 cell below that says `pending final commit` resolves to implementation commit `4b6a444`; the final evidence/roadmap commit is recorded separately in the repository history.

Latest historical offline verification on 2026-07-10:

- `.venv/bin/pytest -o addopts='' -q` → `374 passed`, `16 warnings`, in `44.50s`; no failures/skips.
- Python compilation, `zsh -n`, `git diff --check`, temporary migration rerun, and the ten-check integrity report passed.
- The hard network guard covered socket, requests, and urllib paths; legacy credential tests use deterministic fake API failures.
- No production database, launchd service, broker, Telegram endpoint, OpenAI endpoint, or market-data provider was accessed or mutated.

The current safety/accounting implementation supersedes the older counts above.
Its current offline counts and commit are reported with the release handoff;
runtime deployment verification remains a separate check.
- Completion verification after audit repairs: `649 passed`, `16 warnings`, in `43.55s`; dedicated Phase 0 suite `288 passed`; crash matrix `20 passed`; concurrency matrix `12 passed`.
- SQLite backup rehearsal on a 546,091,008-byte production-source clone: read-only source, `integrity_check=ok`, repeat migration identical, fourteen integrity counters zero after clone-only legacy backfill, and restoration schema/counts exact. See `docs/PHASE0_COMPLETION_EVIDENCE.md`.
- Safety incident note: an already-scheduled scanner loaded the active working tree at 07:00 UTC and applied the new additive lot/workflow schema to the production database before the read-only clone rehearsal. No order path was invoked by this work, but the strict "production database unmodified" task boundary was therefore not met; see the completion verdict and rollout condition below.

## Phase 0 continuation audit discrepancies (2026-07-10)

The continuation audit at `cbd2ded58b9615899f1271f0d339a377fcf8c317` confirmed that the prior report's `partial` verdict was correct. Before repair, the durable workflow only surfaced consumed approvals without intents for manual review; it did not automatically and exclusively claim/resume every locally deterministic state. The executable evidence also did not yet include the required 20 crash boundaries, twelve controlled concurrency interleavings, complete batch-capacity exhaustion, prospective FIFO realized lots, production-clone migration/restoration proof, unified secret retrieval/redaction, listener PID-start identity tests, or the complete transition matrix. These are release blockers, not deferred Phase 1 work, and Phase 1 remains pending until their completion gates are met.

## Ledger rules

Statuses are `pending`, `partial`, `implemented`, `tested`, or `blocked`. `Implemented` means code exists; `tested` means its listed offline acceptance evidence passes. No item is complete from code inspection alone when runtime evidence is required. Commit fields remain `8d7d1f5` until the actual commit exists.

Every ledger entry uses these fields: stable ID, description, rationale, dependencies, phase, acceptance criteria, status, implementing commit, tests/evidence, rollout condition, rollback approach, and unresolved questions. To keep the ledger reviewable, repeated rollout/rollback policy is referenced as follows:

- Phase 0 rollout condition `P0-R`: full offline suite passes; migration and integrity tests pass on temporary databases; no external requests, production DB writes, launchd changes, risk increases, or live capability; later paper integration requires separate authorization.
- Phase 0 rollback `P0-B`: revert the entry's isolated commit; new tables are additive and may remain dormant; never delete production rows during rollback.
- Later-phase rollout `L-R`: the phase gate and all dependency gates pass in shadow/paper evidence before activation.
- Later-phase rollback `L-B`: disable the new feature flag/sleeve and retain its point-in-time evidence for audit.

## Phase 0 invariants

| ID | Invariant | Code/test anchor |
|---|---|---|
| P0-A-001 | One logical trade action has one stable intent. | `logical_action_key`; unique constraint |
| P0-A-002 | One intent has one stable client order ID. | `stable_client_order_id`; unique constraint |
| P0-A-003 | Lost response never causes client ID replacement. | unknown/restart tests |
| P0-A-004 | Broker submission occurs only after committed intent and reservation. | `DurableExecutionStore`; inspection test |
| P0-A-005 | Unknown remains unresolved until reconciliation proof. | state machine and bounded absence policy |
| P0-A-006 | Unknown blocks conflicting duplicates. | active-intent conflict query |
| P0-A-007 | Capacity includes filled and reserved exposure. | canonical risk snapshot and final context |
| P0-A-008 | Approval consumption cannot create an invisible unrecoverable gap. | workflow recovery/manual-review state |
| P0-A-009 | State transitions and broker events are idempotent. | transition counter/event keys/fill keys |
| P0-A-010 | Emergency approval policy may differ; execution integrity does not. | unified `Executor` route |
| P0-A-011 | Shadow, observation, and research records cannot create intents. | intent admission guard |
| P0-A-012 | Unknown risk telemetry is never silently zero. | `CanonicalRiskSnapshot.unavailable` |
| P0-A-013 | Health distinguishes healthy, degraded, blocked, stale, failed, unknown. | `HealthMonitor` |

## Phase 0 implementation ledger

| ID | Description | Rationale | Dependencies | Acceptance criteria | Status | Commit | Tests/evidence | Rollout | Rollback | Unresolved questions |
|---|---|---|---|---|---|---|---|---|---|---|
| P0-B-001 | Durable order intent/outbox schema with full source, sizing, price, risk, broker, time, error, and version fields | Persist identity before uncertainty | P0-A | Additive idempotent temp-DB migration; unique logical/client IDs | tested | 8d7d1f5 | `test_broker_submission_has_committed_intent_and_reservation_first` | P0-R | P0-B | Production-size migration timing needs later read-only rehearsal |
| P0-B-002 | Deterministic Alpaca-safe client ID | Restart identity | P0-B-001 | Stable, non-secret, unique, reused after timeout | tested | 8d7d1f5 | ambiguous/restart tests | P0-R | P0-B | None |
| P0-B-003 | Persist intent and reservation, commit, then call broker | Eliminate pre-submit crash gap | P0-B-001 | Inspection at broker boundary sees committed `SUBMITTING` intent/reservation | tested | 8d7d1f5 | boundary inspection test | P0-R | P0-B | None |
| P0-B-004 | Unknown-result semantics | Prevent duplicate order | P0-C, P0-F | UNKNOWN retains full remaining reservation and never auto-resubmits | tested | 8d7d1f5 | ambiguous submission test | P0-R | P0-B | Paper integration drill still required in Phase 5 |
| P0-C-001 | Central validated order state machine | Deterministic lifecycle | P0-B | All named semantic states; invalid transitions fail closed | tested | 8d7d1f5 | invalid transition test | P0-R | P0-B | Broker-specific rare statuses may need mapping additions |
| P0-C-002 | Append-only order event ledger and reconstruction counter | Auditability | P0-C-001 | Each transition/event keyed and ordered; repeat is idempotent | tested | 8d7d1f5 | fill/transition tests | P0-R | P0-B | Add standalone reconstruction CLI after core closure |
| P0-C-003 | Monotonic quantities, UTC timestamps, redacted errors | Correctness/privacy | P0-C-001 | Constraints and transactional checks pass | tested | 8d7d1f5 | phase0 integrity tests | P0-R | P0-B | None |
| P0-D-001 | Ordinary entry/exit/add routes use unified executor | One uncertainty policy | P0-B/C/F | Repository AST guard permits submit only at executor/adapter boundary | tested | 8d7d1f5 | submit-boundary guard | P0-R | P0-B | None |
| P0-D-002 | Sleep/extreme/emergency exits use unified executor | Emergency must not bypass integrity | P0-D-001 | No direct service submit; stable intent/reservation/UNKNOWN | implemented | 8d7d1f5 | static guard; existing emergency tests pending full rerun | P0-R | P0-B | Exact emergency final-risk fixture regressions pending full suite |
| P0-E-001 | Durable Telegram inbox with received vs processed state | Cursor must not hide work | P0-B | Inbox commit precedes cursor; update ID idempotent; no raw identity stored | implemented | 8d7d1f5 | existing routing suite pending full rerun | P0-R | P0-B | Inbox stores a normalized safe envelope, not raw message text |
| P0-E-002 | Approval workflow links accepted approval to intent | Recover accepted work | P0-B/E-001 | Workflow has update uniqueness, intent link, version, terminal/manual review | tested | pending final commit | `test_phase0_approval_recovery.py`; crash 1/6/7/15 | P0-R plus installed-runtime isolation | P0-B | Startup leaves externally dependent `VALIDATING` work visible/degraded until normal revalidation |
| P0-E-003 | Startup/periodic recovery sweep | Restart safety | P0-E-002 | Detects received updates, approvals without intents, awaiting/unknown/stale intents, terminal reservations | tested | pending final commit | CAS lease, deterministic validator/submitter, lookup-only recovery tests | P0-R plus installed-runtime isolation | P0-B | Broker callbacks are bounded APIs; startup remains local-only by safety policy |
| P0-E-004 | Authorization, targeting, expiry, plain-yes, paper yes-all unchanged/stricter | Preserve approval safety | P0-E-001 | Existing routing tests pass | tested | pending final commit | full Telegram/batch regression in 640-test suite | P0-R | P0-B | None |
| P0-F-001 | Durable one-per-intent reservation ledger | Count approved/submitted uncertainty | P0-B | Atomic create, nonnegative uniqueness, auditable release | tested | 8d7d1f5 | reservation tests | P0-R | P0-B | None |
| P0-F-002 | Conservative long-entry notional and stop-risk rule | Avoid understatement | P0-F-001 | Highest approved/observed local price; remaining qty formula | tested | 8d7d1f5 | partial fill example/test | P0-R | P0-B | Sell/replacement overlap policy remains conservative/local |
| P0-F-003 | Filled plus active reservation projections for total/symbol/cluster/BP/open risk | Prevent sequential over-allocation | P0-F-001, P0-I | Final context includes earlier reservations; missing held stop risk fails closed | tested | pending final commit | `test_phase0_reservation_batch.py`; concurrency 7/8 | P0-R | P0-B | None |
| P0-F-004 | Idempotent release on fill/reject/cancel/expiry/confirmed absence; UNKNOWN retained | Correct capacity lifecycle | P0-C/H | State and reservation mutate atomically | tested | 8d7d1f5 | partial/final/unknown tests | P0-R | P0-B | Replacement overlap tests pending |
| P0-G-001 | Lossless broker fill-event ledger plus aggregate | Multiple fills and dedupe | P0-C/F | Unique event keys, monotonic cumulative/delta, average price | tested | 8d7d1f5 | partial/duplicate/final test | P0-R | P0-B | Stream execution IDs deferred with P0-O |
| P0-G-002 | Partial-fill reservation transfer and terminal release | Accurate exposure | P0-G-001 | Remaining ratio correct; never negative | tested | 8d7d1f5 | proportional release test | P0-R | P0-B | None |
| P0-G-003 | Distinct partial and final notification states | Do not suppress final fill | P0-G-001 | Final fill resets notification state; retry is idempotent | tested | 8d7d1f5 | safety repair notification tests | P0-R | P0-B | None |
| P0-G-004 | Replacement/chasing interface disabled by default | Avoid aggressive behavior | P0-B/P | `replacement_enabled=0`; no automatic cancel/chase | implemented | 8d7d1f5 | schema/static review | P0-R | P0-B | Bounded policy belongs to Phase 3 |
| P0-H-001 | Reconcile only broker-relevant states | Remove false lookup noise | P0-C | Never-submitted blocked/terminal legacy rows excluded | tested | 8d7d1f5 | blocked-row test | P0-R | P0-B | None |
| P0-H-002 | Broker-ID first, stable client-ID fallback | Deterministic lookup | P0-B | Correct lookup key, no submission | tested | 8d7d1f5 | legacy/new reconcile tests | P0-R | P0-B | None |
| P0-H-003 | Bounded not-found policy and classified attempt telemetry | One 404 is not proof | P0-H-002 | Three observations before confirmed absence/release | tested | pending final commit | safety-repair reconciliation tests; crash 14/20 | P0-R | P0-B | Threshold remains conservative constant |
| P0-H-004 | Found/not-yet-found/absent/failure/divergence/manual-review telemetry | Operational signal quality | P0-H-001 | Reconciliation result and attempt rows separate outcomes | tested | pending final commit | reconciliation and crash suites | P0-R | P0-B | None |
| P0-I-001 | Canonical snapshot for filled gross/net, reserved/projected exposure, stop risk, unknown exposure, BP/cash/source time | Replace placeholders | P0-F/J | Auditable inputs; unavailable values explicit | implemented | 8d7d1f5 | canonical snapshot persistence; full consistency tests pending | P0-R | P0-B | Position stop risk is unavailable if lifecycle lacks initial stop |
| P0-I-002 | Daily/weekly realized P&L and loss percentage | Truthful loss telemetry | P0-G/J | Complete lot ledger and correct equity denominator | tested (prospective) | pending final commit | `test_phase0_lot_ledger.py`; FIFO/day/week/manual/unavailable tests | P0-R plus prospective boundary | P0-B | Pre-boundary history remains explicitly unavailable and is never fabricated |
| P0-I-003 | Preserve existing absolute equity-loss controls | No risk expansion | P0-I-002 | Existing authoritative missing-loss checks continue to fail closed | tested | 8d7d1f5 | `test_unknown_weekly_loss_blocks_risk` | P0-R | P0-B | Rename legacy controls after compatibility migration |
| P0-I-004 | Canonical snapshot consistency across batch/final/display | One source of truth | P0-I-001 | Same snapshot ID/inputs produce checks and labels | tested | pending final commit | snapshot unknown/nonzero and service-context tests | P0-R | P0-B | Legacy display paths remain compatibility-only |
| P0-J-001 | Persistent position lifecycle identity | Prevent stale symbol state | P0-C | zero→nonzero new ID; partial retains; flip closes/reopens | tested | 8d7d1f5 | reopen regression test | P0-R | P0-B | Broker position ID availability varies; symbol+continuity fallback used |
| P0-J-002 | Archive/clear management state on close | Historical analysis plus clean reopen | P0-J-001 | Peak/trailing/profit flags archived and current row deleted | tested | 8d7d1f5 | reopen archive test | P0-R | P0-B | Historical archive is JSON pending normalized later analytics schema |
| P0-J-003 | Propagate lifecycle through decisions/proposals/intents | Full ownership | P0-J-001 | PM state/decisions and intents carry ID | partial | 8d7d1f5 | PM SQL and intent schema | P0-R | P0-B | All proposal construction branches need exhaustive regression proof |
| P0-K-001 | Measured scanner/listener/reconcile/DB/recovery/order/reservation health | Truthful operations | P0-E/H/I | Six health states; freshness thresholds; commit | tested | 8d7d1f5 | stale health test | P0-R | P0-B | Protected-position count awaits Phase 3 broker protection |
| P0-K-002 | `/status` derives from measured health | Remove unconditional Active | P0-K-001 | Never reports Active from process existence alone | tested | 8d7d1f5 | status test | P0-R | P0-B | Formatting regressions pending full Telegram suite |
| P0-K-003 | Scanner/listener lock owner metadata and stale recovery | KeepAlive resilience | P0-K-001 | PID, epoch, repo, commit; live owner preserved; stale reclaimed/logged | tested | pending final commit | `test_phase0_secrets_and_listener_lock.py` owner/start/PID/race matrix | P0-R | P0-B | None |
| P0-K-004 | Overlap skip is telemetry, not fresh processing | Honest freshness | P0-K-003 | Dedicated runtime log event; scanner success heartbeat unchanged | implemented | 8d7d1f5 | shell review | P0-R | P0-B | DB row impossible before lock ownership; repository log used |
| P0-L-001 | Startup schema/semantic validation for paper/live/auto/crypto, type, bounds, ordering, timeouts, expiry, symbols | Fail safely before scanner | P0-A | Contradictions raise before DB/service creation | tested | 8d7d1f5 | invalid config test; current config loads | P0-R | P0-B | Full unknown nested-key inventory remains pending |
| P0-L-002 | Unknown/deprecated/inactive configuration policy | No silent ignored controls | P0-L-001 | Deprecated keys warn; safety contradictions error | partial | 8d7d1f5 | current deprecated warnings | P0-R | P0-B | Several inactive keys need migration/removal in cleanup ledger |
| P0-L-003 | Unified env/Keychain secret precedence | Coherent retrieval without exposure | P0-L-001 | Token/chat/allowed/provider loaders share one policy | tested | pending final commit | 12 redaction cases; synthetic pre-import isolation; raw-access search | P0-R | P0-B | Keychain compatibility is injected/tested without real access |
| P0-L-004 | Volatility threshold is validated configuration | Remove hardcoded safety value | P0-L-001 | Strategy honors `maximum_volatility_20d=.45` | implemented | 8d7d1f5 | volatility suite pending | P0-R | P0-B | Other duplicated display bands still need canonicalization |
| P0-M-001 | “5m” renamed to previous stored observation | Accurate meaning | P0-A | New semantic columns/version; old rows untouched | implemented | 8d7d1f5 | schema trigger/migration test pending | P0-R | P0-B | Internal variable remains compatibility-only |
| P0-M-002 | UTC-midnight movement labeled truthfully | Avoid fake market session | P0-M-001 | Operator text says first UTC-day observation | implemented | 8d7d1f5 | Telegram expectations need update/full suite | P0-R | P0-B | Actual NY-open metric belongs to Phase 1/2 data pipeline |
| P0-M-003 | Scan cadence distinct from bar timeframe | Accurate config/docs | P0-L | Deprecated scan key warns; daily bar calls remain explicit | partial | 8d7d1f5 | config warnings | P0-R | P0-B | New authoritative cadence key not yet consumed by launchd generator |
| P0-N-001 | Default root collection without PYTHONPATH workaround | Reproducible tests | P0-A | `pytest --collect-only` succeeds | tested | 8d7d1f5 | 374 tests collected and passed on 2026-07-10 | P0-R | P0-B | None |
| P0-N-002 | Hard offline network guard and fake invalid-credential tests | No accidental services | P0-N-001 | socket/requests/urllib fail immediately | tested | 8d7d1f5 | network guard test; targeted suite | P0-R | P0-B | Additional async HTTP clients should be added if introduced |
| P0-N-003 | All 20 required crash boundaries | Prove recovery | P0-B–K | Each named boundary deterministic and passing | partial | pending final commit | 20 named passing tests and traceability table; independent audit found exact case-3 boundary and per-row assertion gaps | blocked pending exact-boundary proof | P0-B | Every row must repeat all mandated assertions; shared invariant substitution is not sufficient |
| P0-N-004 | Required invariants and deterministic concurrency | Race safety | P0-B–H | duplicate worker produces one intent/submit; SQLite contention covered | tested | pending final commit | 12 controlled interleavings in `test_phase0_concurrency.py` | P0-R | P0-B | None |
| P0-N-005 | Fresh/current/repeat/interrupted migration tests preserving all named data | Safe 543 MB evolution | P0-B schema | Temp fixtures and fingerprints; no production DB | tested with safety incident | pending final commit | backup/repeat/interruption/WAL/restore tests plus 546 MB clone | blocked pending operator review of unintended scheduled-runtime DDL | pre-migration SQLite restore | Existing scanner loaded working-tree schema during task; production boundary not preserved |
| P0-N-006 | Read-only integrity report | Detect corrupt relationships | P0-B/F/G/J | All ten required checks reported; repair separate/not invoked | tested | 8d7d1f5 | integrity report test | P0-R | P0-B | Add CLI wrapper before operator rollout |
| P0-O-001 | Disabled broker-event interface/stream foundation | Future low-latency updates | P0-G/H/K | Mock-only adapter, REST fallback, heartbeat, reconnect dedupe | pending (deferred) | — | Local SDK capability confirmed only | L-R | L-B | Deferred to first bounded post-Phase-0 item to avoid unreliable partial stream |
| P0-P-001 | Parent/protective/target/OCO/replacement representation boundary | Future broker protection | P0-B | Parent/group/type/role/protection-confirmed fields; feature inactive | implemented | 8d7d1f5 | schema/static review | P0-R | P0-B | Whole/fractional capability tests are Phase 3 |
| P0-P-002 | Never claim broker protection without confirmation | Operator truth | P0-P-001 | Default `protection_confirmed=0`; health omits protected claim | tested | 8d7d1f5 | schema defaults/health behavior | P0-R | P0-B | None |
| P0-LOG-001 | Redacted transition/recovery logging and noise reduction | Privacy/operability | P0-C/E/H | Internal IDs only; no raw Telegram/account identifiers; local-only rows excluded | tested | pending final commit | redaction matrix and changed-file secret scan | P0-R | P0-B | Historical legacy audits are not rewritten |
| P0-LOG-002 | Repository-managed direct log rotation | Prevent unbounded files | P0-K | Rotation without installed plist modification | pending | — | Existing `scripts/rotate_logs.sh` reachability needs verification | P0-R | P0-B | Keep roadmap item if not safely wired |

## Phase 1 — evidence and validation pipeline

Common metadata for all `P1-*` entries: rationale = establish point-in-time out-of-sample evidence before risk changes; dependencies = completed Phase 0 plus named prerequisites; acceptance = reproducible cost-aware report with source timestamps and missing-data disclosure; status = pending; commit/evidence = none; rollout = `L-R` and Phase 1 gate; rollback = `L-B`; unresolved question = exact minimum sample is set before analysis, not after results.

| IDs | Pending requirements |
|---|---|
| P1-OUT-001..005 | Complete 1/5/20-day outcomes; benchmark-relative outcomes; MFE/MAE; intended stop/target outcomes; realized/hypothetical R multiples |
| P1-EXEC-001..004 | Slippage; spread; implementation shortfall; delayed-entry cost |
| P1-HOLD-001 | Holding-period analysis |
| P1-CAL-001..004 | Score bands/deciles; calibration reliability plots; Brier score; remove AI availability as hard gate unless incremental value proven |
| P1-DATA-001..006 | Regime tags; strategy IDs; point-in-time feature snapshots; point-in-time universe; delisting/corporate actions; scanner cadence redesign for slow features |
| P1-SIM-001..003 | Realistic costs/partial fills; gap-through-stop simulation; backtest-shadow-paper-live path parity |
| P1-VAL-001..008 | Walk-forward; purged/embargoed overlap validation; sensitivity; ablation; probabilistic Sharpe; deflated Sharpe; PBO; data-snooping correction |
| P1-PERF-001..006 | Profitable-month percentage; worst month; drawdown/recovery; exposure-adjusted return; performance by strategy/regime/score/execution; completed forward sample report |
| P1-RL-001 | Explicitly exclude deep reinforcement learning until the evidence environment is valid |

### Phase 1 implementation checkpoint (2026-07-11)

| IDs | Status | Evidence and remaining gate |
|---|---|---|
| P1-OUT-001..005 | tested | Canonical session-based 1/5/20 outcomes, SPY-relative returns, MFE/MAE, barrier ordering, actual/hypothetical R and explicit unknowns; point-in-time bar coverage is still unavailable for completed historical evidence. |
| P1-EXEC-001..004 | implemented/tested | Versioned spread, slippage, commission/regulatory, delayed-entry parameters and provenance; current 8 bps report value is an assumption, not observed fill calibration. |
| P1-HOLD-001 | implemented | Horizon results and barrier exit sessions support holding-period analysis; no completed OOS sample exists. |
| P1-CAL-001..004 | implemented/inconclusive | Score bands, Brier reliability, blocker and AI-gate grouping exist; no completed OOS labels support calibration or incremental AI value. Existing trading behavior is unchanged. |
| P1-DATA-001..006 | partial | Deterministic regime/version fields, point-in-time feature prefixes, universe version contracts, session cadence design, and explicit revised/delisting limitations exist. A historical point-in-time universe/corporate-action bundle is not yet available. |
| P1-SIM-001..003 | tested | Historical simulation reuses `evaluate_symbol`; costs and conservative same-bar stop/target ordering are tested. Partial-fill simulation remains unavailable from daily bars. |
| P1-VAL-001..008 | partial | Walk-forward, purge/embargo, sensitivity, ablation grouping, bootstrap uncertainty, score calibration, and probabilistic Sharpe exist. Deflated Sharpe/PBO/data-snooping conclusions remain unavailable without multiple independent configurations and completed OOS samples. |
| P1-PERF-001..006 | partial | Reproducible grouped reporting with sample sizes and explicit coverage exists; drawdown/month/recovery statistics remain unavailable with zero completed OOS rows. |
| P1-RL-001 | tested | No reinforcement-learning code or Phase 2 strategy was added. |

Pending-outcome root cause: two independent calendar-day/rolling-window calculators silently retried invalid timestamps, absent bars, and provider errors while inventing an 8% stop when the intended stop was unknown. The Phase 1 path uses one exchange-session calculator, explicit `completed`/`maturing`/`unavailable`/`failed` states, immutable fingerprints, and compatibility projections only. The duplicate legacy calculation bodies were removed.

The clone-only evidence cut imported 3,114 duplicate-free opportunities and classified all 9,342 horizon rows: 5,804 were still maturing and 3,538 were unavailable (3,526 missing immutable asset-session bars; 12 missing/invalid entry prices). Completed OOS n=0. Therefore `P1-GATE-001` is **not met** and Phase 1 status is **partial**. See `docs/PHASE1_EVIDENCE_REPORT.md` and `docs/PHASE1_RESEARCH_OPERATIONS.md`.

Phase 1 gate `P1-GATE-001`: no score-based risk increase or broader profile until completed outcomes show positive out-of-sample expectancy after realistic costs.

### Profitability authority checkpoint (2026-07-17)

The operational paper policy now persists and independently recomputes one
complete predeclared family across every known strategy version. It uses exact
label-overlap purging, an explicit embargo, deterministic circular block
bootstrap bounds, Benjamini-Hochberg family FDR, parameter stability groups,
and immutable fold/decision/family fingerprints. Mature failed or unvalidated
strategies are suspended; insufficient authority is limited to
`RESEARCH_ONLY`/`PROBE`. Candidate economics cannot bypass this boundary.

Closed actual-paper FIFO lifecycles now receive immutable expected-versus-
realized attribution. Complete records reconcile market outcome, entry timing,
fill/exit execution, fees, holding/opportunity, and uncertainty exactly;
actual-only evidence is labeled partial and structural gaps remain unavailable.
Counterfactual records never enter operational validation.

`P1-VAL-001..008` remains **partial**, not complete: Deflated Sharpe and PBO are
explicitly unavailable until honest multi-configuration trial history and a
suitable result matrix exist. The evidence gate is not weakened and no risk
profile is broadened by this checkpoint. See
[PROFITABILITY_VALIDATION_AND_ATTRIBUTION.md](PROFITABILITY_VALIDATION_AND_ATTRIBUTION.md).

## Phase 2 — shadow strategy and market-context expansion

Common metadata: rationale = measure independent alpha sleeves without opaque score blending; dependencies = P1 gate and point-in-time data; acceptance = standardized insight, shadow-only isolation, sleeve-level cost-aware OOS report across multiple regimes; status pending; commit/evidence none; rollout `L-R` plus Phase 2 gate; rollback `L-B`; unresolved questions = calibration horizon and sleeve correlation budget.

| IDs | Pending requirements |
|---|---|
| P2-STRAT-001..005 | Cross-sectional momentum; time-series trend/breakout; uptrend pullback; ETF sector/broad-asset rotation; distinct breakout continuation |
| P2-INS-001..010 | Insight symbol; direction; strategy ID; signal time; horizon; calibrated expected return; confidence; uncertainty; stop/invalidation; feature snapshot ID |
| P2-REG-001..013 | SPY MA100/200 and slopes; breadth; vol percentile; drawdown; dispersion; defensive/credit confirmation; panic-rebound; favorable/narrow/high-vol trend; defensive; panic/rebound; uncertain |
| P2-EVT-001..009 | Earnings; ex-dividend/corporate actions; FOMC/CPI/employment/holiday/early-close; pre-earnings entry ban; reduced adds; overnight gap class; quote/spread/liquidity; session-aware features; point-in-time universe |
| P2-SAFE-001 | All new strategies shadow-only initially |

Phase 2 gate `P2-GATE-001`: each sleeve shows positive cost-aware OOS expectancy and acceptable drawdown in more than one regime before paper capital.

## Phase 3 — execution quality, protection, and moderate paper risk

Common metadata: rationale = improve execution/protection only after evidence; dependencies = P1/P2 gates and Phase 0 integrity; acceptance = bounded paper experiment with rollback; status implemented in paper-only mode; rollout `L-R`; rollback `L-B`; unresolved questions = live-readiness and broker protective-capability evidence.

| IDs | Pending requirements |
|---|---|
| P3-EXEC-001..007 | Marketable-limit normal entries; max slippage; bounded expiry; cancel/reassess; no price chase; urgent-exit policy; opening/late-session experiments |
| P3-HOURS-001 | Extended hours disabled by default/separate limits |
| P3-PROT-001..010 | Native stop capability; whole-share brackets; fractional+stop; stop replacement; trailing replacement; partial-fill protection; cancel after partial; confirmed protection health; structural/ATR stop; Mac-independent catastrophe protection |
| P3-MGMT-001..009 | Trailing variants; time stops; partial profits; add only after initial risk reduction; no averaging down; reservation-aware adds; liquidity participation; gap/overnight reserves; stress tests |
| P3-RISK-001..012 | Test inactive profile: 0.20–0.25% trade risk; 0.35% calibrated max; 0.10–0.15% add; 1.00–1.25% total risk; 1.50% favorable; <=0.50% defensive; 25–30% gross; 35–40% favorable; 50% hard paper ceiling; 5–6% symbol; 12–15% cluster; no leverage |
| P3-LOSS-001..002 | 0.75% daily throttle; 1.5–2.0% weekly throttle |
| P3-DD-001..004 | Drawdown multipliers: <2 normal; 2–4 -25%; 4–6 -50%; >6 halt/review |

Phase 3 operational risk is active only in paper mode, remains manual-approval gated,
requires version-matched executable evidence, validated stop evidence, and complete
authoritative exposure/loss state. Its ceilings remain authoritative and cannot be
raised by Phase 4.

## Phase 4 — calibrated adaptive portfolio construction

Common metadata: rationale = separate alpha, risk, execution, uncertainty, and concentration; dependencies = validated sleeves and cost model; acceptance = shadow-first calibrated results with deterministic hard limits; status implemented as paper-only adaptive/exploration accounting; rollout `L-R`; rollback `L-B`; unresolved question = sufficient evidence for future adaptive updates.

| IDs | Pending requirements |
|---|---|
| P4-CAL-001..005 | Separate alpha/risk/execution assessments; EV in R; calibrated positive probability; confidence intervals/uncertainty; isotonic/logistic calibration |
| P4-PORT-001..006 | Ledoit-Wolf covariance; sector fallback; component/marginal risk; concentration penalties; stress correlations; Expected Shortfall |
| P4-SCEN-001..007 | SPY -3%; SPY -5%; sector -7%; vol doubles; 2-ATR gaps; correlations→1; largest position -10–15% |
| P4-ADAPT-001..006 | Conservative vol target; bounded fractional Kelly; Bayesian sleeve expectancy; change points; auto throttling; contextual bandit among validated sleeves |
| P4-SAFE-001..003 | Adaptive shadow-first; hard deterministic limits cannot be overridden; no unconstrained deep-RL agent |

### Cross-asset allocation checkpoint (2026-07-18)

The first cross-asset allocation boundary is implemented as immutable
`research_advisory` evidence. It normalizes net value by stop risk, capital,
holding time, uncertainty, costs, and marginal portfolio contribution, then
applies total, asset, crypto, symbol, cluster, strategy, heat, buying-power,
cash-reserve, position-count, loss, drawdown, and operational-health limits.
Every portfolio total must reconcile before ranking.

This checkpoint creates no proposal, approval, intent, reservation, or broker
authority. Crypto remains research-only and keeps its 1% gross, 0.05% heat,
and 0.01% per-candidate stop-risk ceilings. Operational cross-asset execution
remains pending complete crypto profitability evidence and a later exact
authority/display/approval integration. See
[CROSS_ASSET_ALLOCATION.md](CROSS_ASSET_ALLOCATION.md).

### Accounting and Performance Lab checkpoint (2026-07-18)

Performance Lab proposal populations are now fill-bound: proposals, approvals,
blocks, expiry, rejection, supersession, submission without fill, cancellation,
and ambiguous submission remain separate nonactual evidence. Only a durable
fill becomes `actual_fill`, and actual-trade summaries and integrity checks
reconcile to that evidence. Reports group lifecycle populations by asset class.
See [PERFORMANCE_LAB_CLASSIFICATION.md](PERFORMANCE_LAB_CLASSIFICATION.md).

## Phase 5 — live-readiness validation (does not enable live)

Common metadata: rationale = prove operational readiness without authorizing capital; dependencies = Phases 0–4 gates; acceptance = signed evidence pack with no unresolved critical/high issue; status pending; commit/evidence none; rollout requires separate explicit human authorization and tiny-capital plan; rollback `L-B`; unresolved question = legal/account rules at future review date.

| IDs | Pending requirements |
|---|---|
| P5-PAR-001 | Paper/live behavioral parity review |
| P5-FAULT-001..004 | Full fault injection; production-like restarts; real paper broker capability tests; unknown-submission drills |
| P5-ORDER-001..003 | Partial-fill drills; protective-order verification; reconciliation stability |
| P5-EVID-001..003 | Realistic transaction costs; sufficient forward sample; strategy/regime minimum evidence |
| P5-OPS-001..005 | Kill switch; manual emergency procedures; account/settlement rules; observability/alerting; secrets/permissions review |
| P5-GATE-001 | No unresolved critical/high issue |
| P5-ROLLOUT-001..004 | Tiny-capital staged plan; human live enable procedure; rollback procedure; live remains disabled until separate authorization |

## Cleanup disposition ledger

Common acceptance: reachability, tests, docs, scripts, and runtime references verified before deletion. Status is pending unless noted.

| ID | Item | Disposition | Rationale/dependency | Status | Commit/evidence | Rollout/rollback | Question |
|---|---|---|---|---|---|---|---|
| CLN-001 | `config/risk_limits.yaml` | absent; responsibilities consolidated | no active file; `config/config.yaml` is authoritative | implemented | configuration validation/docs/tests | L-R/L-B | none |
| CLN-002 | `scripts/start_agent.sh` | retain documented legacy wrapper | launchd uses `run_once.sh` | pending | reachability audit | L-R/L-B | remove only after operator docs migrate |
| CLN-003 | unused cash manager | archive after reachability proof | avoid dead policy confusion | pending | tests exist only | L-R/L-B | any operator import? |
| CLN-004 | market snapshot helper | retain/integrate | storage path is active | pending | service references | L-R/L-B | canonical v2 linkage |
| CLN-005 | disabled ML shadow | retain isolated | research evidence only | pending | tests | L-R/L-B | Phase 1 ablation use |
| CLN-006 | misleading movement names | compatibility alias + deprecate | P0-M | partial | v2 semantics columns | P0-R/P0-B | alias removal version |
| CLN-007 | hardcoded volatility threshold | integrate validated setting | P0-L | partial | strategy uses config max | P0-R/P0-B | remaining display bands |
| CLN-008 | score sizing before calibration | retain current risk, remove Phase 1 | no risk expansion | pending | — | L-R/L-B | effective conservative equivalence |
| CLN-009 | false reconciliation of blocked orders | remove behavior | P0-H | tested | blocked-row test | P0-R/P0-B | None |
| CLN-010 | hardcoded zero telemetry | remove/mark unavailable | P0-I | partial | v2 snapshot | P0-R/P0-B | realized lot ledger |
| CLN-011 | no-effect config keys | deprecate/migrate | P0-L | partial | warnings | P0-R/P0-B | inventory completion |
| CLN-012 | unconditional `/status` | remove | P0-K | tested | measured status test | P0-R/P0-B | None |
| CLN-013 | symbol-keyed stale position state | integrate lifecycle archive | P0-J | tested | reopen test | P0-R/P0-B | normalize archive later |
| CLN-014 | duplicate execution branches | remove direct branches | P0-D | tested | AST guard | P0-R/P0-B | None |
| CLN-015 | inactive cancellation capability | retain disabled interface | Phase 3 dependency | pending | no callers | L-R/L-B | paper capability result |
| CLN-016 | unrotated launchd/listener output | integrate repository rotation | P0-LOG-002 | pending | — | P0-R/P0-B | safe wiring without plist edit |

## Current phase verdict

Phase 0 code and release isolation remain **complete** on the verified development base. Phase 1 implementation is **partial**: the evidence machinery and explicit coverage classification are present, but the point-in-time historical data needed for completed OOS expectancy is unavailable. The Phase 1 gate is not met; Phase 2–5 remain pending and all moderate-risk values remain inactive.

Next dependency-ordered task: isolate the active checkout from installed scheduling, prove the exact case-3 pre-adapter crash boundary, execute old application code against the migrated clone, and repeat the production-clone rehearsal from a pre-change backup before any Phase 1 implementation.
