"""Anthropic Messages API northbound router.

Serves both /v1/messages and /anthropic/v1/messages via two decorators on the
same handler.

This task (Task 12) covers the non-streaming path. Streaming dispatch lands
in Task 13.

Field translation lives in adapter.messages() / adapter.stream_messages();
this router only owns:
  - auth, rate limiting, concurrency
  - model resolution (with Anthropic alias map)
  - field sanitization for OpenAI-backed dispatch
  - error formatting (Anthropic shape)
  - DB logging + metrics
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import time
from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from routing.endpoints import endpoint_id_for_adapter
from serving.adapters.anthropic_aliases import resolve_anthropic_alias
from serving.adapters.anthropic_translator import normalize_inline_system
from serving.adapters.key_pool import KeyPool, KeyPoolExhausted
from serving.config.settings import has_role
from serving.exceptions import operator_safe_error, scrub_error_for_user
from serving.model_access import is_model_disabled_for_user, is_model_outside_grant_scope
from serving.observability.rejection_log import log_rejection
from serving.observability.tracked_tasks import tracked_task
from serving.servers.auth import (
    _next_utc_midnight,
    verify_api_key,
    verify_api_key_for_balance,
)
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import (
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.storage.utils import calculate_cost
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip_info
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens

logger = get_logger(__name__)
router = APIRouter()

# Streaming keepalive. Anthropic clients (e.g. Claude Code) drop a streaming
# request after ~30s of silence, but slow upstream backends can take longer to
# emit a first token. During idle gaps the stream emits an SSE comment heartbeat
# every _KEEPALIVE_INTERVAL seconds to keep the connection alive, giving the
# upstream up to _MAX_STREAM_IDLE seconds to produce the next FORWARDED frame
# before we give up. _STREAM_SENTINEL marks end-of-upstream on the internal queue.
#
# The idle timer only resets on a frame the adapter actually yields, but a
# provider can be actively streaming while producing no forwardable frame for a
# while -- e.g. GLM/Qwen processors buffer a whole XML tool call before emitting
# it, and a large tool call (a big Write) can buffer for a minute-plus. A 60s
# ceiling false-aborts those healthy generations, so the ceiling is generous and
# env-tunable. The client still gets a heartbeat every _KEEPALIVE_INTERVAL, so a
# higher ceiling only delays detection of a genuinely dead upstream (rare), which
# is the right trade vs. killing a good turn. (A byte-level idle detector inside
# the adapter is the fuller fix -- tracked in the surface bug backlog.)
_KEEPALIVE_INTERVAL = 15
try:
    _MAX_STREAM_IDLE = int(os.environ.get("STREAM_MAX_IDLE_S", "300"))
except (TypeError, ValueError):
    _MAX_STREAM_IDLE = 300
_STREAM_SENTINEL: Any = object()


# --- Anthropic-format error envelope ---------------------------------------

_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
}


def _anthropic_error(
    status: int,
    message: str,
    *,
    error_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {
                "type": error_type or _ERROR_TYPE_BY_STATUS.get(status, "api_error"),
                "message": message,
            },
        },
        headers=headers,
    )


def _map_upstream_status(status: int) -> tuple[int, str]:
    """Map an upstream provider HTTP status to (client_status, anthropic error type).

    Upstream auth/permission/billing failures mean the operator's provider
    account is invalid/revoked/out of credit -- never the client's gateway
    key, which already authenticated. Surfacing them verbatim would make the
    Anthropic SDK raise AuthenticationError / a 402 and Claude Code blame the
    user's (valid) key or payment, so 401/402/403 are remapped to a retryable
    502 api_error. Other statuses pass through with their natural Anthropic error
    type (429 -> rate_limit_error, 503 -> overloaded_error, ...).
    """
    if status in (401, 402, 403):
        return 502, "api_error"
    return status, _ERROR_TYPE_BY_STATUS.get(status, "api_error")


_ANTHROPIC_PATHS = ("/v1/messages", "/anthropic/")


async def anthropic_aware_http_exception_handler(request: Request, exc: HTTPException):
    """Path-aware HTTP exception handler.

    Emits Anthropic-format errors for requests against the Anthropic surfaces,
    and the default OpenRouter JSON shape for everything else. Pre-shaped
    error bodies (``exc.detail`` is a dict containing ``"error"``) are
    forwarded verbatim on non-Anthropic surfaces -- this preserves structured
    errors such as ``concurrency_limit_exceeded`` regardless of path. Matches
    the behaviour of the global handler installed by
    ``serving.servers.middleware.error.install_error_handlers``.
    """
    path = request.url.path
    is_anthropic = any(path.startswith(p) for p in _ANTHROPIC_PATHS)

    if isinstance(exc.detail, dict) and "error" in exc.detail:
        # On the Anthropic surfaces these dict bodies (e.g. the concurrency
        # limiter's {"error": {"code": ...}} or the quota check's
        # {"error": "Daily cost quota exceeded", ...}) are NOT Anthropic-shaped,
        # so Claude Code's parser finds no error.type/message and shows an
        # opaque failure. Re-wrap them into the Anthropic envelope, preserving a
        # human-readable message from the inner error.
        if is_anthropic:
            inner = exc.detail["error"]
            if isinstance(inner, dict):
                message = inner.get("message") or inner.get("code") or str(inner)
            else:
                message = str(inner)
            return _anthropic_error(
                exc.status_code, str(message), headers=dict(exc.headers or {}) or None
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=dict(exc.headers or {}),
        )

    from serving.utils.errors import categorize_exception

    err_type = categorize_exception(exc)
    log_fn = logger.error if exc.status_code >= 500 else logger.warning
    log_fn(
        "http_error",
        extra={
            "error_type": err_type,
            "status_code": exc.status_code,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=exc if exc.status_code >= 500 else None,
    )

    if is_anthropic:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": "error",
                "error": {
                    "type": _ERROR_TYPE_BY_STATUS.get(exc.status_code, "api_error"),
                    "message": str(exc.detail),
                },
            },
            headers=dict(exc.headers or {}),
        )
    # Non-Anthropic paths: produce the same OpenRouter shape as install_error_handlers.
    from serving.servers.middleware.error import _build_error_response

    content = _build_error_response(str(exc.detail), code=exc.status_code, typ=err_type)
    return JSONResponse(
        status_code=exc.status_code, content=content, headers=dict(exc.headers or {})
    )


# --- Model resolution ------------------------------------------------------


def _pick_dispatch_adapter(model_id: str, canonical: str, router_exec, user_role: str):
    """Return the route adapter this request may actually be dispatched to.

    This surface forwards an Anthropic-native body, which FixedRouter has no
    method for, so it picks its own adapter rather than calling into the router.
    Picking blind meant it ignored both of the router's admission rules: an
    admin-disabled provider still served traffic here, and an open circuit was
    still chosen while a healthy sibling on the same route sat idle. There is no
    fallback on this handler -- a dispatch failure is what the client gets -- so
    a bad pick is terminal.

    ``eligible_adapters`` applies those rules, in route order; the key-tier
    preference then chooses among the survivors. Admission comes first because it
    is a hard rule -- a disabled or tripped provider is not a candidate no matter
    whose keys it holds -- while the tier check only expresses a preference.
    Taking a survivor in route order also keeps the existing preference instead
    of introducing the router's weighted selection, which would redistribute
    traffic on this surface.
    """
    eligible = router_exec.eligible_adapters(canonical)
    if not eligible:
        # Every provider for this model is admin-disabled or has an open
        # circuit. Same disposition the chat path gives AllCircuitsOpenError:
        # 503, which this surface renders as overloaded_error so the client
        # backs off instead of treating it as a permanent failure.
        raise HTTPException(503, f"No provider is currently available for model '{model_id}'")
    adapter, _weight = _pick_adapter_for_role(eligible, user_role)
    return adapter


async def _resolve(
    model_id: str,
    router_exec,
    user_ctx: dict | None,
    model_visibility_resolver=None,
    *,
    for_dispatch: bool = True,
):
    """Return (canonical_model_id, route, adapter).

    With ``for_dispatch`` (the default) the returned adapter is one the request
    may actually be sent to: admin-disabled providers and open circuits are
    skipped, and a 503 is raised once none are left. Callers that only need the
    visibility check pass ``for_dispatch=False`` -- count_tokens answers locally
    and never reaches an upstream, so a provider outage must not stop it.
    """
    canonical = resolve_anthropic_alias(model_id)
    route = router_exec.routes.get(canonical)
    if route is None or not route.published:
        req_ctx.mark_model_not_found()
        raise HTTPException(404, f"Model '{model_id}' not found")
    required = route.required_role or ("admin" if route.admin_only else "free")
    user_role = (user_ctx or {}).get("role", "free")
    if model_visibility_resolver is not None:
        required = await model_visibility_resolver.get_effective_required_role(canonical, required)
    if not has_role(user_role, required):
        req_ctx.mark_model_not_found()
        raise HTTPException(404, f"Model '{model_id}' not found")
    # Same answer for "the owner disabled it" and "your grant does not name
    # it". Checked on the canonical id, which the alias resolution above has
    # already produced — a grant stores canonical ids, so scoping on the
    # requested spelling would let an alias walk straight past it.
    if is_model_disabled_for_user(canonical, user_ctx) or is_model_outside_grant_scope(
        canonical, user_ctx
    ):
        req_ctx.mark_model_not_found()
        raise HTTPException(404, f"Model '{model_id}' not found")
    if not route.adapters:
        raise HTTPException(404, f"Model '{model_id}' has no adapters")
    if not for_dispatch:
        adapter, _ = route.adapters[0]
        return canonical, route, adapter
    return canonical, route, _pick_dispatch_adapter(model_id, canonical, router_exec, user_role)


def _pick_adapter_for_role(adapters, user_role: str):
    """Return the first of ``adapters`` that holds a key *user_role* may spend.

    This surface commits to one adapter up front instead of walking the router's
    fallback chain, so a first adapter whose keys are all reserved above the
    caller would turn an otherwise routable request into a hard 429 — even with a
    perfectly usable second provider on the route. Preferring a serviceable
    adapter keeps tier reservation from costing availability here.

    ``adapters`` is the already-admitted candidate list (see
    ``_pick_dispatch_adapter``), so key tiers only ever reorder providers this
    request was allowed to use in the first place.

    Falls back to the first candidate when none can serve the role, so the
    resulting error is the same one the caller would have seen before: the
    request is genuinely unservable, and the adapter raises the 429 the handler
    already maps.
    """
    for entry in adapters:
        candidate = entry[0]
        has_capacity = getattr(candidate, "has_capacity_for_role", None)
        # Adapters without a key pool (or predating the check) are always eligible.
        if not callable(has_capacity) or has_capacity(user_role):
            return entry
    return adapters[0]


# --- Small-budget reasoning-call reroute -----------------------------------
#
# Agent harnesses (notably Claude Code) issue many tiny auxiliary calls --
# conversation-title generation, topic detection, auto-compaction summaries --
# with a very small ``max_tokens``. When such a call is routed to a *reasoning*
# model, the model spends the entire budget on hidden chain-of-thought and is
# truncated at ``max_tokens`` before emitting any visible content. The client
# gets back ``content: []`` with ``stop_reason: "max_tokens"`` -- no usable
# output -- while we still bill the full (often large) prompt, and the harness
# typically retries, multiplying the waste.
#
# These calls are rerouted to a fast non-reasoning model with an output budget
# large enough to actually answer. The reroute is best-effort: if the target is
# unavailable (not configured, or not visible to the caller) the request is left
# on its original model rather than failed. Set the threshold env var to 0 to
# disable entirely.
_SMALL_MAXTOK_REROUTE_TARGET = os.environ.get("SMALL_MAXTOK_REASONING_TARGET", "qwen3.6-35b")
_SMALL_MAXTOK_THRESHOLD = int(os.environ.get("SMALL_MAXTOK_REASONING_THRESHOLD", "64"))
_SMALL_MAXTOK_FLOOR = int(os.environ.get("SMALL_MAXTOK_REASONING_FLOOR", "512"))
# A model is treated as "reasoning" when it advertises a thinking/reasoning knob.
_REASONING_PARAMS = ("thinking", "reasoning_effort")

# Runtime kill-switch / opt-in. The reroute changes which model serves a class of
# requests, so it is gated on an admin-toggleable runtime setting (default off).
_REROUTE_SETTING_KEY = "reasoning_small_call_reroute_enabled"

# Tool-permission / safety-check calls are deliberately EXCLUDED from the reroute:
# the model that adjudicates "is this action safe to run?" must stay the model the
# caller's agent chose, never silently swapped underneath them. Claude Code's
# permission classifier ships a fixed preamble as its sole user turn; these phrases
# are distinctive to it and do not appear in ordinary coding prompts. When a small
# call carries this signature it is left on its original model. Matched
# case-insensitively.
_TOOL_SAFETY_SIGNATURES = (
    "the specific action under review",
    "must not lower your block threshold",
)


async def _reroute_enabled() -> bool:
    """Return whether the small-budget reroute is enabled (admin runtime toggle).

    Read defensively: if the runtime-settings singleton is not initialized (e.g.
    in unit tests) or the lookup fails, fall back to the registry default.
    """
    from serving.config.runtime_settings import (
        RUNTIME_SETTINGS_REGISTRY,
        get_runtime_settings_instance,
    )

    default = bool(RUNTIME_SETTINGS_REGISTRY[_REROUTE_SETTING_KEY]["default"])
    try:
        return await get_runtime_settings_instance().get_bool(_REROUTE_SETTING_KEY)
    except Exception:
        return default


def _is_reasoning_model(adapter) -> bool:
    """True when the adapter's model advertises a thinking/reasoning parameter."""
    cfg = getattr(adapter, "config", None)
    params = getattr(cfg, "supported_params", None) or ()
    return any(p in params for p in _REASONING_PARAMS)


