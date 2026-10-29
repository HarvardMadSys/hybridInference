"""OpenAI Responses API northbound router (``/v1/responses``).

The Responses API is a re-shaping of Chat Completions. Rather than duplicate
routing, fallback, cost accounting, DB logging and metrics, this router
translates a Responses request into a Chat Completions request and **delegates
to the chat execution** (:func:`~serving.servers.routers.completions.execute_chat`,
called once, under the same execution context and admission), then translates
the result back into a Responses object. Field translation lives in
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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from serving.responses_translator import (
    ResponsesStreamTranslator,
    assistant_message_from_chat,
    chat_response_to_responses,
    merge_leading_system_messages,
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
from serving.servers.routers.completions import (
    ChatOptions,
    ChatResponse,
    ChatServices,
    ChatStream,
    execute_chat,
    release_unstarted_chat_streams,
)
from serving.servers.surface_http import client_origin, timeout_probe
from serving.surfaces.context import ExecutionContext, admission_from, read_only
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)
router = APIRouter()

# `no-transform` stops intermediary CDNs (e.g. Cloudflare) from buffering the
# stream to compress it, which collapses TTFT; `X-Accel-Buffering: no` asks a
# proxy not to buffer at all. The latter is an nginx-originated header name,
# kept because several proxies honour it — nothing in this deployment reads it
# now that no nginx sits in front, but it costs nothing and travels with the
# response wherever it is redeployed.
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


#: Dropped from what the chat execution sees. The Responses surface never
#: exposes reasoning text, but ``reasoning_content`` must survive into the
#: persisted assistant turn for ``previous_response_id`` chaining (some
#: providers require the reasoning history echoed back). A strict-mode header on
#: the inbound request would otherwise make chat strip reasoning before the
#: translator can capture it. Every entry, in any case, is removed.
_REASONING_PASSTHROUGH_HEADER = "x-reasoning-passthrough"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesServices:
    """What :func:`execute_responses` serves a request with."""

    chat: ChatServices
    #: Where responses are kept for GET and ``previous_response_id``; None
    #: stores nothing (and a batch item never has one).
    response_store: Any


@dataclass(frozen=True, slots=True)
class ResponsesResult:
    """A served non-streaming request: the Responses object."""

    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResponsesStream:
    """A streaming request: Responses SSE events, re-framed from the chat stream.

    The chat stream behind it is already primed (its uncommitted failures were
    raised before this existed). Terminal events are emitted only once the
    response is stored; an interrupted stream is stored as ``incomplete``.
    """

    events: AsyncIterator[bytes]
    #: The primed chat stream the events re-frame (None if chat answered unary).
    chat_stream: ChatStream | None = None


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
    _streams: list[ChatStream] = Depends(release_unstarted_chat_streams, scope="request"),
):
    """Handle an OpenAI Responses API create request (stream or non-stream)."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON in request body") from exc
    result = await execute_responses(
        body,
        ExecutionContext(
            user_ctx=read_only(user_ctx),
            origin=client_origin(request, authorization=authorization),
            # This route's own slot and tracker: the chat execution it delegates
            # to retargets the same tracker and takes no slot of its own.
            admission=admission_from(_concurrency_slot),
            batch=None,
            timeout_probe=timeout_probe(request),
        ),
        ResponsesServices(
            chat=ChatServices(
                router=router_exec,
                model_router_registry=model_router_registry,
                visibility=model_visibility_resolver,
                log_store=log_store,
                completions_logger=completions_logger,
                pricing_lookup=pricing_lookup,
                cost_tracker=cost_tracker,
                app_services=getattr(request.app.state, "services", None),
            ),
            response_store=response_store,
        ),
    )
    if isinstance(result, ResponsesStream):
        if result.chat_stream is not None:
            # Closed at the end of the request if the response never started.
            _streams.append(result.chat_stream)
        return StreamingResponse(
            result.events, media_type="text/event-stream", headers=_SSE_HEADERS
        )
    return JSONResponse(content=result.body)


