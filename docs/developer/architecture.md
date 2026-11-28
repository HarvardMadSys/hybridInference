# Architecture

HybridInference is a FastAPI gateway that speaks the OpenAI and OpenRouter HTTP
APIs. It sends each request to a local inference server (vLLM, SGLang, Ollama
or anything else OpenAI-compatible) or to a hosted API. The client names a
*model id*; the gateway decides which *endpoint* serves it. That split lets
one client call be load-balanced, failed over, priced and logged across
machines you own and machines you rent.

The backend lives under `apps/backend/` and imports as two top-level packages
(`pyproject.toml` sets `apps/backend` as the import root):

| Directory | Responsibility |
| --- | --- |
| `apps/backend/serving/` | HTTP surface, authentication and quota, request schemas, provider adapters, storage, observability |
| `apps/backend/routing/` | Route table, router strategies, endpoint health, circuit breaker, fallback |

## Design principles

1. **One model id, many endpoints.** Clients name a model; the gateway picks
   the machine.
2. **Route around failure instead of retrying into it.** A non-idempotent
   generation is never re-sent with the same key. Resilience comes from key
   rotation, the fallback chain and the circuit breaker.
3. **A caller learns nothing it is not entitled to.** Every access failure is
   the same `404`, and upstream errors are scrubbed before they reach a client.
