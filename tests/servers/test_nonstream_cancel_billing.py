"""A non-streaming request cut off before it answered is billed for what it sent (H15).

``TimeoutMiddleware`` cancels a request at the gateway's deadline (120 s by
default) and answers 504, while the upstream -- whose own timeout is 600 s --
goes on with the request. A stream cut off that way has long been billed for
what it used; a non-streaming request was neither billed nor logged. Now it is
billed by an estimate of its prompt once it left the gateway (the outbound
limiter's ``report_sent``), and logged as 504, or as 499 when the client left.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.dispatch_watch import report_sent
from serving.batch.dispatch import dispatch_item
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.timeout import TimeoutMiddleware
from serving.servers.routers import anthropic_messages, completions, responses
from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.storage.utils import calculate_cost
from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

pytestmark = pytest.mark.asyncio

PRICING = {"prompt": "2.8", "completion": "6.4"}
PROMPT = "lorem ipsum dolor sit amet " * 4_000


class _SlowUpstream(BaseAdapter):
    """Takes the request, and answers long after any deadline.

    ``first`` is a reply given at once, before the call that hangs.
    ``sends=False`` hangs before the request leaves the gateway: a slot wait,
    say, which never reports the request sent.
    """

    def __init__(self, *, sends: bool = True, first: dict[str, Any] | None = None) -> None:
        super().__init__(
            ModelConfig(
                id="m",
                name="m",
                provider="test",
                endpoint_id="m:test",
                base_url="http://localhost:9999/v1",
                context_length=400_000,
                max_output_length=131_072,
                pricing=dict(PRICING),
            )
        )
        self.sends = sends
        self.first = first
        self.calls = 0
        self.sent = asyncio.Event()

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls += 1
        if self.first is not None and self.calls == 1:
            return self.first
        if self.sends:
            # What the outbound limiter reports as it gives the request its slot.
            report_sent()
            self.sent.set()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("not streamed")
        yield


def _chat_app(
    upstream: _SlowUpstream, log_store: Any, *, timeout_s: float = 0.3
) -> tuple[FastAPI, AsyncMock, CostTracker]:
    table = FixedRouter()
    table.register_route("m", [(upstream, 1.0)])
    registry = ModelRouterRegistry({"m": {"router": "fixed"}}, shared_fixed_router=table)
    pricing = PricingLookup(router=table)
    store = AsyncMock()
    tracker = CostTracker(op_store=store, pricing=pricing)
    app = FastAPI()
    app.include_router(completions.router)
    app.add_middleware(TimeoutMiddleware, timeout_s=timeout_s, stream_timeout_s=60)
    app.state.services = AppServices(
        router=table,
        model_router_registry=registry,
        log_store=log_store,
        pricing_lookup=pricing,
        cost_tracker=tracker,
    )
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "u",
        "role": "internal",
        "authenticated": True,
    }
    return app, store, tracker


def _chat(**extra: Any) -> dict[str, Any]:
    return {"model": "m", "messages": [{"role": "user", "content": PROMPT}], **extra}


async def _logged_row(log_store: Any) -> dict[str, Any]:
    for _ in range(300):
        if log_store.log_request.await_count:
            return log_store.log_request.await_args.kwargs
        await asyncio.sleep(0.01)
    raise AssertionError("no request log row was written")


async def _charged(store: AsyncMock, tasks: set[asyncio.Task[Any]]) -> float | None:
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0.01)
    if not store.increment_user_cost.await_count:
        return None
    return float(store.increment_user_cost.await_args.args[1])


async def test_a_request_the_deadline_cuts_off_after_dispatch_is_billed_and_logged(
    monkeypatch, mock_log_store
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    upstream = _SlowUpstream()
    app, store, tracker = _chat_app(upstream, mock_log_store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/v1/chat/completions", json=_chat(max_tokens=100))

    assert response.status_code == 504
    row = await _logged_row(mock_log_store)
    assert row["status_code"] == 504
    assert row["metadata"]["terminal_state"] == "request_timeout"
    assert row["metadata"]["usage_estimated"] is True
    assert row["usage"]["completion_tokens"] == 0
    assert row["usage"]["prompt_tokens"] > 10_000
    assert row["pricing"] == PRICING
    charged = await _charged(store, tracker._background_tasks)
    assert charged == pytest.approx(calculate_cost(row["usage"], row["pricing"]))


async def test_a_request_cut_off_before_it_was_sent_is_logged_but_not_billed(
    monkeypatch, mock_log_store
):
    """Before the upstream was asked, there is nothing it charged for."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    upstream = _SlowUpstream(sends=False)
    app, store, tracker = _chat_app(upstream, mock_log_store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/v1/chat/completions", json=_chat())

    assert response.status_code == 504
    row = await _logged_row(mock_log_store)
    assert (row["status_code"], row["usage"], row["pricing"]) == (504, None, None)
    assert await _charged(store, tracker._background_tasks) is None


async def test_a_reply_already_given_is_billed_with_the_retry_cut_off(monkeypatch, mock_log_store):
    """A structured-output retry in flight: the first reply as reported, the retry by estimate."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    first = {
        "id": "first",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "prose, not JSON"},
            }
        ],
        "usage": {"prompt_tokens": 1_000, "completion_tokens": 50, "total_tokens": 1_050},
    }
    upstream = _SlowUpstream(first=first)
    app, store, tracker = _chat_app(upstream, mock_log_store)
    body = _chat(response_format={"type": "json_object"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 504
    assert upstream.calls == 2
    row = await _logged_row(mock_log_store)
    assert row["usage"]["completion_tokens"] == 50
    assert row["usage"]["prompt_tokens"] > 1_000 + 10_000
    charged = await _charged(store, tracker._background_tasks)
    assert charged == pytest.approx(calculate_cost(row["usage"], row["pricing"]))


async def test_a_client_that_leaves_after_dispatch_is_billed_and_logged_as_499(
    monkeypatch, mock_log_store
):
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    upstream = _SlowUpstream()
    app, store, tracker = _chat_app(upstream, mock_log_store, timeout_s=60)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        pending = asyncio.create_task(client.post("/v1/chat/completions", json=_chat()))
        await asyncio.wait_for(upstream.sent.wait(), 3)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    row = await _logged_row(mock_log_store)
    assert row["status_code"] == 499
    assert row["metadata"]["terminal_state"] == "client_disconnect"
    assert await _charged(store, tracker._background_tasks) == pytest.approx(
        calculate_cost(row["usage"], row["pricing"])
    )


async def test_a_responses_request_the_deadline_cuts_off_after_dispatch_is_billed(
    monkeypatch, mock_log_store
):
    """The Responses API delegates to the chat execution, and is billed the same way."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    upstream = _SlowUpstream()
    app, store, tracker = _chat_app(upstream, mock_log_store)
    app.include_router(responses.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/v1/responses", json={"model": "m", "input": PROMPT, "store": False}
        )

    assert response.status_code == 504
    row = await _logged_row(mock_log_store)
    assert (row["status_code"], row["metadata"]["terminal_state"]) == (504, "request_timeout")
    charged = await _charged(store, tracker._background_tasks)
    assert charged
    assert charged == pytest.approx(calculate_cost(row["usage"], row["pricing"]))


async def test_a_batch_attempt_cut_off_at_its_deadline_is_left_to_the_batch_worker():
    """The batch worker settles its own abandoned attempts; nothing is billed here."""
    upstream = _SlowUpstream()
    router = FixedRouter()
    router.register_route("m", [(upstream, 1.0)])
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()
    services = AppServices(router=router, log_store=log_store, operational_store=op_store)
    app = FastAPI()
    app.state.services = services

    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx={
            "user_id": "owner-1",
            "role": "internal",
            "authenticated": True,
            "is_admin": False,
            "disabled_models": [],
            "max_concurrent_requests": None,
            "auth_key_hash": "owner-key-hash",
        },
        body=_chat(),
        batch_id="batch-t",
        target="chat",
        timeout_s=0.3,
    )

    assert result.status_code == 504
    assert result.cost_status == "unknown"
    await asyncio.sleep(0.05)
    op_store.increment_user_cost.assert_not_awaited()
    log_store.log_request.assert_not_awaited()


