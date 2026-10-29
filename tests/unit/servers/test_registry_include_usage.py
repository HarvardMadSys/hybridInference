"""Registry tests for the route-level ``include_usage_in_stream`` capability.

The flag says "this upstream answers ``stream_options.include_usage`` with a
final usage chunk", which is what makes cached-token counts and completion
tokens on tool-call-only responses measured rather than estimated. It is a
protocol fact about one endpoint, so it resolves per route: the same model can
carry a deepseek route that reports usage beside an OpenAI-compatible fallback
that rejects the unknown body field.

These tests cover the three layers that have to agree: the YAML loader, the
per-kind defaults in ``_make_adapter``, and the outbound request body the
adapter actually sends.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from routing.routers import FixedRouter
from serving.servers.registry import register_from_models_yaml

if TYPE_CHECKING:
    from pathlib import Path

MODEL_ID = "usage-test"


def _load(tmp_path: Path, body: str) -> list:
    """Load one model from YAML and return its adapters in route order."""
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    router = FixedRouter()
    register_from_models_yaml(router, path)
    return [adapter for adapter, _weight in router.routes[MODEL_ID].adapters]


def _chunk(*, delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> str:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}"


async def _stream_and_capture(adapter, *, usage: dict | None = None) -> tuple[dict, list[str]]:
    """Run one streamed request; return (outbound JSON body, emitted SSE chunks).

    ``usage`` is attached to a final empty-choices delta, which is the shape an
    upstream uses for the ``include_usage`` chunk.
    """

    async def fake_stream_post(*args, **kwargs):
        yield _chunk(delta={"content": "hi"})
        if usage is not None:
            yield _chunk(delta={}, usage=usage)
        yield _chunk(delta={}, finish_reason="stop")
        yield "data: [DONE]"

    # Replace the adapter's client wholesale. ``adapter.http`` is the
    # process-wide ``AsyncHTTPClient.shared()`` singleton, so patching a method
    # on it in place would leak the mock into every later test that streams --
    # exactly what a full-suite run of ``tests/unit/serving/test_http.py``
    # caught. Shadowing the instance attribute leaves the singleton pristine.
    http = MagicMock()
    http.stream_post = MagicMock(side_effect=fake_stream_post)
    adapter.http = http
    chunks = [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]
    return adapter.http.stream_post.call_args.kwargs["json"], chunks


def _final_usage(chunks: list[str]) -> dict:
    payloads = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]"
    ]
    usage_chunks = [payload for payload in payloads if payload.get("usage")]
    assert usage_chunks, "expected a final chunk carrying usage"
    return usage_chunks[-1]["usage"]


# ---------------------------------------------------------------------------
# Loader: per-route resolution and kind defaults
# ---------------------------------------------------------------------------

_DEEPSEEK_PLUS_OLLAMA = """
models:
  - id: usage-test
    name: usage-test
    provider: deepseek
    base_url: https://api.deepseek.com
    route:
      - kind: deepseek
        weight: 1.0
        base_url: https://api.deepseek.com
      - kind: ollama
        weight: 1.0
        base_url: http://ollama.local:11434/v1
"""


def test_kind_defaults_apply_per_route(tmp_path) -> None:
    """Omitted values keep today's defaults: deepseek on, ollama off."""
    deepseek, ollama = _load(tmp_path, _DEEPSEEK_PLUS_OLLAMA)

    assert deepseek.config.include_usage_in_stream is True
    assert ollama.config.include_usage_in_stream is False


def test_explicit_values_override_kind_defaults_per_route(tmp_path) -> None:
    body = _DEEPSEEK_PLUS_OLLAMA.replace(
        "      - kind: deepseek\n        weight: 1.0\n",
        "      - kind: deepseek\n        weight: 1.0\n        include_usage_in_stream: false\n",
    ).replace(
        "      - kind: ollama\n        weight: 1.0\n",
        "      - kind: ollama\n        weight: 1.0\n        include_usage_in_stream: true\n",
    )

    deepseek, ollama = _load(tmp_path, body)

    assert deepseek.config.include_usage_in_stream is False
    assert ollama.config.include_usage_in_stream is True


def test_same_kind_routes_stay_independently_configurable(tmp_path) -> None:
    """Two vLLM nodes may disagree about accepting the field; each says so."""
    body = """
    models:
      - id: usage-test
        name: usage-test
        provider: vllm
        base_url: http://gpu-a:8000/v1
        route:
          - kind: vllm
            weight: 1.0
            base_url: http://gpu-a:8000/v1
            include_usage_in_stream: true
          - kind: vllm
            weight: 1.0
            base_url: http://gpu-b:8000/v1
            include_usage_in_stream: false
          - kind: vllm
            weight: 1.0
            base_url: http://gpu-c:8000/v1
    """

    first, second, third = _load(tmp_path, body)

    assert first.config.include_usage_in_stream is True
    assert second.config.include_usage_in_stream is False
    assert third.config.include_usage_in_stream is True


