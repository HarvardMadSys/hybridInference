# General Batch Processing Endpoint — Design

**Date:** 2026-10-10
**Status:** Proposed
**Related:** #1503 (add a general batch processing endpoint); follows the
delegation pattern of `2026-06-23-openai-responses-api-design.md`
(`responses.py` → `chat_completions`).

## 1. Goal

Add a first-class batch surface to the gateway: submit many inference requests
in one call, get an id back, and retrieve per-request results later — instead of
forcing clients to fan out their own concurrency. Results are returned in
OpenAI-shaped batch objects.

## 2. Why

Research and eval workloads issue thousands of short calls. Client-side fan-out
works but is heavy on connections and rate limits and offers no place for the
gateway to defer work to idle capacity. A batch surface lets the gateway (a)
accept the work once, (b) schedule it against real load, and (c) return results
in a well-known shape.

## 3. Locked decisions

| # | Decision | Choice |
|---|---|---|
| D1 | API shape | **Hybrid** — accept an inline `requests[]` array in the create call; return OpenAI-shaped batch objects. No Files API. |
| D2 | Execution | **In-process** call to the existing `chat_completions` handler (precedent: `responses.py:51`, `compat.py`), not self-HTTP. |
| D3 | Worker runtime | **In-process asyncio task** started in the bootstrap lifespan. Not a separate service, not APScheduler. |
| D4 | Load gate metric | **Fresh-token rate** = `prompt_tokens - cache_read_tokens` from `api_logs`, over a recent window vs a baseline. |
| D5 | Quota | Batch **shares** the normal per-user quota/concurrency. No separate batch quota. |
| D6 | Model scope | **Mixed models** allowed within one batch; the scheduler groups items by model. |
| D7 | DELETE | `DELETE /v1/batches/{id}` **purges** the batch and all its items from our stores so nothing further is processed. |
| D8 | First deliverable | This design spec, reviewed before code. |
| D9 | Gate scope | **Per model.** Batch is a scavenger: any non-batch traffic on the model makes it yield fully. |
| D10 | Headroom | Batch may consume up to **80%** of the model's normal (interactive baseline) load. |
| D11 | Processing slice | **15 min** of processing per grant, then a **one-tick (~10 min) cooldown** before the next slice. |
| D12 | Bounds | Cap batch **size** and **duration** (items expire 24h after creation). |
| D13 | Attribution | Add `api_logs.batch_job_id` (mirrors `agent_job_id`). |
| D14 | No DB | Batch surface returns **404**, worker does not start. |

## 4. Architecture

```
client
  → POST /v1/batches            { requests: [ {custom_id, method, url, body}, ... ], metadata? }
    → validate, mint batch_id, persist batch_jobs + batch_items (Postgres)
    → return OpenAI-shaped batch object (status: validating)

  (in-process asyncio worker task)
  → tick (periodic) or event (model availability flips up)
    → select pending batches whose models are available
    → check fresh-token load gate for the target endpoint(s)
      → below baseline for the sustain window: allow
      → otherwise: skip this tick (subject to starvation escape, OPEN)
    → for each batch: dispatch items via asyncio.Semaphore(4)
        → build synthetic Request with the item body
        → call chat_completions(request, ...) in-process   # routing/fallback/cost/logging/quota
        → persist item response (or error) to batch_items
    → when all items terminal: mark batch terminal, (webhooks — separate issue)

  → GET /v1/batches/{id}   → OpenAI-shaped object (+ per-item results)
  → GET /v1/batches        → list (OPEN)
  → DELETE /v1/batches/{id}→ purge batch + items
```

The worker reuses the same execution path interactive requests use, so routing,
fallback, circuit breaking, cost accounting, DB logging and per-user concurrency
all apply unchanged.

## 5. Components

| File | Role |
|---|---|
| `serving/servers/routers/batches.py` | Northbound router: create / get / list / delete. Auth, validation, persistence, ownership. |
| `serving/batch_scheduler.py` | In-process asyncio worker: tick + event triggers, availability check, load gate, backoff, dispatch at width 4. |
| `serving/batch_dispatch.py` | Builds a synthetic Request for a `batch_items` row and delegates to `chat_completions`; captures the result/error. |
| `serving/storage/batch_store.py` | Standalone Postgres store (`batch_jobs`, `batch_items`), separate from the `OperationalStore`/`LogStore` ABCs — same shape as `responses_store.py`. |
| `servers/deps.py`, `servers/bootstrap.py`, `servers/app.py` | Wiring: `batch_store` on `AppServices`, `get_batch_store` dependency, table init, router include, worker task start/stop. |

Absent when no DB is configured: batch routes return 404 (Responses-style
graceful degradation) and the worker does not start.

## 6. Data model (proposed)

