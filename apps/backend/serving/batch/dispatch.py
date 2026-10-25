"""Run one batch item through the chat-completions handler.

A batch item is served by the handler behind ``POST /v1/chat/completions``,
called directly -- the way ``responses.py`` and ``compat.py`` delegate to it --
so routing, fallback, circuit breaking, model access, cost accounting and
request logging apply to it unchanged.

What the HTTP stack provides around that handler is provided here instead:

* a request scope, opened as ``RequestIdMiddleware`` opens one, in a context of
  its own, so nothing leaks between items or into the worker;
* the caller role, published as ``verify_api_key`` publishes it, so the
  upstream key pool keeps keys reserved for a higher tier away from batch
  traffic exactly as it does from the owner's own requests;
* a ``Request`` for the handler to read: the item's body with ``stream`` forced
  off, a batch user agent, and no network peer. A batch item has none, and must
  never pass for a trusted or loopback caller: its client resolves to
  ``"unknown"``, unresolved, so its routing affinity follows the owner's key
  and nothing keyed on a client address applies to it;
* the serialization the route applies to the handler's return value.

Whether an item runs at all -- its owner re-authenticated, the owner's
concurrency slot held -- is the worker's decision (:mod:`serving.batch.scheduler`).
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import secrets
from dataclasses import dataclass, replace
from typing import Any

import anyio
from fastapi import HTTPException, Response
from pydantic import ValidationError
from starlette.requests import Request

from serving.exceptions import safe_detail_text, scrub_error_for_user
from serving.pricing import get_pricing_snapshot
from serving.schemas import ChatCompletionResponse
from serving.servers.auth import publish_caller_role
from serving.servers.deps import get_completions_logger, get_cost_tracker, get_pricing_lookup
from serving.servers.routers.completions import chat_completions
from serving.storage.utils import json_safe
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import ClientIpInfo, preset_client_ip_info

logger = get_logger(__name__)

#: User agent of every request the batch worker runs, as ``api_logs`` records it.
BATCH_USER_AGENT = "hybridinference-batch/1.0"

_CHAT_PATH = "/v1/chat/completions"

# Statuses besides 5xx on which the same request may well succeed later.
_RETRYABLE_STATUSES = frozenset({408, 425, 429})

_ERROR_CODES: dict[int, str] = {
    400: "invalid_request",
    401: "forbidden",
    403: "forbidden",
    404: "model_not_found",
    408: "timeout",
    413: "invalid_request",
    422: "invalid_request",
    425: "rate_limited",
    429: "rate_limited",
    503: "service_unavailable",
    504: "timeout",
}

# Who a batch item's request comes from, as client-IP resolution
# (serving/utils/request_ip.py) answers it, preset as the resolver's answer for
# the request. No network peer: unknown and unresolved, never a
# loopback, private or trusted-proxy address. Supplied rather than resolved
# because resolving a request without a peer logs
# ``client_ip_resolution_unresolved``, the signal for degraded client
# provenance, and batch traffic would flood it with one line per item.
_BATCH_CLIENT = ClientIpInfo(
    client_ip="unknown",
    peer_ip="unknown",
    source="batch",
    trusted_proxy_headers=False,
    resolved=False,
)


@dataclass(frozen=True)
class DispatchResult:
    """How one attempt at a batch item went.

    ``response`` is the chat completion body on success. On failure, ``error``
    is ``{"code", "message", "status"}`` and ``retryable`` says whether the same
    request may succeed if sent again. ``request_id`` is the ``api_logs`` id of
    the request, once the handler got far enough to log it under one.
    """

    status_code: int
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    retryable: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    request_id: str | None = None


class _CostCapture:
    """The shared cost tracker, remembering what the increments it schedules cost.

    The handler charges the owner through ``schedule_increment``, which returns
    the routing info enriched with the computed ``upstream_cost_usd``; that is
    the item's cost. Everything else is the wrapped tracker's.
    """

    def __init__(self, tracker: Any) -> None:
        """Wrap *tracker*."""
        self._tracker = tracker
        self.cost_usd: float | None = None

    async def schedule_increment(self, **kwargs: Any) -> Any:
        """Charge as the wrapped tracker does, and keep the cost it computed."""
        routing = await self._tracker.schedule_increment(**kwargs)
        cost = getattr(routing, "upstream_cost_usd", None)
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + float(cost)
        return routing

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the wrapped tracker."""
        return getattr(self._tracker, name)


async def dispatch_item(
    *,
    app: Any,
    services: Any,
    user_ctx: dict[str, Any],
    body: dict[str, Any],
    batch_id: str,
    timeout_s: float | None = None,
) -> DispatchResult:
    """Run one batch item's chat request and report how it went. Never raises.

    Args:
        app: The FastAPI application; the handler reads its services off it.
        services: The application's services.
        user_ctx: The owner's user context for this attempt, as
            :func:`serving.servers.auth.authenticate_stored_key` returned it.
        body: The item's ``/v1/chat/completions`` request body.
        batch_id: The item's batch, recorded in ``api_logs.batch_job_id``.
        timeout_s: How long the attempt may run before it is abandoned as a
            timeout; ``None`` for no limit beyond the upstream timeouts.

    Returns:
        The outcome. Cancellation still propagates, as it must.
    """
    # A task of its own, in an empty context: the request scope it opens can
    # neither leak into the caller's context nor inherit anything from it.
    return await asyncio.create_task(
        _dispatch(
            app=app,
            services=services,
            user_ctx=user_ctx,
            body=body,
            batch_id=batch_id,
            timeout_s=timeout_s,
        ),
        name=f"batch_dispatch:{batch_id}",
        context=contextvars.Context(),
    )


