"""OpenAI-compatible chat completions endpoint with routing and auth."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from routing.endpoints import endpoint_id_for_adapter
from routing.executor import ProviderPinError
from routing.protocols import RoutingRequestOptions
from routing.routers import AllCircuitsOpenError
from serving.config.runtime_settings import RuntimeSettings, get_runtime_settings
from serving.config.settings import has_role
from serving.exceptions import scrub_error_for_user
from serving.model_access import is_model_disabled_for_user
from serving.openai_chat_serializer import resolve_mode, sanitize_response
from serving.schemas import (
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
from serving.servers.routers.completions_stream import StreamSession, ToolCallAccumulator
from serving.servers.routers.routing_info import (
    RoutingInfo,
    _provider_for_error,
    _status_code_from_exception,
    build_initial_routing_info,
    merge_adapter_routing,
)
from serving.servers.streaming_state import (
    REQUEST_TIMEOUT_SCOPE_STATE_KEY,
    STREAMING_RESPONSE_SCOPE_STATE_KEY,
)
from serving.storage.utils import billable_output_tokens, json_safe
from serving.utils import context as req_ctx
from serving.utils.errors import format_exception_for_db
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip
from serving.utils.token_utils import normalize_usage

if TYPE_CHECKING:
    from collections.abc import Callable

    from serving.servers.routers.completions_cost import CostTracker, PricingLookup
    from serving.servers.routers.completions_logging import CompletionsLogger

logger = get_logger(__name__)
router = APIRouter()
_background_tasks: set[asyncio.Task[Any]] = set()

# Maps a multimodal content block "type" to the input modality it requires.
# Used by the router pre-flight to reject media a model can't accept before
# any provider call (so streaming and non-streaming both get a clean 400
# instead of silently dropping the media downstream).
_CONTENT_BLOCK_MODALITY = {
    "image_url": "image",
    "image": "image",
    "input_image": "image",
    "input_audio": "audio",
    "audio": "audio",
}


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
            required = _CONTENT_BLOCK_MODALITY.get(block.get("type"))
            if required and required not in supported:
                return required
    return None


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


async def _buffer_streaming_response_for_non_stream_client(
    stream_chunks: Any,
    *,
    request_id: str,
    model: str,
    request_headers: Any,
) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls = ToolCallAccumulator()
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    response_id = request_id
    created = int(time.time())

    async for chunk in stream_chunks:
        if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
            continue
        try:
            chunk_json = json.loads(chunk[6:])
        except json.JSONDecodeError:
            continue

        error = chunk_json.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            status_code = code if isinstance(code, int) else 500
            logger.error(f"Upstream error in stream: {error}", extra={"request_id": request_id})
            raise HTTPException(
                status_code=status_code,
                detail=scrub_error_for_user(None, request_id, status_code),
            )

        response_id = chunk_json.get("id") or response_id
        created = int(chunk_json.get("created") or created)
        if chunk_json.get("usage"):
            usage = normalize_usage(chunk_json.get("usage")) or chunk_json.get("usage")

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
        response["usage"] = usage
    return sanitize_response(response, resolve_mode(request_headers)).response_json


_FORCE_STREAMING_KEEPALIVE_S = 15


def _timeout_fired_probe(request: Request) -> Callable[[], bool]:
    """Build a probe telling whether TimeoutMiddleware's deadline has fired.

    Read lazily at exception time (not at request entry): the middleware's
    ``CancelScope.cancel_called`` only flips once the deadline expires, which
    is what distinguishes a gateway timeout from a client disconnect.
    """

    def _fired() -> bool:
        state = request.scope.get("state") or {}
        timeout_scope = state.get(REQUEST_TIMEOUT_SCOPE_STATE_KEY)
        return bool(getattr(timeout_scope, "cancel_called", False))

    return _fired


async def _close_stream_quietly(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if not callable(aclose):
        return
    try:
        with anyio.CancelScope(shield=True):
            await aclose()
    except (Exception, asyncio.CancelledError):
        pass


async def _yield_with_cleanup(stream: Any) -> Any:
    try:
        async for data in stream:
            yield data
    finally:
        await _close_stream_quietly(stream)


async def _chain_first_then_rest(first: bytes | None, stream: Any) -> Any:
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
            except asyncio.TimeoutError:
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
                usage = normalize_usage(chunk_json.get("usage")) or chunk_json.get("usage")

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
        response["usage"] = usage
    sanitized = sanitize_response(response, resolve_mode(request_headers)).response_json
    yield json.dumps(sanitized).encode()


def derive_affinity_key(auth_key_hash: str | None, client_ip: str) -> str:
    """Compute the affinity key used for sticky multi-key routing."""
    if auth_key_hash:
        return auth_key_hash
    return f"ip:{client_ip}"


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

    Parses the request body using Pydantic for validation, then routes
    to the appropriate adapter. Streaming and non-streaming flows are
    both supported.
    """
    try:
        body = await request.json()
        payload = ChatCompletionRequest.model_validate(body)
    except Exception as e:
        raise HTTPException(400, "Invalid JSON or schema in request body") from e

    is_synthetic_probe = request.headers.get("x-probe", "").lower() == "synthetic"
    # ``log_synthetic_probes`` opts probe traffic into api_logs persistence so it
    # (and its real usage/cost) shows in the requests dashboard. The per-user
    # quota increment and X-Provider header stay keyed on ``is_synthetic_probe``.
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
    session_id = request.headers.get("X-Session-ID")
    requested_stream = bool(payload.stream)
    force_streaming = await _should_force_chat_completions_streaming(
        runtime_settings,
        requested_stream,
    )
    effective_stream = requested_stream or force_streaming

    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "referer": request.headers.get("referer"),
        "ip": get_client_ip(request),
        "authorization": bool(authorization) or is_authenticated,
        "authenticated": is_authenticated,
        "user_id": user_ctx.get("user_id"),
    }
    if is_synthetic_probe:
        metadata["synthetic_probe"] = True
    if session_id:
        metadata["session_id"] = session_id

    early_params: dict[str, Any] = {"stream": effective_stream}
    if session_id:
        early_params["session_id"] = session_id

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
        raise HTTPException(404, f"Model '{model}' not found")

    # Role-based model gate: insufficient role sees a 404 as if the model doesn't exist
    route = router_exec.routes[model]
    if not route.published:
        req_ctx.mark_model_not_found()
        raise HTTPException(404, f"Model '{model}' not found")
    required = route.required_role or ("admin" if route.admin_only else "free")
    user_role = user_ctx.get("role", "free")
    if model_visibility_resolver is not None:
        canonical_id = route.adapters[0][0].config.id if route.adapters else model
        required = await model_visibility_resolver.get_effective_required_role(
            canonical_id, required
        )
    if not has_role(user_role, required):
        logger.info(
            "Insufficient role for model",
            extra={"model": model, "user_id": user_ctx.get("user_id"), "role": user_role},
        )
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
        raise HTTPException(404, f"Model '{model}' not found")
    if is_model_disabled_for_user(
        route.adapters[0][0].config.id if route.adapters else model, user_ctx
    ):
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
        raise HTTPException(404, f"Model '{model}' not found")

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
    # hyi-xxx key in use (not user_id, since a user may have multiple keys).
    auth_key_hash = user_ctx.get("auth_key_hash")
    affinity_key = derive_affinity_key(auth_key_hash, get_client_ip(request))
    req_ctx.update(
        {
            "request_id": request_id,
            "auth_key_hash": auth_key_hash or "_anon",
            "affinity_key": affinity_key,
            "synthetic_probe": is_synthetic_probe,
            # User identity for failure attribution — the routing layer reads
            # these to name the offending users in circuit-breaker alerts.
            "user_id": user_id,
            "user_name": user_ctx.get("user_name"),
        }
    )

    # Provider pinning: allows the harness (or admin tooling) to force routing
    # to a specific backend.  Only honoured for admin users to prevent abuse.
    pin_provider = request.headers.get("X-Route-Pin")
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
    model_modalities = (
        route_config.adapters[0][0].config.input_modalities
        if route_config and route_config.adapters
        else []
    )
    unsupported_modality = _find_unsupported_modality(messages, model_modalities)
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
    routing_options = RoutingRequestOptions(pin_provider=pin_provider) if pin_provider else None
    active_router = router_exec
    if model_router_registry is not None and routing_options is None:
        active_router = model_router_registry.get_router(model)

    # Thread the external request id into params so the router correlates its
    # routing metadata, prefix-cache stash, and observation under one id instead
    # of generating a divergent internal id.
    params["request_id"] = request_id
    router_params = dict(params)
    if routing_options is not None:
        # Only pinned requests need the new keyword today, and the registry has
        # already routed those to FixedRouter. This keeps one-release custom
        # strategies that accept only **params from forwarding an empty options
        # object to their provider adapter.
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
            request_headers=request.headers,
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
            timeout_fired_probe=_timeout_fired_probe(request),
        )

        logger.debug(f"Creating StreamingResponse for model: {model}")

        if force_streaming:
            keepalive_gen = _streaming_response_with_keepalive(
                session.stream(adapter_chunks),
                request_id=request_id,
                model=model,
                request_headers=request.headers,
            )
            first_chunk: bytes | None = None
            try:
                first_chunk = await keepalive_gen.__anext__()
            except StopAsyncIteration:
                first_chunk = None
            except HTTPException:
                raise
            if is_synthetic_probe:
                provider_header = get_single_route_provider()
                if provider_header:
                    http_response.headers["X-Provider"] = provider_header

            # This path emits keepalive whitespace to keep intermediaries alive;
            # without no-transform / no-buffering a CDN (e.g. Cloudflare) can
            # buffer it and defeat the keepalive on long completions.
            request.scope.setdefault("state", {})[STREAMING_RESPONSE_SCOPE_STATE_KEY] = True
            streaming_headers: dict[str, str] = {
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            }
            if is_synthetic_probe:
                provider_header = get_single_route_provider()
                if provider_header:
                    streaming_headers["X-Provider"] = provider_header

            return StreamingResponse(
                _chain_first_then_rest(first_chunk, keepalive_gen),
                media_type="application/json",
                headers=streaming_headers or None,
            )

        # `no-transform` stops intermediary CDNs (e.g. Cloudflare) from buffering
        # the stream to compress it, which collapses TTFT to total latency.
        response_headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        }
        if is_synthetic_probe:
            provider_header = get_single_route_provider()
            if provider_header:
                response_headers["X-Provider"] = provider_header
        return StreamingResponse(
            _yield_with_cleanup(session.stream(adapter_chunks)),
            media_type="text/event-stream",
            headers=response_headers,
        )

    # Non-streaming path
    try:
        response = await active_router.chat_completion(
            model,
            messages,
            **router_params,
        )
        serializer_mode = resolve_mode(request.headers)

        # Apply serializer: strip _routing metadata and enforce reasoning_content
        # policy (strict / passthrough), mirroring the streaming path contract.
        provider = "router"
        base_url = None
        if isinstance(response, dict):
            sanitize_result = sanitize_response(response, serializer_mode)
            response = sanitize_result.response_json
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
        if not is_synthetic_probe:
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
                    cache_read_tokens=int(_ns_usage.get("cache_read_tokens", 0) or 0),
                    cache_write_tokens=int(_ns_usage.get("cache_write_tokens", 0) or 0),
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
        usage = normalize_usage(raw_usage) or {}
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
            ns_usage = normalize_usage(raw_usage) or {}
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
            )

        if is_synthetic_probe and provider != "router":
            http_response.headers["X-Provider"] = provider
        return response

    except ProviderPinError as exc:
        raise HTTPException(
            status_code=400,
            detail=scrub_error_for_user(exc, request_id, 400),
        ) from exc

    except AllCircuitsOpenError as exc:
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

        # Background DB log on the error path. Prefer the real upstream provider
        # preserved on ``exc._routing``; the req_ctx push scope has already been
        # reset by the time we get here, so reading it would misattribute genuine
        # upstream failures to the "router" sentinel and hide them from the
        # provider-performance aggregations.
        provider_for_error = _provider_for_error(exc_routing)

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
