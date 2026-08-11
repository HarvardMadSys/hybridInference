# Adding a New Model (OpenRouter-Compatible)

This guide explains how to add support for new LLM models and providers to the hybridInference gateway while keeping full OpenRouter/OpenAI API compatibility.

Reference PR for provider integration example: https://github.com/HarvardMadSys/hybridInference/pull/34

## Overview

There is a single guide for both needs. Depending on your case, follow one of:
1) Use an existing provider adapter — only YAML + env changes.
2) Integrate a new provider — add an adapter class + small registration changes, then YAML + env.

## Quick Start

### Adding a Model with an Existing Provider

If the provider is already supported, you only need to add configuration.

1. **Add model configuration** in `config/models.yaml`:

```yaml
models:
  - id: your-model-id
    name: Your Model Display Name
    provider: existing_provider  # e.g., "gemini", "deepseek"
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
        base_url: ${PROVIDER_BASE_URL}
        api_key: ${PROVIDER_API_KEY}
```

2. **Configure environment variables** in `.env`:

```bash
PROVIDER_BASE_URL=https://api.provider.com/v1
PROVIDER_API_KEY=your-api-key
```

3. **Restart the server** to load the new model.

4. **Verify**:
```bash
curl http://localhost:8080/v1/models | jq
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model-id","messages":[{"role":"user","content":"Hello"}]}' | jq
```

Note on aliases: If you want the model to appear under an OpenRouter-style slug (e.g., a local vLLM path), add it in `aliases` so clients can call either name.

## Adding a New Provider

If you need to integrate a completely new provider, follow these steps:

### Step 1: Create Provider Adapter

