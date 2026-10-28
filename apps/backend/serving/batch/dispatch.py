"""Run one batch item through the handler of its target.

A batch item is served by the handler behind its endpoint, called directly --
the way ``responses.py`` and ``compat.py`` delegate to the chat handler -- so
routing, fallback, circuit breaking, model access, cost accounting and request
logging apply to it unchanged:

=================  ==========================================================
target             handler
=================  ==========================================================
``chat``           ``completions.chat_completions``
``embeddings``     ``embeddings.create_embeddings``
``messages``       ``anthropic_messages.anthropic_messages``
``responses``      ``responses.create_response``
=================  ==========================================================

What the HTTP stack provides around a handler is provided here instead:

* a request scope, opened as ``RequestIdMiddleware`` opens one, in a context of
  its own, so nothing leaks between items or into the worker;
* the caller role, published as ``verify_api_key`` publishes it, so the
  upstream key pool keeps keys reserved for a higher tier away from batch
  traffic exactly as it does from the owner's own requests;
* every argument FastAPI would inject. A handler's parameters default to
  ``Depends(...)``/``Header(...)`` markers, which are truthy objects, so an
  argument left out would silently reach the handler as a marker.
  :func:`call_handler` refuses any call that leaves one out;
* a ``Request`` for the handler to read: the item's body with ``stream`` forced
  off, a batch user agent, and no network peer. A batch item has none, and must
  never pass for a trusted or loopback caller: its client resolves to
  ``"unknown"``, unresolved, so its routing affinity follows the owner's key
  and nothing keyed on a client address applies to it;
* the serialization the route applies to the handler's return value, and the
  reading of a ``JSONResponse`` -- the Messages and Responses handlers answer
  errors with one rather than raising, and its status decides the outcome.

The handler's own billing point reports what it charged
(:mod:`serving.batch.attempts`); nothing here charges anything. The attempt's
``api_logs`` rows carry the batch, item and attempt.

Whether an item runs at all -- its owner re-authenticated, the owner's
concurrency slot held -- is the worker's decision (:mod:`serving.batch.scheduler`).
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import inspect
import json
import secrets
from dataclasses import dataclass, replace
from typing import Any

import anyio
from fastapi import HTTPException, Response, params as fastapi_params
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response as StarletteResponse

from serving.batch.attempts import observing
from serving.batch.targets import TARGET_PATHS, AdapterRequirement
from serving.exceptions import safe_detail_text, scrub_error_for_user
from serving.pricing import get_pricing_snapshot
from serving.schemas import ChatCompletionResponse, EmbeddingRequest
from serving.servers.auth import publish_caller_role
from serving.servers.deps import get_completions_logger, get_cost_tracker, get_pricing_lookup
from serving.storage.batch_store import (
    TARGET_CHAT,
    TARGET_EMBEDDINGS,
    TARGET_MESSAGES,
    TARGET_RESPONSES,
)
from serving.storage.utils import json_safe
from serving.surfaces.context import (
    UNTRACKED,
    BatchAttribution,
    ClientOrigin,
    ExecutionContext,
    read_only,
)
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import ClientIpInfo, preset_client_ip_info

logger = get_logger(__name__)

#: User agent of every request the batch worker runs, as ``api_logs`` records it.
BATCH_USER_AGENT = "hybridinference-batch/1.0"

# Statuses a handler answers with when an upstream call was made but its
# answer lost (a gateway timeout, a broken connection, an internal failure after
# sending): the attempt's cost is unknown, not zero.
_UNCERTAIN_STATUSES = frozenset({500, 502, 504})

# Statuses besides 5xx on which the same request may well succeed later (529:
# Anthropic's "overloaded").
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
    529: "service_unavailable",
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

# The FastAPI markers a handler parameter defaults to when FastAPI is meant to
# fill it in.
_INJECTED_DEFAULTS = (fastapi_params.Depends, fastapi_params.Param, fastapi_params.Body)


@dataclass(frozen=True)
class DispatchResult:
    """How one attempt at a batch item went.

    ``response`` is the target's response body on success. On failure,
    ``error`` is ``{"code", "message", "status"}`` (plus ``"anthropic"``, the
    Anthropic error object, for a Messages item) and ``retryable`` says whether
    the same request may succeed if sent again. ``request_id`` is the
    ``api_logs`` id of the request, once the handler got far enough to log it.
    ``cost_status`` is ``billed`` (``cost_usd`` was charged), ``not_billed`` or
    ``unknown``; ``dispatched`` whether the handler was entered at all.
    """

    status_code: int
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    retryable: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_status: str = "not_billed"
    dispatched: bool = True


def call_handler(handler: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call a FastAPI handler directly, refusing to leave any injected argument out.

    Raises:
        TypeError: A parameter that FastAPI would inject (one defaulting to a
            ``Depends``/``Header``/``Query``/``Body`` marker) was not supplied.
    """
    signature = inspect.signature(handler)
    bound = signature.bind_partial(*args, **kwargs)
    missing = [
        name
        for name, parameter in signature.parameters.items()
        if name not in bound.arguments and isinstance(parameter.default, _INJECTED_DEFAULTS)
    ]
    if missing:
        raise TypeError(
            f"{getattr(handler, '__name__', handler)!r} called without {', '.join(missing)}: "
            "a batch dispatch must supply every argument FastAPI would inject"
        )
    return handler(*args, **kwargs)