async def test_a_messages_request_the_deadline_cuts_off_after_dispatch_is_billed_and_logged(
    anthropic_test_app, anthropic_compat_router, mock_operational_store, mock_log_store, monkeypatch
):
    adapter = anthropic_compat_router.routes["glm-4.7"].adapters[0][0]

    async def _slow_messages(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        report_sent()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    monkeypatch.setattr(adapter, "messages", _slow_messages)
    anthropic_test_app.add_middleware(TimeoutMiddleware, timeout_s=0.3, stream_timeout_s=60)
    body = {
        "model": "glm-4.7",
        "max_tokens": 64,
        "system": "You are terse.",
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
    }

    async with AsyncClient(
        transport=ASGITransport(app=anthropic_test_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json=body,
            headers={"Authorization": f"Bearer {ANTHROPIC_TEST_API_KEY}"},
        )

    assert response.status_code == 504
    estimate = anthropic_messages._estimate_request_input_tokens(body)
    row = await _logged_row(mock_log_store)
    assert row["status_code"] == 504
    assert row["metadata"]["terminal_state"] == "request_timeout"
    assert row["metadata"]["usage_estimated"] is True
    assert (row["usage"]["prompt_tokens"], row["usage"]["completion_tokens"]) == (estimate, 0)
    charged = await _charged(mock_operational_store, set(anthropic_messages._cost_increment_tasks))
    assert charged == pytest.approx(calculate_cost(row["usage"], row["pricing"]))
    assert charged == pytest.approx(estimate * 0.6 / 1_000_000)
