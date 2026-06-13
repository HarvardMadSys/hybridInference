"""Tests for the probe logic using a mocked HTTP transport."""

from __future__ import annotations

import httpx
import pytest

from status_monitor.config import GatewayConfig, Settings
from status_monitor.prober import probe_model

pytestmark = pytest.mark.asyncio


def _gateway() -> GatewayConfig:
    return GatewayConfig(base_url="http://gw:8080", api_key="k", probe_header="synthetic")


async def test_nonstreaming_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The gateway only recognizes the "X-Probe" header to mark synthetic traffic.
        assert request.headers["X-Probe"] == "synthetic"
        assert "X-FreeInference-Probe" not in request.headers
        body = request.read().decode()
        assert '"stream": false' in body or '"stream":false' in body
        return httpx.Response(200, json={"usage": {"completion_tokens": 12}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=False,
        )

    assert result.ok is True
    assert result.completion_tokens == 12
    assert result.latency_ms is not None


async def test_streaming_measures_ttft() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        'data: {"usage":{"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=True,
        )

    assert result.ok is True
    assert result.completion_tokens == 2
    assert result.ttft_ms is not None
    # Decode-window throughput is computed when tokens > 1 and TTFT is known.
    assert result.throughput_tps is not None


async def test_streaming_handles_data_without_space() -> None:
    # Some gateways/proxies emit "data:{...}" with no space after the colon.
    sse = 'data:{"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=True,
        )

    assert result.ok is True
    assert result.completion_tokens == 1


async def test_embedding_probe_uses_embeddings_endpoint() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="bge-m3",
            kind="embedding",
        )

    assert seen["path"] == "/v1/embeddings"
    assert result.ok is True


async def test_streaming_reasoning_only_records_ttft() -> None:
    # Reasoning models may emit only reasoning_content before usage.
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
        'data: {"usage":{"completion_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=True,
        )

    assert result.ok is True
    assert result.ttft_ms is not None  # reasoning counted as first token


async def test_streaming_truncated_empty_fails_probe() -> None:
    # HTTP 200 but the stream closes with no [DONE] and no meaningful event.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=": keep-alive\n\n")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=True,
        )

    assert result.ok is False
    assert "incomplete stream" in result.error


async def test_streaming_content_without_terminal_fails_probe() -> None:
    # Content arrives but the stream is cut before finish_reason/usage/[DONE].
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=True,
        )

    assert result.ok is False
    assert "incomplete stream" in result.error


async def test_streaming_in_band_error_fails_probe() -> None:
    # Gateway returns HTTP 200 but emits an in-band error chunk on upstream failure.
    sse = (
        'data: {"error":{"message":"upstream exploded","type":"server_error"}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=True,
        )

    assert result.ok is False
    assert "upstream exploded" in result.error


async def test_http_error_is_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await probe_model(
            client,
            gateway=_gateway(),
            settings=Settings(),
            model_id="glm-4.7",
            streaming=False,
        )

    assert result.ok is False
    assert result.error == "HTTP 503"