async def _dispatch(
    *,
    app: Any,
    services: Any,
    user_ctx: dict[str, Any],
    body: dict[str, Any],
    batch_id: str,
    timeout_s: float | None,
) -> DispatchResult:
    if not isinstance(body, dict):
        return _failure(400, "The request body must be a JSON object.")
    try:
        raw = json.dumps({**body, "stream": False}).encode()
    except (TypeError, ValueError):
        return _failure(400, "The request body is not valid JSON.")

    scope_request_id = secrets.token_hex(12)
    cost: _CostCapture | None = None
    result: Any = None
    deadline = anyio.move_on_after(timeout_s)
    try:
        # The scope RequestIdMiddleware opens for an HTTP request.
        req_ctx.reset_request_scope(
            request_id=scope_request_id,
            client_user_agent=BATCH_USER_AGENT,
            pricing_time=dt.datetime.now(dt.UTC),
            pricing_snapshot=get_pricing_snapshot(),
        )
        caller = publish_caller_role({**user_ctx, "batch_job_id": batch_id})
        cost = _CostCapture(get_cost_tracker(services))
        with deadline:
            result = await chat_completions(
                _request(app, raw, scope_request_id),
                Response(),
                authorization=None,
                user_ctx=caller,
                router_exec=services.router,
                log_store=getattr(services, "log_store", None),
                model_router_registry=getattr(services, "model_router_registry", None),
                model_visibility_resolver=getattr(services, "model_visibility_resolver", None),
                runtime_settings=None,
                completions_logger=get_completions_logger(services),
                pricing_lookup=get_pricing_lookup(services),
                cost_tracker=cost,
            )
    except HTTPException as exc:
        outcome = (
            _TIMED_OUT
            if deadline.cancel_called
            else _failure(exc.status_code, _detail_message(exc.detail, exc.status_code))
        )
    except Exception as exc:
        if deadline.cancel_called:
            outcome = _TIMED_OUT
        else:
            logger.exception(
                "batch item dispatch failed unexpectedly (batch %s)",
                batch_id,
                extra={"event": "batch_dispatch_error", "error_type": type(exc).__name__},
            )
            outcome = _failure(500, scrub_error_for_user(exc, None, 500))
    else:
        outcome = _TIMED_OUT if deadline.cancelled_caught else _completed(result)
    return replace(
        outcome,
        cost_usd=cost.cost_usd if cost is not None else None,
        request_id=_logged_request_id(scope_request_id),
    )


def _request(app: Any, raw_body: bytes, request_id: str) -> Request:
    """Build the ``Request`` the handler reads: *raw_body*, from no network peer."""
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    state: dict[str, Any] = {"request_id": request_id}
    preset_client_ip_info(state, _BATCH_CLIENT)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": None,
        "client": None,
        "root_path": "",
        "path": _CHAT_PATH,
        "raw_path": _CHAT_PATH.encode(),
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw_body)).encode()),
            (b"user-agent", BATCH_USER_AGENT.encode()),
        ],
        "app": app,
        "state": state,
    }
    return Request(scope, receive)


def _client_body(result: dict[str, Any]) -> dict[str, Any]:
    """Return the body ``POST /v1/chat/completions`` would have sent for *result*.

    The route serializes the handler's return value through its response model
    (``ChatCompletionResponse``, ``exclude_none``); a direct call skips that,
    so it is applied here. A result the model rejects is kept as it came rather
    than dropped: the request was served, and billed.
    """
    try:
        return ChatCompletionResponse.model_validate(result).model_dump(
            mode="json", exclude_none=True
        )
    except ValidationError:
        logger.warning(
            "batch item response does not match the chat completion schema; stored as returned",
            extra={"event": "batch_response_unvalidated"},
        )
        return json_safe(result)


def _logged_request_id(scope_request_id: str) -> str | None:
    """Return the ``api_logs`` request id the handler published, if it got that far.

    The handler publishes its own id over the scope's once it has passed its
    early checks (unknown model, access, modality); a request refused before
    that has none to report.
    """
    published = req_ctx.get().get("request_id")
    if isinstance(published, str) and published != scope_request_id:
        return published
    return None


def _detail_message(detail: Any, status_code: int) -> str:
    """Return the message an ``HTTPException`` detail carries for the caller.

    The same text the route's error envelope would show: an ``{"error": {...}}``
    detail is passed through as the body, anything else goes through
    :func:`serving.exceptions.safe_detail_text`.
    """
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        message = detail["error"].get("message")
        if isinstance(message, str) and message:
            return message
    return safe_detail_text(detail, status_code)


def _error_code(status_code: int) -> str:
    """Return the batch error code for an HTTP status."""
    code = _ERROR_CODES.get(status_code)
    if code is not None:
        return code
    return "upstream_error" if status_code >= 500 else "invalid_request"


def _failure(status_code: int, message: str) -> DispatchResult:
    """Return a failed attempt with *status_code*, retryable when that status is."""
    return DispatchResult(
        status_code=status_code,
        error={"code": _error_code(status_code), "message": message, "status": status_code},
        retryable=status_code >= 500 or status_code in _RETRYABLE_STATUSES,
    )


# An attempt abandoned at its deadline.
_TIMED_OUT = _failure(504, "The request did not complete in time.")


def _completed(result: Any) -> DispatchResult:
    """Return the attempt that produced *result*, the handler's return value."""
    if not isinstance(result, dict):
        return _failure(502, "The upstream response had an unexpected shape.")
    response = _client_body(result)
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return DispatchResult(
        status_code=200,
        response=response,
        prompt_tokens=_token_count(usage.get("prompt_tokens")),
        completion_tokens=_token_count(usage.get("completion_tokens")),
    )


def _token_count(value: Any) -> int | None:
    """Return *value* as a token count, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
