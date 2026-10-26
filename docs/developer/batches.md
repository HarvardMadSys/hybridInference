# Batch Inference

`/v1/batches` takes many chat-completion requests in one call and answers them
later. You submit the whole job, poll it, then page through the results. The
gateway runs the requests in the background, a few at a time per model, beside
its interactive traffic.

The shapes follow OpenAI's Batch API: batch objects, their statuses and the
result lines are the same. Two things differ. The requests travel inline in the
create call, because the gateway has no Files API, and the results are read a
page at a time rather than downloaded as a file.

```{note}
Only **internal** users and administrators can create batches unless the
operator opens them to other roles. A gateway with batch inference turned off,
or without a database, answers every `/v1/batches` route with `404`.
```

## How it works

Creating a batch validates every request in it and stores them. Nothing runs
during that call: the batch starts as `validating`, and the gateway's worker
starts its requests as soon as their models have room.

Each request then runs exactly as if you had sent it to `/v1/chat/completions`
yourself: the same models, routing and fallback, the same price, logged under
your account. It counts toward your daily cost quota and takes one of your
concurrent-request slots while it runs, so a batch never gets more than your own
traffic could. Requests run in no particular order, and a request that fails for
a reason that may pass — a timeout, a `429`, a `5xx` — is retried with backoff,
up to eight attempts in all. A request the model rejects (any other `4xx`)
fails at once.

Before each request runs, the gateway checks the API key that created the batch
again:

- if the key was revoked or has expired, or the account was suspended or
  deleted, the batch fails;
- if your daily quota is used up, your batches pause until it renews at
  midnight UTC;
- if your role no longer allows batches, they pause until it does.

So keep the key you created a batch with until the batch finishes.

## Create a batch

`POST /v1/batches` with your API key:

```bash
curl -s https://your-gateway.example/v1/batches \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{
    "requests": [
      {
        "custom_id": "q-1",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
          "model": "your-model-id",
          "messages": [{"role": "user", "content": "Summarize the plot of Hamlet."}]
        }
      },
      {
        "custom_id": "q-2",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
          "model": "your-model-id",
          "messages": [{"role": "user", "content": "Translate \"good morning\" into French."}],
          "max_tokens": 50
        }
      }
    ],
    "metadata": {"project": "eval-42"}
  }'
```

| Field | Required | Meaning |
|---|---|---|
| `requests` | yes | The requests, at least one |
| `requests[].custom_id` | yes | Your name for the request, 1–512 characters, unique in the batch. Results carry it back |
| `requests[].method` | no | `POST`, the default |
| `requests[].url` | no | `/v1/chat/completions`, the default and the only endpoint batches serve |
| `requests[].body` | yes | A chat-completion request body, as you would send it to `/v1/chat/completions`. `stream` must not be `true` |
| `metadata` | no | Up to 16 string pairs for your own use; keys up to 64 characters, values up to 512 |

One batch may use several models. Auto models (`auto`, `auto-<tier>`) cannot be
used in a batch; name a specific model.

If that name is an alias, the stored request retains the name you sent, but the
worker dispatches it to the canonical model the alias named when you created the
batch. Changing an alias later cannot move already-submitted work to another
model or another model's concurrency limit.

The whole batch is refused when any request in it is invalid. The error has
the gateway's usual `{"error": {...}}` shape, and its message names the request:

```json
{
  "error": {
    "message": "requests[1] (custom_id 'q-2'): Model 'no-such-model' not found",
    "code": 400
  }
}
```

A model you may not use is reported exactly like one that does not exist.

The answer is the new batch:

```json
{
  "id": "batch_6f1c0a7e2b8d4e55a1c3f9d2e4b6a8c0",
  "object": "batch",
  "endpoint": "/v1/chat/completions",
  "errors": null,
  "input_file_id": null,
  "completion_window": "24h",
  "status": "validating",
  "output_file_id": null,
  "error_file_id": null,
  "created_at": 1792929600,
  "in_progress_at": null,
  "expires_at": 1793016000,
  "finalizing_at": null,
  "completed_at": null,
  "failed_at": null,
  "expired_at": null,
  "cancelling_at": null,
  "cancelled_at": null,
  "request_counts": {"total": 2, "completed": 0, "failed": 0},
  "metadata": {"project": "eval-42"},
  "cost_usd": 0.0,
  "error": null
}
```

