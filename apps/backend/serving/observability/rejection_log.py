"""Best-effort persistent log of rejected inference requests.

Writes a row to ``api_logs`` (via :class:`BaseLogStore.log_request`) for
inference-path requests rejected at the gate — concurrency limit, quota,
model-not-found. 401 auth challenges are intentionally excluded because
normal clients produce them during token refresh/auth probing. Gated by the
``log_rejected_requests`` runtime setting (default off). Never raises: a
logging failure must not alter the rejection HTTP response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from fastapi import Request

    from serving.config.runtime_settings import RuntimeSettings
    from serving.storage.base import BaseLogStore

logger = get_logger(__name__)

INFERENCE_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/completion",
    "/anthropic/v1/messages",
)


def _is_inference_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in INFERENCE_PATH_PREFIXES)


async def log_rejection(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    reason: str,
    user: dict[str, Any] | None,
    model_id: str = "",
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
    rejections.
    """
    if log_store is None or runtime_settings is None:
        services = getattr(getattr(request.app, "state", None), "services", None)
        if log_store is None:
            log_store = getattr(services, "log_store", None) if services else None
        if runtime_settings is None:
            runtime_settings = getattr(services, "runtime_settings", None) if services else None

    if log_store is None or runtime_settings is None:
        return
    if not _is_inference_path(request.url.path):
        return
    if status_code == 401:
        return

    try:
        enabled = await runtime_settings.get_bool("log_rejected_requests")
    except Exception:
        logger.exception(
            "rejection_log_failed",
            extra={"event": "rejection_log_failed", "stage": "toggle_read"},
        )
        return
    if not enabled:
        return

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

    try:
        await log_store.log_request(
            request_id=request_id,
            model_id=model_id,
            provider="",
            prompt="",
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
