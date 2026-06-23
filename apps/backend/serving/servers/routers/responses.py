"""OpenAI Responses API northbound router (``/v1/responses``).

The Responses API is a re-shaping of Chat Completions. Rather than duplicate
routing, fallback, cost accounting, DB logging and metrics, this router
translates a Responses request into a Chat Completions request and **delegates
to the existing ``chat_completions`` handler**, then translates the result back
into a Responses object. Field translation lives in
``serving.responses_translator``; this router owns:

  - auth, concurrency (shared dependencies)
  - request/response envelope translation + dispatch
  - streaming SSE re-framing (chat chunks → Responses events)
  - statefulness: ``store`` / ``previous_response_id`` / ``GET`` / ``DELETE``

Errors use the same ``{"error": {...}}`` envelope as Chat Completions, so the
global HTTP exception handler formats them correctly with no special-casing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from serving.responses_translator import (
    ResponsesStreamTranslator,
    assistant_message_from_chat,
    chat_response_to_responses,
    new_response_id,
    now_ts,
    responses_input_to_messages,
    responses_request_to_chat_params,
)
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_completions_logger,
    get_cost_tracker,
    get_log_store,
    get_model_router_registry,
    get_model_visibility_resolver,
    get_pricing_lookup,
    get_response_store,
    get_router,
)
from serving.servers.routers.completions import chat_completions
from serving.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()

# `no-transform` stops intermediary CDNs (e.g. Cloudflare) from buffering the
# stream to compress it, which collapses TTFT; `X-Accel-Buffering: no` disables
# nginx buffering.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Background persistence tasks for streaming responses (fire-and-forget); held
# so they aren't garbage-collected mid-flight.
_background_tasks: set[asyncio.Task[Any]] = set()


def _schedule(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _load_prior_messages(
    response_store: Any,
    previous_response_id: str,
    user_id: str | None,
) -> list[dict[str, Any]]:
    """Return the stored cumulative conversation for ``previous_response_id``.

    Raises 404 (Responses/OpenAI error envelope) when statefulness is
    unavailable, the prior response is unknown, or it belongs to another user.
    """
    if response_store is None:
        raise HTTPException(404, f"Previous response '{previous_response_id}' not found")
    row = await response_store.get(previous_response_id)
    if not row or row.get("user_id") != user_id:
        raise HTTPException(404, f"Previous response '{previous_response_id}' not found")
    msgs = row.get("messages")
    return msgs if isinstance(msgs, list) else []


@router.post("/v1/responses", response_model=None)
async def create_response(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    model_router_registry=Depends(get_model_router_registry),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    completions_logger=Depends(get_completions_logger),
    pricing_lookup=Depends(get_pricing_lookup),
    cost_tracker=Depends(get_cost_tracker),
    response_store=Depends(get_response_store),
    _concurrency_slot=Depends(enforce_user_concurrency),
):
    """Handle an OpenAI Responses API create request (stream or non-stream)."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON in request body") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")

    model = body.get("model")
    if not model:
        raise HTTPException(400, "Missing required parameter: 'model'")
    if "input" not in body and "previous_response_id" not in body:
        raise HTTPException(400, "Missing required parameter: 'input'")

    # Normalise the owner key so anonymous/auth-disabled callers share a stable,
    # non-None identity — never let a None user_id bypass ownership scoping.
    user_id = user_ctx.get("user_id") or "anonymous"
    previous_response_id = body.get("previous_response_id")
    prior_messages: list[dict[str, Any]] = []
    if previous_response_id:
        prior_messages = await _load_prior_messages(response_store, previous_response_id, user_id)

    instructions = body.get("instructions")
    # Conversation messages (no system/instructions message — that is per-turn
    # and prepended below, matching the Responses API instructions semantics).
    input_messages = responses_input_to_messages(body.get("input"))
    params, dropped_tools = responses_request_to_chat_params(body)
    if dropped_tools:
        logger.warning(
            "Dropped unsupported hosted tool types for Responses request: %s",
            sorted(set(dropped_tools)),
        )

    convo_messages = prior_messages + input_messages
    chat_messages: list[dict[str, Any]] = []
    if instructions:
        chat_messages.append({"role": "system", "content": instructions})
    chat_messages.extend(convo_messages)

    chat_body: dict[str, Any] = {"model": model, "messages": chat_messages, **params}

    # Overwrite the cached parsed body so the delegated handler validates and
    # logs the translated chat request (Starlette caches both `_json` and the
    # raw `_body`). This is the same delegation pattern used by compat.py.
    request._json = chat_body  # type: ignore[attr-defined]
    request._body = json.dumps(chat_body).encode()  # type: ignore[attr-defined]

    is_stream = bool(body.get("stream"))
    # store defaults to true; only an explicit ``false`` disables persistence
    # (missing or null → true). When no store is configured (privacy mode / no
    # DB) nothing is persisted, so the echoed value reflects that truthfully.
    store = body.get("store") is not False and response_store is not None
    response_id = new_response_id()
    created_at = now_ts()

    if is_stream:
        return await _stream_response(
            request=request,
            http_response=http_response,
            authorization=authorization,
            user_ctx=user_ctx,
            router_exec=router_exec,
            log_store=log_store,
            model_router_registry=model_router_registry,
            model_visibility_resolver=model_visibility_resolver,
            completions_logger=completions_logger,
            pricing_lookup=pricing_lookup,
            cost_tracker=cost_tracker,
            response_store=response_store,
            body=body,
            model=model,
            response_id=response_id,
            created_at=created_at,
            previous_response_id=previous_response_id,
            store=store,
            convo_messages=convo_messages,
            user_id=user_id,
        )

    # Non-streaming: delegate, then translate the chat completion to a Response.
    # `runtime_settings=None` keeps the delegate on its plain non-streaming path
    # (the force-streaming keepalive path returns a StreamingResponse).
    chat_result = await chat_completions(
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        router_exec=router_exec,
        log_store=log_store,
        model_router_registry=model_router_registry,
        model_visibility_resolver=model_visibility_resolver,
        runtime_settings=None,
        completions_logger=completions_logger,
        pricing_lookup=pricing_lookup,
        cost_tracker=cost_tracker,
    )
    if not isinstance(chat_result, dict):
        raise HTTPException(502, "Unexpected upstream response shape")

    responses_obj = chat_response_to_responses(
        chat_result,
        response_id=response_id,
        created_at=created_at,
        model=model,
        request_body=body,
        previous_response_id=previous_response_id,
        store=store,
    )

    if store and response_store is not None:
        assistant_msg = assistant_message_from_chat(chat_result)
        await _persist(
            response_store,
            response_id=response_id,
            user_id=user_id or "anonymous",
            response=responses_obj,
            messages=[*convo_messages, assistant_msg],
            previous_response_id=previous_response_id,
            model=model,
        )

    return JSONResponse(content=responses_obj)


