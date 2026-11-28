# Adding a New Model

Add a model in **Admin → Routing → Create model**: choose its router, pricing,
context and output limits, input capabilities, required role and first
provider route, then verify the route and save. Route edits apply across all
backend processes without a restart.

A provider with its own protocol needs an adapter first; see
[Writing a Provider Adapter](provider-adapters.md). Local inference servers use
the OpenAI-compatible adapter; see [Adding a New Local Model](add-local-model.md).

## Where your model registry lives

The model registry is the `MODEL_CATALOG` JSON setting in PostgreSQL, edited in
**Admin → Routing → Models**. Edit it for aliases or other metadata that
**Create model** does not offer. No model file is read, and a fresh gateway has
no models.

Store credentials as secret settings and reference them as `${NAME}`; the
catalog refuses inline credentials. A catalog change is marked
**Restart required** and applies after `make restart s=backend`. A minimal
entry is in [The model registry](configuration.md#the-model-registry), and every
field is in the [Configuration Reference](#configuration-reference).

## Removing a model

Delete the model's last route in **Admin → Routing**. That removes the model
from `MODEL_CATALOG`: it stops serving at once, other processes drop it at
their next refresh, and the next start does not register it. Deleting a route
while others remain only hides that route until you **Restore** it.

## Verify it through the gateway

```bash
make doctor
make smoke MODEL=your-model-id
```

Set `GATEWAY_API_KEY` first when authentication is on. The smoke test uses the
real inference path and quota; check streaming separately if clients need it.

## Configuration Reference

### Model fields

Most fields can also be set on a route, where the route's value wins. `id` and
`name` are read only on the model.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | required | The name clients send |
| `name` | string | `id` | Display name |
| `provider` | string | required | Adapter kind, and the default `route[].kind`. On a route, `provider` is only a dashboard label |
| `base_url` | string | required here or on every route | API base URL |
| `api_key` | string | | API key, as a `${NAME}` reference |
| `provider_model_id` | string | `id` | Model name sent upstream |
| `model_type` | string | `chat` | `chat` or `embedding` (alias `type`). Embedding models skip the weighted router and try their routes in order |
| `aliases` | list[string] | | Other names for the model |
| `quantization` | string | `bf16` | Quantization format |
| `input_modalities` | list[string] | `["text"]` | `text`, `image` |
| `output_modalities` | list[string] | `["text"]` | `text` |
| `context_length` | int | 8192 | Context window |
| `max_output_length` | int | 4096 | `max_tokens` is clamped to it |
| `supports_tools` | bool | false | Function calling |
| `supports_structured_output` | bool | false | JSON mode |
| `supported_params` | list[string] | `temperature`, `top_p`, `max_tokens` | Parameters forwarded upstream |
| `reasoning_efforts` | list[string] | | Values `reasoning_effort` accepts; read only when `supported_params` includes `reasoning_effort`. No default, because models differ. See [Reasoning effort in the model listings](#reasoning-effort-in-the-model-listings) |
| `on_demand` | bool | false | Loaded on demand on shared GPUs. Shown in `/v1/models`; RouteWise's latency prober skips it |
| `processor` | string | auto | Output processor for `OpenAICompatAdapter`: `default`, `glm`, `qwen_coder` or `think_block` |
| `extra_body` | dict | | Fields merged into OpenAI-compatible request bodies; core fields and client parameters win |
| `priority_scheduling` | bool | false | Normally set per route; see [Prioritizing decode on an sglang route](add-local-model.md#prioritizing-decode-on-an-sglang-route) |
| `route_metadata` | dict | | Free-form metadata for routing strategies |
| `pricing` | dict | zeros | `prompt`, `completion`, `input_cache_reads` and `input_cache_writes` per 1M tokens; `request` per request; `image` per image |
| `pricing_currency` | string | `USD` | `USD` or `CNY`, for `pricing` and `pricing_schedule`; see [Accounting Currency](accounting.md) |
| `pricing_schedule` | dict | | Price windows by time of day; see [Calendar-aware prices](#calendar-aware-prices) |
| `pricing_source` | dict | | Follow a provider's published prices; see [Automatic DeepSeek prices](#automatic-deepseek-prices). `null` on a route keeps that route's own prices |

A model also takes `route` (below), `router` and `router_params` (see
[Choosing a router per model](routing.md#choosing-a-router-per-model)), and
`required_role`, the lowest user role that can see and call the model.

Without `required_role`, a model defaults to `free` only if it has no price and
every route is a `sglang`, `vllm` or `ollama` engine on `localhost`,
`127.0.0.1`, `0.0.0.0` or `host.docker.internal`. Every other model defaults to
`pro`, because a zero price does not prove the upstream is free: an
`openai_compat` route on localhost can forward to a paid API, and a `${VAR}` URL
cannot be classified. Set `required_role` to choose another tier.
`admin_only: true` is an older way to write `required_role: admin`; an explicit
`required_role` replaces it, except that `required_role: free` with
`admin_only: true` stays admin-only.

### Reasoning effort in the model listings

`/v1/models` lists the `reasoning_effort` values each model accepts, so a
client can offer only those. The OpenAI shape carries
`reasoning: {"supported_efforts": [...]}`. The Anthropic shape (an
`anthropic-version` header, or `/anthropic/v1/models`) carries
`capabilities.effort`: `supported`, plus one `{"supported": true|false}` for
each of `low`, `medium`, `high`, `xhigh` and `max`.

A value is listed only when every route of the model delivers it. A route
delivers:

1. the keys of its `reasoning_effort_map`;
2. otherwise, on the OpenAI-compatible adapter (OpenRouter included) with
   `reasoning_effort` in `supported_params`, its `reasoning_efforts`;
3. otherwise the levels of a matching [deployment preset](#deployment-presets);
4. otherwise nothing, if `supported_params` leaves `reasoning_effort` out; the
   model is then listed with no values (`supported: false`).

No effort information is listed (`reasoning: null`) when a route's adapter does
not forward the parameter (`anthropic`, `claude`, `gemini`) or names
`reasoning_effort` without a valid `reasoning_efforts` list. Auto models list
none.

### Mapping reasoning-effort levels on a route

When the level clients send differs from the value a server takes, declare the
translation with `reasoning_effort_map` on the route. Each key is a public level
(what clients send as `reasoning_effort`, `reasoning.effort` on Responses or
`output_config.effort` on Messages, and what `/v1/models` lists). Each value is
what this server receives as `reasoning_effort`.

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

On that route:

- Only the keys are accepted, in their order (`low`, `high`, `max` above). Any
  other level, including a switch such as `none`, is refused with `400` before
  anything is sent.
- The level is translated once, by the route that serves the request. A retry
  or fallback starts again from the client's level.
- The route forwards `reasoning_effort` whatever `supported_params` says, and
  ignores `reasoning_efforts`.
- A request without a level sends none, and the gateway adds no default. Claude
  Code, for one, sends `output_config.effort: high` when its user chose nothing.

The map is allowed only on a route, and only on an OpenAI-compatible kind; on
`anthropic`, `claude` or `gemini` it is a configuration error. Keys and values
are up to 32 letters, digits, `_`, `.` or `-`. Admins can set a route's map in
the console; see [Routing tab](configuration.md#routing-tab).

### Deployment presets

A trusted backend extension can register a verified mapping with
`register_reasoning_effort_preset` (from
`serving.config.reasoning_effort_presets`) in its `register()` hook. A preset
names the upstream provider, adapter kind, exact upstream model ID and base URL
it was tested against, plus a display label and an `effort_map`. It applies only
to that exact target. The provider is the route's real upstream, not its
dashboard label; for an OpenRouter pin, use provider `openrouter` and kind
`openrouter[<pin>]`. Admin overrides, route maps and a route's own effort
declarations take precedence over a preset.

## Automatic DeepSeek prices

To follow DeepSeek's published prices, set the official model as the pricing
source. It works for any route, self-hosted ones included. It needs PostgreSQL,
cannot be combined with `pricing_schedule`, and supports `deepseek-flash` and
`deepseek-v4-pro`.

```json
{
  "pricing_source": {
    "provider": "deepseek",
    "model": "deepseek-flash"
  }
}
```

Every five minutes the gateway reads DeepSeek's
[English price page](https://api-docs.deepseek.com/quick_start/pricing/) (USD
accounting) or [Chinese price page](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
(CNY accounting), and Chinese public holidays for this year and next from
[holiday-cn](https://github.com/NateScarlet/holiday-cn), a third-party dataset.
No API key is sent to either. Peak windows are resolved in UTC and switch
locally at their boundaries. Workers reload the stored prices every ten
seconds, and each request keeps the prices it started with.

A failed fetch or parse keeps the last validated prices and is logged. A first
start needs stored prices or a successful fetch. An unrecognized page or
calendar never turns into zero prices or an empty holiday list. The parser
accepts DeepSeek's weekday schedule only while its UTC peak windows stay within
one Chinese calendar day.

### Calendar-aware prices

`pricing_schedule` sets prices by UTC time. Before `effective_at`, `pricing`
applies. After it, each window's `pricing` applies inside its half-open
`[start, end)` range and `default` applies outside. `weekdays` (ISO numbers,
1 = Monday) and `excluded_dates` (`YYYY-MM-DD`) limit the UTC dates that use
windows; other dates use `default` all day, and an overnight window checks the
current UTC date. You maintain these holidays and prices yourself. A request
keeps the prices selected at its start, even if its stream crosses a boundary.

For example, higher prices on weekday mornings, except on a listed holiday:

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

## Route configuration

Routes give one model several endpoints and split its traffic by weight:

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

A route takes the model fields above, plus:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | string | Adapter kind ([below](#supported-adapter-kinds)); defaults to the model's `provider` |
| `weight` | float | Share of traffic (default 1.0). `0` keeps the route configured but unused, even as a fallback |
| `api_keys` | list[string] | A key pool instead of `api_key` (not both). The pool counts as one endpoint, so put separate resources on separate routes |
| `embeddings_path` | string | Embeddings path under `base_url`; see [Custom embeddings paths](#custom-embeddings-paths) |
| `optional` | bool | Skip only this route, with a warning, when a `${VAR}` key, `base_url` or `embeddings_path` is unset or blank |
| `provider`, `provider_display_name` | string | Dashboard label and display name; they do not select an adapter. See [Naming a route in the dashboard](add-local-model.md#naming-a-route-in-the-dashboard) |
| `provider_type` | string | RouteWise cost category: `on_demand`, `quota` or `concurrency` |
| `deployment_type` | string | Admission class: `auto` (default; recognizes loopback and Docker-host addresses), `self_hosted` (an engine you run, also on a LAN) or `provider_api` (provider-key concurrency, even through a loopback proxy). See [Outbound concurrency](routing.md#outbound-concurrency) |
| `include_usage_in_stream` | bool | Ask an OpenAI-compatible upstream for a final usage chunk, so cached and tool-call tokens are measured, not estimated. On by default for `deepseek`, `zai`, `kimi`, `minimax`, `vllm` and `sglang`. Set `false` for an endpoint that rejects unknown fields. Route-only, and must be a real boolean. OpenRouter always asks for usage |
| `routewise_pool`, `quota_pool`, `concurrency_pool`, `quota_source`, `quota`, `concurrency` | | RouteWise pool and budget metadata |
| `reasoning_effort_map` | dict | Route-only; see [Mapping reasoning-effort levels on a route](#mapping-reasoning-effort-levels-on-a-route) |

Without `provider:`, a route's dashboard label is its `kind`, so two routes of
the same kind share one row.

### Custom embeddings paths

A base URL ending in `/v1` gets `/embeddings`, and any other base gets
`/v1/embeddings` (trailing slashes are removed first). For a gateway with
another prefix, set `embeddings_path` on the route:

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

This route then calls `https://gateway.example/api/v2/embeddings`; its
fallbacks are unaffected. `null` or `""` keeps the default. The field is
route-only, and Admin refuses a whitespace-only value, a non-string or a `..`
segment. A `${VAR}` value must resolve to a non-blank path: if it does not, the
catalog registers no models, unless the route is `optional: true`, in which
case only that route is skipped.

### Supported adapter kinds

Every kind marked OpenAI-compat uses `OpenAICompatAdapter`, with the provider's
profile applied.

| Kind | Category | Notes |
|------|----------|-------|
| `openai_compat` | OpenAI-compat | Generic OpenAI-compatible endpoint; use when no specific kind fits |
| `staging` | OpenAI-compat | Same as `openai_compat` with its own label, to track a second generic endpoint separately |
| `vllm` | OpenAI-compat | Local vLLM inference server |
| `sglang` | OpenAI-compat | Local SGLang inference server |
| `ollama` | OpenAI-compat | Local or remote Ollama server |
| `chutes` | OpenAI-compat | Chutes.ai hosted inference |
| `featherless` | OpenAI-compat | Featherless.ai hosted inference |
| `cliproxy` | OpenAI-compat | CLI proxy endpoint for OpenAI-compatible models |
| `deepseek` | OpenAI-compat | DeepSeek API (DeepSeek usage profile) |
| `kimi` | OpenAI-compat | Moonshot/Kimi pay-per-token API under `/v1` (Kimi usage profile) |
| `zai` | OpenAI-compat | Z.AI's API under `/api/paas/v4/`; the profile appends `/chat/completions` without another version segment |
| `minimax` | OpenAI-compat | MiniMax API (MiniMax usage profile) |
| `openrouter` | Custom | OpenRouter; `openrouter[<slug>]` pins one upstream. See [OpenRouter routes](#openrouter-routes) |
| `gemini` | Custom | Google Gemini API |
| `claude` | Custom | Anthropic Claude via Google Vertex |
| `anthropic` | Custom | Anthropic Messages API directly |

Any other kind needs a [backend extension](backend-extensions.md) that
registers it. Otherwise Admin refuses the catalog: the kind "names no adapter
kind this gateway has".

### Weights across routes

Traffic splits by the catalog weights, and Admin can override a weight per
endpoint without editing the catalog. On a gateway without a database, the
routing policy can also shift weights between local and remote routes. See
[Routing](routing.md).

## OpenRouter routes

Add the OpenRouter key in **Admin → Providers** (the keys are on its overview
page), then create the model and its OpenRouter route in **Admin → Routing**.
`config/examples/models.openrouter.json` is an example catalog to import into
`MODEL_CATALOG`, including a local model with an OpenRouter fallback. Add the
keys it references as secret settings first, and check its model ids and
prices for your account: the gateway bills from catalog prices.

```json
{
  "kind": "openrouter",
  "weight": 1.0,
  "base_url": "https://openrouter.ai/api/v1",
  "api_keys": [
    "${OPENROUTER_API_KEY}"
  ],
  "provider_model_id": "meta-llama/llama-3.3-70b-instruct"
}
```

`provider_model_id` is OpenRouter's name for the model, independent of the `id`
your clients use. With `kind: openrouter`, OpenRouter picks the upstream
provider. To pin one, use `kind: openrouter[<slug>]`, for example
`openrouter[deepinfra]`: requests then carry
`provider: {order: [<slug>], allow_fallbacks: false}`. A slug matches
`[A-Za-z0-9_.-]+`, optionally in `/`-separated segments (`deepinfra/turbo`);
OpenRouter lists them at <https://openrouter.ai/docs/features/provider-routing>.
Each pin gets its own `endpoint_id` and circuit breaker, while `api_logs.provider`
records `openrouter` for all of them.

An unpinned route created in Admin can set `openrouter_sort` to `price`,
`throughput` or `latency`, sent as `provider: {sort: <policy>}`. A pin takes
precedence.

`OpenRouterAdapter` also sends:

- `X-Title` and `HTTP-Referer`, from `SITE_NAME` and `SITE_PUBLIC_BASE_URL` (or
  `SITE_CONFIGURATION`). OpenRouter credits leaderboard placement and free-tier
  limits to that site. Without a public address no `HTTP-Referer` is sent, and
  without a name `X-Title` is `HybridInference`.
- `usage: {include: true}`, plus `stream_options: {include_usage: true}` when
  streaming, so OpenRouter reports each request's cost. It is stored as
  `upstream_cost_usd`, beside `cost_usd`, which is what the caller pays at
  catalog prices.

Errors follow the normal fallback and
[circuit breaker](routing.md#endpoint-health-and-circuit-breaking) rules. 401 (a
rejected OpenRouter key) and 407 (an egress proxy) count against the circuit and
page. 402 and 403 do not count by themselves, but on an `api_keys:` pool they
rotate and then mute the key, so an account out of credit soon has no usable key
and the circuit opens. 403 does not page, because providers also use it to
refuse single requests. If every OpenRouter request returns 401, the gateway's
key is wrong: check it in **Admin → Providers**.

## Troubleshooting

### Model not appearing in `/v1/models`

- At startup the backend logs `Registered N routes from the database route
  catalog`, or an error when the catalog is empty or was refused
  (`The model catalog (MODEL_CATALOG in Admin > Providers > Models) was not
  registered: …`).
- A route whose `${VAR}` key or `base_url` is unset drops its model, and the log
  names the model and the variable. Mark the route `optional: true` to drop only
  that route.
- Check the model's required role, and for an Admin Routing model, whether its
  route is verified and applied.
- An alias claimed by two models resolves to the one loaded last, with a
  warning in the log.

### Authentication failures

- A `401` from the gateway means your gateway API key is missing or invalid.
- A `401` from the upstream usually means a wrong upstream key. Chat logs it as
  `upstream_auth_misconfig`; embeddings log `Embedding request failed for
  model=...`. Some gateways also answer 401 for an unknown path, so check the URL
  and `embeddings_path` first.
- Only a whole value of the form `${NAME}` is expanded, and only in `base_url`,
  `api_key`, `api_keys`, `provider_model_id` and a route's `embeddings_path`.

For a runnable gateway from first request to local server, see the
[Quickstart](router-tutorial.md).
