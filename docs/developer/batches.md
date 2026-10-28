# Batch Inference

A batch is many inference requests handed to the gateway in one call and
answered later. You submit the whole job, poll it, then collect the results.
The gateway runs the requests in the background, a few at a time per model,
beside its interactive traffic.

There is one batch API, `/v1/batches`. Every batch has one **target**, the
endpoint each of its requests is for:

| Target | `url` / `endpoint` | Request body |
|---|---|---|
| Chat completions | `/v1/chat/completions` | A chat completion request |
| Embeddings | `/v1/embeddings` | An embeddings request |
| Responses | `/v1/responses` | An independent Responses request |
| Messages | `/v1/messages` | An Anthropic Messages request; the batch also names its `anthropic_version` |

Its requests arrive in one of two ways, the **input methods** of the same
batch resource:

| Input method | Submit | Results |
|---|---|---|
| Direct requests | `POST /v1/batches` with the requests in a `requests` array | Paged from `/v1/batches/{id}/results`, or one JSONL export |
| JSONL file | Upload a JSONL file to `/v1/files`, then `POST /v1/batches` with its `input_file_id` | An output file and an error file, downloaded from `/v1/files` |

Whatever the target and input method, a batch is the same object, with the
same statuses, counts, results, cancel and delete. The shapes follow OpenAI's
Batch API; the differences are listed at the end of this page.

