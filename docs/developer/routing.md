# Routing

Routing is what HybridInference is for. A client asks for one public model id;
the gateway decides which upstream endpoint actually serves it, retries a
different one when that fails, stops sending traffic to endpoints that are
down, and keeps a conversation on the endpoint whose prefix cache is already
warm.

This page describes the engine, its knobs, and how to add a routing strategy of
your own. For where the config files live and how they are found, see
[Configuration](configuration.md); for the shape of a model entry and its
`route:` list, see [Adding Models](adding-models.md).

## Architecture

Three pieces, all under `apps/backend/routing/`:

| Layer | Code | Responsibility |
|---|---|---|
| Deployment-wide weight strategy | `manager.py` + `strategies/weight.py` | Reads the routing file and rewrites each model's per-route weights from a local/remote split. Optional. |
| Per-model router selection | `model_router_registry.py` + `strategies/__init__.py` | Maps each model id to a `RouterProtocol` implementation, chosen by that model's `router:` field. |
| Execution | `routers.py` (`FixedRouter`) | Selects one adapter per request, dispatches it, falls back on failure, and records endpoint health. |

`apps/backend/routing/executor.py` is a backward-compatibility shim that re-exports
`FixedRouter` as `RouteExecutor` along with `AllCircuitsOpenError`,
`ProviderPinError`, and `RouteConfig`. Do not edit it — edit `routers.py`.

Startup wiring lives in `apps/backend/serving/servers/bootstrap.py`: it
registers routes from the model registry into one process-scoped
`FixedRouter`, optionally applies the `RoutingManager`, then builds a
`ModelRouterRegistry` over the same route table. Routers for every known model
are constructed eagerly at boot, so a bad strategy name or bad `router_params:`
is reported at startup rather than on the first request.

## Choosing a router per model

The routing file's `default_router:` names the strategy for models that do not
choose one. A model entry in the registry overrides it:

