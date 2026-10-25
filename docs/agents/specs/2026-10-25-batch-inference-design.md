# Batch Inference (`/v1/batches`)

**Date:** 2026-10-25
**Status:** Accepted; implemented with this record
**Related:** #1503 (a general batch endpoint), #1510 (the first draft and its
spec, `2026-10-10-batch-processing-design.md`, which this record replaces; it was
not merged), #1509 (completion webhooks, deferred)

## 1. Goal

Research and evaluation workloads send thousands of short chat requests. Fanning
them out from the client works, but it holds many connections, runs into the
per-user concurrency limit and gives the gateway no say in when the work runs.
A batch surface lets a caller hand the gateway the whole job at once, poll it,
and collect the results later, while the gateway runs it at a pace each model can
afford beside its interactive traffic.

The surface is OpenAI's Batch API in shape (batch objects, statuses, result
lines), with the requests sent inline instead of through a Files API.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | API shape | `POST /v1/batches` takes an inline `requests` array, each entry shaped like a line of an OpenAI batch input file (`custom_id`, `method`, `url`, `body`). Responses are OpenAI batch objects. No Files API. |
| D2 | Targets | `/v1/chat/completions` only, non-streaming. Mixed models in one batch are allowed. Auto models (`auto`, `auto-<tier>`) are refused at create. |
| D3 | Who may create | **Internal users and above by default.** `BATCH_MIN_ROLE` (free / pro / internal / admin) changes it at runtime; `BATCH_ENABLED` turns the whole surface off (every route 404) and stops the worker from claiming. |
| D4 | Who may read | The owner, or an administrator. Reading, cancelling and deleting one's own batches are not role-gated, so an owner whose role was lowered can still collect and erase their data. |
| D5 | Execution | An in-process worker in every gateway process runs each request through the ordinary `chat_completions` handler (routing, fallback, pricing, logging and quota all apply), not through a self-HTTP call. |
| D6 | Concurrency | A **per-model cap, deployment-wide**: at most N batch requests of a model run at once across all processes. `BATCH_MODEL_CONCURRENCY` (default 4) is the cap of a model without an override; an administrator sets per-model overrides from the admin console, live. `0` pauses batch work on that model. |
| D7 | Interactive traffic | While `BATCH_YIELD_TO_INTERACTIVE` is on (the default), interactive requests in flight on a model take batch slots from its cap. |
| D8 | Identity | Every attempt re-authenticates the **owner's stored key** first: revocation, suspension, deletion, the current role, `disabled_models` and the daily cost quota all apply to work queued before they changed. |
| D9 | Accounting | Batch requests share the owner's quota and per-user concurrency slots, with no separate budget and no discount. `api_logs.batch_job_id` attributes each request to its batch; each item records its cost and the batch keeps the total. |
| D10 | Results | Paged (`GET /v1/batches/{id}/results`), never inline on the batch object. |
| D11 | Bounds | Body size, requests per batch, unfinished batches per user, a completion window, and a retention period, all runtime settings. |
| D12 | No database | No batch store: every route answers 404 and no worker starts. The same holds while a lock-blocked batch schema migration waits to land. |

## 3. Architecture

```text
client
  POST /v1/batches {requests: [...], metadata}
    -> role gate, body read under the size cap, every request validated
    -> batch_jobs row + batch_items rows, one transaction        (status validating)
    -> wake this process's worker
  GET /v1/batches/{id}, GET /v1/batches/{id}/results, cancel, delete

worker (one per gateway process)
  loop: for each model with claimable items and room under its cap
    claim_items(model)          advisory lock per model; deployment-wide cap
    per claimed item, in its own task:
      authenticate_stored_key   -> fail batch / pause owner / release
      user concurrency slot     -> release and retry shortly when taken
      dispatch_item             -> chat_completions(...) in-process
      complete / retry with backoff / fail
  heartbeat leases of running items; sweep expiry and retention every minute
```

