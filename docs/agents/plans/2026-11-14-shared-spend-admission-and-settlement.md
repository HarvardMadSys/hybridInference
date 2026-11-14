# Shared spend admission and batched settlement (phase 2a)

Status: implementation contract, 2026-11-14. Base: `dev` at `b98678a42`.
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
it), hears every billing point's report (`observe_billing`: a cost, zero for a
free model, or None when it could not price the request) and the existing
"left the gateway" signal (`dispatch_watch.report_sent`, which the existing
cancellation billing already relies on). When the request ends, its slot is
released at once (in memory) and the reservation is queued as one settlement
entry. HTTP, grant and playground requests are classified at teardown
(`SharedReservation.finish`):

| What happened | Entry |
|---|---|
| a billing point could not price it | `unresolved` |
| billing points priced it (zero included), or costs were recorded | `settle` (those costs) |
| no upstream call left the gateway | `settle` with no cost (proven not dispatched) |
| sent, ended normally, billed by nobody | `unresolved` |
| sent, then failed with an error the policy does not bill (4xx, 429, 503...) | `settle` with no cost |
| sent, then cancelled, timed out, or 500/502/504 | `unresolved` |

The pricing policy is unchanged (what is charged is what the billing points
record; the cancellation billing still prices a cut-off call), and an estimate
is never charged: an unresolved entry keeps the estimate as a liability, with
any known part charged. Batch attempts keep the dispatcher's own
`cost_status` (`unknown` → `unresolved`). Costs recorded with no open
reservation (no check, or after the reservation was queued: late costs) are
plain increments.

Each batch has a random `batch_id`, frozen membership and a payload digest.
A batch that lists a reservation twice, or carries a member cost attributed
to another account, key or project than the member, is refused whole before
anything is written (`invalid`). Otherwise one transaction:

1. `INSERT INTO spend_settlements (batch_id, digest, ...)`; on conflict the
   stored digest decides: same, the batch already committed (its stored
   result is returned); different, nothing is written (`conflict`).
2. Fence locks (shared) as today.
3. Retire the members this instance holds *for the same account, key and
   project*: `DELETE ... USING unnest(...)` for settle/release, `UPDATE
   state='unresolved'` for unresolved. A member not retired is `duplicate`
   (another batch retired it), `mismatch` (held, but not as the member says),
   `erased` or `missing`; none of them is charged.
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

In memory per reservation: active (executing) → completed, pending settlement
→ writing → settled/released (confirmed), or unknown (replaying), or
quarantined. In the database: `active`, `unresolved`, `quarantined`; a
settled or released reservation's row is gone and its id is in
`spend_settlement_members`. Releasing an execution slot is not retiring a
reservation; the row count is never used as concurrency, and no distributed
concurrency policy is added.

An `active` row counts against the current day and month. `unresolved` and
`quarantined` rows count against the day (and month) in which they ended,
the day their cost would have been recorded on, and stay until an explicit
resolution. Nothing expires them.

## 6. Recovery, liabilities and what is not done here

- Writer cancelled or failing: the same batch (id and payload) is sent again
  until it gets an answer; on shutdown `flush_all` sends what it can and
  reports the rest (`holdings`, `unwritten`).
- Process crash: its `active` rows stay counted in the current day and month;
  `status`/`list` show them with their owner's registration state.
- A settlement that finds a member not held as recorded (`missing`,
  `mismatch`) adds none of its costs and keeps the member in this process as
  an *unaccounted* liability: counted in this process's checks at the larger
  of its estimate and its known cost, reported by `holdings`/`unwritten`,
  logged and alerted. An `unresolved` or `quarantined` member stays in the
  database (and in `holdings`).
- Nothing expires: no TTL, heartbeat or retention retires a reservation,
  forgives a liability or deletes a settlement receipt (`spend_settlements`,
  `spend_settlement_members` grow with traffic; a retention rule needs a
  replay horizon that a later review would have to establish).
- No reconciliation command is shipped. Retiring an unresolved, quarantined
  or stranded reservation needs explicit evidence of its actual charge and a
  separately reviewed tool; an estimate is never charged as if it were the
  cost.
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

## 7. Verification plan

- Unit: the shared ledger's state machine with a fake store (admit retry and
  ambiguity, cancellation, settle/release/unresolved, late cost, unknown
  replay, refusal split, quarantine), and the mode switch at every call site
  (HTTP surfaces, grants, playground, batch).
- `dbtest`: two connections/processes contending on the same and on
  independent scopes under the partial-headroom policy; settlement racing
  admission with deterministic interleavings (including the naive two-read
  hazard, shown to lose a cost, and the protocol, shown not to); lock wait
  before read; replay, changed payload, overlap, unknown commit, refused
  entry, crash with stranded reservations, erasure; the migration and the
  no-runtime-DDL guard.
- A local fake-upstream exercise with two gateway processes on a task-owned
  database: real admission, response/cancel/tail settlement, cross-instance
  visibility. A narrow latency benchmark, local vs shared, on a task-owned
  disk-backed PostgreSQL, with its settings recorded.
