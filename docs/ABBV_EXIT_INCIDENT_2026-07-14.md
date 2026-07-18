# ABBV approved-exit incident and final hardening report

## Scope and safety boundary

This investigation used canonical repository `Elijah-Ang/Plutus` at baseline commit
`20ac6c1e529bc84d2b9bcaed5746e14282f09cf4` and immutable runtime
`/Users/elijahang/TradingAgentReleases/20ac6c1e529b`. The runtime link and production
paper database were not changed. Broker access was read-only and verified as Alpaca
paper. Database analysis and migration/remediation tests used SQLite copies.

No live-trading, autonomous-execution, leverage, shorting, broker retry, merge,
deployment, runtime-pointer change, or production database mutation was performed.

## Exact incident timeline

All timestamps below include UTC and Singapore time (UTC+08:00).

| Time | Durable evidence | Event |
|---|---|---|
| 2026-07-14 15:15:40.738163Z / 23:15:40.738163 SGT | decision `f43ca8d0-c33c-4f3e-bfe9-19d56b372890`; signal `c3912343-b6f1-472f-9557-c1ab6dd26f8f` | Current position management produced actionable `TIME_STOP_EXIT` for ABBV. Held quantity was `0.433314683`; average entry `253.873595`; decision price `244.555`. |
| 2026-07-14 15:15:40.738163Z / 23:15:40.738163 SGT | review `51238a30-4f84-46df-b597-808d54d65f9a`; proposal `e1986bb1-a498-4111-843b-0090ecdeeb0d` | A paper SELL proposal for `0.2166573415` ABBV shares and estimated notional `52.9846361505325` was created. It expired at 15:20:40Z / 23:20:40 SGT and was sent as Telegram message `537`. |
| 2026-07-14 15:17:23.002137Z / 23:17:23.002137 SGT | Telegram update `188312896`, message `539` | Authorized user replied plain `yes`. It was not a Telegram reply-to; target resolution used `single_pending`. |
| 2026-07-14 15:17:23.029812Z / 23:17:23.029812 SGT | approval `7ac91ac8-7d4b-4854-9301-c2c56c13096a` | Approval was accepted. |
| 2026-07-14 15:17:23.872404Z / 23:17:23.872404 SGT | same approval | Approval was consumed and final revalidation began. |
| 2026-07-14 15:17:24.639 SGT | `agent.log.2026-07-14` | Authoritative quote refresh failed: `quote spread is outside the configured bound`. |
| 2026-07-14 15:17:24.641480Z / 23:17:24.641480 SGT | approval final decision | Final validation blocked with `Price refresh failed or price is unavailable`. |
| 2026-07-14 15:17:24.657851Z / 23:17:24.657851 SGT | workflow `52b9b701-1d6a-467e-a01d-4b56ec87180f` | Workflow became terminal `blocked`; no intent was linked. |
| 2026-07-14 15:17:25.510440Z / 23:17:25.510440 SGT | Telegram update/workflow | Update processing completed. |

Audit events `51590` through `51593` independently record acceptance,
`target_resolved -> validating`, `validating -> blocked`, and update completion.

## Root cause and broker outcome

The immediate root cause was a final authoritative ABBV quote whose spread exceeded
the configured bound. The approval path consumed the manual approval before this
final quote validation completed, then correctly failed closed.

There was no `order_intent`, risk reservation, order, fill, or reconciliation record
for proposal `e1986bb1-a498-4111-843b-0090ecdeeb0d`. Therefore the broker submission
surface was unreachable for this attempt. Read-only broker inspection also found no
open ABBV order. The outcome is deterministically **no broker submission**, not an
ambiguous broker result.

ABBV remained held at `0.433314683` shares during investigation. The active position
lifecycle was `ca1318cf-f24e-46e3-ae0e-74122cd96d90`.

## Subsequent state and current blocker