| File | Role |
|---|---|
| `serving/servers/routers/batches.py` | The six HTTP routes: admission, validation, ownership, the response shapes |
| `serving/batch/schemas.py` | Request and response bodies |
| `serving/batch/config.py` | `batch_config()` (every setting, read live) and `may_use_batch()` |
| `serving/batch/model_limits.py` | `BatchModelLimits`: the per-model overrides, cached for about ten seconds |
| `serving/batch/scheduler.py` | `BatchScheduler`: the worker loop, claiming, leases, retries, cooldown |
| `serving/batch/dispatch.py` | `dispatch_item`: one item through `chat_completions`, in a fresh request scope |
| `serving/batch/interactive_load.py` | `INTERACTIVE_LOAD`: interactive requests in flight per model, in this process |
| `serving/storage/batch_store.py` | `BatchStore`: tables, claiming SQL, guarded status transitions, sweep |
| `serving/servers/routers/admin/batch.py` | Admin API for the per-model caps |
| `serving/servers/auth.py` | `authenticate_stored_key`: a stored key to a user context, shaped like `verify_api_key`'s |
| `serving/servers/concurrency.py` | `user_slot_for`: the per-user slot decision `enforce_user_concurrency` makes, reusable |
| `serving/servers/bootstrap.py`, `serving/servers/app.py` | Wiring: the store and caps beside the Responses store; the worker starts from the app lifespan and stops first at shutdown |

## 4. Data model

The store is standalone, like the Responses store: it creates its own tables at
startup, idempotently. A migration that cannot get its lock (another process
booting at the same moment, a long reader on a table it indexes) does not fail
startup: it is retried in the background as the other schema passes are, and
until it lands the services carry no batch store, so batch inference is absent
(404) rather than served from a half-built schema. The hard-delete purge still
reaches the batches stored before that boot through the pending store
(`batch_store_pending`); where the tables do not exist yet there is nothing to
purge.

### `batch_jobs`

One row per batch: `id` (`batch_<hex>`), `user_id`, `key_hash` (the HMAC of the
creating API key, the value `api_keys.key_hash` holds; NULL on a gateway with
user auth disabled), `endpoint`, `status`, `model_ids` (canonical ids),
`request_count`, `completed_count`, `failed_count`, `metadata`, `error`,
`cost_usd`, one timestamp per status (`created_at`, `in_progress_at`,
`completed_at`, `failed_at`, `expired_at`, `cancelling_at`, `cancelled_at`),
`expires_at`, `paused_until`, and `last_claimed_at` for fair claiming.

`key_hash` and `user_id` never leave the gateway: the router maps rows to the API
object field by field.

### `batch_items`

