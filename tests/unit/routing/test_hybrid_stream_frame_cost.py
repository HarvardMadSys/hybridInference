"""The hybrid streaming path parses only the frames that can carry ``_routing``.

Every chunk of a stream served through ``HybridRouter`` used to be parsed as
JSON two or three times -- once to remember the dispatch, once to decide whether
the client could see it, once to tag it with its backend -- although nearly all
of them are content deltas carrying nothing either step reads. The frame helpers
now skip a frame whose text cannot hold the key, and the dispatch record stops
reading content once the stream is committed.

What must not change: which frames are visible, how frames are tagged (and that
every other frame goes out byte for byte), and the dispatch record that routes
feedback -- including a routing frame that arrives after the content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from routing import hybrid as hybrid_module, hybrid_stream_frames as frames_module
from routing.decisions import RoutingDecision
from routing.hybrid import HybridRouter
from routing.hybrid_stream_frames import carries_routing, is_client_visible, tag_backend_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MODEL_ID = "frames-model"
_MESSAGES = [{"role": "user", "content": "hi"}]


def _frame(payload: Any) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# Every shape a stream can carry, the awkward ones included.
_CORPUS: list[Any] = [
    _frame({"choices": [], "_routing": {"provider": "p", "endpoint_id": "m:a"}}),
    _frame({"choices": [{"delta": {"content": "x"}}], "_routing": {"provider": "p"}}),
    _frame({"choices": [], "usage": {"prompt_tokens": 1}, "_routing": {"upstream_cost_usd": 0.1}}),
    _frame({"choices": [], "_routing": "not-a-dict"}),
    _frame({"choices": [{"delta": {"content": "hello"}}]}),
    _frame({"choices": [{"delta": {"content": 'quoting "_routing" in prose'}}]}),
    _frame({"choices": [{"delta": {"content": "", "role": "assistant"}}]}),
    _frame({"model": "_routing", "choices": []}),
    _frame([1, 2, 3]),
    _frame("just a string"),
    "data: [DONE]\n\n",
    'data: {"choices": [], "_routing": {"provider": broken}\n\n',
    "data: not json at all\n\n",
    ": keep-alive\n\n",
    "",
    b'data: {"choices": [], "_routing": {}}\n\n',
    {"choices": [], "_routing": {"provider": "p"}},
    None,
]


def _reference_is_client_visible(chunk: Any) -> bool:
    """The helper as it was before the pre-check."""
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return True
    raw = chunk[6:].strip()
    if raw == "[DONE]":
        return True
    try:
        payload = json.loads(raw)
    except ValueError:
        return True
    if not isinstance(payload, dict):
        return True
    if "_routing" not in payload:
        return True
    return bool(payload.get("choices")) or payload.get("usage") is not None


def _reference_tag_backend_chunk(chunk: Any, backend_name: str) -> Any:
    """The helper as it was before the pre-check."""
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return chunk
    raw = chunk[6:].strip()
    if raw == "[DONE]":
        return chunk
    try:
        payload = json.loads(raw)
    except ValueError:
        return chunk
    if not isinstance(payload, dict):
        return chunk
    routing = payload.get("_routing")
    if not isinstance(routing, dict):
        return chunk
    routing.setdefault("backend", backend_name)
    return f"data: {json.dumps(payload)}\n\n"


@pytest.mark.unit
@pytest.mark.parametrize("chunk", _CORPUS, ids=repr)
def test_visibility_is_unchanged(chunk):
    assert is_client_visible(chunk) == _reference_is_client_visible(chunk)


@pytest.mark.unit
@pytest.mark.parametrize("chunk", _CORPUS, ids=repr)
def test_tagging_is_unchanged(chunk):
    expected = _reference_tag_backend_chunk(
        json.loads(json.dumps(chunk)) if isinstance(chunk, dict) else chunk, "cloud"
    )
    assert tag_backend_chunk(chunk, "cloud") == expected


@pytest.mark.unit
def test_a_content_frame_is_not_parsed(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a content frame was parsed")

    monkeypatch.setattr(frames_module.json, "loads", refuse)
    content = _frame({"choices": [{"delta": {"content": "hello"}}]})

    assert not carries_routing(content)
    assert is_client_visible(content) is True
    assert tag_backend_chunk(content, "cloud") is content


# ---------------------------------------------------------------------------
# Through HybridRouter's stream
# ---------------------------------------------------------------------------


@dataclass
class _ForceBackend:
    backend_name: str

    def select_backend(self, model_id, messages, *, routing_options=None, **params):
        return RoutingDecision(backend=self.backend_name, target=None)

    def fallback_backends(self, model_id, messages, decision, *, routing_options=None, **params):
        return ()


@dataclass
class _ScriptedBackend:
    """Streams a fixed list of frames and records the observations it is given."""

    backend_name: str
    frames: tuple[str, ...] = ()
    observations: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.backend_name

    async def chat_completion(self, model_id, messages, **kwargs) -> dict[str, Any]:
        raise AssertionError("not used")

    async def stream_chat_completion(self, model_id, messages, **kwargs) -> AsyncGenerator[str]:
        for frame in self.frames:
            yield frame

    def record_observation(self, obs) -> None:
        self.observations.append(obs.endpoint_id)

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        return {}

    def owns_observation(self, obs) -> bool:
        return False

    def serves(self, model_id: str) -> bool:
        return True


def _stream_frames(content_chunks: int, *, trailing: dict[str, Any] | None = None) -> tuple:
    frames = [_frame({"choices": [], "_routing": {"provider": "p", "endpoint_id": "m:primary"}})]
    frames += [
        _frame({"choices": [{"delta": {"content": f"t{i}"}}]}) for i in range(content_chunks)
    ]
    if trailing is not None:
        frames.append(_frame({"choices": [], "_routing": trailing}))
    frames.append("data: [DONE]\n\n")
    return tuple(frames)


async def _drain(router: HybridRouter, request_id: str) -> list[Any]:
    return [
        chunk
        async for chunk in router.stream_chat_completion(
            _MODEL_ID, _MESSAGES, request_id=request_id
        )
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parsing_does_not_grow_with_the_stream(monkeypatch):
    calls = {"loads": 0}
    real_loads = json.loads

    def counting(*args, **kwargs):
        calls["loads"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(frames_module.json, "loads", counting)
    monkeypatch.setattr(hybrid_module.json, "loads", counting)

    counts = []
    for content_chunks in (10, 200):
        calls["loads"] = 0
        cloud = _ScriptedBackend("cloud", frames=_stream_frames(content_chunks))
        router = HybridRouter(
            policy=_ForceBackend("cloud"), local=_ScriptedBackend("local"), cloud=cloud
        )
        chunks = await _drain(router, f"req-{content_chunks}")
        assert len(chunks) == content_chunks + 2
        counts.append(calls["loads"])

    # Only the routing frame is parsed (to remember, to test, to tag), however long the answer.
    assert counts[0] == counts[1] <= 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_frames_go_out_tagged_or_untouched():
    content = _frame({"choices": [{"delta": {"content": "hello"}}]})
    cloud = _ScriptedBackend(
        "cloud",
        frames=(
            _frame({"choices": [], "_routing": {"provider": "p", "endpoint_id": "m:primary"}}),
            content,
            "data: [DONE]\n\n",
        ),
    )
    router = HybridRouter(
        policy=_ForceBackend("cloud"), local=_ScriptedBackend("local"), cloud=cloud
    )

    routing_frame, forwarded, done = await _drain(router, "req-tag")

    assert json.loads(routing_frame[6:])["_routing"]["backend"] == "cloud"
    assert forwarded is content
    assert done == "data: [DONE]\n\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_routing_frame_after_the_content_still_routes_feedback():
    """A late frame can name an attempt no earlier frame did, such as a failed hedge leg."""
    trailing = {
        "failed_attempts": [{"endpoint_id": "m:hedge-leg", "error_type": "E", "error": "x"}]
    }
    local = _ScriptedBackend("local")
    cloud = _ScriptedBackend("cloud", frames=_stream_frames(5, trailing=trailing))
    router = HybridRouter(policy=_ForceBackend("cloud"), local=local, cloud=cloud)

    await _drain(router, "req-late")
    record = router._backend_decisions["req-late"]
    assert record.endpoints == {"m:primary": "cloud", "m:hedge-leg": "cloud"}

    from routing.routers import RoutingObservation

    router.record_observation(
        RoutingObservation(
            model_id=_MODEL_ID,
            endpoint_id="m:hedge-leg",
            ttft_ms=None,
            total_latency_ms=0.0,
            token_count=0,
            success=False,
            request_id="req-late",
            terminal=False,
        )
    )
    assert cloud.observations == ["m:hedge-leg"]
    assert local.observations == []
