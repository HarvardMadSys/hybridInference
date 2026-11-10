"""An embeddings response is bounded by its request and decoded without stalling the loop.

One free request of 12,000 one-character inputs made an embedding model answer
about 255 MiB of JSON floats, which the gateway read whole and decoded on the
event loop in one piece (3.3 s), before validating and re-encoding it. The
request now carries at most OpenAI's 2,048 inputs, the response read is bounded
by the inputs it answers, and a large response is decoded a part at a time.

``json.loads`` holds the GIL throughout, so a thread would not have freed the
loop: the decode yields to it between the response's elements instead.
"""

from __future__ import annotations

import asyncio
import json
import random
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from pydantic import ValidationError

import serving.http as http_mod
from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import (
    EMBEDDING_RESPONSE_BYTES_PER_INPUT,
    OpenAICompatAdapter,
    embeddings_response_limit,
)
from serving.http import (
    MAX_RESPONSE_BODY_BYTES,
    AsyncHTTPClient,
    UpstreamPayloadTooLarge,
    loads_yielding,
)
from serving.schemas import MAX_EMBEDDING_INPUTS, EmbeddingRequest

pytestmark = pytest.mark.unit


def _embeddings_body(inputs: int, dimensions: int = 1024) -> dict:
    rng = random.Random(inputs)
    vector = [rng.uniform(-0.1, 0.1) for _ in range(dimensions)]
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": vector} for i in range(inputs)],
        "model": "bge-m3",
        "usage": {"prompt_tokens": inputs, "total_tokens": inputs},
    }


# ---------------------------------------------------------------------------
# The request: at most OpenAI's 2,048 inputs
# ---------------------------------------------------------------------------


def test_an_embeddings_request_carries_at_most_openais_2048_inputs():
    assert MAX_EMBEDDING_INPUTS == 2048
    assert len(EmbeddingRequest(model="m", input=["a"] * 2048).input) == 2048
    assert EmbeddingRequest(model="m", input="one long text " * 1000).input

    with pytest.raises(ValidationError, match="at most 2048"):
        EmbeddingRequest(model="m", input=["a"] * 12_000)


@pytest.mark.asyncio
async def test_a_batched_embeddings_request_over_the_cap_is_refused_at_admission():
    from serving.batch.targets import ItemRejected, _admit_embeddings

    with pytest.raises(ItemRejected) as refused:
        await _admit_embeddings({"model": "m", "input": ["a"] * 2049}, ctx=None)  # type: ignore[arg-type]

    assert refused.value.code == "invalid_request"


# ---------------------------------------------------------------------------
# The response read: bounded by the inputs it answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_data", "inputs"), [("one", 1), (["a", "b", "c"], 3), (["a"] * 2048, 2048)]
)
def test_the_response_limit_follows_the_inputs(input_data, inputs):
    expected = min(MAX_RESPONSE_BODY_BYTES, 64 * 1024 + inputs * EMBEDDING_RESPONSE_BYTES_PER_INPUT)

    assert embeddings_response_limit(input_data) == expected


def test_a_vector_of_8192_dimensions_fits_the_per_input_allowance():
    body = json.dumps(_embeddings_body(1, dimensions=8192)).encode()

    assert len(body) < embeddings_response_limit(["one"])


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [True, False], ids=["key-pool", "single-key"])
async def test_the_adapter_reads_no_more_than_its_inputs_allow(pooled):
    config = ModelConfig(
        id="bge-m3",
        name="BGE-M3",
        provider="sglang",
        base_url="https://embeddings.example/v1",
        model_type="embedding",
        **({"api_keys": ["k1"]} if pooled else {"api_key": "k1"}),
    )
    adapter = OpenAICompatAdapter(config)
    adapter.http = MagicMock()
    adapter.http.json_post = AsyncMock(return_value={"data": []})
    adapter.http.json_post_with_retry = AsyncMock(return_value={"data": []})

    await adapter.embeddings(["a", "b"])

    post = adapter.http.json_post if pooled else adapter.http.json_post_with_retry
    assert post.await_args.kwargs["max_body_bytes"] == embeddings_response_limit(["a", "b"])


