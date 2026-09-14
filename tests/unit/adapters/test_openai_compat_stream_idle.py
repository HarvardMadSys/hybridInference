"""Mid-stream idle detection for OpenAI-compatible upstreams.

The failure these guard against: an sglang replica wedges -- the process stays
up, the socket stays open, its Prometheus counters go byte-identical for 4-7
minutes, and it sends nothing. The gateway had no clock of its own for that, so
it learned about the stall only when the deployment proxy's byte-anchored 300s
read timeout expired every committed stream at once, and it kept committing new
streams to the dead replica in the meantime.

These drive the real ``serving.http`` reader over a fake socket whose body
arrives on a script of delays, which is the only shape that exercises the gap
between two frames rather than a stubbed iterator's instantaneous one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.exceptions import UpstreamStreamIdleError
from serving.http import AsyncHTTPClient

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _adapter() -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        ModelConfig(
            id="deepseek-v4-flash",
            name="DeepSeek-V4-Flash",
            provider="sglang",
            base_url="http://h200a.local:8003/v1",
            provider_model_id="deepseek-v4-flash",
            processor="default",
            supported_params=["temperature", "max_tokens"],
        )
    )


class _Resp:
    """Fake aiohttp response whose body arrives on a script of ``(delay, bytes)``."""

    def __init__(self, steps: list[tuple[float, bytes]]):
        self._steps = steps
        self.content = self
        self.headers = {"Content-Type": "text/event-stream"}
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def iter_chunked(self, _n: int):
        for delay, chunk in self._steps:
            if delay:
                await asyncio.sleep(delay)
            yield chunk


def _serve(monkeypatch, steps: list[tuple[float, bytes]]) -> None:
    async def fake_ensure(self):
        class _Session:
            def post(self, *_a, **_k):
                return _Resp(steps)

        return _Session()

    monkeypatch.setattr(AsyncHTTPClient, "_ensure_session", fake_ensure)


def _frame(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-idle",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


_DONE = b"data: [DONE]\n\n"


async def _collect(adapter) -> list[str]:
    return [
        chunk async for chunk in adapter.stream_chat_completion([{"role": "user", "content": "hi"}])
    ]


def _text(chunks: list[str]) -> str:
    out = ""
    for chunk in chunks:
        if not chunk.startswith("data: ") or chunk.strip() == "data: [DONE]":
            continue
        payload = json.loads(chunk[6:])
        for choice in payload.get("choices") or []:
            out += (choice.get("delta") or {}).get("content", "") or ""
    return out


# --- the stall itself ------------------------------------------------------


async def test_idle_gap_beyond_threshold_raises_the_distinct_error(monkeypatch):
    """A gap between two frames longer than the budget is an upstream fault."""
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "0.05")
    _serve(
        monkeypatch,
        [
            (0.0, _frame({"role": "assistant"})),
            (0.0, _frame({"content": "par"})),
            (0.6, _frame({"content": "tial"})),  # the wedge
            (0.0, _DONE),
        ],
    )

    delivered: list[str] = []
    with pytest.raises(UpstreamStreamIdleError) as excinfo:
        async for chunk in _adapter().stream_chat_completion([{"role": "user", "content": "hi"}]):
            delivered.append(chunk)

    assert excinfo.value.idle_seconds == 0.05
    assert excinfo.value.endpoint_id  # attributed to an endpoint, not "somewhere"
    assert excinfo.value.frames >= 1
    # Distinct from the end-of-body check, which is what the proxy's own timeout
    # produces once it gives up -- an operator has to be able to tell them apart.
    assert "stopped sending stream data" in str(excinfo.value)
    assert "ended without a terminal" not in str(excinfo.value)
    # The partial answer is not capped with a fabricated terminal chunk.
    assert all(c.strip() != "data: [DONE]" for c in delivered)


async def test_gaps_under_the_threshold_complete_normally(monkeypatch):
    """A merely slow upstream is not a stalled one."""
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "0.5")
    _serve(
        monkeypatch,
        [
            (0.02, _frame({"role": "assistant"})),
            (0.05, _frame({"content": "hel"})),
            (0.05, _frame({"content": "lo"})),
            (0.05, _frame({}, finish_reason="stop")),
            (0.0, _DONE),
        ],
    )

    chunks = await _collect(_adapter())

    assert _text(chunks) == "hello"
    assert chunks[-1].strip() == "data: [DONE]"


# --- the long-prefill safety property --------------------------------------


async def test_slow_first_token_is_not_killed(monkeypatch):
    """The one that matters: prefill must not be charged against the idle budget.

    These models are configured up to 1M context, and a full 1M prompt measures
    138s to first token on the local sglang replicas. A byte-anchored socket
    timeout cannot tell that apart from a stall, which is why the idle clock
    starts only once the first frame is in hand.
    """
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "0.05")
    _serve(
        monkeypatch,
        [
            (0.6, _frame({"role": "assistant"})),  # 12x the idle budget, spent prefilling
            (0.0, _frame({"content": "hello"})),
            (0.0, _frame({}, finish_reason="stop")),
            (0.0, _DONE),
        ],
    )

    chunks = await _collect(_adapter())

    assert _text(chunks) == "hello"
    assert chunks[-1].strip() == "data: [DONE]"


async def test_first_byte_budget_is_unbounded_unless_configured(monkeypatch):
    """Nothing is put on the socket clock by default, so prefill keeps its freedom."""
    monkeypatch.delenv("STREAM_FIRST_BYTE_TIMEOUT_SECONDS", raising=False)
    assert _adapter()._build_stream_timeout() is None

    monkeypatch.setenv("STREAM_FIRST_BYTE_TIMEOUT_SECONDS", "600")
    assert _adapter()._build_stream_timeout().sock_read == 600.0


# --- what must NOT fire it -------------------------------------------------


async def test_client_disconnect_is_not_an_idle_timeout(monkeypatch):
    """A caller hanging up is a BaseException, and must stay one.

    Charging it to the upstream would let flaky client networks open the circuit
    for every other caller of the model.
    """
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "5")
    _serve(
        monkeypatch,
        [
            (0.0, _frame({"role": "assistant"})),
            (0.0, _frame({"content": "hi"})),
            (30.0, _DONE),  # still generating when the client goes away
        ],
    )

    seen = asyncio.Event()
    raised: list[BaseException] = []

    async def _consume() -> None:
        try:
            async for chunk in _adapter().stream_chat_completion(
                [{"role": "user", "content": "hi"}]
            ):
                if "hi" in chunk:
                    seen.set()
        except BaseException as exc:
            raised.append(exc)
            raise

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(seen.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert raised and isinstance(raised[0], asyncio.CancelledError)
    assert not any(isinstance(exc, UpstreamStreamIdleError) for exc in raised)


async def test_detector_can_be_switched_off(monkeypatch):
    """A non-positive override disables it, for a deployment that wants none."""
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "0")
    _serve(
        monkeypatch,
        [
            (0.0, _frame({"role": "assistant"})),
            (0.3, _frame({"content": "late"})),
            (0.0, _frame({}, finish_reason="stop")),
            (0.0, _DONE),
        ],
    )

    chunks = await _collect(_adapter())

    assert _text(chunks) == "late"


async def test_env_override_widens_the_budget(monkeypatch):
    """The same gap passes or fails purely on the configured budget."""
    steps = [
        (0.0, _frame({"role": "assistant"})),
        (0.3, _frame({"content": "late"})),
        (0.0, _frame({}, finish_reason="stop")),
        (0.0, _DONE),
    ]

    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "0.05")
    _serve(monkeypatch, steps)
    with pytest.raises(UpstreamStreamIdleError):
        await _collect(_adapter())

    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "2")
    _serve(monkeypatch, steps)
    assert _text(await _collect(_adapter())) == "late"
