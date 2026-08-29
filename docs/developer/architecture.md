# Architecture

HybridInference is a FastAPI gateway that speaks the OpenAI/OpenRouter HTTP API
and dispatches each request to one of several interchangeable upstreams: a local
inference server (vLLM, SGLang, Ollama, or anything else OpenAI-compatible) or a
hosted API. The point of the gateway is that a *model id* the client asks for is
decoupled from the *endpoint* that actually serves it, so the same client call
can be load-balanced, failed over, priced, and logged across a mix of machines
you own and machines you rent.

The backend is one Python package tree under `apps/backend/`, split in two:

| Directory | Responsibility |
| --- | --- |
| `apps/backend/serving/` | HTTP surface, authentication and quota, request schemas, provider adapters, storage, observability |
| `apps/backend/routing/` | Route table, router strategies, endpoint health, circuit breaker, fallback |

Both are importable as top-level packages (`serving.*`, `routing.*`); the import
root is `apps/backend`, declared in `pyproject.toml`.

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
    observability/  structured logs, alert rules, Slack alerting
```

For how a public deployment gets traffic *to* the gateway — reverse proxy, CDN,
and the console's own path rewrites — see
[Edge and console routing](edge-and-console-routing.md). Nothing in this page
depends on that topology; the gateway is an ordinary HTTP server.

## Request lifecycle

The chat-completions path in
`apps/backend/serving/servers/routers/completions.py` is the canonical one; the
other inference surfaces reuse its parts.

### 1. Middleware

`create_app()` in `apps/backend/serving/servers/app.py` registers five
middlewares. Starlette runs the last-registered outermost, so the effective
order from the outside in is:

1. `RequestIdMiddleware` — mints or adopts a request id used by every log line.
2. `FallbackErrorMiddleware` — last-resort error shaping.
3. `RequestLogMiddleware` — one structured log record per HTTP request.
4. `TimeoutMiddleware` — `REQUEST_TIMEOUT_SECONDS` (default 120s) for ordinary
   requests, `STREAM_REQUEST_TIMEOUT_SECONDS` (default 3600s, `<=0` disables the
   cap) once a response has been marked as streaming.
5. `CORSMiddleware`.

Middleware order is load-bearing: streaming responses are `StreamingResponse`
objects that must not be buffered. Any new middleware that reads a response body
before forwarding it will break Server-Sent Events.

### 2. Authentication and quota

`verify_api_key` in `apps/backend/serving/servers/auth.py` is a FastAPI
dependency on every inference endpoint. It resolves, in order:

- **Agent grant tokens** — a separate credential namespace, checked first.
  A grant may only be used on inference paths; anything else gets `403
  insufficient_scope`.
- **Auth disabled** — when `USER_AUTH_ENABLED` is falsy the caller is treated as
  an anonymous admin. The setting is fail-closed: auth is on unless explicitly
  turned off.
- **API key** — keys are minted as `hyi-<url-safe token>` and stored only as an
  HMAC-SHA256 digest keyed by `API_KEY_SECRET`. The key may arrive as
  `Authorization: Bearer …` or `X-API-Key`.

The same dependency enforces the caller's daily spend quota and returns `429`
with a shared payload builder (`apps/backend/serving/quota.py`) whichever door
the request came through. A separate `enforce_user_concurrency` dependency holds a
per-user in-flight slot for the duration of the request.

Read-only endpoints such as `/v1/models` use `optional_verify_api_key` instead:
a missing or invalid key is anonymous rather than rejected, and the key only
decides which models are visible.

### 3. Model gate

Before any provider is contacted, the handler rejects the request with `404` if
the model is not in the route table, is unpublished, requires a role the caller
does not have, was disabled for that user, or is outside a grant's scope. All
five conditions return the same "model not found" body on purpose, so a caller
cannot enumerate models it is not entitled to by reading the difference between
`403` and `404`.

### 4. Router selection

Each model resolves to a router instance through `ModelRouterRegistry`
(`apps/backend/routing/model_router_registry.py`), which reads the model's
`router:` / `router_params:` fields from the model registry and caches one
router per model id. A model with no `router:` uses the `default_router` value
from the routing config.

The router then picks one adapter for this request (`FixedRouter._select_adapter`):

- routes whose provider is admin-disabled carry weight 0 and are skipped;
- routes whose input modalities cannot accept the request's media are excluded;
- routes whose circuit is open are not admitted — if that leaves nothing, the
  request fails with `AllCircuitsOpenError`;
- among the survivors, selection is weighted-random, biased away from endpoints
  already saturated with prefill work;
- a short-lived per-caller affinity (5 minutes) re-pins a conversation to the
  endpoint that already holds its prefix cache, unless that endpoint has built
  up a backlog.

An admin may bypass selection entirely with an `X-Route-Pin` request header
naming a provider label or `endpoint_id`. A pinned request never falls back —
a silent switch would make the pin meaningless.

### 5. Dispatch, fallback, and the circuit breaker

`FixedRouter.chat_completion` / `stream_chat_completion` call the chosen
adapter. On success the endpoint is recorded healthy and the response carries an
internal `_routing` block (provider, base URL, `endpoint_id`).

On failure the endpoint records a failure and — unless the caller pinned a
provider — the router walks the model's remaining route legs in route order,
skipping legs that are disabled, modality-incompatible, or circuit-open, and
tries each in turn. Every attempt is appended to a `failed_attempts` list that
travels with the eventual response or error, so the request log attributes the
failure to real upstreams rather than to the router. If every leg fails, the
*primary* error is re-raised.

Circuit state lives per `endpoint_id` in
`apps/backend/routing/endpoint_health.py`:

| Knob | Env var | Default |
| --- | --- | --- |
| Consecutive failures that open a circuit | `CIRCUIT_FAILURE_THRESHOLD` | 3 |
| Seconds an open circuit waits before a half-open probe | `CIRCUIT_COOLDOWN_SECONDS` | 30 |
| Availability floor | `CIRCUIT_MIN_AVAILABILITY` | 0.7 |
| EWMA smoothing for the availability estimate | `ROUTER_HEALTH_EWMA_ALPHA` | 0.1 |

Client errors do not trip the breaker: a 4xx other than 408, 429, 401, and 407
is the caller's problem, and counting it would let one malformed request take an
endpoint away from everybody. 408 and 429 signal upstream overload and do count;
401 and 407 are unambiguous rejections of *the gateway's own* credential and
count too, because no user can fix them.

Separately, `RoutingManager` can start a `HealthMonitor` that polls the
`/health` path of local endpoints listed in the routing config, on the
`health_check` interval. It only probes local deployments — remote providers do
not serve that path and would be marked unhealthy for no reason.

### 6. Logging

After the response is produced, `CompletionsLogger`
(`apps/backend/serving/servers/routers/completions_logging.py`) schedules two fire-and-forget
side effects: a row in `api_logs`, and a `RoutingObservation` handed back to the
router. `FixedRouter` ignores observations; online-learning routers use them to
update their cost model.

## The routing engine in detail

### Routers and strategies

There are exactly three modules under `apps/backend/routing/strategies/`, and
they are not all the same kind of thing:

| Module | What it is |
| --- | --- |
| `fixed.py` | Registers the `fixed` strategy: weighted-random selection with automatic fallback, implemented by `FixedRouter` in `apps/backend/routing/routers.py` |
| `routewise.py` | Registers the `routewise` strategy: a cost-aware router that learns from `RoutingObservation` feedback, implemented under `apps/backend/routing/routewise/` |
| `weight.py` | Not a per-model router at all. `FixedRatioStrategy` splits a *weight budget* between the local and remote endpoint groups; it is used by `RoutingManager`, not by the router registry |

`fixed` and `routewise` are the only values `router:` accepts. Selecting an
unregistered name fails configuration validation with a message listing the
known strategies. Each strategy declares a Pydantic params model with
`extra="forbid"`, so a typo in `router_params:` fails at boot instead of
silently falling back to a default.

`apps/backend/routing/executor.py` is a backward-compatibility shim that
re-exports `FixedRouter` under its old name `RouteExecutor`. Edit
`apps/backend/routing/routers.py` instead.

### Two layers of "strategy"

This is the part that most often confuses newcomers:

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

`RoutingManager` only rewrites weights when the effective strategy is `fixed`;
it returns without touching anything otherwise. Per-route weights declared in
the model registry already encode a local/remote split, so a deployment that
does not list endpoint pools in its routing config simply keeps its declared
weights.

### `provider` versus `endpoint_id`

Two identifiers look similar and mean different things.

`provider` is a label on the route. It defaults to the adapter kind and can be
overridden per route with `provider:` in the model registry. It is what
`api_logs.provider` records, what per-provider dashboards group by, and what the
admin disable switch targets. Two local vLLM boxes can carry different provider
labels so their traffic stays in separate cohorts. A route may not borrow a
label that `_make_adapter` already derives from a kind — `RESERVED_PROVIDER_LABELS`
in `apps/backend/serving/servers/registry.py` rejects those.

`endpoint_id` is the unique key for *one endpoint of one model*, minted by
`_make_provider_id` as `{model_id}:{location}`:

- a host in `_LOCAL_HOSTS` (`localhost`, `127.0.0.1`, `0.0.0.0`,
  `host.docker.internal`) yields `local-<port>`, or `local` when there is no
  port;
- otherwise the location is derived from the kind or hostname, e.g.
  `<model>:openrouter-api`.

Latency profiles, availability tracking, and circuit-breaker state are all keyed
on `endpoint_id`, which is why two route legs pointing at the same base URL with
different pinned upstreams still get independent breakers.

The suffix is not an ownership signal. A gateway-owned server on a LAN address
is stamped from its hostname like any remote service, and an admin-supplied
`route_id` becomes the `endpoint_id` verbatim. Do not infer "is this machine
mine?" from the string.

## Adapters

An **adapter** is the object that knows how to talk to one provider: it builds
the URL and headers, holds the API key (or a rotating pool of keys), translates
the request body and the response, normalizes usage accounting, and raises
errors in a shape the router understands. Adapters live in
`apps/backend/serving/adapters/` and are constructed from the model registry by
`_make_adapter` in `apps/backend/serving/servers/registry.py`.

| Module | Class | Covers |
| --- | --- | --- |
| `openai_compat.py` | `OpenAICompatAdapter` | Every OpenAI-compatible service, including local vLLM, SGLang, and Ollama. Local inference servers have no dedicated adapter |
| `openrouter.py` | `OpenRouterAdapter` | OpenRouter; see [Routing through OpenRouter](openrouter.md) |
| `anthropic.py` | `AnthropicAdapter` | The Anthropic Messages API directly |
| `claude.py` | `ClaudeAdapter` | Claude served through Google Vertex |
| `gemini.py` | `GeminiAdapter` | The Gemini API |
| `coding_identity.py` | `CodingIdentityAdapter` | OpenAI-compatible providers that gate access on a coding-tool identity |

`_make_adapter` maps a route's `kind:` onto one of these and pre-seeds
provider-specific configuration — a usage profile, a non-standard chat path, or
whether it is safe to send `stream_options: {include_usage: true}`. Adding a
provider that is already OpenAI-compatible usually means adding a `kind` here
rather than writing a new class; see [Adding a New Model](adding-models.md).

Key rotation is an adapter concern. When a route declares `api_keys:` (plural),
`OpenAICompatAdapter` draws from a pool: a key that hits a key-specific or
transient failure (429, 401/402/403, 408/425, 5xx, timeouts) is muted for five
minutes and the request advances to the next key. Request-scoped errors such as
400 and 422 fail on every key, so they propagate immediately instead of burning
the pool. A completion POST is never retried against the *same* key — re-sending
a non-idempotent generation would double-bill it. Resilience comes from the
router's fallback chain, not from blind retries.

## Configuration

Three YAML files describe a deployment: a model registry, a routing config, and
an alert config. **They are not at fixed paths.** `resolve_config_path()` in
`apps/backend/serving/config/distribution.py` resolves each one through a
three-step precedence:

1. **An explicit environment variable** — `MODELS_CONFIG_PATH`,
   `ROUTING_CONFIG_PATH`, `ALERTS_CONFIG_PATH`. Always wins.
2. **The distribution manifest** — a single versioned YAML naming the site
   identity and the config-file locations, pointed at by
   `DISTRIBUTION_CONFIG_PATH`. Relative `paths:` in the manifest resolve against
   the manifest's own directory, so a deployment overlay under
   `distributions/<name>/` is self-contained.
3. **Built-in defaults** — `config/examples/models.openrouter.yaml` and
   `config/examples/routing.minimal.yaml`. This is what a fresh clone with no
   environment and no overlay gets: supply one `OPENROUTER_API_KEY` and the
   gateway serves a working catalog. The alert default is `config/alerts.yaml`,
   a path this repository deliberately does not ship — with no alert file
   present the built-in thresholds apply.

The manifest is opt-in and defaults to a dry run. With `DISTRIBUTION_CONFIG_PATH`
set but `DISTRIBUTION_CONFIG_MODE` unset, the mode is `dark`: the manifest is
loaded, validated, and compared against the paths that are actually in effect —
logging a per-file digest comparison — while resolution stays unchanged. Setting
`DISTRIBUTION_CONFIG_MODE=active` makes manifest paths effective, and in that
mode a manifest that fails to load stops the process rather than quietly serving
a different registry than the deployment named.

All three YAML files support environment interpolation: `${VAR}` and
`${VAR:-default}`. A route whose `api_key`, `api_keys`, or `base_url` expands to
nothing is either skipped (if the route is marked optional) or fails startup —
never registered as a dead endpoint.

See [Configuration](configuration.md) for the field-by-field reference and
[Quickstart](router-tutorial.md) for a working end-to-end example.

## Storage

Persistence is defined by two abstract base classes in
`apps/backend/serving/storage/base.py`:

- **`OperationalStore`** — accounts, API keys, roles, quotas, admin-managed
  providers and routes, runtime settings.
- **`LogStore`** — the request log.

Postgres implements both (`postgres_operational.py`, `postgres_log.py`).
`CachedOperationalStore` wraps the operational store with an in-process cache,
because auth resolves against it on every single request.

The request log table `api_logs` and its hourly rollup are defined in exactly
one place, `apps/backend/serving/storage/log_schema.py`, applied by both code paths that create
tables. Columns worth knowing: `request_id`, `model_id`, `provider`,
`served_model_id` / `served_endpoint_id` (which endpoint actually answered, as
opposed to what the client asked for), `ttft_ms` and `latency_ms`, the token
counts, `cost_usd` (what the caller is billed, from the model's own pricing) and
`upstream_cost_usd` (what the upstream reported, when it reports one).

Schema migrations read the catalog first and issue only the DDL that is actually
missing, under a bounded lock wait — an `ALTER TABLE` that queues behind a long
query would block every reader behind it. See [Database](database.md).

The gateway starts without a database. `/health` then reports
`database_connected: false`, and request logging and accounts are unavailable,
but routing and completions still work — which is what makes the router
tutorial's first stage runnable with no Postgres at all.

## Observability

There is no Prometheus exporter; it was removed. The supported surfaces are:

- **Structured logs.** `RequestLogMiddleware` emits one record per HTTP request;
  `apps/backend/serving/utils/logging.py` shapes them and suppresses noise from health-probe
  paths unless `LOG_LEVEL=DEBUG`.
- **The request log.** `api_logs` is the durable record and the source for the
  admin dashboards and usage reporting.
- **Health endpoints.** `/health` is a liveness check that returns 200 with
  `status: "degraded"` when a configured store is down but traffic can still be
  served, and 503 only when every configured store is unreachable.
  `/health/ready` is the strict variant: 503 unless everything configured is up.
  `/health/deep` adds per-endpoint availability and circuit state, and reports
  `degraded` when any endpoint is.
- **Alerting.** `alert_slack()` and `alert_on_transition()` in
  `apps/backend/serving/observability/alerts.py` post to a Slack webhook
  (`SLACK_ALERTS_WEBHOOK_URL`), and to an on-call relay when
  `CODEX_ONCALL_RELAY_URL` / `CODEX_ONCALL_RELAY_TOKEN` are set. Thresholds come
  from the alert config resolved by the same `resolve_config_path()` chain; with
  no alert file present the built-in thresholds apply. The circuit breaker pages
  on `circuit_open` transitions and on upstream-credential rejections, with
  per-endpoint cooldowns so a persistent fault re-pages on a fixed cadence
  instead of flooding.

## HTTP surface

Client-facing groups, all served by the same app:

| Group | Paths | Auth |
| --- | --- | --- |
| OpenAI-compatible inference | `POST /v1/chat/completions`, `POST /v1/completions`, `POST /completion`, `POST /v1/embeddings`, `POST /v1/responses` | API key |
| Anthropic-compatible inference | `POST /v1/messages`, `POST /anthropic/v1/messages`, `…/count_tokens` | API key |
| Model catalog | `GET /v1/models`, `GET /models`, `GET /openrouter/models`, `GET /anthropic/v1/models` | Optional — a key only widens what is listed |
| Health | `GET /health`, `/health/ready`, `/health/deep` | None |
| Routing disclosure | `GET /routing`, `GET /admin/routing` | **None at this commit** |
| Admin | `GET /admin/stats` and the rest of `/admin/*` | Admin |
| Accounts and console APIs | `/auth/*`, `/user/*`, `/site-config` | Mixed |

```{warning}
`GET /routing` and `GET /admin/routing` carry no authentication dependency at
this commit. Both return every route's provider label, **upstream base URL**, and
traffic weight; `/admin/routing` additionally includes unpublished routes. If
your gateway is reachable from the internet, block these two paths at your
reverse proxy unless you intend to publish your upstream topology.
```

`/v1/models` shape-shifts by client: an Anthropic-family client calling it
receives the Anthropic list response, while `/models` and `/openrouter/models`
always return the OpenAI/OpenRouter shape.

## Deployment shape

The repository ships Dockerfiles and Compose files under `deploy/docker/`:
`Dockerfile.backend` (the gateway), `Dockerfile.frontend` (the Next.js console
in `apps/frontend/`), `Dockerfile.rocm` (an AMD GPU variant), and
`docker-compose.yml`. Systemd units live in `deploy/systemd/`.

The console and the API share one origin: the Next.js rewrites in
`apps/frontend/next.config.js` proxy `/v1`, `/anthropic`, `/auth`, `/user`,
`/admin`, `/health`, and `/site-config` to the backend, so a browser session and
an API key reach the same paths on the same host. Those rewrites — not a reverse
proxy config — are the public path table; see
[Edge and console routing](edge-and-console-routing.md). The frontend has its
own toolchain and quality gates, separate from the Python `make` targets.

See [Deployment Guide](deployment.md) to run it, [Installation](installation.md) for a
local checkout, and [Contributing](contributing.md) before sending a change.

## Design principles

1. **One model id, many endpoints.** Clients name a model; the gateway owns
   which machine serves it. Everything else follows from that.
2. **Failure is routed around, not retried into.** A non-idempotent generation
   is never re-sent to the same endpoint; resilience comes from the fallback
   chain and the circuit breaker.
3. **A caller learns nothing it is not entitled to.** Access failures collapse
   to a uniform `404`, and upstream errors are scrubbed before they reach a
   client.
4. **Configuration is a deployment's, not the project's.** The repository ships
   runnable examples; a real deployment supplies its own registry and routing
   files through the resolution chain above.
5. **Extension points are declarative.** New provider: a `kind` and, if the API
   is not OpenAI-compatible, an adapter. New routing behaviour: a module in
   `apps/backend/routing/strategies/` that self-registers. Neither requires touching the request
   handler.
