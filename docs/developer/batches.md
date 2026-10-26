# Batch Inference

A batch is many inference requests handed to the gateway in one call and
answered later. You submit the whole job, poll it, then collect the results.
The gateway runs the requests in the background, a few at a time per model,
beside its interactive traffic.

There are three ways to submit one, and each speaks its own protocol:

| API | Submit | Results |
|---|---|---|
| OpenAI, inline | `POST /v1/batches` with the requests in a `requests` array | Paged from `/v1/batches/{id}/results`, or one JSONL export |
| OpenAI, from a file | Upload a JSONL file to `/v1/files`, then `POST /v1/batches` with its `input_file_id` | An output file and an error file, downloaded from `/v1/files` |
| Anthropic Message Batches | `POST /v1/messages/batches` (also under `/anthropic`) | JSONL from `/v1/messages/batches/{id}/results` |

A batch request can be a chat completion (`/v1/chat/completions`), an embedding
(`/v1/embeddings`), an independent Responses request (`/v1/responses`) or an
Anthropic message. The shapes follow OpenAI's Batch API and Anthropic's Message
Batches API; the differences are listed at the end of this page.

```{note}
Only **internal** users and administrators can create batches unless the
operator opens them to other roles. A gateway with batch inference turned off,
or without a database, answers every batch route with `404`. File batches,
Anthropic Message Batches, embeddings, Responses requests and webhooks are
available once the operator turns on `BATCH_EXPANSION_ENABLED`.
```

## How it works

Creating a batch validates every request in it and stores them. Nothing runs
during that call: the batch starts as `validating`, and the gateway's worker
starts its requests as soon as their models have room.

Each request then runs exactly as if you had sent it to its endpoint yourself:
the same models, routing and fallback, the same price, logged under your
account. It counts toward your daily cost quota. It does **not** use your
concurrent-request limit: batch work has a separate quota per role, so a
running batch never blocks your own interactive requests, and they never hold
up the batch. Requests run in no particular order, and a request that fails for
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
| `requests[].url` | no | `/v1/chat/completions` (the default), `/v1/embeddings` or `/v1/responses`. Every request of one batch uses the same `url` |
| `requests[].body` | yes | The request body, as you would send it to that endpoint. `stream` must not be `true` |
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