One row per request, ids in submission order: `batch_id` (cascade delete),
`custom_id` (unique within the batch), `model_id` (the canonical id of the model
the body names), `request` (the caller's body exactly as sent), `status`,
`attempts`, `next_attempt_at`, `claimed_by`, `lease_expires_at`, `response`,
`error`, `status_code`, `prompt_tokens`, `completion_tokens`, `cost_usd`,
`request_id`, `started_at`, `completed_at`.

The stored item keeps the caller's body untouched, so retrieval retains the
alias the caller sent. Dispatch copies that body and replaces `model` with the
stored canonical `model_id`; an alias remapped after creation therefore cannot
run model B while consuming model A's cap. The caps, interactive-load count and
claim all key on that same canonical id.

### `batch_model_limits`

`model_id` (canonical), `max_concurrency` (>= 0), `updated_by`, `updated_at`. A
model without a row takes `BATCH_MODEL_CONCURRENCY`.

### `api_logs.batch_job_id`

A nullable column filled from `metadata["batch_job_id"]` by both insert paths, as
`agent_job_id` is. Unlike `agent_job_id` it has no index: no request path reads
`api_logs` by batch (the batch keeps its own counts and cost), and an index built
by the startup schema pass holds a `SHARE` lock that blocks every log write for a
full scan of the table. An operator who wants one builds it `CONCURRENTLY`.

## 5. Statuses

Batch: `validating` (created, nothing claimed yet) -> `in_progress` (first claim)
-> `completed` (no open items left). Cancelling moves it to `cancelling`, then
`cancelled` once its running items finish. `failed` means the owner's key or
account is no longer valid (`error.code = owner_unauthorized`); `expired` means
the completion window closed. Terminal: completed, failed, expired, cancelled.
Active (counted against the per-user limit): validating, in_progress, cancelling.
OpenAI's `finalizing` is never used: results are written as each request ends.

Item: `queued` -> `running` -> `completed` or `failed`, or back to `queued` (a
retry with backoff, or a release that does not consume the attempt). A queued
item becomes `cancelled`, `expired` or `failed` (`batch_failed`) when its batch
settles that way. Open items: queued and running.

Every transition is guarded in SQL: an item write applies only while the item is
`running`, claimed by the writer, and on the same attempt; a batch moves only
from the statuses its transition expects. Each item write "settles" its batch in
the same transaction: queued items follow a cancelling, failed or expired batch,
and a batch with no open items left becomes completed (or cancelled). A cancel
racing a claim therefore cannot be lost: the claim's update re-checks the batch
status, and whichever commits second sees the other.

Error JSON (item `error`, batch `error`): `{"code", "message"}`, plus `"status"`
when an HTTP status caused it. Codes: `owner_unauthorized`, `batch_failed`,
`batch_expired`, `invalid_request` (400/422), `model_not_found` (404),
`forbidden` (403), `rate_limited` (429), `timeout` (504), `service_unavailable`
(503), `upstream_error` (other 5xx and unexpected exceptions).

## 6. Claiming and leases

`claim_items(model_id, worker_id, limit, max_running, lease_seconds)` runs in one
transaction under `pg_advisory_xact_lock` keyed on the model, so processes claiming
the same model serialize while different models proceed in parallel:

1. `live` = the model's running items whose lease has not expired, across every
   process; `room = min(limit, max_running - live)`. No room, no claim. This is
   what makes the cap deployment-wide.
2. Claimable items are those of a claimable batch (validating or in progress, not
   paused, not expired) that are queued and due (`next_attempt_at` passed or
   unset), or running with an **expired lease** — the items of a worker that died.
3. Items are taken **round-robin across batches**: the batch claimed from longest
   ago first (`last_claimed_at` NULLS FIRST, then `created_at`), in id order
   within it, moving to the next batch until the room is filled. One large batch
   cannot starve a small one submitted after it.
4. Claimed items become `running` with `attempts + 1`, `claimed_by`, a lease and
   `started_at`; touched batches get `last_claimed_at` and leave `validating`.

The worker extends the leases of its running items about every minute (lease
about five minutes). A process that dies stops heartbeating; its leases run out
and any process claims the items again. A process that stops cleanly hands its
unfinished items back at once (`release_item`), without spending their attempt.
An item claimed with its attempts already exhausted is completed as failed with
its last error.

## 7. Scheduling and yielding

One loop per process. It runs when a batch is created in this process
(`notify()`), when one of its items finishes, and otherwise every couple of
seconds while any batch has work (less often while none has), so a batch drains
continuously at its models' pace rather than in timed slices.

For every model with claimable items (`active_models()`) that is not cooling
down, the round computes

```text
cap   = limits.limit_for(model, BATCH_MODEL_CONCURRENCY)
cap  -= INTERACTIVE_LOAD.in_flight(model)        while BATCH_YIELD_TO_INTERACTIVE
claim limit = cap - this process's in-flight items of the model, max_running = cap
```

so a busy model never holds up an idle one, and an administrator's change to a
cap applies within the cache's ten seconds in every process (at once in the
process that made it). Interactive requests are counted by
`enforce_user_concurrency` for every inference request while its slot is held.
The yield is per process; the cap is deployment-wide.

Outcomes of an attempt:

- **Success**, or a **caller error** (a 4xx other than 408, 425 and 429): the item
  is completed (`completed` or `failed`) and not retried. Caller errors do not
  count against the model.
- **Retryable** (408, 425, 429, 5xx, timeouts, unexpected exceptions): back to the
  queue with exponential backoff and jitter, a bounded number of attempts (eight),
  then failed with the last error. Consecutive retryable failures on one model
  cool it down for a few minutes, during which this process claims nothing for it.

## 8. Re-authentication

An item carries no credential of its own; before every attempt the worker
resolves the owner from the stored `key_hash` through
`authenticate_stored_key`, which builds the same user context `verify_api_key`
returns (role, `disabled_models`, per-user concurrency override, quota) without
the last-used write or the rejection logging. Then:

| Outcome | Effect |
|---|---|
| Key revoked or expired, account suspended, deleted or unverified, or user auth turned on for a batch created anonymously | Item released; the batch fails with `owner_unauthorized`, its queued items with `batch_failed` |
| Daily cost quota spent | Item released; the owner's batches pause until the quota renews (next UTC midnight) |
| Role below `BATCH_MIN_ROLE` | Item released; the owner's batches pause for about five minutes, then the role is checked again |
| Store error | Item released and retried shortly |

