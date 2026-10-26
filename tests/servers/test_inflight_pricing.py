"""Settlement follows the executed adapter across runtime route changes."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.routers import completions
from serving.servers.routers.completions_cost import CostTracker, PricingLookup


class HeldAdapter(BaseAdapter):
    """Wait for a route edit after dispatch, then return successful usage."""

    def __init__(self, price: str):
        super().__init__(
            ModelConfig(
                id="m",
                name="m",
                provider="test",
                endpoint_id="m:test",
                base_url="http://localhost:9999/v1",
                context_length=4096,
                max_output_length=1024,
                pricing={"prompt": price, "completion": "0"},
            )
        )
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def chat_completion(self, messages, **params):
        self.started.set()
        await self.finish.wait()
        return {
            "id": "test-completion",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101},
        }

    async def stream_chat_completion(self, messages, **params):
        self.started.set()
        await self.finish.wait()
        yield self.format_stream_chunk(model="m", content="ok")
        yield (
            "data: "
            + json.dumps(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101},
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("strategy", ["fixed", "routewise"])
@pytest.mark.parametrize("change", ["delete", "replace"])
async def test_inflight_request_keeps_its_price(
    monkeypatch, mock_log_store, stream, strategy, change
):
    """Both quota and api_logs keep the old price; a new request gets the edit."""
    monkeypatch.setenv("USER_AUTH_ENABLED", "0")
    table = FixedRouter()
    old = HeldAdapter("2")
    table.register_route("m", [(old, 1.0)])
    registry = ModelRouterRegistry({"m": {"router": strategy}}, shared_fixed_router=table)
    pricing = PricingLookup(router=table)
    store = AsyncMock()
    tracker = CostTracker(op_store=store, pricing=pricing)
    logged = asyncio.Event()
    mock_log_store.log_request.side_effect = lambda *a, **kw: logged.set()
    app = FastAPI()
    app.include_router(completions.router)
    app.state.services = AppServices(
        router=table,
        model_router_registry=registry,
        log_store=mock_log_store,
        pricing_lookup=pricing,
        cost_tracker=tracker,
    )
    app.dependency_overrides[verify_api_key] = lambda: {
        "user_id": "test-user",
        "role": "internal",
        "authenticated": True,
    }
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post("/v1/chat/completions", json=payload))
        try:
            await asyncio.wait_for(old.started.wait(), 3)
            if change == "delete":
                table.routes.pop("m")
            else:
                new = HeldAdapter("9")
                new.finish.set()
                table.register_route("m", [(new, 1.0)])
            old.finish.set()
            response = await pending
        finally:
            old.finish.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert response.status_code == 200, response.text
        await asyncio.wait_for(logged.wait(), 3)
        if tracker._background_tasks:
            await asyncio.gather(*tracker._background_tasks)
        store.increment_user_cost.assert_awaited_once_with("test-user", pytest.approx(0.0002))
        log = mock_log_store.log_request.call_args.kwargs
        assert log["pricing"] == {"prompt": "2", "completion": "0"}
        assert log["upstream_cost_usd"] == pytest.approx(0.0002)

        if change == "replace":
            # The gateway's RouteWise registry rebuilds the model router after a
            # route edit; simulate that while preserving the shared lookup.
            if strategy == "routewise":
                app.state.services.model_router_registry = ModelRouterRegistry(
                    {"m": {"router": strategy}}, shared_fixed_router=table
                )
            logged.clear()
            response = await client.post("/v1/chat/completions", json=payload)
            assert response.status_code == 200, response.text
            await asyncio.wait_for(logged.wait(), 3)
            if tracker._background_tasks:
                await asyncio.gather(*tracker._background_tasks)
            assert store.increment_user_cost.await_count == 2
            assert store.increment_user_cost.call_args.args[1] == pytest.approx(0.0009)
            assert mock_log_store.log_request.call_args.kwargs["pricing"]["prompt"] == "9"