An inline batch has no files, so its file fields are `null`. Several fields are
not in OpenAI's object: `cost_usd`, what the batch's finished requests were
billed so far; `cost_unknown_attempts`, attempts whose cost is unknown and not
in `cost_usd` (see [Costs](#costs)); `results_expires_at`, when the results will
be deleted; and `error`, why the whole batch failed. `usage` totals the tokens
of the finished requests.

## Check on a batch

`GET /v1/batches/{id}` returns the batch object again. Poll it until `status` is
one of the four final ones:

| Status | Meaning |
|---|---|
| `validating` | Stored; no request has started yet |
| `in_progress` | Requests are running |
| `finalizing` | A file batch: every request has finished and its output files are being written |
| `completed` | Every request has finished, successfully or not. Read each one's outcome in the results |
| `failed` | The batch stopped because the key or account that created it is no longer valid; `error` says so (`owner_unauthorized`). Requests that had not started are `failed` with `batch_failed` |
| `expired` | The completion window (`completion_window`, from `created_at` to `expires_at`) closed first. Requests that had not started are `expired` with `batch_expired`; finished ones keep their results |
| `cancelling` | Cancelled while requests were running; they are finishing |
| `cancelled` | Cancelled. Requests that had not started never will |

`request_counts.completed` counts the requests that succeeded and
`request_counts.failed` those that failed, including the ones a failed or
expired batch never ran. Cancelled requests are in neither.

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
`data`. It lists OpenAI batches, inline and from files; Message Batches are
listed by `GET /v1/messages/batches`, and an id of one API reads as not found on
the other.

Reading, downloading, cancelling, deleting and listing your batches work even if
your role no longer allows creating them or your daily quota is used up, so you
can always collect and remove what you submitted, with any of your API keys.
Another user's batch reads as not found. Administrators may read, cancel and
delete any batch.

## Batches from a file

The OpenAI flow: upload a JSONL file whose every line is one request, then
create a batch from it.

```bash
curl -s https://your-gateway.example/v1/files \
  -H "Authorization: Bearer $API_KEY" \
  -F purpose=batch -F file=@requests.jsonl

curl -s https://your-gateway.example/v1/batches \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"input_file_id": "file-…", "endpoint": "/v1/chat/completions", "completion_window": "24h"}'
```

Each line is `{"custom_id", "method": "POST", "url", "body"}`, the `url` equal
to the batch's `endpoint`. Every line must use the same model.

The batch starts as `validating` while the gateway reads the file in the
background. If any line is invalid — not JSON, a repeated `custom_id`, another
`url` or model, a body the endpoint would refuse — the whole batch is `failed`
and nothing of it runs; `errors` lists the problems with their line numbers.
Otherwise it moves to `in_progress`. Once every request has finished it is
`finalizing` for a moment while its result files are written, and then reaches
its final status with `output_file_id` (the successful requests) and
`error_file_id` (the others) set; either is `null` when it has no lines.

Download them with `GET /v1/files/{id}/content`. A result file is the same
bytes every time it is downloaded, and its size and SHA-256 (the `ETag`) are
fixed when the batch ends. `GET /v1/files` lists your files, `GET
/v1/files/{id}` returns one, and `DELETE /v1/files/{id}` deletes one for good:
a deleted output file is not written again, though the batch still names it.
Deleting an input file does not affect a batch already created from it.

The official OpenAI SDKs work with this flow:

```python
from openai import OpenAI

client = OpenAI(base_url="https://your-gateway.example/v1", api_key="hyi-...")
upload = client.files.create(file=open("requests.jsonl", "rb"), purpose="batch")
batch = client.batches.create(
    input_file_id=upload.id, endpoint="/v1/chat/completions", completion_window="24h"
)
# ... poll client.batches.retrieve(batch.id) until it has ended, then:
output = client.files.content(batch.output_file_id).text
```

An inline batch's results are also available as one JSONL file, in the same
line shape, once it has ended: `GET /v1/batches/{id}/output.jsonl`.

## Anthropic Message Batches

`/v1/messages/batches` (and `/anthropic/v1/messages/batches`) speak Anthropic's
Message Batches API, so the official Anthropic SDKs work against it:

```python
import anthropic

client = anthropic.Anthropic(base_url="https://your-gateway.example", api_key="hyi-...")
batch = client.messages.batches.create(
    requests=[
        {
            "custom_id": "q-1",
            "params": {
                "model": "your-model-id",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Summarize the plot of Hamlet."}],
            },
        }
    ]
)
# ... poll client.messages.batches.retrieve(batch.id) until processing_status is "ended"
for result in client.messages.batches.results(batch.id):
    print(result.custom_id, result.result.type)
```

Every request needs the `anthropic-version` header. `2023-06-01` and
`2026-10-21` are accepted; a batch keeps the version it was created with, and
each of its requests is checked against that version's rules (under
`2026-10-21`, `temperature`, `top_p` and `top_k` are refused, as are document
blocks in assistant messages and citations on user text). `anthropic-beta` is
refused.

The envelope — `custom_id` (up to 64 letters, digits, `_` and `-`, unique),
the number of requests, the body size — is checked at once and any problem
refuses the batch. Each request's `params` is then checked on its own: one the
gateway cannot run as written, including one naming a model you may not use,
becomes an `errored` result and the others still run. A request is never sent
with a parameter silently dropped: when a model's route cannot carry a
parameter (extended thinking, cache control or structured output on a
non-Anthropic backend, for example), the request errors instead.

`request_counts` hold every request as `processing` until the whole batch has
`ended`; `results_url` appears then. Cancelling makes it `canceling` until its
running requests finish. A batch can be deleted only once it has ended.

Each request is a single inference turn. A result that asks for a tool call is
for your program to carry out and send back in a new request; the gateway never
runs your tools.

## Embeddings and Responses in a batch

`/v1/embeddings` and `/v1/responses` can be batched like chat completions,
inline or from a file. A batched Responses request must be independent: omit
`store` or set it to `false`, and leave out `previous_response_id`,
`conversation`, `background` and `stream`. Hosted tools, file inputs and any
other field the gateway would have to drop are refused.

## Submitting safely twice

Send an `Idempotency-Key` header with a create call (any of the three APIs) to
make it safe to retry. The same key with the same request returns the batch
the first call created, even if your quota has since run out or your role
changed; the same key with a different request answers `409`. After the batch
is deleted the key answers `410` for at least a day; a key is remembered until a
day after its batch ends. The key belongs to your account, so any of your API
keys can replay it, and a replay never changes which key the batch runs under.
The SDKs do not send this header for you.

## Costs

Every attempt at a request is recorded with what it was billed, at the same
point and the same price as an interactive request; a batch is never billed a
second time on top. `cost_usd` adds up the attempts whose cost is known.

When an attempt's result was lost after the request had been sent — the
gateway timed out, or a worker stopped mid-request — the provider may still
have run and charged it. Its cost is then *unknown*: it is counted in
`cost_unknown_attempts` and left out of `cost_usd` rather than counted as zero.
A retried request may therefore have run more than once.

## Webhooks

Instead of polling, register one HTTPS endpoint for your batches with `PUT
/v1/batch_webhook` (`{"url": "https://…"}`), or on the console's **Batches**
page. The answer carries the signing secret, `whsec_…`, once; send
`"rotate_secret": true` to get a new one. `GET /v1/batch_webhook` shows the
webhook, `DELETE /v1/batch_webhook` removes it, and `GET
/v1/batch_webhook/events` lists recent deliveries.

When a batch ends, after its results can be read, the gateway POSTs an event:
`batch.completed`, `batch.failed`, `batch.expired` or `batch.cancelled` for an
OpenAI batch, `message_batch.ended` for a Message Batch. The body names the
batch only (`{"id", "object": "event", "type", "created_at", "data": {"id"}}`);
fetch the results with your API key. Deliveries are signed as Standard Webhooks
are: verify `webhook-signature` (`v1,` and a base64 HMAC-SHA256 of
`"{webhook-id}.{webhook-timestamp}.{body}"` with the secret's base64 part as the
key). A delivery that is not answered with a `2xx` is retried with backoff, up
to eight attempts; the same event can arrive more than once, so deduplicate on
`webhook-id`.

A webhook URL must be HTTPS and reach a public address. Private, loopback,
link-local and cloud-metadata addresses are refused when the URL is saved, again
on every redirect, and again on every connection.

## Batches in the console

The **Batches** page of the console (from the dashboard) lists your batches
across all three APIs with their progress, tokens and known cost, and lets you
cancel, delete and download results, and set up your webhook.

## Limits

| Limit | Default |
|---|---|
| Size of the create request's body | 32 MiB (`413` above it) |
| Requests per batch | 50,000 |
| Batches you may have unfinished at once (`validating`, `in_progress` or `cancelling`) | 10 (`429` above it) |
| Unfinished requests across all your batches | 100,000 (`429` above it) |
| Size of an input file, and of one of its lines | 32 MiB; 4 MiB per line |
| Input files you may keep, and upload per hour | 512 MiB; 60 |
| Time a batch has to finish (`completion_window`) | 24 hours (always 24 hours for file batches and Message Batches) |
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

## Compared with OpenAI's and Anthropic's batch APIs

- Batch requests cost the same as interactive ones: there is no batch discount,
  and the gateway runs each request itself rather than handing the batch to a
  provider's own batch service.
- The limits are the deployment's (see [Limits](#limits)), lower than the
  providers' own: 50,000 requests and 32 MiB per batch by default.
- The 24-hour window of file batches and Message Batches is when the gateway
  stops *starting* requests. Requests already running when it ends finish, so
  a batch may reach its final status after `expires_at`.
- The inline form, `GET /v1/batches/{id}/results`, the JSONL export,
  `DELETE /v1/batches/{id}`, `Idempotency-Key` and the cost fields are this
  gateway's own.
- Only `purpose=batch` files exist, and they serve batches only.
- Responses requests in a batch are independent: `store: true`,
  `previous_response_id`, `conversation`, `background` and hosted tools are
  refused.
- Message Batches belong to your account; there are no workspaces.
  `anthropic-beta` is refused, and so are server tools, Files API sources, MCP
  servers and containers.

## Running batch inference

These settings are on the admin console's **Configuration** tab, under
**Batch inference**. None needs a restart: a change applies to the next request
and to the worker's next round.

| Setting | Default | Meaning |
|---|---|---|
| `BATCH_ENABLED` | on | Serve batch inference. Off, every `/v1/batches` route answers `404` and no queued request starts; stored batches are kept |
| `BATCH_MIN_ROLE` | `internal` | Lowest role that may create batches (`free`, `pro`, `internal`, `admin`). A queued request also waits while its owner's role is below it |
| `BATCH_MODEL_CONCURRENCY` | 64 | Batch requests a model runs at once, across the whole deployment, when it has no cap of its own. `0` pauses batch work on those models |
| `BATCH_YIELD_TO_INTERACTIVE` | on | Interactive requests in flight on a model take batch slots from its cap |
| `BATCH_MAX_REQUESTS` | 50000 | Requests one batch may hold |
| `BATCH_MAX_BODY_MB` | 32 | Largest create request, in MiB |
| `BATCH_MAX_ACTIVE_PER_USER` | 10 | Unfinished batches one user may have |
| `BATCH_COMPLETION_WINDOW_HOURS` | 24 | Hours a new batch has before its unstarted requests expire, 1–168 |
| `BATCH_RETENTION_DAYS` | 7 | Days a finished batch and its results are kept. A batch keeps the value it was created with |
| `BATCH_EXPANSION_ENABLED` | off | Allow creating file batches, Message Batches, embedding and Responses batches, and webhooks. Off, batches already accepted keep running and stay readable, downloadable, cancellable and deletable |
| `BATCH_MAX_QUEUED_REQUESTS_PER_USER` | 100000 | Unfinished requests one user may have across all their batches |
| `BATCH_USER_MAX_CONCURRENCY` | 64 | Batch requests one user may have running at once across the deployment and every model; `0` removes this cap |
| `BATCH_ROLE_CONCURRENCY_FREE` / `_PRO` / `_INTERNAL` / `_ADMIN` | 8 / 16 / 64 / 64 | Batch requests one user of that role may have running at once in each gateway process. Separate from the role's interactive concurrency limit (`user_concurrency_<role>` and a user's own override apply to interactive requests only); `0` removes this cap |
| `BATCH_SHARED_INTERACTIVE_LOAD` | on | Yield to the interactive requests of every gateway process, not only the worker's own |
| `BATCH_INTERACTIVE_RESERVE` | 0 | Slots of every model's batch cap kept free for interactive requests, on top of those in flight |
| `BATCH_MAX_LINE_KB` | 4096 | Largest line of an input file |
| `BATCH_MAX_RESULT_KB` | 4096 | Largest response one batch request may store |
| `BATCH_FILES_MAX_TOTAL_MB_PER_USER` | 512 | Uploaded input files one user may keep |
| `BATCH_FILE_UPLOADS_PER_HOUR` | 60 | Input files one user may upload per hour |
| `BATCH_FILE_RETENTION_DAYS` | 30 | Days an uploaded input file is kept |
| `BATCH_ANTHROPIC_VERSIONS` | `2023-06-01,2026-10-21` | `anthropic-version` values a Message Batch may be created under |
| `BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS` | off | Let webhooks reach private and loopback addresses. For isolated test deployments only |

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