async def _stream_response(
    *,
    request: Request,
    http_response: Response,
    authorization: str | None,
    user_ctx: dict,
    router_exec: Any,
    log_store: Any,
    model_router_registry: Any,
    model_visibility_resolver: Any,
    completions_logger: Any,
    pricing_lookup: Any,
    cost_tracker: Any,
    response_store: Any,
    body: dict[str, Any],
    model: str,
    response_id: str,
    created_at: int,
    previous_response_id: str | None,
    store: bool,
    convo_messages: list[dict[str, Any]],
    user_id: str | None,
) -> StreamingResponse:
    """Delegate streaming to ``chat_completions`` and re-frame chunks as events.

    Pre-stream validation errors (model not found, role gate, unsupported
    modality) propagate as ``HTTPException`` here — before any byte is sent — so
    the client gets a clean JSON error rather than a broken stream.
    """
    delegate = await chat_completions(
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        router_exec=router_exec,
        log_store=log_store,
        model_router_registry=model_router_registry,
        model_visibility_resolver=model_visibility_resolver,
        runtime_settings=None,
        completions_logger=completions_logger,
        pricing_lookup=pricing_lookup,
        cost_tracker=cost_tracker,
    )

    translator = ResponsesStreamTranslator(
        response_id=response_id,
        created_at=created_at,
        model=model,
        request_body=body,
        previous_response_id=previous_response_id,
        store=store,
    )

    # ``body_iterator`` is Starlette's StreamingResponse stream attribute — the
    # same access compat.py relies on to re-wrap a delegated streaming response.
    body_iterator = getattr(delegate, "body_iterator", None)

    def _persist_messages() -> list[dict[str, Any]]:
        assistant_msg = translator.assistant_message
        return [*convo_messages, assistant_msg] if assistant_msg else list(convo_messages)

    async def _gen() -> Any:
        persisted = False
        try:
            if body_iterator is not None:
                async for chunk in body_iterator:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", errors="replace")
                    for event in translator.feed(chunk):
                        yield event.encode()
            # Build the terminal events, then persist *before* emitting them so a
            # client that issues GET / previous_response_id immediately after
            # ``response.completed`` cannot race the store write and 404.
            closing = list(translator.finalize())
            if (
                store
                and response_store is not None
                and translator.final_response is not None
                and not translator.failed
            ):
                await _persist(
                    response_store,
                    response_id=response_id,
                    user_id=user_id or "anonymous",
                    response=translator.final_response,
                    messages=_persist_messages(),
                    previous_response_id=previous_response_id,
                    model=model,
                )
            persisted = True
            for event in closing:
                yield event.encode()
        finally:
            # Early client disconnect / cancellation interrupts the loop before
            # the synchronous persist above runs. Force-finalize and persist
            # best-effort (fire-and-forget — the client is gone) so the partial
            # turn is still stored for ``previous_response_id`` chaining, but
            # mark it ``incomplete`` so an aborted stream is never recorded as a
            # normal completion.
            if not persisted and store and response_store is not None and not translator.failed:
                if translator.final_response is None:
                    for _ in translator.finalize():
                        pass
                if translator.final_response is not None:
                    aborted = translator.final_response
                    aborted["status"] = "incomplete"
                    # finalize() already set incomplete_details (to None for a
                    # non-truncated finish), so setdefault would be a no-op —
                    # assign explicitly when it is unset.
                    if not aborted.get("incomplete_details"):
                        aborted["incomplete_details"] = {"reason": "interrupted"}
                    _schedule(
                        _persist(
                            response_store,
                            response_id=response_id,
                            user_id=user_id or "anonymous",
                            response=aborted,
                            messages=_persist_messages(),
                            previous_response_id=previous_response_id,
                            model=model,
                        )
                    )

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


