# Routing

Routing is what HybridInference is for. A client asks for one public model id;
the gateway decides which upstream endpoint actually serves it, retries a
different one when that fails, stops sending traffic to endpoints that are
down, and keeps a conversation on the endpoint whose prefix cache is already
warm.

This page describes the engine, its knobs, and how to add a routing strategy of
your own. For where the config files live and how they are found, see
[Configuration](configuration.md); for the shape of a model entry and its
`route:` list, see [Adding a New Model](adding-models.md). How a router hands a
request to the adapter it chose is in [Routing Internals](routing-internals.md).

## Choosing a router per model

The routing file's `default_router:` names the strategy for models that do not
choose one. A model entry in the registry overrides it:

```yaml
models:
  - id: <model-id>
    provider: openai_compat
    router: fixed            # strategy name; omit to use default_router
    route:
      - kind: openai_compat
        weight: 0.7
        base_url: http://localhost:8000/v1
        provider_model_id: <served-model-name>
      - kind: openai_compat
        weight: 0.3
        base_url: https://api.your-provider.example/v1
        api_key: ${YOUR_PROVIDER_API_KEY}
        provider_model_id: <upstream-model-id>
```

Two strategies are registered out of the box:

- **`fixed`** — weighted random selection with automatic fallback. This is the
  default and the only one that needs no extra dependency. The only one of its
  settings that has an effect today, `router_params.hybrid_composition`
  (default `false`), turns on an experimental local/cloud composition described in
  [Routing Internals](routing-internals.md).
