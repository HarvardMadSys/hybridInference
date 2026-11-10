"""A chat request with ``response_format``, through the real chat execution.

The batch path is used because it runs ``execute_chat`` without an HTTP
request; an online non-streaming request takes the same code. Only the upstream
adapter is a stand-in, answering prose before JSON the way a model that does
not enforce the schema does.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch.dispatch import dispatch_item
from serving.servers.deps import AppServices
from serving.storage.utils import calculate_cost

pytestmark = pytest.mark.asyncio

MODEL = "target-model"
SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"enum": ["A", "B", "C", "D"], "type": "string"}},
    "additionalProperties": False,
}
BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Pick one."}],
    "response_format": {
        "type": "json_schema",
        "json_schema": {"name": "wmdp_answer", "schema": SCHEMA, "strict": True},
    },
}


class _Upstream(BaseAdapter):
    def __init__(self, *replies: str, pricing: dict[str, str] | None = None) -> None:
        config = ModelConfig(
            id=MODEL,
            name=MODEL,
            provider="openai",
            base_url="https://upstream.test",
            # The gateway's own server, so batch work may use it.
            deployment_type="self_hosted",
        )
        if pricing is not None:
            config.pricing = pricing
        super().__init__(config)
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        self.calls.append(messages)
        usage = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return self.format_response(content=self.replies.pop(0), model=MODEL, usage=usage)

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("a batch item must never stream")
        yield


def _setup(upstream: _Upstream) -> tuple[FastAPI, AppServices]:
    router = FixedRouter()
    router.register_route(MODEL, [(upstream, 1.0)])
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    op_store = MagicMock()
    op_store.increment_user_cost = AsyncMock()
    services = AppServices(router=router, log_store=log_store, operational_store=op_store)
    app = FastAPI()
    app.state.services = services
    return app, services


async def _run(upstream: _Upstream) -> tuple[Any, dict[str, Any]]:
    app, services = _setup(upstream)
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
        body=BODY,
        batch_id="batch-s",
        target="chat",
    )
    for _ in range(200):
        if services.log_store.log_request.await_count:
            return result, services.log_store.log_request.await_args.kwargs
        await asyncio.sleep(0.001)
    raise AssertionError("no request log row was written")


async def test_a_prose_reply_is_asked_again_and_the_json_one_returned():
    upstream = _Upstream("B. Eremophilane O-acetyltransferase", '```json\n{"answer": "B"}\n```')

    result, row = await _run(upstream)

    assert result.error is None, result
    assert json.loads(result.response["choices"][0]["message"]["content"]) == {"answer": "B"}
    assert len(upstream.calls) == 2
    assert upstream.calls[0][-1]["content"] == "Pick one."  # first attempt untouched
    assert "JSON Schema" in upstream.calls[1][-1]["content"]
    assert row["metadata"]["structured_output"] == {"attempts": 2, "conforms": True}


async def test_a_reply_that_never_conforms_is_still_a_reply():
    upstream = _Upstream("prose", "more prose", "still prose")

    result, row = await _run(upstream)

    assert result.error is None, result
    assert result.response["choices"][0]["message"]["content"] == "still prose"
    assert row["metadata"]["structured_output"]["conforms"] is False


async def test_every_upstream_call_is_billed_not_only_the_reply_returned():
    """Three generations are three charges' worth (H18): the retries used to be free.

    The row is logged with what was billed, so its cost is the charge; the
    client's copy keeps the usage of the reply it gets.
    """
    pricing = {"prompt": "1", "completion": "2"}
    upstream = _Upstream("prose", "more prose", "still prose", pricing=pricing)

    result, row = await _run(upstream)

    one_call = calculate_cost({"prompt_tokens": 10, "completion_tokens": 5}, pricing)
    assert len(upstream.calls) == 3
    assert result.cost_usd == pytest.approx(3 * one_call)
    assert (row["usage"]["prompt_tokens"], row["usage"]["completion_tokens"]) == (30, 15)
    assert row["usage"]["total_tokens"] == 45
    assert calculate_cost(row["usage"], row["pricing"]) == pytest.approx(result.cost_usd)
    assert result.response["usage"]["prompt_tokens"] == 10


async def test_summed_usage_keeps_a_total_only_when_every_call_reported_one():
    """A total some calls lacked would decide how reasoning is billed; cache stays unreported."""
    from serving.servers.routers.completions import _total_usage

    both = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    usage = _total_usage(
        [{**both, "reasoning_tokens": 3, "cache_read_tokens": 4}, {"prompt_tokens": 1}, None]
    )

    assert usage == {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "total_tokens": None,
        "reasoning_tokens": 3,
        "cache_read_tokens": 4,
    }
    assert _total_usage([both, both])["total_tokens"] == 30
    assert _total_usage([None, {}]) is None