Create a new file in `serving/adapters/` (e.g., `serving/adapters/your_provider.py`):

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
        # Validate and filter parameters
        validated_params = self.validate_params(params)

        # Build provider-specific request payload
        payload = {
            "model": self.config.provider_model_id or self.config.id,
            "messages": messages,
            **validated_params,
        }

        # Add optional features
        if params.get("tools"):
            payload["tools"] = params["tools"]

        if params.get("response_format", {}).get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}

        # Set up authentication headers
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

        # Make API request
        data = await self.http.json_post_with_retry(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # Extract usage information
        usage = UsageInfo(
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
            total_tokens=data.get("usage", {}).get("total_tokens", 0),
        )

        # Fallback to estimation if provider doesn't return usage
        if usage.total_tokens == 0:
            content = data["choices"][0]["message"].get("content", "")
            prompt_tokens = estimate_prompt_tokens(messages)
            completion_tokens = estimate_text_tokens(content)
            usage = UsageInfo(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                total_tokens=int(prompt_tokens + completion_tokens),
            )

        # Extract tool calls if present
        tool_calls = None
        if "tool_calls" in data["choices"][0]["message"]:
            tool_calls = data["choices"][0]["message"]["tool_calls"]

        # Return normalized response
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
                # Emit final usage chunk using shared helper for consistency
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

                # Extract usage if available
                if "usage" in chunk_data:
                    prompt_tokens = chunk_data["usage"].get("prompt_tokens", prompt_tokens)

                # Extract and yield content delta
                if chunk_data["choices"][0]["delta"].get("content"):
                    content = chunk_data["choices"][0]["delta"]["content"]
                    total_content += content
                    yield self.format_stream_chunk(content, self.config.id)
            except json.JSONDecodeError:
                continue
```

### Step 2: Register the Adapter

1. **Update `serving/adapters/__init__.py`**:

```python
from .your_provider import YourProviderAdapter

__all__ = [
    # ... existing exports
    "YourProviderAdapter",
]
```

2. **Update `serving/servers/registry.py`**:

Add the import at the top:
```python
from serving.adapters import (
    # ... existing imports
    YourProviderAdapter,
)
```

Wire the new kind into `_make_adapter`. There are two patterns, depending on the protocol:

**A) OpenAI-compatible providers (preferred when possible).** Most new providers expose an OpenAI-style `/chat/completions` endpoint. In that case do NOT add a custom adapter class — instead, register a `provider_profile` and add the `kind` to the OpenAI-compat dispatch tuple. Example for a hypothetical "your_provider":

```python
elif kind == "your_provider":
    cfg = {**cfg, "provider_profile": "your_provider"}

# ...further down in the same function:
if kind in (
    "vllm",
    "sglang",
    "chutes",
    "featherless",
    "ollama",
    "openai_compat",
    "deepseek",
    "minimax",
    "your_provider",  # <-- add it here
):
    return OpenAICompatAdapter(model_cfg)
```

This is how `deepseek` and `minimax` are integrated today: a per-provider profile in `serving/adapters/profiles.py` carries any usage-metric or path quirks, and `OpenAICompatAdapter` does the rest. `zai` and `kimi_coding` follow the same profile pattern but are gated on a coding-tool identity, so they short-circuit to `CodingIdentityAdapter` (a thin `OpenAICompatAdapter` subclass) before reaching this dispatch tuple — see `serving/servers/registry.py:_make_adapter`.

**B) Genuinely custom protocols.** If the provider speaks a non-OpenAI wire format (e.g., Gemini's `generateContent`, the Anthropic Messages API, OpenRouter's provider-pinning header), add a dedicated adapter class and a dispatch branch:

```python
if kind == "your_provider":
    return YourProviderAdapter(model_cfg)
```

`gemini`, `claude`, `anthropic`, and `openrouter` all follow this pattern.

### Step 3: Add Model Configuration

Add your model to `config/models.yaml`:

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

### Step 4: Configure Environment Variables

Add to `.env`:

```bash
YOUR_PROVIDER_BASE_URL=https://api.yourprovider.com/v1
YOUR_PROVIDER_API_KEY=your-api-key-here
```

### Step 5: Test the Integration

```bash
# Start the server (Docker)
make build s=backend
# Or locally without Docker:
# uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080

# List available models
curl http://localhost:8080/v1/models

# Test chat completion
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model-id",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Streaming test:
```bash
curl -N -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model-id",
    "messages": [{"role": "user", "content": "Stream test"}],
    "stream": true,
    "max_tokens": 64
  }'
```

## Configuration Reference

### ModelConfig Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | Unique model identifier |
| `name` | string | Yes | Display name |
| `provider` | string | Yes | Provider/adapter kind |
| `base_url` | string | Yes | API endpoint base URL |
| `api_key` | string | No | API authentication key |
| `provider_model_id` | string | No | Provider's model identifier (overrides `id`) |
| `aliases` | list[string] | No | Alternative names for routing |
| `quantization` | string | No | Quantization format (default: "bf16") |
| `input_modalities` | list[string] | No | Input types: "text", "image" |
| `output_modalities` | list[string] | No | Output types: "text" |
| `context_length` | int | No | Maximum context window (default: 8192) |
| `max_output_length` | int | No | Maximum output tokens (default: 4096) |
| `supports_tools` | bool | No | Function calling support (default: false) |
| `on_demand` | bool | No | Model is lazily loaded on shared GPUs (started on first request, stopped when idle). Exposed in `/v1/models`; synthetic monitors skip chat-probing such models (default: false) |
| `supports_structured_output` | bool | No | JSON mode support (default: false) |
| `supported_params` | list[string] | No | Allowed parameter names |
| `pricing` | dict | No | Cost information per token/request |

### Route Configuration

Routes allow multiple endpoints for a single model with weighted distribution:

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
    base_url: https://api.provider.com
    api_key: ${API_KEY}
```

A route reports its `kind` as the provider label — the value recorded in
`api_logs.provider` and grouped on by every provider-scoped admin view. Two
routes of the same kind therefore share one dashboard row. Add `provider:` (and
optionally `provider_display_name:`) to a route to give it its own label and
name; see
[Naming a route in the dashboard](add-local-model.md#naming-a-route-in-the-dashboard).

### Supported Adapter Kinds

The `kind` field in each route entry selects the backend adapter. All kinds marked **OpenAI-compat** share the same `OpenAICompatAdapter` implementation with provider-specific profiles applied automatically.

| Kind | Category | Notes |
|------|----------|-------|
| `openai_compat` | OpenAI-compat | Generic OpenAI-compatible endpoint; use when no specific kind fits |
| `staging` | OpenAI-compat | Clone of `openai_compat` with its own provider label, so a second generic OpenAI-compatible endpoint can be tracked separately in metrics/analytics |
| `vllm` | OpenAI-compat | Local vLLM inference server |
| `sglang` | OpenAI-compat | Local SGLang inference server |
| `ollama` | OpenAI-compat | Local or remote Ollama server |
| `chutes` | OpenAI-compat | Chutes.ai hosted inference |
| `featherless` | OpenAI-compat | Featherless.ai hosted inference |
| `deepseek` | OpenAI-compat | DeepSeek API (applies DeepSeek usage profile) |
| `zai` | OpenAI-compat | Z.AI API (uses non-`/v1` chat path); dispatches to `CodingIdentityAdapter`, which presents a coding-tool `User-Agent` and leading OpenCode system message for the coding-plan endpoint |
| `kimi` | OpenAI-compat | Moonshot/Kimi pay-per-token API (applies Kimi usage profile) |
| `kimi_coding` | OpenAI-compat | Kimi Code coding-plan subscription endpoint; also dispatches to `CodingIdentityAdapter` |
| `minimax` | OpenAI-compat | MiniMax API (applies MiniMax usage profile) |
| `cliproxy` | OpenAI-compat | CLI proxy endpoint for OpenAI-compatible models |
| `openrouter` | Custom | OpenRouter aggregator. Use the bracket form `openrouter[<slug>]` (e.g. `openrouter[deepinfra]`) to pin a sub-provider. |
| `gemini` | Custom | Google Gemini API (message format translation) |
| `claude` | Custom | Anthropic Claude API (direct API key, not subscription) |
| `anthropic` | Custom | Generic Anthropic Messages API client |

### Hybrid Routing

- Weighted routes are applied at registration time. You can further adjust weights or override distribution centrally using `config/routing.yaml` (loaded by the `RoutingManager`).

## BaseAdapter API Reference

All adapters must inherit from `BaseAdapter` and implement:

### Required Methods

```python
async def chat_completion(
    self, messages: list[dict[str, Any]], **params
) -> dict[str, Any]:
    """Execute non-streaming chat completion."""
    pass

async def stream_chat_completion(
    self, messages: list[dict[str, Any]], **params
) -> AsyncGenerator[str, None]:
    """Execute streaming chat completion."""
    pass
```

### Utility Methods

```python
def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp parameters to supported ranges."""

def format_response(
    self,
    content: str,
    model: str,
    usage: UsageInfo | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """Format response in OpenAI-compatible format."""

def format_stream_chunk(
    self, content: str, model: str, finish_reason: str | None = None
) -> str:
    """Format SSE chunk for streaming responses."""
```

### Available Attributes

```python
self.config       # ModelConfig instance
self.http         # AsyncHTTPClient for API requests
```

## Advanced Features

### Multi-Modal Support

For models supporting images:

```yaml
input_modalities: ["text", "image"]
```

Implement image handling in your adapter's `chat_completion` method.

### Tool/Function Calling

For models supporting function calls:

```yaml
supports_tools: true
```

Parse and include `tool_calls` in the response:

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

### Structured Output (JSON Mode)

For models supporting JSON schema:

```yaml
supports_structured_output: true
```

Handle `response_format` parameter:

```python
if params.get("response_format", {}).get("type") == "json_object":
    payload["response_format"] = {"type": "json_object"}
```

### Rate Limiting (Optional)

`serving/servers/bootstrap.py` does not currently configure per-provider rate limiters; the only in-process limiter wired there is `UserConcurrencyLimiter`. Static limits live in `serving/config/settings.py` (see the `signup_rate_limit_*` and `login_rate_limit_*` fields). If you need a per-provider token-bucket or quota, add it to `serving/admin/provider_quotas.py` (or a new module) and surface it through `serving/servers/deps.py`.

## Examples

### Example 1: OpenAI-Compatible Provider

DeepSeek does not have its own adapter file. It is wired through `OpenAICompatAdapter` by setting `provider_profile = "deepseek"` in `serving/servers/registry.py:_make_adapter`; the profile itself lives in `serving/adapters/profiles.py`. Follow that pattern for any new OpenAI-compatible provider.

### Example 2: Custom API Format

See `serving/adapters/gemini.py` for handling non-standard API formats with message conversion.

### Example 3: Local Deployment

Local vLLM and SGLang servers reuse `serving/adapters/openai_compat.py` (`OpenAICompatAdapter`). The `vllm` and `sglang` kinds just dispatch to the same class — local-vs-remote routing is handled by `base_url` and the `RoutingManager`, not by a dedicated adapter file.

## Troubleshooting

### Model Not Appearing in `/v1/models`

- Check `config/models.yaml` syntax
- Verify environment variables are set
- Check server logs for configuration errors
- If using `aliases`, verify the canonical `id` appears exactly once and aliases do not collide with other model IDs.

### Authentication Failures

- Verify API key in `.env`
- Check if `${ENV_VAR}` expansion is working
- Ensure `base_url` is correct

### Response Format Errors

- Ensure `format_response()` returns OpenAI-compatible structure
- Validate `UsageInfo` fields are integers
- Check `finish_reason` is valid: "stop", "length", "content_filter"
 - For streaming, ensure the first non-empty content chunk is emitted as soon as available so TTFT metrics record properly.

### Streaming Issues

- Ensure chunks are SSE-formatted: `data: {json}\n\n`
- Send final usage chunk before `data: [DONE]`
- Handle JSON parsing errors gracefully

## Best Practices

1. **Error handling**: Use `self.http.json_post_with_retry()` and handle provider faults gracefully with useful messages.
2. **Usage accounting**: Prefer provider usage when available; otherwise fall back to `estimate_prompt_tokens()`/`estimate_text_tokens()`.
3. **Streaming helpers**: Use `format_stream_chunk()`, `make_final_usage_chunk()`, and `done_sentinel()` for consistent SSE.
4. **Type safety**: Provide full type hints and keep request/response shapes aligned with `serving/schemas.py`.
5. **Testing**: Exercise both streaming and non-streaming paths, and try large prompts to validate token clamping.
6. **Docs & style**: Keep adapter docstrings and comments in English (Google style). Avoid provider-specific logic in shared code.
7. **Env expansion**: Use `${ENV_VAR}` in YAML instead of hardcoding secrets or endpoints; let dotenv load `.env`.

## See Also

- [OpenRouter Gateway Overview](openrouter.md) — Architecture and endpoints
- [Routing Configuration](routing.md) — Central weight overrides and strategy
- [Configuration Guide](configuration.md) — Environment and YAML configuration
- [API Reference](https://doc.freeinference.org/) — the FreeInference deployment's user docs, as a worked example of what this generates
