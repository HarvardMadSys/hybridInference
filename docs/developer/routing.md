# Hybrid Inference Routing System

The routing system implements a two-layer architecture for intelligent traffic distribution:

## Architecture

### Decision Layer (`routing/manager.py` + `routing/strategies/weight.py`)
Reads `config/routing.yaml` and computes weight distributions between local and remote deployments. Currently supports a fixed-ratio strategy with plans for expansion.

### Execution Layer (`routing/routers.py`)
Performs weighted random selection based on computed weights and provides automatic fallback to alternative adapters on request failure. The concrete `FixedRouter` lives in `routing/routers.py`; `routing/executor.py` is a backward-compatibility shim that re-exports `FixedRouter` as `RouteExecutor` along with `ProviderPinError` and `RouteConfig`.

## Features

- **Fixed-ratio routing**: Configurable traffic split between local and remote deployments
- **Health monitoring** (optional): Simple health checks with automatic weight adjustment
- **Automatic fallback**: Seamless failover when primary adapter fails
- **Environment variable support**: Configuration with `${VAR}` and `${VAR:-default}` syntax

## Configuration

See the [Configuration guide](configuration.md) for detailed options and examples.

### Required Files
- `config/models.yaml`: Registers available models and adapters

### Optional Files
- `config/routing.yaml`: Configures local/remote deployment split and health checking

### Example Configuration (60/40 split):

```yaml
default_router: fixed
routing_parameter:
  local_fraction: 0.6
timeout: 2
health_check: 30
logging:
  output: output.log
local_deployment:
  - endpoint: ${LOCAL_DEPLOYMENT_URL:-http://localhost:8000}
    models:
remote_deployment:
    models:
```

## Running the Server

```bash
# Development: run FastAPI app with routing enabled
uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080

# Or use a custom port for local development
PORT=9000 uvicorn serving.servers.app:app --host 0.0.0.0 --port $PORT
```

When the application starts, `serving.servers.bootstrap` loads `config/models.yaml` and optionally `config/routing.yaml`. If `routing.yaml` is present the `RoutingManager` applies the configured weights; otherwise default weights from `models.yaml` are used.

## API Endpoints

- `GET /v1/models` - List available models
- `POST /v1/chat/completions` - Chat completion with automatic routing
- `GET /routing` - View current routing configuration and weights
- `GET /health` - Health check endpoint

## Extending the System

### Adding New Strategies

1. Create a new strategy class in `routing/strategies/weight.py`:
```python
class RoundRobinStrategy:
    def assign(self, local: List, remote: List) -> Dict[object, float]:
        # Implementation
```

2. Update `routing/manager.py` to use the new strategy based on `default_router` config.

### RouteWise Strategy

In addition to the deployment-wide fixed-ratio strategy in `routing.yaml`, a
cost-aware `routewise` strategy is available as a per-model opt-in, enabled by
adding `router: routewise` to a model entry in `config/models.yaml`. Each
opted-in model gets its own `RouteWiseRouter` instance, constructed by
`routing/model_router_registry.py` from that model's `router_params:` block
(validated by the `RouteWiseParams` schema in
`routing/strategies/routewise.py`).

Configuration splits by ownership:

- **`router_params:` (per model)** — algorithm knobs only: `budget_alpha`
  (LP cost budget), `latency_hedge_mode`, latency SLO/window, envelope
  percentiles/window/min-samples, output predictor, prefix-cache flag.
- **Route entries (per provider)** — resource semantics: `provider_type:
  on_demand | quota | concurrency`, `pricing:`, `quota: {limit}` plus a
  required `quota_source:` block (the provider usage API is the quota truth
  source, including window/reset semantics), `concurrency: {limit}`, and
  optional `quota_pool:` / `concurrency_pool:` ids for routes that share a
  subscription.

Quota-bearing models refuse to start until the cost envelope is calibrated
from recent `api_logs` traffic (see `envelope_min_samples`); a cold deploy
with no history fails fast by design. The reference block in
`config/models.yaml` (under `minimax-fast`) lists every option and is
completeness-tested against the `RouteWiseConfig` dataclass. Design specs
live under `docs/agents/specs/`.

#### Runtime RouteWise settings

RouteWise tuning is model-scoped. The admin UI and model-settings API resolve
aliases to the canonical model id, so an alias and its canonical model always
read and update the same router settings. Changing one model does not change
another model's RouteWise algorithm.

The effective value for each runtime-adjustable setting is selected in this
order:

