# Shared spend admission and batched settlement (phase 2a)

Status: implementation contract, 2026-11-14; revised 2026-11-15 after review
(what a finished request holds, section 4; the day a liability counts against,
section 5) and 2026-11-16 (what a stopped process leaves and who cleans it up,
section 6b; what a process reports, section 6c; what activation still needs,
section 6d). Base: `dev` at `b98678a42`.
Scope: the shared-accounting preparation layer for the backend release
handoff. It does not implement Pod handoff, drain, ingress control or a
deployment controller, and it does not activate anything: the new mode is
off by default and nothing in this change turns it on.

## 1. What exists today (current source)

- `serving/spend_ledger.py` keeps, per process, the requests in flight
  (reservations, in memory) and the costs recorded but not yet written. One
  writer per store sends them to the counters in coalesced batches
  (`PostgresOperationalStore.increment_user_costs`, one transaction, fenced
  against erased accounts, `txid_status` after a lost COMMIT).
- The gates (`verify_api_key`, grants via `authenticate_grant_model_call`,
  `authenticate_stored_key` for batch items, the playground's session gate,
  and `verify_batch_management_identity`, which only reports) read the
  counters and build a `SpendCheck`. Admission (`SpendCheck.admit`) checks
  again and reserves, synchronously, against what this process knows.
