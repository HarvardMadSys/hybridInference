# Writing a Provider Adapter

This page is for adding a provider the gateway cannot talk to yet. Serving
another model from a supported provider needs no code; see
[Adding a New Model](adding-models.md).

## Step 1: decide whether you need an adapter at all

If the provider exposes an OpenAI-style `/chat/completions` endpoint, **do not
write an adapter class.** Add a provider profile in
`apps/backend/serving/adapters/profiles.py` for its usage or path quirks, then
change `_build_adapter` in `apps/backend/serving/servers/registry.py` (called by
`_make_adapter`) in two places:

```python
# ...among the per-kind arms of _build_adapter:
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

Also add the kind to `RESERVED_PROVIDER_LABELS` in the same module. That set is
the list of kinds the catalog accepts, and it stops routes from borrowing the
kind as a label; a unit test fails if a dispatchable kind is missing. If the
provider answers `stream_options.include_usage` with a final usage chunk, add
the kind to `_STREAM_USAGE_DEFAULT_KINDS` too. `deepseek`, `zai`, `kimi` and
`minimax` are built this way.

Write a dedicated adapter only for a different wire format, such as Gemini's
`generateContent`, the Anthropic Messages API or OpenRouter's provider-pinning
fields. `gemini`, `claude`, `anthropic` and `openrouter` each have their own
branch:

```python
if kind == "your_provider":
    return YourProviderAdapter(model_cfg)
```

## Step 2: write the adapter (custom protocols only)

Create `apps/backend/serving/adapters/your_provider.py`. The package root is
`apps/backend`, so imports start with `serving.` or `routing.`:

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

Add an entry like this to the `models` list of the catalog (**Admin →
Routing → Models**); every field is in the
[Configuration Reference](adding-models.md#configuration-reference):

```json
{
  "id": "your-model-id",
  "name": "Your Model Name",
  "provider": "your_provider",
  "provider_model_id": "actual-model-id",
  "base_url": "${YOUR_PROVIDER_BASE_URL}",
  "api_key": "${YOUR_PROVIDER_API_KEY}",
  "context_length": 8192,
  "max_output_length": 4096,
  "supports_tools": true,
  "supports_structured_output": true,
  "supported_params": ["temperature", "top_p", "max_tokens", "stop"],
  "route": [
    {
      "kind": "your_provider",
      "weight": 1.0
    }
  ]
}
```

## Step 4: set the variables

Add both variables in **Admin → Settings → Providers** with **Add
variable**, mark the key **Secret**, and restart the backend. A gateway without
a database reads them from `.env`; a gateway with one also copies them from
`.env` into the database at startup unless it already stores that name:

```bash
YOUR_PROVIDER_BASE_URL=https://api.yourprovider.example/v1
YOUR_PROVIDER_API_KEY=your-api-key-here
```

If a `${VAR}` in `api_key`, `api_keys` or `base_url` is empty, the whole model
is skipped and the log names the model and variable. Mark a route
`optional: true` to skip only that route.

## Step 5: verify it

Restart the backend with your code, then check the model as in
[Verify it through the gateway](adding-models.md#verify-it-through-the-gateway).

## Deployment-local adapters

A deployment can register its own adapter factory without editing the built-in
dispatch. Its module exposes a synchronous `register()` that takes no
arguments:

```python
from serving.adapters import ModelConfig, OpenAICompatAdapter
from serving.servers.registry import register_adapter_factory


def make_example_adapter(cfg):
    return OpenAICompatAdapter(ModelConfig(**cfg))


def register():
    register_adapter_factory("example_service", make_example_adapter)
```

Set `BACKEND_EXTENSIONS` to the module's import name and use
`"kind": "example_service"` in the catalog. The factory receives the route's
configuration dictionary before any built-in defaults, so it must supply its
own profile and path defaults. Registering a kind twice fails; replacing a
built-in kind needs `register_adapter_factory(kind, factory, override=True)`
and is logged. See [Backend Extensions](backend-extensions.md).

## BaseAdapter API reference

Every adapter subclasses `BaseAdapter` (`apps/backend/serving/adapters/base.py`)
and implements:

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

The base class's `messages()` and `stream_messages()` serve the Anthropic
Messages API by translating to and from these two methods; an Anthropic-native
adapter overrides them. Helpers:

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

Attributes:

```python
self.config       # ModelConfig instance
self.http         # AsyncHTTPClient (apps/backend/serving/http.py), shared
```

## Capabilities

- **Images.** A model with `"input_modalities": ["text", "image"]` sends image
  content blocks; handle them in `chat_completion`. A route can declare
  narrower modalities, so a text-only fallback never receives media.
- **Tools.** With `"supports_tools": true`, convert the provider's tool calls to
  the OpenAI shape and pass them to `format_response(tool_calls=...)`.
- **Structured output.** With `"supports_structured_output": true`, forward
  `response_format`. The gateway also checks replies itself: when an endpoint
  cannot take the format, the OpenAI-compatible adapter puts it in the prompt;
  `kind: vllm` routes get `structured_outputs` with `disable_any_whitespace`;
  and a non-streaming reply that is not valid JSON for the schema is retried up
  to twice with the schema in the prompt (not when `max_tokens` cut it off).
  If no attempt conforms, the last reply is returned as is, and
  `metadata.structured_output` in the request log records the attempts.
- **Concurrency.** Do not add a limiter. The gateway caps open requests per
  provider key and lowers the cap on 429 (see
  [Outbound concurrency](routing.md#outbound-concurrency)); wrap your upstream
  call in a slot from `apps/backend/serving/adapters/upstream_limiter.py`, as
  the built-in adapters do.

## Checklist

- Return the OpenAI response shape from `format_response()`, with integer
  `UsageInfo` fields and a `finish_reason` of `stop`, `length`,
  `content_filter` or `tool_calls`.
- Prefer provider-reported usage; fall back to `estimate_prompt_tokens()` and
  `estimate_text_tokens()`.
- Stream SSE lines (`data: {json}\n\n`) with `format_stream_chunk()`. Send the
  first content chunk as soon as you have it (time to first token is measured
  from it), then the final usage chunk (`make_final_usage_chunk()`), then
  `done_sentinel()`. Skip lines that are not valid JSON.
- Use `self.http.json_post_with_retry()` and give provider errors useful
  messages.
- Keep request and response shapes aligned with
  `apps/backend/serving/schemas.py`, and keep provider-specific logic out of
  shared code.
- Test streaming and non-streaming paths, including large prompts.
- Put keys and endpoints in settings and reference them as `${NAME}`; the
  catalog refuses inline credentials.

For examples, read `apps/backend/serving/adapters/gemini.py` (a non-OpenAI
format) and the `deepseek` profile in `apps/backend/serving/adapters/profiles.py`
(an OpenAI-compatible provider with no adapter file). Where adapters sit in a
request is in [Architecture](architecture.md#adapters).