def _is_tool_safety_check(body: dict[str, Any]) -> bool:
    """True when the request is Claude Code's tool-permission/safety classifier.

    The classifier carries its preamble in the system prompt and/or the first user
    turn; only those are scanned (and only their heads) to keep this cheap.
    """
    parts: list[str] = []
    sysval = body.get("system")
    if isinstance(sysval, str):
        parts.append(sysval[:4000])
    elif isinstance(sysval, list):
        parts.append(" ".join(b.get("text", "") for b in sysval if isinstance(b, dict))[:4000])
    for msg in body.get("messages", []) or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content[:4000])
            elif isinstance(content, list):
                parts.append(
                    " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )[:4000]
                )
            break  # the classifier prompt is the first user turn
    blob = " ".join(parts).lower()
    return any(sig in blob for sig in _TOOL_SAFETY_SIGNATURES)


async def _maybe_reroute_small_reasoning_call(
    canonical: str,
    route,
    adapter,
    body: dict[str, Any],
    router_exec,
    user_ctx: dict | None,
    model_visibility_resolver,
    request_id: str,
):
    """Reroute a tiny-``max_tokens`` call aimed at a reasoning model to a fast model.

    Returns a possibly-updated ``(canonical, route, adapter)``. When a reroute
    applies, ``body["model"]`` and ``body["max_tokens"]`` are mutated in place.
    Any failure to resolve the target leaves everything unchanged.
    """
    max_tokens = body.get("max_tokens")
    # Cheap, in-memory gates first (so ordinary traffic never reads the toggle):
    # only tiny budgets, aimed at a reasoning model, that are NOT tool-safety
    # checks. Safety verdicts are always left on the caller's chosen model.
    if (
        _SMALL_MAXTOK_THRESHOLD <= 0
        or not isinstance(max_tokens, int)
        or max_tokens > _SMALL_MAXTOK_THRESHOLD
        or canonical == _SMALL_MAXTOK_REROUTE_TARGET
        or not _is_reasoning_model(adapter)
        or _is_tool_safety_check(body)
    ):
        return canonical, route, adapter
    # Master switch (admin runtime toggle), checked only once the cheap gates pass.
    if not await _reroute_enabled():
        return canonical, route, adapter
    try:
        new_canonical, new_route, new_adapter = await _resolve(
            _SMALL_MAXTOK_REROUTE_TARGET, router_exec, user_ctx, model_visibility_resolver
        )
    except HTTPException:
        # Target not configured, not visible to this caller, or currently
        # unservable (every provider disabled / circuit open) -- leave the
        # request on its original model rather than failing it.
        return canonical, route, adapter
    body["model"] = new_canonical
    body["max_tokens"] = max(max_tokens, _SMALL_MAXTOK_FLOOR)
    logger.info(
        f"[{request_id}] Rerouted small-budget reasoning call: {canonical} "
        f"(max_tokens={max_tokens}) -> {new_canonical} (max_tokens={body['max_tokens']})"
    )
    return new_canonical, new_route, new_adapter


