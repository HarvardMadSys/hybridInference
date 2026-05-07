# Hybrid Inference Routing System

The routing system implements a two-layer architecture for intelligent traffic distribution:

## Architecture

### Decision Layer (`routing/manager.py` + `routing/strategies.py`)
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
routing_strategy: fixed
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

1. Create a new strategy class in `routing/strategies.py`:
```python
class RoundRobinStrategy:
    def assign(self, local: List, remote: List) -> Dict[object, float]:
        # Implementation
```

2. Update `routing/manager.py` to use the new strategy based on `routing_strategy` config.

### RouteWise Strategy

In addition to the deployment-wide fixed-ratio strategy in `routing.yaml`, a
cost-aware `routewise` strategy is available as a per-model opt-in. It is
enabled by adding `routing_strategy: routewise` to a model entry in
`config/models.yaml`; tuning parameters (decision rule, predictor, quota,
shadow-price bounds, canary rollout, etc.) live in `config/routewise.yaml`.
On startup, `serving/servers/bootstrap.py` instantiates a single
`RouteWiseRouter` if any model opts in (or `enable_routewise=true` in
settings) and registers it for those models via `model_router_registry`. See
`config/routewise.yaml` for the full set of tuning parameters and the design
specs under `docs/agents/specs/`.

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

## Migration Notes

For users migrating from older versions:
- The old `deployment.example.yaml` format is deprecated
- Use the simplified `config/routing.yaml` structure shown above
- Legacy `RoutingStrategy/select_deployment` patterns have been replaced with the current `FixedRatioStrategy.assign()` approach