The file fields are always `null`. `cost_usd`, what the batch's finished
requests have cost so far, and `error`, why the whole batch failed, are not in
OpenAI's object.

## Check on a batch

`GET /v1/batches/{id}` returns the batch object again. Poll it until `status` is
one of the four final ones:

| Status | Meaning |
|---|---|
| `validating` | Stored; no request has started yet |
| `in_progress` | Requests are running |
| `completed` | Every request has finished, successfully or not. Read each one's outcome in the results |
| `failed` | The batch stopped because the key or account that created it is no longer valid; `error` says so (`owner_unauthorized`). Requests that had not started are `failed` with `batch_failed` |
| `expired` | The completion window (`completion_window`, from `created_at` to `expires_at`) closed first. Requests that had not started are `expired` with `batch_expired`; finished ones keep their results |
| `cancelling` | Cancelled while requests were running; they are finishing |
| `cancelled` | Cancelled. Requests that had not started never will |

`request_counts.completed` counts the requests that succeeded and
`request_counts.failed` those that failed, including the ones a failed or
expired batch never ran. Cancelled requests are in neither.

## Completion webhooks

Polling is optional. Add `webhook_url` to the create body and the gateway POSTs
one event when the batch reaches a final status:

```json
{
  "requests": [{"custom_id": "q-1", "method": "POST", "url": "/v1/chat/completions", "body": {"model": "glm-4.7", "messages": [{"role": "user", "content": "hi"}]}}],
  "webhook_url": "https://ci.example.com/hooks/batch"
}
```

The URL must be HTTPS, at most 4096 characters, and carry no credentials or
fragment; anything else is a `400` at create. Every delivery resolves it afresh
and refuses loopback, private, link-local and metadata addresses, so a host that
starts answering privately after the batch was created is not posted to.
Redirects are not followed.

The event is:

```json
{
  "id": "evt_batch_0123456789abcdef",
  "object": "batch.event",
  "type": "batch.finished",
  "created_at": 1761480000,
  "data": {
    "batch": {"...": "the same object GET /v1/batches/{id} returns"},
    "results_url": "/v1/batches/batch_0123456789abcdef/results"
  }
}
```

with `X-HybridInference-Event: batch.finished` and
`X-HybridInference-Delivery: evt_batch_0123456789abcdef`, the latter stable
across retries. One event fires for every final status (`completed`, `failed`,
`cancelled` or `expired`); read `data.batch.status`.

Delivery is one POST per attempt, with a 10 second timeout, retried three times
five and ten minutes apart. Only a 2xx is success. It is at least once: a
gateway that dies between your answer and its own bookkeeping posts again. Use
the event as a signal, and `GET /v1/batches/{id}/results` as the source of truth.
Payloads are unsigned in this release; confirm one with your API key by reading
the batch.

The batch object's `webhook` says where delivery stands:

```json
"webhook": {
  "url_fingerprint": "3f9c2a1b7d40",
  "status": "delivered",
  "attempts": 1,
  "last_status_code": 200,
  "last_error": null,
  "last_attempt_at": 1761480005,
  "delivered_at": 1761480005
}
```

`status` is `pending`, `delivering`, `delivered` or `failed`. The URL itself is
never returned. Deleting the batch cancels a callback that has not gone out.

## Read the results

`GET /v1/batches/{id}/results` pages through the batch's requests in the order
you submitted them, with each one's outcome:

```bash
curl -s "https://your-gateway.example/v1/batches/$BATCH_ID/results?limit=100" \
  -H "Authorization: Bearer $API_KEY"
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "batch_req_1041",
      "custom_id": "q-1",
      "status": "completed",
      "response": {
        "status_code": 200,
        "request_id": "req_2c9f6a1d8e7b4c03a5f1e2d3c4b5a697",
        "body": {
          "id": "chatcmpl-9b1e",
          "object": "chat.completion",
          "model": "your-model-id",
          "choices": [
            {
              "index": 0,
              "message": {"role": "assistant", "content": "Prince Hamlet ..."},
              "finish_reason": "stop"
            }
          ],
          "usage": {"prompt_tokens": 14, "completion_tokens": 120, "total_tokens": 134}
        }
      },
      "error": null
    },
    {
      "id": "batch_req_1042",
      "custom_id": "q-2",
      "status": "failed",
      "response": null,
      "error": {"code": "invalid_request", "message": "...", "status": 400}
    }
  ],
  "has_more": false,
  "first_id": "batch_req_1041",
  "last_id": "batch_req_1042"
}
```

`limit` is 1–1000 (default 100). When `has_more` is `true`, ask for the next
page with `after` set to the page's `last_id`. Every request of the batch is
listed, finished or not: one still `queued` or `running` has neither `response`
nor `error` yet, so read the results once the batch is in a final status, or
read them again later.

A finished request has a `status` of `completed`, with the chat completion in
`response.body`, or `failed`, with an `error` instead. A request that never ran
is `cancelled`, `expired` or `failed`. Error codes:

| `code` | Meaning |
|---|---|
| `invalid_request` | The model rejected the request (`400`, `422`) |
| `model_not_found` | The model is gone, or you may no longer use it (`404`) |
| `forbidden` | Refused (`403`) |
| `rate_limited` | Still rate-limited after every retry (`429`) |
| `timeout` | Still timing out after every retry (`504`) |
| `service_unavailable` | No endpoint could serve it after every retry (`503`) |
| `upstream_error` | Another server error after every retry |
| `batch_failed` | Not run: the batch failed |
| `batch_expired` | Not run before the completion window closed |

`error.status` is the HTTP status behind the error, when there was one.

A short Python client that submits a batch, waits for it and collects every
result:

```python
import time

import httpx

client = httpx.Client(
    base_url="https://your-gateway.example/v1",
    headers={"Authorization": "Bearer hyi-..."},
    timeout=120,
)

prompts = ["Summarize the plot of Hamlet.", "Name three prime numbers."]
batch = client.post(
    "/batches",
    json={
        "requests": [
            {
                "custom_id": f"q-{i}",
                "body": {
                    "model": "your-model-id",
                    "messages": [{"role": "user", "content": prompt}],
                },
            }
            for i, prompt in enumerate(prompts)
        ]
    },
).raise_for_status().json()

while batch["status"] not in ("completed", "failed", "expired", "cancelled"):
    time.sleep(30)
    batch = client.get(f"/batches/{batch['id']}").raise_for_status().json()

results, after = {}, None
while True:
    params = {"limit": 1000, **({"after": after} if after else {})}
    page = client.get(f"/batches/{batch['id']}/results", params=params).raise_for_status().json()
    for result in page["data"]:
        results[result["custom_id"]] = result
    if not page["has_more"]:
        break
    after = page["last_id"]
```

## Cancel or delete a batch

`POST /v1/batches/{id}/cancel` stops a batch. Requests that have not started
are cancelled; running ones finish and keep their results. The batch is
`cancelling` until they do, then `cancelled`. Cancelling a batch that has
already finished returns it unchanged.

`DELETE /v1/batches/{id}` deletes a batch with its requests and results and
answers `{"id": "batch_...", "object": "batch.deleted", "deleted": true}`.
Its status does not matter, but deletion is blocked with `409` while any request
holds a live upstream lease. Cancel the batch, wait for its running requests to
finish (or their leases to expire), then delete it. A stale `running` item whose
lease has expired does not block deletion.

A finished batch is deleted automatically after the deployment's retention
period, seven days by default, so collect its results before then.

## List your batches

`GET /v1/batches` lists your own batches, newest first. `limit` is 1–100
(default 20); for the next page pass `after` set to the `last_id` of the
previous one. The answer has the shape of a results page, with batch objects in
`data`.

