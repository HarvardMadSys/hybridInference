"""End-to-end SSE framing between the HTTP reader and the adapter.

Every other ``_INCOMPLETE_STREAM_ERROR`` test stubs ``adapter.http.stream_post``
and hands the adapter pre-framed strings, so none of them can see a framing bug
in ``serving.http``. These drive the real reader over a fake socket instead --
the only shape that reproduces the production failure, where a wire-level frame
loss (not a truncated generation) surfaced to the circuit breaker as
``stream_exception``.
"""

from __future__ import annotations

import json

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.key_pool import KeyPool
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.http import AsyncHTTPClient


def _adapter() -> OpenAICompatAdapter:
    """An adapter wired to the real HTTP client (no ``stream_post`` stub)."""
    return OpenAICompatAdapter(
        ModelConfig(
            id="glm-5.2",
            name="GLM-5.2",
            provider="staging",
            base_url="http://mock.local/v1",
            provider_model_id="glm-5.2",
            processor="default",
            supported_params=["temperature", "max_tokens"],
        )
    )


class _Resp:
    """Fake aiohttp response streaming a scripted, byte-exact SSE body."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.content = self
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def iter_chunked(self, _n: int):
        for chunk in self._chunks:
            yield chunk


def _serve(monkeypatch, chunks: list[bytes]) -> None:
    async def fake_ensure(self):
        class _Session:
            def post(self, *_a, **_k):
                return _Resp(chunks)

        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure)


def _frame(delta: dict, finish_reason: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _stream(adapter) -> list[str]:
    return [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_ending_on_done_without_blank_line_is_not_an_incomplete_stream(monkeypatch):
    """The production failure: a finished generation reported as truncated.

    ``[DONE]`` is this provider's only terminal signal -- no frame carries a
    ``finish_reason`` -- and the body ends after ``data: [DONE]\\n`` instead of
    the blank line SSE asks for. Losing that one frame left the adapter with
    neither terminal marker, so it raised ``_INCOMPLETE_STREAM_ERROR`` over a
    complete answer, and the router charged the endpoint a ``stream_exception``.
    """
    _serve(
        monkeypatch,
        [
            _frame({"role": "assistant"}),
            _frame({"content": "hi"}),
            b"data: [DONE]\n",  # no terminating blank line
        ],
    )

    chunks = await _stream(_adapter())

    assert chunks[-1].strip() == "data: [DONE]"
    payloads = [json.loads(c[6:]) for c in chunks if not c.startswith("data: [DONE]")]
    content = "".join(
        p["choices"][0]["delta"].get("content", "") for p in payloads if p.get("choices")
    )
    assert content == "hi"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_ending_on_finish_reason_without_done_is_not_incomplete(monkeypatch):
    """Same, for a provider whose last frame is the ``finish_reason`` chunk."""
    _serve(
        monkeypatch,
        [
            _frame({"content": "hi"}),
            _frame({}, finish_reason="stop").rstrip(b"\n"),
        ],
    )

    chunks = await _stream(_adapter())

    assert chunks[-1].strip() == "data: [DONE]"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_genuinely_truncated_body_still_raises(monkeypatch):
    """The check still catches what it exists to catch.

    A half-received final frame is not rescued: it does not parse, so neither
    terminal flag is set and the adapter still reports the stream as incomplete
    rather than capping a partial answer with a fabricated ``[DONE]``.
    """
    _serve(
        monkeypatch,
        [
            _frame({"content": "hi"}),
            b'data: {"choices":[{"delta":{"content":"par',  # cut mid-JSON
        ],
    )

    chunks: list[str] = []
    with pytest.raises(aiohttp.ClientError, match=r"ended without.*finish_reason.*\[DONE\]"):
        async for chunk in _adapter().stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert all(chunk.strip() != "data: [DONE]" for chunk in chunks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_with_no_terminal_signal_at_all_still_raises(monkeypatch):
    """A body of well-formed frames that simply stops is still incomplete."""
    _serve(monkeypatch, [_frame({"role": "assistant"}), _frame({"content": "partial"})])

    with pytest.raises(aiohttp.ClientError, match=r"ended without.*finish_reason.*\[DONE\]"):
        async for _ in _adapter().stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [b"", b"data: [DONE]\n\n"])
@pytest.mark.parametrize("code", [429, "429"])
async def test_in_band_error_preserves_upstream_status_and_message(monkeypatch, tail, code):
    """A gateway's HTTP 200 SSE body can carry the real upstream 429."""
    error = {"message": "Weekly/Monthly Limit Exhausted", "type": "server_error", "code": code}
    _serve(monkeypatch, [f"data: {json.dumps({'error': error})}\n\n".encode(), tail])

    with pytest.raises(aiohttp.ClientResponseError, match="Weekly/Monthly Limit Exhausted") as exc:
        await _stream(_adapter())

    assert exc.value.status == 429


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_band_error_after_content_does_not_finish_successfully(monkeypatch):
    _serve(
        monkeypatch,
        [
            _frame({"content": "partial"}),
            b'data: {"error":{"message":"provider failed","code":503}}\n\n',
            b"data: [DONE]\n\n",
        ],
    )
    chunks = []
    with pytest.raises(aiohttp.ClientResponseError, match="provider failed") as exc:
        async for chunk in _adapter().stream_chat_completion([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert exc.value.status == 503
    assert chunks
    assert all(chunk.strip() != "data: [DONE]" for chunk in chunks)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, "insufficient_quota", 200, 1302, True, 429.5])
async def test_in_band_non_http_error_code_defaults_to_bad_gateway(monkeypatch, code):
    error = {"message": "upstream unavailable", "code": code}
    _serve(monkeypatch, [f"data: {json.dumps({'error': error})}\n\n".encode()])

    with pytest.raises(aiohttp.ClientResponseError, match="upstream unavailable") as exc:
        await _stream(_adapter())

    assert exc.value.status == 502


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 503])
async def test_in_band_error_releases_key_with_real_status_without_retry(monkeypatch, status):
    error = {"message": "upstream refused request", "code": status}
    _serve(monkeypatch, [f"data: {json.dumps({'error': error})}\n\n".encode()])
    adapter = _adapter()
    pool = KeyPool(keys=["test-key-one", "test-key-two"], provider_label="staging")
    adapter._key_pool = pool
    releases = []
    original_release = pool.release

    def release(lease, **kwargs):
        releases.append(kwargs["status_code"])
        return original_release(lease, **kwargs)

    monkeypatch.setattr(pool, "release", release)
    with pytest.raises(aiohttp.ClientResponseError):
        await _stream(adapter)

    assert releases == [status]