# --- Inbound header forwarding to upstream ---------------------------------

_FORWARDED_HEADERS = ("anthropic-beta",)


def _extract_forwarded_headers(request: Request) -> dict[str, str]:
    """Extract allowlisted Anthropic headers from the inbound request to forward upstream.

    Only headers in ``_FORWARDED_HEADERS`` are forwarded; auth-related headers
    (``x-api-key``, ``authorization``) are never forwarded because the adapter
    injects its own upstream credentials.
    """
    out: dict[str, str] = {}
    for k in _FORWARDED_HEADERS:
        v = request.headers.get(k)
        if v:
            out[k] = v
    return out


# --- Field sanitization for OpenAI backends --------------------------------


def _sanitize_for_openai_backend(body: dict[str, Any]) -> list[str]:
    """Strip Anthropic-only fields the OpenAI translator can't represent.

    Removes ``cache_control`` from content blocks and pops top-level fields
    (``thinking``, ``top_k``, ``container``) that have no OpenAI equivalent.
    Also detects unsupported ``metadata`` keys beyond ``user_id``.

    Returns sorted list of dropped-field names for warning logging.
    """
    dropped: set[str] = set()
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    block.pop("cache_control")
                    dropped.add("cache_control")
    for k in ("thinking", "top_k", "container"):
        if k in body:
            body.pop(k)
            dropped.add(k)
    metadata = body.get("metadata") or {}
    extra_meta = set(metadata.keys()) - {"user_id"}
    if extra_meta:
        dropped.add(f"metadata.{','.join(sorted(extra_meta))}")
    return sorted(dropped)


def _count_non_object_tool_inputs(body: dict[str, Any]) -> int:
    """Count tool-use inputs that the OpenAI translator will normalize."""
    count = 0
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and not isinstance(block.get("input", {}), dict)
            ):
                count += 1
    return count


# --- DB logging (fire-and-forget) ------------------------------------------


# Retains in-flight cost-increment tasks so the event loop's garbage collector
# can't cancel a fire-and-forget increment before it commits.
_cost_increment_tasks: set[asyncio.Task[Any]] = set()


def _schedule_messages_cost_increment(
    op_store: Any,
    user_id: str | None,
    usage: dict[str, int],
    pricing: dict[str, str] | None,
) -> None:
    """Fire-and-forget the daily quota cost increment for a billed request.

    The ``/v1/messages`` surface logs ``api_logs.cost_usd`` but that column is
    not what ``verify_api_key`` reads for quota enforcement -- the per-user
    daily counter in ``user_daily_cost`` (bumped here via
    ``increment_user_cost``) is. Without this, Anthropic-surface traffic
    (e.g. Claude Code) bypasses the per-user daily cost cap entirely, the same
    way the chat and embedding paths would if they omitted their own
    increments (``CostTracker.schedule_increment`` /
    ``embeddings._schedule_cost_increment``).

    ``usage`` is the OpenAI-shaped dict already built for the log row, so the
    increment reuses the exact same ``calculate_cost`` inputs and stays in
    lockstep with the logged ``cost_usd``. No-ops for unauthenticated callers,
    missing stores, or zero-cost (free) models.
    """
    if op_store is None or not user_id:
        return
    cost = calculate_cost(usage, pricing)
    if not cost or cost <= 0:
        return

    async def _increment() -> None:
        try:
            await op_store.increment_user_cost(user_id, cost)
        except Exception as exc:
            logger.warning(f"Failed to increment messages cost counter for {user_id}: {exc}")
            raise  # let tracked_task record the failure

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    task = tracked_task(_increment(), name="messages_cost_increment")
    _cost_increment_tasks.add(task)
    task.add_done_callback(_cost_increment_tasks.discard)


def _schedule_log_store_task(
    log_store,
    *,
    request_id: str,
    model_id: str,
    provider: str,
    usage: dict[str, int],
    latency_ms: int,
    status_code: int,
    pricing: dict[str, str],
    metadata: dict[str, Any],
    params: dict[str, Any],
    prompt: list[dict[str, Any]] | str | None = None,
    response: dict[str, Any] | str | None = None,
    request_payload: dict[str, Any] | None = None,
    ttft_ms: int | None = None,
    error: str | None = None,
    operator_error: str | None = None,
    op_store: Any = None,
    user_id: str | None = None,
) -> None:
    """Schedule a background log store task (fire-and-forget).

    ``error`` is the user-facing, scrubbed message (also returned to the
    client). ``operator_error`` is the richer operator-facing cause (secrets and
    provider URLs already removed) and is stored in ``metadata.operator_error``
    -- a field no user-facing route returns -- so the real failure cause is
    diagnosable without exposing it to the user.

    ``op_store`` / ``user_id``, when supplied, advance the per-user daily quota
    counter for successful (status 200) requests via
    :func:`_schedule_messages_cost_increment`; error rows are logged only.

    ``usage`` is the upstream Anthropic usage shape with ``input_tokens`` /
    ``output_tokens`` (and optional ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``). To match OpenAI semantics used by the
    rest of the system (admin dashboard, completions logger, downstream
    metrics), ``prompt_tokens`` here is the *total* input including the
    cached subset (input_tokens + cache_read + cache_write). The cached
    subset is also stored in the dedicated ``cache_read_tokens`` /
    ``cache_write_tokens`` columns for separate billing; ``calculate_cost``
    subtracts the cached portion from ``prompt_tokens`` before applying
    ``prompt_price`` so cache is not double-billed.
    """
    if operator_error:
        # Operator-only: stored in metadata, never returned to the user.
        metadata = {**(metadata or {}), "operator_error": operator_error}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    prompt_tokens = input_tokens + cache_read + cache_write
    total_tokens = prompt_tokens + output_tokens
    prompt_for_log: list[dict[str, Any]] | str = prompt if prompt is not None else []

    # OpenAI-shaped usage, shared verbatim by the api_logs row and the quota
    # counter increment below so the logged cost_usd and the billed amount agree.
    usage_for_cost = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }

    async def _log() -> None:
        try:
            await log_store.log_request(
                request_id=request_id,
                model_id=model_id,
                provider=provider,
                prompt=prompt_for_log,
                response=response,
                usage=usage_for_cost,
                latency_ms=latency_ms,
                status_code=status_code,
                params=params,
                metadata=metadata,
                pricing=pricing,
                ttft_ms=ttft_ms,
                error=error,
                request_payload=request_payload,
            )
        except Exception:
            logger.debug(f"Background log store task failed for {request_id}", exc_info=True)

    asyncio.create_task(_log())  # noqa: RUF006

    # Successful, billed requests must also advance the daily quota counter that
    # verify_api_key enforces; error rows (status != 200) are logged but never
    # billed, matching the chat/embedding paths.
    if status_code == 200:
        _schedule_messages_cost_increment(op_store, user_id, usage_for_cost, pricing)


