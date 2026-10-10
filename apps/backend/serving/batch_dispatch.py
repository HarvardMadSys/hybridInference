"""In-process execution of a single batch item.

A batch item is run by calling the existing ``chat_completions`` handler
directly -- the same delegation pattern ``responses.py`` and ``compat.py`` use
-- rather than making an HTTP call back into the gateway. That reuses routing,
fallback, circuit breaking, cost accounting, DB logging and concurrency
unchanged.

Because there is no inbound HTTP request, we synthesise one: a Starlette
``Request`` whose body is the item's chat payload and whose ``app`` is the live
FastAPI app (so any dependency lookup that reaches for ``app.state.services``
still resolves). Batch attribution rides on ``user_ctx['batch_job_id']``, which
``chat_completions`` folds into the ``api_logs`` metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import Response
from starlette.requests import Request

from serving.utils.logging import get_logger

logger = get_logger(__name__)

CHAT_PATH = "/v1/chat/completions"


@dataclass
class DispatchResult:
    """Outcome of one item execution."""

    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    provider: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def build_synthetic_request(app: Any, body: dict[str, Any]) -> Request:
    """Wrap a chat payload in a Request the delegated handler can consume."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": CHAT_PATH,
        "raw_path": CHAT_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
        "app": app,
    }
    request = Request(scope)
    request._body = json.dumps(body).encode()  # type: ignore[attr-defined]
    request._json = body  # type: ignore[attr-defined]
    return request


async def dispatch_chat_item(
    *,
    app: Any,
    services: Any,
    user_ctx: dict[str, Any],
    body: dict[str, Any],
) -> DispatchResult:
    """Run one chat payload through ``chat_completions`` and capture the result.

    Errors are returned, never raised: a single failing item must not abort the
    batch (partial success). ``HTTPException`` becomes ``{"message", ...}``;
    any other exception is logged and returned as a generic item error.
    """
    from serving.servers.routers.completions import chat_completions

    request = build_synthetic_request(app, body)
    http_response = Response()
    try:
        result = await chat_completions(
            request,
            http_response,
            authorization=None,
            user_ctx=user_ctx,
            router_exec=services.router,
            log_store=services.log_store,
            model_router_registry=services.model_router_registry,
            model_visibility_resolver=services.model_visibility_resolver,
            runtime_settings=None,
            completions_logger=_completions_logger(services),
            pricing_lookup=_pricing_lookup(services),
            cost_tracker=_cost_tracker(services),
        )
    except Exception as exc:
        return DispatchResult(error=_error_from_exception(exc))
    if not isinstance(result, dict):
        return DispatchResult(
            error={"message": "Unexpected upstream response shape", "type": "upstream_error"}
        )
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return DispatchResult(
        response=result,
        provider=http_response.headers.get("X-Provider"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


def _error_from_exception(exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if detail is not None:
        message = detail if isinstance(detail, str) else json.dumps(detail)
    else:
        message = str(exc) or exc.__class__.__name__
    error: dict[str, Any] = {"message": message, "type": exc.__class__.__name__}
    if status is not None:
        error["code"] = status
    if status is None:
        logger.exception("batch item dispatch failed")
    return error


def _completions_logger(services: Any) -> Any:
    if services.completions_logger is not None:
        return services.completions_logger
    from serving.servers.routers.completions_logging import CompletionsLogger

    services.completions_logger = CompletionsLogger(
        log_store=services.log_store,
        model_router_registry=services.model_router_registry,
    )
    return services.completions_logger


def _pricing_lookup(services: Any) -> Any:
    if services.pricing_lookup is not None:
        return services.pricing_lookup
    from serving.servers.routers.completions_cost import PricingLookup

    return PricingLookup(router=services.router)


def _cost_tracker(services: Any) -> Any:
    if services.cost_tracker is not None:
        return services.cost_tracker
    from serving.servers.routers.completions_cost import CostTracker

    return CostTracker(op_store=services.operational_store, pricing=_pricing_lookup(services))