4. **Configuration belongs to the deployment.** A deployment configures its
   model registry, routing policy and branding in Admin, which stores them in
   PostgreSQL. See [Configuration](#configuration).
5. **Extension points are declarative.** A new provider needs a `kind` and, if
   its API is not OpenAI-compatible, an adapter. A new routing strategy is a
   module in `apps/backend/routing/strategies/` that registers itself. Neither
   touches the request handler.

## The four layers

```text
                    ┌──────────────────────────────────────────┐
   HTTP client ────▶│  Serving layer  apps/backend/serving/     │
   (OpenAI SDK,     │                                          │
    Anthropic SDK,  │  middleware → auth/quota → model gate     │
    curl, IDE)      │  servers/app.py, servers/auth.py,         │
                    │  servers/routers/completions.py           │
                    └────────────────────┬─────────────────────┘
                                         │ model id + messages
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │  Routing layer  apps/backend/routing/     │
                    │                                          │
                    │  per-model router → weighted selection    │
                    │  circuit admission → automatic fallback   │
                    │  routers.py, model_router_registry.py,    │
                    │  endpoint_health.py                       │
                    └────────────────────┬─────────────────────┘
                                         │ chosen adapter
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │  Adapter layer                            │
                    │  apps/backend/serving/adapters/           │
                    │                                          │
                    │  translate request/response, own the      │
                    │  API key, normalize usage + errors        │
                    └────────────────────┬─────────────────────┘
                                         │ HTTPS
                                         ▼
                    ┌──────────────────────────────────────────┐
                    │  Providers: local vLLM / SGLang / Ollama, │
                    │  OpenRouter, Anthropic, Gemini, any       │
                    │  OpenAI-compatible service                │
                    └──────────────────────────────────────────┘

  Alongside every layer, both under apps/backend/serving/:
    storage/        Postgres operational store + request log (api_logs)
    observability/  structured logs, alert rules, Slack or WeCom alerts
```

How public traffic reaches the gateway (proxy, CDN, the console's rewrites) is
in [The public path table](public-path-table.md).

## Request lifecycle

`apps/backend/serving/servers/routers/completions.py` (chat completions) is the
reference path; the other inference surfaces reuse its parts.

### 1. Middleware

`create_app()` in `apps/backend/serving/servers/app.py` registers ten
middlewares. From the outside in:

1. `NoSniffMiddleware`: adds `X-Content-Type-Options: nosniff`.
2. `HandoffMiddleware`: counts requests for a release handoff and refuses new
   ones with `503` once the process is sealed; see
   [Handing traffic to a new gateway process](deployment.md#handing-traffic-to-a-new-gateway-process).
3. `RequestIdMiddleware`: the request id on every log line.
4. `FallbackErrorMiddleware`: shapes errors nothing else handled.
5. `KubernetesProxyTrustMiddleware`: discovers which proxies may report the
   client address, when Kubernetes discovery is configured.
6. `RequestLogMiddleware`: one structured log record per request.
7. `TimeoutMiddleware`: `REQUEST_TIMEOUT_SECONDS` (default 120) for ordinary
   requests, `STREAM_REQUEST_TIMEOUT_SECONDS` (default 3600; `0` or less
   removes the cap) once the response is streaming.
8. `SettingsCORSMiddleware`: CORS, reading `CORS_ALLOWED_ORIGINS` per request.
9. `TrailingSlashRedirectMiddleware`: `308` from a `/v1/…/` or
   `/anthropic/…/` path to the same path without the slash; see
   [The trailing-slash interaction](public-path-table.md#the-trailing-slash-interaction).
10. `RequestBodyLimitMiddleware`: `413` for a body over `REQUEST_MAX_BODY_MB`
    (default 32 MiB), checked against `Content-Length` and again as bytes
    arrive. Batch uploads use `BATCH_MAX_BODY_MB` instead. Control routes get
    256 KiB (`/auth/`, `/user/`, `/internal/`, `/v1/identity/`, `/v1/auth/`,
    `/v1/batch_webhook`) or 1 MiB (`/admin/`), because FastAPI decodes a body
    before authentication runs. The few routes that need a large body read it
    only after authentication.

Never add a middleware that buffers response bodies: it breaks Server-Sent
Events streaming.

`HTTP_IDLE_TIMEOUT_SECONDS` (default 600; `0` turns it off) closes an accepted
connection that sends no bytes in either direction for that long, including a
client that never finishes its headers. Uvicorn's keep-alive timer does not
cover that case. The value stays above the request and stream timeouts, so a
slow prefill still gets a `504`, not a dropped connection.

### 2. Authentication and quota

`verify_api_key` (`apps/backend/serving/servers/auth.py`) runs on every
inference endpoint and checks, in order:

1. **Agent grant tokens**, a separate credential namespace. A grant works only
   on inference paths; anything else gets `403 insufficient_scope`.
2. **Auth disabled.** With `USER_AUTH_ENABLED` false, the caller is an
   anonymous admin. Authentication is on unless explicitly turned off.
3. **API key.** Keys look like `hyi-<token>` and are stored only as an
   HMAC-SHA256 digest keyed by `API_KEY_SECRET`. Send one as
   `Authorization: Bearer …` or `X-API-Key`.

It also enforces the key's budgets and the project's and account's quotas,
answering `429` from one shared builder (`apps/backend/serving/quota.py`).
`enforce_user_concurrency` then holds a per-user in-flight slot for the whole
request. It reads the body first (a concurrency-exempt model draws on its own
budget), and a caller may have at most four bodies being read at once.

The quota check reads the counter tables (`user_daily_cost`,
`api_key_daily_cost`). The spend ledger (`apps/backend/serving/spend_ledger.py`)
adds what they do not show yet:

- **Requests in flight.** Admission reserves each request's estimated cost and
  releases it when the request ends. A request is refused only when not even
  `ESTIMATED_REQUEST_COST_USD` fits; an admitted one reserves up to
  `MAX_RESERVED_REQUEST_COST_USD`, so concurrent requests cannot all spend the
  same remaining budget.
- **Costs not yet written.** One writer per store adds finished costs to the
  counters in batches. The ledger keeps counting a cost until its write is
  confirmed, so the delay never lets a request past a limit.

A cost the counters refuse (for example one that would overflow) is logged as
`cost_counter_write_refused` and alerted for an operator to reconcile. One
request adds at most $1,000; a larger cost is capped and logged as
`cost_above_request_ceiling`.

The ledger is per process, so limits hold within each worker but not across
workers; production runs one. A shared spend mode, off unless an operator turns
it on, keeps reservations in PostgreSQL for all processes; see
[Shared accounting](accounting.md#shared-accounting).

A response cut off by the client or the request timeout is billed for what the
upstream already did: its reported usage, or an estimate marked
`metadata.usage_estimated`. A request the upstream fails is not billed.

Read-only endpoints such as `/v1/models` use `optional_verify_api_key`: a
missing or invalid key is anonymous, and the key only decides what is listed.

### 3. Model gate

Before any provider is contacted, the handler answers `404` if the model is not
in the route table, is unpublished, needs a role the caller lacks, was disabled
for the user, or is outside the scope of the caller's key, project, user tags
or inference grant. All cases return the same "model not found" body, so a
caller cannot probe for models by comparing `403` with `404`. The check is
`model_access_denial` in `apps/backend/serving/model_access.py`.

### 4. Router selection

`ModelRouterRegistry` (`apps/backend/routing/model_router_registry.py`) builds
and caches one router per model from its `router` and `router_params`. A model
without `router` uses `default_router` from the routing policy
(`ROUTING_POLICY`).

The `fixed` router drops routes with weight `0` (an admin-disabled provider
counts as `0`), routes that cannot take the request's media and routes with an
open circuit, then draws one by weight, biased away from endpoints busy with
prefill. A 5-minute per-caller affinity keeps a conversation on the endpoint
that holds its prefix cache. An admin can pin a request with `X-Route-Pin:
<provider-or-endpoint-id>`; pinned requests never fall back. See
[How `FixedRouter` picks an endpoint](routing.md#how-fixedrouter-picks-an-endpoint).

### 5. Dispatch, fallback, and the circuit breaker

The router calls the adapter it chose through a thin wrapper, so an admin edit
cannot redirect a request already on its way. On success the response carries
an internal `_routing` block (provider, base URL, `endpoint_id`). On failure the
router tries the model's remaining routes in order; every attempt is recorded
in `failed_attempts` for the request log, and if all fail the client gets the
*primary* error.

Each `endpoint_id` has a circuit breaker
(`apps/backend/routing/endpoint_health.py`). Client errors other than 408, 429,
401 and 407 never trip it: 408 and 429 mean overload, and 401 and 407 reject the
gateway's own credential. Thresholds and the per-caller rule are in
[Endpoint health and circuit breaking](routing.md#endpoint-health-and-circuit-breaking).

### 6. Logging

`CompletionsLogger` (`apps/backend/serving/servers/routers/completions_logging.py`)
writes the `api_logs` row in the background and hands a `RoutingObservation`
back to the router. `FixedRouter` ignores it; learning routers update their
cost model.

## The routing engine in detail

Diagrams are in
[Router types and execution boundaries](routing.md#router-types-and-execution-boundaries).

### Routers and strategies

| Module in `apps/backend/routing/strategies/` | What it is |
| --- | --- |
| `fixed.py` | The `fixed` strategy: weighted-random selection with fallback, implemented by `FixedRouter` in `apps/backend/routing/routers.py` |
| `routewise.py` | The `routewise` strategy: a cost-aware router that learns from `RoutingObservation` feedback, implemented under `apps/backend/routing/routewise/` |
| `weight.py` | Not a per-model router. `FixedRatioStrategy` splits a weight budget between local and remote endpoint groups for `RoutingManager` |

`router` accepts `fixed` or `routewise`; any other name is refused when the
catalog or routing policy is saved. Each strategy's params model sets
`extra="forbid"`, so a typo in `router_params` is refused rather than ignored.
To add one, see [Adding a routing strategy](routing.md#adding-a-routing-strategy).

### Two layers of "strategy"

```text
routing config          default_router: fixed
   │                    (+ local_deployment / remote_deployment pools)
   ▼
RoutingManager ──uses──▶ FixedRatioStrategy      → rewrites per-adapter WEIGHTS
   (routing/manager.py)  (strategies/weight.py)    for models whose endpoints
                                                   appear in those pools

model registry          router: fixed | routewise
   │                    router_params: {...}
   ▼
ModelRouterRegistry ──▶ build_router()           → chooses WHICH ROUTER runs
   (model_router_registry.py)                      for one model's requests
```

`RoutingManager` rewrites weights from the routing policy's local/remote split
once at startup, and only when `default_router` is `fixed`. A gateway with a
database still runs it, but selects from the catalog weights plus Admin
overrides, so the rewrite has no effect there; see
[Routes excluded from selection](routing.md#routes-excluded-from-selection).

### Execution and composition

Every router runs its chosen endpoint through a thin wrapper called a leaf. An
experimental composition, `HybridRouter`, splits a model's routes into a local
and a cloud pool; a model opts in with `router_params.hybrid_composition: true`.
See [Hybrid composition](routing.md#hybrid-composition).

### `provider` versus `endpoint_id`

`provider` is a label on the route. It defaults to the adapter kind and can be
overridden per route. `api_logs.provider`, per-provider dashboards and the
admin disable switch all use it. A route may not borrow a label that belongs to
an adapter kind.

`endpoint_id` identifies one endpoint of one model: `{model_id}:{location}`.
A host of `localhost`, `127.0.0.1`, `0.0.0.0` or `host.docker.internal` gives
`local-<port>` (or `local`); anything else is named from the kind or hostname,
for example `<model>:openrouter-api`. Latency profiles, availability and
circuit state are keyed on it. Do not read ownership from the suffix: a
gateway-owned server on a LAN address gets `<name>-api`, and an admin-supplied
`route_id` is used as written.

## Adapters

An adapter talks to one provider: it builds the URL and headers, holds the API
key or key pool, translates the request and response, normalizes usage and
raises errors the router understands. `_make_adapter` in
`apps/backend/serving/servers/registry.py` builds adapters from the model
registry, first checking factories registered by
[backend extensions](backend-extensions.md).

| Module | Class | Covers |
| --- | --- | --- |
| `openai_compat.py` | `OpenAICompatAdapter` | Every OpenAI-compatible service, including local vLLM, SGLang, and Ollama. Local inference servers have no dedicated adapter |
| `openrouter.py` | `OpenRouterAdapter` | OpenRouter; see [OpenRouter routes](adding-models.md#openrouter-routes) |
| `anthropic.py` | `AnthropicAdapter` | The Anthropic Messages API directly |
| `claude.py` | `ClaudeAdapter` | Claude served through Google Vertex |
| `gemini.py` | `GeminiAdapter` | The Gemini API |

A route with `api_keys:` (a list) gets a key pool. A key that fails with 429,
401, 402, 403, 408, 425, a 5xx or a timeout hands the request to the next key;
a key is muted for 20 seconds only when no untried key is left. Request errors
such as 400 and 422 are not rotated, since they fail on every key. A completion
is never re-sent to the same key, because that would bill it twice.

### Streaming timeouts

The gateway has to notice an upstream that stops sending but keeps the socket
open; otherwise every stream on that endpoint dies together when an outer
proxy gives up. These clocks fire in order, innermost first:

| Setting | Default | Covers |
|---|---|---|
| `STREAM_IDLE_TIMEOUT_SECONDS` | 180s | Gap between two data-bearing SSE frames, after the first |
| `STREAM_MAX_IDLE_S` | 240s | Same gap on `/v1/messages`, counted in *forwarded* frames |
| `STREAM_MAX_FIRST_FRAME_IDLE_S` | 300s | First frame on `/v1/messages`; never less than `STREAM_MAX_IDLE_S` |
| `STREAM_FIRST_BYTE_TIMEOUT_SECONDS` | unset | Connect to first frame, i.e. prefill |
| `STREAM_REQUEST_TIMEOUT_SECONDS` | 3600s | The whole streaming response |

Prefill and inter-chunk time have separate budgets because a 1M-token prompt
can take minutes to reach its first token while later chunks arrive
milliseconds apart. `0` turns off either of the first-byte and inter-chunk
clocks. `/v1/messages` is looser because a buffered XML tool call can hold back
a forwarded frame for over a minute. An expired inter-chunk budget raises
`UpstreamStreamIdleError`, which counts against the endpoint's circuit instead
of ending the stream with a fake `[DONE]`.

## Configuration

Application documents are validated JSON settings in `app_config`:
`MODEL_CATALOG`, `ROUTING_POLICY`, `ALERT_RULES`, `SITE_CONFIGURATION`,
`SITE_BRANDING` and `SSO_PROVIDERS`. No YAML file is read at runtime, and a
fresh gateway serves no models until an administrator configures it.
Credentials are secret settings that documents reference as `${NAME}`.

Admin route edits apply across backend processes without a restart, and
requests in flight keep the router and feedback they started with. Catalog,
routing-policy and alert-rule changes need a restart. See
[Configuration](configuration.md) and the
{download}`accepted design <../agents/specs/2026-11-08-database-only-configuration.md>`.

## Storage

`apps/backend/serving/storage/base.py` defines `OperationalStore` (accounts,
API keys, roles, quotas, admin-managed providers and routes, runtime settings)
and `LogStore` (the request log). Postgres implements both
(`postgres_operational.py`, `postgres_log.py`); `CachedOperationalStore` caches
the operational store because authentication reads it on every request.

The schema comes only from the migrations in `apps/backend/migrations/`. A
deploy applies them before restarting the backend, and the backend refuses to
start on a database they have not reached; see [Database](database.md). Useful
`api_logs` columns: `request_id`, `model_id`, `provider`, `served_model_id` and
`served_endpoint_id` (what actually answered), `ttft_ms`, `latency_ms`, token
counts, `cost_usd` (what the caller is billed) and `upstream_cost_usd` (what
the upstream reported).

With `DB_ENABLED=false` the gateway reads its documents from environment
values and still routes requests, without request logging or accounts. Stage 1
of the [Quickstart](router-tutorial.md) runs this way.

## Observability

There is no Prometheus exporter. The gateway reports through:

- **Structured logs**, one record per request, formatted by
  `apps/backend/serving/utils/logging.py`. Health-probe noise is hidden unless
  `LOG_LEVEL=DEBUG`.
- **The request log** (`api_logs`), the source for admin dashboards and usage.
- **Health endpoints.** `/health` returns 200 (`status: "degraded"` when a
  store is down) and 503 only when every store is unreachable. `/health/ready`
  returns 503 unless everything is up. `/health/deep` adds per-endpoint
  availability and circuit state. Anonymous callers see only `status`; all
  three share one store probe for two seconds.
- **Alerts.** `alert_slack()` and `alert_on_transition()` in
  `apps/backend/serving/observability/alerts.py` post to one Slack or WeCom
  webhook, set in **Admin → Settings → Operations**. Thresholds come from
  `ALERT_RULES`. A channel change needs a restart unless the `alerts.runtime`
  live group is active; see
  [Settings that apply without a restart](configuration.md#settings-that-apply-without-a-restart).
  The circuit breaker pages on `circuit_open` and on rejected upstream
  credentials, with a cooldown per endpoint.
- **Alert history**, on when there is a database (`ALERT_HISTORY_ENABLED=false`
  turns it off), records every alert decision without changing what is sent;
  see [Alert history](alert-history.md).

## HTTP surface

| Group | Paths | Auth |
| --- | --- | --- |
| OpenAI-compatible inference | `POST /v1/chat/completions`, `POST /v1/completions`, `POST /completion`, `POST /v1/embeddings`, `POST /v1/responses`; `GET` and `DELETE /v1/responses/{id}` | API key |
| Anthropic-compatible inference | `POST /v1/messages`, `POST /anthropic/v1/messages`, `…/count_tokens` | API key |
| [Batch inference](batches.md) | `POST` and `GET /v1/batches`, `GET` and `DELETE /v1/batches/{id}`, `POST /v1/batches/{id}/cancel`, `POST /v1/batches/{id}/retry-failed`, `GET /v1/batches/{id}/results`, `GET /v1/batches/{id}/output.jsonl` | API key; creating a batch needs `BATCH_MIN_ROLE` (default `internal`) |
| [Batch files](batches.md#batches-from-a-file) | `POST` and `GET /v1/files`, `GET` and `DELETE /v1/files/{id}`, `GET /v1/files/{id}/content` | API key; uploading needs `BATCH_MIN_ROLE` |
| [Batch webhooks](batches.md#webhooks) | `GET`, `PUT` and `DELETE /v1/batch_webhook`, `GET /v1/batch_webhook/events` | API key; changing a webhook needs `BATCH_MIN_ROLE` |
| Model catalog | `GET /v1/models`, `GET /models`, `GET /openrouter/models`, `GET /anthropic/v1/models` | Optional: a key only widens what is listed |
| Balance probes | `GET /v1/usage`, `GET /v1/dashboard/billing/subscription`, `GET /v1/dashboard/billing/usage`, `GET /v1/dashboard/billing/credit_grants`, `GET /v1/sub2api/billing`, `GET /anthropic/user/balance` | API key; answers even when the quota is spent |
| API key description | `GET /v1/key` | API key |
| Health | `GET /health`, `/health/ready`, `/health/deep` | None; upstream addresses only for an admin |
| Public routing disclosure | `GET /routing` | None; upstream addresses only for an admin |
| Admin | `GET /admin/routing`, `GET /admin/stats` and the rest of `/admin/*` | Admin |
| Accounts and console APIs | `/auth/*`, `/user/*`, `/site-config` | Mixed |

- `BATCH_ENABLED` switches every batch, file and webhook route; when it is off
  they answer `404` and no queued request starts.
- `GET /routing` lists each published route's provider label and weight.
  Upstream base URLs and probe results appear only with an administrator's JWT
  or `ADMIN_TOKEN` as a bearer token; `GET /admin/routing` also lists
  unpublished routes.
- `/v1/models` answers an Anthropic-family client in the Anthropic shape;
  `/models` and `/openrouter/models` always use the OpenAI shape.
- The balance probes serve the quota checks of new-api, one-api, sub2api and
  cc-switch. They report what is left under the tightest of the daily quota,
  the monthly quota and the key's and project's budgets (`/anthropic/user/balance`:
  the daily quota only). Remaining is `0` exactly when a request would be
  refused, and the answer is still HTTP 200.
- `GET /v1/key` shows the key's project, models, budgets and
  `concurrency.max_in_flight` (`null` when unlimited). A project cap is shared
  by all the project's keys, so a key can get `429` below its own limit.

## Deployment shape

`deploy/docker/` holds `Dockerfile.backend`, `Dockerfile.frontend` (the Next.js
console in `apps/frontend/`) and `docker-compose.yml`; systemd units are in
`deploy/systemd/`. The console and the API share one origin: console route
handlers forward `/v1`, `/anthropic`, `/auth`, `/user`, `/admin` and the
published `/internal` paths, and `apps/frontend/next.config.js` rewrites
`/health`, `/site-config`, `/site-updates` and `/public-stats`. Together they
are [the public path table](public-path-table.md). The frontend has its own
quality gates, separate from the Python `make` targets.

Next: [Deployment Guide](deployment.md), [Installation](installation.md),
[Contributing](contributing.md).