# --- Endpoint health recording ---------------------------------------------


def _is_non_empty_content_event(event_type: str, data: str) -> bool:
    """Return whether one Anthropic SSE event carries generated content.

    The Anthropic-native equivalent of ``routing.streaming.has_non_empty_content``,
    which the chat path uses as its stream success signal: a delta that actually
    produced text, thinking, or tool-call JSON proves the upstream is generating,
    where ``message_start`` and keepalive pings prove only that it answered.

    Best-effort like the accumulator beside it -- a malformed event is not
    content, and never raises into the forwarded stream.
    """
    if event_type != "content_block_delta":
        return False
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    delta = payload.get("delta")
    if not isinstance(delta, dict):
        return False
    return any(delta.get(key) for key in ("text", "thinking", "partial_json"))


# --- Anthropic SSE accumulator ---------------------------------------------


def _apply_sse_event(acc: dict | None, event_type: str, data: str) -> dict | None:
    """Update *acc* in-place given one Anthropic SSE event; return the (possibly new) accumulator.

    Best-effort: any malformed event is swallowed so streaming logging never
    impacts the forwarded client stream.
    """
    if event_type in ("ping", "message_stop", "error", ""):
        return acc
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return acc
    if not isinstance(payload, dict):
        return acc

    try:
        if event_type == "message_start":
            msg = payload.get("message") or {}
            if not isinstance(msg, dict):
                return acc
            return {
                "id": msg.get("id"),
                "type": "message",
                "role": msg.get("role", "assistant"),
                "model": msg.get("model"),
                "content": list(msg.get("content") or []),
                "stop_reason": msg.get("stop_reason"),
                "stop_sequence": msg.get("stop_sequence"),
                "usage": dict(msg.get("usage") or {}),
            }

        if acc is None:
            return acc

        if event_type == "content_block_start":
            idx = payload.get("index")
            block_in = payload.get("content_block")
            if not isinstance(idx, int) or not isinstance(block_in, dict):
                return acc
            block = dict(block_in)
            if block.get("type") == "text":
                block.setdefault("text", "")
            elif block.get("type") == "tool_use":
                block.setdefault("input", {})
                block["_partial_json"] = ""
            content = acc["content"]
            while len(content) <= idx:
                content.append(None)
            content[idx] = block

        elif event_type == "content_block_delta":
            idx = payload.get("index")
            delta = payload.get("delta") or {}
            if not isinstance(idx, int) or not isinstance(delta, dict):
                return acc
            content = acc["content"]
            if 0 <= idx < len(content) and isinstance(content[idx], dict):
                block = content[idx]
                dtype = delta.get("type")
                if dtype == "text_delta":
                    block["text"] = block.get("text", "") + (delta.get("text") or "")
                elif dtype == "input_json_delta":
                    block["_partial_json"] = block.get("_partial_json", "") + (
                        delta.get("partial_json") or ""
                    )
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + (delta.get("thinking") or "")

        elif event_type == "content_block_stop":
            idx = payload.get("index")
            if not isinstance(idx, int):
                return acc
            content = acc["content"]
            if 0 <= idx < len(content) and isinstance(content[idx], dict):
                _finalize_block(content[idx])

        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            if isinstance(delta, dict):
                if "stop_reason" in delta:
                    acc["stop_reason"] = delta["stop_reason"]
                if "stop_sequence" in delta:
                    acc["stop_sequence"] = delta["stop_sequence"]
            extra_usage = payload.get("usage") or {}
            if isinstance(extra_usage, dict):
                if "output_tokens" in extra_usage:
                    acc["usage"]["output_tokens"] = extra_usage["output_tokens"]
                for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                    if k in extra_usage:
                        acc["usage"][k] = extra_usage[k]
    except Exception:
        # Logging must never disrupt the forwarded stream; drop this event.
        pass

    return acc


def _finalize_block(block: dict) -> None:
    """Resolve any partial-JSON buffer on a tool_use block; remove sentinel keys."""
    if block.get("type") == "tool_use" and "_partial_json" in block:
        raw = block.pop("_partial_json")
        try:
            block["input"] = json.loads(raw) if raw else block.get("input") or {}
        except (json.JSONDecodeError, ValueError):
            if raw:
                block["input"] = raw  # type: ignore[assignment]


def _finalize_response_acc(acc: dict | None) -> dict | None:
    """Normalize the accumulator for persistence: finalize partial blocks, drop sentinels."""
    if not isinstance(acc, dict):
        return acc
    for block in acc.get("content") or []:
        if isinstance(block, dict):
            _finalize_block(block)
    return acc


# Above this many characters, estimate with a cheap char heuristic instead of
# tiktoken. Usage recovery runs in the streaming teardown path; coding agents
# send very large contexts, and a full tiktoken encode of hundreds of KB would
# block the event loop. ~400K chars is well past any real prompt's token cap.
_ESTIMATE_CHAR_CAP = 400_000


