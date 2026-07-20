"""Telemetry helpers shared by routing implementations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from routing.endpoints import endpoint_id_for_adapter

if TYPE_CHECKING:
    from serving.adapters.base import BaseAdapter


def failed_attempt(
    adapter: BaseAdapter | None,
    exc: BaseException,
) -> dict[str, str]:
    """Return fallback-attempt telemetry."""
    if adapter is None:
        provider = "unknown-backup"
        endpoint_id = "unknown-backup"
    else:
        provider = str(adapter.config.provider)
        endpoint_id = str(endpoint_id_for_adapter(adapter))
    return {
        "provider": provider,
        "endpoint_id": endpoint_id,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


def routing_chunk(
    adapter: BaseAdapter,
    *,
    fallback: bool = False,
    failed_attempts: list[dict[str, str]] | None = None,
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
    """
    routing: dict[str, Any] = {
        "provider": adapter.config.provider,
        "base_url": adapter.config.base_url,
        "endpoint_id": getattr(adapter.config, "endpoint_id", None),
    }
    if fallback:
        routing["fallback"] = True
    if failed_attempts:
        routing["failed_attempts"] = failed_attempts
    return f"data: {json.dumps({'choices': [], '_routing': routing})}\n\n"
