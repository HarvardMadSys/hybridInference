# Adding a New Model

This guide explains how to add LLM models and providers to the HybridInference
gateway while keeping the OpenAI-compatible API surface that clients see.

There is a single guide for both needs. Depending on your case, follow one of:

1. Use an existing provider adapter — only registry YAML plus environment changes.
2. Integrate a new provider — add or wire an adapter, then the registry YAML and
   environment.

For a model you serve yourself on vLLM, SGLang, or Ollama, see
[Adding a New Local Model](add-local-model.md), which owns route registration for
local servers.

## Where your model registry lives

**There is no `config/models.yaml` in this repository.** The backend resolves the
registry path at startup in
`apps/backend/serving/config/distribution.py`
(`resolve_config_path("models")`), with this precedence:

1. **`MODELS_CONFIG_PATH`** (legacy alias `MODELS_CONFIG`). An explicit path
   always wins.
2. **A distribution manifest's `paths.models`** — used only when
   `DISTRIBUTION_CONFIG_PATH` names a manifest *and* `DISTRIBUTION_CONFIG_MODE=active`.
   Relative values in the manifest resolve against the manifest's own directory.
   `DISTRIBUTION_CONFIG_MODE` defaults to `dark`, where the manifest is loaded,
   validated, and logged for comparison but never applied.
3. **`config/examples/models.openrouter.yaml`** — the bundled reference registry a
   fresh checkout serves when nothing else is configured.

Routing configuration follows the same chain (`ROUTING_CONFIG_PATH` /
`paths.routing`, defaulting to `config/examples/routing.minimal.yaml`).

So a self-hoster has two shapes to choose from:

- **Point at a file.** Keep your registry wherever you like and set
  `MODELS_CONFIG_PATH=/path/to/models.yaml`. This is what the quickstart in
  `README.md` does.
- **Ship an overlay.** Create `distributions/<name>/` containing a
  `distribution.yaml` manifest and a `config/models.yaml`, then set
  `DISTRIBUTION_CONFIG_PATH=distributions/<name>/distribution.yaml` and
  `DISTRIBUTION_CONFIG_MODE=active`. `distributions/example/` is a working
  overlay you can copy; `config/examples/distribution.example.yaml` is an
  annotated manifest.

Throughout this guide, "your model registry" means whichever file that
resolution picks.

If no registry is found at the resolved path, the backend logs an error, serves
an empty `/v1/models`, and reports every model as not found.

## Quick Start: adding a model to an existing provider

If the provider already has an adapter, you only need configuration.

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

