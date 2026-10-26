# Batch completion webhooks

Status: draft, implemented behind a per-batch `webhook_url`.
Date: 2026-10-26.
Issue: [#1509](https://github.com/HarvardMadSys/hybridInference/issues/1509).
Follows: [`2026-10-25-batch-inference-design.md`](2026-10-25-batch-inference-design.md).

## Problem

Batch jobs run for hours. Without a callback, every caller has to poll
`GET /v1/batches/{id}` in a loop. The issue asks for a per-batch URL, an HMAC
signature, retries with backoff, and a delivery log.

## Shape

- The URL is a `webhook_url` field on `POST /v1/batches`, alongside `requests`
  and `metadata`. It is fixed for the batch; there is no separate webhook
  resource and no update endpoint.
- One event, `batch.finished`, fires for every terminal status (`completed`,
  `failed`, `cancelled`, `expired`). The body carries the same batch object
  `GET /v1/batches/{id}` returns plus `data.results_url`. Consumers branch on
  `data.batch.status` rather than subscribing per status.
- Delivery state lives on `batch_jobs`, because a batch has exactly one
  callback. There is no delivery table; the retained log is the latest attempt's
  status code and error, exposed as the batch's `webhook` object.

## Deliberate deviations from the issue

- **No HMAC signature in this draft.** The issue asks for one; the draft ships
  unsigned so the callback and its state can be reviewed first. The payload is
  the batch object, so a receiver can confirm an event by reading the batch with
  its own key. A per-batch secret returned once at create and an
  `X-HybridInference-Signature` header are the planned follow-up.
- **The "delivery log" is the last attempt per batch**, not a history. A table
  of attempts would earn its place when retries need per-attempt diagnostics or
  when a second event type exists.

## Delivery

- Attempts are three, spaced five and ten minutes apart. A receiver that is
  deploying for a few minutes still gets the event; fifteen minutes is also
  short enough that a permanently failing receiver is marked `failed` before the
  default 24-hour batch window closes.
- The sender is an in-process asyncio task started with the batch worker
  (`serving.batch.webhooks.WebhookSender`). It claims terminal batches whose
  `webhook_status` is `pending`, with `FOR UPDATE SKIP LOCKED`, and holds a
  20-minute lease. A dead process's callback is reclaimed once the lease
  expires. Delivery is therefore at least once: the receiver must tolerate a
  duplicate and read results from the API.
- Retries use `tenacity.AsyncRetrying` with `wait_chain(5m, 10m)`. The
  attempt count is persisted before the outcome is dismissed, so a restart
  resumes the remaining budget instead of starting over.
- Non-2xx and network errors are retried; redirects are not followed. A target
  the gateway refuses (see below) fails immediately.

## URL safety

The URL is caller-supplied, so the batch endpoint would otherwise be an SSRF
primitive. At create: HTTPS, at most 4096 characters, no fragment, no
credentials, a host and a valid port. At every delivery: resolve the host and
refuse unless every answer is a public address. The second check is per attempt
on purpose, because DNS can change between creating the batch and delivering
it. This is stricter than the admin alert webhooks, whose URL an administrator
types and which may legitimately point at an internal endpoint.

## Coexistence with batch inference

- Webhooks do not change admission, concurrency or dispatch. A callback is sent
  only after the batch is terminal, so it cannot compete with interactive
  traffic.
- `BATCH_ENABLED` off answers every `/v1/batches` route 404, but a callback for
  an already-finished batch still goes out: the sender reads only the store.
- Deleting a batch, retaining it away, or hard-deleting its owner removes the
  callback with the row. A delivery already in flight may still land; there is
  no way to recall it.

## Deferred

- HMAC signature and a per-batch signing secret.
- Per-attempt delivery history and receiver-side replay protection.
- Event subscriptions (`batch.completed` and friends) and other event types.
- Custom headers or a caller-supplied signing scheme.
- Admin visibility into other users' callbacks beyond the batch object admins
  already read.
