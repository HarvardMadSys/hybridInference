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
from serving.adapters.openai_compat import OpenAICompatAdapter, UpstreamStreamError
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


def _error_frame(error: dict) -> bytes:
    """An upstream's mid-stream error frame, as OpenAI-compatible servers send it."""
    return f"data: {json.dumps({'error': error})}\n\n".encode()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_frame_surfaces_upstream_status_and_message(monkeypatch):
    """The production page: a relayed 4xx counted against the endpoint.

    A gateway upstream refused the request with 403 ("concurrent request
    limit"), sent it as an error frame, and closed without a terminator. Both
    terminal flags were unset, so the adapter raised the statusless
    ``_INCOMPLETE_STREAM_ERROR``; with no status to classify on,
    ``record_failure`` skipped its client-error exemption and the breaker
    tripped -- over a refusal the upstream had itself excused.
    """
    _serve(
        monkeypatch,
        [
            _frame({"role": "assistant"}),
            _error_frame(
                {
                    "message": "You've reached your concurrent request limit.",
                    "type": "server_error",
                    "code": 403,
                }
            ),
        ],
    )

    with pytest.raises(UpstreamStreamError) as excinfo:
        async for _ in _adapter().stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    assert excinfo.value.status == 403
    assert "concurrent request limit" in str(excinfo.value)
    # The framing complaint must not mask the reason the upstream gave.
    assert "finish_reason" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_frame_without_numeric_code_still_counts_as_upstream_fault(monkeypatch):
    """A string ``code`` is not a status, and guessing one is worse than 502.

    ``rate_limit_exceeded`` and friends are the OpenAI spelling. Falling back to
    502 keeps the breaker counting the failure exactly as it did before this
    check existed -- the exemption is opt-in on a status we can actually read.
    """
    _serve(
        monkeypatch,
        [_error_frame({"message": "upstream exploded", "code": "internal_error"})],
    )

    with pytest.raises(UpstreamStreamError) as excinfo:
        async for _ in _adapter().stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    assert excinfo.value.status == 502
    assert "upstream exploded" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_null_error_on_a_content_chunk_is_not_an_error_frame(monkeypatch):
    """Several servers stamp ``"error": null`` on every delta. That is not a failure."""
    payload = {
        "model": "glm-5.2",
        "error": None,
        "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
    }
    _serve(
        monkeypatch,
        [
            f"data: {json.dumps(payload)}\n\n".encode(),
            _frame({}, finish_reason="stop"),
            b"data: [DONE]\n\n",
        ],
    )

    chunks = await _stream(_adapter())

    assert chunks[-1].strip() == "data: [DONE]"
