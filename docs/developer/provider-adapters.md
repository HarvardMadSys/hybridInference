# Writing a Provider Adapter

This page is for contributors adding support for a provider the gateway
cannot talk to yet. To serve another model from a provider the gateway already
supports, you need no code at all; see [Adding a New Model](adding-models.md).

Most providers do not need a new adapter either. If the API is
OpenAI-compatible, a profile and two small edits to the dispatch are enough, as
Step 1 shows. A dedicated adapter is only for a provider with its own wire format.

## Step 1: decide whether you need an adapter at all

Most new providers expose an OpenAI-style `/chat/completions` endpoint. For
those, **do not write an adapter class.** Register a provider profile, then add
two things to `_make_adapter` in `apps/backend/serving/servers/registry.py`: an
arm that selects the profile, and the kind in the OpenAI-compatible tuple
further down the same function:

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
    "zai",
    "kimi",
    "minimax",
    "your_provider",  # <-- add it here
):
    return OpenAICompatAdapter(model_cfg)
```

This is how `deepseek`, `zai`, `kimi`, and `minimax` are integrated today: a
per-provider profile in `apps/backend/serving/adapters/profiles.py` carries the
usage-metric or path quirks, and `OpenAICompatAdapter` does the rest.

Write a dedicated adapter only when the provider speaks a genuinely non-OpenAI
wire format — Gemini's `generateContent`, the Anthropic Messages API,
OpenRouter's provider-pinning body fields. `gemini`, `claude`, `anthropic`, and
`openrouter` all follow that pattern, each with its own dispatch branch:

```python
if kind == "your_provider":
    return YourProviderAdapter(model_cfg)
```

## Step 2: write the adapter (custom protocols only)

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

## Step 3: add the model to your registry

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

## Step 4: configure environment variables

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

## Step 5: verify it

Start the gateway with a registry that uses the new kind, then check it as
described in [Verify it through the gateway](adding-models.md#verify-it-through-the-gateway).

## Deployment-local adapters

A deployment can register a local factory without editing the built-in dispatch.
Its module exposes a synchronous, no-argument `register()` function:

```python
from serving.adapters import ModelConfig, OpenAICompatAdapter
from serving.servers.registry import register_adapter_factory


def make_example_adapter(cfg):
    return OpenAICompatAdapter(ModelConfig(**cfg))


def register():
    register_adapter_factory("example_service", make_example_adapter)
```

Set `BACKEND_EXTENSIONS` to this module's import name and use
`kind: example_service` in the model registry. The factory receives the route's
configuration dictionary and returns an adapter. It runs before built-in
provider defaults, so it must supply any profile or path defaults it needs.
Duplicate registrations fail; replacing a built-in kind requires
`register_adapter_factory(kind, factory, override=True)` and is logged.
See [Backend Extensions](backend-extensions.md) for the startup and deployment
requirements.

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

### Limiting concurrent requests

An adapter does not need its own limiter. The gateway already caps how many
requests it keeps open against each provider key, and lowers the cap when the
provider answers 429; see [Outbound concurrency](routing.md#outbound-concurrency).
A new adapter takes part by wrapping its upstream call in a slot from
`apps/backend/serving/adapters/upstream_limiter.py`, as the built-in adapters
do.

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

- [Adding a New Model](adding-models.md) — every field a model entry accepts
- [Architecture](architecture.md#adapters) — where adapters sit in a request
- [Backend Extensions](backend-extensions.md) — registering an adapter from
  outside the source tree