- **`routewise`** — a cost-aware strategy that lives in a separate package; see
  [RouteWise](#routewise) below.

`router_params:` holds settings for the chosen strategy. An unknown strategy
name stops startup with a message listing the known strategies, and a key the
strategy does not accept stops it with a message naming that key. `ENABLE_ROUTEWISE=true` is an older switch that makes
`routewise` the default when the routing file leaves `default_router` at
`fixed`.

An alias shares its model's router, so a router that learns from traffic sees
the model's requests and its aliases' requests together.

## How `FixedRouter` picks an endpoint

For each request the `fixed` router narrows the model's routes in this order:

1. **Route must be published and non-empty.** Otherwise there is no route and
   the request 404s.
2. **Modality filter.** A request carrying non-text input keeps only routes
   whose `input_modalities` cover it. If none do, `AllCircuitsOpenError` is
   raised naming the required modalities.
3. **Explicit pin.** `X-Route-Pin: <provider-or-endpoint-id>` on
   `/v1/chat/completions` selects that route directly and disables fallback —
   an admin debugging aid, not a client feature. A pin that matches nothing
   raises `ProviderPinError`.
4. **Weight and circuit admission.** Routes with weight `0` are dropped, as are
   routes whose circuit is open or already carrying a half-open probe. If
   nothing survives, `AllCircuitsOpenError`
   names the endpoints it considered. A model's offload route is held back
   here too, unless it is the only route left (see
   [Queue-wait offload](#queue-wait-offload)).
5. **Session affinity.** A live pin for this caller and model wins, unless the
   pinned endpoint is backlogged (see [Session affinity](#session-affinity)).
6. **Weighted draw.** Remaining weights are renormalised to sum to 1 and one
   route is drawn, biased away from endpoints currently busy with prefill (see
   [Prefill-aware selection](#prefill-aware-selection)).

### Fallback

When the selected adapter raises, `FixedRouter` records the failure against
that endpoint, drops the caller's affinity pin, and walks the model's remaining
routes in declaration order — skipping weight-`0` routes, routes that cannot
accept the request's modalities, and routes the breaker refuses a dispatch
claim for (open, or already carrying a probe). The first one that succeeds
answers the request. If every route fails, the *primary*
error is re-raised, with the whole attempt list attached. A model's offload
route is not in that order: it comes next after an attempt that waited out its
queue budget or its engine's first token, and last otherwise (see
[Queue-wait offload](#queue-wait-offload)).

Two deliberate exceptions:

- **A pinned request never falls back.** The caller asked for one endpoint, so
  a silent switch would produce a misleading result.
- **A streaming response never falls back once bytes have reached the client.**
  Splicing a second provider into a committed SSE stream would duplicate role
  events, switch voice mid-message, and produce mismatched usage totals; the
  truncated stream is the lesser harm.

Successful responses carry a `_routing` blob (`provider`, `base_url`,
`endpoint_id`, plus `fallback` and `failed_attempts` when a fallback ran) so
the serving layer can attribute the request to the endpoint that really served
it. Streaming does the same in-band through a synthetic chunk built by
`routing_chunk()` in `apps/backend/routing/telemetry.py`, which the completions
router strips before forwarding. Clients never see either.

## Endpoint health and circuit breaking

Every endpoint has a circuit breaker and a running average of its success
rate, shared by all routers in the process. The breaker opens after
`CIRCUIT_FAILURE_THRESHOLD` consecutive failures, or on any failure that leaves
the success rate below `CIRCUIT_MIN_AVAILABILITY`, and then stops routing
requests to the endpoint. After `CIRCUIT_COOLDOWN_SECONDS` it lets **one** test request
through: a success closes it, a failure opens it again.

While that test request is in flight, other requests that have nowhere else to
go get **503** rather than a second request onto a provider that is probably
still down. A model with a healthy route elsewhere simply uses that one
instead. The four settings:

| Variable | Default | Meaning |
|---|---|---|
| `CIRCUIT_FAILURE_THRESHOLD` | `3` | Consecutive failures before the circuit opens. |
| `CIRCUIT_COOLDOWN_SECONDS` | `30` | Seconds before a half-open probe is admitted. |
| `CIRCUIT_MIN_AVAILABILITY` | `0.7` | Success-rate floor: a failure that leaves the endpoint's average below it opens the circuit. |
| `ROUTER_HEALTH_EWMA_ALPHA` | `0.1` | Smoothing factor for the availability EWMA. |

`GET /health/deep` shows each endpoint's availability and circuit state. It
also marks an endpoint degraded from the first time a provider rejects the
gateway's own credential, without waiting for the success-rate average to fall:
the average moves slowly, and a rejected key fails every request.

State is per process. Each backend worker keeps its own breakers.

The routing file's `health_check:` probe is separate and only reports; it never
changes which endpoint a request goes to. See
[The routing file](configuration.md#the-routing-file).

### Admission, for router authors

A router checks an endpoint in two steps, and the split matters:

- **`allow_request(endpoint_id)`** is a pure predicate — is this endpoint a
  candidate? Routers ask it of *every* route candidate while enumerating and
  then dispatch to at most one, so it changes nothing.
- **`begin_dispatch(endpoint_id)`** is the commit. It is called once, where a
  router has settled on an endpoint, and returns a `DispatchClaim` or `None`
  when the endpoint cannot take this request. The caller hands the claim back
  through `end_dispatch(claim)` in a `finally` covering the dispatch, alongside
  the prefill lease and for the same reason: a client disconnect or a cancelled
  coroutine has to release it too.

The test request's slot belongs to the claim that took it: nothing else can
free it, because an outcome recorded by a request that bypassed admission (an
explicit `X-Route-Pin`, or one admitted while the circuit was still closed)
says nothing about whether the test has finished. A claim also expires after
`CIRCUIT_COOLDOWN_SECONDS`, so a dispatch that never unwinds costs one extra
cooldown rather than keeping the endpoint out of routing for good.

## Routes excluded from selection

A route whose *effective* weight is zero is skipped by weighted selection and by
every fallback loop, so it is configured capacity that does not exist. Four
mechanisms can produce one, and `GET /health/deep` names which under
`route_exclusions` — one entry per (model, endpoint) pair carrying the configured
weight, the effective weight, and a `reasons` list:

| Reason | Set by | Undone in |
|---|---|---|
| `weight_override` | a `provider_weight_overrides` row for that (model, endpoint) | admin console → routing weights |
| `provider_disabled` | a `disabled_providers` row for that provider label | admin console → providers |
| `routing_yaml` | the local/remote split `RoutingManager` applies once at boot | the overlay's `routing.yaml` |
| `configured_zero` | `weight: 0` in the model registry | the overlay's `models.yaml` |

`routing_yaml` appears only on a gateway without a database. With one, the
routing file's local/remote split never reaches the weights; see
[The routing file](configuration.md#the-routing-file).

The same exclusions are merged into the `providers` map — as
`excluded_from_models` and `exclusion_reasons` — including for endpoints that
appear nowhere else in it, because a route that is never dispatched to never
reaches the endpoint-health registry at all. They deliberately do not degrade the
deep-health verdict: zeroing a route is an operator decision, not an outage.

The gateway also names them at boot and after every override reload:
`route_weight_zeroed` at `WARNING` when a configured route is zeroed at runtime,
`route_weight_overridden` at `INFO` when it is merely re-weighted. Both lines
carry `configured_weight`, `effective_weight`, and the reason.

## Prefill-aware selection

Weighted-random selection balances *request counts*, which is the wrong unit
for a prefill-bound deployment: one very large cache-miss prompt can occupy a
replica for minutes while a small prompt costs milliseconds, and both count as
one request. `apps/backend/routing/prefill_load.py` tracks, per endpoint, how many
*un-cached* prompt tokens are currently in prefill, and uses that as a
selection signal.

- Selection is power-of-two-choices: two independent weighted draws, keep the
  endpoint holding less prefill. A route weighted 10× is still drawn about 10×
  as often, but the draw is unlikely to land on the endpoint buried in prefill.
- The tie-break only engages once the heavier draw carries at least
  `ROUTING_PREFILL_INTERVENE_TOKENS` of un-cached prompt tokens queued for
  prefill on that endpoint; below that, configured weights decide
  alone, because weights encode cost and provider preference and not only
  capacity.
- Very large prompts ("elephants") additionally skip endpoints already at the
  per-endpoint elephant limit, so two mega-prefills go to different replicas
  instead of stacking on one. If every candidate is at the limit the
  restriction is dropped: this degrades to "least loaded", never to "refuse to
  route".
- Un-cached size is estimated from the caller's last completed prompt on that
  endpoint, so a warm continuation is not mistaken for a cold mega-prefill.
- A lease is released at the first token, not at end of stream: a long cheap
  decode is not prefill pressure.

| Variable | Default | Meaning |
|---|---|---|
| `ROUTING_PREFILL_AWARE_ENABLED` | `1` | Set to `0` to fall back to a plain weighted draw. |
| `ROUTING_PREFILL_INTERVENE_TOKENS` | `50000` | Backlog at which load starts overriding the weighted draw. |
| `ROUTING_PREFILL_ELEPHANT_TOKENS` | `200000` | Estimated un-cached tokens at which a prompt counts as an elephant. |
| `ROUTING_PREFILL_ELEPHANT_LIMIT` | `1` | Concurrent elephants allowed per endpoint. |
| `ROUTING_PREFILL_AFFINITY_CEILING` | `150000` | Backlog above which an affinity pin is abandoned. |
| `ROUTING_PREFILL_HINT_TTL_SEC` | `1200` | Lifetime of the per-(caller, endpoint) prompt-size memory. |

This module never blocks, rejects, or queues a request. The worst it does is
prefer a different endpoint that was already admissible.

## Session affinity

`FixedRouter` keeps a per-(caller, model) pin to the last-selected endpoint for
five minutes on a sliding TTL (`AFFINITY_TTL_SECONDS` in `routers.py`). Goals:

- Keep one conversation on one backend so prompt caches stay warm and latency
  stays consistent.
- Drop the pin the moment that backend errors, so callers do not get stuck on a
  failing provider.

**Affinity key** — derived by `derive_affinity_key()` in
`apps/backend/serving/utils/request_ip.py`, first match wins:

1. Authenticated requests: the caller's API-key hash.
2. Requests presenting an inference grant instead of a key: `grant:<grant_id>`.
   Such callers commonly share one NAT or relay address, so an IP key would put
   every concurrent job on one endpoint.
3. Anonymous requests: `ip:<bucket>`, with IPv6 folded to its `/64` so rotating
   privacy addresses stay on one backend.

Every request surface that dispatches to an adapter publishes the key on the
request context — `/v1/chat/completions`, `/v1/messages`, and `/v1/embeddings`
— so the router's endpoint pin and `KeyPool`'s upstream-key binding see the
same caller. Internal traffic with no caller identity (health probes, warmups,
the admin playground) publishes nothing and shares one anonymous binding.

**Pin lifecycle:**

1. First request from `(key, model)` → weighted pick → entry stored.
2. Later requests within the TTL on the same `(key, model)` reuse the same
   endpoint and refresh the TTL.
3. Any exception from the pinned endpoint drops the entry; fallback runs; the
   next request creates a fresh pin.
4. If the pinned endpoint is no longer admissible — weight `0`, or its circuit
   is open — the entry is dropped and a fresh weighted pick runs.
5. If the pinned endpoint's prefill backlog is above
   `ROUTING_PREFILL_AFFINITY_CEILING`, the pin is abandoned for this request and
   selection re-runs *excluding* that endpoint, then re-pins to whatever it
   picks. A pin is a cache-locality optimisation, not a promise to queue.
6. After the TTL elapses with no traffic the entry expires. Expired entries are
   swept lazily once the table exceeds `AFFINITY_SWEEP_THRESHOLD` (1000).

**Scope and limits:**

- State is in-process; each worker keeps its own table, as `key_pool.py` does.
- Affinity does not survive a restart.
- An explicit `X-Route-Pin` bypasses affinity entirely.

**Kill switch:** set `ROUTING_AFFINITY_ENABLED=0`.

## Outbound concurrency

The gateway limits how many requests it keeps open at once against each
provider account — one limit per provider label and API key — and queues the
rest. The limit adapts: it starts at `UPSTREAM_CONCURRENCY_INITIAL_LIMIT`,
drops by one each time the provider answers `429`, and rises by one after every
`UPSTREAM_CONCURRENCY_PROBE_SUCCESS_INTERVAL` successful responses, up to
`UPSTREAM_CONCURRENCY_MAX_LIMIT`. A request that cannot get a slot within
`UPSTREAM_CONCURRENCY_ACQUIRE_TIMEOUT_SEC` fails over to the model's next
route, and is not counted against the endpoint's circuit breaker: the provider
never saw it.

Servers addressed as `localhost`, `127.0.0.1`, `0.0.0.0` or
`host.docker.internal` are never limited, because they schedule their own work.
A server you run on another machine is limited like a hosted provider.

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_CONCURRENCY_ENABLED` | `true` | `false` sends every request immediately |
| `UPSTREAM_CONCURRENCY_INITIAL_LIMIT` | `8` | Starting limit per provider key |
| `UPSTREAM_CONCURRENCY_MAX_LIMIT` | `64` | Highest the limit can rise |
| `UPSTREAM_CONCURRENCY_PROBE_SUCCESS_INTERVAL` | `100` | Successful responses between increases |
| `UPSTREAM_CONCURRENCY_ACQUIRE_TIMEOUT_SEC` | `30` | Longest a request waits for a slot before failing over |

A model can also send the requests that wait too long to a route reserved for
them; see [Queue-wait offload](#queue-wait-offload).

## Queue-wait offload

A model routed by `fixed` can reserve one of its routes as its **offload
route**: the route that takes the requests its other routes cannot seat. It is
set per model in the admin console (**Routing → Queue offload**) or with
`PUT /admin/routing/offload-routes/{model_id}`, as a route id and a wait in
seconds, and stored in `site_settings` under `model_offload_route:<model_id>`.
The routing mechanics live in `apps/backend/routing/offload.py`.

The wait is measured in the gateway's own outbound queue. The concurrency
limiter (`apps/backend/serving/adapters/upstream_limiter.py`) keeps at most a
learned number of requests open against each provider key and queues the rest;
without an offload route a queued request waits up to
`UPSTREAM_CONCURRENCY_ACQUIRE_TIMEOUT_SEC` (default `30`) for a slot and then
fails over. With one:

- **Selection.** The offload route is never the primary while another route is
  admissible: the weighted draw, affinity and a preferred target all skip it,
  so it takes no ordinary traffic whatever its weight.
- **Queue wait.** Every other attempt the request makes may wait at most the
  configured seconds for an outbound slot. When that runs out the request leaves
  the queue and goes to the offload route next, ahead of the rest of the
  fallback order. A wait ended by the limiter's own acquire timeout counts the
  same way: the request queued and never got a slot.
- **Last resort.** The offload route is also the last fallback after every
  other route has failed, and the primary when no other route is admissible.
- **The offload attempt** queues normally, with the full acquire timeout, since
  there is nowhere left to send it. A request is offloaded at most once.

Cutting a queue wait short is safe because the request has not left the
gateway: giving up its place in line releases nothing upstream, cannot
duplicate a generation, and is not charged to the endpoint's circuit breaker or
its prefix-cache hints.

### Engine waits

The gateway cannot see an inference engine's own queue. A vLLM or SGLang server
accepts every request and queues what it cannot schedule yet, and a request
waiting there looks, from outside, like one that is merely slow — until the
engine sends its first token. For a model with an offload route, `FixedRouter`
therefore gives every streaming attempt on the model's other routes a wait for
that token (`apps/backend/routing/engine_wait.py`):

- **First-token wait.** An attempt whose engine sends no token within the same
  wait is cancelled — closing the stream aborts the request at the engine — and
  the request goes to the offload route next. The wait starts when the request
  leaves the gateway, so time spent in its outbound queue is bounded by the
  queue wait instead, and a local server, which never queues there, gets the
  whole of it. Anything the engine sends before its first token, such as a
  role-only delta, is held back until the token arrives, so a client never sees
  the attempt that was abandoned.
- **Per request.** The wait decides for the one request it times. Nothing is
  recorded against the engine: the next request is sent to it as usual, with a
  wait of its own, and an engine wait is not charged to the circuit breaker or
  the endpoint's prefix-cache hints.
- **Output an adapter holds back counts.** Some adapters hold output until they
  can tell what it is: the GLM and Qwen Coder stream processors keep an XML tool
  call until it is complete, the MiniMax one strips out a `<think>` block, and
  the Claude adapters keep a tool call's JSON until the message ends. Each tells
  the router when it reads the first output from the upstream, and from then on
  the request waits as long as the engine needs.

Only streaming requests are watched: a non-streaming response arrives whole, so
there is no first token to wait for. The first-token wait includes the engine's
prefill, so a model serving very long prompts needs a wait above the time it
takes to start answering them. The wait needs Python 3.11 or newer: its
deadline is `asyncio.timeout`, which can tell its own cancellation from a client
that hangs up at the same moment. On Python 3.10 streams are never timed.

A response served by the offload route carries `_routing.offload` beside the
usual `fallback` and `failed_attempts`: `queue_wait` or `engine_wait` after an
attempt that waited too long, and `last_resort` otherwise. The request log's
metadata keeps it, streamed or not, and every dispatch to an offload route also
logs a `route_offload` line.

What does not offload:

- **A non-streaming request the gateway does not queue.** Only the limiter
  queues, and it exempts local inference servers (a host in
  `registry._LOCAL_HOSTS`), which schedule their own work. Nothing queues at all
  while `UPSTREAM_CONCURRENCY_ENABLED=false`. A streaming request there is still
  offloaded when its engine sends no first token in time, and the offload route
  still serves as the last resort.
- **A pinned request.** `X-Route-Pin` names one endpoint and never falls back,
  so it never offloads either. Nor does an attempt whose caller owns the
  candidate order (`allow_fallback` off, as the hybrid composition plans them),
  and the offload route has to be inside the dispatch scope and accept the
  request's modalities.
- **A prompt the offload route cannot fit.** Once a route refuses a prompt as
  too long for its context window, fallback passes over every route whose
  configured `context_length` is no wider, the offload route included. No later
  attempt leaves its queue for an offload route that would be passed over.
- **`/v1/messages`.** It picks one adapter itself and has no fallback. It keeps
  the offload route out of that pick unless nothing else is eligible, but does
  not offload on a wait. `/v1/chat/completions` and the surfaces built on it —
  `/v1/responses`, `/v1/completions` and the admin playground — do.
- **RouteWise.** A `routewise` model plans its own candidates and ignores offload
  routes, as does a `fixed` model with `hybrid_composition: true`. The admin API
  refuses to set an offload route on either, and refuses to switch a model that
  has one to `routewise`.

The wait must be positive, and may be longer than the acquire timeout: a request
still leaves the gateway's queue at that timeout, which offloads it as well, and
the longer wait then bounds only the engine's first token — what a model whose
long prompts take a while to start answering needs. The offload route needs an
effective weight above `0`: a weight override of `0` or a disabled provider turns the offload
off, and `GET /admin/routing/offload-routes` then reports the policy with
`active: false` and an `inactive_reason`. The route is named by its route id, which survives an admin
retarget; deleting a runtime route that is a model's offload route is refused
until the offload route is cleared.

## RouteWise

`routewise` is the second registered strategy. It keeps the average time to
first token as low as it can while staying within a cost budget: it prices
every usable route, measures how fast each one starts answering, and picks the
mix of routes that is fastest for the money. It is a per-model opt-in
(`router: routewise`), and each opted-in model gets its own `RouteWiseRouter`
instance. How it executes the routes it picks is in
[Routing Internals](routing-internals.md).

Its implementation lives in a separate package: the MIT-licensed
[`llm-routewise`](https://github.com/HarvardMadSys/RouteWise), which this
gateway takes as a required dependency and which any application can use to
choose a provider — it performs no network I/O and reads no credentials. The
design is described in *RouteWise: Latency--Cost Optimization for
Multi-Provider LLM Routing* (EuroSys '27).

If the package is missing — a partial install, or a build that leaves the
strategy out — the backend still imports, and a gateway whose models do not use
`routewise` starts normally. If any model selects it, startup stops with a
message saying the package is missing.

For a registry you can run unedited against two loopback providers, with every
option annotated, see
[`config/examples/models.routewise.yaml`](../../config/examples/models.routewise.yaml).

**RouteWise needs latency measurements.** Until it has measured an endpoint,
it cannot tell which one is faster, so it sends everything to the cheapest —
and the other endpoint then never gets measured. Measurements come from live
traffic, from recent request logs replayed at startup
(`db_bootstrap_enabled`, on by default), and from the built-in latency prober
(`routewise_probe_enabled`, off by default), which sends small test requests
and needs no database. A new deployment has no logs to replay, so turn the
prober on.

**One probe limit per worker.** `routewise_probe_max_concurrency`
(default `1`) is set per model, but all RouteWise models in a worker share one
limit, set to the lowest value any of them asks for. Otherwise two models on
the same provider subscription could probe it at the same time, and the
provider's refusal of the second could land on a real request. When a model is
added while probes are running, the new limit applies to every probe not yet
sent. Across
workers, a database lease (`routewise_probe_leases`) stops two workers from
probing the same model at once; it does not cap probe traffic for the whole
deployment.

Configuration splits by ownership:

- **`router_params:` (per model)** — algorithm knobs only. The accepted keys and
  their defaults are generated field-for-field from the `RouteWiseConfig`
  dataclass in `apps/backend/routing/routewise/config.py`, which is the
  authoritative list: cost-budget interpolation, latency hedging mode, latency
  SLO/window, cost-envelope percentiles/window/minimum samples, output-length
  predictor, and the prefix-cache flag.
- **Route entries (per provider)** — resource semantics: `provider_type:
  on_demand | quota | concurrency`, `pricing:`, `quota: {limit}` plus a
  `quota_source:` block, `concurrency: {limit}`, and optional `quota_pool:` /
  `concurrency_pool:` ids for routes that share one subscription.

Resource limits used to live in `router_params:`. Those keys are now rejected
at boot with a message naming their route-level replacement, so a stale
registry fails loudly rather than silently using defaults.

**Quota sources.** `quota_source:` is a selector, not a fetcher. It names
`provider` / `usage_label` / `unit`, and `_find_usage` matches all three
exactly against the usage records a provider's quota fetcher returns. RouteWise
calls those fetchers itself through `ProviderQuotaSnapshotStore`
(`apps/backend/routing/routewise/quota.py`) rather than reading the admin
poller's cache, and resolves them through the registry the Providers tab uses:
the gateway enables no fetchers by default, and a backend extension registers one per
provider with `register_quota_fetcher` (see
[Quota reporting](backend-extensions.md#quota-reporting)). `usage_label` is
the fetcher's own label string, not an operator-chosen name.

The route's `kind:` chooses its inference protocol; its `provider:` identifies
the deployment's service/account and can differ from the kind. The fetcher
must measure the account used by the corresponding routes, whether it uses
their inference key or a separate billing credential. The
[local quota example](../../config/examples/models.routewise.quota.yaml) uses
`kind: openai_compat` with `provider: example_quota` and a matching registered
source. It runs without a real supplier account; the default two-provider
example remains unchanged until this separate registry is selected.

A `provider` with no registered fetcher, or a mistyped label, simply never
resolves. Nothing warns about it — the only quota log lines are a refresh
failure and a provider/route limit mismatch — so the route stays unready and is
skipped in silence. Verify a new quota source against the fetcher before
shipping it.

The `local` provider you may see on admin-created routes is not a quota
source. It is a simple counter inside the gateway, used for a quota route whose
`route_metadata` sets `local_quota_fallback: true`, or whose route and upstream
providers differ; naming `provider: local` in a `quota_source:` does not turn it
on. It counts requests in the worker process, starting from zero on every
restart and never adding up across workers, and resets at the server's local
midnight. So it can only express a daily request allowance. A four-hour window,
a monthly window, a token or spend limit, or a plan whose reset the provider
decides all need a real `quota_source`.

**Endpoint identity.** Latency profiles, availability tracking and request logs
share one key per route, derived by `registry._make_provider_id` as
`{model_id}:{location}`. `location` is `local-{port}` for a base URL on a local
host, otherwise a name taken from the hostname (`api.minimax.io` →
`minimax-api`) for the generic adapter kinds, and `{kind}-api` for the rest. So
an edit that changes that derived part — a new port locally, a new vendor
hostname remotely — renames the endpoint and restarts its latency profile,
while one that does not (moving between local hosts on the same port) keeps it.
A `route_id:` in a static `models.yaml` never overrides it; that field belongs
to the admin provider-routes API.

**Cold start.** A quota-bearing route needs a calibrated cost envelope, built
from recent request history. A model whose quota route sits alongside a
non-quota route starts degraded — the quota route is masked until traffic
calibrates the envelope. A model whose *only* routes are quota-bearing raises
`EnvelopeNotCalibratedError` at startup, which bootstrap propagates, so the
deployment fails fast instead of serving an unroutable model.

**Worker scope.** RouteWise decision state, latency profiles, quota and
concurrency reservations, and per-model transition locks are process-local.
Configuring a `quota` or `concurrency` route while `WEB_CONCURRENCY`,
`UVICORN_WORKERS`, or `GUNICORN_WORKERS` exceeds 1 raises at startup (this
guard is the `stateful_providers_single_worker_only` config field, default
true). On-demand-only RouteWise models can run with multiple workers, but each
worker still learns independently.

**Runtime tuning.** Admin endpoints resolve a model id passed as a query
parameter, because model ids may contain `/`:

```text
GET    /admin/routewise/model-settings?model_id=<model-id>
PATCH  /admin/routewise/model-settings/{key}?model_id=<model-id>
DELETE /admin/routewise/model-settings/{key}?model_id=<model-id>
```

Settings are model-scoped, and aliases resolve to the canonical model, so an
alias and its canonical model always read and write the same values. `DELETE`
removes only the override and restores the inherited value. The older
`GET`/`PATCH /admin/routewise/settings` endpoints remain, and now set the
fallback used by models that have neither a model override nor a
`router_params:` value.

## Reserving upstream keys for a tier

A provider credential can be reserved for a user role and above, so premium
upstream capacity is not spent by the lowest tier. Reservation lives on the
key, not on the model: the model catalog's `required_role` decides *what* a
user may call, while a key's `min_role` decides *whose* requests may spend that
credential.

Each key in a `KeyPool` (`apps/backend/serving/adapters/key_pool.py`) carries a
`min_role`, defaulting to `free` — no reservation. Anything higher (`pro`,
`internal`, `admin`) makes the key invisible to callers below it:

- **Selection.** Reserved keys are filtered out for callers that do not meet
  `min_role`. Among the keys a caller *may* use, the most-reserved go first, so
  an entitled caller drains the capacity set aside for it before falling back
  to the keys every tier shares.
- **Affinity.** The pool's own five-minute key binding is honoured only for the
  role that created it, and any change to what the pool holds — a tier moving,
  a key added, re-enabled, or removed — drops every binding whose *preferred*
  key moved. *Muted* here is `KeyPool`'s own term for a key it has temporarily
  taken out of rotation after a key-specific failure (401/402/403/429); the key
  stays declared and comes back on its own. A binding to a merely muted key
  survives, because that state is
  transient and `acquire` re-picks around it; a binding to a removed key always
  goes. Only a declaration change triggers this, so ordinary traffic never
  loses prompt-cache warmth to it.
- **Exhaustion.** A caller whose usable keys are all muted (or who has none)
  gets `KeyPoolExhausted`, which the router treats as an upstream failure and
  fails over to the next route.
- **Health accounting.** When the pool could still serve an unrestricted
  caller, the refusal is a `KeyPoolRoleRestricted` (a `KeyPoolExhausted`
  subclass) and `EndpointHealthRegistry.record_failure` skips it. Nothing was
  sent upstream and the endpoint is still serving the tiers that own those
  keys; counting it would let a burst of lower-tier traffic open the circuit
  and strip reserved capacity from the callers it was reserved for. A pool
  usable by *nobody* stays a plain `KeyPoolExhausted` and still counts.
- **Single-adapter surfaces.** `/v1/messages` commits to one adapter up front
  instead of walking the fallback chain, so it picks the first adapter holding
  a key the caller may spend. Without that, a reserved first adapter would
  hard-fail a request another route could serve.

The caller's role reaches the pool through the request context, published by
the API-key auth dependency. Requests with no user identity — health probes,
warmups, the admin playground — carry no role and are treated as unrestricted:
reservation withholds capacity from lower *tiers*, not from the gateway's own
machinery.

**Managing it.** Reservation is declared through the admin API rather than an
env var, because pool membership comes from each route's `api_keys` list, which
need not be one of the `<PROVIDER>_API_KEY` variables. New keys accept
`min_role` on `POST /admin/provider-keys`. Changes apply to live pools
immediately, with no restart. The endpoint differs by key source:

| Source | Endpoint | Where the reservation lives |
|---|---|---|
| Database (dashboard-added) | `POST /admin/provider-keys/{key_id}/min-role` | `provider_api_keys.min_role` |
| Environment (`<PROVIDER>_API_KEY`, registry `api_keys`) | `POST /admin/provider-keys/min-role-env` | `provider_env_key_min_roles`, keyed by the key's hash |

An environment credential has no row of its own, so its reservation is keyed by
hash. Two consequences: the reservation outlives the key leaving rotation
(restore the variable and it returns at the tier it was reserved for), and the
admin list can show a reservation whose credential is not configured anywhere,
so it can be lifted rather than lying in wait.

`apps/backend/serving/adapters/dynamic_keys.py` owns the enforced tier, because
pools are built from adapter config (which carries no tier) and are rebuilt
often. Declarations are cached per provider and refreshed from the database at
boot and after every admin mutation. Where one credential is configured twice,
the **most restrictive declaration wins**: `free` is the *absence* of a
declaration rather than an assertion that everyone may spend the key, so adding
a laxer duplicate cannot widen access, and the result does not depend on
configuration order.

**Limit: a provider with no key pool cannot carry a reservation.** `min_role` is
enforced by `KeyPool`, so it reaches `openai_compat` routes (which include
local vLLM/SGLang/Ollama servers) and `openrouter`, which subclasses it — plus
any single-`api_key` route, which is promoted onto the pooled path when and only
when a reservation applies to it. The dedicated `anthropic`, `claude`, and
`gemini` adapters hold their credential directly and are never registered with
`dynamic_keys`, so a reservation recorded against one of those providers is
stored and never applied. Gate those models with the catalog's `required_role`
instead.

## API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | List published models. |
| `POST /v1/chat/completions` | Chat completion with automatic routing. |
| `GET /health` | Liveness plus a `routes_configured` count. |
| `GET /health/deep` | Per-endpoint availability, circuit state, and `route_exclusions`. |
| `GET /routing` | Current weight distribution per model, plus the probe's `endpoint_health` map. |

```{warning}
`GET /routing` requires no authentication and returns, for every published
model, each route's `provider`, **`base_url`**, and weight. On a public host
that discloses your upstream topology — including private LAN addresses and
any internal hostnames in a route's base URL. Put it behind your reverse proxy,
or do not expose it.

`GET /health/deep` is unauthenticated on the same terms: its `providers` keys
are `endpoint_id`s (`<model-id>:<location>`), and each `route_exclusions` entry
carries that route's `base_url` too. Gate both, not just `/routing`.
```

Runtime administration lives under `/admin/...`, requires admin
authentication, and is backed by the operational store. The routing-related
endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /admin/routing` | The weight distribution, including unpublished routes. |
| `GET /admin/routing/provider-routes` | Every route the gateway serves, per model, tagged `source: yaml`, `override` or `runtime`; `.../{model_id}` for one model. |
| `POST /admin/routing/provider-route-models` | Create a runtime model with its first route. |
| `POST /admin/routing/provider-route-candidates/{model_id}` | Add a route to a model; `PATCH` and `DELETE` on `.../{model_id}/{route_id}` edit or remove it. |
| `PUT /admin/routing/provider-routes/{model_id}/{route_id}` | Retarget a registry route; `DELETE` restores the YAML route. |
| `PUT` / `DELETE /admin/routing/weights/{model_id}/{endpoint_id}` | Set or clear a weight override. |
| `PATCH /admin/routing/provider-route-strategies/{model_id}` | Switch a model's router. |
| `GET /admin/routing/offload-routes` | Every model's offload route and whether routing applies it; `PUT` / `DELETE .../{model_id}` sets or clears one — see [Queue-wait offload](#queue-wait-offload). |
| `/admin/routewise/model-settings` | Per-model RouteWise tuning — see [RouteWise](#routewise). |

Each `POST` or `PUT` that changes a route has a `...-verifications` twin that
tries the upstream without saving anything. Which admin-console tab drives
which endpoint, what each one stores, and how the stored state combines with
the registry at boot is in
[Runtime configuration from the admin console](configuration.md#runtime-configuration-from-the-admin-console).

## Where the code lives

Four pieces, all under `apps/backend/routing/`:

| Layer | Code | Responsibility |
|---|---|---|
| Deployment-wide weight strategy | `manager.py` + `strategies/weight.py` | Reads the routing file and rewrites each model's per-route weights from a local/remote split. Optional. |
| Per-model router selection | `model_router_registry.py` + `strategies/__init__.py` | Maps each model id to a `RouterProtocol` implementation, chosen by that model's `router:` field. |
| Routing | `routers.py` (`FixedRouter`), `routewise/`, `hybrid.py` (`HybridRouter`) | Chooses the endpoint for a request, admits it against capacity, walks the fallback order, and records endpoint health. |
| Execution | `backends.py` + `dispatch.py` | Runs the one endpoint the router already chose, or delegates the request to a pool that chooses inside its own declared range. |

`apps/backend/routing/executor.py` is a backward-compatibility shim that re-exports
`FixedRouter` as `RouteExecutor` along with `AllCircuitsOpenError`,
`ProviderPinError`, and `RouteConfig`. Do not edit it — edit `routers.py`.

How the router hands a request to the adapter it chose, and the experimental
composition that plans across a local and a cloud pool, are described in
[Routing Internals](routing-internals.md).

## Adding a routing strategy

A routing strategy is the main extension point of this repository. Adding one
means writing a class that satisfies `RouterProtocol` and registering it under
a name that a model's `router:` field can select.

### 1. Understand the contract

`RouterProtocol` (`apps/backend/routing/protocols.py`) is a runtime-checkable
`Protocol` with four members:

```python
async def chat_completion(
    self,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    routing_options: RoutingRequestOptions | None = None,
    **params: Any,
) -> dict[str, Any]: ...

def stream_chat_completion(
    self,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    routing_options: RoutingRequestOptions | None = None,
    **params: Any,
) -> AsyncIterator[Any]: ...

def record_observation(self, obs: RoutingObservation) -> None: ...

def get_provider_status(self) -> dict[str, dict[str, Any]]: ...
```

`RoutingRequestOptions` carries the router-owned controls that must never be
forwarded to a provider adapter: `pin_provider` (the caller's hard pin, which
collapses the route to one provider and disables fallback), `preferred_endpoint_id`
(the preferred endpoint unless `require_target` makes it mandatory),
`endpoint_scope` (the allowed candidate range), `allow_fallback` (whether an
execution failure may try another candidate), `bound_endpoint` (the resolved
adapter binding), `require_target`, and `required_modalities`.
`RoutingObservation` (`routers.py`) is how the serving layer reports a
completed request — endpoint id, TTFT, total latency, token counts, success,
and a `strategy_metadata` dict the strategy itself populated during selection.
A stateless strategy returns `None` from `record_observation`; an
online-learning one updates its model there.

Two optional capabilities:

- `RouteTableRefreshable` — implement `refresh_route_table()` if your router
  derives state from the route table and must rebuild it after an admin route
  change. `ModelRouterRegistry.refresh_route_tables()` calls it.
- `ManagedRouter` (`routers.py`) — implement `async start()` / `async stop()`
  if your router owns background tasks. Bootstrap drives their lifecycle.

### 2. Get the routes

Your router is not handed the route table in its constructor. The registry
builds it, then calls `attach_route_table(route_table)` on it if that method
exists. The object you receive satisfies `RouteTableView`
(`apps/backend/routing/route_table.py`):

```python
def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]: ...
def canonical_id(self, model_id: str) -> str: ...
```

Each `EffectiveRoute` is a frozen `(route_key, canonical_model_id, adapters)`
triple where `adapters` is a tuple of `(adapter, weight)` pairs with runtime
weight overrides and admin provider disables already applied. Snapshot it; do
not hold a lock across a dispatch.

**A router that never implements `attach_route_table` sees no routes at all**,
so this is not optional in practice.

### 3. Write the strategy module

Strategies live in `apps/backend/routing/strategies/`. One module per strategy,
each exporting a router class and a Pydantic params model. Here is a complete
round-robin strategy — save it as
`apps/backend/routing/strategies/round_robin.py`:

```python
"""Round-robin routing strategy."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from routing.backends import LeafBackend
from routing.dispatch import binding_for_adapter
from routing.endpoint_health import EndpointHealthRegistry
from routing.endpoints import endpoint_id_for_adapter
from routing.strategies import register_strategy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.protocols import RoutingRequestOptions
    from routing.route_table import RouteTableView
    from routing.routers import RoutingObservation
    from serving.adapters.base import BaseAdapter


class RoundRobinParams(BaseModel):
    """Parameters accepted under ``router_params:`` for this strategy."""

    model_config = {"extra": "forbid"}

    skip_open_circuits: bool = True


class RoundRobinRouter:
    """Cycle through a model's routes in declaration order."""

    def __init__(
        self,
        params: RoundRobinParams | None = None,
        *,
        health_registry: EndpointHealthRegistry | None = None,
    ) -> None:
        self.params = params or RoundRobinParams()
        self._health = health_registry or EndpointHealthRegistry()
        self._lock = threading.Lock()
        self._cursor: dict[str, int] = {}
        self._routes: dict[str, tuple[BaseAdapter, ...]] = {}
        self.route_table: RouteTableView | None = None

    # -- registry binding -------------------------------------------------
    def attach_route_table(self, route_table: RouteTableView) -> None:
        """Bind the shared read-only route table after construction."""
        self.route_table = route_table
        self.refresh_route_table()

    def refresh_route_table(self) -> None:
        """Rebuild route-derived state after an admin route change."""
        table = self.route_table
        if table is None:
            return
        rebuilt: dict[str, tuple[BaseAdapter, ...]] = {}
        for route in table.iter_effective_routes():
            rebuilt[route.route_key] = tuple(
                adapter for adapter, weight in route.adapters if weight > 0
            )
        with self._lock:
            self._routes = rebuilt

    # -- selection --------------------------------------------------------
    def _next_adapter(self, model_id: str) -> BaseAdapter | None:
        with self._lock:
            adapters = self._routes.get(model_id, ())
            if not adapters:
                return None
            start = self._cursor.get(model_id, 0)
            for offset in range(len(adapters)):
                index = (start + offset) % len(adapters)
                adapter = adapters[index]
                if self.params.skip_open_circuits and not self._health.allow_request(
                    endpoint_id_for_adapter(adapter)
                ):
                    continue
                self._cursor[model_id] = index + 1
                return adapter
        return None

    # -- RouterProtocol ---------------------------------------------------
    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Route a non-streaming chat completion request."""
        adapter = self._next_adapter(model_id)
        if adapter is None:
            raise ValueError(f"No route available for model {model_id}")
        leaf = LeafBackend.for_binding(binding_for_adapter(adapter, model_id=model_id))
        endpoint_id = leaf.endpoint_id
        self._health.ensure(endpoint_id)
        try:
            response = await leaf.chat_completion(messages, **params)
        except Exception as exc:
            self._health.record_failure(endpoint_id, reason="chat_exception", exc=exc)
            raise
        self._health.record_success(endpoint_id)
        response.setdefault(
            "_routing",
            {
                "provider": adapter.config.provider,
                "base_url": adapter.config.base_url,
                "endpoint_id": endpoint_id,
            },
        )
        return response

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[str]:
        """Route a streaming chat completion request."""
        adapter = self._next_adapter(model_id)
        if adapter is None:
            raise ValueError(f"No route available for model {model_id}")
        leaf = LeafBackend.for_binding(binding_for_adapter(adapter, model_id=model_id))
        endpoint_id = leaf.endpoint_id
        self._health.ensure(endpoint_id)
        try:
            async for chunk in leaf.stream_chat_completion(messages, **params):
                yield chunk
        except Exception as exc:
            self._health.record_failure(endpoint_id, reason="stream_exception", exc=exc)
            raise
        self._health.record_success(endpoint_id)

    def record_observation(self, obs: RoutingObservation) -> None:
        """Ignore observations: this strategy keeps no online-learning state."""
        return None

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return endpoint health and circuit state."""
        return self._health.snapshot()


register_strategy("round_robin")((RoundRobinRouter, RoundRobinParams))
```

Points worth copying:

- **`extra="forbid"` on the params model.** A typo in `router_params:` then
  fails at boot with a clear message instead of silently using a default.
- **Accept `health_registry=`.** When the registry passes application-scoped
  `RouterBuildDependencies`, it *requires* the constructor to accept
  `health_registry=` (or `**kwargs`) and raises `TypeError` otherwise. Sharing
  the process registry is also what makes one endpoint's circuit visible to
  every router.
- **`params` must be the first parameter name.** `build_router` calls
  `router_cls(params=validated, ...)`.
- **Reuse `endpoint_id_for_adapter`.** Endpoint ids are the key for health,
  latency profiling, and log attribution; deriving your own would split them.
- **Execute selected adapters through a leaf.** Follow Fixed and RouteWise:
  create a binding with `binding_for_adapter()` and a leaf with
  `LeafBackend.for_binding()`, then call the leaf with the adapter's arguments.
  Keep admission, health, and `_routing` metadata in the owning router. When
  delegating to a pool, use `ExecuteEndpoint` only if the parent has resolved an
  exact target; use `DelegatePool` when the child should choose. The child still
  performs admission, so pass the resolved binding and the target/fallback
  controls through `RoutingRequestOptions` as well.

### 4. Register it at import time

Registration happens as an import side effect, so the module must be imported.
Add it at the *bottom* of `apps/backend/routing/strategies/__init__.py`, next to
the existing imports:

```python
from routing.strategies import fixed  # noqa: F401
from routing.strategies import round_robin  # noqa: F401
```

The imports sit at the bottom of that file on purpose: strategy modules import
from `routing.routers` at their top, so the dependency direction is one-way
(`strategies -> routers`) and a top-of-file import here would be a cycle.

If your strategy depends on an optional package, guard the import and call
`register_missing_strategy(name, reason)` in the `except ImportError:` branch.
Selecting it then fails configuration validation with your message instead of
breaking backend import for everyone — this is exactly how `routewise` is
handled.

### 5. Select it

Per model, in the model registry:

```yaml
models:
  - id: <model-id>
    router: round_robin
    router_params:
      skip_open_circuits: true
```

Or deployment-wide, in the routing file:

```yaml
default_router: round_robin
```

Note that `RoutingManager.apply()` only rewrites weights when the effective
`default_router` is `fixed`; setting it to anything else leaves each model's
`route:` weights exactly as written.

### 6. Test it

`tests/unit/routing/test_router_contract.py` holds the behavioural contract
every serving router must satisfy — add your class to it. `test_strategies.py`
covers the registry itself: registration, params validation, and the
dependency-injection check. An `isinstance(router, RouterProtocol)` assertion is
meaningful because the protocol is `@runtime_checkable`.

Run the routing tests with:

```bash
uv run pytest tests/unit/routing -q
```
