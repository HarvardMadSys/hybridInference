"""dispatch_item: one batch item through the real chat-completions handler.

The handler, the router and the cost tracker are the real ones; only the
upstream adapter is a stand-in, so these tests see what a batch item actually
gets: the request the handler reads, the scope it runs in, the cost it is
charged and the log row it leaves.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig, UsageInfo
from serving.batch.dispatch import BATCH_USER_AGENT, DispatchResult, dispatch_item
from serving.schemas import ChatCompletionResponse
from serving.servers.deps import AppServices
from serving.utils import context as req_ctx

MODEL = "batch-model"
BATCH_ID = "batch_0123456789abcdef"
PRICING = {
    "prompt": "1.00",
    "completion": "2.00",
    "input_cache_reads": "0",
    "input_cache_writes": "0",
    "image": "0",
    "request": "0",
}


class _StatusError(Exception):
    """An upstream error carrying an HTTP status, as SDK errors do."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream said {status_code}")
        self.status_code = status_code


class _Upstream(BaseAdapter):
    """Answers non-streaming calls, recording what each one saw."""

    def __init__(self, *, error: BaseException | None = None, delay: float = 0.0) -> None:
        super().__init__(
            ModelConfig(
                id=MODEL,
                name="Batch Model",
                provider="openai",
                base_url="https://upstream.test",
                pricing=PRICING,
            )
        )
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "params": params, "ctx": dict(req_ctx.get())})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.format_response(
            content="batch answer",
            model=MODEL,
            usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def stream_chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> Any:
        raise AssertionError("a batch item must never stream")
        yield  # pragma: no cover


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


def _owner(**overrides: Any) -> dict[str, Any]:
    ctx = {
        "user_id": "owner-1",
        "user_name": "Batch Owner",
        "role": "internal",
        "authenticated": True,
        "is_admin": False,
        "disabled_models": [],
        "max_concurrent_requests": None,
        "auth_key_hash": "owner-key-hash",
    }
    ctx.update(overrides)
    return ctx


def _body(**overrides: Any) -> dict[str, Any]:
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]}
    body.update(overrides)
    return body


async def _dispatch(app: FastAPI, services: AppServices, **overrides: Any) -> DispatchResult:
    kwargs: dict[str, Any] = {
        "app": app,
        "services": services,
        "user_ctx": _owner(),
        "body": _body(),
        "batch_id": BATCH_ID,
    }
    kwargs.update(overrides)
    return await dispatch_item(**kwargs)


async def _logged_row(services: AppServices) -> dict[str, Any]:
    """The api_logs row the handler scheduled (a background write)."""
    for _ in range(200):
        if services.log_store.log_request.await_count:
            return services.log_store.log_request.await_args.kwargs
        await asyncio.sleep(0.001)
    raise AssertionError("no request log row was written")


