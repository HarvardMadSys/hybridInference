"""Run one batch item through the execution function of its target.

A batch item is served by the same protocol execution its online route calls
(issue #1690), so model access, routing (and the router's own fallback and
circuit breaking), cost accounting and request logging apply to it unchanged:

=================  ==========================================================
target             execution
=================  ==========================================================
``chat``           ``completions.execute_chat`` (``ChatOptions.batch()``)
``embeddings``     ``embeddings.execute_embeddings``
``messages``       ``anthropic_messages.execute_messages``
``responses``      ``responses.execute_responses`` (no response store)
=================  ==========================================================

No HTTP request is built and no route handler is called. What the HTTP stack
provides around a route is provided here instead:

* a request scope, opened as ``RequestIdMiddleware`` opens one, in a context of
  its own, so nothing leaks between items or into the worker;
* the caller role, published as ``verify_api_key`` publishes it, so the
  upstream key pool keeps keys reserved for a higher tier away from batch
  traffic exactly as it does from the owner's own requests;
* the execution context: the owner as authenticated for this attempt, an
  origin with no network peer (a batch item must never pass for a trusted or
  loopback caller: its client is ``"unknown"``, unresolved, so its routing
  affinity follows the owner's key), no interactive admission, and the batch
  attribution its ``api_logs`` rows carry;
* the item's body with ``stream`` forced off, and for a Messages item the
  constraints it was admitted under (served as written, on its model, under its
  stored ``anthropic-version``);
* the serialization the route applies to the execution's answer.

The execution's own billing point reports what it charged
(:mod:`serving.batch.attempts`); nothing here charges anything.

Whether an item runs at all -- its owner re-authenticated, the owner's
batch quota, the model's capacity and the lease held -- is the worker's
decision (:mod:`serving.batch.scheduler`).
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
from fastapi import HTTPException
from pydantic import ValidationError

from serving.batch.attempts import observing
from serving.batch.targets import TARGET_PATHS
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
from serving.utils.request_ip import ClientIpInfo

logger = get_logger(__name__)

#: User agent of every request the batch worker runs, as ``api_logs`` records it.
BATCH_USER_AGENT = "hybridinference-batch/1.0"

# Statuses an execution answers with when an upstream call was made but its
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
# (serving/utils/request_ip.py) would answer it. No network peer: unknown and
# unresolved, never a loopback, private or trusted-proxy address. Supplied
# rather than resolved because resolving a request without a peer logs
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

    ``response`` is the target's response body on success. On failure,
    ``error`` is ``{"code", "message", "status"}`` (plus ``"anthropic"``, the
    Anthropic error object, for a Messages item) and ``retryable`` says whether
    the same request may succeed if sent again. ``request_id`` is the
    ``api_logs`` id of the request, once the execution got far enough to log it.
    ``cost_status`` is ``billed`` (``cost_usd`` was charged), ``not_billed`` or
    ``unknown``; ``dispatched`` whether the execution was entered at all.
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
        app: The FastAPI application, whose services the routes read their
            auto-mode configuration and rejection-log destination from.
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
    executor = _EXECUTORS.get(target)
    if executor is None:
        return replace(_failure(400, f"Unsupported batch target {target!r}."), dispatched=False)
    request_body = {**body, "stream": False} if target != TARGET_EMBEDDINGS else dict(body)
    try:
        json.dumps(request_body)
    except (TypeError, ValueError):
        return replace(_failure(400, "The request body is not valid JSON."), dispatched=False)

    scope_request_id = secrets.token_hex(12)
    seed: dict[str, Any] = {
        "request_id": scope_request_id,
        "client_user_agent": BATCH_USER_AGENT,
        "pricing_time": dt.datetime.now(dt.UTC),
        "pricing_snapshot": get_pricing_snapshot(),
    }
    result: Any = None
    # The upstream may have run, and charged, without its billing point reporting it:
    # an attempt abandoned at its deadline, or one that crashed.
    uncertain = False
    deadline = anyio.move_on_after(timeout_s)
    with observing() as observer:
        try:
            # The scope RequestIdMiddleware opens for an HTTP request.
            req_ctx.reset_request_scope(**seed)
            publish_caller_role(user_ctx)
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
            with deadline:
                result = await executor(
                    services, ctx, request_body, app=app, protocol_profile=protocol_profile
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
            # The execution answered with an error; the surfaces bill only success.
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


# -- executors: one per target, each calling its protocol's execution -----------


async def _run_chat(
    services: Any, ctx: ExecutionContext, body: dict, *, app: Any, protocol_profile: str | None
) -> Any:
    from serving.servers.routers.completions import ChatOptions, ChatStream, execute_chat

    outcome = await execute_chat(
        body, ctx, await _chat_services(services, app), ChatOptions.batch()
    )
    if isinstance(outcome, ChatStream):
        # Never expected (stream is forced off); closed here, answered as a 502.
        await outcome.aclose()
    return outcome


async def _chat_services(services: Any, app: Any) -> Any:
    """The services the chat route would serve with, from the application's own."""
    from serving.servers.routers.completions import ChatServices

    return ChatServices(
        router=services.router,
        model_router_registry=getattr(services, "model_router_registry", None),
        visibility=getattr(services, "model_visibility_resolver", None),
        log_store=getattr(services, "log_store", None),
        completions_logger=await get_completions_logger(services),
        pricing_lookup=await get_pricing_lookup(services),
        cost_tracker=await get_cost_tracker(services),
        # The services the route reads off its application: auto-mode config.
        app_services=getattr(getattr(app, "state", None), "services", None),
    )