async def execute_responses(
    body: Any, ctx: ExecutionContext, services: ResponsesServices
) -> ResponsesResult | ResponsesStream:
    """Serve one Responses request, online or as a batch item.

    Validates and translates it, then delegates once to the chat execution with
    ``ChatOptions.delegated()`` -- answered as requested, never force-streamed
    -- under this request's own context: its admission (so the chat retarget
    moves this request's count, and no second slot is taken), and its origin
    without ``X-Reasoning-Passthrough``. Routing, billing, logging and the
    routing observation all happen there, once. Raises ``HTTPException`` with
    the status the route answers with.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    response_store = services.response_store

    model = body.get("model")
    if not model:
        raise HTTPException(400, "Missing required parameter: 'model'")
    if "input" not in body and "previous_response_id" not in body:
        raise HTTPException(400, "Missing required parameter: 'input'")

    # Normalise the owner key so anonymous/auth-disabled callers share a stable,
    # non-None identity — never let a None user_id bypass ownership scoping.
    user_id = ctx.user_ctx.get("user_id") or "anonymous"
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
    # Collapse multiple system-role messages (top-level `instructions` +
    # a "developer" item folded to system within `input`, or a stray one
    # resent mid-transcript by clients that don't use previous_response_id)
    # into one leading system message — some backends (e.g. sglang) reject
    # a request with more than one, or one not in position 0.
    chat_messages = merge_leading_system_messages(chat_messages)

    chat_body: dict[str, Any] = {"model": model, "messages": chat_messages, **params}
    # Neither is a chat parameter, but the chat execution reads both to resolve
    # the session the client declared (serving/utils/session_identity.py) --
    # Codex uses ``client_metadata``, other clients ``metadata``. Translation
    # replaces the body chat sees, so without carrying them over a Responses
    # client's declaration would be dropped here and its requests would log
    # unlabelled. The chat request schema ignores extra fields, so nothing
    # else reads them and nothing reaches a provider.
    for field in ("metadata", "client_metadata"):
        if isinstance(body.get(field), dict):
            chat_body[field] = body[field]

    # Chat validates and logs the translated request (it is the body it is
    # given), and keeps reasoning_content available for persisted chaining
    # regardless of any strict-reasoning header on the inbound request.
    chat_ctx = replace(ctx, origin=ctx.origin.without(_REASONING_PASSTHROUGH_HEADER))

    is_stream = bool(body.get("stream"))
    # store defaults to true; only an explicit ``false`` disables persistence
    # (missing or null → true). Persistence also requires a write-enabled store
    # (privacy mode disables writes), so the echoed value reflects whether the
    # response is actually stored.
    store = (
        body.get("store") is not False
        and response_store is not None
        and getattr(response_store, "persist_enabled", True)
    )
    response_id = new_response_id()
    created_at = now_ts()

    # Never force-streamed: a non-streaming request gets a plain answer.
    outcome = await execute_chat(chat_body, chat_ctx, services.chat, ChatOptions.delegated())

    if is_stream:
        # Pre-stream failures (model not found, role gate, unsupported
        # modality, an upstream refusal before the first byte) propagate as
        # ``HTTPException`` here -- before any byte is sent -- so the client gets
        # a clean JSON error rather than a broken stream.
        frames = None
        chat_stream = None
        if isinstance(outcome, ChatStream):
            await outcome.prime()
            frames = outcome.frames()
            chat_stream = outcome
        return ResponsesStream(
            _response_events(
                frames,
                response_store=response_store,
                body=body,
                model=model,
                response_id=response_id,
                created_at=created_at,
                previous_response_id=previous_response_id,
                store=store,
                convo_messages=convo_messages,
                user_id=user_id,
            ),
            chat_stream=chat_stream,
        )

    # Non-streaming: translate the chat completion to a Response.
    chat_result = outcome.body if isinstance(outcome, ChatResponse) else outcome
    if isinstance(outcome, ChatStream):
        await outcome.aclose()
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

    return ResponsesResult(responses_obj)


async def _response_events(
    frames: AsyncIterator[Any] | None,
    *,
    response_store: Any,
    body: dict[str, Any],
    model: str,
    response_id: str,
    created_at: int,
    previous_response_id: str | None,
    store: bool,
    convo_messages: list[dict[str, Any]],
    user_id: str | None,
) -> AsyncIterator[bytes]:
    """Re-frame the chat stream's *frames* as Responses events, storing the turn."""
    translator = ResponsesStreamTranslator(
        response_id=response_id,
        created_at=created_at,
        model=model,
        request_body=body,
        previous_response_id=previous_response_id,
        store=store,
    )

    def _persist_messages() -> list[dict[str, Any]]:
        assistant_msg = translator.assistant_message
        return [*convo_messages, assistant_msg] if assistant_msg else list(convo_messages)

    persisted = False
    try:
        if frames is not None:
            async for chunk in frames:
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
