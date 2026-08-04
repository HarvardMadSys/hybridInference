"""Best-effort persistent log of rejected inference requests.

Writes a row to ``api_logs`` (via :class:`BaseLogStore.log_request`) for
inference-path requests rejected at the gate — concurrency limit, quota,
model-not-found, blocked IP. 401 auth challenges are intentionally excluded
because normal clients produce them during token refresh/auth probing. Gated by
the ``log_rejected_requests`` runtime setting (default off). Never raises: a
logging failure must not alter the rejection HTTP response.

A gate rejection fires before any handler has parsed the request, so the prompt
and the caller's identity are not simply lying around the way they are on the
success path. :func:`rejection_logging_enabled` lets a call site check whether a
row will be written at all, and :func:`capture_rejected_prompt` recovers the
prompt under bounds that keep a refused request from becoming a lever on the
server. Both are optional: skipping them costs detail in the row, never the row.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from fastapi import Request

    from serving.config.runtime_settings import RuntimeSettings
    from serving.storage.base import BaseLogStore

logger = get_logger(__name__)

#: Largest request body read purely to recover a prompt for a rejection log.
#: Bodies above this are skipped rather than buffered: the request is being
#: refused either way, so buffering an arbitrarily large payload to enrich a log
#: row would hand a rejected source a cheap memory-amplification lever.
REJECTED_PROMPT_MAX_BODY_BYTES = 1_048_576

#: Wall-clock cap on that read. A rejected caller has no claim on the server's
#: time, so a stalled or trickled upload yields no prompt rather than pinning a
#: task for as long as the client cares to hold the connection open.
REJECTED_PROMPT_BODY_TIMEOUT_SEC = 2.0

INFERENCE_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/completion",
    # The Anthropic Messages handler is registered at both aliases, so both
    # must be recognized or rejections on the root route are dropped silently.
    "/v1/messages",
    "/anthropic/v1/messages",
)


def _is_inference_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in INFERENCE_PATH_PREFIXES)


def extract_prompt_from_body(body: Any) -> list[dict[str, Any]] | str:
    """Best-effort pull of the prompt content from a parsed request body.

    Handles the three inference shapes the gateway accepts: chat/messages
    (``messages``, used by both OpenAI chat completions and Anthropic
    Messages), embeddings (``input``), and legacy completions (``prompt``).
    Returns ``""`` for anything unrecognized so callers can pass the result
    straight through to ``log_request`` without branching.
    """
    if not isinstance(body, dict):
        return ""
    for key in ("messages", "input", "prompt"):
        value = body.get(key)
        if value:
            return value
    return ""


def _resolve_services(
    request: Request,
    log_store: BaseLogStore | None,
    runtime_settings: RuntimeSettings | None,
) -> tuple[BaseLogStore | None, RuntimeSettings | None]:
    """Fill in unset ``log_store`` / ``runtime_settings`` from app state."""
    if log_store is not None and runtime_settings is not None:
        return log_store, runtime_settings
    try:
        app = request.app
    except Exception:
        # ``Request.app`` raises on a bare ASGI scope with no "app" key. These
        # helpers are diagnostics, so an unusual request shape must degrade to
        # "don't log", never to an exception on the rejection path.
        app = None
    services = getattr(getattr(app, "state", None), "services", None)
    if log_store is None:
        log_store = getattr(services, "log_store", None) if services else None
    if runtime_settings is None:
        runtime_settings = getattr(services, "runtime_settings", None) if services else None
    return log_store, runtime_settings


def _is_synthetic_probe(request: Request) -> bool:
    return request.headers.get("x-probe", "").lower() == "synthetic"


async def rejection_logging_enabled(
    request: Request,
    *,
    status_code: int,
    log_store: BaseLogStore | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> bool:
    """Return whether a rejection for this request would be persisted.

    Public so a call site can find out *before* paying for optional enrichment
    — reading the request body, resolving the caller's identity — whether that
    work would end up anywhere. :func:`log_rejection` applies the same gate, so
    a caller that skips this only loses the enrichment, never correctness.

    Never raises.
    """
    log_store, runtime_settings = _resolve_services(request, log_store, runtime_settings)
    if log_store is None or runtime_settings is None:
        return False
    if not _is_inference_path(request.url.path):
        return False
    if status_code == 401:
        return False

    try:
        if not await runtime_settings.get_bool("log_rejected_requests"):
            return False
    except Exception:
        logger.exception(
            "rejection_log_failed",
            extra={"event": "rejection_log_failed", "stage": "toggle_read"},
        )
        return False

    # Synthetic probes are suppressed from rejection logging too, unless
    # ``log_synthetic_probes`` opts them in — mirrors the handler-path
    # suppression so a probe rejected at the gate (e.g. during the overload it
    # is meant to detect) does not pollute api_logs while probe logging is off.
    if _is_synthetic_probe(request):
        try:
            return await runtime_settings.get_bool("log_synthetic_probes")
        except Exception:
            return False
    return True


async def capture_rejected_prompt(
    request: Request,
    *,
    max_body_bytes: int = REJECTED_PROMPT_MAX_BODY_BYTES,
    timeout_sec: float = REJECTED_PROMPT_BODY_TIMEOUT_SEC,
) -> list[dict[str, Any]] | str:
    """Best-effort prompt for a request rejected before its handler ran.

    Prefers a body an earlier dependency already parsed and cached (Starlette
    stores it on ``request._json`` after ``await request.json()``). Otherwise
    reads the body itself — which a pre-handler gate has not yet done — under
    two bounds: a declared ``Content-Length`` no larger than *max_body_bytes*,
    and *timeout_sec* of wall clock. A body whose length is unknown (chunked
    transfer) is skipped outright. Without those bounds, awaiting the full body
    of a request the server is refusing is a slowloris foothold.

    Returns ``""`` when no prompt can be recovered, for any reason.

    Must be awaited *before* the rejection response is sent: once the response
    completes, the ASGI ``receive`` channel no longer yields body chunks.
    """
    cached_json = getattr(request, "_json", None)
    if cached_json is not None:
        try:
            return extract_prompt_from_body(cached_json)
        except Exception:
            return ""

    declared = request.headers.get("content-length")
    if declared is None:
        return ""
    try:
        length = int(declared)
    except ValueError:
        return ""
    if length <= 0 or length > max_body_bytes:
        return ""

    try:
        raw = await asyncio.wait_for(request.body(), timeout_sec)
        return extract_prompt_from_body(json.loads(raw))
    except Exception:
        # Timeout, disconnect, non-JSON body — all mean "no prompt", never an
        # error the caller has to handle.
        return ""


async def log_rejection(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    reason: str,
    user: dict[str, Any] | None,
    model_id: str = "",
    prompt: list[dict[str, Any]] | str = "",
    log_store: BaseLogStore | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> None:
    """Persist a rejection row when the toggle is on.

    Resolves ``log_store`` and ``runtime_settings`` from
    ``request.app.state.services`` when callers don't supply them, so call
    sites only need to pass the rejection-specific context. Tests can inject
    explicit instances via the keyword args.

    ``error_code`` is a short machine-readable identifier (e.g.
    ``"concurrency_limit_exceeded"``); ``reason`` is a brief human-readable
    detail; ``user`` is the verified user dict or ``None`` for pre-auth
    rejections. ``prompt`` is the original request prompt/messages; it is
    persisted only when the store's content-retention policy
    (``store_full_content``) allows, exactly as on the success path.
    """
    log_store, runtime_settings = _resolve_services(request, log_store, runtime_settings)
    if not await rejection_logging_enabled(
        request,
        status_code=status_code,
        log_store=log_store,
        runtime_settings=runtime_settings,
    ):
        return
    is_synthetic_probe = _is_synthetic_probe(request)

    ctx = req_ctx.get()
    request_id = ctx.get("request_id") or ""
    metadata: dict[str, Any] = {
        "rejection": True,
        "reason": reason,
        "route": request.url.path,
        "role": user.get("role") if user else None,
        "user_id": user.get("user_id") if user else None,
        "ip": get_client_ip(request),
    }
    # Tag persisted probe rejections so consumers that exclude probes via this
    # field (e.g. PostgresLogStore.get_model_activity) don't miscount them as
    # real-user traffic. Only reached when log_synthetic_probes opted them in.
    if is_synthetic_probe:
        metadata["synthetic_probe"] = True
    # Classify embedding rejections so they match the success-path tagging and
    # are excluded from chat-performance aggregates (deps like verify_api_key /
    # enforce_user_concurrency reject before the handler sets this metadata).
    if request.url.path.startswith("/v1/embeddings"):
        metadata["request_type"] = "embedding"

    try:
        await log_store.log_request(
            request_id=request_id,
            model_id=model_id,
            provider="",
            prompt=prompt,
            response=None,
            usage=None,
            latency_ms=0,
            status_code=status_code,
            error=error_code,
            params=None,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "rejection_log_failed",
            extra={
                "event": "rejection_log_failed",
                "stage": "log_request",
                "error_code": error_code,
                "status_code": status_code,
            },
        )