The failed proposal is terminal and is not itself an active blocker. Later scans
continued to reproduce valid `TIME_STOP_EXIT` decisions from the current ABBV
position. The latest investigated decision was
`679ddfed-caf6-4ee2-8823-5bc9ab1a22ae` in run
`6f07e3da-3645-44eb-99fd-70682af8fe58`. Proposal creation was blocked by a stale
Alpaca price (`price timestamp must be fresh`).

Accordingly, the current safety state is: fresh exit decision awaiting a new proposal
from fresh valid data. The terminal approved attempt must not be reused, and a buy
must remain blocked while the freshly reproduced exit decision has priority.

## Follow-up quote-feed diagnosis

Read-only follow-up inspection found the same final-validation failure on three
separate approved proposals. All three approvals are terminal and none created an
intent, reservation, broker request, order, or fill:

| Approval-time quote | Alpaca default/IEX quote | Spread | Consolidated SIP comparison |
|---|---:|---:|---:|
| 2026-07-14 15:17:23Z | `233.70 / 259.88` | `1060.821 bps` | `244.14 / 244.36` (`9.007 bps`) |
| 2026-07-16 15:12:37Z | `237.23 / 252.07` | `606.581 bps` | `251.80 / 252.06` (`10.320 bps`) |
| 2026-07-17 about 15:00:08Z | `242.72 / 261.02` | `726.565 bps` | `258.34 / 258.54` (`7.739 bps`) |