async def test_item_runs_through_the_handler_and_reports_its_response():
    upstream = _Upstream()
    app, services = _setup(upstream)

    result = await _dispatch(app, services, body=_body(stream=True))

    assert result.status_code == 200
    assert result.error is None
    assert result.retryable is False
    # The stored body asked to stream; the item ran as a plain completion.
    assert len(upstream.calls) == 1
    # Shaped as the route would have sent it: the response model, no internals.
    assert result.response == ChatCompletionResponse.model_validate(result.response).model_dump(
        mode="json", exclude_none=True
    )
    assert result.response["choices"][0]["message"]["content"] == "batch answer"
    assert "_routing" not in result.response
    assert result.response["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }
    assert (result.prompt_tokens, result.completion_tokens) == (100, 50)


async def test_cost_and_request_id_are_captured_and_logged_against_the_batch():
    app, services = _setup(_Upstream())

    result = await _dispatch(app, services)

    row = await _logged_row(services)
    # 100 prompt tokens at $1/M plus 50 completion tokens at $2/M.
    assert result.cost_usd == pytest.approx(0.0002)
    assert row["upstream_cost_usd"] == pytest.approx(result.cost_usd)
    assert result.request_id is not None
    assert result.request_id == row["request_id"]
    assert row["metadata"]["batch_job_id"] == BATCH_ID
    assert row["metadata"]["user_id"] == "owner-1"
    assert row["metadata"]["user_agent"] == BATCH_USER_AGENT
    assert row["status_code"] == 200
    for _ in range(100):
        if services.operational_store.increment_user_cost.await_count:
            break
        await asyncio.sleep(0.001)
    services.operational_store.increment_user_cost.assert_awaited_once_with(
        "owner-1", pytest.approx(0.0002)
    )


async def test_owner_role_is_published_in_a_scope_of_its_own():
    upstream = _Upstream()
    app, services = _setup(upstream)
    req_ctx.set({req_ctx.USER_ROLE: "admin", "request_id": "worker", "affinity_key": "other"})

    await _dispatch(app, services, user_ctx=_owner(role="pro"))

    seen = upstream.calls[0]["ctx"]
    assert seen[req_ctx.USER_ROLE] == "pro"
    assert seen["client_user_agent"] == BATCH_USER_AGENT
    assert seen[req_ctx.PRICING_TIME] is not None
    assert seen["affinity_key"] == "owner-key-hash"
    assert seen["user_id"] == "owner-1"
    # Nothing leaked back into the caller's context.
    assert req_ctx.get() == {
        req_ctx.USER_ROLE: "admin",
        "request_id": "worker",
        "affinity_key": "other",
    }


async def test_item_has_no_client_address_and_resolving_it_logs_nothing(caplog):
    app, services = _setup(_Upstream())
    caplog.set_level(logging.DEBUG, logger="serving.utils.request_ip")

    await _dispatch(app, services, user_ctx=_owner(auth_key_hash=None))

    row = await _logged_row(services)
    assert row["metadata"]["ip"] == "unknown"
    assert not [r for r in caplog.records if r.getMessage() == "client_ip_resolution_unresolved"]


async def test_anonymous_item_gets_no_sticky_affinity():
    upstream = _Upstream()
    app, services = _setup(upstream)
    anonymous = {"user_id": "anonymous", "role": "admin", "authenticated": False, "is_admin": True}

    result = await _dispatch(app, services, user_ctx=anonymous)

    assert result.status_code == 200
    assert upstream.calls[0]["ctx"]["affinity_key"] is None


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"model": MODEL}, 400, "invalid_request"),
        (
            {"model": "no-such-model", "messages": [{"role": "user", "content": "x"}]},
            404,
            "model_not_found",
        ),
    ],
)
async def test_caller_errors_are_final(body, status, code):
    upstream = _Upstream()
    app, services = _setup(upstream)

    result = await _dispatch(app, services, body=body)

    assert result.status_code == status
    assert result.retryable is False
    assert result.error["code"] == code
    assert result.error["status"] == status
    assert result.error["message"]
    assert result.response is None
    assert upstream.calls == []


async def test_a_model_the_owner_disabled_is_not_found():
    app, services = _setup(_Upstream())

    result = await _dispatch(app, services, user_ctx=_owner(disabled_models=[MODEL]))

    assert (result.status_code, result.error["code"], result.retryable) == (
        404,
        "model_not_found",
        False,
    )


async def test_a_body_that_is_not_an_object_is_final():
    app, services = _setup(_Upstream())

    result = await _dispatch(app, services, body=["not", "an", "object"])

    assert (result.status_code, result.error["code"], result.retryable) == (
        400,
        "invalid_request",
        False,
    )


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (_StatusError(503), 503, "service_unavailable"),
        (_StatusError(429), 429, "rate_limited"),
        (_StatusError(502), 502, "upstream_error"),
        (RuntimeError("connection reset"), 500, "upstream_error"),
    ],
)
async def test_upstream_failures_may_be_retried(error, status, code):
    app, services = _setup(_Upstream(error=error))

    result = await _dispatch(app, services)

    assert result.status_code == status
    assert result.retryable is True
    assert result.error["code"] == code
    assert result.error["status"] == status
    # The handler logged the failed request; its id comes back with the error.
    row = await _logged_row(services)
    assert result.request_id == row["request_id"]


async def test_an_unexpected_exception_is_reported_not_raised(monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("handler bug")

    from serving.servers.routers import completions

    monkeypatch.setattr(completions, "execute_chat", broken)
    app, services = _setup(_Upstream())

    result = await _dispatch(app, services)

    assert (result.status_code, result.error["code"], result.retryable) == (
        500,
        "upstream_error",
        True,
    )
    assert "handler bug" not in result.error["message"]


async def test_an_attempt_past_its_deadline_times_out():
    app, services = _setup(_Upstream(delay=5.0))

    result = await _dispatch(app, services, timeout_s=0.05)

    assert (result.status_code, result.error["code"], result.retryable) == (504, "timeout", True)


async def test_cancellation_propagates():
    upstream = _Upstream(delay=5.0)
    app, services = _setup(upstream)
    task = asyncio.create_task(_dispatch(app, services))
    for _ in range(100):
        if upstream.calls:
            break
        await asyncio.sleep(0.001)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
