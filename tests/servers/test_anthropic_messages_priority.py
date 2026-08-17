"""Prefill accounting and scheduling priority on the Anthropic Messages surface.

/v1/messages picks its own adapter and never enters FixedRouter, so none of the
router's prefill accounting applied here -- and this is where the
prefill-dominated Claude Code traffic arrives. A local sglang backend started
with --enable-priority-scheduling would therefore have ordered its queue by
arrival for exactly the requests the priority exists to order.

Companion to test_anthropic_messages_health_recording.py, which covers the other
half of what this surface has to do for itself.
"""

from __future__ import annotations

import asyncio

import pytest

from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import PRIORITY_ELEPHANT, PRIORITY_INTERACTIVE

# The OpenAI-compatible route on this fixture, reached through the Anthropic
# surface's translation -- the shape a local sglang backend has here.
COMPAT_MODEL = "glm-4.7"


def _auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _body(**overrides):
    body = {
        "model": COMPAT_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(overrides)
    return body


def _adapter(router):
    adapter, _weight = router.routes[COMPAT_MODEL].adapters[0]
    return adapter


@pytest.fixture
def no_log_store(monkeypatch):
    captured: dict = {}
    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(
        amod, "_schedule_log_store_task", lambda log_store, **kwargs: captured.update(kwargs)
    )
    return captured


@pytest.fixture
def priority_route(anthropic_compat_router):
    """Mark the dispatch route as an sglang server running priority scheduling."""
    adapter = _adapter(anthropic_compat_router)
    original = adapter.config.priority_scheduling
    adapter.config.priority_scheduling = True
    yield adapter
    adapter.config.priority_scheduling = original


def _capture_upstream(monkeypatch):
    """Record the body the OpenAI-compatible adapter sends upstream.

    The Anthropic body is translated to OpenAI form by ``BaseAdapter.messages``
    before it reaches this payload, which is exactly the path a local sglang
    backend serves Claude Code traffic through.
    """
    sent: dict = {}
    upstream_resp = {
        "id": "chatcmpl-ok",
        "object": "chat.completion",
        "model": "glm-4.7",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }

    async def fake_post(self, url, payload):
        sent.clear()
        sent.update(payload or {})
        return upstream_resp

    from serving.adapters.openai_compat import OpenAICompatAdapter

    monkeypatch.setattr(OpenAICompatAdapter, "_post_with_pool", fake_post)
    return sent


@pytest.mark.asyncio
async def test_small_request_is_stamped_interactive(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store, priority_route
):
    sent = _capture_upstream(monkeypatch)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 200
    assert sent["priority"] == PRIORITY_INTERACTIVE
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_system_prompt_counts_toward_the_size(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store, priority_route
):
    """Anthropic carries the system prompt beside ``messages``, not inside it.

    For a coding agent that block is the largest fixed part of the prompt, so a
    handler that sized only ``messages`` would rank a mega-prompt interactive.
    """
    sent = _capture_upstream(monkeypatch)
    huge_system = "You are a coding agent. " * 40_000  # ~240k tokens

    r = await anthropic_test_client.post(
        "/v1/messages", json=_body(system=huge_system), headers=_auth()
    )

    assert r.status_code == 200
    assert sent["priority"] == PRIORITY_ELEPHANT
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_route_without_the_flag_is_not_stamped(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store
):
    # The flag says the *server* understands the field. A remote Anthropic
    # endpoint that validates its request body must not receive it.
    sent = _capture_upstream(monkeypatch)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 200
    assert "priority" not in sent
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_lease_is_returned_after_a_successful_request(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store, priority_route
):
    """A leaked lease would leave the endpoint permanently charged for prefill.

    That is worse than no accounting at all: selection would steer traffic away
    from a replica that is actually idle, forever.
    """
    _capture_upstream(monkeypatch)
    endpoint_id = endpoint_id_for_adapter(_adapter(anthropic_compat_router))

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 200
    assert anthropic_compat_router.prefill_load.backlog(endpoint_id) == 0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_lease_is_returned_after_a_failed_request(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store, priority_route
):
    # Every except arm on this handler returns its own error response, so the
    # release has to come from a finally that covers all of them.
    from serving.adapters.key_pool import KeyPoolExhausted

    async def fake_post(self, url, payload):
        raise KeyPoolExhausted("all keys muted")

    from serving.adapters.openai_compat import OpenAICompatAdapter

    monkeypatch.setattr(OpenAICompatAdapter, "_post_with_pool", fake_post)
    endpoint_id = endpoint_id_for_adapter(_adapter(anthropic_compat_router))

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 429
    assert anthropic_compat_router.prefill_load.backlog(endpoint_id) == 0
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_completed_turn_makes_the_next_one_interactive(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store, priority_route
):
    """The warm-continuation discount has to work on this surface too.

    Without the lease being recorded here, every turn of a long Claude Code
    session would read cold and be stamped elephant -- the mis-tiering this
    whole mechanism exists to avoid, just relocated to the busiest surface.
    """
    sent = _capture_upstream(monkeypatch)
    system = "You are a coding agent. " * 40_000
    first = _body(system=system, messages=[{"role": "user", "content": "find the bug"}])

    r1 = await anthropic_test_client.post("/v1/messages", json=first, headers=_auth())
    assert r1.status_code == 200
    assert sent["priority"] == PRIORITY_ELEPHANT

    follow_up = _body(
        system=system,
        messages=[
            {"role": "user", "content": "find the bug"},
            {"role": "assistant", "content": "looking"},
            {"role": "user", "content": "and now?"},
        ],
    )
    r2 = await anthropic_test_client.post("/v1/messages", json=follow_up, headers=_auth())

    assert r2.status_code == 200
    assert sent["priority"] == PRIORITY_INTERACTIVE
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_streaming_lease_is_taken_when_the_generator_runs(
    anthropic_test_client, anthropic_compat_router, monkeypatch, no_log_store, priority_route
):
    """Acquisition must be bound to the generator's execution, not the handler's.

    Starlette starts iterating only after the handler returns, so a client that
    disconnects in between leaves a generator that never runs -- and a lease
    taken in the handler body would never be released, charging the endpoint
    until process restart while selection steers traffic away from a replica
    that is idle. Pinned by construction time: no lease may exist yet when the
    response object is built.
    """
    from fastapi import responses as fastapi_responses

    tracker = anthropic_compat_router.prefill_load
    endpoint_id = endpoint_id_for_adapter(_adapter(anthropic_compat_router))
    calls: list[str] = []
    real_acquire = tracker.acquire

    def spy_acquire(*args, **kwargs):
        calls.append("acquire")
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(tracker, "acquire", spy_acquire)

    at_construction: dict = {}
    real_cls = fastapi_responses.StreamingResponse

    class _Recording(real_cls):
        def __init__(self, content, **kwargs):
            at_construction["leases"] = len(calls)
            super().__init__(content, **kwargs)

    monkeypatch.setattr(fastapi_responses, "StreamingResponse", _Recording)

    async def fake_stream(self, body, request_id, usage_sink=None, extra_headers=None):
        yield b'event: message_start\ndata: {"type":"message_start"}\n\n'

    from serving.adapters.base import BaseAdapter

    monkeypatch.setattr(BaseAdapter, "stream_messages", fake_stream)

    r = await anthropic_test_client.post("/v1/messages", json=_body(stream=True), headers=_auth())

    assert r.status_code == 200
    # The response object existed before any lease did...
    assert at_construction["leases"] == 0
    # ...the generator then took one, and gave it back.
    assert calls == ["acquire"]
    assert tracker.backlog(endpoint_id) == 0
    await asyncio.sleep(0)