Reading, cancelling, deleting and listing your batches work even if your role no
longer allows creating them, so you can always collect and remove what you
submitted. Another user's batch reads as not found. Administrators may read,
cancel and delete any batch.

## Limits

| Limit | Default |
|---|---|
| Size of the create request's body | 32 MiB (`413` above it) |
| Requests per batch | 50,000 |
| Batches you may have unfinished at once (`validating`, `in_progress` or `cancelling`) | 10 (`429` above it) |
| Time a batch has to finish (`completion_window`) | 24 hours |
| Time a finished batch is kept | 7 days |

The operator may have changed any of them. A larger job is several batches.

## Errors from the create call

| Status | When |
|---|---|
| `400` | The body is not valid JSON or not a batch; a request is invalid, uses an unknown or auto model, or repeats a `custom_id`; too many requests; or the request carries `X-On-Behalf-Of` |
| `401` | The API key is invalid or expired when authentication runs, or the account becomes unavailable before the batch is stored |
| `403` | Your role may not create batches |
| `404` | Batch inference is not available on this gateway |
| `413` | The body is larger than the size limit |
| `429` | You have the most unfinished batches allowed, or your daily quota is used up |

## Compared with OpenAI's Batch API

- The requests are the `requests` array of the create call. There is no
  `input_file_id`, and no `output_file_id` or `error_file_id` to download:
  results come from `/v1/batches/{id}/results`.
- The completion window is the deployment's, not chosen per batch.
- Only `/v1/chat/completions` can be batched, and `stream` is refused.
- `DELETE /v1/batches/{id}` exists; the status `finalizing` never occurs.
- Batch requests cost the same as interactive ones.

## Running batch inference

These settings are on the admin console's **Configuration** tab, under
**Batch inference**. None needs a restart: a change applies to the next request
and to the worker's next round.

| Setting | Default | Meaning |
|---|---|---|
| `BATCH_ENABLED` | on | Serve batch inference. Off, every `/v1/batches` route answers `404` and no queued request starts; stored batches are kept |
| `BATCH_MIN_ROLE` | `internal` | Lowest role that may create batches (`free`, `pro`, `internal`, `admin`). A queued request also waits while its owner's role is below it |
| `BATCH_MODEL_CONCURRENCY` | 4 | Batch requests a model runs at once, across the whole deployment, when it has no cap of its own. `0` pauses batch work on those models |
| `BATCH_YIELD_TO_INTERACTIVE` | on | Interactive requests in flight on a model take batch slots from its cap |
| `BATCH_MAX_REQUESTS` | 50000 | Requests one batch may hold |
| `BATCH_MAX_BODY_MB` | 32 | Largest create request, in MiB |
| `BATCH_MAX_ACTIVE_PER_USER` | 10 | Unfinished batches one user may have |
| `BATCH_COMPLETION_WINDOW_HOURS` | 24 | Hours a new batch has before its unstarted requests expire, 1–168 |
| `BATCH_RETENTION_DAYS` | 7 | Days a finished batch and its results are kept |

A model's own cap is set in **Batch Concurrency** on the admin Settings tab:
empty takes `BATCH_MODEL_CONCURRENCY`, `0` pauses that model's batch work, and
the section shows how many batch requests each model is running and how many
are queued. A change reaches every gateway process within about ten seconds.
Behind it are `GET /admin/batch/models` and
`PATCH /admin/batch/models/{model_id}` with `{"max_concurrency": <n or null>}`.
The cap counts the requests of every gateway process against one limit, so it
does not grow with the number of processes.

Every gateway process runs a worker; requests of one batch may run in several of
them. A process that stops hands its unfinished requests back, and the requests
of one that dies are taken over by another once their lease runs out, a few
minutes later.

The batches live in the operational database (`batch_jobs`, `batch_items` and
`batch_model_limits`), and each request a batch runs is a row in `api_logs`
with `batch_job_id` set. Permanently deleting a user deletes their batches too.