```yaml
models:
  - id: <model-id>
    provider: openai_compat
    router: fixed            # strategy name; omit to use default_router
    router_params:           # validated by that strategy's Pydantic model
      local_fraction: 0.6
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
  default and the only one that needs no extra dependency.
- **`routewise`** — a cost-aware strategy that lives in a separate package; see
  [RouteWise](#routewise) below.

Names come from the registry in `apps/backend/routing/strategies/__init__.py`.
An unknown name raises at validation time with the list of known strategies in
the message; unknown keys in `router_params:` are rejected too, because every
strategy's params model sets `extra="forbid"`.

Aliases share the canonical model's router: `ModelRouterRegistry` resolves an
alias to its canonical id before looking up or building anything, so a stateful
router is never split across a model and its alias.

## How `FixedRouter` picks an endpoint

For each request `FixedRouter._select_adapter` narrows the model's routes in
this order:

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
   routes whose circuit is open. If nothing survives, `AllCircuitsOpenError`
   names the endpoints it considered.
5. **Session affinity.** A live pin for this caller and model wins, unless the
   pinned endpoint is backlogged (see [Session affinity](#session-affinity)).
6. **Weighted draw.** Remaining weights are renormalised to sum to 1 and one
   route is drawn, biased away from endpoints currently busy with prefill (see
   [Prefill-aware selection](#prefill-aware-selection)).

### Fallback

When the selected adapter raises, `FixedRouter` records the failure against
that endpoint, drops the caller's affinity pin, and walks the model's remaining
routes in declaration order — skipping weight-`0` routes, routes that cannot
accept the request's modalities, and routes whose circuit is open. The first
one that succeeds answers the request. If every route fails, the *primary*
error is re-raised, with the whole attempt list attached.

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
`apps/backend/routing/telemetry.py::routing_chunk`, which the completions router strips
before forwarding. Clients never see either.

## Endpoint health and circuit breaking

`apps/backend/routing/endpoint_health.py` holds one `_ProviderHealth` (an EWMA of success
rate) and one `_CircuitBreaker` per `endpoint_id`, in a process-scoped
`EndpointHealthRegistry` shared by every router. `allow_request` is consulted
during selection *and* during fallback, so an open circuit is skipped by both.

The breaker opens after `failure_threshold` consecutive failures, stays open
for `cooldown_seconds`, then admits one half-open probe: a success closes it, a
failure re-opens it. All four knobs read the environment first:

| Variable | Default | Meaning |
|---|---|---|
| `CIRCUIT_FAILURE_THRESHOLD` | `3` | Consecutive failures before the circuit opens. |
| `CIRCUIT_COOLDOWN_SECONDS` | `30` | Seconds before a half-open probe is admitted. |
| `CIRCUIT_MIN_AVAILABILITY` | `0.7` | EWMA success floor below which the endpoint counts as unhealthy. |
| `ROUTER_HEALTH_EWMA_ALPHA` | `0.1` | Smoothing factor for the availability EWMA. |

`GET /health/deep` reports this registry: per-endpoint availability, circuit
state, and a consecutive-upstream-auth-rejection counter that degrades the
endpoint from the first rejection, because an upstream refusing the gateway's
credential fails every request without moving an availability average.

State is per process. Each backend worker keeps its own breakers.

This is separate from the routing file's `health_check:` probe loop
(`apps/backend/routing/health.py`), which polls `local_deployment` endpoints' `/health` on
an interval and only influences the weight assignment described in
[Configuration](configuration.md).

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
  `ROUTING_PREFILL_INTERVENE_TOKENS`; below that, configured weights decide
  alone, because weights encode cost and provider preference and not only
  capacity.
- Very large prompts ("elephants") additionally skip endpoints already at the
  per-endpoint elephant limit, so two mega-prefills serialise across replicas
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

## API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | List published models. |
| `POST /v1/chat/completions` | Chat completion with automatic routing. |
| `GET /health` | Liveness plus a `routes_configured` count. |
| `GET /health/deep` | Per-endpoint availability and circuit state. |
| `GET /routing` | Current weight distribution per model. |

```{warning}
`GET /routing` requires no authentication and returns, for every published
model, each route's `provider`, **`base_url`**, and weight. On a public host
that discloses your upstream topology — including private LAN addresses and
any internal hostnames in a route's base URL. Put it behind your reverse proxy,
or do not expose it.
```

Runtime route and weight administration lives under `/admin/routing/...` and
does require admin authentication.

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

`RoutingRequestOptions` carries the two router-owned controls that must never
be forwarded to a provider adapter: `pin_provider` and `required_modalities`.
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
        endpoint_id = endpoint_id_for_adapter(adapter)
        self._health.ensure(endpoint_id)
        try:
            response = await adapter.chat_completion(messages, **params)
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
        endpoint_id = endpoint_id_for_adapter(adapter)
        self._health.ensure(endpoint_id)
        try:
            async for chunk in adapter.stream_chat_completion(messages, **params):
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
dependency-injection check. A `isinstance(router, RouterProtocol)` assertion is
meaningful because the protocol is `@runtime_checkable`.

Run the routing tests with:

```bash
uv run pytest tests/unit/routing -q
```

## RouteWise

`routewise` is the second registered strategy: a cost-aware router that
converts every feasible route to one effective cost, solves a cost-budgeted
mean-TTFT linear program over them, and samples a primary from the resulting
sparse mixture. It is a per-model opt-in (`router: routewise`), and each
opted-in model gets its own `RouteWiseRouter` instance.

Its implementation lives in a separate package: the MIT-licensed
[`llm-routewise`](https://github.com/HarvardMadSys/RouteWise), which this
gateway takes as a required dependency and which any application can use to
choose a provider — it performs no network I/O and reads no credentials. The
design is described in *RouteWise: Latency--Cost Optimization for
Multi-Provider LLM Routing* (EuroSys '27).

Required, but not load-bearing for startup. When the package is absent —
a partial install, or a build that deliberately drops the strategy —
`apps/backend/routing/strategies/__init__.py` registers `routewise` as a
*missing* strategy: selecting it fails configuration validation with an
actionable message rather than breaking backend import. No single routing
algorithm decides whether the gateway can start.

For a registry you can run unedited against two loopback providers, with every
option annotated, see
[`config/examples/models.routewise.yaml`](../../config/examples/models.routewise.yaml).

**Latency evidence.** The policy only trades latency against cost where it has
measurements. Two endpoints with no TTFT samples tie on the latency objective,
`cost_tiebroken_objective` breaks that tie on price, and the LP returns a
one-hot solution on the cheaper one at every cost budget — so nothing ever
measures the endpoint the policy is avoiding. Evidence comes from live traffic,
from `db_bootstrap_enabled` replaying recent `api_logs` at startup, and from
the active prober (`routewise_probe_enabled`), which runs in-process and does
not require an operational store; persisting probe samples is an optimization
on top.

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
exactly against the usage records a provider fetcher returns. RouteWise calls
those fetchers itself through `ProviderQuotaSnapshotStore`
(`apps/backend/routing/routewise/quota.py`) rather than reading the admin
poller's cache, and only two are registered: `chutes` and `minimax`.
`usage_label` is the fetcher's own label string, not an operator-chosen name —
the Chutes fetcher emits `Daily requests` with `unit: requests`.

The route's `kind:` and credential belong to the same contract. Each fetcher
discovers its own keys by provider — `fetch_chutes` looks for `CHUTES_API_KEY`
and for keys bound to `chutes` routes — so a quota route served through the
generic `openai_compat` adapter under an unrelated key never joins that pool:
inference authenticates, and the quota snapshot stays `not_configured`. Match
`kind:` to the provider and use the provider's own key variable.

A `provider` outside the registered pair, or a mistyped label, simply never
resolves. Nothing warns about it — the only quota log lines are a refresh
failure and a provider/route limit mismatch — so the route stays unready and is
skipped in silence. Verify a new quota source against the fetcher before
shipping it.

The `local` provider seen in admin-created routes is not a third fetcher: it is
a gateway-side per-request counter installed through
`configure_local_fallbacks`. Naming `provider: local` in a `quota_source:` does
not arm it, but static YAML can still reach it — `_uses_local_quota_fallback`
selects a quota route whose `route_metadata` sets `local_quota_fallback: true`,
or whose `route_provider` and `upstream_provider` differ. Know what it is
before relying on it: the count lives in the worker process, so it starts at
zero on every restart and does not add up across workers; it increments by one
per request, so it can only express a **request-count** allowance, never tokens
or spend; and it resets at the next **server-local midnight**, so it models a
daily cap and nothing else. A four-hour window, a monthly window, or a plan
whose reset the provider decides all need a real `quota_source`.

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
  key moved. A binding to a merely muted key survives, because that state is
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