- Admission call sites: `servers/concurrency._admit_spend` (every API
  surface behind `enforce_user_concurrency`, grants included, and the
  playground's `_reserve_playground_spend`), and the batch scheduler
  (`BatchScheduler` dispatch step). Cost recording call sites:
  `CostTracker.charge` (chat, responses, streams, cancellations),
  `anthropic_messages`, `embeddings`.
- Policy (`serving/quota.py`): a request is refused only when not even the
  flat $0.01 fits; it then reserves its estimate clamped to
  `[0.01, min(5.00, tightest headroom)]`. The estimate is not a maximum of the
  actual cost. Enforced scopes: account day (always; default $1000), account
  month (optional), key day/month (when the request is attributed to a key
  that has a budget), project day/month (when the project has a budget).
  The "organization" ceiling is applied to the *account's* spend
  (`COALESCE(o.budget_*, k.quota_*)`); there is no independent organization
  aggregate counter, so there is no organization scope here.
- The billing point already prices cancellations and gateway timeouts (an
  estimate once the call left the gateway, nothing before), and the batch
  dispatcher marks an attempt whose charge cannot be known as
  `cost_status="unknown"`.

## 2. Mode and activation boundary

- New single-row table `spend_accounting` (`mode` = `local` | `shared`),
  created by migration `0019_shared_spend` with `local`. `local` is today's
  behaviour, unchanged, and is what every existing deployment keeps. Nothing
  in this change writes the row.
- A process reads the mode once, at startup (after the schema check, inside
  the same retry loop) and keeps it for its lifetime, so its admissions and
  settlements always follow one protocol. It reports
  `capabilities.spend_accounting = {"protocol": 1, "mode": ...}` in
  `config_instances`.
- `python -m serving.shared_spend status | list` is read-only: stored mode,
  registered processes and their reported mode (`participation_proven` is
  always false: an older build never registers), open reservations by
  instance and state with how their owner is known, the receipts kept, the
  closures and reconciliations recorded, and counter writes made outside the
  shared settlement. `reconcile INSTANCE_ID` retires what a process that is
  gone left (section 6b); it shows its plan and changes nothing unless
  `--apply` is given. No command changes the mode.
- A statement-level trigger on the counter tables records any counter write
  made outside the shared settlement while the mode is `shared`
  (`spend_unidentified_writes`). It is evidence after the fact, not
  prevention: it never refuses a write (that would lose the writer's money).
- **Not in this change, and required before any activation:** a separately
  reviewed, controlled *offline* transition. Because the mode is cached at
  startup, a rolling restart would run local and shared processes side by
  side, which is no better than today's per-process accounting and is not
  claimed safe. The transition must stop every serving writer (local mode
  flushes on shutdown), change the row, and start every writer in the new
  mode; its rollback is the same in reverse, after `status` shows no open
  reservation. Its bootstrap/release mechanics belong to that later review.

**Store protocol.** The gateway's operational store is the
`CachedOperationalStore` proxy over the PostgreSQL store. Both expose
`shared_spend_root()` (the proxy returns its wrapped store's root), and the
shared ledger is kept per root, so the gate, the billing points and the batch
worker reach one ledger whichever object they hold. In the shared mode a
store without a root raises (`SharedSpendUnsupported`) instead of falling
back to the per-process ledger, and startup checks the store it built.

## 3. Admission (shared mode)

One short transaction per admission on the request pool, never held during
inference:

1. Mint `reservation_id` (random, server-side) before the first attempt.
2. `BEGIN`; `SET LOCAL lock_timeout` / `statement_timeout` to the remaining
   budget; statement A returns `txid_current()` and takes, in one fixed
   order, the shared erasure-fence lock of the account (when a fence secret
   is configured) and then the exclusive transaction-level advisory locks of
   every scope the request is checked against (account; key when it has a
   budget; project when it has a budget), sorted by `(class, key)`.
3. Statement B, issued after A returned (so its snapshot is taken after every
   lock was granted), reads in one snapshot: the existing row for this
   `reservation_id`, the erasure tombstone, the counters (account day/month,
   key day/month, project day/month) and the sums of open reservations per
   scope. Counters and reservations come from one statement, so a settlement
   (which moves a cost from a reservation to a counter in one commit) is seen
   wholly or not at all.
4. Python applies the unchanged policy (`quota.first_refusal`, the headroom
   clamp) to what B read plus what only this process knows (its costs not yet
   settled), then statement C inserts the reservation; `COMMIT`.
5. An existing row with the same digest (owner, key, project, amount, day,
   operation) is the previous attempt's commit: admitted, nothing inserted. A
   different digest is a conflict and is refused.

Bounds: a per-process `asyncio.Lock` per scope key (taken in the same order)
keeps one admission per scope per process in the database, so a hot account
cannot hold the request pool; the whole operation, retries included, runs
under one deadline (`REQUEST_ACQUIRE_TIMEOUT_SEC`, the existing request-path
bound), which also bounds pool acquisition, `lock_timeout`,
`statement_timeout` and `idle_in_transaction_session_timeout`. The last one is
for the time *between* the transaction's statements, which the other two do
not cover: a process that stalls there (a blocked event loop, a stopped
container) would hold its scopes' locks, and every other process's admission
for them, until it came back; the server ends its session instead. Overload or
an unavailable database raises the existing `DatabaseBusyError` (503 with
`Retry-After`); batch work is handed back for a later attempt. A lost COMMIT
answer, or a connection that failed otherwise (SQLSTATE class 08), is retried
with the same id within the deadline. If the outcome is still unknown, or the
admitting coroutine is cancelled after the id was used, the request is not
dispatched and a
release-not-dispatched is queued carrying the attempts' transaction ids; the
writer only releases once `txid_status` reports each attempt final.

## 4. Settlement (shared mode)

The batched writer stays. A request's reservation collects the costs recorded
under it (`req_ctx.SPEND_RESERVATION`, set at admission; batch dispatch seeds
it). When the request ends, however it ends, its slot is released at once (in
memory) and the reservation is queued as one settlement entry carrying exactly
those costs, none included. The entry retires the reservation and adds the
costs in one transaction; the rest of the estimate is given back.

**What a request is charged is decided where it always was:** by the billing
points (`CostTracker.charge`, the Anthropic surface, embeddings) under the
unchanged pricing policy. They price an answer from its usage, a call that was
cancelled or timed out from what it used or an estimate of that (the
cancellation billing), and charge nothing for a call the upstream refused or
failed. The shared ledger records what they recorded and infers nothing of its
own. In particular it does not keep a reservation because the request ended
with a 500, 502 or 504, was cancelled, or was billed by nobody: such a request
holds nothing once it has ended, exactly as in the per-process mode.

| How the request ended | Settlement entry |
|---|---|
| billing points recorded one or more costs (a cut-off stream's tail included) | `settle` with those costs |
| priced at zero, not priced, refused, failed upstream, cancelled unbilled | `settle` with no cost: the reservation is released |
| never became a request (admission abandoned, see section 3) | `release` |

An earlier revision kept a request that was sent and not billed, or that failed
with 500/502/504, as an `unresolved` liability at its estimate, counted against
its day and month until an operator resolved it. Review withdrew it: it changed
what a failure costs the caller (four requests of $0.30 the upstream answered
502 used up a $1.00 quota with nothing spent, and nothing released them) and it
accumulated without bound. Whether an upstream charged the operator for a call
it failed is a question for the pricing policy and its billing points, not for
the accounting that records them. The trade-off is deliberate, and worth
saying plainly: like the per-process mode, the shared mode gives the operator
no protection against an upstream that charged for work whose answer was lost.

A batch attempt whose charge could not be told is recorded as such by the batch
itself (`batch_attempts.cost_status = 'unknown'`), as before; its reservation is
given back like any other. Costs recorded with no open reservation (no check,
or after the reservation was queued: late costs) are plain increments.

Each batch has a random `batch_id`, frozen membership and a payload digest.
A batch that lists a reservation twice, or carries a member cost attributed
to another account, key or project than the member, is refused whole before
anything is written (`invalid`). Otherwise one transaction:

1. `INSERT INTO spend_settlements (batch_id, digest, ...)`; on conflict the
   stored digest decides: same, the batch already committed (its stored
   result is returned); different, nothing is written (`conflict`).
2. Fence locks (shared) as today.
3. Retire the members this instance holds *for the same account, key and
   project*: `DELETE ... USING unnest(...)`. A member not retired is
   `duplicate` (another batch retired it), `mismatch` (held, but not as the
   member says), `erased` or `missing`; none of them is charged. (`erased` is
   told by the erasure fence, which the gateway always configures.)
4. Counter increments (the existing fenced statement) for the costs of the
   retired members and for the plain increments.
5. `spend_settlement_members` rows (primary key `reservation_id`: at most one
   retirement per reservation, enforced by the database) and the batch result.

Any failure other than a refusal of the batch's values sends the identical
batch (same id and payload) again; the recorded id makes that harmless
whether or not the earlier attempt committed, so `txid_status` is not needed
for settlement and an unknown outcome is never taken as success. A batch the
database refused for its values (SQLSTATE class 22/23, so known not
committed) is split into one batch per entry under new ids; an entry refused
alone is set aside (plain increment) or quarantined (reservation), kept
counted and reported. Admission does use `txid_status`, to learn whether an
abandoned attempt committed before releasing its row.

Order inside a batch writes plain increments before retirements, so a split
can only over-count transiently. Batches are capped in size.

## 5. States

In memory per reservation: active (executing) → queued (ended, pending
settlement) → retired (settled or released, confirmed), or quarantined (the
counters refused its costs), or unaccounted (its settlement found it no longer
held as recorded). In the database: `active`, `quarantined`; a settled or
released reservation's row is gone and its id is in
`spend_settlement_members`. Releasing an execution slot is not retiring a
reservation; the row count is never used as concurrency, and no distributed
concurrency policy is added.

An `active` row counts against the current day and month. A `quarantined` row
counts against the day on which its request ended, and that day's month, at the
larger of its estimate and its known cost (its costs are in no counter), and
stays until an explicit resolution. Nothing expires either.

**One day, in the database and in memory.** The process that holds a
quarantined or unaccounted reservation adds it to its own checks -- the gate's
early refusal reads the counters only, so it adds the quarantined ones too --
against that same day and month (`SharedReservation.ended_day`), never against
a later one. The gate of the process that served a request and every other
process's gate therefore answer a caller alike on every day; they differ only
in where a refusal happens (the holder's gate, before the database is asked).
Liabilities of an earlier month count against nothing a check can still be made
for: they leave the set every check reads and stay in what the process reports
(`holdings`, `unwritten`, the shutdown log).

## 6. Recovery, liabilities and what is not done here

- Writer cancelled or failing: the same batch (id and payload) is sent again
  until it gets an answer; on shutdown `flush_all` sends what it can and
  reports the rest (`holdings`, `unwritten`).
- Process crash: its `active` rows stay counted against the current day and
  month, and against every later one for as long as they stay (an active row
  counts against the current one). `status`/`list` show them with how their
  owner is known, `status` adds them up under `stranded`, and every process's
  hourly survey alerts on them (section 6c). Nothing expires them: an operator
  reconciles them (section 6b).
- A defect in the writer's own bookkeeping (not a failed write) is tried again
  after the usual back-off: not at once, where a defect that persists would
  spin the writer, and not never, which would leave what is queued waiting for
  the next request.
- A settlement that finds a member not held as recorded (`missing`,
  `mismatch`) adds none of its costs and keeps the member in this process as
  an *unaccounted* liability: counted in this process's checks at the larger
  of its estimate and its known cost, on the day it ended, reported by
  `holdings`/`unwritten`, logged and alerted. A `quarantined` member stays in
  the database (and in `holdings`).
- A member the counters refused is marked `quarantined` in the database by a
  write of its own, and nothing is assumed before that write is answered:
  until then its row is active at its estimate, so the reservation stays a
  waiting one in its process (its cost above the estimate counted there), and
  a mark that fails is sent again with the writer's back-off (it is harmless
  to repeat). The answer says which it is: the row is quarantined; another
  batch retired it first (a duplicate, below); or there is neither a row nor
  a receipt, and the process then counts it as unaccounted.
- A settlement that finds a member another batch retired first (`duplicate`:
  an operator reconciled this process while it was running, section 6b) adds
  none of its costs. They are then in no counter, and the settlement says so
  (`spend_settlement_duplicate`, with the reservations and the amount): the
  operator's audit rows say what was charged instead.
- Nothing expires a reservation or forgives a liability: no TTL and no
  heartbeat retires one. Settlement receipts are deleted under the retention
  rule of section 6b, never by age alone.
- A `quarantined` reservation is not reconciled by the command of section 6b.
  The counters refused its costs for their own values, which is a defect to
  look into, not a question of who is gone; it stays counted on its day and
  month until that is decided (section 6d).
- Hard delete: the account's reservations are purged in the hard-delete
  transaction; admission checks the tombstone under the fence lock, so no row
  is written for an erased account; a settlement for one finds its rows gone
  and, as today, writes no counter. The closure, reconciliation and receipt
  tables name no account.

## 6a. The accounting guarantee, precisely

Shared atomic admission prevents two admissions, in any processes, from being
granted the same estimated headroom. It does not bound actual cost: the
existing policy admits while $0.01 fits and reserves an *estimate* clamped to
what is left, and a request may cost more than its estimate. Within the
admitting process, the known costs of a running request that exceed its
estimate (several charges included) are added to every later check at once;
other processes see the actual cost when the settlement commits. Late costs
(after the request ended) are plain increments of the next batch.

An admission adds what only its process knows to what it read from the
database, and the two are not taken at one instant: the process's own
settlement can commit after the read and be answered before the decision. The
read then has the request at its estimate and the counters without its cost,
and by the decision the process no longer holds it. So what a settlement
moved into the counters -- its plain costs, and the part of each request's
cost above its estimate -- stays known to the process for a few seconds (as
long as an admission can run), and an admission adds whatever was answered
since its read was made. A settlement that committed just before the read and
was answered just after is counted twice for that one admission, which errs
towards refusing.

A request that has ended keeps its estimate in the database until its
settlement commits, normally milliseconds later. An admission made in that
interval counts the estimate rather than the (usually smaller) cost, so a
caller within one estimate of a limit can be refused then and admitted a moment
later: conservative, never the reverse.

## 6b. When a process stops: closure, reconciliation, receipt retention

Besides the reservations and the receipts of section 4, two tables:
`spend_instance_closures` (the processes that will send nothing more) and
`spend_reconciliations` (the audit record of what an operator retired).

**How an owner is known** (`store.owner_state`), from its closure and the
instance registry (`config_instances`): `closed`, `stopped`, `stale` or `not
registered` -- these four mean it may never settle what it holds -- or its
lifecycle while the registry hears from it. The registry cannot prove a
process gone (a stale heartbeat may be a process cut off from the database),
so nothing below acts on the registry's word alone.

**Closure.** At shutdown, after the final flush, a ledger with nothing left to
send, no batch unanswered and no request still running records
`closed_by = 'shutdown'` for its instance id and sends nothing after that
(`SharedSpendLedger.close`). The registry's
own `stopped` cannot serve as this record: it is reported earlier in shutdown,
before that flush, and its row is pruned after a week. A process that leaves
something unsent, or cannot reach the database, is not closed
(`spend_instance_not_closed` is logged) and is then in the position of a
killed one.

**Reconciliation** (`python -m serving.shared_spend reconcile INSTANCE_ID`),
for an instance known as gone. Without `--apply` it prints its plan and
changes nothing. With `--apply --operator NAME --reason TEXT`:

1. It is refused for an instance the registry hears from (the transaction of
   every chunk checks again), and for an instance id that no reservation,
   receipt, registration or closure names: a mistyped id records no closure.
2. The instance's active reservations are taken oldest first, 256 at a time,
   each paired with the request-log rows written under it. As a row is
   written, in the shared mode only, `CompletionsLogger` puts in its metadata
   the reservation the request ran under (`spend_reservation`) and what the
   billing points had recorded under that reservation by then
   (`spend_charged_usd`): the ledger's own record of what the reservation's
   settlement would add. Rows are looked up by the reservation's account from
   its admission on, which the log's existing index serves.
3. A reservation whose rows record a charge is settled with it, on the UTC
   day the row was written; with several rows, each carries the total when it
   was written and the largest is used. One with no row, or rows recording no
   charge, is settled with none: its whole estimate is given back. The row's
   own `cost_usd` is shown beside the charge and never used -- a failed
   request's row can carry a cost no billing point charged -- and the
   estimate itself is never charged.
4. Each chunk is one transaction: a settlement made in the gone process's
   name (the statements, fence and receipts of section 4; batch id `op_...`)
   and one `spend_reconciliations` row for each reservation it retired -- what
   it held, the outcome, the charge, the request-log rows (`evidence`), how
   its owner was known, the operator and the reason. No account is named
   there; the request ids lead to the log.
5. The instance is recorded `closed_by = 'operator'` with the name and the
   reason. A `shutdown` closure already there stands.

A chunk the counters refuse for a charge's own values is retired one
reservation at a time; a reservation refused alone is left as it is and named
in the command's result, and the instance is then not recorded closed. One
charge the counters cannot take does not keep the rest held.

The command derives the erasure fence as the gateway does: it loads the
deployment's stored configuration for the secret, and refuses a secret whose
fingerprint is not the one pinned in the database. A wrong secret would make
the fence's lock and tombstone check guard nothing.

*Why a reservation with no recorded charge is given back, not kept.* A request
cut off by its process dying never reached its billing point; in the
per-process mode it is charged nothing. Keeping it would hold an account's
budget on no evidence, with no end. What this gives up: a request that ended,
was billed in memory and whose log row was lost with the process (or was
written before a later charge of the same request) is not charged for that.
The row is queued when the request ends and written by a small pool, so that
is the last moments of traffic (longer while log writes are failing), and
never more than the requests the process had in flight or just ended.

*Why a wrong "it is gone" cannot charge twice.* The retirements are recorded
like any settlement's. If the process is alive and settles the same
reservation later, its settlement finds it `duplicate` and adds nothing. The
mistake can leave a request uncharged (released here, its real cost then
dropped as a duplicate); it cannot count one twice.

**Receipt retention** (`SharedSpendStore.purge_receipts`;
`RECEIPT_RETENTION_SECONDS` is seven days). A receipt is what makes a repeated
batch harmless, and a batch is only ever sent again by the process that made
it, until that process has its answer. So a receipt older than the retention
is deleted

- by its own process, unless it is of a batch that process has not been
  answered for (it knows those exactly: its replay queue); and
- by any process, once its maker's closure is that old and the registry does
  not hear from that maker now.

Age alone never deletes one: the receipts of a process that stopped without
closing stay until an operator reconciles it. A `shutdown` closure is deleted
once it is that old and its instance has no receipt and no reservation left.
An operator's closure, and every reconciliation row, is kept.

*Residual risk.* A process an operator wrongly closed, then cut off from the
database for more than a week while holding an unanswered batch, would on its
return send a batch whose receipt is gone. Its members would be reported
missing -- not charged again, kept as unaccounted and alerted -- and its plain
increments would be added a second time.

**Upkeep.** Each ledger runs the purge and a survey (section 6c) about once an
hour, the first time at a random point within the hour, in its own tracked
task: off the request path and apart from the settlement writer. Only a
process that is admitting or settling runs it. A run deletes at most 20
chunks of 500 batches; one that stopped there with more to delete is followed
by the next within a minute, not an hour, so a busy process (which makes a
receipt for nearly every request) cannot fall behind for good. The survey
takes the planner's estimates for the receipt counts rather than counting the
tables.

## 6c. What a process reports

The gateway has no metrics endpoint; its established surfaces are used.

- `SharedSpendLedger.stats()`, shown by `/health/deep` to an administrator
  under `spend_accounting.ledgers` (anyone is shown the mode). Since the
  process started: admissions by outcome (`admitted`, `refused`,
  `unavailable`, ...) and a latency histogram with p50/p95/p99 as bucket upper
  bounds; what it holds now -- running requests, and ended ones awaiting
  settlement, each with its amount and how long the oldest has been so --
  plus quarantined and unaccounted; settlement counters (batches answered,
  write failures, entries refused, members unaccounted), how long writes have
  been failing and the last error; receipts purged; and its last hourly survey
  of the database: every process's active and quarantined totals, what is
  stranded, the receipts kept. None of it changes `status`.
- `python -m serving.shared_spend status`: the database-wide view on demand,
  with closures and reconciliations.
- Alerts, on the gateway's own path, each a catalogued type:
  `spend_admission_unavailable` (an admission the database did not answer;
  every such request is answered 503 and logged `DATABASE_BUSY`),
  `spend_settlement_stalled` (no settlement written for 60 s, so ended
  requests keep their estimates held; once per stall) and
  `spend_reservations_stranded` (active reservations of an owner known as
  gone, the oldest at least 15 minutes old; 6 h cooldown), besides
  `spend_settlement_refused` and `spend_settlement_unaccounted`. With several
  processes, each one that observes a condition alerts once per cooldown.
- Log lines. The log formatters emit only the structured fields they are
  told of, and none of the shared ledger's were among them, so a line an
  alert pointed to would have carried its event name only. Its fields
  (`reservations`, `increments`, `amount_usd`, `batch_id`, `sqlstate`, ...)
  are added, under names no other line uses. The per-process ledger's lines
  have the same gap on `dev` (`entries`, `cost_usd`, `requests`); they are
  left exactly as they are here, since a list of every pending cost wants
  writing in several lines rather than one, and that is a change to the
  default mode's output for a change of its own.

## 6d. Required before the shared mode is activated (not in this change)

Merging this change activates nothing. Before any deployment switches the row:

1. The offline transition and its rollback (section 2).
2. Sizing the request pool for the admission's round trips, measured on the
   deployment's own database (section 7's benchmark is a loopback exercise).
3. A decision on what resolves a `quarantined` reservation (section 6).
4. Who runs `reconcile`, and how they establish that a process is gone, in
   the deployment's runbook: the command records the statement, it cannot
   make it.
5. The cost of a check at scale. `_local_extra` walks every reservation the
   process holds and every cost not yet settled, for every gate check and
   again inside every admission while its locks are held. That is fine for
   hundreds of requests in flight and not for thousands at hundreds of
   requests a second: keep running totals per account, key and project, as
   the per-process ledger does, and measure, before a deployment of that size
   switches.
6. What a batch owner sees while budget is held by stranded reservations. The
   batch scheduler takes a refusal that the counters alone do not explain to
   be requests in flight and probes again every 30 s; with reservations left
   by a killed process that lasts until they are reconciled, and the batch
   looks in progress meanwhile. The stranded alert is what tells an operator.

## 7. Verification plan

- Unit: the shared ledger's state machine with a fake store (admit retry and
  ambiguity, cancellation, settle/release, late cost, unknown replay, refusal
  split, quarantine, the day a liability counts against, the writer recovering
  from its own defect), and the mode switch at every call site (HTTP surfaces,
  grants, playground, batch), including that a request the upstream failed
  gives its reservation back.
- `dbtest`: two connections/processes contending on the same and on
  independent scopes under the partial-headroom policy; settlement racing
  admission with deterministic interleavings (including the naive two-read
  hazard, shown to lose a cost, and the protocol, shown not to); lock wait
  before read; replay, changed payload, overlap, unknown commit, refused
  entry, crash with stranded reservations, erasure; the migration and the
  no-runtime-DDL guard. Two gateways through the whole request path (the
  key's gate, then the admission) on one database: what one answers a caller
  the other answers too, after upstream failures and across midnight with a
  quarantined liability.
- Sections 6b and 6c. Unit: the numbers a ledger reports, each alert (once a
  stall, at most once a minute for an unavailable admission, not at once for
  an owner that missed a heartbeat), closure only with everything sent, the
  upkeep's schedule and what it keeps. `dbtest`: a killed process's
  reservations charged from request-log rows written through the real logger
  and log store, or given back, each with its audit row; an instance the
  registry hears from refused, by the command and by the statement; a process
  wrongly declared gone settling afterwards without a second charge; own and
  closed-process receipt deletion, including a batch that committed, lost its
  answer and is sent again after an upkeep; closures deleted only when
  nothing is left under them. Each safeguard was checked by removing it and
  seeing a test fail.
- After three reviews of `9847718`. `dbtest`: a settlement that commits
  between an admission's read and its decision is still counted (the request
  that cost the whole limit no longer lets the next one in); an admission
  stalled between two statements is ended by the server and another process
  admits the account; a charge the counters refuse does not keep the rest of
  a reconciliation from being retired; the command refuses a fence secret the
  database was not pinned to; a duplicate's dropped cost is logged. Unit: a
  defect while building a batch loses nothing it had taken; a failed
  connection is retried, a cancelled statement is not; no closure with a
  request still running; an upkeep that stopped short comes again within a
  minute. Each again checked by removal.
- After a check of those fixes. `dbtest`: a request whose cost the counters
  refused stays counted while its row is being marked (it cost the whole
  limit, and the next request is refused throughout); a mark that failed, or
  whose answer was lost, is sent again and nothing is counted twice, in the
  process that holds it or in another. Unit: what the process holds until the
  mark is answered, and what it does with each answer.
- A local fake-upstream exercise with two gateway processes on a task-owned
  database: real admission, response/cancel/tail settlement, cross-instance
  visibility. A narrow latency benchmark, local vs shared, on a task-owned
  disk-backed PostgreSQL, with its settings recorded.
