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

    # Synthetic probes are suppressed from rejection logging too, unless
    # ``log_synthetic_probes`` opts them in — mirrors the handler-path
    # suppression so a probe rejected at the gate (e.g. during the overload it
    # is meant to detect) does not pollute api_logs while probe logging is off.
    is_synthetic_probe = request.headers.get("x-probe", "").lower() == "synthetic"
    if is_synthetic_probe:
        try:
            if not await runtime_settings.get_bool("log_synthetic_probes"):
                return
        except Exception:
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
