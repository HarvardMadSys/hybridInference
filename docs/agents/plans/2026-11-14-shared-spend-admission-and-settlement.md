# Shared spend admission and batched settlement

Status: implemented but not activated. The shared mode, its admission and
settlement protocol, the operator commands and the reporting described below
are in `apps/backend/serving/shared_spend/`. The mode is `local` in every
deployment and nothing in the code switches it. What remains is the activation
work in section 7.

Purpose: let several gateway processes share one spend ledger, so a backend
release can run old and new processes side by side (see the
[entry split design](../specs/2026-11-11-inference-entry-split-design.zh.md),
section 4.5). This plan covers the accounting only. Pod handoff, drain and the
deployment controller are separate. For how spend is charged and shown, see
[Accounting](../../developer/accounting.md).

## 1. Starting point

Each process keeps its own ledger (`serving/spend_ledger.py`): requests in
flight (reservations, in memory) and costs recorded but not yet written. One
writer per store sends them to the counters in coalesced batches, in one
transaction, fenced against erased accounts, with `txid_status` after a lost
COMMIT.

The gates (`verify_api_key`, grants, `authenticate_stored_key` for batch items,
the playground's session gate) read the counters and build a `SpendCheck`.
`SpendCheck.admit` checks again and reserves, synchronously, against what this
process knows. Admission call sites are `servers/concurrency._admit_spend` and
the batch scheduler's dispatch step. Cost recording sites are
`CostTracker.charge`, `anthropic_messages` and `embeddings`.

The policy lives in `serving/quota.py` and does not change. A request is refused
only when not even the flat minimum fits. Otherwise it reserves its estimate,
clamped to `[0.01, min(5.00, tightest headroom)]`. The estimate is not a cap on
the actual cost. The scopes enforced are account day (always), account month
(optional), key day and month (when the request is attributed to a key with a
budget) and project day and month (when the project has a budget). There is no
organization scope.

What a request is charged is decided by the billing points under the existing
pricing policy: an answer from its usage, a cancelled or timed-out call from
what it used or an estimate of it, and nothing for a call the upstream refused
or failed. The shared ledger records what they recorded and infers nothing.

## 2. Mode and activation boundary

- The single-row table `spend_accounting` (`mode` = `local` | `shared`) comes
  from migration `0020_shared_spend`, created as `local`. `local` is the
  per-process accounting every deployment runs today. Nothing writes the row.
- A process reads the mode once at startup and keeps it for its lifetime, so its
  admissions and settlements follow one protocol. It reports
  `capabilities.spend_accounting` (`protocol`, `mode`, currency and rate) in
  `config_instances`. The protocol is version 2: amounts are in the ledger
  currency, and a version 1 process cannot take part.
- `python -m serving.shared_spend status | list` is read-only: stored mode,
  registered processes and their reported mode, open reservations by instance and
  state with how their owner is known, receipts kept, closures and
  reconciliations recorded, and counter writes made outside the shared
  settlement. `reconcile INSTANCE_ID` retires what a gone process left (section
  6). It prints its plan and changes nothing without `--apply`. No command
  changes the mode.
- A statement-level trigger on the counter tables records any counter write made
  outside the shared settlement while the mode is `shared`
  (`spend_unidentified_writes`). It is evidence after the fact and never refuses
  a write, because refusing would lose the writer's money.
- The store protocol: the gateway's operational store is the
  `CachedOperationalStore` proxy over the PostgreSQL store. Both expose
  `shared_spend_root()`, and the shared ledger is kept per root, so the gate,
  the billing points and the batch worker reach one ledger. In shared mode a
  store without a root raises `SharedSpendUnsupported` instead of falling back
  to the per-process ledger.

Because the mode is cached at startup, a rolling restart would run local and
shared processes together, which is no safer than today. The transition must be
offline: stop every serving writer (local mode flushes on shutdown), change the
row, start every writer in the new mode. The rollback is the same in reverse,
after `status` shows no open reservation.

## 3. Admission (shared mode)

One short transaction per admission on the request pool, never held during
inference.

1. Mint a random `reservation_id` before the first attempt.
2. `BEGIN`, set `lock_timeout` and `statement_timeout` to the remaining budget.
   Statement A returns `txid_current()` and takes, in one fixed order, the shared
   erasure-fence lock of the account and then the exclusive transaction-level
   advisory locks of every scope the request is checked against (account, key
   with a budget, project with a budget), sorted by `(class, key)`.
3. Statement B runs after A returned, so its snapshot is taken after every lock
   was granted. In one snapshot it reads the existing row for this
   `reservation_id`, the erasure tombstone, the counters (account, key and
   project, day and month) and the sums of open reservations per scope. A
   settlement moves a cost from a reservation to a counter in one commit, so B
   sees it wholly or not at all.
4. Python applies the unchanged policy (`quota.first_refusal`, the headroom
   clamp) to what B read plus what only this process knows (its costs not yet
   settled). Statement C inserts the reservation, then `COMMIT`.
5. An existing row with the same digest (owner, key, project, amount, day,
   operation) is the previous attempt's commit: admitted, nothing inserted. A
   different digest is a conflict and is refused.

Bounds:

- A per-process `asyncio.Lock` per scope key (same order) allows one admission
  per scope per process in the database, so a hot account cannot hold the pool.
- The whole operation, retries included, runs under one deadline
  (`REQUEST_ACQUIRE_TIMEOUT_SEC`, 3 s), which also bounds pool acquisition,
  `lock_timeout`, `statement_timeout` and `idle_in_transaction_session_timeout`.
  The last covers the time between the transaction's statements: a process
  stalled there would hold its scopes' locks and block every other process, so
  the server ends its session.
- Overload or an unavailable database raises `DatabaseBusyError` (503 with
  `Retry-After`). Batch work is handed back for a later attempt.
- A lost COMMIT answer, or a connection that failed otherwise (SQLSTATE class
  08), is retried with the same id within the deadline. If the outcome is still
  unknown, or the admitting coroutine is cancelled after the id was used, the
  request is not dispatched and a release-not-dispatched is queued with the
  attempts' transaction ids. The writer releases only once `txid_status` reports
  each attempt final.
- A session the server ended must give its connection's place in the request
  pool back. asyncpg has one path that does not: a statement sent after the
  client read the server's goodbye fails with `InternalClientError`, the driver
  aborts the connection without cleaning it up, and the pool takes it as cleaned
  up. That place was never free again, and the pool has four. So
  `request_connection` cleans up a connection that was closed while out
  (`free_if_closed`), and admission counts that error as a failed connection.

## 4. Settlement (shared mode)

The batched writer stays. A request's reservation collects the costs recorded
under it (`req_ctx.SPEND_RESERVATION`, set at admission; batch dispatch seeds
it). When the request ends, however it ends, its slot is released in memory at
once and the reservation is queued as one settlement entry carrying exactly
those costs. The entry retires the reservation and adds the costs in one
transaction, and the rest of the estimate is given back.

| How the request ended | Settlement entry |
|---|---|
| Billing points recorded one or more costs (a cut-off stream's tail included) | `settle` with those costs |
| Priced at zero, not priced, refused, failed upstream, cancelled unbilled | `settle` with no cost: the reservation is released |
| Never became a request (admission abandoned) | `release` |

A request that ended with a 500, 502 or 504, was cancelled, or was billed by
nobody holds nothing once it ended, as in the per-process mode. An earlier
design kept such a request as an `unresolved` liability at its estimate. It was
dropped because it changed what a failure costs the caller (four $0.30 requests
the upstream answered 502 used up a $1.00 quota with nothing spent) and grew
without bound. So the shared mode, like the per-process mode, gives the operator
no protection against an upstream that charged for work whose answer was lost.

A batch attempt whose charge could not be told is recorded by the batch itself
(`batch_attempts.cost_status = 'unknown'`), and its reservation is given back.
Costs recorded with no open reservation (no check, or after the reservation was
queued) are plain increments.

Each settlement batch has a random `batch_id`, frozen membership and a payload
digest. A batch that lists a reservation twice, or carries a member cost
attributed to another account, key or project than the member, is refused whole
(`invalid`). Otherwise one transaction does:

1. `INSERT INTO spend_settlements (batch_id, digest, ...)`. On conflict the
   stored digest decides: the same digest means the batch already committed (its
   stored result is returned), a different one writes nothing (`conflict`).
2. Take the fence locks (shared).
3. Retire the members this instance holds for the same account, key and
   project (`DELETE ... USING unnest(...)`). A member not retired is
   `duplicate` (another batch retired it), `mismatch` (held, but not as the
   member says), `erased` or `missing`. None of them is charged.
4. Run the counter increments (the existing fenced statement) for the retired
   members' costs and the plain increments.
5. Insert `spend_settlement_members` rows (primary key `reservation_id`, so the
   database allows at most one retirement per reservation) and the batch result.

Any failure other than a refusal of the batch's values sends the identical batch
(same id and payload) again. The recorded id makes that harmless whether or not
the earlier attempt committed, so an unknown outcome is never taken as success
and settlement does not need `txid_status`. A batch the database refused for its
values (SQLSTATE class 22/23) is split into one batch per entry under new ids.
An entry refused alone is set aside (plain increment) or quarantined
(reservation), kept counted and reported. Plain increments are written before
retirements, so a split can only over-count transiently. Batches are capped in
size.

## 5. States and recovery

A reservation in memory is active (executing), queued (ended, pending
settlement), retired (settled or released, confirmed), quarantined (the counters
refused its costs) or unaccounted (its settlement found it no longer held as
recorded). In the database it is `active` or `quarantined`. A settled or
released reservation's row is gone and its id is in `spend_settlement_members`.
Releasing an execution slot is not retiring a reservation. The row count is
never used as concurrency, and no distributed concurrency policy is added.

An `active` row counts against the current day and month. A `quarantined` row
counts against the day its request ended and that day's month, at the larger of
its estimate and its known cost, until an explicit resolution. The process that
holds a quarantined or unaccounted reservation adds it to its own checks against
the same day (`SharedReservation.ended_day`), so every process's gate answers a
caller alike. Liabilities of an earlier month count against nothing a check can
still be made for. They stay in what the process reports.

Recovery rules:

- A writer that is cancelled or failing sends the same batch again until it gets
  an answer. At shutdown `flush_all` sends what it can and reports the rest
  (`holdings`, `unwritten`). A defect in the writer's own bookkeeping is retried
  after the usual back-off.
- A crashed process leaves `active` rows counted against the current day and
  month and every later one, until an operator reconciles them. `status` shows
  them with how their owner is known and adds them up under `stranded`, and every
  process's hourly survey alerts on them.
- A settlement that finds a member not held as recorded (`missing`, `mismatch`)
  adds none of its costs and keeps the member as an unaccounted liability in this
  process: counted at the larger of estimate and known cost, on the day it ended,
  reported, logged and alerted.
- A member the counters refused is marked `quarantined` by a write of its own.
  Until that write is answered, its row is active at its estimate, so the process
  keeps it as a waiting reservation with its excess cost counted. A failed mark
  is retried with its own back-off and timer, so it neither holds back the other
  settlements nor the marks behind it. A mark that keeps failing for a minute is
  logged once at ERROR. The answer says which case it is: quarantined, retired by
  another batch first (a duplicate), the account was hard-deleted (dropped), or
  neither row nor receipt exists (unaccounted).
- A settlement that finds a member another batch retired first (`duplicate`,
  which happens when an operator reconciled this process while it ran) adds none
  of its costs. They are then in no counter. The settlement logs
  `spend_settlement_duplicate` with the reservations and the amount, and the
  operator's audit rows say what was charged instead.
- Nothing expires a reservation or forgives a liability: no TTL and no heartbeat
  retires one. A `quarantined` reservation is not touched by `reconcile`. The
  counters refused its costs for their own values, which is a defect to
  investigate, and it stays counted until that is decided.
- Hard delete purges the account's reservations in the hard-delete transaction.
  Admission checks the tombstone under the fence lock, so no row is written for
  an erased account. The closure, reconciliation and receipt tables name no
  account.

### What the guarantee covers

Shared atomic admission prevents two admissions, in any processes, from being
granted the same estimated headroom. It does not bound actual cost: a request may
cost more than its estimate. Inside the admitting process, the known costs of a
running request above its estimate are added to every later check at once. Other
processes see the actual cost when the settlement commits.

An admission adds what only its process knows to what it read from the database,
and the two are not taken at one instant. A settlement of this process can commit
after the read and be answered before the decision. So what a settlement moved
into the counters (plain costs, and each request's cost above its estimate) stays
known to the process for a few seconds, and an admission adds whatever was
answered since its read. A settlement that committed just before the read and was
answered just after is counted twice for that admission, which errs toward
refusing.

A request that has ended keeps its estimate in the database until its settlement
commits, normally milliseconds later. An admission in that interval counts the
estimate rather than the smaller cost, so a caller within one estimate of a limit
can be refused then and admitted a moment later. This errs toward refusing, never
the reverse.

## 6. When a process stops: closure, reconciliation, receipts

Two tables besides reservations and receipts: `spend_instance_closures` (the
processes that will send nothing more) and `spend_reconciliations` (the audit
record of what an operator retired).

**How an owner is known** (`store.owner_state`), from its closure and the
instance registry (`config_instances`): `closed`, `stopped`, `stale` or `not
registered` mean it may never settle what it holds. Otherwise it is its lifecycle
while the registry hears from it. A stale heartbeat may be a process cut off from
the database, so nothing here acts on the registry's word alone.

**Closure.** At shutdown, after the final flush, a ledger with nothing left to
send, no unanswered batch and no running request records `closed_by = 'shutdown'`
for its instance id and sends nothing after that. A process that leaves something
unsent, or cannot reach the database, is not closed (`spend_instance_not_closed`
is logged) and is then in the position of a killed one.

**Reconciliation** (`python -m serving.shared_spend reconcile INSTANCE_ID`) is for
an instance known to be gone. Without `--apply` it prints its plan. With `--apply
--operator NAME --reason TEXT`:

1. It refuses an instance the registry hears from (every chunk's transaction
   checks again), and an id that no reservation, receipt, registration or closure
   names, so a mistyped id records no closure.
2. It takes the instance's active reservations oldest first, 256 at a time, each
   paired with the request-log rows written under it. In shared mode
   `CompletionsLogger` puts into each row's metadata the reservation the request
   ran under (`spend_reservation`), what the billing points had recorded under it
   by then (`spend_charged_usd`) and the UTC day each part was recorded on
   (`spend_charged_days`).
3. A reservation whose rows record a charge is settled with it, on the days the
   row says it was recorded, not the day the row was written (the row can be
   written after midnight for a charge recorded just before it). With several
   rows, the largest total is used. A reservation with no row, or rows recording
   no charge, is settled with none: its estimate is given back. The row's own
   `cost_usd` is shown beside the charge and never used, and the estimate itself
   is never charged.
4. Each chunk is one transaction: a settlement in the gone process's name
   (batch id `op_...`) and one `spend_reconciliations` row per retired
   reservation (what it held, the outcome, the charge, the request-log rows as
   `evidence`, how its owner was known, the operator and the reason). No account
   is named there, and the request ids lead to the log.
5. The instance is recorded `closed_by = 'operator'` with the name and reason. A
   `shutdown` closure already there stands.

A chunk the counters refuse for a charge's own values is retired one reservation
at a time. A reservation refused alone is left as it is and named in the result,
and the instance is not recorded closed. The command derives the erasure fence as
the gateway does, loading the deployment's stored configuration for the secret,
and refuses a secret whose fingerprint is not the one pinned in the database.

Why a reservation with no recorded charge is given back: a request cut off by its
process dying never reached its billing point, and in the per-process mode it is
charged nothing. Keeping it would hold an account's budget on no evidence, with no
end. This gives up charging a request that ended, was billed in memory and whose
log row was lost with the process. That window is the last moments of traffic
(longer while log writes are failing), never more than the requests the process
had in flight or just ended.

Why a wrong "it is gone" cannot charge twice: the retirements are recorded like
any settlement. If the process is alive and settles the same reservation later,
its settlement finds it a `duplicate` and adds nothing. The mistake can leave a
request uncharged, but never counts one twice.

**Receipt retention** (`SharedSpendStore.purge_receipts`, seven days, constant
`RECEIPT_RETENTION_SECONDS`). A receipt makes a repeated batch harmless, and only
the process that made a batch ever sends it again. A receipt older than the
retention is deleted by its own process, unless it belongs to a batch that process
has not had an answer for (its replay queue), or by any process once its maker's
closure is that old and the registry does not hear from the maker. Age alone never
deletes one, so the receipts of a process that stopped without closing stay until
an operator reconciles it. A `shutdown` closure is deleted once it is that old and
its instance has no receipt or reservation left. Operator closures and
reconciliation rows are kept.

Residual risk: a process an operator wrongly closed, then cut off from the
database for over a week while holding an unanswered batch, would on return send a
batch whose receipt is gone. Its members are reported missing (not charged again,
kept as unaccounted, alerted), but its plain increments are added a second time.

**Upkeep.** Each ledger runs the purge and a survey about once an hour, first at a
random point within the hour, in its own tracked task, off the request path and
apart from the settlement writer. Only a process that is admitting or settling runs
it. A run deletes at most 20 chunks of 500 batches, and one that stopped with more
to delete is followed by the next within a minute. The survey uses the planner's
estimates for receipt counts rather than counting the tables.

### What a process reports

The gateway has no metrics endpoint, so the existing surfaces are used.

- `SharedSpendLedger.stats()`, shown by `/health/deep` to an administrator under
  `spend_accounting.ledgers` (anyone sees the mode). It covers, since the process
  started: admissions by outcome with a latency histogram (p50, p95, p99 as bucket
  upper bounds); what it holds now (running and ended-awaiting-settlement
  requests, each with amount and the age of the oldest, plus quarantined and
  unaccounted); settlement counters; how long writes have been failing and the
  last error; receipts purged; and the last hourly survey of every process's
  active and quarantined totals, what is stranded and the receipts kept.
- `python -m serving.shared_spend status`: the database-wide view, with closures
  and reconciliations.
- Alerts (catalogued types): `spend_admission_unavailable` (the database did not
  answer an admission; the request gets 503 and is logged `DATABASE_BUSY`),
  `spend_settlement_stalled` (no settlement written for 60 s, once per stall),
  `spend_reservations_stranded` (active reservations of a gone owner, the oldest at
  least 15 minutes old, 6 h cooldown), `spend_settlement_refused` and
  `spend_settlement_unaccounted`. With several processes, each one that observes a
  condition alerts once per cooldown.
- Structured log fields for the shared ledger's lines (`reservations`,
  `increments`, `amount_usd`, `batch_id`, `sqlstate`, ...) are emitted under names
  no other line uses.

## 7. Remaining work before activation

Nothing here is done. Before any deployment switches the row:

1. Build and review the offline transition and its rollback (section 2).
2. Size the request pool for the admission's round trips, measured on the
   deployment's own database. The local benchmark was a loopback exercise.
3. Decide what resolves a `quarantined` reservation (section 5).
4. Write into the deployment's runbook who runs `reconcile` and how they establish
   that a process is gone. The command records the decision, it cannot make it.
5. Fix the cost of a check at scale. `_local_extra` walks every reservation the
   process holds and every cost not yet settled, for each gate check and again
   inside every admission while its locks are held. That is fine for hundreds of
   requests in flight and not for thousands at hundreds of requests a second. Keep
   running totals per account, key and project, as the per-process ledger does,
   and measure before a deployment of that size switches.
6. Decide what a batch owner sees while budget is held by stranded reservations.
   The batch scheduler takes a refusal the counters do not explain to mean requests
   in flight and probes again every 30 s. With reservations left by a killed
   process that lasts until they are reconciled, and the batch looks in progress
   meanwhile. The stranded alert is what tells an operator.

## 8. Tests that guard it

- Unit: the shared ledger's state machine with a fake store (admit retry and
  ambiguity, cancellation, settle and release, late cost, unknown replay, refusal
  split, quarantine, the day a liability counts against, the writer recovering from
  its own defect), the mode switch at every call site (HTTP surfaces, grants,
  playground, batch), closure only with everything sent, the numbers a ledger
  reports and each alert, and the upkeep schedule.
- `dbtest`: two processes contending on the same and on independent scopes;
  settlement racing admission with deterministic interleavings (the naive two-read
  hazard shown to lose a cost, the protocol shown not to); lock wait before read;
  replay, changed payload, overlap, unknown commit, refused entry, crash with
  stranded reservations, erasure; the migration and the no-runtime-DDL guard; two
  gateways through the whole request path answering a caller alike, after upstream
  failures and across midnight with a quarantined liability; a stalled admission
  ended by the server so another process admits; a connection the server ended
  giving its pool place back; a killed process's reservations charged from
  request-log rows written through the real logger, or given back, each with its
  audit row; an instance the registry hears from refused by the command and by the
  statement; a process wrongly declared gone settling afterwards without a second
  charge; receipt deletion rules; closures deleted only when nothing is left under
  them. Each safeguard was checked by removing it and seeing a test fail.