def _bounded_text_tokens(text: str) -> int:
    """:func:`estimate_text_tokens` with a size cap for the teardown path."""
    if len(text) > _ESTIMATE_CHAR_CAP:
        return max(1, len(text) // 4)
    return estimate_text_tokens(text)


def _estimate_request_input_tokens(request_payload: dict[str, Any] | None) -> int:
    """Estimate prompt tokens from a stored Anthropic request payload.

    Folds the top-level ``system`` prompt into the message list (Anthropic keeps
    it separate from ``messages``) and adds a coarse estimate for tool schemas,
    since coding agents send large tool definitions. Used only as a fallback
    when the upstream never reported usage (see :func:`_resolve_stream_usage`).
    """
    if not isinstance(request_payload, dict):
        return 0
    messages = request_payload.get("messages")
    est_messages: list[dict[str, Any]] = list(messages) if isinstance(messages, list) else []
    system = request_payload.get("system")
    if system:
        # ``system`` is a string or a list of content blocks; both shapes are
        # handled by the content estimator via a synthetic system message.
        est_messages = [{"role": "system", "content": system}, *est_messages]
    # estimate_prompt_tokens is multimodal-safe (it flat-counts image/audio
    # blocks rather than tokenizing base64 payloads), so it is used as-is.
    total = estimate_prompt_tokens(est_messages)
    tools = request_payload.get("tools")
    if isinstance(tools, list) and tools:
        with contextlib.suppress(TypeError, ValueError):
            total += _bounded_text_tokens(json.dumps(tools))
    return total


def _accumulated_output_text(response_acc: dict | None) -> str:
    """Concatenate assistant text/thinking/tool-call JSON from the accumulated response.

    Used for an output-token estimate when the upstream omitted usage. Expects a
    finalized accumulator (tool_use ``input`` already resolved).
    """
    if not isinstance(response_acc, dict):
        return ""
    parts: list[str] = []
    for block in response_acc.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            parts.append(block["thinking"])
        elif btype == "tool_use":
            inp = block.get("input")
            if isinstance(inp, str):
                parts.append(inp)
            elif inp:
                with contextlib.suppress(TypeError, ValueError):
                    parts.append(json.dumps(inp))
    return "".join(parts)


def _resolve_stream_usage(
    request_usage: dict[str, int],
    response_acc: dict | None,
    request_payload: dict[str, Any] | None,
) -> tuple[dict[str, int], bool]:
    """Resolve the usage to log for a streaming request, recovering lost counts.

    When the upstream stream is cut short before the adapter flushes
    ``usage_sink``, the counts are recovered from the forwarded stream or
    estimated rather than logged as zero.

    On a normal completion the adapter populates ``request_usage`` (input +
    output) at end of stream, so it is used verbatim. But when the client
    disconnects mid-stream -- common with coding agents that abort slow requests
    -- ``usage_sink`` is never flushed and ``request_usage`` stays all zero,
    which previously logged 0 input / 0 output even though a prompt was sent and
    tokens may already have been produced.

    Recovery, applied only when ``request_usage`` is empty:
      1. Real partial input/cache usage observed on the forwarded stream
         (``response_acc``'s ``message_start`` -- the native passthrough
         captures these even mid-stream).
      2. Estimate input tokens from the request payload (messages + system).
      3. Output: the larger of any observed count and an estimate from the
         accumulated assistant content.

    Returns ``(usage, estimated)`` where ``usage`` is the Anthropic-shaped dict
    and ``estimated`` is True when any field was filled by estimation.
    """
    base = {
        "input_tokens": int(request_usage.get("input_tokens", 0) or 0),
        "output_tokens": int(request_usage.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": int(request_usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            request_usage.get("cache_creation_input_tokens", 0) or 0
        ),
    }
    # Adapter flushed real usage (normal completion) -> trust it as-is.
    if base["input_tokens"] or base["output_tokens"]:
        return base, False

    estimated = False

    # 1. Real partial input/cache usage captured from the forwarded SSE. The
    #    native passthrough fills message_start.input_tokens (and cache counts)
    #    mid-stream; its output_tokens is only a placeholder, handled in step 3.
    acc_usage = response_acc.get("usage") if isinstance(response_acc, dict) else None
    if isinstance(acc_usage, dict):
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = acc_usage.get(key)
            if isinstance(value, int) and value > 0:
                base[key] = value

    # 2. Estimate input from the request when still unknown (translator path, or
    #    a disconnect before message_start was forwarded).
    if base["input_tokens"] == 0:
        est_in = _estimate_request_input_tokens(request_payload)
        if est_in > 0:
            base["input_tokens"] = est_in
            estimated = True

    # 3. Output tokens. message_start carries only a placeholder count (e.g. 1);
    #    the real total arrives in the terminal message_delta, which never fires
    #    on a mid-stream disconnect. Take the larger of any observed count and an
    #    estimate from the accumulated content, so a long partial response is not
    #    logged as ~1 output token.
    observed_output = 0
    if isinstance(acc_usage, dict):
        value = acc_usage.get("output_tokens")
        if isinstance(value, int) and value > 0:
            observed_output = value
    est_out = _bounded_text_tokens(_accumulated_output_text(response_acc))
    base["output_tokens"] = max(observed_output, est_out)
    if est_out > observed_output:
        estimated = True

    return base, estimated


def _log_failure(
    log_store,
    *,
    request_id: str,
    canonical: str,
    adapter,
    metadata: dict[str, Any],
    params_for_log: dict[str, Any],
    messages_for_log,
    request_payload_for_log: dict[str, Any] | None,
    start: float,
    status_code: int,
    error_message: str,
    operator_error: str | None = None,
) -> None:
    latency_ms = int((time.time() - start) * 1000)
    if log_store:
        _schedule_log_store_task(
            log_store,
            request_id=request_id,
            model_id=canonical,
            provider=adapter.config.provider,
            usage={},
            latency_ms=latency_ms,
            status_code=status_code,
            pricing=adapter.config.pricing,
            metadata=metadata,
            params=params_for_log,
            prompt=messages_for_log,
            response=None,
            error=error_message,
            request_payload=request_payload_for_log,
            operator_error=operator_error,
        )


_SSE_LEFTOVER_CAP = 65536


def _parse_sse_chunk(buffer: bytes, raw: bytes) -> tuple[list[tuple[str, str]], bytes]:
    r"""Extract complete SSE events from *buffer* + *raw*; return (events, leftover).

    Events are delimited by a blank line; both ``\n\n`` and ``\r\n\r\n``
    are recognized. The trailing partial event is returned as *leftover* so the
    caller can prepend it to the next chunk. Leftover is capped at
    ``_SSE_LEFTOVER_CAP`` bytes; a malformed stream without separators will be
    discarded rather than grow without bound.
    """
    buffer = buffer + raw
    events: list[tuple[str, str]] = []
    pos = 0
    while True:
        sep_n = buffer.find(b"\n\n", pos)
        sep_r = buffer.find(b"\r\n\r\n", pos)
        if sep_n >= 0 and (sep_r < 0 or sep_n < sep_r):
            sep, sep_len = sep_n, 2
        elif sep_r >= 0:
            sep, sep_len = sep_r, 4
        else:
            break
        block = buffer[pos:sep].decode("utf-8", errors="replace").strip()
        pos = sep + sep_len
        if not block:
            continue
        event_type = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            events.append((event_type, "\n".join(data_lines)))
    leftover = buffer[pos:]
    if len(leftover) > _SSE_LEFTOVER_CAP:
        leftover = b""
    return events, leftover


# --- Route handler ---------------------------------------------------------


@router.post("/v1/messages", response_model=None)
@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    log_store=Depends(get_log_store),
    op_store=Depends(get_operational_store),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
    _conc=Depends(enforce_user_concurrency),
):
    """Handle Anthropic Messages API requests (non-streaming)."""
    request_id = f"amsg_{int(time.time() * 1_000_000)}"
    start = time.time()

    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Invalid JSON in request body")

    model_id = body.get("model")
    if not model_id:
        return _anthropic_error(400, "Missing required field: model")
    if "messages" not in body:
        return _anthropic_error(400, "Missing required field: messages")
    if "max_tokens" not in body:
        return _anthropic_error(400, "Missing required field: max_tokens")

    # Before dispatch, so this covers the native passthrough as well as the
    # translated path. A native Anthropic upstream is forwarded this body
    # unchanged and rejects an inline `role: "system"` message outright, so
    # normalizing only inside the OpenAI translator would leave the Anthropic
    # routes broken for exactly the clients that send it.
    body = normalize_inline_system(body)

    try:
        canonical, _route, adapter = await _resolve(
            model_id,
            router_exec,
            user_ctx,
            model_visibility_resolver,
        )
    except HTTPException as exc:
        asyncio.create_task(  # noqa: RUF006 — fire-and-forget rejection log
            log_rejection(
                request=request,
                status_code=exc.status_code,
                # _resolve rejects for two different reasons now: the model is
                # not visible to this caller (404), or none of its providers can
                # currently serve (503). Logging both as model_not_found would
                # hide a provider outage inside the not-found counters.
                error_code=(
                    "model_not_found" if exc.status_code == 404 else "no_provider_available"
                ),
                reason=str(exc.detail),
                user={
                    "user_id": user_ctx.get("user_id"),
                    "role": user_ctx.get("role"),
                },
                model_id=model_id,
                prompt=body.get("messages") or "",
            )
        )
        return _anthropic_error(exc.status_code, str(exc.detail))

    request_payload_for_log = copy.deepcopy(body)

    body["model"] = canonical

    # Reroute tiny-budget calls aimed at a reasoning model to a fast model so the
    # request returns usable content instead of an empty max_tokens stop. Mutates
    # body["model"]/["max_tokens"] in place; request_payload_for_log above keeps
    # the original client request intact.
    _orig_model, _orig_max_tokens = canonical, body.get("max_tokens")
    canonical, _route, adapter = await _maybe_reroute_small_reasoning_call(
        canonical,
        _route,
        adapter,
        body,
        router_exec,
        user_ctx,
        model_visibility_resolver,
        request_id,
    )
    reroute_info = (
        {
            "from": _orig_model,
            "to": canonical,
            "reason": "small_max_tokens_reasoning",
            "orig_max_tokens": _orig_max_tokens,
            "new_max_tokens": body.get("max_tokens"),
        }
        if canonical != _orig_model
        else None
    )

    forwarded_headers = _extract_forwarded_headers(request)

    # Endpoint health is read on the way in (adapter admission) but, until this
    # surface recorded its own outcomes, was written only by the chat path. An
    # endpoint served exclusively through /v1/messages -- which is the bulk of
    # Claude Code traffic -- could therefore fail every request without ever
    # opening its breaker. Resolved after the reroute, so the outcome lands on
    # the endpoint that actually served the request.
    health_registry = router_exec.endpoint_health_registry
    dispatch_endpoint_id = endpoint_id_for_adapter(adapter)
    # Register before dispatch so the endpoint appears in the health snapshot
    # even while its first request is still in flight (mirrors FixedRouter).
    health_registry.ensure(dispatch_endpoint_id)

    # Snapshot messages before _sanitize_for_openai_backend mutates them in-place
    # (strips cache_control blocks). The log must preserve the original client payload.
    messages_for_log = copy.deepcopy(body.get("messages"))

    if adapter.native_format == "openai":
        normalized_tool_inputs = _count_non_object_tool_inputs(body)
        if normalized_tool_inputs:
            logger.warning(
                f"[{request_id}] Normalizing {normalized_tool_inputs} non-object "
                "Anthropic tool_use.input value(s) before OpenAI-backed dispatch"
            )
        dropped = _sanitize_for_openai_backend(body)
        if dropped:
            logger.warning(
                f"[{request_id}] Dropped Anthropic-only fields for OpenAI backend: {dropped}"
            )

    ip_info = get_client_ip_info(request)
    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "referer": request.headers.get("referer"),
        "ip": ip_info.client_ip,
        "peer_ip": ip_info.peer_ip,
        "ip_source": ip_info.source,
        "x_forwarded_for": ip_info.x_forwarded_for,
        "x_real_ip": ip_info.x_real_ip,
        "authenticated": bool(user_ctx.get("authenticated")),
        "user_id": user_ctx.get("user_id"),
        "surface": "anthropic_messages",
        "alias_input": model_id if model_id != canonical else None,
        "reroute": reroute_info,
    }
    # Agent-sandbox attribution (issue #1041). This surface is the one Claude
    # Code actually uses, so omitting it here would leave the flagship runtime's
    # spend unattributed — and the per-job budget reads this same ledger.
    if user_ctx.get("agent_job_id"):
        metadata["agent_job_id"] = user_ctx["agent_job_id"]

    params_for_log: dict[str, Any] = {"surface": "anthropic_messages"}
    for k in ("temperature", "top_p", "max_tokens", "stop_sequences", "stream"):
        if k in body:
            params_for_log[k] = body[k]
    if body.get("tools"):
        params_for_log["tools"] = request_payload_for_log["tools"]
        params_for_log["tool_count"] = len(body["tools"])

    is_streaming = bool(body.get("stream"))
    if is_streaming:
        from fastapi.responses import StreamingResponse

        sse_headers = {
            # `no-transform` stops intermediary CDNs (e.g. Cloudflare) from
            # buffering the stream to compress it, which collapses TTFT.
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }

        async def _gen():
            request_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            stream_failed = False
            stream_status_code: int = 200
            stream_error_message: str | None = None
            stream_error_operator: str | None = None
            # Whether this stream's success has already been reported to the
            # health registry. Recorded once, on the first content delta.
            health_success_recorded = False
            ttft_ms: int | None = None
            ttft_buffer = b""
            response_acc: dict | None = None
            sse_buffer = b""
            try:
                upstream = adapter.stream_messages(
                    body,
                    request_id=request_id,
                    usage_sink=request_usage,
                    extra_headers=forwarded_headers,
                )
                # Consume the upstream via a queue + background reader so idle
                # gaps can be filled with keepalive heartbeats without cancelling
                # the upstream read (see _KEEPALIVE_INTERVAL / _MAX_STREAM_IDLE).
                # The queue MUST stay unbounded: a bounded queue can wedge teardown
                # -- if a fast upstream fills it while the client is gone, the
                # reader parks on put(), and on cancel its finally sentinel-put
                # blocks forever on the full queue, leaking the upstream connection.
                chunk_queue: asyncio.Queue = asyncio.Queue()

                async def _reader() -> None:
                    try:
                        async for c in upstream:
                            await chunk_queue.put(c)
                    except Exception as exc:
                        # Forward upstream errors to the main loop to re-raise.
                        await chunk_queue.put(exc)
                    finally:
                        await chunk_queue.put(_STREAM_SENTINEL)

                reader_task = asyncio.create_task(_reader())
                idle_seconds = 0.0
                try:
                    while True:
                        try:
                            item = await asyncio.wait_for(
                                chunk_queue.get(), timeout=_KEEPALIVE_INTERVAL
                            )
                        except asyncio.TimeoutError:
                            idle_seconds += _KEEPALIVE_INTERVAL
                            if idle_seconds >= _MAX_STREAM_IDLE:
                                stream_failed = True
                                stream_status_code = 504
                                stream_error_message = (
                                    f"Upstream sent no data for {_MAX_STREAM_IDLE}s"
                                )
                                stream_error_operator = stream_error_message
                                logger.warning(
                                    f"[{request_id}] Stream idle timeout after "
                                    f"{_MAX_STREAM_IDLE}s; aborting"
                                )
                                # An upstream that went silent for the whole
                                # ceiling is a genuine fault, even though it
                                # arrives as a timer rather than an exception.
                                # No ``exc``: there is no HTTP status, so the
                                # registry's client-error exemption is moot.
                                health_registry.record_failure(
                                    dispatch_endpoint_id,
                                    reason="messages_stream_idle",
                                    detail=stream_error_operator,
                                )
                                err = {
                                    "type": "error",
                                    "error": {
                                        "type": "api_error",
                                        "message": stream_error_message,
                                    },
                                }
                                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
                                break
                            # Heartbeat so a slow stream isn't dropped by the client.
                            yield b": keepalive\n\n"
                            continue
                        idle_seconds = 0.0
                        if item is _STREAM_SENTINEL:
                            break
                        if isinstance(item, Exception):
                            raise item
                        chunk = item
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        if ttft_ms is None:
                            ttft_buffer += chunk
                            nl = ttft_buffer.rfind(b"\n")
                            if nl >= 0:
                                head = ttft_buffer[: nl + 1]
                                ttft_buffer = ttft_buffer[nl + 1 :]
                                if b"event: content_block_delta" in head:
                                    ttft_ms = int((time.time() - start) * 1000)
                                    ttft_buffer = b""
                            elif len(ttft_buffer) > 16384:
                                ttft_buffer = b""
                        events, sse_buffer = _parse_sse_chunk(sse_buffer, chunk)
                        for event_type, data in events:
                            response_acc = _apply_sse_event(response_acc, event_type, data)
                            if not health_success_recorded and _is_non_empty_content_event(
                                event_type, data
                            ):
                                # Same success signal as
                                # FixedRouter.stream_chat_completion: the first
                                # delta that carries content, not the mere fact
                                # that the upstream accepted the connection.
                                health_success_recorded = True
                                health_registry.record_success(dispatch_endpoint_id)
                        yield chunk
                finally:
                    reader_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader_task
            except aiohttp.ClientResponseError as exc:
                stream_failed = True
                # On a 200 SSE stream the error event's `error.type` is the only
                # signal the client gets to classify the failure, so map the
                # upstream status to the right Anthropic type (429 ->
                # rate_limit_error, 503 -> overloaded_error) instead of a flat
                # api_error. Upstream 401/403 are remapped to api_error so the
                # client doesn't treat the operator's key failure as its own.
                client_status, err_type = _map_upstream_status(exc.status)
                # Log the client-facing status; the true upstream status stays
                # in stream_error_operator.
                stream_status_code = client_status
                scrub_exc = exc if client_status == exc.status else None
                stream_error_message = scrub_error_for_user(scrub_exc, request_id, client_status)
                stream_error_operator = operator_safe_error(exc)
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                # ``exc=`` so the registry can apply its client-error exemption:
                # a 400 from a malformed request is one caller's mistake and
                # must not open the circuit for everyone.
                health_registry.record_failure(
                    dispatch_endpoint_id,
                    reason="messages_stream_exception",
                    detail=stream_error_operator,
                    exc=exc,
                )
                err = {
                    "type": "error",
                    "error": {
                        "type": err_type,
                        "message": stream_error_message,
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
            except KeyPoolExhausted as exc:
                stream_failed = True
                stream_status_code = 429
                stream_error_message = scrub_error_for_user(None, request_id, 429)
                stream_error_operator = operator_safe_error(exc)
                logger.warning(f"[{request_id}] Streaming dispatch failed: key pool exhausted")
                # Carries no HTTP status, so it is never exempt: every key for
                # this endpoint is muted and nothing it is sent can succeed.
                health_registry.record_failure(
                    dispatch_endpoint_id,
                    reason="messages_stream_exception",
                    detail=stream_error_operator,
                    exc=exc,
                )
                err = {
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": stream_error_message,
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
            except Exception as exc:
                # Deliberately not BaseException: a client disconnect arrives as
                # CancelledError and must pass straight through. The client gave
                # up, the upstream did not fail, and counting it would let a
                # flaky network open the circuit for every other caller.
                stream_failed = True
                stream_status_code = 502
                stream_error_message = scrub_error_for_user(exc, request_id, 502)
                stream_error_operator = operator_safe_error(exc)
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                health_registry.record_failure(
                    dispatch_endpoint_id,
                    reason="messages_stream_exception",
                    detail=stream_error_operator,
                    exc=exc,
                )
                err = {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": stream_error_message,
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
            finally:
                latency_ms = int((time.time() - start) * 1000)
                if log_store:
                    final_acc = _finalize_response_acc(response_acc)
                    # A client disconnect (CancelledError) skips the adapter's
                    # end-of-stream usage_sink flush, so request_usage is still
                    # all zero here. Recover the best-available counts rather
                    # than logging a 0-token row. Failed streams keep their
                    # zeros: don't attribute usage/cost to an errored request.
                    try:
                        if stream_failed:
                            resolved_usage, usage_estimated = request_usage, False
                        else:
                            resolved_usage, usage_estimated = _resolve_stream_usage(
                                request_usage, final_acc, request_payload_for_log
                            )
                    except Exception:
                        # Usage recovery must never cost us the log row itself.
                        logger.debug(
                            f"[{request_id}] stream usage resolution failed", exc_info=True
                        )
                        resolved_usage, usage_estimated = request_usage, False
                    log_metadata = (
                        {**metadata, "usage_estimated": True} if usage_estimated else metadata
                    )
                    # A well-formed 200 stream can still end with no deliverable
                    # content (empty text/thinking, no tool_use) -- no exception,
                    # but nothing for the user either. Seen on zai/minimax at
                    # meaningful volume; flag it so these rows are distinguishable
                    # from a normal completion instead of blending in silently.
                    if not stream_failed and not _accumulated_output_text(final_acc).strip():
                        log_metadata = {**log_metadata, "empty_completion": True}
                    _schedule_log_store_task(
                        log_store,
                        request_id=request_id,
                        model_id=canonical,
                        provider=adapter.config.provider,
                        usage=resolved_usage,
                        latency_ms=latency_ms,
                        status_code=stream_status_code,
                        pricing=adapter.config.pricing,
                        metadata=log_metadata,
                        params=params_for_log,
                        prompt=messages_for_log,
                        response=final_acc,
                        request_payload=request_payload_for_log,
                        ttft_ms=ttft_ms,
                        error=stream_error_message if stream_failed else None,
                        operator_error=stream_error_operator if stream_failed else None,
                        op_store=op_store,
                        user_id=user_ctx.get("user_id"),
                    )

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=sse_headers)

    try:
        resp = await adapter.messages(body, request_id=request_id, extra_headers=forwarded_headers)
    except HTTPException as exc:
        error_message = str(exc.detail)
        # ``exc=`` throughout: HTTPException/ClientResponseError carry a status,
        # and the registry drops 4xx client errors on that basis so one caller's
        # malformed request cannot open the circuit for everyone. The
        # status-less failures below (key pool, timeout) are never exempt.
        health_registry.record_failure(
            dispatch_endpoint_id,
            reason="messages_exception",
            detail=operator_safe_error(exc),
            exc=exc,
        )
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            request_payload_for_log=request_payload_for_log,
            start=start,
            status_code=exc.status_code,
            error_message=error_message,
            operator_error=operator_safe_error(exc),
        )
        return _anthropic_error(exc.status_code, error_message)
    except aiohttp.ClientResponseError as exc:
        client_status, err_type = _map_upstream_status(exc.status)
        # For a remapped upstream auth/permission failure, suppress the
        # provider's message (it would wrongly implicate the user's key) and
        # use a generic gateway message; otherwise surface the scrubbed body.
        scrub_exc = exc if client_status == exc.status else None
        error_message = scrub_error_for_user(scrub_exc, request_id, client_status)
        logger.exception(f"[{request_id}] Adapter messages() failed")
        health_registry.record_failure(
            dispatch_endpoint_id,
            reason="messages_exception",
            detail=operator_safe_error(exc),
            exc=exc,
        )
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            request_payload_for_log=request_payload_for_log,
            start=start,
            # Log the client-facing status so the row matches what the client
            # saw; the true upstream status survives in operator_error.
            status_code=client_status,
            error_message=error_message,
            operator_error=operator_safe_error(exc),
        )
        return _anthropic_error(client_status, error_message, error_type=err_type)
    except KeyPoolExhausted as exc:
        # Every upstream key is in cooldown (typically after provider 429/401
        # muted them). This is a rate-limit condition, not an internal error:
        # return 429 rate_limit_error so Claude Code applies backoff instead of
        # hammering with immediate retries for the whole mute window.
        error_message = scrub_error_for_user(None, request_id, 429)
        logger.warning(f"[{request_id}] Adapter messages() failed: key pool exhausted")
        health_registry.record_failure(
            dispatch_endpoint_id,
            reason="messages_exception",
            detail=operator_safe_error(exc),
            exc=exc,
        )
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            request_payload_for_log=request_payload_for_log,
            start=start,
            status_code=429,
            error_message=error_message,
            operator_error=operator_safe_error(exc),
        )
        return _anthropic_error(
            429, error_message, headers={"retry-after": str(int(KeyPool.MUTE_SECONDS))}
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        # Upstream exceeded the (generous) completion timeout. Surface a 504
        # gateway-timeout rather than a generic 502 "Internal server error" so
        # the client can tell a slow upstream from a real server fault.
        error_message = scrub_error_for_user(None, request_id, 504)
        logger.warning(f"[{request_id}] Adapter messages() timed out")
        health_registry.record_failure(
            dispatch_endpoint_id,
            reason="messages_exception",
            detail=operator_safe_error(exc),
            exc=exc,
        )
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            request_payload_for_log=request_payload_for_log,
            start=start,
            status_code=504,
            error_message=error_message,
            operator_error=operator_safe_error(exc),
        )
        return _anthropic_error(504, error_message)
    except Exception as exc:
        # Not BaseException: a client disconnect cancels this coroutine, and
        # CancelledError must propagate uncounted -- the upstream did not fail.
        error_message = scrub_error_for_user(exc, request_id, 502)
        logger.exception(f"[{request_id}] Adapter messages() failed")
        health_registry.record_failure(
            dispatch_endpoint_id,
            reason="messages_exception",
            detail=operator_safe_error(exc),
            exc=exc,
        )
        _log_failure(
            log_store,
            request_id=request_id,
            canonical=canonical,
            adapter=adapter,
            metadata=metadata,
            params_for_log=params_for_log,
            messages_for_log=messages_for_log,
            request_payload_for_log=request_payload_for_log,
            start=start,
            status_code=502,
            error_message=error_message,
            operator_error=operator_safe_error(exc),
        )
        return _anthropic_error(502, error_message)

    # Every branch above returns, so reaching here means the adapter produced a
    # response. Recorded before logging so a slow log store can't delay the
    # recovery signal that closes an open circuit.
    health_registry.record_success(dispatch_endpoint_id)

    usage = (resp.get("usage") or {}) if isinstance(resp, dict) else {}
    usage_for_log = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
    }
    latency_ms = int((time.time() - start) * 1000)
    provider = adapter.config.provider
    if log_store:
        _schedule_log_store_task(
            log_store,
            request_id=request_id,
            model_id=canonical,
            provider=provider,
            usage=usage_for_log,
            latency_ms=latency_ms,
            status_code=200,
            pricing=adapter.config.pricing,
            metadata=metadata,
            params=params_for_log,
            prompt=messages_for_log,
            response=resp if isinstance(resp, dict) else None,
            request_payload=request_payload_for_log,
            op_store=op_store,
            user_id=user_ctx.get("user_id"),
        )
    return JSONResponse(content=resp)