### `batch_jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | e.g. `batch_<ulid>` |
| `user_id` | TEXT NOT NULL | owner; the "acc_id" — **OPEN: confirm meaning.** |
| `status` | TEXT NOT NULL | **OPEN: vocabulary.** |
| `endpoint` | TEXT | target surface, e.g. `/v1/chat/completions` |
| `models` | JSONB | distinct models in the batch (mixed allowed per D6) |
| `request_count` | INTEGER | |
| `completed_count` / `failed_count` | INTEGER | |
| `metadata` | JSONB | passthrough |
| `callback_url` | TEXT | webhooks — separate issue |
| `created_at` / `started_at` / `completed_at` / `expires_at` | TIMESTAMPTZ | |
| `error` | JSONB | batch-level failure |

### `batch_items`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | internal |
| `batch_id` | TEXT FK | |
| `custom_id` | TEXT | client-supplied, unique within batch |
| `endpoint` | TEXT | |
| `model_id` | TEXT | |
| `request` | JSONB | the item body |
| `response` | JSONB NULL | **the "response" field the caller asked for; empty until run** |
| `error` | JSONB NULL | per-item failure |
| `status` | TEXT | |
| `ttft_ms` / `prompt_tokens` / `completion_tokens` / `cost_usd` | | accounting |
| `created_at` / `completed_at` | TIMESTAMPTZ | |

**OPEN:** `sessions` (the caller's spec listed a `sessions?` field) — meaning
undecided: item grouping? cache-affinity `session_id`? results layout (per-item
rows vs one output blob)? retention/cleanup policy? per-user row cap?

## 7. Load gate and per-model lease (D4)

Fresh tokens = `prompt_tokens - cache_read_tokens` (a request count would
misread load: one cold 700k prompt costs a replica for minutes, a cache hit
costs milliseconds). The gate is **per model** and batch is a *scavenger* of
spare capacity, never a first-class consumer.

Two sides of the comparison, deliberately asymmetric:

- **Baseline** = interactive-only fresh tokens/hour for this model at this hour
  of day (batch rows excluded), so a batch cannot inflate its own bar.
- **Recent** is split: non-batch recent usage decides whether *someone else* is
  on the model; batch recent usage is capped against the baseline.

Per-model lease, evaluated each tick:

- **Others present** (any non-batch traffic on the model in the window) -> the
  batch yields fully; no new items launch.
- **No others** -> a slice of **15 min** opens; the batch runs at up to **80%**
  of the model's normal load.
- **Slice ends** -> a cooldown of **one tick** (~10 min) releases the model
  before the next slice, so a real user can take it and the decision is
  re-evaluated.

This bounds how long a batch can occupy a model and guarantees interactive
callers win the model the moment they appear. A zero baseline (no history) is
treated as idle with no cap.

## 8. Availability mapping

v1 default: optimistic in-process availability. A model is available until it
fails a few times in a row, then cools down; a success clears the streak. Each
item attempt is the live signal, and the tick is the coarse fallback.

**OPEN:** the "N recent gateway successes -> up" mapping from the original ask
(rolling window vs consecutive-success streak; DB-backed vs in-memory) is not
implemented; the optimistic-plus-cooldown heuristic stands in for it.

## 9. Dispatch & attribution

- Synthetic `Request` per item, then `chat_completions(request, ...)` in-process
  (D2). The worker acquires the user's concurrency slot itself (D5).
- `api_logs.batch_job_id` is added for attribution, mirroring `agent_job_id`;
  it also lets the gate exclude batch traffic from the interactive baseline.
- Batch items are non-streaming (OpenAI rejects `stream=true` in batches), so
  `ttft_ms` is not available for batch items.

## 10. Backoff & cold start

- Exponential backoff with jitter: base 2s, cap 60s, up to 5 attempts per item;
  a failed item is marked failed and the batch continues (partial success).
- Cold start / warm-up is handled by the same retry loop: the first item on a
  cold model may fail; backoff retries until it succeeds, then the batch runs
  at width. Repeated failures cool the model down (see §8).

## 11. Concurrency

Run **4 requests at a time per model**, configurable. Batch shares the owner's
normal per-user concurrency (D5) and only pauses at item boundaries -- a running
generation cannot be preempted.

## 12. Webhooks

Completion callbacks are a **separate** issue + PR, filed on
`HarvardMadSys/hybridInference` (#1509), split out to keep this surface
reviewable. See that issue for payload/signing/retry design.

## 13. Non-goals

- A Files API / `.jsonl` upload (D1 uses inline `requests[]`).
- Streaming batch items.
- A separate batch quota (D5).
- Embeddings / Responses targets (v1 is chat-completions only).
- A 50% batch discount (v1 has no discount).

## 14. Remaining open decisions

- The "N recent gateway successes" availability mapping (§8).
- Embeddings / Responses as batch targets.
- Pricing discount and a configurable completion window (v1 caps at 24h).
- Deployment process count (determines whether the worker needs a
  `pg_try_advisory_lock` guard -- see `provider_stats_rollup.py:182`).
- Retention (v1 keeps batches until DELETE).