The official OpenAI SDKs work with the file flow for the chat, embeddings and
Responses targets: point the SDK's base URL at
`https://your-gateway.example/v1` and pass your gateway API key. The Messages
target is this gateway's extension of that shape. Anthropic's own Message
Batches API (`/v1/messages/batches`, the Anthropic SDKs' `messages.batches`) is
not served; see [Moving from Message Batches](#moving-from-message-batches).
There is no HybridInference SDK of its own. Direct requests and the results
pages are this gateway's own, so the SDKs have no methods for them; call them
over HTTP, as the examples below do.

```{note}
Only **internal** users and administrators can create batches unless the
operator opens them to other roles. A gateway with batch inference turned off,
or without a database, answers every batch route with `404`. Otherwise
everything on this page is available: both input methods, every target, and
webhooks.
```

## How it works

Creating a batch stores its requests; nothing runs during that call. Every
request is checked before any of them runs: direct requests are checked whole
before the call answers ([Create a batch](#create-a-batch)), and a file is
checked whole in the background ([Batches from a file](#batches-from-a-file)).
One invalid request refuses the whole batch. A batch starts as `validating`,
and the gateway's worker starts its requests as soon as their models have room.

Each request then runs exactly as if you had sent it to its endpoint yourself:
the same models, routing and fallback, the same price, logged under your
account. It counts toward your daily cost quota. It does **not** take a slot of
your concurrent-request limit: batch work has a separate quota per role, so a
running batch does not use up the slots your interactive requests need, and
they do not use up the batch's.

Batch and interactive requests still share the models and the upstream servers
behind them. By default a model runs fewer batch requests while interactive
requests are in flight on it, so a batch gives way to interactive traffic on
the same model and may slow down while that traffic is heavy (see
[Running batch inference](#running-batch-inference)).

Requests run in no particular order, and a request that fails for a reason
that may pass — a timeout, a `429`, a `5xx` — is retried with backoff, up to
eight attempts in all. A request the model rejects (any other `4xx`) fails at
once.

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
| `requests[].url` | no | The target: `/v1/chat/completions` (the default), `/v1/embeddings`, `/v1/responses` or `/v1/messages`. Every request of one batch uses the same `url` |
| `requests[].body` | yes | The request body, as you would send it to that endpoint. `stream` must not be `true` |
| `anthropic_version` | for `/v1/messages` | The Anthropic API version the batch's Messages requests are checked and run under ([Messages in a batch](#messages-in-a-batch)). Refused on any other target |
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

A batch of direct requests has no files, so its file fields are `null`. Several
fields are not in OpenAI's object: `anthropic_version`, the version of a
Messages batch (`null` otherwise); `cost_usd`, what the batch's finished requests were
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
| `failed` | The batch stopped because its creating key or account is no longer valid; `error` says so (`owner_unauthorized`), and unstarted requests fail with `batch_failed`. A file batch also fails if its input cannot be validated; no request runs and `errors` lists the problems |
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
listed, finished or not. A request still `queued` or `running` has no
`response` yet, but may retain its last `error` while being retried. Read the
results once the batch is in a final status, or read them again later.

A finished request has a `status` of `completed`, with its endpoint's response
(a chat completion, an embeddings list, a Responses object or a Messages
`message`) in `response.body`, or `failed`, with an `error` instead. A request that never ran
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
`data`. It lists every batch of yours, of every target and input method,
including those created through the former `/v1/messages/batches` (their ids
start with `msgbatch_`).

Reading, downloading, cancelling, deleting and listing your batches work even if
your role no longer allows creating them or your daily quota is used up, so you
can always collect and remove what you submitted, with any of your API keys.
Another user's batch reads as not found. Administrators may read, cancel and
delete any batch.

## Batches from a file

Upload a JSONL file whose every line is one request, then create a batch from
it.

```bash
curl -s https://your-gateway.example/v1/files \
  -H "Authorization: Bearer $API_KEY" \
  -F purpose=batch -F file=@requests.jsonl

curl -s https://your-gateway.example/v1/batches \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"input_file_id": "file-…", "endpoint": "/v1/chat/completions", "completion_window": "24h"}'
```

Each line is `{"custom_id", "method": "POST", "url", "body"}`, the `url` equal
to the batch's `endpoint`. Every line must use the same model. A file batch for
`/v1/messages` also names its `anthropic_version` in the create call, and every
line is checked under it.

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

The results of a batch of direct requests are also available as one JSONL file,
in the same line shape, once it has ended: `GET /v1/batches/{id}/output.jsonl`.

## Messages in a batch

A batch whose target is `/v1/messages` carries Anthropic Messages request
bodies and names the Anthropic API version they are written for in its
`anthropic_version` field, beside `requests` or `input_file_id`:

```bash
curl -s https://your-gateway.example/v1/batches \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{
    "anthropic_version": "2023-06-01",
    "requests": [
      {
        "custom_id": "q-1",
        "url": "/v1/messages",
        "body": {
          "model": "your-model-id",
          "max_tokens": 512,
          "messages": [{"role": "user", "content": "Summarize the plot of Hamlet."}]
        }
      }
    ]
  }'
```

From a file, every line has `"url": "/v1/messages"` and the create call names
the version:

```json
{"input_file_id": "file-…", "endpoint": "/v1/messages", "completion_window": "24h", "anthropic_version": "2023-06-01"}
```

With the OpenAI SDK, pass it as an extra field:
`client.batches.create(input_file_id=upload.id, endpoint="/v1/messages",
completion_window="24h", extra_body={"anthropic_version": "2023-06-01"})`.

- `anthropic_version` is required for a Messages batch, and refused on any
  other. `2023-06-01` and `2026-10-21` are known; the operator chooses which
  of them new batches may use (`BATCH_ANTHROPIC_VERSIONS`). A batch keeps its
  version, and every request is checked, and runs, under it: under
  `2026-10-21`, `temperature`, `top_p` and `top_k` are refused, as are document
  blocks in assistant messages and citations on user text.
- The version is not taken from headers. `anthropic-beta` is refused, and an
  `anthropic-version` header, if you send one, must equal `anthropic_version`.
  A request body cannot carry its own version or `betas` either.
- Each request is checked like any batch request: one the gateway cannot run as
  written refuses the whole batch, naming it. That includes a model you may not
  use (reported exactly as an unknown one), server tools, Files API sources,
  MCP servers and containers, and any parameter a model's route cannot carry —
  extended thinking, cache control or structured output on a non-Anthropic
  backend, for example. A parameter is never silently dropped.
- The result of a request is the same line as for any target, with the
  Messages `message` in `response.body`.

Each request is a single inference turn. A result that asks for a tool call is
for your program to carry out and send back in a new request; the gateway never
runs your tools.

## Moving from Message Batches

The Anthropic Message Batches routes — `/v1/messages/batches` and
`/anthropic/v1/messages/batches` with every operation under them — have been
removed and answer `404`. The Anthropic SDKs' `messages.batches` methods no
longer work against this gateway. Online `/v1/messages` (and
`/anthropic/v1/messages`) is unchanged. Use `/v1/batches` with the Messages
target instead:

| Before | Now |
|---|---|
| `POST /v1/messages/batches`, header `anthropic-version`, body `{"requests": [{"custom_id", "params"}]}` | `POST /v1/batches`, body `{"anthropic_version", "requests": [{"custom_id", "url": "/v1/messages", "body"}]}` |
| `GET /v1/messages/batches`, `GET …/{id}` | `GET /v1/batches`, `GET /v1/batches/{id}` |
| `POST …/{id}/cancel` | `POST /v1/batches/{id}/cancel` |
| `DELETE …/{id}`, only once ended | `DELETE /v1/batches/{id}`, refused only while a request is running |
| `GET …/{id}/results` (`{"custom_id", "result"}` lines) | `GET /v1/batches/{id}/results` (pages) or `/output.jsonl` (the line shape above) |
| `processing_status` `in_progress` / `canceling` / `ended` | `status`, as for every batch (`completed`, `cancelled`, `expired`, …) |
| `request_counts` `processing` / `succeeded` / `errored` / `canceled` / `expired` | `request_counts` `total` / `completed` / `failed` |
| An invalid request became one `errored` result | An invalid request refuses the batch, naming it |
| `custom_id` up to 64 letters, digits, `_`, `-` | `custom_id` of 1–512 characters |
| Webhook event `message_batch.ended` | `batch.completed`, `batch.failed`, `batch.expired` or `batch.cancelled` |

Batches created through the removed routes are kept and still run. They are
listed, read, cancelled, deleted and downloaded through `/v1/batches` and the
console like any other batch (their ids start with `msgbatch_`), in the shapes
above: their results come as the common result lines, no longer as
`{"custom_id", "result"}` lines. An `Idempotency-Key` used on the removed
routes does not replay through `/v1/batches`.

## Embeddings and Responses in a batch

`/v1/embeddings` and `/v1/responses` can be batched like chat completions,
inline or from a file. A batched Responses request must be independent: omit
`store` or set it to `false`, and leave out `previous_response_id`,
`conversation`, `background` and `stream`. Hosted tools, file inputs and any
other field the gateway would have to drop are refused.

## Submitting safely twice

Send an `Idempotency-Key` header with a create call (either input method) to
make it safe to retry. The same key with the same request — for a Messages
batch, the same `anthropic_version` too — returns the batch
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

Setting up a webhook, changing its URL, rotating its secret or turning it back
on needs the same role as creating a batch (`BATCH_MIN_ROLE`); below it, `PUT`
answers `403` and nothing is stored or looked up. The check uses your role at
the time of the request. If your role is lowered, you can still read your
webhook, list its deliveries and delete it, and events for your existing
batches are still delivered while it stays enabled.

When a batch ends, after its results can be read, the gateway POSTs an event:
`batch.completed`, `batch.failed`, `batch.expired` or `batch.cancelled`,
whatever the batch's target. (An event a previous version queued for a Message
Batch before the upgrade is still delivered as `message_batch.ended`.) The body names the
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

The **Batches** page of the console (from the dashboard) lists all your
batches — each with its target, model and input method, status, progress,
tokens and known cost — and lets you cancel, delete and download results, and
set up your webhook. Batches are created through the API, not there.

## Limits

| Limit | Default |
|---|---|
| Size of the create request's body | 32 MiB (`413` above it) |
| Requests per batch | 50,000 |
| Batches you may have unfinished at once (`validating`, `in_progress`, `finalizing` or `cancelling`) | 10 (`429` above it) |
| Unfinished requests across all your batches | 100,000 (`429` above it) |
| Size of an input file, and of one of its lines | 32 MiB; 4 MiB per line |
| Input files you may keep, and upload per hour | 512 MiB; 60 |
| Time a batch has to start its pending requests (`completion_window`) | 24 hours (always 24 hours for file batches) |
| Time a finished batch is kept | 7 days |

The operator may have changed any of them. A larger job is several batches.

## Errors from the create call

| Status | When |
|---|---|
| `400` | The body is not valid JSON or not a batch; a request is invalid, uses an unknown or auto model, or repeats a `custom_id`; too many requests; a Messages batch without a known, allowed `anthropic_version`, or another batch with one; an `anthropic-beta` header, or an `anthropic-version` header that differs from `anthropic_version`; or the request carries `X-On-Behalf-Of` |
| `401` | The API key is invalid or expired when authentication runs, or the account becomes unavailable before the batch is stored |
| `403` | Your role may not create batches |
| `404` | Batch inference is not available on this gateway |
| `409` | The `Idempotency-Key` was already used with a different request |
| `410` | The `Idempotency-Key`'s batch has been deleted |
| `413` | The body is larger than the size limit |
| `429` | You have the most unfinished batches allowed, the batch would take your unfinished requests over their limit, or your daily quota is used up |

A file batch is counted against the limit on unfinished requests while its file
is read, after the create call has answered: over the limit, the batch is
`failed` with `queued_request_limit` in `errors`.

## Compared with OpenAI's batch API

- Batch requests cost the same as interactive ones: there is no batch discount,
  and the gateway runs each request itself rather than handing the batch to a
  provider's own batch service.
- The limits are the deployment's (see [Limits](#limits)), lower than the
  providers' own: 50,000 requests and 32 MiB per batch by default.
- The 24-hour window of file batches is when the gateway stops *starting*
  requests. Requests already running when it ends finish, so
  a batch may reach its final status after `expires_at`.
- Direct requests, `GET /v1/batches/{id}/results`, the JSONL export,
  `DELETE /v1/batches/{id}`, `Idempotency-Key`, the Messages target with
  `anthropic_version`, and the cost fields are this gateway's own. OpenAI's
  file flow is compatible for the chat, embeddings and Responses targets.
- Only `purpose=batch` files exist, and they serve batches only.
- Responses requests in a batch are independent: `store: true`,
  `previous_response_id`, `conversation`, `background` and hosted tools are
  refused.
- Messages batches belong to your account; there are no workspaces. Betas,
  server tools, Files API sources, MCP servers and containers are refused.
  Messages requests run through the gateway's own routing, not a provider's
  batch service.

## Running batch inference

These settings are on the admin console's **Configuration** tab, under
**Batch inference**. None needs a restart: a change applies to the next request
and to the worker's next round.

| Setting | Default | Meaning |
|---|---|---|
| `BATCH_ENABLED` | on | Serve batch inference, every kind of it. Off, every batch, file and webhook route answers `404` and no queued request starts; stored batches are kept |
| `BATCH_MIN_ROLE` | `internal` | Lowest role that may create batches, upload files and set up a batch webhook (`free`, `pro`, `internal`, `admin`). A queued request also waits while its owner's role is below it |
| `BATCH_MODEL_CONCURRENCY` | 64 | Batch requests a model runs at once, across the whole deployment, when it has no cap of its own. `0` pauses batch work on those models |
| `BATCH_YIELD_TO_INTERACTIVE` | on | Interactive requests in flight on a model take batch slots from its cap, so batch work gives way to interactive traffic on the same model. Off, batch work fills the cap whatever the interactive load, and `BATCH_SHARED_INTERACTIVE_LOAD` and `BATCH_INTERACTIVE_RESERVE` have no effect |
| `BATCH_MAX_REQUESTS` | 50000 | Requests one batch may hold |
| `BATCH_MAX_BODY_MB` | 32 | Largest create request, in MiB |
| `BATCH_MAX_ACTIVE_PER_USER` | 10 | Unfinished batches one user may have |
| `BATCH_COMPLETION_WINDOW_HOURS` | 24 | Hours a new batch has before its unstarted requests expire, 1–168 |
| `BATCH_RETENTION_DAYS` | 7 | Days a finished batch and its results are kept. A batch keeps the value it was created with |
| `BATCH_MAX_QUEUED_REQUESTS_PER_USER` | 100000 | Unfinished requests one user may have across all their batches |
| `BATCH_ROLE_CONCURRENCY_FREE` / `_PRO` / `_INTERNAL` / `_ADMIN` | 8 / 16 / 64 / 64 | Batch requests one user of that role may have running at once, across every model. Counted in each gateway process on its own, so with several processes a user may run up to this many in each; a model's cap, by contrast, is counted in the database across every process. Separate from the role's interactive concurrency limit (`user_concurrency_<role>` and a user's own override apply to interactive requests only); `0` removes this cap |
| `BATCH_SHARED_INTERACTIVE_LOAD` | on | Yield to the interactive requests of every gateway process, not only the worker's own. While the shared counts cannot be read from the database, no new batch request starts; running ones continue. Applies only while `BATCH_YIELD_TO_INTERACTIVE` is on |
| `BATCH_INTERACTIVE_RESERVE` | 0 | Slots of every model's batch cap kept free for interactive requests, on top of those in flight. Applies only while `BATCH_YIELD_TO_INTERACTIVE` is on |
| `BATCH_MAX_LINE_KB` | 4096 | Largest line of an input file |
| `BATCH_MAX_RESULT_KB` | 4096 | Largest response one batch request may store |
| `BATCH_FILES_MAX_TOTAL_MB_PER_USER` | 512 | Uploaded input files one user may keep |
| `BATCH_FILE_UPLOADS_PER_HOUR` | 60 | Input files one user may upload per hour |
| `BATCH_FILE_RETENTION_DAYS` | 30 | Days an uploaded input file is kept |
| `BATCH_ANTHROPIC_VERSIONS` | `2023-06-01,2026-10-21` | `anthropic_version` values a new `/v1/messages` batch may name. A stored batch keeps running under its own |
| `BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS` | off | Let webhooks reach private and loopback addresses. For isolated test deployments only |

`BATCH_EXPANSION_ENABLED`, which earlier versions used to hold back file
batches, Message Batches, embedding and Responses batches and webhooks, no
longer controls anything. A value left in the environment or the database
changes nothing, is not listed on the Settings tab and cannot be set
there; the gateway keeps it, so that a rollback reads it again, and warns about
it once at startup. Once no earlier version will run, delete the stored row
(`DELETE /admin/config/BATCH_EXPANSION_ENABLED`, or
`python -m serving.config.manage reset BATCH_EXPANSION_ENABLED`) and unset the
variable.

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
and `batch_webhook_events`, `batch_model_limits`, the worker's scheduling state
in `batch_user_state` and `batch_interactive_load`, and download leases in
`batch_read_leases`. Each request a batch runs is a row in `api_logs` with `batch_job_id` set,
and its item and attempt in the row's metadata. Permanently deleting a user
deletes their batches, files, idempotency records and webhook too.

The admin console's **Batches** tab lists every user's batches, totals them per
user and lets an administrator cancel one.

### Upgrading a deployment

A worker only runs the kinds of batch it knows: claims, expiry, finalizing,
validation, sealing and cleanup all skip a batch whose surface, target or
schema version is newer than the worker's. The workers that do not know this
are the old ones: a gateway older than the one that added file batches and
the former Message Batches (before #1660) has no such filter and would run their
requests as chat completions. New code cannot change what an old process already
running does, so before upgrading from one of those versions, make sure no old
worker is running alongside the new version:

1. On the old version, turn `BATCH_ENABLED` off (Settings tab, or
   `python -m serving.config.manage set BATCH_ENABLED false`). Within about ten
   seconds every old process stops claiming and sweeping, and logs that its
   batch worker is disabled. A request it was already running finishes or is
   handed back; those are chat batches, which the new version runs too.
2. Upgrade every gateway process. The new processes start with
   `BATCH_ENABLED` still off.
3. Once no process of the old version is left (check your process manager or
   orchestrator, not only the health endpoint), turn `BATCH_ENABLED` on.

Stopping every old process before any new one starts is equally safe. From a
version that already has file batches and the former Message Batches (#1660 or
later), a rolling upgrade needs none of this. Messages batches created through
`/v1/batches` are kinds those workers do not know, so while old processes are
still running they neither run, validate, seal nor expire them; only this
version's workers do. A create for the Messages target that an old process
happens to answer is refused with `400` (it does not know the target); retry
it, or wait until every process runs this version. Until the last old process
is gone, a client may still
reach the old `/v1/messages/batches` on one of them; batches it creates there are
drained by this version as described in
[Moving from Message Batches](#moving-from-message-batches).

Upgrading from a version without `BATCH_ROLE_CONCURRENCY_<ROLE>` changes three
things:

- `BATCH_MODEL_CONCURRENCY` now defaults to 64, but a value stored for it on
  the **Configuration** tab still applies. A deployment that saved the old
  default of 4 keeps 4 until an administrator changes it.
- `BATCH_USER_MAX_CONCURRENCY` no longer exists. A value stored for it is
  ignored; `BATCH_ROLE_CONCURRENCY_<ROLE>` is the per-user batch limit.
- `user_concurrency_<role>` and a user's own concurrency override no longer
  limit batch requests.

### Rolling back

To a version that already has file batches and the former Message Batches
(#1660 or later), first cancel or let finish every Messages batch created
through `/v1/batches`: that version neither runs nor expires them, though it can
still cancel and delete them. Wait until this returns no rows, then roll back as
usual:

```sql
SELECT id, phase, status FROM batch_jobs
WHERE target = 'messages' AND api_surface <> 'anthropic' AND phase <> 'sealed';
```

That version reads `BATCH_EXPANSION_ENABLED` again, off by default, so new
batches of its newer kinds are refused until it is on; those already accepted
keep running. It serves `/v1/messages/batches` again, and shows there only the
batches created through it.

To an older version, drain the newer batches with this version, which alone
can manage them, and start the older version only once every process of this
version has stopped and the database shows none left:

1. Set `BATCH_MIN_ROLE` to `admin`, so no one else can create batches. Other
   users' queued requests then stop starting too, so their batches have to be
   cancelled rather than waited for. To let them finish instead, skip this
   step; step 6 still catches a batch created meanwhile.
2. Cancel (or let finish) every batch that is not an inline chat batch: the
   admin **Batches** tab lists and cancels anyone's. A cancelled file batch
   still needs a moment to seal. Wait until this returns no rows:

   ```sql
   SELECT api_surface, target, phase, status, count(*)
   FROM batch_jobs
   WHERE (api_surface, target) <> ('openai_inline', 'chat') AND phase <> 'sealed'
   GROUP BY 1, 2, 3, 4;
   ```

3. Turn `BATCH_ENABLED` off. New batch requests now answer `404`, but a
   request that had already passed its checks may still be on its way to the
   database, so an empty result at this point proves nothing yet.
4. Stop every gateway process of this version, every replica and worker, and
   keep them stopped: turn off whatever would start one again on its own
   (restart policies, autoscaling, health-check replacement, deploy jobs) for
   as long as the final check takes. Do not start any process of the older
   version yet.
5. Confirm the database has finished with them: no session of the gateway's
   database user is left, so every transaction those processes opened has
   committed or rolled back. Run this as a different user, or in your own
   session, which the query leaves out; it must return no rows:

   ```sql
   SELECT pid, client_addr, state, xact_start
   FROM pg_stat_activity
   WHERE datname = current_database()
     AND usename = '<the gateway database user>'
     AND pid <> pg_backend_pid();
   ```

   A session can outlive its process: the server may still be finishing a
   query or commit that was already sent, or not yet have noticed that the
   connection is gone. Wait for it to end. If a session stays and you have
   confirmed its host no longer runs a gateway, `pg_terminate_backend(pid)`
   ends it and rolls back its open transaction.
6. Only then run the batch query from step 2 again. Only if it returns no
   rows, start the older version. It keeps `BATCH_ENABLED` off; turn it on,
   and set `BATCH_MIN_ROLE` back, once only the older version runs.

Why the last check waits until the database is quiet: turning `BATCH_ENABLED`
off refuses new requests, but it does not stop a create that was admitted a
moment earlier; that create still stores its batch, however long its body or
the database takes. A process having exited is not enough either, since a
commit it sent may still be in progress on the server. Each create is one
transaction and only gateway processes write batches, so once no gateway
session is left and none can start, every create has either committed or
rolled back, and the query sees everything that will ever be there.

If step 6 returns rows, do not start the older version: it would run them as
chat completions. Start this version again, and only this version, turn
`BATCH_ENABLED` on so its worker can run or seal those batches, cancel them or
let them finish (step 2), then repeat steps 3 to 6.

Finished batches of the newer kinds stay in the database; the older version
lists and purges them as if they were inline chat batches. Uploaded files and
webhooks stay unread until the next upgrade. Turning `BATCH_ENABLED` off is
not enough on its own: it stops the newer version's worker too, so the
newer batches would never drain.