@router.post("/v1/messages/count_tokens", response_model=None)
@router.post("/anthropic/v1/messages/count_tokens", response_model=None)
async def anthropic_count_tokens(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    model_visibility_resolver=Depends(get_model_visibility_resolver),
):
    """Handle Anthropic ``POST /v1/messages/count_tokens`` requests.

    Claude Code calls this for context-window accounting and auto-compaction
    thresholds. There is no universal upstream token-counter across the
    OpenAI-compatible backends, so the count is estimated locally from the
    translated request (a useful estimate beats the 404 the client got before,
    which left its context tracking blind). Returns ``{"input_tokens": N}``.
    """
    from serving.adapters.anthropic_translator import anthropic_request_to_openai

    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Invalid JSON in request body")

    model_id = body.get("model")
    if not model_id:
        return _anthropic_error(400, "Missing required field: model")
    if "messages" not in body:
        return _anthropic_error(400, "Missing required field: messages")

    # Enforce the same model visibility as /v1/messages: an unknown, admin-only,
    # or per-user-disabled model must 404 here too, so token counting can't be
    # used to probe hidden models or make an unusable model look available.
    # Visibility only: the count is computed locally, so a provider outage must
    # not blind the client's context tracking.
    try:
        await _resolve(
            model_id, router_exec, user_ctx, model_visibility_resolver, for_dispatch=False
        )
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))

    try:
        oai_messages, oai_params = anthropic_request_to_openai(body)
        input_tokens = estimate_prompt_tokens(oai_messages)
        # Tool schemas are billed as input; approximate their serialized size.
        tools = oai_params.get("tools")
        if tools:
            input_tokens += estimate_text_tokens(json.dumps(tools))
    except Exception:
        logger.exception("count_tokens estimation failed")
        return _anthropic_error(500, "Failed to count tokens")

    return JSONResponse(content={"input_tokens": int(input_tokens)})