The item then takes the owner's per-user concurrency slot with
`user_slot_for`, the decision `enforce_user_concurrency` makes (exempt models,
per-user overrides); when every slot is taken it is released and retried a few
seconds later. Dispatch publishes the owner's role for the provider key pool,
sets `batch_job_id` on the request's log metadata, and forces `stream` off.

## 9. Limits

| Bound | Setting | Default | Where |
|---|---|---|---|
| Create body size | `BATCH_MAX_BODY_MB` | 32 MiB | Router: a declared `Content-Length` over it is refused before reading, otherwise the stream is counted as it arrives (413) |
| Requests per batch | `BATCH_MAX_REQUESTS` | 50,000 | Router (400) |
| Unfinished batches per user | `BATCH_MAX_ACTIVE_PER_USER` | 10 | Router, before the body is read and again before the insert (429) |
| Completion window | `BATCH_COMPLETION_WINDOW_HOURS` | 24 (1–168) | `expires_at` at create; the sweep expires what has not started |
| Retention | `BATCH_RETENTION_DAYS` | 7 | The sweep deletes batches that finished longer ago |
| `custom_id` | — | 1–512 characters (it is indexed), no NUL character, unique in the batch | Schema, router; the store's own refusal also answers 400 |
| `metadata` | — | 16 pairs; keys 64, values 512 characters (OpenAI's) | Schema |

## 10. API

When the surface is available, all routes authenticate with `verify_api_key`.
The availability gate precedes authentication and request validation: without a
batch store, or with `BATCH_ENABLED` off, every route answers 404.
`X-On-Behalf-Of` is refused with 400 on every available route: a batch runs later
under the key that created it, so a service key acting for someone else would
store a batch owned by one identity and authorised by another.

| Route | Behaviour |
|---|---|
| `POST /v1/batches` | 403 below `BATCH_MIN_ROLE`; 413 over the body cap; 400 for invalid JSON (`NaN` and `Infinity` included), an invalid envelope, too many requests, a duplicate `custom_id`, a `url` other than `/v1/chat/completions`, a `method` other than `POST`, a body that is not a valid chat request, `stream: true`, an unknown model or one the caller may not use (worded exactly as an unknown model), an auto model; 429 at the active-batch limit; 401 with the ordinary invalid-or-expired-key response if the authenticated owner becomes inactive, disappears or enters hard deletion before the insert locks it. A request's error names it: `requests[3] (custom_id 'q-4'): ...`. Returns the batch object. |
| `GET /v1/batches?after=&limit=` | The caller's own batches, newest first; `limit` 1–100 (20) |
| `GET /v1/batches/{id}` | The batch object; owner or admin, otherwise 404 |
| `POST /v1/batches/{id}/cancel` | Queued requests cancelled, running ones finish; a finished batch is returned unchanged |
| `DELETE /v1/batches/{id}` | The batch with its requests and results; `{"id", "object": "batch.deleted", "deleted": true}`. Status does not matter, but an unexpired running-item lease answers 409 with instructions to cancel and wait; expired leases do not block deletion. |
| `GET /v1/batches/{id}/results?after=&limit=` | Requests in submission order with their outcomes; `limit` 1–1000 (100); `after` is a result id (`batch_req_<n>`) |

The batch object has OpenAI's fields (`id`, `object: "batch"`, `endpoint`,
`errors: null`, `input_file_id: null`, `completion_window` (`"24h"`, from the
batch's own timestamps), `status`, `output_file_id: null`, `error_file_id: null`,
the timestamps, `finalizing_at: null`, `request_counts {total, completed,
failed}`, `metadata`), plus two of ours: `cost_usd` and `error`.

A result is `{"id": "batch_req_<n>", "custom_id", "status", "response":
{"status_code", "request_id", "body"} | null, "error": {...} | null}`; a page is
`{"object": "list", "data", "has_more", "first_id", "last_id"}`.

## 11. Configuration

Every setting is database-backed (Admin -> Configuration, category **Batch inference**),
read on each request and each worker round, so a change needs no restart.

| Setting | Default | Meaning |
|---|---|---|
| `BATCH_ENABLED` | on | The surface and the worker. Off: 404 everywhere, nothing claimed, stored batches kept |
| `BATCH_MIN_ROLE` | `internal` | Lowest role that may create batches |
| `BATCH_MODEL_CONCURRENCY` | 4 | Cap of a model without an override; 0 pauses them |
| `BATCH_YIELD_TO_INTERACTIVE` | on | Interactive requests in flight take batch slots |
| `BATCH_MAX_REQUESTS` | 50000 | Requests per batch |
| `BATCH_MAX_BODY_MB` | 32 | Create body size |
| `BATCH_MAX_ACTIVE_PER_USER` | 10 | Unfinished batches per user |
| `BATCH_COMPLETION_WINDOW_HOURS` | 24 | Time a batch has before unstarted requests expire |
| `BATCH_RETENTION_DAYS` | 7 | Days a finished batch is kept |

Per-model caps: `GET /admin/batch/models` lists the canonical published chat
models (and any with an override or open items) with their override, effective
cap, running and queued counts; `PATCH /admin/batch/models/{model_id}` sets
(0–10000) or clears (`null`) an override, audited as `batch.models.update`. The
admin Settings page has a **Batch Concurrency** section over the same API.

A permanent user deletion purges the user's batches with their requests and
results, beside the Responses purge.

## 12. What changed from the PR #1510 draft, and why

| Draft | Now | Why |
|---|---|---|
| `request.json()` on the whole body | Streamed read under `BATCH_MAX_BODY_MB`, `Content-Length` checked first; `BATCH_MAX_REQUESTS` | Review: an unbounded body is a memory exhaustion vector |
| Per-item results inline on every `GET` | Batch object only; `/results` pages of up to 1000 | Review: a 50,000-item batch made every poll a huge response |
| No limit on batches per user | `BATCH_MAX_ACTIVE_PER_USER`, 429 | Review: unbounded queued work per user |
| Only `stream` and `model` checked at create | Every body validated as a chat request; model resolved against the route table and the caller's access; auto models refused; duplicates, `url` and `method` checked | Review: a bad item should fail the create, not fail later in the worker |
| Ownership was the only gate | `BATCH_MIN_ROLE` (internal by default) on create; `BATCH_ENABLED`; the owner re-authenticated before every attempt | Review and maintainer: batch is for internal users unless an operator opens it, and queued work must not outlive revoked access |
| Load gate: fresh-token rate vs an hourly baseline, 15-minute slices, then a 10-minute cooldown tick | Per-model concurrency caps, deployment-wide and editable live, minus interactive load; continuous draining | Maintainer: admins adjust per-model concurrency on the fly. The gate scanned `api_logs` every tick, made progress in coarse slices, and could not be tuned per model |
| `asyncio.Semaphore(4)` per process | Cap enforced in the claim SQL across processes | A per-process limit multiplies with the process count |
| One round per tick over whole batches | Per-model claiming, round-robin across batches | An idle model starved behind a busy one; a large batch starved later ones |
| No leases; a crashed worker's items were lost or ran twice | Leases, heartbeat, guarded writes, release on clean stop | Multi-process deployments and restarts |
| Own per-user concurrency acquisition; caller role not published | `user_slot_for` (exempt models, overrides); role published for the key pool | Review: diverged from the interactive path |
| Every failure retried and counted | 4xx not retried and not counted against the model | Review: a caller error is not an outage |
| Kept until `DELETE`; hard delete left batches | `BATCH_RETENTION_DAYS` sweep; hard delete purges | Bounded storage; erasure |
| Status vocabulary and `sessions` left open | OpenAI's statuses (no `finalizing`); `sessions` dropped | — |
| `api_logs.batch_job_id` proposed | Implemented in both insert paths; per-item and per-batch cost recorded | Attribution and cost reporting |

## 13. Deferred

- Embeddings and Responses as batch targets.
- A Files API: JSONL upload, output and error files (`output_file_id`).
- Completion webhooks (#1509).
- A batch price discount.
- Auto models in batches.
- An atomic per-user active-batch limit: it is checked twice around the insert,
  not inside the insert's transaction, so concurrent creates can pass it together.
- Fairness across users rather than batches: a user with several batches gets
  a share per batch.
- Reading one's batches with the daily quota spent: `verify_api_key` refuses
  every request then, reads included, until the quota renews.
- Yielding to interactive load in other processes: the in-flight count is this
  process's.
- An administrator listing of every user's batches.
- Token totals on the batch object.