@pytest.mark.asyncio
async def test_a_chat_completion_keeps_the_default_limit():
    """Only an embeddings request has a size to bound its answer by."""
    adapter = OpenAICompatAdapter(
        ModelConfig(id="m", name="m", provider="p", base_url="https://x.example/v1", api_keys=["k"])
    )
    adapter.http = MagicMock()
    adapter.http.json_post = AsyncMock(
        return_value={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    )

    await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert "max_body_bytes" not in adapter.http.json_post.await_args.kwargs


class _Upstream:
    def __init__(self) -> None:
        self.body = b"{}"

    async def handle(self, request: web.Request) -> web.Response:
        return web.Response(body=self.body, content_type="application/json")


@pytest.fixture
async def upstream(monkeypatch):
    client = AsyncHTTPClient()  # one session per test, on this test's loop
    monkeypatch.setattr(AsyncHTTPClient, "_shared", client)
    server = _Upstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    server.url = f"http://127.0.0.1:{port}/v1/embeddings"
    try:
        yield server
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_response_larger_than_its_inputs_allow_is_refused(upstream):
    upstream.body = json.dumps(_embeddings_body(20)).encode()  # ~420 KB for 20 vectors
    client = AsyncHTTPClient.shared()

    with pytest.raises(UpstreamPayloadTooLarge):
        await client.json_post(
            upstream.url, json={}, max_body_bytes=embeddings_response_limit(["one"])
        )

    out = await client.json_post(
        upstream.url, json={}, max_body_bytes=embeddings_response_limit(["a"] * 20)
    )
    assert len(out["data"]) == 20


# ---------------------------------------------------------------------------
# The decode: a part at a time, the event loop running between the parts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_large_response_is_decoded_while_the_event_loop_keeps_running():
    text = json.dumps(_embeddings_body(600))  # about 13 MB
    ticks = 0
    done = False

    async def ticker() -> None:
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    before = ticks
    value = await loads_yielding(text)
    during = ticks - before
    done = True
    await task

    assert value == json.loads(text)
    # One turn per 256 KiB or so; json.loads would allow none.
    assert during >= 20


@pytest.mark.asyncio
async def test_a_large_body_takes_the_yielding_decode(upstream, monkeypatch):
    body = _embeddings_body(100)
    upstream.body = json.dumps(body).encode()
    calls: list[int] = []

    async def spy(text: str):
        calls.append(len(text))
        return await loads_yielding(text)

    monkeypatch.setattr(http_mod, "loads_yielding", spy)
    monkeypatch.setattr(http_mod, "YIELDING_DECODE_MIN_CHARS", 1024)

    assert await AsyncHTTPClient.shared().json_post(upstream.url, json={}) == body
    assert calls == [len(upstream.body)]


_FRAGMENTS = [
    *('{"a": 1}', "[1, 2.5, -3e2]", '"s\\u00e9\\"q"', "true", "false", "null", "NaN"),
    *("{", "}", "[", "]", ",", ":", '"k"', " ", "\n", "\t", "\r", "1", "-", "e", "."),
]


def _outcome(fn, text):
    try:
        return ("ok", json.dumps(fn(text), allow_nan=True))
    except json.JSONDecodeError:
        return ("error",)


@pytest.mark.asyncio
async def test_the_yielding_decode_returns_what_json_loads_returns(monkeypatch):
    """Valid documents decode to the same value; malformed ones raise as before."""
    monkeypatch.setattr(http_mod, "_YIELDING_DECODE_SLICE_CHARS", 1)  # yield after every part
    rng = random.Random(11)
    documents = [json.dumps(_embeddings_body(3, dimensions=4)), " [ ] ", "{}", "[{}, []]"]
    for _ in range(3000):
        documents.append("".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 30))))

    for text in documents:
        try:
            got = ("ok", json.dumps(await loads_yielding(text), allow_nan=True))
        except json.JSONDecodeError:
            got = ("error",)
        assert got == _outcome(json.loads, text), text
