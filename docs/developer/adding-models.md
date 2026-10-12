# Adding a New Model

This guide is for operators who want a gateway to serve another model. If the
provider is one the gateway already supports — any OpenAI-compatible API,
OpenRouter, Anthropic, Claude on Vertex or Gemini — this is configuration only:
add an entry to the model registry, set its credential, and restart.

- For a model you serve yourself on vLLM, SGLang, or Ollama, see
  [Adding a New Local Model](add-local-model.md).
- For a provider with its own, non-OpenAI API, you need an adapter first; see
  [Writing a Provider Adapter](provider-adapters.md).
- When a database is configured, a model, a route or a key can also be added
  from the admin console while the gateway runs; see
  [Runtime configuration from the admin console](configuration.md#runtime-configuration-from-the-admin-console).
  A model created there has no catalog metadata, so a model that needs a
  context length, modalities or aliases still belongs in the registry.

## Where your model registry lives

The gateway uses the file named by `MODELS_CONFIG_PATH`; failing that, the one
named by an active distribution manifest; failing both, the bundled example
`config/examples/models.openrouter.yaml`.
[How a gateway finds its config](configuration.md#how-a-gateway-finds-its-config)
has the details.

That leaves two ways to set up your own:

- **Point at a file.** Keep your registry wherever you like and set
  `MODELS_CONFIG_PATH=/path/to/models.yaml`. This is what the quickstart in
  `README.md` does.
- **Ship a distribution.** Create `distributions/<name>/` containing a
  `distribution.yaml` manifest and a `config/models.yaml`, then set
  `DISTRIBUTION_CONFIG_PATH=distributions/<name>/distribution.yaml` and
  `DISTRIBUTION_CONFIG_MODE=active`. `distributions/example/` is a working
  distribution you can copy; `config/examples/distribution.example.yaml` is an
  annotated manifest.

Throughout this guide, "your model registry" means whichever file that
resolution picks.

If no registry is found at the resolved path, the backend logs an error, serves
an empty `/v1/models`, and reports every model as not found.

## Adding a model

1. **Add a model entry** to your model registry:

```yaml
models:
  - id: your-model-id
    name: Your Model Display Name
    provider: existing_provider  # e.g. "gemini", "deepseek"
    provider_model_id: "actual-provider-model-id"
    base_url: ${PROVIDER_BASE_URL}
    api_key: ${PROVIDER_API_KEY}
    quantization: "bf16"
    input_modalities: ["text"]
    output_modalities: ["text"]
    context_length: 8192
    max_output_length: 4096
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
      - kind: existing_provider
        weight: 1.0
```

The route inherits `base_url` and `api_key` from the model, so it only has to
carry what differs. Repeat them on a route entry when a second route points
somewhere else.

2. **Set the variables.** On a gateway with a database, add each one on the
   admin console's **Configuration** tab with **Add variable**, and mark the
   API key secret. Without a database, put them in `.env` at the repository
   root, which the backend's settings loader reads:

```bash
PROVIDER_BASE_URL=https://api.provider.example/v1
PROVIDER_API_KEY=your-api-key
```

A value in `.env` also reaches a gateway with a database: at startup the
backend copies it into the database, unless the database already has a value
of that name.

If a `${VAR}` used for `api_key`, `api_keys` or `base_url` is unset or empty,
the gateway skips the whole model and logs which variable was missing. Mark a route `optional: true` to
skip only that route instead. Until such a variable has a value, the console
also reports it as a missing setting to every signed-in user, so mark a route
you leave unconfigured on purpose `optional: true` as well.

3. **Restart the backend** to load the new model.

4. **Verify** it, as described below.

Note on aliases: to let clients also call the model under a second name — an
OpenRouter-style vendor slug, or the raw model path your serving runtime uses —
list those names in `aliases`. They resolve to the same routes.

## Verify it through the gateway

Start the backend. From a source checkout:

```bash
MODELS_CONFIG_PATH=/path/to/your/models.yaml \
  uv run uvicorn serving.servers.app:app --no-proxy-headers --port 8080
```

With the bundled Docker Compose setup, `make build s=backend` rebuilds and
restarts the backend instead.

`GET /v1/models` accepts an anonymous request — it resolves an API key only to
decide whether to include admin-visible entries:

```bash
curl -s http://localhost:8080/v1/models | jq
```

`POST /v1/chat/completions` **returns 401 without a valid gateway API key**
unless the backend is running with `USER_AUTH_ENABLED=false`. Send the key you issued for your own gateway (this is
the gateway's key, not the upstream provider's):

```bash
export GATEWAY_API_KEY=<your gateway API key>

curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model-id",
    "messages": [{"role": "user", "content": "Hello!"}]
  }' | jq
```

Streaming test:

```bash
curl -N -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model-id",
    "messages": [{"role": "user", "content": "Stream test"}],
    "stream": true,
    "max_tokens": 64
  }'
```

## Configuration Reference

### Model fields

These keys can appear on a model entry. Most of them can also be set on a
route entry, where the route value wins; `id` and `name` identify the model
itself and are read only at the model level.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique model identifier; the name clients send |
| `name` | string | Yes | Display name |
| `provider` | string | Yes | Provider/adapter kind; also the default `route[].kind` when no `route:` list is given. Note the collision: at the model level `provider:` selects an adapter, while `provider:` on a *route* is only an analytics label — see the route-fields table |
| `base_url` | string | Yes, here or on every route | API endpoint base URL |
| `api_key` | string | No | API authentication key |
| `provider_model_id` | string | No | Provider's model identifier (overrides `id` on the wire) |
| `model_type` | string | No | `"chat"` (default) or `"embedding"`. The alias `type:` is equivalent; embedding models bypass the weighted router and use their routes as an ordered fallback chain |
| `aliases` | list[string] | No | Alternative names for routing |
| `quantization` | string | No | Quantization format (default: `"bf16"`) |
| `input_modalities` | list[string] | No | Input types: `"text"`, `"image"` |
| `output_modalities` | list[string] | No | Output types: `"text"` |
| `context_length` | int | No | Maximum context window (default: 8192) |
| `max_output_length` | int | No | Maximum output tokens (default: 4096); `max_tokens` is clamped to it |
| `supports_tools` | bool | No | Function calling support (default: false) |
| `supports_structured_output` | bool | No | JSON mode support (default: false) |
| `supported_params` | list[string] | No | Allowed parameter names (default: `temperature`, `top_p`, `max_tokens`) |
| `reasoning_efforts` | list[string] | No | The values this model's `reasoning_effort` accepts. Meaningful only when `reasoning_effort` is in `supported_params`; empty means "not offered". It has no default, because the accepted values differ from model to model |
| `on_demand` | bool | No | Model is lazily loaded on shared GPUs (started on first request, stopped when idle). Exposed in `/v1/models`, and the RouteWise background latency prober never probes such endpoints (default: false) |
| `processor` | string | No | Output-processor override for `OpenAICompatAdapter`, bypassing model-ID auto-detection. Accepted: `"default"`, `"glm"`, `"qwen_coder"`, `"think_block"` |
| `extra_body` | dict | No | Default fields merged into OpenAI-compatible upstream request bodies. Core fields and validated client parameters win |
| `priority_scheduling` | bool | No | The endpoint runs an sglang server started with `--enable-priority-scheduling`; see [Prioritizing decode on an sglang route](add-local-model.md#prioritizing-decode-on-an-sglang-route). Normally set per route, not per model |
| `route_metadata` | dict | No | Free-form per-route metadata consumed by routing strategies |
| `pricing` | dict | No | Base cost information. `prompt`, `completion`, `input_cache_reads` and `input_cache_writes` are USD per 1M tokens; `request` is USD per request and `image` USD per image |
| `pricing_schedule` | dict | No | UTC-only activation time plus recurring daily price windows: `pricing` stays active before `effective_at`; afterwards `default` applies outside each half-open `[start, end)` window, and a window's own `pricing` overrides the base fields |

A model entry also accepts `route` (below), `router` and `router_params` (see
[Choosing a router per model](routing.md#choosing-a-router-per-model)), and
`required_role`, the lowest user role that can see and call the model
(`admin_only: true` is an older way to write `required_role: admin`).

### Route configuration

Routes give one model several endpoints with weighted distribution:

```yaml
route:
  # Local vLLM deployment
  - kind: vllm
    weight: 0.7  # 70% of traffic
    base_url: http://localhost:8000
    provider_model_id: "/models/local-model"

  # Remote API fallback
  - kind: your_provider
    weight: 0.3  # 30% of traffic
    base_url: https://api.provider.example
    api_key: ${API_KEY}
```

Beyond the model fields above, a route entry accepts:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | string | Adapter kind (see below). Defaults to the model's `provider` |
| `weight` | float | Relative share of traffic (default 1.0). `0` keeps the route configured but unused: it is not tried even as a fallback |
| `api_keys` | list[string] | Key pool for this endpoint, instead of `api_key`. Setting both is an error. The whole pool is one endpoint for latency and quota accounting; split genuinely separate resources into separate routes |
| `embeddings_path` | string | OpenAI-compatible embeddings path appended to this route's `base_url`; a leading slash is optional. Omitted, null, or empty keeps the default URL inference |
| `optional` | bool | Skip this route with a warning when a `${VAR}`-backed key, `base_url`, or `embeddings_path` resolves unset or blank |
| `provider` / `provider_display_name` | string | Analytics label override only — it renames the row in the dashboard and does **not** select an adapter; that is `kind`. See [Naming a route in the dashboard](add-local-model.md#naming-a-route-in-the-dashboard) |
| `provider_type` | string | RouteWise cost category: `on_demand`, `quota`, or `concurrency` |
| `routewise_pool`, `quota_pool`, `concurrency_pool`, `quota_source`, `quota`, `concurrency` | — | RouteWise pool and budget metadata |

A route reports its `kind` as the provider label — the value recorded in
`api_logs.provider` and grouped on by every provider-scoped admin view. Two
routes of the same kind therefore share one dashboard row; `provider:` splits
them.

#### Custom embeddings paths

An OpenAI-compatible gateway may use an API prefix other than `/v1`. Set
`embeddings_path` on that route to avoid appending an unwanted `/v1` segment:

```yaml
models:
  - id: text-embedding-example
    name: Example embeddings
    provider: openai_compat
    model_type: embedding
    route:
      - kind: openai_compat
        base_url: https://gateway.example/api/v2
        api_key: ${EMBEDDING_API_KEY}
        embeddings_path: /embeddings
```

This sends requests to `https://gateway.example/api/v2/embeddings`. The override
belongs to one route and does not affect its fallbacks. Without it, a base URL
ending in `/v1` gets `/embeddings`; any other base gets `/v1/embeddings`.
Trailing slashes on the base URL are removed before joining the path.

Declare `embeddings_path` only inside `route:`. A model-level declaration,
including on a shorthand model without a route list, is rejected. Surrounding
whitespace is trimmed, but whitespace-only strings, non-string values other
than `null`, and parent (`..`) path segments are rejected. Explicit `null` and
`""` still select the default URL inference.

A whole-value `${VAR}` reference must resolve to a non-blank path; it is not an
instruction to use the default if the variable is missing. A missing or blank
variable aborts startup for a required route. With `optional: true`, only that
route is skipped, with a warning naming the model, route, and variable.

Any other invalid `embeddings_path` stops startup, even on an optional route,
so the gateway never starts with part of the registry missing.

### Supported adapter kinds

The `kind` field in each route entry selects the backend adapter. All kinds
marked **OpenAI-compat** share the same `OpenAICompatAdapter` implementation,
with provider-specific profiles applied automatically.

| Kind | Category | Notes |
|------|----------|-------|
| `openai_compat` | OpenAI-compat | Generic OpenAI-compatible endpoint; use when no specific kind fits |
| `staging` | OpenAI-compat | Clone of `openai_compat` with its own provider label, so a second generic endpoint can be tracked separately in metrics |
| `vllm` | OpenAI-compat | Local vLLM inference server |
| `sglang` | OpenAI-compat | Local SGLang inference server |
| `ollama` | OpenAI-compat | Local or remote Ollama server |
| `chutes` | OpenAI-compat | Chutes.ai hosted inference |
| `featherless` | OpenAI-compat | Featherless.ai hosted inference |
| `cliproxy` | OpenAI-compat | CLI proxy endpoint for OpenAI-compatible models |
| `deepseek` | OpenAI-compat | DeepSeek API (applies the DeepSeek usage profile) |
| `kimi` | OpenAI-compat | Moonshot/Kimi pay-per-token API under `/v1` (applies the Kimi usage profile) |
| `zai` | OpenAI-compat | Z.AI's ordinary API under `/api/paas/v4/`; the Z.AI profile appends `/chat/completions` without adding another version segment |
| `minimax` | OpenAI-compat | MiniMax API (applies the MiniMax usage profile) |
| `openrouter` | Custom | OpenRouter aggregator. Use the bracket form `openrouter[<slug>]` to pin a sub-provider |
| `gemini` | Custom | Google Gemini API (message format translation) |
| `claude` | Custom | Anthropic Claude via Google Vertex |
| `anthropic` | Custom | Direct Anthropic Messages API client |

Any other kind needs a [backend extension](backend-extensions.md) that registers
it; otherwise registry loading fails with `ValueError: Unknown adapter kind`.

### Weights across routes

A model's routes share its traffic by the weights written in the registry, and
the admin console can override a weight without editing the file. A routing
file can also shift weights between local and remote routes, but only on a
gateway without a database; see [The routing file](configuration.md#the-routing-file).
[Routing](routing.md) explains how a route is chosen for each request.

## Troubleshooting

### Model not appearing in `/v1/models`

- Confirm the registry the backend actually loaded. It logs
  `Registered N routes from <path>` at startup, and logs an error naming the
  path when no registry is there.
- Check the YAML syntax and indentation under `models:`.
- Check for skipped models: a route whose `${VAR}`-backed key or `base_url`
  resolves empty drops the model, and the log names both the model and the unset
  variable.
- If using `aliases`, verify the canonical `id` appears exactly once and aliases
  do not collide with another model's. A duplicate alias resolves to whichever
  model loads last, and is logged as a warning.

### Authentication failures

- A 401 from the gateway means your **gateway** API key was missing or invalid,
  or auth is enabled and you sent no `Authorization` header.
- A 401 surfaced from the route can mean the **upstream** key is wrong. Some
  gateways also return 401 for an unmatched path, so check the request URL and
  `embeddings_path` before replacing a valid key. Chat routing logs these failures
  as `upstream_auth_misconfig`; embeddings instead logs
  `Embedding request failed for model=...` with the upstream status and message.
- Check that `${ENV_VAR}` expansion resolved: only a value of exactly the form
  `${NAME}` is expanded, and only for `base_url`, `api_key`, `api_keys`, and
  `provider_model_id`, plus route-level `embeddings_path`.

## See also

- [Adding a New Local Model](add-local-model.md) — registering a self-hosted
  vLLM/SGLang/Ollama server
- [Quickstart](router-tutorial.md) — a runnable deployment from first
  request to local server
- [Routing through OpenRouter](openrouter.md) — architecture and endpoints
- [Routing](routing.md) — central weight overrides and strategies
- [Configuration](configuration.md) — environment and YAML configuration
