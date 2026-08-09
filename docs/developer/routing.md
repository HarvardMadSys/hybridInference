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

**Affinity key:**
- Authenticated requests: the user's `auth_key_hash`.
- Anonymous requests: `f"ip:{client_ip}"`.

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
- **Affinity.** A binding is dropped when the bound key is re-tiered above the
  caller, so an entitlement change takes effect on the next request.
- **Mute / rotation.** The sole-remaining-key backoff is judged against the keys
  the *leaseholder* could rotate to. A pro-only key is not a fallback for a
  free-tier request, so it cannot cancel the free tier's blip protection.
- **Exhaustion.** A caller whose usable keys are all muted (or who has none)
  gets `KeyPoolExhausted`, which the router treats as an upstream failure and
  fails over to the next provider in the chain.

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
  and restore its env var, and it returns at the tier it was reserved for.
- Pools are seeded from adapter config at registry load, before any DB read, so
  `apply_db_keys_at_boot` re-applies stored env reservations (and re-applies
  again after any pool promotion, e.g. when a DB key is added to a route that
  had a single static `api_key`). A DB read failure there logs and leaves env
  keys unreserved rather than failing the boot.

Reservation is declared through the admin API rather than an env var: pool
membership comes from each route's `api_keys` in `models.yaml`, which need not be
one of the `<PROVIDER>_API_KEY` vars, so an env-var-per-key convention would not
cover every configured key.

One remaining limit: **route-bound keys ignore `min_role`.** A key pinned to a
provider route via `api_key_id` is handed to that route's adapter directly rather
than through the shared pool, so access is governed by the model's
`required_role` instead.

## Migration Notes

For users migrating from older versions:
- The old `deployment.example.yaml` format is deprecated
- Use the simplified `config/routing.yaml` structure shown above
- Legacy `RoutingStrategy/select_deployment` patterns have been replaced with the current `FixedRatioStrategy.assign()` approach