async def _run_embeddings(
    services: Any, ctx: ExecutionContext, body: dict, *, app: Any, protocol_profile: str | None
) -> Any:
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


async def _run_messages(
    services: Any, ctx: ExecutionContext, body: dict, *, app: Any, protocol_profile: str | None
) -> Any:
    from serving.servers.routers.anthropic_messages import (
        MessagesConstraints,
        MessagesServices,
        MessagesStream,
        execute_messages,
    )

    outcome = await execute_messages(
        body,
        ctx,
        MessagesServices(
            router=services.router,
            log_store=getattr(services, "log_store", None),
            op_store=getattr(services, "operational_store", None),
            visibility=getattr(services, "model_visibility_resolver", None),
            completions_logger=await get_completions_logger(services),
            # The services the Messages route reads off its application: the
            # auto-mode configuration and where a rejection row is written.
            app_services=getattr(getattr(app, "state", None), "services", None),
        ),
        # Served only by an adapter that takes the item as written, never
        # rerouted, under the anthropic-version the batch was created with.
        MessagesConstraints.for_batch_item(body, protocol_profile),
    )
    if isinstance(outcome, MessagesStream):
        # Never expected (stream is forced off); its health claim is released
        # here, in the item's own scope, and the attempt answered as a 502.
        await outcome.aclose()
    return outcome


async def _run_responses(
    services: Any, ctx: ExecutionContext, body: dict, *, app: Any, protocol_profile: str | None
) -> Any:
    from serving.servers.routers.responses import (
        ResponsesServices,
        ResponsesStream,
        execute_responses,
    )

    outcome = await execute_responses(
        body,
        ctx,
        ResponsesServices(
            chat=await _chat_services(services, app),
            # Never stored: a batch Responses request is independent (see targets).
            response_store=None,
        ),
    )
    if isinstance(outcome, ResponsesStream):
        # Never expected (stream is forced off); closed here, answered as a 502.
        await outcome.events.aclose()
    return outcome


#: Each target's executor: its protocol's execution function, with no request.
_EXECUTORS = {
    TARGET_CHAT: _run_chat,
    TARGET_EMBEDDINGS: _run_embeddings,
    TARGET_MESSAGES: _run_messages,
    TARGET_RESPONSES: _run_responses,
}


def _client_body(result: dict[str, Any]) -> dict[str, Any]:
    """Return the body ``POST /v1/chat/completions`` would have sent for *result*.

    The route serializes the execution's answer through its response model
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
    """Return the ``api_logs`` request id the execution published, if it got that far.

    The execution publishes its own id over the scope's once it has passed its
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


def _messages_completed(result: Any) -> tuple[int, Any] | DispatchResult:
    """``(status, body)`` of a Messages outcome, as the route would have sent it.

    The body goes through the JSON encoding the route's response applies, so
    the result stored is the one an online caller receives; a body that cannot
    be encoded raises, as rendering it would have.
    """
    from serving.servers.routers.anthropic_messages import (
        MessagesError,
        MessagesResponse,
        MessagesStream,
    )

    if isinstance(result, MessagesError):
        return result.status, result.body()
    if isinstance(result, MessagesResponse):
        encoded = json.dumps(
            result.body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        return 200, json.loads(encoded)
    if isinstance(result, MessagesStream):
        # The item forced stream off; a stream here never contacted the upstream.
        return _failure(502, "The handler returned a stream for a batch request.")
    return _failure(502, "The upstream response had an unexpected shape.")


def _chat_completed(result: Any) -> Any:
    """The body of a Chat outcome, or the failed attempt it is."""
    from serving.servers.routers.completions import ChatResponse, ChatStream

    if isinstance(result, ChatResponse):
        return result.body
    if isinstance(result, ChatStream):
        # The item forced stream off; a stream here was never read.
        return _failure(502, "The handler returned a stream for a batch request.")
    return result


def _responses_completed(result: Any) -> Any:
    """The body of a Responses outcome, or the failed attempt it is."""
    from serving.servers.routers.responses import ResponsesResult, ResponsesStream

    if isinstance(result, ResponsesResult):
        return result.body
    if isinstance(result, ResponsesStream):
        return _failure(502, "The handler returned a stream for a batch request.")
    return result


def _completed(target: str, result: Any) -> DispatchResult:
    """Return the attempt that produced *result*, the execution's return value."""
    if target == TARGET_RESPONSES:
        result = _responses_completed(result)
        if isinstance(result, DispatchResult):
            return result
    if target == TARGET_CHAT:
        result = _chat_completed(result)
        if isinstance(result, DispatchResult):
            return result
    if target == TARGET_MESSAGES:
        read = _messages_completed(result)
        if isinstance(read, DispatchResult):
            return read
        status, body = read
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
    """A failed attempt from an error body the execution answered with."""
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