def test_model_level_declaration_is_refused(tmp_path) -> None:
    """A model-wide claim would ride to every fallback, so it is an error."""
    body = """
    models:
      - id: usage-test
        name: usage-test
        provider: deepseek
        base_url: https://api.deepseek.com
        include_usage_in_stream: true
        route:
          - kind: deepseek
            weight: 1.0
            base_url: https://api.deepseek.com
    """

    with pytest.raises(ValueError, match="must be declared on a route"):
        _load(tmp_path, body)


def test_shorthand_model_cannot_declare_it(tmp_path) -> None:
    """A synthesized route has no `route:` block for a reviewer to inspect."""
    body = """
    models:
      - id: usage-test
        name: usage-test
        provider: deepseek
        base_url: https://api.deepseek.com
        include_usage_in_stream: true
    """

    with pytest.raises(ValueError, match="must be declared on a route"):
        _load(tmp_path, body)


@pytest.mark.parametrize("scalar", ['"false"', '"true"', '"no"', '"on"', "1", "0", "null"])
def test_non_boolean_scalar_is_refused(tmp_path, scalar: str) -> None:
    """`bool("false")` is True: a quoted opt-out must not become an opt-in."""
    body = f"""
    models:
      - id: usage-test
        name: usage-test
        provider: deepseek
        base_url: https://api.deepseek.com
        route:
          - kind: deepseek
            weight: 1.0
            base_url: https://api.deepseek.com
            include_usage_in_stream: {scalar}
    """

    with pytest.raises(ValueError, match="must be a YAML boolean"):
        _load(tmp_path, body)


# ---------------------------------------------------------------------------
# Outbound request body: what actually reaches the upstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_kind_sends_stream_options(tmp_path) -> None:
    deepseek = _load(tmp_path, _DEEPSEEK_PLUS_OLLAMA)[0]

    payload, _chunks = await _stream_and_capture(deepseek)

    assert payload["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_unsupported_upstream_receives_no_stream_options(tmp_path) -> None:
    """An omitted flag on a non-default kind must stay absent on the wire."""
    _deepseek, ollama = _load(tmp_path, _DEEPSEEK_PLUS_OLLAMA)

    payload, _chunks = await _stream_and_capture(ollama)

    assert "stream_options" not in payload


@pytest.mark.asyncio
async def test_explicit_false_removes_stream_options_from_a_default_kind(tmp_path) -> None:
    body = _DEEPSEEK_PLUS_OLLAMA.replace(
        "      - kind: deepseek\n        weight: 1.0\n",
        "      - kind: deepseek\n        weight: 1.0\n        include_usage_in_stream: false\n",
    )
    deepseek, _ollama = _load(tmp_path, body)

    payload, _chunks = await _stream_and_capture(deepseek)

    assert "stream_options" not in payload


@pytest.mark.asyncio
async def test_explicit_true_opts_a_non_default_kind_in(tmp_path) -> None:
    body = _DEEPSEEK_PLUS_OLLAMA.replace(
        "      - kind: ollama\n        weight: 1.0\n",
        "      - kind: ollama\n        weight: 1.0\n        include_usage_in_stream: true\n",
    )
    _deepseek, ollama = _load(tmp_path, body)

    payload, _chunks = await _stream_and_capture(ollama)

    assert payload["stream_options"] == {"include_usage": True}


# ---------------------------------------------------------------------------
# Cache-read measurements: preserved when reported, never fabricated when not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reported_cache_reads_survive_the_stream(tmp_path) -> None:
    deepseek = _load(tmp_path, _DEEPSEEK_PLUS_OLLAMA)[0]

    _payload, chunks = await _stream_and_capture(
        deepseek,
        usage={
            "prompt_tokens": 320,
            "completion_tokens": 7,
            "total_tokens": 327,
            "prompt_cache_hit_tokens": 256,
            "prompt_cache_miss_tokens": 64,
        },
    )

    usage = _final_usage(chunks)
    assert usage["prompt_tokens"] == 320
    assert usage["completion_tokens"] == 7
    assert usage["cache_read_tokens"] == 256
    assert usage["prompt_tokens_details"]["cached_tokens"] == 256


@pytest.mark.asyncio
async def test_absent_cache_reads_are_not_reported_as_zero(tmp_path) -> None:
    """Silence stays silence: no measured 0 when the upstream reports nothing."""
    deepseek = _load(tmp_path, _DEEPSEEK_PLUS_OLLAMA)[0]

    _payload, chunks = await _stream_and_capture(
        deepseek,
        usage={
            "prompt_tokens": 320,
            "completion_tokens": 7,
            "total_tokens": 327,
        },
    )

    usage = _final_usage(chunks)
    assert usage["prompt_tokens"] == 320
    assert "cache_read_tokens" not in usage
    assert "prompt_tokens_details" not in usage