async def dispatch_item(
    *,
    app: Any,
    services: Any,
    user_ctx: dict[str, Any],
    body: dict[str, Any],
    batch_id: str,
    target: str = TARGET_CHAT,
    item_id: int | None = None,
    attempt: int | None = None,
    protocol_profile: str | None = None,
    timeout_s: float | None = None,
) -> DispatchResult:
    """Run one batch item's request and report how it went. Never raises.

    Args:
        app: The FastAPI application; the handlers read its services off it.
        services: The application's services.
        user_ctx: The owner's user context for this attempt, as
            :func:`serving.servers.auth.authenticate_stored_key` returned it.
        body: The item's request body for *target*.
        batch_id: The item's batch, recorded in ``api_logs.batch_job_id``.
        target: ``chat``, ``embeddings``, ``messages`` or ``responses``.
        item_id: The item, recorded in the log row's metadata.
        attempt: The attempt number, recorded likewise.
        protocol_profile: The ``anthropic-version`` of a Messages item.
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
            target=target,
            item_id=item_id,
            attempt=attempt,
            protocol_profile=protocol_profile,
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
    target: str,
    item_id: int | None,
    attempt: int | None,
    protocol_profile: str | None,
    timeout_s: float | None,
) -> DispatchResult:
    if not isinstance(body, dict):
        return replace(_failure(400, "The request body must be a JSON object."), dispatched=False)
    if target not in _CORE_EXECUTORS and target not in _HANDLER_EXECUTORS:
        return replace(_failure(400, f"Unsupported batch target {target!r}."), dispatched=False)
    request_body = {**body, "stream": False} if target != TARGET_EMBEDDINGS else dict(body)
    try:
        raw = json.dumps(request_body).encode()
    except (TypeError, ValueError):
        return replace(_failure(400, "The request body is not valid JSON."), dispatched=False)

    scope_request_id = secrets.token_hex(12)
    seed: dict[str, Any] = {
        "request_id": scope_request_id,
        "client_user_agent": BATCH_USER_AGENT,
        "pricing_time": dt.datetime.now(dt.UTC),
        "pricing_snapshot": get_pricing_snapshot(),
    }
    if target == TARGET_MESSAGES:
        seed[req_ctx.BATCH_EXECUTION] = AdapterRequirement(body=request_body)
        if protocol_profile:
            seed[req_ctx.ANTHROPIC_VERSION] = protocol_profile
    result: Any = None
    # The upstream may have run, and charged, without the handler reporting it:
    # an attempt abandoned at its deadline, or one that crashed.
    uncertain = False
    deadline = anyio.move_on_after(timeout_s)
    with observing() as observer:
        try:
            # The scope RequestIdMiddleware opens for an HTTP request.
            req_ctx.reset_request_scope(**seed)
            caller = publish_caller_role(
                {
                    **user_ctx,
                    "batch_job_id": batch_id,
                    "batch_item_id": item_id,
                    "batch_attempt": attempt,
                }
            )
            ctx = ExecutionContext(
                user_ctx=read_only(user_ctx),
                origin=ClientOrigin.for_batch(
                    path=TARGET_PATHS[target], ip=_BATCH_CLIENT, user_agent=BATCH_USER_AGENT
                ),
                # Not interactive traffic: the worker's own admission (the
                # owner's batch quota, the model's capacity, the lease) holds it.
                admission=UNTRACKED,
                batch=BatchAttribution(batch_id, item_id, attempt),
                timeout_probe=None,
            )
            core = _CORE_EXECUTORS.get(target)
            with deadline:
                if core is not None:
                    result = await core(services, ctx, request_body)
                else:
                    request = _request(
                        app, raw, scope_request_id, TARGET_PATHS[target], protocol_profile
                    )
                    result = await _HANDLER_EXECUTORS[target](
                        services, request, caller, request_body
                    )
        except HTTPException as exc:
            if deadline.cancel_called:
                outcome, uncertain = _TIMED_OUT, True
            else:
                outcome = _failure(exc.status_code, _detail_message(exc.detail, exc.status_code))
        except Exception as exc:
            uncertain = True
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
            if deadline.cancelled_caught:
                outcome, uncertain = _TIMED_OUT, True
            else:
                outcome = _completed(target, result)
        request_id = observer.request_id or _logged_request_id(scope_request_id)
        cost_usd: float | None = None
        if observer.billed:
            cost_status = "billed" if observer.cost_known else "unknown"
            cost_usd = observer.cost_usd
        elif uncertain or outcome.error is None or outcome.status_code in _UNCERTAIN_STATUSES:
            # Served (or perhaps served) but never priced by its billing point:
            # unknown, never zero. A gateway timeout or a broken connection
            # answered as 502/504 is the same: the upstream may have finished,
            # and charged, after the answer was lost.
            cost_status = "unknown"
        else:
            # The handler answered with an error; the surfaces bill only success.
            cost_status = "not_billed"
        # The target's own usage object (native shape) when the response has one.
        usage = outcome.usage or observer.usage
    prompt_tokens, completion_tokens = _usage_tokens(usage, outcome)
    return replace(
        outcome,
        cost_usd=cost_usd,
        cost_status=cost_status,
        request_id=request_id,
        usage=usage,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# -- executors: one per target, every injected argument supplied ----------------


async def _run_chat(services: Any, request: Request, caller: dict[str, Any], body: dict) -> Any:
    from serving.servers.routers.completions import chat_completions

    return await call_handler(
        chat_completions,
        request,
        Response(),
        authorization=None,
        user_ctx=caller,
        router_exec=services.router,
        log_store=getattr(services, "log_store", None),
        model_router_registry=getattr(services, "model_router_registry", None),
        model_visibility_resolver=getattr(services, "model_visibility_resolver", None),
        runtime_settings=None,
        completions_logger=await get_completions_logger(services),
        pricing_lookup=await get_pricing_lookup(services),
        cost_tracker=await get_cost_tracker(services),
        _concurrency_slot=None,
    )


async def _run_embeddings(services: Any, ctx: ExecutionContext, body: dict) -> Any:
    from serving.servers.routers.embeddings import EmbeddingServices, execute_embeddings

    try:
        payload = EmbeddingRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(400, "The request is not a valid embeddings request.") from exc
    return await execute_embeddings(
        payload,
        body,
        ctx,
        EmbeddingServices(
            embedding_adapters=getattr(services, "embedding_adapters", None) or {},
            # The access gate recorded on the router (refusing a model without
            # one) and its runtime overrides apply as on /v1/embeddings.
            router=services.router,
            visibility=getattr(services, "model_visibility_resolver", None),
            log_store=getattr(services, "log_store", None),
            completions_logger=await get_completions_logger(services),
            op_store=getattr(services, "operational_store", None),
            runtime_settings=None,
        ),
    )


async def _run_messages(services: Any, request: Request, caller: dict[str, Any], body: dict) -> Any:
    from serving.servers.routers.anthropic_messages import anthropic_messages

    return await call_handler(
        anthropic_messages,
        request,
        user_ctx=caller,
        router_exec=services.router,
        log_store=getattr(services, "log_store", None),
        op_store=getattr(services, "operational_store", None),
        model_visibility_resolver=getattr(services, "model_visibility_resolver", None),
        completions_logger=await get_completions_logger(services),
        _conc=None,
    )


async def _run_responses(
    services: Any, request: Request, caller: dict[str, Any], body: dict
) -> Any:
    from serving.servers.routers.responses import create_response

    return await call_handler(
        create_response,
        request,
        Response(),
        authorization=None,
        user_ctx=caller,
        router_exec=services.router,
        log_store=getattr(services, "log_store", None),
        model_router_registry=getattr(services, "model_router_registry", None),
        model_visibility_resolver=getattr(services, "model_visibility_resolver", None),
        completions_logger=await get_completions_logger(services),
        pricing_lookup=await get_pricing_lookup(services),
        cost_tracker=await get_cost_tracker(services),
        # Never stored: a batch Responses request is independent (see targets).
        response_store=None,
        _concurrency_slot=None,
    )


#: Targets run through their protocol's execution function, with no request.
_CORE_EXECUTORS = {
    TARGET_EMBEDDINGS: _run_embeddings,
}

#: Targets still run through their route's handler (transitional, issue #1690).
_HANDLER_EXECUTORS = {
    TARGET_CHAT: _run_chat,
    TARGET_MESSAGES: _run_messages,
    TARGET_RESPONSES: _run_responses,
}

#: The handler each handler-run target calls, for the tests that hold every
#: injected parameter of each to a supplied argument.
HANDLERS: dict[str, tuple[str, str]] = {
    TARGET_CHAT: ("serving.servers.routers.completions", "chat_completions"),
    TARGET_MESSAGES: ("serving.servers.routers.anthropic_messages", "anthropic_messages"),
    TARGET_RESPONSES: ("serving.servers.routers.responses", "create_response"),
}


def _request(
    app: Any, raw_body: bytes, request_id: str, path: str, anthropic_version: str | None
) -> Request:
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
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(raw_body)).encode()),
        (b"user-agent", BATCH_USER_AGENT.encode()),
    ]
    if anthropic_version:
        headers.append((b"anthropic-version", anthropic_version.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": None,
        "client": None,
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
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


def _failure(
    status_code: int, message: str, *, anthropic: dict[str, Any] | None = None
) -> DispatchResult:
    """Return a failed attempt with *status_code*, retryable when that status is."""
    error: dict[str, Any] = {
        "code": _error_code(status_code),
        "message": message,
        "status": status_code,
    }
    if anthropic is not None:
        error["anthropic"] = anthropic
    return DispatchResult(
        status_code=status_code,
        error=error,
        retryable=status_code >= 500 or status_code in _RETRYABLE_STATUSES,
    )


# An attempt abandoned at its deadline.
_TIMED_OUT = _failure(504, "The request did not complete in time.")


def _read_json_response(result: StarletteResponse) -> tuple[int, Any]:
    """The status and decoded JSON body of a ``JSONResponse`` a handler returned."""
    try:
        decoded = json.loads(bytes(result.body))
    except (ValueError, TypeError, AttributeError):
        decoded = None
    return int(result.status_code), decoded


def _completed(target: str, result: Any) -> DispatchResult:
    """Return the attempt that produced *result*, the handler's return value."""
    if isinstance(result, StarletteResponse):
        if not hasattr(result, "body"):
            # A streaming response: the batch forced stream off, so this is a
            # handler that did not honour it.
            return _failure(502, "The handler returned a stream for a batch request.")
        status, body = _read_json_response(result)
        if status >= 400 or not isinstance(body, dict):
            return _error_from_body(target, status if status >= 400 else 502, body)
        result = body
    if not isinstance(result, dict):
        return _failure(502, "The upstream response had an unexpected shape.")
    if target == TARGET_MESSAGES and result.get("type") == "error":
        return _error_from_body(target, 502, result)
    response = _client_body(result) if target == TARGET_CHAT else json_safe(result)
    usage = response.get("usage") if isinstance(response, dict) else None
    return DispatchResult(
        status_code=200,
        response=response,
        usage=usage if isinstance(usage, dict) else None,
    )


