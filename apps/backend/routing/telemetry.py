"""Telemetry helpers shared by routing implementations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from routing.endpoint_health import _uncharged_failure_event, http_status_of
from routing.endpoints import endpoint_id_for_adapter, endpoint_publishable_for_adapter
from serving.pricing import effective_pricing

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter


def is_uncharged_failure(exc: BaseException | None) -> bool:
    """Return whether ``exc`` ended an attempt with no evidence against its endpoint.

    The breaker's own classification (``endpoint_health``): nothing was sent --
    no outbound slot (``UpstreamSaturated``), no usable key (``KeyPoolExhausted``)
    -- or the endpoint answered exactly as designed. Read from one place so the
    breaker and a learning router never disagree about which failures are the
    endpoint's.
    """
    return exc is not None and _uncharged_failure_event(exc) is not None


def failed_attempt(
    adapter: BaseAdapter | None,
    exc: BaseException,
) -> dict[str, Any]:
    """Return fallback-attempt telemetry.

    ``status_code`` is present when the failure carried an upstream HTTP status,
    so whoever reads the attempt later -- a learning router's feedback, the
    request log -- can tell a refused request from an endpoint fault.
    ``uncharged`` is present, and True, for a failure no endpoint is charged
    with (``is_uncharged_failure``), so the same readers can leave it out.
    """
    if adapter is None:
        provider = "unknown-backup"
        endpoint_id = "unknown-backup"
    else:
        provider = str(adapter.config.provider)
        endpoint_id = str(endpoint_id_for_adapter(adapter))
    attempt: dict[str, Any] = {
        "provider": provider,
        "endpoint_id": endpoint_id,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }
    status = http_status_of(exc)
    if status is not None:
        attempt["status_code"] = status
    if is_uncharged_failure(exc):
        attempt["uncharged"] = True
    return attempt


def adapter_pricing_snapshot(adapter: BaseAdapter) -> dict[str, str]:
    """Copy the executing adapter's price instead of re-reading the live route.

    Runtime edits replace adapters, while in-flight calls retain their original
    object. Carry its price through settlement, including an empty dict for an
    unpriced attempt so a later route edit cannot supply a different price.
    Scheduled pricing uses the request's existing pricing instant and snapshot.
    """
    return dict(effective_pricing(adapter.config) or {})


def routing_chunk(
    adapter: BaseAdapter,
    *,
    fallback: bool = False,
    failed_attempts: list[dict[str, Any]] | None = None,
    offload: str | None = None,
    offload_endpoint_id: str | None = None,
) -> str:
    """Build a synthetic SSE chunk carrying ``_routing`` metadata for streaming.

    Mirrors the ``resp["_routing"]`` injection used by ``chat_completion`` so
    the ``completions`` router can recover the actual upstream provider,
    base_url, and endpoint_id during streaming. Without this, the request
    context's ``provider`` (set inside ``_execute_stream_adapter``) is
    invisible to the parent coroutine when the adapter stream is consumed
    via an ``asyncio.create_task`` reader, and api_logs end up with
    ``provider="router"`` and ``cost_usd=NULL``.

    The chunk is emitted before any adapter chunks so the completions router
    sees routing info on the very first iteration. ``sanitize_chunk`` pops
    ``_routing`` before forwarding to the client, so users never see this
    field on the wire.

    ``offload`` says why the request was sent to the model's offload route
    (``routing.offload.OFFLOAD_QUEUE_WAIT``, ``OFFLOAD_ENGINE_WAIT`` or
    ``OFFLOAD_LAST_RESORT``), the same marker the non-streaming path puts on
    ``resp["_routing"]``. ``offload_endpoint_id`` names that route when this
    attempt is another one, tried after the offload route failed.
    """
    routing: dict[str, Any] = {
        "provider": adapter.config.provider,
        "base_url": adapter.config.base_url,
        "endpoint_id": getattr(adapter.config, "endpoint_id", None),
        "pricing": adapter_pricing_snapshot(adapter),
        # The serving surface reads publishability from the routing contract,
        # so every surface answers the same question the same way instead of
        # re-deriving it from provider and base URL.
        "endpoint_publishable": endpoint_publishable_for_adapter(adapter),
    }
    if fallback:
        routing["fallback"] = True
    if failed_attempts:
        routing["failed_attempts"] = failed_attempts
    if offload:
        routing["offload"] = offload
    if offload_endpoint_id:
        routing["offload_endpoint_id"] = offload_endpoint_id
    return f"data: {json.dumps({'choices': [], '_routing': routing})}\n\n"
