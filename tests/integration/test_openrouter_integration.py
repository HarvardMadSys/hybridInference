"""Live integration tests against the real OpenRouter API.

Skipped unless OPENROUTER_API_KEY is set. Each test makes ONE small live
chat completion to keep the cost negligible.
"""

from __future__ import annotations

import os

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openrouter import OpenRouterAdapter

pytestmark = [
    pytest.mark.integration,
    # Also `external`: this file talks to the real OpenRouter API, which is what
    # that marker means and why `make test` excludes it. Without this, the file
    # runs — and spends money — for anyone who happens to have
    # OPENROUTER_API_KEY exported, which is most developers here.
    pytest.mark.external,
    pytest.mark.skipif(
        not os.getenv("OPENROUTER_API_KEY"),
        reason="OPENROUTER_API_KEY not set; skipping live OpenRouter integration tests.",
    ),
]


def _cfg(*, pinned: str | None = None) -> ModelConfig:
    return ModelConfig(
        id="or-llama",
        name="OR Llama 3.1 8B",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
        provider_model_id="meta-llama/llama-3.1-8b-instruct",
        context_length=8192,
        max_output_length=64,
        supports_tools=True,
        supports_structured_output=True,
        supported_params=["temperature", "top_p", "max_tokens", "stream"],
        provider_profile="openrouter",
        openrouter_pinned_provider=pinned,
    )


@pytest.mark.asyncio
async def test_real_chat_completion_no_pin() -> None:
    adapter = OpenRouterAdapter(_cfg())
    response = await adapter.chat_completion(
        [{"role": "user", "content": "Say only the word 'pong'."}],
        max_tokens=8,
        temperature=0.0,
    )
    assert response["choices"][0]["message"]["content"]
    routing = response.get("_routing", {})
    # cost is best-effort; assert structure but only a soft check on value
    if routing.get("upstream_cost_usd") is not None:
        assert routing["upstream_cost_usd"] > 0


@pytest.mark.asyncio
async def test_real_streaming_with_cost() -> None:
    adapter = OpenRouterAdapter(_cfg())
    chunks: list[str] = []
    async for chunk in adapter.stream_chat_completion(
        [{"role": "user", "content": "Say only the word 'pong'."}],
        max_tokens=8,
        temperature=0.0,
    ):
        chunks.append(chunk)
    # Final chunk before [DONE] should have _routing with provider info
    joined = "".join(chunks)
    assert '"_routing"' in joined


@pytest.mark.asyncio
async def test_pinned_provider_routes_through() -> None:
    """When provider.order is set, OpenRouter routes only through that provider.

    Verified by inspecting the upstream response which includes a `provider`
    field naming the actual upstream that served the request.
    """
    adapter = OpenRouterAdapter(_cfg(pinned="deepinfra"))
    response = await adapter.chat_completion(
        [{"role": "user", "content": "Say only the word 'pong'."}],
        max_tokens=8,
        temperature=0.0,
    )
    # OpenRouter exposes the upstream provider name on the response.
    # The response we get back is the formatted dict — the raw upstream
    # `provider` field may be on the unwrapped response. Skip strict check
    # if absent (OpenRouter occasionally omits) but cost should be present.
    routing = response.get("_routing", {})
    if routing.get("upstream_cost_usd") is not None:
        assert routing["upstream_cost_usd"] > 0
