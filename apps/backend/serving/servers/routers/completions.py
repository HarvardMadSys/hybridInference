"""OpenAI-compatible chat completions endpoint with routing and auth."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import conversation_fingerprint
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError, ProviderPinError, TargetUnavailableError
from serving.auto_mode import (
    AutoModelUnavailable,
    apply_to_chat_request,
    auto_mode_for,
    hide_served_model,
    openai_features,
    session_key,
)
from serving.config.runtime_settings import RuntimeSettings, get_runtime_settings
from serving.exceptions import scrub_error_for_user
from serving.grant_auth import ledger_attribution
from serving.model_access import ModelAccessDenial, model_access_denial, route_canonical_model_id
from serving.openai_chat_serializer import resolve_mode, sanitize_response
from serving.pricing import require_route_pricing
from serving.schemas import (
    CONTENT_BLOCK_MODALITY,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorResponse,
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
    get_router,
)
from serving.servers.model_availability import model_not_found_detail
from serving.servers.routers.completions_stream import StreamSession, ToolCallAccumulator
from serving.servers.routers.routing_info import (
    RoutingInfo,
    _publish_error_provider,
    _status_code_from_exception,
    build_initial_routing_info,
    merge_adapter_routing,
)
from serving.servers.streaming_state import STREAMING_RESPONSE_SCOPE_STATE_KEY
from serving.servers.surface_http import client_origin, timeout_probe
from serving.storage.utils import billable_output_tokens, json_safe
from serving.surfaces.context import ExecutionContext, admission_from, read_only
from serving.utils import context as req_ctx
from serving.utils.errors import format_exception_for_db
from serving.utils.logging import get_logger
from serving.utils.request_ip import derive_affinity_key
from serving.utils.session_identity import session_identity
from serving.utils.synthetic_probe import is_trusted_probe_headers
from serving.utils.token_utils import (
    normalize_usage,
    optional_token_count,
    sanitize_usage,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from serving.auto_mode import AutoSelection
    from serving.servers.routers.completions_cost import CostTracker, PricingLookup
    from serving.servers.routers.completions_logging import CompletionsLogger

logger = get_logger(__name__)
router = APIRouter()
_background_tasks: set[asyncio.Task[Any]] = set()


def _token_usage_is_sane(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int,
    total_tokens: int,
    max_tokens_cap: int = 10_000_000,
) -> bool:
    if not (
        0 <= prompt_tokens < max_tokens_cap
        and 0 <= completion_tokens < max_tokens_cap
        and 0 <= reasoning_tokens < max_tokens_cap
        and 0 <= total_tokens < max_tokens_cap
    ):
        return False

    output_tokens = billable_output_tokens(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )
    return total_tokens >= prompt_tokens + output_tokens


def _find_unsupported_modality(
    messages: list[dict[str, Any]], model_modalities: list[str] | None
) -> str | None:
    """Return the first input modality a message requires but the model lacks.

    Scans structured ``content`` blocks across messages. Returns the modality
    name (e.g. ``"image"`` or ``"audio"``) of the first block whose modality is
    not in ``model_modalities``, or ``None`` when every block is supported.
    """
    supported = set(model_modalities or [])
    for msg in messages:
        content = msg.get("content")
        # `content` may be a list of blocks (multimodal) or a single block
        # mapping; a bare dict must not bypass the modality gate.
        if isinstance(content, dict):
            blocks: list[Any] = [content]
        elif isinstance(content, list):
            blocks = content
        else:
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            required = CONTENT_BLOCK_MODALITY.get(block.get("type"))
            if required and required not in supported:
                return required
    return None


def _required_modalities(messages: list[dict[str, Any]]) -> frozenset[str]:
    """Return the set of non-text input modalities the messages require.

    Passed to the router so media is dispatched only to routes that declare the
    modality, never to a text-only fallback of an otherwise-capable model.
    """
    required: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, dict):
            blocks: list[Any] = [content]
        elif isinstance(content, list):
            blocks = content
        else:
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            modality = CONTENT_BLOCK_MODALITY.get(block.get("type"))
            if modality and modality != "text":
                required.add(modality)
    return frozenset(required)


async def _should_force_chat_completions_streaming(
    runtime_settings: RuntimeSettings | None,
    requested_stream: bool,
) -> bool:
    if requested_stream or runtime_settings is None:
        return False
    if not hasattr(runtime_settings, "get_bool"):
        return False
    try:
        return await runtime_settings.get_bool("force_chat_completions_streaming")
    except KeyError:
        return False


_FORCE_STREAMING_KEEPALIVE_S = 15


async def _close_stream_quietly(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if not callable(aclose):
        return
    try:
        with anyio.CancelScope(shield=True):
            await aclose()
    except (Exception, asyncio.CancelledError):
        pass


def _pre_commit_error(chunk: str | None) -> tuple[int, str | None] | None:
    """Return ``(status, message)`` when ``chunk`` is an SSE error frame.

    Applied only to a stream's first item, and only while ``StreamSession`` has
    not committed: there an error frame is a failure that happened before a byte
    was sent (see ``StreamSession.stream``). ``code`` is the status the session
    extracted from the upstream exception; anything that is not an HTTP error
    status becomes a 500, the status that frame would otherwise have carried.
    ``message`` is already scrubbed for the user by the session.
    """
    if not chunk or not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
        return None
    try:
        payload = json.loads(chunk[6:])
    except json.JSONDecodeError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    is_status = isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599
    message = error.get("message")
    return (
        code if is_status else 500,
        message if isinstance(message, str) and message.strip() else None,
    )


async def _chain_first_then_rest(first: bytes | str | None, stream: Any) -> Any:
    try:
        if first is not None:
            yield first
        async for data in stream:
            yield data
    finally:
        await _close_stream_quietly(stream)


async def _streaming_response_with_keepalive(
    stream_chunks: Any,
    *,
    request_id: str,
    model: str,
    request_headers: Any,
) -> Any:
    """Buffer a streaming response while yielding whitespace keepalive bytes.

    Keeps intermediate proxies (e.g. Cloudflare) from timing out by
    yielding ``b" "`` every ``_FORCE_STREAMING_KEEPALIVE_S`` seconds while
    accumulating SSE chunks, then yields the final ``application/json`` body.
    The leading whitespace before the JSON object is harmless — all standard
    JSON parsers ignore it.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls = ToolCallAccumulator()
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    response_id = request_id
    created = int(time.time())

    last_client_byte = time.monotonic()
    yielded_any = False
    chunk_queue: Any = asyncio.Queue()

    async def _reader() -> None:
        try:
            async for item in stream_chunks:
                await chunk_queue.put(item)
        except Exception as exc:
            await chunk_queue.put(exc)
        finally:
            await chunk_queue.put(None)

    reader_task = asyncio.create_task(_reader())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    chunk_queue.get(), timeout=_FORCE_STREAMING_KEEPALIVE_S
                )
            except TimeoutError:
                yield b" "
                yielded_any = True
                last_client_byte = time.monotonic()
                continue

            if chunk is None:
                break
            if isinstance(chunk, Exception):
                # Handle BEFORE the idle-keepalive emission below: yielding a
                # byte first would commit a 200 only to abort it immediately.
                if yielded_any:
                    # Bytes already committed a 200 -- emit the error envelope
                    # as the JSON body instead of aborting mid-body.
                    logger.error(
                        f"Upstream exception in stream: {chunk!r}",
                        extra={"request_id": request_id},
                    )
                    detail = scrub_error_for_user(None, request_id, 500)
                    yield json.dumps({"error": {"message": detail, "code": 500}}).encode()
                    return
                raise chunk

            chunk_json = None
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    chunk_json = json.loads(chunk[6:])
                except json.JSONDecodeError:
                    chunk_json = None

            # StreamSession.stream converts adapter exceptions into in-band
            # error frames, so like the Exception guard above they must be
            # handled BEFORE the idle-keepalive emission below -- a keepalive
            # byte would commit a 200 and downgrade the clean HTTPException
            # (real status code) into a 200-with-error-body.
            error = chunk_json.get("error") if isinstance(chunk_json, dict) else None
            if isinstance(error, dict):
                code = error.get("code")
                status_code = code if isinstance(code, int) else 500
                logger.error(f"Upstream error in stream: {error}", extra={"request_id": request_id})
                detail = scrub_error_for_user(None, request_id, status_code)
                if yielded_any:
                    # A keepalive byte already committed a 200 response --
                    # raising now would abort the connection mid-body with no
                    # error payload. Emit the error envelope as the JSON body
                    # instead (parsers ignore the leading whitespace).
                    yield json.dumps({"error": {"message": detail, "code": status_code}}).encode()
                    return
                raise HTTPException(status_code=status_code, detail=detail)

            # Inner traffic (buffered content chunks, SSE keepalive comments)
            # re-arms the wait_for timer above without sending the client a
            # single byte -- the JSON body is only emitted at the end. Track
            # the last client-visible byte ourselves and emit a keepalive
            # whenever the client has been idle a full interval, or proxies
            # (e.g. Cloudflare) time the connection out mid-generation.
            if time.monotonic() - last_client_byte >= _FORCE_STREAMING_KEEPALIVE_S:
                yield b" "
                yielded_any = True
                last_client_byte = time.monotonic()

            if chunk_json is None:
                continue

            response_id = chunk_json.get("id") or response_id
            created = int(chunk_json.get("created") or created)
            if chunk_json.get("usage"):
                raw_usage = chunk_json.get("usage")
                normalized_usage = normalize_usage(raw_usage)
                usage = normalized_usage if normalized_usage is not None else raw_usage

            choices = chunk_json.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                content_parts.append(content)
            reasoning = (
                delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            )
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
            if delta.get("tool_calls"):
                tool_calls.add(delta["tool_calls"])
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        await _close_stream_quietly(stream_chunks)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls.to_list()

    response: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }
        ],
    }
    if usage:
        # The client's body only (billing ran in the stream session): give it the
        # totals the response model requires.
        response["usage"] = sanitize_usage(usage, complete=True)
    sanitized = sanitize_response(response, resolve_mode(request_headers)).response_json
    yield json.dumps(sanitized).encode()


