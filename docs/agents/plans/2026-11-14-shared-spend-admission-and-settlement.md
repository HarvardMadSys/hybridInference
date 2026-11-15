# Shared spend admission and batched settlement (phase 2a)

Status: implementation contract, 2026-11-14; revised 2026-11-15 after review
(what a finished request holds, section 4; the day a liability counts against,
section 5; what activation still needs, section 6b). Base: `dev` at `b98678a42`.
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
  created by migration `0018_shared_spend` with `local`. `local` is today's
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
  instance and state, and counter writes made outside the shared settlement.
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
bound), which also bounds pool acquisition, `lock_timeout` and
`statement_timeout`. Overload or an unavailable database raises the existing
`DatabaseBusyError` (503 with `Retry-After`); batch work is handed back for a
later attempt. A lost COMMIT answer is retried with the same id within the
deadline. If the outcome is still unknown, or the admitting coroutine is
cancelled after the id was used, the request is not dispatched and a
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
the accounting that records them.

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
- Process crash: its `active` rows stay counted in the current day and month;
  `status`/`list` show them with their owner's registration state.
- Process crash, continued: such rows count against every later day and month
  as well, for as long as they stay (an active row counts against the current
  one). `status` adds them up under `stranded` -- the active reservations whose
  owner the registry no longer hears from -- with their oldest age; the
  registry cannot prove a process gone, so that is where an operator looks, not
  a verdict. Retiring them is section 6b's.
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
- Nothing expires: no TTL, heartbeat or retention retires a reservation,
  forgives a liability or deletes a settlement receipt (`spend_settlements`,
  `spend_settlement_members` grow with traffic; a retention rule needs a
  replay horizon that a later review would have to establish).
- No reconciliation command is shipped. Retiring a quarantined or stranded
  reservation needs explicit evidence of its actual charge and a separately
  reviewed tool; an estimate is never charged as if it were the cost.
- Hard delete: the account's reservations are purged in the hard-delete
  transaction; admission checks the tombstone under the fence lock, so no row
  is written for an erased account; a settlement for one finds its rows gone
  and, as today, writes no counter.

## 6a. The accounting guarantee, precisely

Shared atomic admission prevents two admissions, in any processes, from being
granted the same estimated headroom. It does not bound actual cost: the
existing policy admits while $0.01 fits and reserves an *estimate* clamped to
what is left, and a request may cost more than its estimate. Within the
admitting process, the known costs of a running request that exceed its
estimate (several charges included) are added to every later check at once;
other processes see the actual cost when the settlement commits. Late costs
(after the request ended) are plain increments of the next batch.

A request that has ended keeps its estimate in the database until its
settlement commits, normally milliseconds later. An admission made in that
interval counts the estimate rather than the (usually smaller) cost, so a
caller within one estimate of a limit can be refused then and admitted a moment
later: conservative, never the reverse.

## 6b. Required before the shared mode is activated (not in this change)

Merging this change activates nothing. Before any deployment switches the row:

1. The offline transition and its rollback (section 2).
2. **Retiring what a dead process left.** A killed gateway's `active` rows
   lower their accounts' headroom on every later day and month until someone
   removes them by hand. This needs an operator path with evidence and an
   audit record, and two decisions it depends on: what evidence is enough (the
   reservation row does not yet reference the request's `api_logs` row, which
   is where its actual charge would be found), and whether anything may be
   released on the registry's word that the owner is gone.
3. **A retention rule for the receipts** (`spend_settlements`,
   `spend_settlement_members`: about 0.25-0.8 kB per request, never deleted)
   that keeps the replay guarantee. A batch is only ever sent again by the
   process that made it, so the rule has to be stated in terms of that
   process being gone, not of age alone.
4. **Monitoring.** There is none yet: admission latency and its refusals for
   an unavailable database, the amount and age of what is held (queued,
   stranded, quarantined), and settlement failures.
5. Sizing the request pool for the admission's round trips, measured on the
   deployment's own database (section 7's benchmark is a loopback exercise).

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
- A local fake-upstream exercise with two gateway processes on a task-owned
  database: real admission, response/cancel/tail settlement, cross-instance
  visibility. A narrow latency benchmark, local vs shared, on a task-owned
  disk-backed PostgreSQL, with its settings recorded.