async def _persist(
    response_store: Any,
    *,
    response_id: str,
    user_id: str,
    response: dict[str, Any],
    messages: list[dict[str, Any]],
    previous_response_id: str | None,
    model: str,
) -> None:
    """Persist a response; never let a storage error break the request."""
    try:
        await response_store.save(
            response_id=response_id,
            user_id=user_id,
            response=response,
            messages=messages,
            previous_response_id=previous_response_id,
            model=model,
        )
    except Exception:
        # Visible at default log level: the request still succeeded, but the
        # response was not stored (GET / previous_response_id will 404).
        logger.warning("Failed to persist response %s", response_id, exc_info=True)


@router.get("/v1/responses/{response_id}", response_model=None)
async def get_response(
    response_id: str,
    user_ctx: dict = Depends(verify_api_key),
    response_store=Depends(get_response_store),
):
    """Retrieve a previously stored response by id."""
    if response_store is None:
        raise HTTPException(404, f"Response '{response_id}' not found")
    row = await response_store.get(response_id)
    owner = user_ctx.get("user_id") or "anonymous"
    if not row or row.get("user_id") != owner:
        raise HTTPException(404, f"Response '{response_id}' not found")
    return JSONResponse(content=row.get("response") or {})


@router.delete("/v1/responses/{response_id}", response_model=None)
async def delete_response(
    response_id: str,
    user_ctx: dict = Depends(verify_api_key),
    response_store=Depends(get_response_store),
):
    """Delete a stored response by id."""
    if response_store is None:
        raise HTTPException(404, f"Response '{response_id}' not found")
    owner = user_ctx.get("user_id") or "anonymous"
    deleted = await response_store.delete(response_id, user_id=owner)
    if not deleted:
        raise HTTPException(404, f"Response '{response_id}' not found")
    return JSONResponse(content={"id": response_id, "object": "response.deleted", "deleted": True})