def _fallback_error_summary(routing: RoutingInfo) -> str | None:
    failed_attempts = routing.extra.get("failed_attempts")
    if not isinstance(failed_attempts, list) or not failed_attempts:
        return None
    first = failed_attempts[0]
    if not isinstance(first, dict):
        return None
    provider = first.get("endpoint_id") or first.get("provider") or "upstream"
    error_type = first.get("error_type") or "error"
    error = first.get("error") or "unknown"
    return f"Upstream fallback after {provider}: {error_type}: {error}"


def _metadata_with_fallback_diagnostic(
    metadata: dict[str, Any],
    routing: RoutingInfo,
) -> dict[str, Any]:
    fallback_error = _fallback_error_summary(routing)
    if fallback_error is None:
        return metadata
    return {**metadata, "upstream_error": fallback_error}


# --- Protocol execution, shared with Responses, compat and Batch (issue #1690) --


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatOptions:
    """How a caller of :func:`execute_chat` has the response delivered.

    ``runtime_settings`` decides two things only: whether a non-streaming
    request is streamed upstream and buffered (``force_chat_completions_streaming``),
    and whether a trusted probe is logged (``log_synthetic_probes``). Without
    settings neither applies. Nothing here touches access, billing or routing.
    """

    runtime_settings: RuntimeSettings | None

    @classmethod
    def online(cls, runtime_settings: RuntimeSettings | None) -> ChatOptions:
        """``/v1/chat/completions`` and the compatibility routes."""
        return cls(runtime_settings=runtime_settings)

    @classmethod
    def delegated(cls) -> ChatOptions:
        """The Responses API: answered as requested, never force-streamed."""
        return cls(runtime_settings=None)

    @classmethod
    def batch(cls) -> ChatOptions:
        """A batch item: never streamed."""
        return cls(runtime_settings=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatServices:
    """What :func:`execute_chat` serves a request with."""

    #: The routing manager; a pinned request goes to it directly.
    router: Any
    #: Per-model routers (Fixed, RouteWise, ...); None routes everything via ``router``.
    model_router_registry: Any
    visibility: Any
    log_store: Any
    completions_logger: CompletionsLogger
    pricing_lookup: PricingLookup
    cost_tracker: CostTracker
    #: The application's services, for the auto-mode configuration.
    app_services: Any


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A served non-streaming request."""

    #: The response as the route returns it (before its response model).
    body: Any
    #: ``X-Provider`` for a trusted probe, else None.
    provider_header: str | None


class ChatStream:
    """A streaming request past every check, its upstream not yet read.

    The router's stream has been created but not iterated, and the session that
    turns it into SSE frames (billing, logging and routing observation at its
    end) is built. :meth:`prime` reads the first item -- before a response is
    started, as the SSE route always has -- and raises ``HTTPException`` when
    that item is a failure nothing was committed for; :meth:`frames` then yields
    everything, unbuffered. A forced (buffered) stream is rendered by the HTTP
    route from :meth:`session_stream` instead.
    """

    __slots__ = (
        "_first",
        "_sse",
        "adapter_chunks",
        "buffered",
        "model",
        "provider_header",
        "request_headers",
        "request_id",
        "session",
    )

    def __init__(
        self,
        *,
        session: StreamSession,
        adapter_chunks: Any,
        buffered: bool,
        request_id: str,
        model: str,
        request_headers: Mapping[str, str],
        provider_header: str | None,
    ) -> None:
        """Hold the session and the router stream it reads."""
        self.session = session
        self.adapter_chunks = adapter_chunks
        self.buffered = buffered
        self.request_id = request_id
        self.model = model
        self.request_headers = request_headers
        self.provider_header = provider_header
        self._sse: Any = None
        self._first: str | None = None

    async def prime(self) -> str | None:
        """Read the first SSE item; refuse with its status if it is an uncommitted failure.

        A 200 is committed with the first byte, and everything the stream could
        say after it arrives in-band: a failure becomes an error frame and a
        close with no [DONE], which a client that is itself a gateway (every
        `kind: staging` / `openai_compat` hop) reads as a truncated generation
        and scores against this endpoint. StreamSession holds its bytes back
        until it has something to send, so a failure that came back first -- a
        4xx the upstream answered in milliseconds, every circuit open -- is the
        first item, with nothing committed yet, and the client can be told the
        real status instead.
        """
        self._sse = self.session.stream(self.adapter_chunks)
        first_sse: str | None
        try:
            first_sse = await self._sse.__anext__()
        except StopAsyncIteration:
            first_sse = None
        pre_commit_error = (
            None if self.session.yielded_first_chunk else _pre_commit_error(first_sse)
        )
        if pre_commit_error is not None:
            await _close_stream_quietly(self._sse)
            # This now relays the upstream's status to the client, so attribute
            # it, as the buffered path does: a relayed 401 with no label reads as
            # the gateway's own routine auth churn to RequestLogMiddleware and the
            # failed-request rule. The session only records the label.
            req_ctx.publish_upstream_provider(self.session.error_provider)
            status_code, message = pre_commit_error
            raise HTTPException(
                status_code=status_code,
                detail=message or scrub_error_for_user(None, self.request_id, status_code),
            )
        self._first = first_sse
        return first_sse

    def frames(self) -> AsyncIterator[str]:
        """The primed first item, then the rest of the stream; closed when done."""
        return _chain_first_then_rest(self._first, self._sse)

    def session_stream(self) -> AsyncIterator[str]:
        """The session's SSE items from the start, for a forced (buffered) stream."""
        return self.session.stream(self.adapter_chunks)

    async def aclose(self) -> None:
        """Close whatever has been started."""
        await _close_stream_quietly(self._sse if self._sse is not None else self.adapter_chunks)


ChatOutcome = ChatResponse | ChatStream


async def render_chat_http(outcome: ChatOutcome, request: Request, http_response: Response) -> Any:
    """Return what the Chat route answers *outcome* with.

    A streaming outcome is primed before the response starts (an uncommitted
    failure raises ``HTTPException``); a forced stream is buffered behind
    whitespace keepalives and answered as one JSON body.
    """
    if isinstance(outcome, ChatResponse):
        if outcome.provider_header is not None:
            http_response.headers["X-Provider"] = outcome.provider_header
        return outcome.body
    return await _render_chat_stream(outcome, request, http_response)


async def _render_chat_stream(
    stream: ChatStream, request: Request, http_response: Response
) -> StreamingResponse:
    if stream.buffered:
        keepalive_gen = _streaming_response_with_keepalive(
            stream.session_stream(),
            request_id=stream.request_id,
            model=stream.model,
            request_headers=stream.request_headers,
        )
        first_chunk: bytes | None = None
        try:
            first_chunk = await keepalive_gen.__anext__()
        except StopAsyncIteration:
            first_chunk = None
        except HTTPException:
            # A pre-first-byte upstream error on this path is relayed to the
            # client with the upstream's own status: the stream converts it to
            # an in-band error frame and the buffering wrapper re-raises that
            # frame's ``code`` as a fresh HTTPException carrying no
            # ``_routing``. So the handler's own attribution below resolves to
            # the "router" sentinel and publishes nothing, and a relayed 401
            # arrives at RequestLogMiddleware and the failed-request rule as a
            # bare, unattributed 401 — filed as routine client-auth churn, the
            # exact silence this alerting exists to break. The label has to be
            # republished *here*, in the request's own context: the stream
            # generator runs inside the wrapper's reader task, so its own
            # publish landed in a context copy that ends with the task.
            req_ctx.publish_upstream_provider(stream.session.error_provider)
            raise
        if stream.provider_header:
            http_response.headers["X-Provider"] = stream.provider_header

        # This path emits keepalive whitespace to keep intermediaries alive;
        # without no-transform / no-buffering a CDN (e.g. Cloudflare) can
        # buffer it and defeat the keepalive on long completions.
        request.scope.setdefault("state", {})[STREAMING_RESPONSE_SCOPE_STATE_KEY] = True
        streaming_headers: dict[str, str] = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        }
        if stream.provider_header:
            streaming_headers["X-Provider"] = stream.provider_header

        return StreamingResponse(
            _chain_first_then_rest(first_chunk, keepalive_gen),
            media_type="application/json",
            headers=streaming_headers or None,
        )

    await stream.prime()
    # `no-transform` stops intermediary CDNs (e.g. Cloudflare) from buffering
    # the stream to compress it, which collapses TTFT to total latency.
    response_headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    if stream.provider_header:
        response_headers["X-Provider"] = stream.provider_header
    return StreamingResponse(
        stream.frames(),
        media_type="text/event-stream",
        headers=response_headers,
    )


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    response_model_exclude_none=True,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "Model Not Found"},
        429: {"model": ErrorResponse, "description": "Too Many Requests"},
        500: {"model": ErrorResponse, "description": "Server Error"},
        503: {"model": ErrorResponse, "description": "Service Unavailable"},
    },
)
async def chat_completions(
    request: Request,
    http_response: Response,
    authorization: str | None = Header(None),
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    model_router_registry=Depends(get_model_router_registry),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    runtime_settings: RuntimeSettings | None = Depends(get_runtime_settings),
    completions_logger: CompletionsLogger = Depends(get_completions_logger),
    pricing_lookup: PricingLookup = Depends(get_pricing_lookup),
    cost_tracker: CostTracker = Depends(get_cost_tracker),
    _concurrency_slot=Depends(enforce_user_concurrency),
) -> dict[str, Any]:
    """Handle chat completion requests with routing and fallback.

    Reads the request and serves it through :func:`execute_chat`, which the
    Responses API, the compatibility routes and the batch worker call too.
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, "Invalid JSON or schema in request body") from e
    return await serve_chat_http(
        body,
        request,
        http_response,
        authorization=authorization,
        user_ctx=user_ctx,
        concurrency_slot=_concurrency_slot,
        services=ChatServices(
            router=router_exec,
            model_router_registry=model_router_registry,
            visibility=model_visibility_resolver,
            log_store=log_store,
            completions_logger=completions_logger,
            pricing_lookup=pricing_lookup,
            cost_tracker=cost_tracker,
            app_services=getattr(request.app.state, "services", None),
        ),
        runtime_settings=runtime_settings,
    )


async def serve_chat_http(
    body: Any,
    request: Request,
    http_response: Response,
    *,
    authorization: str | None,
    user_ctx: Mapping[str, Any],
    concurrency_slot: object,
    services: ChatServices,
    runtime_settings: RuntimeSettings | None,
) -> Any:
    """Serve a decoded chat request for an HTTP route and render the answer.

    For ``/v1/chat/completions`` and the compatibility routes, each under its
    own concurrency dependency: *concurrency_slot* is what it yielded.
    """
    outcome = await execute_chat(
        body,
        ExecutionContext(
            user_ctx=read_only(user_ctx),
            origin=client_origin(request, authorization=authorization),
            admission=admission_from(concurrency_slot),
            batch=None,
            timeout_probe=timeout_probe(request),
        ),
        services,
        ChatOptions.online(runtime_settings),
    )
    return await render_chat_http(outcome, request, http_response)


async def execute_chat(
    body: Any,
    ctx: ExecutionContext,
    services: ChatServices,
    options: ChatOptions,
) -> ChatOutcome:
    """Serve one chat completion request: online, delegated, or as a batch item.

    *body* is the decoded request body; it is validated here, so a caller that
    translated it (the Responses API) gets the same 400 for an invalid one.
    Routes through the model's own router (FixedRouter, RouteWise, ...), whose
    selection, retries, fallback and health this does not touch; bills, logs
    and reports the routing observation of a non-streaming request within the
    call. A streaming request is returned as a :class:`ChatStream`, not yet
    read. Errors are raised as ``HTTPException`` with the status and message
    the route answers with.
    """
    user_ctx = ctx.user_ctx
    router_exec = services.router
    log_store = services.log_store
    model_router_registry = services.model_router_registry
    model_visibility_resolver = services.visibility
    runtime_settings = options.runtime_settings
    completions_logger = services.completions_logger
    pricing_lookup = services.pricing_lookup
    cost_tracker = services.cost_tracker
    try:
        payload = ChatCompletionRequest.model_validate(body)
    except Exception as e:
        raise HTTPException(400, "Invalid JSON or schema in request body") from e

    # The probe marker is honoured only from a caller who cannot abuse it: an
    # authenticated internal/admin key, never an agent-sandbox credential.
    # An untrusted caller's header is simply ignored — the request logs,
    # records routing observations and gets no X-Provider header, like any
    # other traffic. See serving/utils/synthetic_probe.py.
    is_synthetic_probe = is_trusted_probe_headers(ctx.origin.headers, user_ctx)
    # Published immediately, before any of the early 404/403 exits below:
    # RequestLogMiddleware demotes probe request lines from this ctx key, so a
    # later publish would leave a trusted probe's unknown-model 404 suppressed
    # in api_logs yet logged at INFO — the two noise controls disagreeing
    # about the same request.
    req_ctx.update({"synthetic_probe": is_synthetic_probe})
    # ``log_synthetic_probes`` opts probe traffic into api_logs persistence so
    # it (and its real usage/cost) shows in the requests dashboard. The
    # X-Provider header stays keyed on ``is_synthetic_probe``; the per-user
    # quota increment is unconditional (the marker affects noise, never money).
    # A setting read failure defaults to suppression.
    log_synthetic_probes = False
    if (
        is_synthetic_probe
        and runtime_settings is not None
        and hasattr(runtime_settings, "get_bool")
    ):
        try:
            log_synthetic_probes = await runtime_settings.get_bool("log_synthetic_probes")
        except Exception:
            log_synthetic_probes = False
    suppress_synthetic_logging = is_synthetic_probe and not log_synthetic_probes

    model = payload.model
    messages = [m.model_dump() for m in payload.messages]

    # Debug-only: log inbound message roles to verify client behavior.
    try:
        roles = [msg.get("role") for msg in messages]
        tool_count = sum(1 for msg in messages if msg.get("role") == "tool")
        logger.debug(
            f"Inbound roles: model={model}, roles={roles}, tool_messages={tool_count}, total={len(messages)}"
        )
    except Exception:
        pass

    request_id = f"req_{uuid.uuid4().hex}"
    start_time = time.time()
    is_authenticated = bool(user_ctx.get("authenticated"))
    provider = "router"
    # Whatever session the client declared, in whichever idiom it uses: the
    # gateway's own header, an agent's session header, or the session carried in
    # the request body (see serving/utils/session_identity.py). Coding agents
    # declare one but never under the canonical header, so reading that header
    # alone logged the whole agent fleet as session_id = NULL -- and left it out
    # of RouteWise's prefix-cache cost adjustment, which is gated on the
    # ``session_id`` this puts in ``params`` below.
    declared_session = session_identity(ctx.origin.headers, body)
    session_id = declared_session.session_id if declared_session is not None else None
    requested_stream = bool(payload.stream)
    force_streaming = await _should_force_chat_completions_streaming(
        runtime_settings,
        requested_stream,
    )
    effective_stream = requested_stream or force_streaming

    metadata = {
        "user_agent": ctx.origin.user_agent,
        "referer": ctx.origin.referer,
        "ip": ctx.origin.ip.client_ip,
        "authorization": ctx.origin.authorization_header or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
    }
    if is_synthetic_probe:
        metadata["synthetic_probe"] = True
    if declared_session is not None:
        metadata["session_id"] = session_id
        metadata["session_id_source"] = declared_session.source
    # Present only when the caller authenticated with an agent-sandbox
    # capability token; lands in api_logs.agent_job_id, which is both the
    # owner's cost report and the ledger the job's budget is measured from.
    if user_ctx.get("agent_job_id"):
        metadata["agent_job_id"] = user_ctx["agent_job_id"]
    # Present only when the batch worker runs the request for a batch
    # (serving/batch/dispatch.py); lands in api_logs.batch_job_id, the batch's
    # cost and usage attribution, with the item and attempt in the metadata.
    metadata.update(ctx.batch_log_metadata())
    metadata.update(ledger_attribution(user_ctx, started_at=start_time))

    early_params: dict[str, Any] = {"stream": effective_stream}
    if session_id:
        early_params["session_id"] = session_id

    # A request naming an auto model (``auto``, ``auto-<tier>``) is rewritten
    # here to the model its tier chooses, and from here on is served exactly as
    # a request naming that model: the access check below runs on it, its own
    # router picks the endpoint, and it is logged and billed under it with the
    # decision in ``metadata.auto`` (serving/auto_mode.py). The caller is not
    # told which model that is: responses name the auto model, and so do error
    # messages.
    auto_selection: AutoSelection | None = None
    auto_mode = await auto_mode_for(model, services.app_services)
    if auto_mode is not None:
        features = openai_features(
            messages,
            tools=payload.tools,
            response_format=(
                payload.response_format.model_dump(by_alias=True, exclude_none=True)
                if payload.response_format is not None
                else None
            ),
            max_tokens=payload.max_tokens,
            required_modalities=_required_modalities(messages),
        )
        try:
            auto_selection = await auto_mode.select(
                model,
                features,
                router_exec=router_exec,
                user_ctx=user_ctx,
                visibility_resolver=model_visibility_resolver,
                session=session_key(user_ctx, declared_session, conversation_fingerprint(messages)),
            )
        except AutoModelUnavailable as exc:
            if log_store and not suppress_synthetic_logging:
                completions_logger.schedule_log(
                    request_id,
                    {
                        "request_id": request_id,
                        "model_id": model,
                        "provider": "router",
                        "prompt": messages,
                        "response": None,
                        "usage": None,
                        "latency_ms": int((time.time() - start_time) * 1000),
                        "status_code": exc.status_code,
                        "error": exc.detail or f"Model '{model}' not found",
                        "params": early_params,
                        "metadata": metadata,
                        "pricing": None,
                        "request_payload": body,
                    },
                )
            if exc.status_code == 404:
                req_ctx.mark_model_not_found()
                raise HTTPException(404, model_not_found_detail(model, user_ctx=user_ctx)) from None
            raise HTTPException(exc.status_code, exc.detail) from None
        apply_to_chat_request(auto_selection, payload)
        hide_served_model(auto_selection)
        model = auto_selection.model_id
        metadata["auto"] = auto_selection.log_metadata()
    # The model name the client is shown, when it is not the one serving it.
    response_model = auto_selection.requested if auto_selection is not None else None

    # Check if model has routing configured
    if model not in router_exec.routes:
        if log_store and not suppress_synthetic_logging:
            completions_logger.schedule_log(
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": "router",
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 404,
                    "error": f"Model '{model}' not found",
                    "params": early_params,
                    "metadata": metadata,
                    "pricing": None,
                    "request_payload": body,
                },
            )
        req_ctx.mark_model_not_found()
        raise HTTPException(404, model_not_found_detail(model, user_ctx=user_ctx))

    # Every reason the caller may not use this model gets one answer: the model
    # does not exist. A sandbox cannot enumerate what is outside its scope by
    # reading the difference between 403 and 404 (serving/model_access.py).
    route = router_exec.routes[model]
    denial = await model_access_denial(
        route, model, user_ctx, visibility_resolver=model_visibility_resolver
    )
    if denial is not None:
        if denial is ModelAccessDenial.ROLE:
            logger.info(
                "Insufficient role for model",
                extra={
                    "model": model,
                    "user_id": user_ctx.get("user_id"),
                    "role": user_ctx.get("role", "free"),
                },
            )
        # An unpublished route is a runtime model creation that has not
        # committed yet. It is refused without a request log row, as it was
        # before this check was shared.
        if (
            denial is not ModelAccessDenial.UNPUBLISHED
            and log_store
            and not suppress_synthetic_logging
        ):
            completions_logger.schedule_log(
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": "router",
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 404,
                    "error": f"Model '{model}' not found",
                    "params": early_params,
                    "metadata": metadata,
                    "pricing": None,
                    "request_payload": body,
                },
            )
        req_ctx.mark_model_not_found()
        raise HTTPException(404, model_not_found_detail(model, user_ctx=user_ctx))

    # The concurrency dependency saw the caller's virtual auto model before
    # selection. Move its full-lifetime load count to the concrete canonical
    # model now that dispatch has resolved it (nothing to move for a batch item).
    ctx.admission.retarget(route_canonical_model_id(route, model))

    require_route_pricing(route)

    # Extract parameters
    params: dict[str, Any] = {"stream": effective_stream}
    if payload.temperature is not None:
        params["temperature"] = payload.temperature
    if payload.top_p is not None:
        params["top_p"] = payload.top_p
    if payload.top_k is not None:
        params["top_k"] = payload.top_k
    if payload.min_p is not None:
        params["min_p"] = payload.min_p
    if payload.max_tokens is not None:
        params["max_tokens"] = payload.max_tokens
    if payload.stop is not None:
        params["stop"] = payload.stop
    if payload.seed is not None:
        params["seed"] = payload.seed
    if payload.frequency_penalty is not None:
        params["frequency_penalty"] = payload.frequency_penalty
    if payload.presence_penalty is not None:
        params["presence_penalty"] = payload.presence_penalty
    if payload.reasoning_effort is not None:
        params["reasoning_effort"] = payload.reasoning_effort
    if payload.thinking is not None:
        params["thinking"] = payload.thinking
    if payload.tools is not None:
        params["tools"] = payload.tools
    if payload.tool_choice is not None:
        params["tool_choice"] = payload.tool_choice
    if payload.response_format is not None:
        params["response_format"] = payload.response_format.model_dump(
            by_alias=True, exclude_none=True
        )
    if session_id:
        params["session_id"] = session_id

    # Stable user identifier used by cost tracking
    user_id: str = user_ctx.get("user_id") or "anonymous"

    # Affinity key for multi-key API rotation — pinned to the specific
    # hyi-xxx key in use (not user_id, since a user may have multiple keys),
    # or to the grant for a sandbox, which presents no key hash.
    auth_key_hash = user_ctx.get("auth_key_hash")
    ip_info = ctx.origin.ip
    affinity_key = derive_affinity_key(
        auth_key_hash,
        ip_info,
        grant_id=user_ctx.get("agent_grant_id"),
    )
    req_ctx.update(
        {
            "request_id": request_id,
            "auth_key_hash": auth_key_hash or "_anon",
            "affinity_key": affinity_key,
            # synthetic_probe is already published, right after the trust
            # check — before the early 404/403 exits above.
            # User identity for blast-radius reporting — the routing layer reads
            # these to name the callers a failure streak hit in circuit-breaker
            # alerts.
            "user_id": user_id,
            "user_name": user_ctx.get("user_name"),
        }
    )

    # Provider pinning: allows the harness (or admin tooling) to force routing
    # to a specific backend.  Only honoured for admin users to prevent abuse.
    pin_provider = ctx.origin.headers.get("X-Route-Pin")
    if pin_provider and not user_ctx.get("is_admin", False):
        pin_provider = None  # silently ignore for non-admin

    # Typed routing context. Enriched once the adapter response surfaces its
    # ``_routing`` dict via ``merge_adapter_routing``. Frozen — every
    # enrichment returns a new instance.
    routing: RoutingInfo = build_initial_routing_info(
        payload, request_id=request_id, pin_provider=pin_provider
    )

    def get_adapter_config_for_provider(provider_name: str, base_url: str | None = None) -> Any:
        """Return the adapter config object for the provider/base_url used."""
        if model not in router_exec.routes:
            return None
        route_config = router_exec.routes[model]
        for adapter, _ in route_config.adapters:
            cfg = getattr(adapter, "config", None)
            if not cfg:
                continue
            if cfg.provider != provider_name:
                continue
            if base_url and getattr(cfg, "base_url", None) != base_url:
                continue
            return cfg
        return None

    def get_single_route_provider() -> str | None:
        """Return a provider name when the route has exactly one backend."""
        if len(route.adapters) != 1:
            return None
        adapter, _ = route.adapters[0]
        config = getattr(adapter, "config", None)
        return getattr(config, "provider", None)

    # Pre-flight: reject media content when the model does not support it.
    # This check is placed before routing so both streaming and non-streaming
    # paths get a clean 400 instead of silently stripping image/audio blocks.
    route_config = router_exec.routes.get(model)
    # A model "supports" a modality if ANY of its routes declares it (the union
    # across routes). The modality-aware router then dispatches media only to
    # the routes that actually accept it, so this 400 fires only when no route
    # can handle the modality at all.
    supported_modalities: set[str] = set()
    if route_config and route_config.adapters:
        for _route_adapter, _ in route_config.adapters:
            supported_modalities.update(_route_adapter.config.input_modalities or ["text"])
    unsupported_modality = _find_unsupported_modality(messages, sorted(supported_modalities))
    if unsupported_modality:
        error_message = f"Model '{model}' does not support {unsupported_modality} input"
        if log_store and not suppress_synthetic_logging:
            completions_logger.schedule_log(
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": "router",
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 400,
                    "error": error_message,
                    "params": early_params,
                    "metadata": metadata,
                    "pricing": None,
                    "request_payload": body,
                },
            )
        raise HTTPException(status_code=400, detail=error_message)

    # Non-text modalities this request needs; passed to the active router so it
    # dispatches media only to routes that declare the modality (a capable
    # model may have text-only fallbacks that must not receive the media).
    required_modalities = _required_modalities(messages)

    # Pre-flight: validate pin_provider before routing.  For streaming this is
    # critical (HTTP 200 is already committed once StreamingResponse starts),
    # but we check unconditionally so non-stream also gets a clean 400.
    if pin_provider:
        route = router_exec.routes.get(model)
        if not route or not any(
            (
                adapter.config.provider == pin_provider
                or endpoint_id_for_adapter(adapter) == pin_provider
            )
            and weight > 0
            for adapter, weight in route.adapters
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Pinned provider '{pin_provider}' not found for model {model}",
            )

    # Per-model routing strategy via ModelRouterRegistry. Pinned requests still
    # bypass RouteWise, but both selected routers now share one execution call
    # contract below.
    routing_options = (
        RoutingRequestOptions(
            pin_provider=pin_provider,
            required_modalities=required_modalities,
        )
        if pin_provider or required_modalities
        else None
    )
    active_router = router_exec
    if model_router_registry is not None and pin_provider is None:
        active_router = model_router_registry.get_router(model)

    # Thread the external request id into params so the router correlates its
    # routing metadata, prefix-cache stash, and observation under one id instead
    # of generating a divergent internal id.
    params["request_id"] = request_id
    router_params = dict(params)
    if routing_options is not None:
        # Keep router-owned controls out of provider adapter kwargs. Empty
        # options are still omitted for one-release custom-router compatibility.
        router_params["routing_options"] = routing_options

    # Streaming path
    if effective_stream:
        adapter_chunks = active_router.stream_chat_completion(
            model,
            messages,
            **router_params,
        )

        session = StreamSession(
            routing=routing,
            model=model,
            messages=messages,
            params=params,
            request_id=request_id,
            start_time=start_time,
            request_headers=ctx.origin.headers,
            metadata=metadata,
            user_id=user_id,
            is_synthetic_probe=is_synthetic_probe,
            suppress_synthetic_logging=suppress_synthetic_logging,
            log_store=log_store,
            active_router=active_router,
            cost_tracker=cost_tracker,
            completions_logger=completions_logger,
            pricing_lookup=pricing_lookup,
            get_adapter_config_for_provider=get_adapter_config_for_provider,
            request_payload=body,
            timeout_fired_probe=ctx.timeout_probe,
            response_model=response_model,
        )

        logger.debug(f"Creating StreamingResponse for model: {model}")

        return ChatStream(
            session=session,
            adapter_chunks=adapter_chunks,
            buffered=force_streaming,
            request_id=request_id,
            model=response_model or model,
            request_headers=ctx.origin.headers,
            provider_header=get_single_route_provider() if is_synthetic_probe else None,
        )

    # Non-streaming path
    try:
        response = await active_router.chat_completion(
            model,
            messages,
            **router_params,
        )
        serializer_mode = resolve_mode(ctx.origin.headers)

        # Apply serializer: strip _routing metadata and enforce reasoning_content
        # policy (strict / passthrough), mirroring the streaming path contract.
        provider = "router"
        base_url = None
        if isinstance(response, dict):
            sanitize_result = sanitize_response(response, serializer_mode)
            response = sanitize_result.response_json
            if isinstance(response.get("usage"), dict):
                # An unreadable count is billed as 0 (token_utils.usage_count).
                # Not completed here: billing and the log read this usage, and
                # a total made up for the response model would change billing.
                response["usage"] = sanitize_usage(response["usage"])
            if response_model is not None:
                response.pop("provider", None)
                if "model" in response:
                    response["model"] = response_model
            routing_info = sanitize_result.routing_info
            if routing_info:
                routing = merge_adapter_routing(routing, routing_info)
                provider = routing.provider or "router"
                base_url = routing.base_url
                # See comment above: strip upstream_cost_usd before merging into metadata JSONB.
                # Persisted metadata intentionally keeps adapter-emitted legacy keys like
                # routewise for DB log compatibility; RoutingInfo.strategy_metadata is
                # only the in-process isolation boundary.
                metadata.update({k: v for k, v in routing_info.items() if k != "upstream_cost_usd"})  # type: ignore[arg-type]
        else:
            # Fallback: get provider from request context when response is not a dict
            ctx = req_ctx.get()
            if ctx and "provider" in ctx:
                provider = ctx["provider"]
                logger.debug(
                    f"Using provider from context for non-streaming DB logging: {provider}"
                )

        # Increment daily cost counter for billed requests (non-streaming).
        # Done before the log payload assembly so the row sees the
        # cost-tracker-populated ``upstream_cost_usd`` on ``routing``.
        # Unconditional — probes included: the ``x-probe`` header must be able
        # to affect log noise but never cost/quota (mirrors embeddings.py).
        _ns_usage = (
            normalize_usage(response.get("usage")) if isinstance(response, dict) else None
        ) or {}
        if _ns_usage:
            routing = await cost_tracker.schedule_increment(
                user_id=user_id,
                routing=routing,
                prompt_tokens=int(_ns_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(_ns_usage.get("completion_tokens", 0) or 0),
                total_tokens=(
                    int(_ns_usage["total_tokens"])
                    if _ns_usage.get("total_tokens") is not None
                    else None
                ),
                cache_read_tokens=_ns_usage.get("cache_read_tokens"),
                cache_write_tokens=_ns_usage.get("cache_write_tokens"),
                reasoning_tokens=int(_ns_usage.get("reasoning_tokens", 0) or 0),
            )

        # Background DB log so the row write doesn't block the HTTP response.
        if log_store and not suppress_synthetic_logging:
            # raw_dict_for_routing prefers adapter-emitted ``extra["pricing"]``
            # then falls back to a registry walk — same precedence the prior
            # ``routing_pricing or get_pricing_for_provider(...)`` had when
            # ``routing.pricing`` was a dict.
            pricing = pricing_lookup.raw_dict_for_routing(routing)
            completions_logger.schedule_log(
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": provider,
                    "prompt": messages,
                    "response": response,
                    "usage": normalize_usage(response.get("usage"))
                    if isinstance(response, dict)
                    else None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": 200,
                    "params": completions_logger.build_db_params(
                        params,
                        provider,
                        base_url,
                        get_adapter_config_for_provider,
                    ),
                    "metadata": _metadata_with_fallback_diagnostic(metadata, routing),
                    "pricing": pricing,
                    "upstream_cost_usd": routing.upstream_cost_usd,
                    "request_payload": body,
                },
            )
            _background_tasks.clear()
            _background_tasks.update(completions_logger._background_tasks)

        # Emit token counters when usage is available, with anomaly checks
        # Normalize usage to extract reasoning_tokens from nested locations
        raw_usage = response.get("usage", {}) if isinstance(response, dict) else {}
        normalized_usage = normalize_usage(raw_usage)
        usage = normalized_usage if normalized_usage is not None else {}
        if usage and not is_synthetic_probe:
            prompt_tokens_raw = usage.get("prompt_tokens")
            completion_tokens_raw = usage.get("completion_tokens")
            total_tokens_raw = usage.get("total_tokens")
            reasoning_tokens_raw = usage.get("reasoning_tokens")

            try:
                prompt_tokens = int(prompt_tokens_raw or 0)
                completion_tokens = int(completion_tokens_raw or 0)
                reasoning_tokens = int(reasoning_tokens_raw or 0)
                total_tokens = int(
                    total_tokens_raw or (prompt_tokens + completion_tokens + reasoning_tokens)
                )
            except Exception:
                logger.warning(f"Invalid token usage types for {model}/{provider}: {usage}")
                prompt_tokens = completion_tokens = reasoning_tokens = total_tokens = 0

            # Basic sanity: non-negative, totals consistent, and not absurdly large.
            # Providers differ on whether reasoning is already counted inside
            # completion_tokens, so mirror billing semantics here.
            sane = _token_usage_is_sane(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                total_tokens=total_tokens,
            )
            if not sane:
                logger.warning(f"Token usage anomaly for {model}/{provider}: {usage}")

        # Record routing observation for online learning (RouteWise)
        if not is_synthetic_probe:
            normalized_usage = normalize_usage(raw_usage)
            ns_usage = normalized_usage if normalized_usage is not None else {}
            # Authoritative observed cache usage from the provider, through the
            # canonical helper so this path shares one validation rule with
            # cost and persistence. None means "provider did not report" (NOT
            # equivalent to 0).
            cached_tokens = optional_token_count(ns_usage.get("cache_read_tokens"))
            completions_logger.record_routing_observation(
                active_router,
                model,
                routing if isinstance(response, dict) else None,
                request_id=request_id,
                ttft_ms=None,
                total_latency_ms=(time.time() - start_time) * 1000,
                prompt_tokens=int(ns_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(ns_usage.get("completion_tokens", 0) or 0),
                success=True,
                cached_tokens=cached_tokens,
            )

        provider_header = provider if is_synthetic_probe and provider != "router" else None
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            # The client's copy only: the response model requires all three totals.
            return ChatResponse(
                {**response, "usage": sanitize_usage(response["usage"], complete=True)},
                provider_header,
            )
        return ChatResponse(response, provider_header)

    except ProviderPinError as exc:
        raise HTTPException(
            status_code=400,
            detail=scrub_error_for_user(exc, request_id, 400),
        ) from exc

    except (AllCircuitsOpenError, TargetUnavailableError) as exc:
        # Full provider outage: every circuit breaker for this model is open.
        # Surface this as 503 Service Unavailable so clients can distinguish
        # "we're temporarily overloaded / all upstreams down" from a generic
        # 500 server error.
        raise HTTPException(
            status_code=503,
            detail=scrub_error_for_user(exc, request_id, 503),
        ) from exc

    except Exception as exc:
        # ``exc._routing`` is still a raw dict from the routing layer;
        # ``record_routing_observation`` accepts both shapes. Computed
        # unconditionally so the failed-probe log branch below can reuse it
        # when ``log_synthetic_probes`` is enabled.
        exc_routing = getattr(exc, "_routing", None)
        # Record failure observation for online learning (RouteWise). A throwing
        # observation update must never abort the error log below: persisting the
        # failed request is the priority (an online-learning router's
        # ``record_observation`` does real work and can raise). If it did, the
        # ``schedule_log`` call further down would be skipped and the failed
        # request would be dropped from ``api_logs``.
        if not is_synthetic_probe:
            try:
                completions_logger.record_routing_observation(
                    active_router,
                    model,
                    exc_routing,
                    request_id=request_id,
                    ttft_ms=None,
                    total_latency_ms=(time.time() - start_time) * 1000,
                    prompt_tokens=0,
                    completion_tokens=0,
                    success=False,
                )
            except Exception:
                logger.exception(
                    "record_routing_observation failed on the failure path; "
                    "continuing to the error log"
                )

        # Best-effort extraction of status code from exception. The 6-attribute
        # fallback chain (status_code → response.status_code → response.status →
        # status → code → 500) lives in ``routing_info._status_code_from_exception``;
        # see that function for the per-library mapping.
        exc_status_code = _status_code_from_exception(exc)

        # Attribution for the error path, used twice: the background DB log below
        # takes it as a value, while RequestLogMiddleware and the in-process alert
        # rules read it from req_ctx, which this also republishes because the
        # ``req_ctx.push`` scope around the adapter call is already unwound here.
        # See ``_publish_error_provider`` for why the "router" sentinel is not
        # published and what breaks when the label is missing.
        provider_for_error = _publish_error_provider(exc_routing)

        if log_store and not suppress_synthetic_logging:
            metadata_for_error = metadata
            if isinstance(exc_routing, dict):
                metadata_for_error = {
                    **metadata,
                    **json_safe({k: v for k, v in exc_routing.items() if k != "upstream_cost_usd"}),
                }
            completions_logger.schedule_log(
                request_id,
                {
                    "request_id": request_id,
                    "model_id": model,
                    "provider": provider_for_error,
                    "prompt": messages,
                    "response": None,
                    "usage": None,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "status_code": exc_status_code,
                    "error": format_exception_for_db(exc),
                    "params": params,
                    "metadata": metadata_for_error,
                    "pricing": None,  # Error case - no pricing available
                    "request_payload": body,
                },
            )
        raise HTTPException(
            exc_status_code,
            scrub_error_for_user(exc, request_id, exc_status_code),
        ) from exc
