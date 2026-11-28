# Batch Inference

A batch is a set of inference requests you send in one call and collect later. You submit the job, poll it, then read the results. The gateway runs the requests in the background, a few at a time per model, beside its interactive traffic.

There is one batch API, `/v1/batches`. Each batch has one target, the endpoint all its requests are for:

| Target | `url` / `endpoint` | Request body |
|---|---|---|
| Chat completions | `/v1/chat/completions` | A chat completion request |
| Embeddings | `/v1/embeddings` | An embeddings request |
| Responses | `/v1/responses` | An independent Responses request |
| Messages | `/v1/messages` | An Anthropic Messages request; the create call also sends the `anthropic-version` header |

The requests arrive in one of two ways:

| Input method | Submit | Results |
|---|---|---|
| Direct requests | `POST /v1/batches` with the requests in a `requests` array | Paged from `/v1/batches/{id}/results`, or one JSONL export |
| JSONL file | Upload a JSONL file to `/v1/files`, then `POST /v1/batches` with its `input_file_id` | An output file and an error file, downloaded from `/v1/files` |

Every batch is the same object, with the same statuses, results, cancel and delete. The shapes follow OpenAI's Batch API; see [Compared with OpenAI's batch API](#compared-with-openais-batch-api). The official OpenAI SDKs work with the file flow for the chat, embeddings and Responses targets: set the base URL to `https://your-gateway.example/v1` and pass your gateway API key. Direct requests, the results pages and the Messages target are this gateway's own, so call them over HTTP as the examples below do. Anthropic's Message Batches API (`/v1/messages/batches`, the Anthropic SDKs' `messages.batches`) is not served; see [Moving from Message Batches](#moving-from-message-batches).

```{note}
By default only `internal` users and administrators can create batches; the operator can open them to other roles. A gateway with batch inference turned off, or without a database, answers every batch route with `404`.
```

## How it works

Creating a batch stores its requests and runs nothing. Every request is checked first: direct requests before the create call answers, a file in the background. One invalid request refuses the whole batch, before any inference call. Checks apply the same per-request limits as interactive requests on messages, content parts, tools and stop sequences, and every JSON number must be finite, including numbers inside tool arguments and schemas. The create call's body, and each line of an input file, may nest arrays and objects at most 128 levels deep.

The gateway's worker then starts requests as their models have room, in no particular order. Each runs as if you had sent it to its endpoint yourself: the same models, fallback and price, logged under your account and counted toward your daily cost quota. By default it runs only on routes the gateway hosts itself (see [Which routes batch work may use](#which-routes-batch-work-may-use)). It takes no slot of your concurrent-request limit, because batch work has its own per-role quota, and a model runs fewer batch requests while interactive requests are in flight on it, so a batch slows down when interactive traffic is heavy.

Each request succeeds or fails on its own:

- A failure that may pass (`408`, `425`, `429`, a `5xx`) is retried with exponential backoff, from 5 seconds up to 15 minutes, with jitter, while the batch's window is open. When the next try would start after the window closes, the request fails with its last error.
- Any other `4xx` fails at once.
- A timeout (a `504`, or the upstream call's own timeout) fails at once with `timeout`: it is usually a long or repetitive output with a large `max_tokens`, which would time out again. It doesn't count toward pausing the model, which several retryable failures in a row do. A connection that times out while opening reached nobody and is retried.
- A provider response that cannot be encoded as valid JSON and UTF-8 fails with `upstream_error` (`502`), without a retry; any known charge stays recorded.
- An on-demand model (a cluster job, say) may answer `503` `model_queued` while it waits for a GPU. That is not a failure and spends no attempt: the request waits for the deployment's `Retry-After` (two minutes if absent or invalid, at most 168 hours), the worker sends that model no new batch requests for as long (at most 15 minutes), and this repeats until the model is up or the window ends. Interactive callers get the same `503`; the circuit breaker and failed-request alerts ignore it. A wait longer than `BATCH_QUEUED_ALERT_HOURS` raises one "Batch model waiting for capacity" alert.

With `BATCH_COMPLETION_WINDOW_HOURS` set to `0`, a new batch has a `null` `expires_at` and never expires; a request that keeps failing for a reason that may pass is tried at most 2,000 times (about two weeks at the backoff cap).

Before each request, the gateway checks the API key that created the batch again:

- a revoked or expired key, or a suspended or deleted account, fails the batch;
- a spent daily quota pauses your batches until it renews at midnight UTC;
- a spent monthly quota pauses them, with a check within the hour, until it is raised or renews on the 1st (UTC);
- a key that has spent its own daily or monthly budget pauses the batches created with it, with a check within the hour; your other keys' batches keep running;
- a role that no longer allows batches pauses them until it does.

Running requests count against quotas at what they may cost. If only those stand in the way and actual charges leave room, the request waits half a minute instead of pausing. Keep the key you created a batch with until the batch finishes.

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
| `metadata` | no | Up to 16 string pairs for your own use; keys up to 64 characters, values up to 512 |

A batch of direct requests may use several models, but not auto models (`auto`, `auto-<tier>`). An alias is resolved when you create the batch: the stored request keeps the name you sent, but runs on the alias's model at that time, so changing the alias later moves nothing.

An invalid request refuses the whole batch with the usual error shape, naming the request. A model you may not use is reported like one that does not exist:

```json
{
  "error": {
    "message": "requests[1] (custom_id 'q-2'): Model 'no-such-model' not found",
    "code": 400
  }
}
```

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

A batch of direct requests has `null` file fields. `usage` totals the tokens of finished requests. These fields are this gateway's own: `anthropic_version` (a Messages batch's version), `cost_usd` (what finished requests were billed so far), `cost_unknown_attempts` (see [Costs](#costs)), `results_expires_at` (when results will be deleted) and `error` (why the whole batch failed).

## Check on a batch

Poll `GET /v1/batches/{id}` until `status` is `completed`, `failed`, `expired` or `cancelled`:

| Status | Meaning |
|---|---|
| `validating` | Stored; no request has started yet |
| `in_progress` | Requests are running |
| `finalizing` | A file batch: every request has finished and its output files are being written |
| `completed` | Every request has finished, successfully or not. Read each one's outcome in the results |
| `failed` | The creating key or account is no longer valid (`error` is `owner_unauthorized`), and unstarted requests fail with `batch_failed`. A file batch also fails when its input is invalid; nothing runs and `errors` lists the problems |
| `expired` | The completion window (`created_at` to `expires_at`) closed first. Unstarted requests are `expired` with `batch_expired`; finished ones keep their results |
| `cancelling` | Cancelled while requests were running; they are finishing |
| `cancelled` | Cancelled. Requests that had not started never will |

`request_counts.completed` counts requests that succeeded, and `request_counts.failed` those that failed, including those a failed or expired batch never ran. Cancelled requests are in neither.

## Read the results

`GET /v1/batches/{id}/results` pages through the requests in submission order, with each one's outcome:

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

`limit` is 1–1000 (default 100), and a page also stops at about 8 MiB, so large results such as embeddings come a few to a page. When `has_more` is `true`, pass the page's `last_id` as `after`. Every request is listed, finished or not; a `queued` or `running` request has no `response` yet but may show its last `error` while it is retried.

A finished request is `completed`, with its endpoint's response in `response.body` (a chat completion, an embeddings list, a Responses object or a Messages `message`), or `failed`, with an `error`. A request that never ran is `cancelled`, `expired` or `failed`. `error.status` is the HTTP status behind the error, when there was one.

| `code` | Meaning |
|---|---|
| `invalid_request` | The model rejected the request (`400`, `422`) |
| `model_not_found` | The model is gone, or you may no longer use it (`404`) |
| `forbidden` | Refused (`403`) |
| `rate_limited` | Still rate-limited after every retry (`429`) |
| `timeout` | Ran out of time (`504`); not retried |
| `service_unavailable` | No endpoint could serve it after every retry (`503`) |
| `upstream_error` | Another server error after every retry |
| `result_too_large` | The response was larger than one request may store, or the batch's responses had reached their storage limit (the requests still to run then fail without running) |
| `batch_failed` | Not run: the batch failed |
| `batch_expired` | Not run before the completion window closed |

A short Python client that submits a batch, waits for it and collects every result:

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

## Retry the failed requests

`POST /v1/batches/{id}/retry-failed` copies a finished batch's requests that failed with `timeout`, `service_unavailable`, `upstream_error` or `rate_limited` into a new batch of direct requests, with their original `custom_id` and body, in submission order. Requests that failed with any other code would fail the same way and are skipped. Completed requests are not rerun or billed again, and the source batch is unchanged.

```json
{
  "id": "batch_0193...",
  "object": "batch.retry_failed",
  "source_batch_id": "batch_0192...",
  "retried": 12,
  "skipped": 3
}
```

The new batch keeps the source's target and endpoint, and its `metadata` is the source's plus `retried_from`. Results of a retried file batch are paged from `/v1/batches/{id}/results`; there is no output file. Only the owner may retry (an administrator can read, cancel and delete another user's batch, but not retry it), with the role that creating needs. The new batch counts toward the limits on unfinished batches and requests, and each request is billed when it runs. The endpoint answers `409` while the source is not finished or when nothing in it may be retried, and `404` for an unknown, deleted or another user's batch; otherwise it fails like a create call. You can retry the same source again while its results exist.

## Cancel or delete a batch

`POST /v1/batches/{id}/cancel` cancels the requests that have not started; running ones finish and keep their results. The batch is `cancelling` until they do, then `cancelled`. Cancelling a finished batch returns it unchanged.

`DELETE /v1/batches/{id}` deletes a batch with its requests and results, in any status, and answers `{"id": "batch_...", "object": "batch.deleted", "deleted": true}`. It answers `409` while a request holds a live upstream lease: cancel, wait for running requests to finish or their leases to expire, then delete.

A finished batch is deleted automatically after the retention period, seven days by default, so collect its results before then.

## List your batches

`GET /v1/batches` lists your own batches, newest first, with batch objects in `data`. `limit` is 1–100 (default 20); pass the previous page's `last_id` as `after`. The list includes batches created through the removed `/v1/messages/batches` (ids starting `msgbatch_`).

You can read, download, cancel, delete and list your batches with any of your API keys, even when your role no longer allows creating them or your daily quota is spent. Another user's batch reads as not found; administrators may read, cancel and delete any batch.

## Batches from a file

Upload a JSONL file with one request per line, then create a batch from it:

```bash
curl -s https://your-gateway.example/v1/files \
  -H "Authorization: Bearer $API_KEY" \
  -F purpose=batch -F file=@requests.jsonl

curl -s https://your-gateway.example/v1/batches \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"input_file_id": "file-…", "endpoint": "/v1/chat/completions", "completion_window": "24h"}'
```

Each line is `{"custom_id", "method": "POST", "url", "body"}`, with `url` equal to the batch's `endpoint`, and every line uses the same model. The batch is `validating` while the gateway reads the file in the background. If any line is invalid (not JSON, a repeated `custom_id`, another `url` or model, a body the endpoint would refuse), the batch is `failed`, nothing runs, and `errors` lists the problems by line number. Otherwise it runs, then is briefly `finalizing` while its result files are written, and ends with `output_file_id` (successful requests) and `error_file_id` (the others); either is `null` when it would be empty.

Download the files with `GET /v1/files/{id}/content`. A result file is the same bytes on every download, and its size and SHA-256 (the `ETag`) are fixed when the batch ends. A download is a stream, so it may run as long as `STREAM_REQUEST_TIMEOUT_SECONDS` allows (an hour by default), not just the 120 seconds an ordinary request gets. `GET /v1/files` lists your files, `GET /v1/files/{id}` returns one and `DELETE /v1/files/{id}` deletes one for good; a deleted output file is not written again, though the batch still names it, and deleting an input file does not affect batches created from it.

With the OpenAI Python SDK:

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

The results of a finished batch of direct requests are also available as one JSONL file in the same line shape: `GET /v1/batches/{id}/output.jsonl` (`409` until the batch has finished).

## Messages in a batch

A batch with the `/v1/messages` target carries Anthropic Messages bodies, and its create call names their API version in the `anthropic-version` header:

```bash
curl -s https://your-gateway.example/v1/batches \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
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

From a file, every line has `"url": "/v1/messages"` and the create call (`{"input_file_id": "file-…", "endpoint": "/v1/messages", "completion_window": "24h"}`) sends the same header. With the OpenAI SDK:

```python
batch = client.batches.create(
    input_file_id=upload.id,
    endpoint="/v1/messages",
    completion_window="24h",
    extra_headers={"anthropic-version": "2023-06-01"},
)
```

- The header is required for a Messages batch and refused on any other. The known versions are `2023-06-01` and `2026-10-21`; `BATCH_ANTHROPIC_VERSIONS` chooses which new batches may use. Only the header names the version: an `anthropic_version` field in the create body or in a request body is refused.
- A batch keeps its version (`anthropic_version` in the batch object), and every request is checked and run under it. Under `2026-10-21`, `temperature`, `top_p` and `top_k` are refused, as are document blocks in assistant messages and citations on user text.
- Reading, listing, cancelling, deleting and downloading need no version header.
- `anthropic-beta` is refused, and a request body cannot carry `betas`.
- A request the gateway cannot run as written refuses the whole batch: server tools, Files API sources, MCP servers, containers, and any parameter the model's route cannot carry, such as extended thinking, cache control or structured output on a non-Anthropic backend. Nothing is silently dropped.

Each request is one inference turn. A result that asks for a tool call is for your program to carry out and send back in a new request; the gateway never runs your tools.

## Moving from Message Batches

The gateway does not serve Anthropic's Message Batches routes, `/v1/messages/batches` and `/anthropic/v1/messages/batches`. Every operation under them answers `410 Gone` with a note on where to go, so the Anthropic SDKs' `messages.batches` methods fail. Online `/v1/messages` and `/anthropic/v1/messages` work as usual. Use `/v1/batches` with the Messages target:

| Message Batches | `/v1/batches` |
|---|---|
| `POST /v1/messages/batches`, header `anthropic-version`, body `{"requests": [{"custom_id", "params"}]}` | `POST /v1/batches`, header `anthropic-version`, body `{"requests": [{"custom_id", "url": "/v1/messages", "body"}]}` |
| `GET /v1/messages/batches`, `GET …/{id}` | `GET /v1/batches`, `GET /v1/batches/{id}` |
| `POST …/{id}/cancel` | `POST /v1/batches/{id}/cancel` |
| `DELETE …/{id}`, only once ended | `DELETE /v1/batches/{id}`, refused only while a request is running |
| `GET …/{id}/results` (`{"custom_id", "result"}` lines) | `GET /v1/batches/{id}/results` (pages) or `/output.jsonl` (the line shape above) |
| `processing_status` `in_progress` / `canceling` / `ended` | `status`, as for every batch (`completed`, `cancelled`, `expired`, …) |
| `request_counts` `processing` / `succeeded` / `errored` / `canceled` / `expired` | `request_counts` `total` / `completed` / `failed` |
| An invalid request became one `errored` result and the others ran | An invalid request refuses the whole batch before anything runs, naming it |
| `custom_id` up to 64 letters, digits, `_`, `-` | `custom_id` of 1–512 characters |
| Webhook event `message_batch.ended` | `batch.completed`, `batch.failed`, `batch.expired` or `batch.cancelled` for a new batch |

Batches created through the old routes (ids starting `msgbatch_`) are kept and still run. Manage them through `/v1/batches` and the console like any other batch; their results come as the common result lines. When one ends, its webhook event is still `message_batch.ended`. An `Idempotency-Key` used on the old routes does not replay through `/v1/batches`.

Check your requests before you migrate: a program that sent good and bad requests together now has the whole batch refused, and must drop the request the error names and resubmit.

## Embeddings and Responses in a batch

`/v1/embeddings` and `/v1/responses` batch like chat completions, inline or from a file. A batched Responses request must be independent: omit `store` or set it to `false`, and leave out `previous_response_id`, `conversation`, `background` and `stream`. Hosted tools, file inputs and any other field the gateway would have to drop are refused.

## Submitting safely twice

Send an `Idempotency-Key` header with a create call to make it safe to retry:

- The same key with the same request (and, for a Messages batch, the same `anthropic-version`) returns the batch the first call created, even if your quota has since run out or your role changed. The same key with a different request answers `409`.
- A key names one batch across both input methods, even when two calls race.
- A key is remembered until a day after its batch ends; after the batch is deleted, it answers `410` for at least a day.
- The key belongs to your account, so any of your API keys can replay it, and a replay never changes which key the batch runs under.

The SDKs do not send this header for you.

## Costs

Each attempt is billed at the same point and price as an interactive request, and the batch adds no charge of its own. `cost_usd` adds up the attempts whose cost is known. When an attempt's result is lost after the request was sent (the gateway timed out, or a worker stopped mid-request), the provider may still have run and charged it. Its cost is then unknown: it counts in `cost_unknown_attempts`, not as zero in `cost_usd`, and a retried request may have run more than once.

## Webhooks

Instead of polling, register one HTTPS endpoint for your batches with `PUT /v1/batch_webhook` (`{"url": "https://…"}`) or on the console's **Batches** page. The answer shows the signing secret, `whsec_…`, once; send `"rotate_secret": true` for a new one. `GET /v1/batch_webhook` shows the webhook, `DELETE /v1/batch_webhook` removes it, and `GET /v1/batch_webhook/events` lists recent deliveries.

`PUT` needs the role that creating a batch needs (`BATCH_MIN_ROLE`) at the time of the request; below it, `PUT` answers `403` and stores or looks up nothing. With a lowered role you can still read your webhook, list its deliveries and delete it, and events for your existing batches are still delivered while it stays enabled.

When a batch ends and its results can be read, the gateway POSTs `batch.completed`, `batch.failed`, `batch.expired` or `batch.cancelled` (`message_batch.ended` for a batch created through the old Message Batches routes). The body names only the batch (`{"id", "object": "event", "type", "created_at", "data": {"id"}}`); fetch the results with your API key. Deliveries are signed as Standard Webhooks are: verify `webhook-signature` (`v1,` and a base64 HMAC-SHA256 of `"{webhook-id}.{webhook-timestamp}.{body}"`, keyed with the base64 part of the secret).

A delivery not answered with a `2xx` is retried with backoff, up to eight attempts; the same event can arrive more than once, so deduplicate on `webhook-id`. Your events go out one at a time. An endpoint that times out on three deliveries in a row gets none for ten minutes, doubling each further time up to an hour. A webhook URL must be HTTPS and reach a public address: private, loopback, link-local and cloud-metadata addresses are refused when the URL is saved, on every redirect and on every connection.

## Batches in the console

The console's **Batches** page (in the side navigation) lists your batches with their name, target, model, input method, status, requests by outcome, tokens and known cost. You can search by name or ID, filter by status, open a batch, cancel or delete it, download its results, and set up your webhook. The page groups statuses for its badges and filter:

| Shown as | Statuses |
|---|---|
| **Processing** | `validating`, `in_progress`, `finalizing` or `cancelling`; a note says which stage, and that running requests still finish while cancelling |
| **Completed** | `completed`, and every request succeeded |
| **With issues** | `failed`, `expired` (the execution window ran out before every request started), or `completed` with a request that did not succeed |
| **Cancelled** | `cancelled`, whatever its requests did before the cancel |

A paused batch stays **Processing**, with a note saying why. Results can be downloaded only once the batch has finished.

The page is linked while `BATCH_ENABLED` is on and your role reaches `BATCH_MIN_ROLE`, or you have batches or a webhook of your own; quotas and keys play no part. Below `BATCH_MIN_ROLE` the page offers no **Create batch** button, but you can still manage what you have. The console reads this from `GET /user/batch-capabilities`.

### Creating a batch in the console

**Create batch** turns a JSONL file into a Chat Completions or Embeddings batch in three steps; nothing runs until you confirm:

1. **Upload data.** Lines as in [Batches from a file](#batches-from-a-file); `url` and `method` may be left out. The page tells Chat from Embeddings by each line's `url`, or by the body (`messages` or `input`), and asks only when no line says. Mixed types, a `url` the body contradicts, or another endpoint are errors.
2. **Configure.** Name the batch, and optionally describe it. Keep the file's model or choose one for every request, which you must do when the file names several models or one you cannot use. For Chat you may also set `temperature` and `max_tokens` for every request.
3. **Check and confirm.** The gateway checks every line without running or billing anything. Each problem names its line, `custom_id` and field, never the content, and the list downloads as CSV or JSONL. A valid file shows the first stored requests, the canonical model, the window (24 hours unless the operator set none), billing and result retention.

The console is stricter than `/v1/batches`: a field the gateway would not forward is an error instead of being dropped, for example `max_completion_tokens` (rename it to `max_tokens`) or `user`, and so are a byte-order mark, a key repeated within an object, and `NaN` or `Infinity`. Stored requests name the canonical model and must fit the upload size limit.

A console batch runs under your active API key; the page never creates one, but links to **API Keys** before you submit. Signing out does not stop a submitted batch, but revoking or regenerating the key fails its unfinished requests. If the key or the resolved model changes between check and submission, check again. Uploading and checking cost nothing; submitting needs quota left.

Each submission keeps its `Idempotency-Key` in your browser until its outcome is known, so a timeout, a reload or a second tab finds the batch instead of creating another. When an answer never arrived and more than a day has passed, the page asks before submitting again, since that can create and bill a second batch.

**Copy as new batch** reopens the create page with a batch's source file and settings, for console batches and for API file batches of the Chat or Embeddings target whose input file is still stored (otherwise you upload the file again). Direct-request, Messages and Responses batches cannot be copied yet. **Uploaded files** lists and deletes your uploads, which are kept for `BATCH_FILE_RETENTION_DAYS`; deleting one does not affect submitted batches. When your settings change what is stored, the batch keeps its own copy of the requests, which counts toward your storage. A finished Chat or Embeddings batch offers **Export unfinished requests**: its failed, expired and cancelled requests as a new input file.

## Limits

Defaults; the operator may change any of them on the admin **Settings** tab. Split a larger job into several batches.

| Limit | Default | Setting |
|---|---|---|
| Size of a create request's body, and of an uploaded input file | 32 MiB (`413` above it) | `BATCH_MAX_BODY_MB` |
| Requests per batch | 50,000 | `BATCH_MAX_REQUESTS` |
| Unfinished batches per user (`validating`, `in_progress`, `finalizing` or `cancelling`) | 10 (`429` above it) | `BATCH_MAX_ACTIVE_PER_USER` |
| Unfinished requests per user, across batches | 100,000 (`429` above it) | `BATCH_MAX_QUEUED_REQUESTS_PER_USER` |
| One line of an input file | 4 MiB | `BATCH_MAX_LINE_KB` |
| Input files a user may keep | 512 MiB | `BATCH_FILES_MAX_TOTAL_MB_PER_USER` |
| Uploads per user per hour, failed ones included (counted in each gateway process) | 60 | `BATCH_FILE_UPLOADS_PER_HOUR` |
| Days an uploaded input file is kept | 30 | `BATCH_FILE_RETENTION_DAYS` |
| Time a batch has to start its requests (`completion_window`) | 24 hours, 1–168; file batches always 24 hours; `0` means no limit for every batch | `BATCH_COMPLETION_WINDOW_HOURS` |
| Days a finished batch and its results are kept | 7 | `BATCH_RETENTION_DAYS` |
| Response one request may store | 4 MiB | `BATCH_MAX_RESULT_KB` |
| Responses one batch may store (counted in each gateway process; `0` means no limit) | 8 GiB | `BATCH_MAX_RESULTS_MB_PER_BATCH` |

A batch keeps the window and retention it was created with.

## Errors from the create call

| Status | When |
|---|---|
| `400` | The body is not valid JSON or not a batch; a request is invalid, uses an unknown or auto model, or repeats a `custom_id`; too many requests; a Messages batch without a known, allowed `anthropic-version` header, or another batch with one; an `anthropic_version` field in the body; an `anthropic-beta` header; or the request carries `X-On-Behalf-Of` |
| `401` | The API key is invalid or expired, or the account becomes unavailable before the batch is stored |
| `403` | Your role may not create batches, or the credential is an inference grant (`insufficient_scope`) |
| `404` | Batch inference is not available on this gateway |
| `409` | The `Idempotency-Key` was already used with a different request |
| `410` | The `Idempotency-Key`'s batch has been deleted |
| `413` | The body is larger than the size limit |
| `429` | You have the most unfinished batches allowed, the batch would take your unfinished requests over their limit, your daily or monthly quota is used up, or the key has spent its own budget |

A file batch is counted against the limit on unfinished requests while its file is read, after the create call has answered; over the limit, it is `failed` with `queued_request_limit` in `errors`.

## Compared with OpenAI's batch API

- Batch requests cost the same as interactive ones. There is no batch discount: the gateway runs each request itself instead of using a provider's batch service.
- The limits are the deployment's (see [Limits](#limits)) and lower than the providers' own.
- The 24-hour window of a file batch is when the gateway stops starting requests. Running requests finish, so a batch may end after `expires_at`. If the operator removed the window, a file batch still reports `completion_window` `24h`, with a `null` `expires_at`.
- Direct requests, `GET /v1/batches/{id}/results`, the JSONL export, `POST /v1/batches/{id}/retry-failed`, `DELETE /v1/batches/{id}`, `Idempotency-Key`, the Messages target and the cost fields are this gateway's own.
- Only `purpose=batch` files exist, and they serve batches only.
- Messages batches belong to your account; there are no workspaces.

## Running batch inference

These settings, and those in [Limits](#limits), are on the admin console's **Settings** tab, under **Batch**. None needs a restart: a change applies to the next request and to the worker's next round.

| Setting | Default | Meaning |
|---|---|---|
| `BATCH_ENABLED` | on | Serve batch inference. Off, every batch, file and webhook route answers `404`, the console hides the Batches entry and no queued request starts; stored batches are kept, and the console's capability check and the admin controls keep working |
| `BATCH_MIN_ROLE` | `internal` | Lowest role that may create batches, upload files and set up a batch webhook (`free`, `pro`, `internal`, `admin`). A queued request also waits while its owner's role is below it |
| `BATCH_MODEL_CONCURRENCY` | 64 | Batch requests a model without its own cap runs at once, across the deployment. `0` pauses batch work on those models |
| `BATCH_YIELD_TO_INTERACTIVE` | on | Interactive requests in flight on a model take slots from its batch cap. Off, batch work fills the cap whatever the interactive load, and the next two settings have no effect |
| `BATCH_SHARED_INTERACTIVE_LOAD` | on | Count every gateway process's interactive requests, not only the worker's own. While the shared counts cannot be read, no new batch request starts |
| `BATCH_INTERACTIVE_RESERVE` | 0 | Slots of every model's batch cap kept free for interactive requests, on top of those in flight |
| `BATCH_ROLE_CONCURRENCY_FREE` / `_PRO` / `_INTERNAL` / `_ADMIN` | 8 / 16 / 64 / 64 | Batch requests one user of that role may run at once, across models, counted in each gateway process. Separate from interactive limits (`user_concurrency_<role>` and per-user overrides don't apply to batches); `0` removes the cap |
| `BATCH_QUEUED_ALERT_HOURS` | 6 | Hours a model may wait for capacity before one alert says so; it resolves when the model next serves a batch request. `0` never alerts |
| `BATCH_ANTHROPIC_VERSIONS` | `2023-06-01,2026-10-21` | `anthropic-version` values a new Messages batch may name. Stored batches keep running under their own |
| `BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS` | off | Let webhooks reach private, loopback and link-local addresses (never cloud metadata). For isolated test deployments only |

A model's own cap is set under **Batch Concurrency** on the same tab: empty takes `BATCH_MODEL_CONCURRENCY` and `0` pauses that model's batch work. The section shows each model's running and queued batch requests, and a change reaches every process within about ten seconds. Behind it are `GET /admin/batch/models` and `PATCH /admin/batch/models/{model_id}` with `{"max_concurrency": <n or null>}`. The cap counts every process's requests against one limit.

Every gateway process runs a worker, and one batch's requests may run in several of them. A stopping process hands its unfinished requests back; a crashed one's are taken over when their lease runs out, a few minutes later.

Batches live in the operational database: `batch_jobs`, `batch_items`, `batch_attempts` (one row per attempt, with its charge), `batch_idempotency`, `batch_files`, `batch_file_chunks`, `batch_webhooks`, `batch_webhook_events`, `batch_model_limits`, `batch_user_state`, `batch_interactive_load` and `batch_read_leases`. Each request a batch runs is an `api_logs` row with `batch_job_id` set. Permanently deleting a user deletes their batches, files, idempotency records and webhook.

The admin console's **Batches** tab lists every user's batches with per-user totals (`GET /admin/batch/summary`) and lets an administrator cancel one. Known cost leaves out attempts with unknown cost, and shows how many there are. **Prompts** opens a batch's stored requests and answers (`GET /admin/batch/jobs/{batch_id}/requests`) only where the request log keeps prompts (`db_store_full_content`), and answers `403` elsewhere; each page read is audited as `batch.requests.read` against the owner, and a page whose read cannot be audited is not returned. The tab's **Requests** page lists the attempts the worker ran. **Recent Requests** leaves batch requests out, so a large batch does not bury direct traffic; the request-log endpoints take `source=batch` or `source=interactive`, and `batch_id` for one batch. Rows logged while a model was queued for capacity (`error` starting `UpstreamModelQueued:`) are left out of every request count.

### Which routes batch work may use

A batch is work nobody waits on, so by default it runs only on servers the gateway runs itself and never spends a cloud provider's quota or money. A route is local when its `deployment_type` is `self_hosted`, or, for `auto` (the default), when its host is `localhost`, `127.0.0.1`, `0.0.0.0` or `host.docker.internal`.

An administrator can allow or forbid batch work on any route with `PATCH /admin/routing/provider-route-batch/{model_id}/{route_id}` and `{"batch_allowed": true}` or `false`, or `null` to follow locality again. The choice is kept in `provider_route_batch_policies` by canonical model id and route id; `GET /admin/routing/provider-routes` reports each route's `batch_allowed`, `batch_allowed_default` and `batch_allowed_choice`.

Batch requests stay on their model's batch-allowed routes through selection, retries, fallback, offload and hedging, and neither follow nor move the owner's routing affinity. A batch-allowed route that is disabled, has an open circuit or has weight 0 makes items wait. Embedding backends follow their locality, with no per-route choice. A batch for a model with no batch-allowed route is refused (`batch_route_unavailable`, `400`), and a queued item fails the same way when its turn comes, for example after an upgrade for a model with only cloud routes: allow one of its routes if its queued batches should finish.

Two cases to check:

- A self-hosted engine on a LAN or cluster address counts as cloud unless its route says `deployment_type: self_hosted`.
- A proxy on the gateway host that forwards to a cloud API counts as local unless its route says `deployment_type: provider_api`, or batch work is forbidden on it.

### Upgrading a deployment

A worker runs only the batch kinds it knows: claims, expiry, finalizing, validation, sealing and cleanup filter on a batch's surface, target and schema version in the database query. That filter exists from #1660 on, so a rolling upgrade is supported from a release that includes #1660. While both versions run:

- an old process refuses a Messages-target create with `400`; retry it, or wait until every process runs the new version;
- an old process ignores an `anthropic-version` header or `anthropic_version` field on a non-Messages batch, which the new version refuses;
- an old process still serves `/v1/messages/batches`, where the new version answers `410`; the new version drains the batches created there.

A release with a batch worker but without #1660 would run any batch it finds, Messages batches included, as chat completions, and nothing the new version does can stop or detect it. Upgrade from such a release, or from an unknown one, with every old process stopped:

1. On the old version, turn `BATCH_ENABLED` off (Settings tab, or `python -m serving.config.manage set BATCH_ENABLED false`). Within about ten seconds every old worker stops claiming and logs that its batch worker is disabled; running requests finish or are handed back.
2. Stop every old gateway process, replica and worker, and turn off whatever would restart one (restart policies, autoscaling, health-check replacement, deploy jobs).
3. Confirm none is left using your orchestrator's identity of each process (image, revision, release or deployment id). Neither a database client address nor the health endpoint shows the version.
4. Wait until no session of the gateway's database user is left (the query in step 5 of [Rolling back](#rolling-back)).
5. Start the new version. It keeps `BATCH_ENABLED` off.
6. Once only the new version runs, confirmed as in step 3, turn `BATCH_ENABLED` on.

Coming from a version without `BATCH_ROLE_CONCURRENCY_<ROLE>`:

- `BATCH_MODEL_CONCURRENCY` now defaults to 64, but a stored value still applies: a deployment that saved the old default of 4 keeps 4.
- `BATCH_USER_MAX_CONCURRENCY` is gone and ignored; `BATCH_ROLE_CONCURRENCY_<ROLE>` is the per-user batch limit.
- `user_concurrency_<role>` and per-user concurrency overrides no longer limit batch requests.

`BATCH_EXPANSION_ENABLED` controls nothing. A leftover value is not shown on the Settings tab, cannot be set there and is warned about once at startup; the gateway keeps it so that a rollback to a release that still uses it (#1660 up to, but not including, #1673) reads it again. Once no such release will run, delete it (`DELETE /admin/config/BATCH_EXPANSION_ENABLED`, or `python -m serving.config.manage reset BATCH_EXPANSION_ENABLED`) and unset the variable.

### Rolling back

Before starting an older version, drain with this version the batches the older one cannot run, and start the older one only once no process of this version is left. Which batches those are, and the query that lists them (the drain query), depends on the target version:

- To #1660 or later: Messages batches created through `/v1/batches`. That version neither runs nor expires them, though it can cancel and delete them.

  ```sql
  SELECT id, phase, status FROM batch_jobs
  WHERE target = 'messages' AND api_surface <> 'anthropic' AND phase <> 'sealed';
  ```

- To a version before #1660: every batch that is not an inline chat batch, which it would run as chat completions.

  ```sql
  SELECT api_surface, target, phase, status, count(*)
  FROM batch_jobs
  WHERE (api_surface, target) <> ('openai_inline', 'chat') AND phase <> 'sealed'
  GROUP BY 1, 2, 3, 4;
  ```

An empty result is not enough while this version still runs, because a create admitted a moment earlier can still store a batch. Follow every step:

1. Set `BATCH_MIN_ROLE` to `admin` so no one else can create batches, and create none yourself. Other users' queued requests stop starting too, so cancel their batches rather than wait. To let them finish instead, skip this step; step 6 still catches a batch created meanwhile.
2. Cancel or let finish every batch the drain query lists (the admin **Batches** tab can cancel anyone's), and wait until it returns no rows; a cancelled file batch needs a moment to seal.
3. Turn `BATCH_ENABLED` off. New batch requests now get `404`, but one already past its checks may still be reaching the database.
4. Stop every process of this version, every replica and worker, and keep them stopped (restart policies, autoscaling, health-check replacement, deploy jobs) until the final check. Don't start the older version yet.
5. Confirm that no session of the gateway's database user is left, so every transaction has committed or rolled back. Run this as another user, or in your own session, which it leaves out; it must return no rows:

   ```sql
   SELECT pid, client_addr, state, xact_start
   FROM pg_stat_activity
   WHERE datname = current_database()
     AND usename = '<the gateway database user>'
     AND pid <> pg_backend_pid();
   ```

   A session can outlive its process while the server finishes a query or commit, or before it notices the connection is gone. Wait for it to end. If one stays and its host certainly runs no gateway, `pg_terminate_backend(pid)` ends it and rolls back its transaction.
6. Run the drain query again. Only if it returns no rows, start the older version. It keeps `BATCH_ENABLED` off; turn it on and restore `BATCH_MIN_ROLE` once only the older version runs.

The last check waits for a quiet database because each create is one transaction and only gateway processes write batches: once no gateway session is left and none can start, every create has committed or rolled back. If step 6 returns rows, do not start the older version (one before #1660 would run them as chat completions; a later one would leave them unrun). Start this version again, alone, turn `BATCH_ENABLED` on so its worker can run or seal those batches, cancel them or let them finish, and repeat steps 3 to 6.

Once the older version runs:

- A release from #1660 up to, but not including, #1673 reads `BATCH_EXPANSION_ENABLED` again, off by default, so it refuses its newer batch kinds until the setting is on; accepted batches keep running.
- Any release from #1660 serves `/v1/messages/batches` again, showing only the batches stored through it.
- A rollback rewrites no rows. A version before #1660 lists and purges batches of the newer kinds as if they were inline chat batches, and leaves uploaded files and webhooks unread until the next upgrade.