The batches live in the operational database: `batch_jobs`, `batch_items`,
`batch_attempts` (one row per attempt, with what it was billed),
`batch_idempotency`, `batch_files` and `batch_file_chunks`, `batch_webhooks`
and `batch_webhook_events`, `batch_model_limits` and a few tables of scheduling
state. Each request a batch runs is a row in `api_logs` with `batch_job_id` set,
and its item and attempt in the row's metadata. Permanently deleting a user
deletes their batches, files, idempotency records and webhook too.

The admin console's **Batches** tab lists every user's batches, totals them per
user and lets an administrator cancel one.

### Upgrading a deployment

A gateway older than the one that added file batches and Message Batches has a
worker that would run their requests as chat completions. So keep
`BATCH_EXPANSION_ENABLED` off until every gateway process runs the new version,
then turn it on. Turning it off again later stops new batches of those kinds and
nothing else.

To roll back to an older version after batches of the new kinds were accepted,
do it while the new version is still running, since only it can manage them:
turn `BATCH_EXPANSION_ENABLED` off, then cancel (or let finish) every file
batch, Message Batch and embedding or Responses batch until none is queued,
running, validating or finalizing, and delete those you no longer need. Then
stop every gateway process before any process of the older version starts. An
older worker cannot tell these batches apart, and turning `BATCH_ENABLED` off
does not make it safe: it only hides the batch routes.
