
# Configuration Guide

The system uses two configuration files for runtime setup:

- `config/models.yaml` (required): Registers available models and their backend adapters
- `config/routing.yaml` (optional): Configures traffic distribution between local and remote deployments

These files are independent: `models.yaml` provides the candidate set of adapters, while `routing.yaml` adjusts weights on top of registered adapters. Without `routing.yaml`, the system uses default weights from `models.yaml` or environment variables (typically 1.0).

## 1. Environment Variables and Priority

### Variable Substitution
The system supports environment variable placeholders in YAML:
- `${VAR}`: Reads `VAR` from environment
- `${VAR:-default}`: Reads `VAR` from environment, uses `default` if not found

### Configuration Priority
Environment variables (`.env` or system) > `routing.yaml`/`models.yaml` > code defaults

## 2. models.yaml (Required)

Registers models and adapters at startup. Each entry describes a model identifier and its backend configuration (base_url, api_key, capabilities, aliases, etc.).

Example:

```yaml
# config/models.yaml
models:
  - id: my-local-model
    name: My Local Model
    provider: ollama
    provider_model_id: "my-local-model"
    context_length: 65536
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop]
    pricing:
      prompt: "0"
      completion: "0"
      image: "0"
      request: "0"
      input_cache_reads: "0"
      input_cache_writes: "0"
    route:
      - kind: ollama
        weight: 1.0
        base_url: "http://localhost:11434/v1"
        provider_model_id: "my-local-model"

  - id: my-remote-model
    name: My Remote Model
    provider: openai_compat
    provider_model_id: "actual-model-id"
    context_length: 131072
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop]
    pricing:
      prompt: "1.0"
      completion: "3.0"
      image: "0"
      request: "0"
      input_cache_reads: "0"
      input_cache_writes: "0"
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: ${PROVIDER_BASE_URL}
        api_key: ${PROVIDER_API_KEY}
        provider_model_id: "actual-model-id"
```

### Key Points:
- `id`: Public model ID exposed by the API (what clients use to call the model)
- `provider_model_id`: The actual model name sent to the backend provider (e.g., vLLM/freeinference's `/models/...`). If omitted, uses `id`
- `aliases`: Additional public aliases that are registered alongside `id` to point to the same adapter
- `provider`: Determines adapter type. Supported kinds (dispatched in `serving/servers/registry.py:_make_adapter`): `openai_compat`, `vllm`, `sglang`, `ollama`, `chutes`, `featherless`, `deepseek`, `zhipu`, `minimax`, `openrouter` (also `openrouter[<slug>]` to pin a sub-provider), `gemini`, `claude`, `anthropic`. See [adding-models.md](adding-models.md) for the full reference table.
- `/v1/models` endpoint dynamically generates its response from registered adapters

## 3. routing.yaml (Optional)

Controls traffic distribution between local and remote deployments with optional health monitoring.

### Fixed-Ratio Strategy
- Set `routing_strategy: fixed`
- Control local traffic percentage via `routing_parameter.local_fraction` (0.0–1.0)
- Weights are distributed equally within local and remote groups

### Health Checking (Optional)
- `health_check: N`: Sends GET request to `/health` every N seconds
- Unhealthy endpoints temporarily get weight 0
- Endpoints recover automatically when health checks succeed
- Set to 0 or omit to disable health checking

### Example: Hybrid Deployment (60% local / 40% remote)

```yaml
# config/routing.yaml
routing_strategy: fixed
routing_parameter:
  local_fraction: 0.6
timeout: 2
health_check: 30
logging:
  output: output.log
local_deployment:
  - endpoint: ${LOCAL_BASE_URL:-http://localhost:8000}
    models:
remote_deployment:
    models:
```

### How It Works:
- At startup, `RoutingManager` applies 60/40 weights to registered adapters
- If local endpoint becomes unhealthy, weights adjust automatically (0% local, 100% remote)
- System falls back gracefully to maintain service availability

### Local-Only Deployment

Simply omit `routing.yaml` to use default weights from `models.yaml` (typically 1.0 for all adapters).

## 4. Running the System

### Set Environment Variables:
  ```bash
  export LOCAL_BASE_URL=http://localhost:8000
  ```

### Start the Server:
```bash
# Use uvicorn (no __main__ block in serving.servers.app)
uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080
```

### Verify Operation:
- `GET /v1/models` - Returns available models
- `POST /v1/chat/completions` - Routes requests based on configured ratios
- `GET /routing` - Shows current routing configuration

## 5. FAQ

**Q: What if routing.yaml conflicts with models.yaml?**
A: `routing.yaml` only adjusts weights; it doesn't add/remove adapters. The candidate set comes from `models.yaml` and environment variables.

**Q: How to disable health checks?**
A: Set `health_check: 0` or omit the field entirely.

**Q: Can I use other routing strategies?**
A: Two routing layers exist with different scopes:

- The deployment-wide weight strategy in `config/routing.yaml` (`routing_strategy:`) currently only supports `fixed`. `RoutingManager.apply()` returns without applying weights for any other value (see `routing/manager.py`).
- A per-model `routewise` strategy is also built-in. Opt in by setting `routing_strategy: routewise` on a model entry in `config/models.yaml`; tuning parameters live in `config/routewise.yaml`. RouteWise provides cost-aware routing via a primal-dual decision algorithm.

To add new deployment-wide strategies, implement them in `routing/strategies.py` and extend the dispatch in `routing/manager.py`.

**Q: What happens during failover?**
A: The system automatically tries alternative adapters when the primary fails, ensuring continuous service availability.