@router.get("/anthropic/user/balance")
async def anthropic_user_balance(
    user_ctx: dict = Depends(verify_api_key_for_balance),
):
    """Return the caller's remaining daily quota as an Anthropic-surface balance check.

    This gateway has no persistent prepaid balance -- quota is a per-user
    daily USD allowance that resets at UTC midnight. This endpoint exists for
    Anthropic-compatible clients that, when pointed at a custom
    ``ANTHROPIC_BASE_URL``, probe a conventional ``/user/balance`` path (as
    popularized by DeepSeek's API) for a status display. Deliberately uses
    :func:`verify_api_key_for_balance` rather than :func:`verify_api_key` so
    that checking a near-zero balance never itself fails with a quota-exceeded
    error.
    """
    if not user_ctx.get("authenticated"):
        # Auth disabled -- no per-user quota is tracked or enforced.
        return JSONResponse(
            content={
                "is_available": True,
                "currency": "USD",
                "balance_usd": None,
                "daily_limit_usd": None,
                "spent_today_usd": None,
                "reset_at": None,
            }
        )

    daily_limit = float(user_ctx.get("quota_daily_cost_usd") or 0.0)
    spent_today = float(user_ctx.get("spent_today_usd") or 0.0)
    # Round before comparing so is_available can't say True while balance_usd
    # displays as 0.0 (e.g. a remainder of 0.00001 rounds down to 0.0).
    remaining = round(max(0.0, daily_limit - spent_today), 4)
    return JSONResponse(
        content={
            "is_available": remaining > 0,
            "currency": "USD",
            "balance_usd": remaining,
            "daily_limit_usd": round(daily_limit, 4),
            "spent_today_usd": round(spent_today, 4),
            "reset_at": _next_utc_midnight().isoformat(),
        }
    )