1. A persisted override for the canonical model.
2. That model's `router_params:` value in `config/models.yaml`.
3. The legacy global runtime setting, retained as a compatibility default.
4. The built-in `RouteWiseConfig` default.

The API/UI source label `Global default` intentionally covers the last two
levels: when no global row exists, the legacy runtime registry supplies the
same value as `RouteWiseConfig`.

The model-settings endpoints accept the model id as a query parameter because
model ids may contain `/`:

```text
GET    /admin/routewise/model-settings?model_id=<model-id>
PATCH  /admin/routewise/model-settings/{key}?model_id=<model-id>
DELETE /admin/routewise/model-settings/{key}?model_id=<model-id>
```

`DELETE` removes only the model override and immediately restores the inherited
YAML, legacy-global, or built-in value. The older
`/admin/routewise/settings` endpoints remain available, but now update only
the fallback used by models that have neither a model override nor a YAML
value.

Model overrides are restored before routers start, applied to newly created
RouteWise routers during strategy transitions, and periodically refreshed in
each worker. A runtime-created model's overrides are removed when its final
route is deleted. Router decision state itself remains process-local.

##### Worker model and scaling

Current production and staging RouteWise deployments run one Uvicorn worker.
Router decisions, circuit and health state, quota/concurrency reservations,
and per-model transition locks are process-local. Periodically polling
persisted settings makes workers eventually converge on values, but does not
serialize concurrent admin route or strategy mutations.

Do not enable multiple backend workers or replicas for RouteWise until shared
state and distributed fencing plus transactional control-plane writes exist.
The quota/concurrency single-worker guard is best-effort; it is not a
replacement for this deployment constraint. On-demand-only RouteWise models
can technically run with multiple workers, but each worker still has
independent health, learning, and affinity state.

### Health Monitoring

Health checks are optional and can be enabled by setting `health_check > 0` in the configuration. The system performs simple GET requests to `/health` endpoints and adjusts weights accordingly.

## Session affinity

`FixedRouter` keeps a per-(user, model) pin to the last-selected provider for
five minutes (sliding TTL). Goals:

- Keep one conversation on one backend so prompt caches stay warm and latency
  stays consistent.
- Drop the pin the moment that backend errors, so users don't get stuck on a
  failing provider.

**Affinity key:** derived by `derive_affinity_key()` in
`serving/utils/request_ip.py`:
- Authenticated requests: the user's `auth_key_hash`.
- Anonymous requests: `f"ip:{client_ip}"` (IPv6 folded to its `/64`).

Every request surface that dispatches to an adapter publishes it on `req_ctx`
as `affinity_key` — `/v1/chat/completions`, `/v1/messages` and `/v1/embeddings`
— so both `FixedRouter`'s provider pin and `KeyPool`'s upstream-key binding see
the same caller. Internal traffic with no caller identity (health probes,
warmups, the admin playground) publishes nothing and shares the `_anon` binding.

**Pin lifecycle:**
1. First request from `(key, model)` → weighted random pick → entry stored.
2. Subsequent requests within 300 s on the same `(key, model)` reuse the same
   endpoint and refresh the TTL.
3. Any exception from the pinned provider drops the entry; fallback runs;
   the next request creates a fresh pin.
4. If the pinned endpoint is no longer in the allowed pool (weight-0 in
   `routing.yaml` or its circuit is open), the entry is dropped and a fresh
   weighted-random pick runs.
5. After 300 s of inactivity the entry expires.

**Scope and limits:**
- State is in-process. Each Uvicorn worker tracks its own table. Same as
  `key_pool.py`.
- Affinity does not survive a restart.
- `pin_provider` (admin override via `X-Route-Pin`) bypasses affinity.

**Kill switch:** set `ROUTING_AFFINITY_ENABLED=0` to disable.

**Metrics:** `routing_affinity_events_total{event,model}` with events
`hit | miss | created | expired | dropped_error | dropped_unavailable`.

## Reserving upstream keys for a tier

A provider key can be reserved for a user role and above, so premium upstream
capacity is not spent by the free tier. Reservation lives on the key, not on the
model: the model catalog's `required_role` decides *what* a user may call, while
a key's `min_role` decides *whose* requests may spend that credential.

Each key in a `KeyPool` carries a `min_role`, defaulting to `free` — no
reservation. Anything higher (`pro`, `internal`, `admin`) makes the key invisible
to callers below it:

- **Selection.** Reserved keys are filtered out for callers that do not meet
  `min_role`. Among the keys a caller *may* use, the most-reserved go first, so
  an entitled caller drains the capacity set aside for it before falling back to
  the keys every tier shares.
- **Affinity.** A binding is honored only for the role that created it, and any
  change to what the pool holds — a tier moving, a key added, re-enabled or removed
  — drops every binding whose *preferred* key moved, not only bindings pointing at
  the key that changed. A binding to a merely *muted* key survives, because that
  state is transient and `acquire` re-picks around it without consulting the
  binding; a binding to a *removed* one always goes. Both directions matter: a caller no
  longer entitled to its bound key must be re-picked, and a caller that should now
  prefer a newly reserved key must stop draining the shared capacity that
  reservation exists to protect — otherwise it would keep doing so for the rest of
  the five-minute TTL. Only a declaration change triggers this, so ordinary traffic
  never loses prompt-cache warmth to it.

  The role match matters most where an affinity key is *shared*: internal traffic
  with no caller identity (health probes, warmups, the admin playground) all lands
  on the single `_anon` entry, and one credential can be issued to users of two
  roles. Without the match, one free request would bind such an entry to a shared
  key and later pro requests would inherit that binding — reserved capacity sitting
  idle, and the entry recording `free` so no re-tier could repoint it.
- **Mute / rotation.** The sole-remaining-key backoff is judged against the keys
  the *leaseholder* could rotate to. A pro-only key is not a fallback for a
  free-tier request, so it cannot cancel the free tier's blip protection.
- **Exhaustion.** A caller whose usable keys are all muted (or who has none)
  gets `KeyPoolExhausted`, which the router treats as an upstream failure and
  fails over to the next provider in the chain.
- **Health accounting.** When the pool could still serve an unrestricted caller,
  the refusal is a `KeyPoolRoleRestricted` (a `KeyPoolExhausted` subclass) and
  `EndpointHealthRegistry.record_failure` skips it — logged as
  `role_restricted_skip_breaker`. Nothing was sent upstream and the endpoint is
  still serving the tiers that own those keys; counting it would let a burst of
  lower-tier traffic open the circuit, strip reserved capacity from the callers
  it was reserved for, and re-trip on every half-open probe. A pool usable by
  *nobody* stays a plain `KeyPoolExhausted` and still counts.
- **Single-adapter surfaces.** `/v1/messages` commits to one adapter up front
  instead of walking the fallback chain, so `_pick_adapter_for_role` picks the
  first adapter holding a key the caller may spend (falling back to
  `adapters[0]` when none can, to keep the error unchanged). Without it, a
  reserved first adapter would hard-fail a request another provider on the same
  route could serve.

The caller's role reaches the pool through `req_ctx["user_role"]`, published by
the API-key auth dependency. Requests with no user identity — health probes,
warmups, the admin playground — carry no role and are treated as unrestricted:
reservation withholds capacity from lower *tiers*, not from the gateway's own
machinery (a probe blocked by a reservation would mark the endpoint unhealthy and
take the route down for the entitled users too).

**Managing it:** the admin dashboard's *Provider Keys* tab has a "Reserved for"
column covering both key sources, and new keys accept `min_role` on
`POST /admin/provider-keys`. Changes apply to the live pools immediately — no
restart. The endpoint differs by source, because the two are addressed
differently:

| Source | Endpoint | Where the reservation lives |
|---|---|---|
| DB (dashboard-added) | `POST /admin/provider-keys/{id}/min-role` | `provider_api_keys.min_role` |
| Env (`<PROVIDER>_API_KEY`, YAML `api_keys`) | `POST /admin/provider-keys/min-role-env` | `provider_env_key_min_roles`, keyed by the key's hash |

An env credential has no row of its own, so its reservation is keyed by hash —
the same addressing `disable-env` already uses for env-key tombstones. Two
consequences worth knowing:

- The reservation outlives the key leaving rotation. Disable/enable it, or drop
  and restore its env var, and it returns at the tier it was reserved for. Because
  it is durable, the admin list also shows a reservation whose credential is not
  configured anywhere — as `status: absent` — and its id stays resolvable, so it can
  be lifted rather than lying in wait for the key to come back.
- Pools are seeded from adapter config at registry load, before any DB read, so
  `apply_db_keys_at_boot` re-applies stored env reservations (and re-applies
  again after any pool promotion, e.g. when a DB key is added to a route that
  had a single static `api_key`). A DB read failure there logs and leaves env
  keys unreserved rather than failing the boot.