2. **Set the environment variables** in `.env` at the repository root (the
   backend's settings loader reads that file):

```bash
PROVIDER_BASE_URL=https://api.provider.example/v1
PROVIDER_API_KEY=your-api-key
```

3. **Restart the backend** to load the new model.

4. **Verify** (see [Step 5: verify through the gateway](#step-5-verify-through-the-gateway)).

Note on aliases: to let clients also call the model under a second name — an
OpenRouter-style vendor slug, or the raw model path your serving runtime uses —
list those names in `aliases`. They resolve to the same routes.

## Adding a New Provider

### Step 1: decide whether you need an adapter at all

Most new providers expose an OpenAI-style `/chat/completions` endpoint. For
those, **do not write an adapter class.** Register a provider profile and add
the kind to the OpenAI-compat dispatch tuple in
`apps/backend/serving/servers/registry.py` (`_make_adapter`):

`_make_adapter` is one long `if kind ...` / `elif kind ...` chain over the
route's `kind`. Add an arm to it for the profile, then add the kind to the
OpenAI-compat tuple further down the same function:

```python
# ...among the per-kind arms of _make_adapter:
elif kind == "your_provider":
    cfg = {**cfg, "provider_profile": "your_provider"}

# ...further down in the same function:
if kind in (
    "vllm",
    "sglang",
    "chutes",
    "featherless",
    "ollama",
    "cliproxy",
    "openai_compat",
    "staging",
    "deepseek",
    "kimi",
    "minimax",
    "your_provider",  # <-- add it here
):
    return OpenAICompatAdapter(model_cfg)
```

This is how `deepseek`, `kimi`, and `minimax` are integrated today: a
per-provider profile in `apps/backend/serving/adapters/profiles.py` carries the
usage-metric or path quirks, and `OpenAICompatAdapter` does the rest. `zai` and
`kimi_coding` use the same profile mechanism but are gated on a coding-tool
identity, so `_make_adapter` short-circuits them to `CodingIdentityAdapter` (a
thin `OpenAICompatAdapter` subclass) before reaching that tuple.

Write a dedicated adapter only when the provider speaks a genuinely non-OpenAI
wire format — Gemini's `generateContent`, the Anthropic Messages API,
OpenRouter's provider-pinning body fields. `gemini`, `claude`, `anthropic`, and
`openrouter` all follow that pattern, each with its own dispatch branch:

```python
if kind == "your_provider":
    return YourProviderAdapter(model_cfg)
```

### Step 2: write the adapter (custom protocols only)

Create a new file under `apps/backend/serving/adapters/`, for example
`apps/backend/serving/adapters/your_provider.py`. The backend package root is
`apps/backend`, so imports are written as `serving.…` / `routing.…`:

```python
import json
from collections.abc import AsyncGenerator
from typing import Any

from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens
from .base import BaseAdapter, UsageInfo


class YourProviderAdapter(BaseAdapter):
    """Adapter for YourProvider API.

    This adapter translates OpenAI-compatible requests to YourProvider's
    API format and normalizes responses back to OpenAI format.
    """

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> dict[str, Any]:
        """Execute a non-streaming chat completion request.

        Args:
            messages: List of chat messages in OpenAI format.
            **params: Additional parameters (temperature, max_tokens, etc.).

        Returns:
            OpenAI-compatible response dictionary.
        """
        # Validate and clamp parameters against this model's declared support.
        validated_params = self.validate_params(params)

        # Build the provider-specific request payload.
        payload = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            **validated_params,
        }

        if params.get("tools"):
            payload["tools"] = params["tools"]

        if params.get("response_format", {}).get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

        data = await self.http.json_post_with_retry(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        usage = UsageInfo(
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
            total_tokens=data.get("usage", {}).get("total_tokens", 0),
        )

        # Fall back to estimation when the provider reports no usage.
        if usage.total_tokens == 0:
            content = data["choices"][0]["message"].get("content", "")
            prompt_tokens = estimate_prompt_tokens(messages)
            completion_tokens = estimate_text_tokens(content)
            usage = UsageInfo(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=int(prompt_tokens + completion_tokens),
            )

        tool_calls = None
        if "tool_calls" in data["choices"][0]["message"]:
            tool_calls = data["choices"][0]["message"]["tool_calls"]

        return self.format_response(
            content=data["choices"][0]["message"].get("content", ""),
            model=self.config.id,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
        )

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        """Execute a streaming chat completion request.

        Args:
            messages: List of chat messages in OpenAI format.
            **params: Additional parameters.

        Yields:
            Server-sent event formatted strings.
        """
        validated_params = self.validate_params(params)

        payload = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            "stream": True,
            **validated_params,
        }

        if params.get("tools"):
            payload["tools"] = params["tools"]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

        total_content = ""
        prompt_tokens = 0

        async for line in self.http.stream_post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers,
        ):
            if not line.startswith("data: "):
                continue

            if line == "data: [DONE]":
                # Emit the final usage chunk with the shared helper.
                yield make_final_usage_chunk(
                    model=self.config.id,
                    messages=messages,
                    total_content=total_content,
                    prompt_tokens_override=prompt_tokens or None,
                    finish_reason="stop",
                )
                yield done_sentinel()
                break

            try:
                chunk_data = json.loads(line[6:])

                if "usage" in chunk_data:
                    prompt_tokens = chunk_data["usage"].get("prompt_tokens", prompt_tokens)

                if chunk_data["choices"][0]["delta"].get("content"):
                    content = chunk_data["choices"][0]["delta"]["content"]
                    total_content += content
                    yield self.format_stream_chunk(content, self.config.id)
            except json.JSONDecodeError:
                continue
```

Export it from `apps/backend/serving/adapters/__init__.py`:

```python
from .your_provider import YourProviderAdapter

__all__ = [
    # ... existing exports
    "YourProviderAdapter",
]
```

and import it at the top of `apps/backend/serving/servers/registry.py`:

```python
from serving.adapters import (
    # ... existing imports
    YourProviderAdapter,
)
```

### Step 3: add the model to your registry

```yaml
models:
  - id: your-model-id
    name: Your Model Name
    provider: your_provider
    provider_model_id: "actual-model-id"
    base_url: ${YOUR_PROVIDER_BASE_URL}
    api_key: ${YOUR_PROVIDER_API_KEY}
    quantization: "bf16"
    input_modalities: ["text"]
    output_modalities: ["text"]
    context_length: 8192
    max_output_length: 4096
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop]
    aliases: []  # Optional alternative names
    pricing:
      prompt: "0"
      completion: "0"
      image: "0"
      request: "0"
      input_cache_reads: "0"
      input_cache_writes: "0"
    route:
      - kind: your_provider
        weight: 1.0
        base_url: ${YOUR_PROVIDER_BASE_URL}
        api_key: ${YOUR_PROVIDER_API_KEY}
```

### Step 4: configure environment variables

Add to `.env`:

```bash
YOUR_PROVIDER_BASE_URL=https://api.yourprovider.example/v1
YOUR_PROVIDER_API_KEY=your-api-key-here
```

A route whose `${VAR}`-backed `api_key`, `api_keys`, or `base_url` resolves to
empty is not registered. By default that drops the whole model, and the backend
logs which models were skipped and which variables were unset. Mark a route
`optional: true` to have only that route be skipped instead, leaving the rest of
the model registered.

### Step 5: verify through the gateway

Start the backend. From a checkout, the quickstart form is:

```bash
PYTHONPATH=apps/backend \
  MODELS_CONFIG_PATH=/path/to/your/models.yaml \
  uv run uvicorn serving.servers.app:app --port 8080
```

With the bundled Docker Compose setup, `make build s=backend` rebuilds and
restarts the backend instead.

`GET /v1/models` accepts an anonymous request — it resolves an API key only to
decide whether to include admin-visible entries:

```bash
curl -s http://localhost:8080/v1/models | jq
```

`POST /v1/chat/completions` runs through `verify_api_key`, so **it returns 401
without a valid gateway API key** unless the backend is running with
`USER_AUTH_ENABLED=false`. Send the key you issued for your own gateway (this is
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

These keys are read from a model entry and passed to `ModelConfig`
(`apps/backend/serving/adapters/base.py`). Most of them can also be set on a
route entry, where the route value wins; `id` and `name` identify the model
itself and are read only at the model level.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique model identifier; the name clients send |
| `name` | string | Yes | Display name |
| `provider` | string | Yes | Provider/adapter kind; also the default `route[].kind` when no `route:` list is given. Note the collision: at the model level `provider:` selects an adapter, while `provider:` on a *route* is only an analytics label — see the route-fields table |
| `base_url` | string | Yes | API endpoint base URL |
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
| `reasoning_efforts` | list[string] | No | The values this model's `reasoning_effort` accepts. Meaningful only when `reasoning_effort` is in `supported_params`; empty means "not offered". There is deliberately no default — the accepted set differs per model, so a guessed one would advertise a value the upstream rejects |
| `on_demand` | bool | No | Model is lazily loaded on shared GPUs (started on first request, stopped when idle). Exposed in `/v1/models`, and the RouteWise background latency prober never probes such endpoints (default: false) |
| `processor` | string | No | Output-processor override for `OpenAICompatAdapter`, bypassing model-ID auto-detection. Accepted: `"default"`, `"glm"`, `"qwen_coder"`, `"think_block"` |
| `extra_body` | dict | No | Default fields merged into OpenAI-compatible upstream request bodies. Core fields and validated client parameters win |
| `priority_scheduling` | bool | No | The endpoint runs an sglang server started with `--enable-priority-scheduling`; see [Prioritizing decode on an sglang route](add-local-model.md#prioritizing-decode-on-an-sglang-route). Normally set per route, not per model |
| `route_metadata` | dict | No | Free-form per-route metadata consumed by routing strategies |
| `pricing` | dict | No | Base cost information. `prompt`, `completion`, `input_cache_reads` and `input_cache_writes` are USD per 1M tokens; `request` is USD per request and `image` USD per image |
| `pricing_schedule` | dict | No | UTC-only activation time plus recurring daily price windows: `pricing` stays active before `effective_at`; afterwards `default` applies outside each half-open `[start, end)` window, and a window's own `pricing` overrides the base fields |

A few model-entry keys are not `ModelConfig` fields and are consumed elsewhere:
`route` (below), `router` and `router_params` (per-model router selection),
`admin_only`, and `required_role`.

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
| `weight` | float | Relative share of traffic (default 1.0). `0` keeps the route configured but unselected |
| `api_keys` | list[string] | Key pool for this endpoint, instead of `api_key`. Setting both is an error. The whole pool is one endpoint for latency and quota accounting; split genuinely separate resources into separate routes |
| `optional` | bool | When a `${VAR}`-backed key or `base_url` resolves empty, skip just this route instead of dropping the model |
| `provider` / `provider_display_name` | string | Analytics label override only — it renames the row in the dashboard and does **not** select an adapter; that is `kind`. See [Naming a route in the dashboard](add-local-model.md#naming-a-route-in-the-dashboard) |
| `provider_type` | string | RouteWise cost category: `on_demand`, `quota`, or `concurrency` |
| `routewise_pool`, `quota_pool`, `concurrency_pool`, `quota_source`, `quota`, `concurrency` | — | RouteWise pool and budget metadata |

A route reports its `kind` as the provider label — the value recorded in
`api_logs.provider` and grouped on by every provider-scoped admin view. Two
routes of the same kind therefore share one dashboard row; `provider:` splits
them.

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
| `kimi` | OpenAI-compat | Moonshot/Kimi pay-per-token API (applies the Kimi usage profile) |
| `kimi_coding` | OpenAI-compat | Kimi coding-plan endpoint; dispatches to `CodingIdentityAdapter` |
| `zai` | OpenAI-compat | Z.AI GLM coding plan: the chat path is `/chat/completions` appended to the base URL rather than the default `/v1/chat/completions`, because Z.AI's base URL already carries its version segment (`profiles.default_chat_path`). Dispatches to `CodingIdentityAdapter`, which presents a coding-tool `User-Agent` and a leading system message |
| `minimax` | OpenAI-compat | MiniMax API (applies the MiniMax usage profile) |
| `openrouter` | Custom | OpenRouter aggregator. Use the bracket form `openrouter[<slug>]` to pin a sub-provider |
| `gemini` | Custom | Google Gemini API (message format translation) |
| `claude` | Custom | Anthropic Claude via Google Vertex |
| `anthropic` | Custom | Direct Anthropic Messages API client |

Any other `kind` raises `ValueError: Unknown adapter kind` during registry load.

### Hybrid routing

Weighted routes are applied at registration time. The routing config file
(resolved via `ROUTING_CONFIG_PATH` / the manifest's `paths.routing`) can then
adjust weights centrally through `RoutingManager`. See
[Routing](routing.md).

## BaseAdapter API reference

All adapters inherit from `BaseAdapter`
(`apps/backend/serving/adapters/base.py`) and implement:

```python
async def chat_completion(
    self, messages: list[dict[str, Any]], **params
) -> dict[str, Any]:
    """Execute non-streaming chat completion."""

async def stream_chat_completion(
    self, messages: list[dict[str, Any]], **params
) -> AsyncGenerator[str, None]:
    """Execute streaming chat completion."""
```

Utility methods provided by the base class:

```python
def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp parameters to supported ranges."""

def format_response(
    self,
    content: str | None,
    model: str,
    usage: UsageInfo | None = None,
    tool_calls: list[dict] | None = None,
    reasoning_content: str | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """Format response in OpenAI-compatible format."""

def format_stream_chunk(
    self,
    content: str,
    model: str,
    finish_reason: str | None = None,
    role: str | None = None,
) -> str:
    """Format an SSE chunk for streaming responses."""

def format_tool_chunk(self, tool_calls: list[dict[str, Any]], model: str) -> str:
    """Format tool calls into an OpenAI-compatible streaming chunk."""
```

Available attributes:

```python
self.config       # ModelConfig instance
self.http         # AsyncHTTPClient (apps/backend/serving/http.py), shared
```

## Advanced features

### Multi-modal support

For models accepting images:

```yaml
input_modalities: ["text", "image"]
```

Handle the image content blocks in your adapter's `chat_completion`. A route may
declare narrower `input_modalities` than the model, so a text-only fallback never
receives media.

### Tool / function calling

```yaml
supports_tools: true
```

Parse the provider's tool calls into OpenAI shape and pass them through:

```python
tool_calls = []
if "function_call" in data:
    tool_calls.append({
        "id": f"call_{int(time.time() * 1000)}",
        "type": "function",
        "function": {
            "name": data["function_call"]["name"],
            "arguments": data["function_call"]["arguments"],
        },
    })

return self.format_response(
    content=content,
    model=self.config.id,
    usage=usage,
    tool_calls=tool_calls,
)
```

### Structured output (JSON mode)

```yaml
supports_structured_output: true
```

Handle the `response_format` parameter:

```python
if params.get("response_format", {}).get("type") == "json_object":
    payload["response_format"] = {"type": "json_object"}
```

### Rate limiting (there is none per provider)

There is no per-provider rate limiter to turn on; this section records where one
would go. `apps/backend/serving/servers/bootstrap.py` does not configure
per-provider rate limiters. The only in-process limiter wired there is `UserConcurrencyLimiter`.
Static auth-flow limits live in `apps/backend/serving/config/settings.py` (the
`signup_rate_limit_*` and `login_rate_limit_*` fields). A per-provider
token-bucket or quota would go in
`apps/backend/serving/admin/provider_quotas.py` (or a new module) and be surfaced
through `apps/backend/serving/servers/deps.py`.

## Examples

- **OpenAI-compatible provider.** DeepSeek has no adapter file. `_make_adapter`
  in `apps/backend/serving/servers/registry.py` sets
  `provider_profile = "deepseek"` and returns `OpenAICompatAdapter`; the profile
  lives in `apps/backend/serving/adapters/profiles.py`.
- **Custom API format.** See `apps/backend/serving/adapters/gemini.py` for
  message conversion against a non-OpenAI wire format.
- **Local deployment.** vLLM and SGLang reuse
  `apps/backend/serving/adapters/openai_compat.py`. The `vllm` and `sglang` kinds
  dispatch to the same class; local-vs-remote behaviour comes from `base_url`
  and the routing layer, not from a dedicated adapter.

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
- A 401 surfaced from the route means the **upstream** key is wrong. The backend
  logs it as `upstream_auth_misconfig` with the endpoint id and the upstream's
  own error body.
- Check that `${ENV_VAR}` expansion resolved: only a value of exactly the form
  `${NAME}` is expanded, and only for `base_url`, `api_key`, `api_keys`, and
  `provider_model_id`.

### Response format errors

- Ensure `format_response()` returns the OpenAI-compatible structure.
- Validate that `UsageInfo` fields are integers.
- Check `finish_reason` is one of `stop`, `length`, `content_filter`,
  `tool_calls`.
- For streaming, emit the first non-empty content chunk as soon as it is
  available so time-to-first-token is recorded accurately.

### Streaming issues

- Ensure chunks are SSE-formatted: `data: {json}\n\n`.
- Send the final usage chunk before `data: [DONE]`.
- Handle JSON parsing errors gracefully.

## Best practices

1. **Error handling** — use `self.http.json_post_with_retry()` and surface
   provider faults with useful messages.
2. **Usage accounting** — prefer provider-reported usage; fall back to
   `estimate_prompt_tokens()` / `estimate_text_tokens()`.
3. **Streaming helpers** — use `format_stream_chunk()`,
   `make_final_usage_chunk()`, and `done_sentinel()` for consistent SSE.
4. **Type safety** — full type hints, and keep request/response shapes aligned
   with `apps/backend/serving/schemas.py`.
5. **Testing** — exercise both streaming and non-streaming paths, and try large
   prompts to validate token clamping.
6. **Docs & style** — Google-style docstrings in English; keep provider-specific
   logic out of shared code.
7. **Secrets** — use `${ENV_VAR}` in YAML rather than hardcoding keys or
   endpoints, and keep the values in `.env`.

## See also

- [Adding a New Local Model](add-local-model.md) — registering a self-hosted
  vLLM/SGLang/Ollama server
- [Quickstart](router-tutorial.md) — a runnable deployment from first
  request to local server
- [Routing through OpenRouter](openrouter.md) — architecture and endpoints
- [Routing](routing.md) — central weight overrides and strategies
- [Configuration](configuration.md) — environment and YAML configuration