def _error_from_body(target: str, status: int, body: Any) -> DispatchResult:
    """A failed attempt from an error body a handler answered with."""
    message = None
    anthropic = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if target == TARGET_MESSAGES and isinstance(error.get("type"), str):
                anthropic = {"type": error["type"], "message": message or ""}
        elif isinstance(body.get("detail"), str):
            message = body["detail"]
    if not isinstance(message, str) or not message:
        message = safe_detail_text(None, status)
    return _failure(status, message, anthropic=anthropic)


def _usage_tokens(usage: Any, outcome: DispatchResult) -> tuple[int | None, int | None]:
    """Prompt and completion tokens from a usage object of any target's shape."""
    if not isinstance(usage, dict):
        return outcome.prompt_tokens, outcome.completion_tokens
    prompt = _token_count(usage.get("prompt_tokens"))
    if prompt is None:
        prompt = _token_count(usage.get("input_tokens"))
        if prompt is not None:
            # Anthropic counts cached input apart; the batch totals count it all.
            for extra in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                prompt += _token_count(usage.get(extra)) or 0
    completion = _token_count(usage.get("completion_tokens"))
    if completion is None:
        completion = _token_count(usage.get("output_tokens"))
    return prompt, completion


def _token_count(value: Any) -> int | None:
    """Return *value* as a token count, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
