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

## 7. Load gate (D4)

Fresh tokens = `prompt_tokens - cache_read_tokens` over a recent window, read
from `api_logs`. Batch runs only while the recent rate sits below a baseline.

**OPEN:**
- Baseline window: same-hour-of-day vs trailing 24h vs weekly (weekday/weekend split).
- Scope: global fleet / per-model / per-endpoint (note hourly rollups are per
  `(provider, model_id)`, not per endpoint — per-endpoint needs raw `api_logs`).
- Local-only, or remote endpoints too (remote has no observable idle state).
- Hysteresis thresholds + sustain window (avoid flapping).
- Starvation escape when the line is never crossed (run-anyway after max age vs
  expire at a deadline).
- Batch traffic must be **excluded** from the baseline (see §9 attribution).

## 8. Availability mapping

Batch items for model X are released only when X is "up". Source: recent gateway
traffic for X.

**OPEN:** what "200 requests through the gateway" means — rolling window vs
consecutive-success streak vs success-only; storage (DB table vs in-memory);
whether the existing circuit-breaker / `HealthMonitor` state feeds it (note
`routing/health.py` is advisory-only today and never gates dispatch).

## 9. Dispatch & attribution

- Synthetic `Request` per item, then `chat_completions(request, ...)` in-process
  (D2). The worker acquires the user's concurrency slot itself (D5).
- **OPEN:** add `batch_job_id` to `api_logs` for attribution and so the load
  baseline can exclude batch traffic.
- Batch items are non-streaming (OpenAI rejects `stream=true` in batches), so
  `ttft_ms` is not available for batch items.

## 10. Backoff & cold start

- Exponential backoff with jitter on per-item failure and on model-unavailable.
- Cold start / warm-up: wait for the first successful 200 before streaming the
  batch at width (detect "starting" vs "failed").
- **OPEN:** base/cap/jitter values; warm-up wait length / give-up; per-item
  failure policy (mark failed and continue vs fail batch); retry count.

## 11. Concurrency

Run 4 requests at a time (D5/TBD).

**OPEN:** scope of "4" — per batch, per model, or global across all batches;
configurable; whether batch yields to interactive load and only pauses at item
boundaries (a running generation cannot be preempted).

## 12. Webhooks

Completion callbacks are a **separate** issue + PR, filed on
`HarvardMadSys/hybridInference`, split out to keep this surface reviewable.
See that issue for payload/signing/retry design.

## 13. Non-goals

- A Files API / `.jsonl` upload (D1 uses inline `requests[]`).
- Streaming batch items.
- A separate batch quota (D5).
- **OPEN:** whether to apply a 50% batch discount and honor a `completion_window`
  / expiry.

## 14. Open decisions (summary)

Everything marked **OPEN** above is undecided and is the maintainer's call:
status vocabulary + exact routes + list/cancel routes; `acc_id` and `sessions`
semantics; results layout + retention; availability definition; load-gate
baseline/scope/hysteresis/starvation; backoff + cold-start parameters;
concurrency scope; `batch_job_id` attribution; discount/expiry; deployment
process count (determines whether the worker needs a `pg_try_advisory_lock`
guard — see `provider_stats_rollup.py:182`).