The deployed request omitted an explicit equity feed. On this Basic-data account,
the default response matched explicit IEX data; a current explicit SIP request was
rejected because the account lacks the required recent-SIP entitlement. IEX reflects
one exchange rather than the consolidated market, so its ABBV bid and ask were not a
usable executable market for this safety policy. Alpaca documents that latest stock
quotes default to SIP when entitled and otherwise IEX, and that IEX is only one
exchange: [latest quote API](https://docs.alpaca.markets/us/v1.4.2/reference/stocklatestquotesingle-1),
[market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq), and
[historical stock data feeds](https://docs.alpaca.markets/us/v1.1/docs/historical-stock-data-1).

The configured `50 bps` spread guard was therefore correct to reject all three
quotes. The defect was operational ambiguity around the implicit feed and generic
Telegram explanation—not a reason to weaken the spread limit. Later current
position-management evidence evolved from `TIME_STOP_EXIT` to a valid
`PROFIT_PROTECT_EXIT`; exit-first BUY suppression remains intentional while that
genuine blocker is active.

The hardening change makes the configured equity feed explicit (`iex` or `sip`),
passes it on every latest-trade and latest-quote request, binds it and the exact
two-sided quote into the immutable Telegram display, and rejects stale, crossed,
wrong-feed, or over-limit quotes before a proposal can be inserted or displayed.
Final approval-time validation records structured bid, ask, feed, timestamp, age,
spread, threshold, and failure code. Its operator message now states the exact IEX
spread and safe next action: wait for fresh spread-valid data, then create a new
proposal and obtain a new manual approval.

## Audit findings and implementation

### A. Approval authority was created at reply time

Fixed. Every Telegram approval surface now creates an immutable
`proposal_display_envelopes` record after Telegram returns the message ID. The record
binds proposal version, exact message, symbol, side, action, lifecycle, strategy,
relationship/group/rotation step, expiry, request basis, ceilings, emergency identity,
configuration, formula versions, source type, and execution path. Approval rejects a
wrong reply-to, changed terms, superseded version, ineligible proposal, or missing
display authority. Test-only legacy fixture synthesis is isolated by
`TRADING_AGENT_TESTING=1`; production fails closed.

### B. Approval source and emergency path binding

Fixed. Approvals persist `approval_source_type` and `execution_path`. Ordinary
approvals cannot acquire emergency trigger authority. Emergency approval requires an
immutable trigger reason and the exact protective paper-exit path. Rotations retain
their step/group identity.

### C. Quantity/notional/risk ambiguity

Fixed in `canonical_sizing.py`. One conservative reference and one explicit/inferred
request basis determine quantity, notional, and stop risk. Both supplied fields must
be mathematically consistent. Caller notional can no longer override quantity-derived
exposure or risk. Canonical values and all ceilings are persisted on the intent.

### D. Caller-controlled execution context

Fixed. Durable approval, proposal, display, workflow, and consumption state are
reloaded before execution and recomputed again inside the `BEGIN IMMEDIATE` intent
transaction. A fresh `execution_risk_snapshots` row binds verified paper account,
balances, holdings, open orders, reservations, loss/kill-switch evidence, market/data
health, configuration, formula versions, capture time, expiry, and fingerprint.
Production cannot create an approvalless intent.

### E. Release environment and manifest truthfulness

Fixed. Release builds create a new virtual environment from `requirements.lock`,
record the installed inventory and its hash, hash release files, and only mark tests
verified after the exact eligibility gate passes. A developer `.venv` is never copied.

### F. Main reachability was too broad

Fixed. Main authority now requires candidate SHA equality with the current GitHub
`main` SHA. Being an ancestor of main is not authority.

### G. Manifest/tag circularity

Fixed with one coherent model. A build-local manifest is not expected inside its
source commit. Alternate immutable authority is an annotated `immutable-release-*`
tag plus one GitHub Release asset named `release-attestation.json`, bound to tag,
commit, configuration, schema/formula versions, and exact successful CI run.

### H. Intent transaction trusted caller fingerprints

Fixed. The transaction reloads and recomputes display, approval, proposal, workflow,
consumption, source/path, expiry/status, canonical sizing, and ceilings before it can
insert an intent or reservation.

### I. Integrity coverage

Expanded to include missing/mismatched display authority, intent/approval/display
fingerprints, canonical sizing, authoritative risk snapshots, broker ambiguity
classification, and durable blocker uniqueness/terminal-state rules. A migration
boundary preserves historical evidence without fabricating displayed terms.

### J. Exit-blocker lifecycle and operator messaging

Added durable `exit_blocker_states` with generations and explicit states for fresh
decision, manual approval, intent, reconciliation, and broker order. Records carry
proposal/approval/workflow/intent/broker provenance plus required user action and
safe automatic recovery. Digest text no longer says `No action needed` when an exit
blocker is active.

### K. Rotation authorization normalization

Fixed. Registry/allocation authorization accepts only non-empty, unique normalized
strategy identities, including the persisted list-of-object representation. Empty,
malformed, duplicated, or mismatched authorization now fails closed.

The adjacent direct-submit audit found no application broker submission path outside
the guarded executor/adapter boundary.

## Migration and remediation evidence

Migration `plutus_final_hardening_v1` is additive. It was applied twice to
`/private/tmp/plutus-abbv-investigation-20260715.sqlite3`; schema signatures remained
stable, `PRAGMA integrity_check` returned `ok`, and all old and new integrity counters
were zero.

`scripts/repair_exit_blocker.py` is dry-run by default, refuses every production-paper
database path, has no broker dependency, requires an exact source ID, and clears only
a terminal proposal-backed blocker with no broker-relevant intent/order ambiguity.
On `/private/tmp/plutus-abbv-repair-test.sqlite3`, dry-run proved eligibility, apply
created one audit event, a second apply was a no-op, and SQLite remained `ok`.

The production database was not remediated because the investigated failed proposal
was already terminal and later blocking came from fresh reproduced exit decisions,
not a corrupt stuck flag.

## Verification and rollout constraints

The complete offline pytest suite and compile pass must be green on the PR head, and
GitHub `CI / offline-tests` must complete successfully for that exact SHA. This branch
must not be merged or deployed by this investigation. Any later rollout requires a
separate approved change window, verified backup/restore rehearsal, explicit migration
of the stopped paper writers, immutable release construction, full release verifier,
and post-cutover paper-account/integrity checks.
