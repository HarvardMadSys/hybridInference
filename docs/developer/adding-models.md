# Adding a New Model

Add a model in **Admin → Routing → Create model**. Choose its router, pricing,
context/output limits, input capabilities, required role and first provider
route. Verify the route, save, then grant the intended users access. Ordinary
route edits propagate between registered backend processes without restarting.

For a non-OpenAI protocol, implement an adapter first; see
[Writing a Provider Adapter](provider-adapters.md). Local inference still uses
the OpenAI-compatible adapter; see [Adding a Local Model](add-local-model.md).

## Where your model registry lives

The catalog is the `MODEL_CATALOG` JSON setting in PostgreSQL. Application YAML
files and path-selection variables are retired. The gateway starts with no
models until an administrator configures them. See [Configuration](configuration.md).

## Adding a model

For aliases or additional metadata, edit the model catalog in
**Admin → Settings → Models**. Store credentials as separate secret settings and
refer to them as `${NAME}`. The catalog refuses inline credentials. The JSON
shape follows the field reference below; a small example is in
[The model registry](configuration.md#the-model-registry).

## Removing a model

In **Admin → Routing**, delete a model's last route. That removes the model
from the stored `MODEL_CATALOG`. It stops serving on this process now. Other
processes drop it on their next configuration refresh, and the next start does
not register it. Deleting one route while others remain only hides that route;
Restore brings it back, and the catalog is left as it is.

## Verify it through the gateway

```bash
make doctor
make smoke MODEL=your-model-id
```

Set `GATEWAY_API_KEY` in the shell when authentication is enabled. The smoke
uses the real inference path and normal quota. Check streaming separately when
clients depend on it. A catalog change marked **Restart required** needs
`make restart s=backend`; ordinary Admin route edits do not.

## Configuration Reference

### Model fields

These keys can appear on a model entry. Most of them can also be set on a
route entry, where the route value wins; `id` and `name` identify the model
itself and are read only at the model level.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique model identifier; the name clients send |
| `name` | string | No | Display name; defaults to `id` |
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
| `reasoning_efforts` | list[string] | No | The values this model's `reasoning_effort` accepts. Meaningful only when `reasoning_effort` is in `supported_params`; empty means "not offered". It has no default, because the accepted values differ from model to model. The model listings publish it; see [Reasoning effort in the model listings](#reasoning-effort-in-the-model-listings) |
| `on_demand` | bool | No | Model is lazily loaded on shared GPUs (started on first request, stopped when idle). Exposed in `/v1/models`, and the RouteWise background latency prober never probes such endpoints (default: false) |
| `processor` | string | No | Output-processor override for `OpenAICompatAdapter`, bypassing model-ID auto-detection. Accepted: `"default"`, `"glm"`, `"qwen_coder"`, `"think_block"` |
| `extra_body` | dict | No | Default fields merged into OpenAI-compatible upstream request bodies. Core fields and validated client parameters win |
| `priority_scheduling` | bool | No | The endpoint runs an sglang server started with `--enable-priority-scheduling`; see [Prioritizing decode on an sglang route](add-local-model.md#prioritizing-decode-on-an-sglang-route). Normally set per route, not per model |
| `route_metadata` | dict | No | Free-form per-route metadata consumed by routing strategies |
| `pricing` | dict | No | Base cost information. `prompt`, `completion`, `input_cache_reads` and `input_cache_writes` are USD per 1M tokens; `request` is USD per request and `image` USD per image |
| `pricing_schedule` | dict | No | UTC-only activation time plus recurring price windows: `pricing` stays active before `effective_at`; afterwards `default` applies outside each half-open `[start, end)` window, and a window's own `pricing` overrides the base fields. Optional `weekdays` (ISO integers 1=Monday through 7=Sunday) and `excluded_dates` (`YYYY-MM-DD`) restrict which UTC dates can use windows |
| `pricing_source` | dict | No | Automatically follow a supported provider: `{provider: deepseek, model: deepseek-flash}` or `deepseek-v4-pro`. Requires PostgreSQL; cannot be combined with `pricing_schedule`. Set to `null` on a route to use its own prices |

A model entry also accepts `route` (below), `router` and `router_params` (see
[Choosing a router per model](routing.md#choosing-a-router-per-model)), and
`required_role`, the lowest user role that can see and call the model
(`admin_only: true` is an older way to write `required_role: admin`).

When `required_role` is omitted, entries marked `admin_only: true` default to
`admin`. Otherwise, models with known or uncertain provider costs default to
`pro`; this includes unpriced LAN or cluster-DNS routes, environment-variable
endpoints that cannot be classified at load time, and `openai_compat` routes
even when their base URL is local. Zero user-facing prices do not prove that
the upstream provider is free. Add `required_role: free` to keep such a model
free-tier; set it explicitly whenever a route should be available at a
different tier, and that value remains authoritative. Unpriced local
`sglang`, `vllm`, and `ollama` engine routes can retain the `free` default
when their local endpoint is identifiable from configuration.

An explicit `required_role` also takes the place of `admin_only: true`, with
one exception kept from earlier releases: `admin_only: true` together with
`required_role: free` stays admin-only. To open such a model to the free tier,
remove `admin_only`.

### Reasoning effort in the model listings

`/v1/models` tells a client which `reasoning_effort` values a request would
deliver to each model, so a client can offer those and nothing else. The
OpenAI/OpenRouter shape carries `reasoning: {"supported_efforts": [...]}`, in
the order `reasoning_efforts` declares them; the Anthropic shape (an
`anthropic-version` header, or `/anthropic/v1/models`) carries
`capabilities.effort` as the Anthropic Models API spells it, with
`supported` and one `{"supported": true|false}` for each of `low`, `medium`,
`high`, `xhigh` and `max`. Only `effort` is filled in under `capabilities`.

A value is published only when every route of the model delivers it:

- a route with a `reasoning_effort_map` delivers the map's public levels (its
  keys, in their order), never the upstream values it sends;
- an `openai_compat` route (OpenRouter included) with `reasoning_effort` in
  `supported_params` delivers its `reasoning_efforts`;
- a route without `reasoning_effort` in `supported_params` delivers none, and
  the model is published with an empty list (`supported: false`);
- for anything else nothing is published (`reasoning: null`, no
  `capabilities`): a route whose adapter does not forward the parameter itself
  (`anthropic`, `claude`, `gemini`), `reasoning_effort` without a declared
  domain, or a domain that is not a list of distinct level names.

Auto models publish nothing, because selection picks the model per request.

### Mapping reasoning-effort levels on a route

The level a client chooses and the value a server takes are not always the
same word. A route can declare the translation with `reasoning_effort_map`:
each key is a **public level** (what clients send as `reasoning_effort` on
Chat Completions, `reasoning.effort` on Responses or `output_config.effort` on
Messages, and what `/v1/models` offers them), and each value is the **upstream
value** this route's server receives as `reasoning_effort`.

```json
{
  "id": "example-reasoner",
  "name": "Example reasoner",
  "provider": "vllm",
  "route": [
    {
      "kind": "vllm",
      "base_url": "${EXAMPLE_REASONER_BASE_URL}",
      "reasoning_effort_map": {"low": "high", "high": "xhigh", "max": "max"}
    }
  ]
}
```

Each key is the public level and each value what this server's
`reasoning_effort` takes.

On that route:

- exactly the keys are offered and accepted, in this order: the listing above
  publishes `low`, `high` and `max`. Any other level, `xhigh` and `medium`
  included, is refused with a 400 before anything is sent; it is never dropped
  or rounded to a neighbour. A thinking switch such as `none` is not a level
  unless the map names it;
- a level is translated once, on the route that serves the request, from the
  level the client sent. A retry, a fallback to another route or a reroute
  starts again from the client's level, and a route without a map sends it
  unchanged;
- the map is the whole declaration: the route forwards `reasoning_effort`
  whether or not `supported_params` names it, and `reasoning_efforts` is not
  read for it;
- a request that names no level sends none, so the server's own default
  applies. The gateway adds no default. Clients have their own: Claude Code,
  for one, sends `output_config.effort: high` when its user chose nothing.

The map is a fact about one server, so it is declared on the route and
rejected on the model (including a shorthand model without a route list), and
only a route whose adapter sends `reasoning_effort` (an OpenAI-compatible
kind) may carry one: on `anthropic`, `claude` or `gemini` it is a
configuration error. Each key and value is one short word of letters, digits,
`_`, `.` or `-`. Routes without a map behave as they always did. An admin can
set or replace a route's map from the console; see
[Route capabilities](configuration.md#routing-tab).

### Automatic DeepSeek prices

To follow DeepSeek prices automatically, configure the official API model id as a pricing source. The source is independent of the inference endpoint, so a self-hosted route can use these reference prices too. PostgreSQL stores the last validated snapshot; no API key is sent to the pricing or calendar sources.

```json
{
  "pricing_source": {
    "provider": "deepseek",
    "model": "deepseek-flash"
  }
}
```

The gateway checks the [official English price page](https://api-docs.deepseek.com/quick_start/pricing/) every five minutes for USD input, output and cache-hit prices and UTC peak windows. It reads the current and following year from [holiday-cn](https://github.com/NateScarlet/holiday-cn), a third-party dataset generated from State Council notices. This calendar is not a DeepSeek API. Published future-year dates are picked up automatically; an empty placeholder is not treated as a year without holidays.

Validated snapshots replace the stored tariff atomically. Workers reload it every ten seconds; each request keeps the snapshot and time captured at its start, including streaming requests. Peak/off-peak switching happens locally at the configured boundaries, without waiting for a network poll. This is periodic synchronization, not a real-time guarantee that all workers observe an official price change simultaneously.

Fetch or parsing failures retain the last validated snapshot and are logged. A first startup needs either a usable stored snapshot or a successful fetch. Unknown calendars and unrecognized price-page rules cannot silently become zero prices or an empty holiday list. The parser currently accepts the documented weekday schedule only while its UTC peak windows stay within the same Chinese calendar day.

### Calendar-aware prices

For example, apply higher prices on weekdays except configured public holidays:

```json
{
  "pricing": {
    "prompt": "0.15",
    "completion": "0.60",
    "input_cache_reads": "0.003"
  },
  "pricing_schedule": {
    "effective_at": "2026-10-17T00:00:00Z",
    "timezone": "UTC",
    "weekdays": [
      1,
      2,
      3,
      4,
      5
    ],
    "excluded_dates": [
      "2026-12-25"
    ],
    "default": {
      "prompt": "0.15",
      "completion": "0.60",
      "input_cache_reads": "0.003"
    },
    "windows": [
      {
        "start": "01:00",
        "end": "04:00",
        "pricing": {
          "prompt": "0.30",
          "completion": "1.20",
          "input_cache_reads": "0.006"
        }
      },
      {
        "start": "06:00",
        "end": "10:00",
        "pricing": {
          "prompt": "0.30",
          "completion": "1.20",
          "input_cache_reads": "0.006"
        }
      }
    ]
  }
}
```

Omitting the calendar fields preserves daily schedules. On excluded dates and
non-selected weekdays, all windows are skipped and `default` applies. Overnight
windows use the current UTC date for both restrictions. For these manually
configured schedules, deployment owners maintain holiday exclusions and prices;
use `pricing_source` above for automatic updates. A request keeps the prices
selected at its start, even if a stream finishes across a boundary.

### Route configuration

Routes give one model several endpoints with weighted distribution:

```json
{
  "route": [
    {
      "kind": "vllm",
      "weight": 0.7,
      "base_url": "http://localhost:8000",
      "provider_model_id": "/models/local-model"
    },
    {
      "kind": "your_provider",
      "weight": 0.3,
      "base_url": "https://api.provider.example",
      "api_key": "${API_KEY}"
    }
  ]
}
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
| `deployment_type` | string | Route-only admission classification: `auto` (default, recognizes loopback/host endpoints), `self_hosted` (an engine you operate, including on a LAN), or `provider_api` (provider-key concurrency, even through a loopback proxy). Independent of `provider_type`; see [Outbound concurrency](routing.md#outbound-concurrency) |
| `include_usage_in_stream` | bool | Whether this OpenAI-compatible upstream answers `stream_options.include_usage` with a final usage chunk, so cached-token counts and completion tokens on tool-call-only streams are measured rather than estimated. Route-level only, and a boolean: Admin refuses a model-wide or non-boolean value (`"false"`, `null`) when the catalog is saved, and a stored catalog with one registers no catalog model and logs an error. An explicit route value wins; otherwise defaults per `kind`: on for `deepseek`, `zai`, `kimi`, `minimax`, `vllm`, `sglang`, off otherwise. OpenRouter always requests usage regardless of this flag; native Anthropic, Claude, and Gemini adapters do not use it. Set `false` on a compatible endpoint that rejects unknown body fields |
| `routewise_pool`, `quota_pool`, `concurrency_pool`, `quota_source`, `quota`, `concurrency` | — | RouteWise pool and budget metadata |
| `reasoning_effort_map` | dict | Route-only. The public reasoning-effort levels this route takes, each mapped to the value its upstream receives as `reasoning_effort`; see [Mapping reasoning-effort levels on a route](#mapping-reasoning-effort-levels-on-a-route) |

A route reports its `kind` as the provider label — the value recorded in
`api_logs.provider` and grouped on by every provider-scoped admin view. Two
routes of the same kind therefore share one dashboard row; `provider:` splits
them.

#### Custom embeddings paths

An OpenAI-compatible gateway may use an API prefix other than `/v1`. Set
`embeddings_path` on that route to avoid appending an unwanted `/v1` segment:

```json
{
  "models": [
    {
      "id": "text-embedding-example",
      "name": "Example embeddings",
      "provider": "openai_compat",
      "model_type": "embedding",
      "route": [
        {
          "kind": "openai_compat",
          "base_url": "https://gateway.example/api/v2",
          "api_key": "${EMBEDDING_API_KEY}",
          "embeddings_path": "/embeddings"
        }
      ]
    }
  ]
}
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
instruction to use the default if the variable is missing. For a required
route, a missing or blank variable means the catalog registers no models, with
an error naming the model, route, and variable. With `optional: true`, only
that route is skipped, with a warning naming the same.

Admin refuses a catalog with any other invalid `embeddings_path`, even on an
optional route. A stored catalog with one registers no catalog model and logs
an error naming the model, so the gateway never serves part of the registry.

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

- Confirm the catalog the backend actually loaded. It logs
  `Registered N routes from the database route catalog` at startup, and logs
  an error when the catalog has no models.
- Check that the model is an entry of the catalog's `models` list. Admin
  validates the JSON when you save it and names the field it refuses.
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
- [Configuration](configuration.md) — where each setting lives, and the application documents