Reservation is declared through the admin API rather than an env var: pool
membership comes from each route's `api_keys` in `models.yaml`, which need not be
one of the `<PROVIDER>_API_KEY` vars, so an env-var-per-key convention would not
cover every configured key.

### One authority for the enforced tier

Pools are built from adapter config, which carries no tier, and are rebuilt often —
a route install, a single-key adapter promoted by a runtime key, a re-enabled env
key. A tier written at one of those call sites is a tier that silently disappears
at the next, so `dynamic_keys` owns the whole question instead:

- **Declarations** are cached per provider — `_env_key_min_roles` (by key hash,
  from `provider_env_key_min_roles`) and `_db_key_min_roles` (by raw value, from
  `provider_api_keys.min_role`). Both are refreshed from the DB, which stays the
  authority: at boot, and after every admin mutation.
- **`_resolve_min_role_locked`** derives the tier a pool entry must enforce. A pool
  holds one entry per raw value, so a credential configured twice (env var plus a
  DB row, or two DB rows) has to resolve rather than race. `free` is the *absence*
  of a declaration, not an assertion that everyone may spend the key, so
  unreserved sources contribute nothing; among real declarations the **most
  restrictive wins**. Adding a laxer duplicate therefore cannot widen access to a
  reserved credential, and the result does not depend on configuration order.
  Releasing a key means clearing its declaration, which is what the min-role
  endpoints do.
- **`_apply_min_roles_locked`** reconciles the live pools, and every path that can
  create a pool or change a declaration ends in it — including
  `register_adapter_for_provider`, the one point both the boot registry load and
  each runtime route install pass through. Without that, a route created with no
  pinned key builds its pool from the provider's whole (untiered) key set and
  serves every reserved key to every tier until the next restart.
- **Pool-less adapters are promoted** when — and only when — a reservation applies
  to their static key. A single-`api_key` route otherwise serves that credential
  through the legacy request path, which never consults a pool, so the reservation
  would persist, report success, and change nothing. Unreserved keys keep the
  cheaper legacy path, which is why the trigger is narrow: promotion moves that
  route onto the pooled path, where failures mute the key and rotate (5-minute
  cooldown, sole-key backoff) instead of returning the provider's error directly.

Reserving a key is therefore a decision to *withhold* capacity, and it is now
visible rather than silent: a lower-tier caller on a route whose keys are all
reserved gets a `KeyPoolRoleRestricted` and fails over to the next provider
instead of quietly spending the reserved credential.

Two ordering details the declarations depend on:

- **Boot loads them twice.** `apply_db_keys_at_boot` loads declarations before
  persisted routes are restored, but a provider can first become *known* during
  that restore — a built-in provider with no YAML route and no provider-definition
  row is reached only through its persisted route. Bootstrap therefore calls
  `load_min_role_declarations` again afterwards. The loader replaces each
  provider's map and re-derives every pool entry, so repeating it is free.
- **A failed re-read never widens access.** Every admin mutation re-reads the
  table, and when that read fails the tier just written is applied directly —
  "keep the previous tiers" is not uniformly safe, since a `free`→`pro` re-tier
  would keep serving the key to free callers and a newly added reserved key enters
  rotation immediately. The direct path only ever *tightens*: the cache holds one
  entry per raw value and so cannot represent a second row declaring something
  stricter, so a release waits for a successful read rather than risk relaxing a
  reservation another row still holds.

Because promotion closes that hole, **route-bound keys now honor `min_role` too**:
a key pinned via `api_key_id` holds the same secret as any other, and opting a
route out of global DB *key injection* is not opting it out of tiering. The admin
list reports the resolved tier from the persisted declarations, so what is
displayed is what is enforced — even for a credential a duplicate row declares
differently, and even before this process has cached that provider's declarations.

**Remaining limit: a provider with no key pool cannot carry a reservation.**
`min_role` is enforced by `KeyPool`, so it reaches `openai_compat` routes (which
include local vLLM/SGLang/Ollama) and any single-`api_key` route promoted on
demand. The dedicated `anthropic`, `claude` and `gemini` adapters hold their
credential directly and are never registered with `dynamic_keys`, so a reservation
recorded against one of those providers is stored and never applied. Gate those
models with the catalog's `required_role` instead.

## Migration Notes

For users migrating from older versions:
- The old `deployment.example.yaml` format is deprecated
- Use the simplified `config/routing.yaml` structure shown above
- Legacy `RoutingStrategy/select_deployment` patterns have been replaced with the current `FixedRatioStrategy.assign()` approach
