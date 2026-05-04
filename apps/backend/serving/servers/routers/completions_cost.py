"""Pricing lookup with caching for the chat-completions handler.

Replaces the duplicated ``get_pricing_for_provider`` closure and the four
inline pricing-lookup blocks scattered across the streaming success /
streaming error / non-streaming / non-streaming-cost paths in the prior
monolithic completions.py. A single ``PricingLookup.for_routing`` call is
cached by ``endpoint_id`` (fallback ``(provider, base_url)``) and produces
the typed :class:`~serving.servers.routers.routing_info.Pricing` consumed
by ``CostTracker`` (added in the next commit, same file).

The handler keeps a single source of truth for the raw adapter pricing dict
(needed by ``LogStore.log_request`` for the api_logs row) by way of
``PricingLookup.raw_dict_for_routing``: same cache, dict-shaped output.
"""

from __future__ import annotations

from typing import Any

from serving.servers.routers.routing_info import Pricing, RoutingInfo
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class PricingLookup:
    """Cache adapter pricing keyed by endpoint_id (fallback: provider+base_url).

    Adapters are immutable after :func:`bootstrap.initialize`, so the cache
    lives for the lifetime of the process. ``invalidate()`` is a future-only
    extension if hot-reload ever lands.
    """

    def __init__(self, *, router: Any) -> None:
        """Construct with the active route executor.

        Args:
            router: The :class:`routing.executor.RouteExecutor` whose
                ``.routes`` mapping holds the registered adapters. The
                lookup walks ``router.routes[model].adapters`` to find the
                config that matches the routing target.
        """
        self._router = router
        self._dict_cache: dict[tuple[str, str | None], dict[str, str] | None] = {}

    # -- public API ----------------------------------------------------------

    def for_routing(self, routing: RoutingInfo) -> Pricing | None:
        """Return the typed :class:`Pricing` for *routing*, or ``None``.

        Resolution order:

        1. Adapter-emitted dict in ``routing.extra["pricing"]`` (some
           adapters override their registered pricing per-call).
        2. Registry walk by ``routing.endpoint_id``.
        3. Registry walk by ``(provider, base_url)``.
        """
        embedded = self._embedded_pricing_dict(routing)
        if embedded is not None:
            return _pricing_from_dict(embedded)

        raw = self._lookup_raw_dict(routing)
        if raw is None:
            return None
        return _pricing_from_dict(raw)

    def raw_dict_for_routing(self, routing: RoutingInfo) -> dict[str, str] | None:
        """Return the raw adapter pricing dict for the api_logs payload.

        ``LogStore.log_request`` consumes the dict directly via
        ``calculate_cost``; preserving the dict shape keeps the api_logs
        ``cost_usd`` column byte-for-byte stable across this refactor.
        """
        embedded = self._embedded_pricing_dict(routing)
        if embedded is not None:
            return embedded
        return self._lookup_raw_dict(routing)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _embedded_pricing_dict(routing: RoutingInfo) -> dict[str, str] | None:
        """Pull adapter-emitted pricing dict from ``routing.extra``."""
        embedded = routing.extra.get("pricing")
        if isinstance(embedded, dict) and embedded:
            return embedded  # type: ignore[return-value]
        return None

    @staticmethod
    def _cache_key(routing: RoutingInfo) -> tuple[str, str | None] | None:
        """Build a stable cache key. ``None`` means "uncacheable" (no info)."""
        if routing.endpoint_id:
            return ("ep", routing.endpoint_id)
        if routing.provider:
            return ("pb", f"{routing.provider}|{routing.base_url or ''}")
        return None

    def _lookup_raw_dict(self, routing: RoutingInfo) -> dict[str, str] | None:
        """Resolve the registered adapter dict, with caching."""
        key = self._cache_key(routing)
        if key is None:
            return None
        if key in self._dict_cache:
            return self._dict_cache[key]

        raw = self._walk_routes_for_pricing_dict(routing)
        self._dict_cache[key] = raw
        return raw

    def _walk_routes_for_pricing_dict(self, routing: RoutingInfo) -> dict[str, str] | None:
        """Walk ``router.routes[model].adapters`` to find a matching pricing dict.

        Mirrors the prior ``get_pricing_for_provider`` heuristic from
        completions.py: match by provider, optionally narrow by base_url,
        and (now) prefer an adapter whose ``endpoint_id`` matches when set.
        """
        if not self._router or routing.model not in getattr(self._router, "routes", {}):
            return None
        route_config = self._router.routes[routing.model]

        # Phase 1: prefer endpoint_id match when present (adapters set this
        # explicitly to disambiguate same-provider, multi-key configs).
        if routing.endpoint_id:
            for adapter, _ in route_config.adapters:
                config = getattr(adapter, "config", None)
                if config is None:
                    continue
                if getattr(config, "endpoint_id", None) == routing.endpoint_id:
                    pricing = getattr(config, "pricing", None)
                    if isinstance(pricing, dict) and pricing:
                        return pricing
                    return None

        # Phase 2: fall back to provider (+ optional base_url narrowing).
        provider = routing.provider
        base_url = routing.base_url
        if not provider:
            return None
        for adapter, _ in route_config.adapters:
            config = getattr(adapter, "config", None)
            if config is None:
                continue
            if getattr(config, "provider", None) != provider:
                continue
            if (
                base_url
                and getattr(config, "base_url", None)
                and config.base_url != base_url
            ):
                continue
            pricing = getattr(config, "pricing", None)
            if isinstance(pricing, dict) and pricing:
                return pricing
            return None
        return None


def _pricing_from_dict(raw: dict[str, Any]) -> Pricing | None:
    """Convert an adapter pricing dict to typed :class:`Pricing`.

    Returns ``None`` when ``prompt`` or ``completion`` is missing or fails
    to parse as a float — matches the defensive behavior of
    ``serving.storage.utils.calculate_cost`` which returns ``None`` on
    parse failure.
    """
    try:
        prompt = float(raw.get("prompt", "0"))
        completion = float(raw.get("completion", "0"))
        cache_read = float(raw.get("input_cache_reads", "0") or 0)
        cache_write = float(raw.get("input_cache_writes", "0") or 0)
    except (TypeError, ValueError):
        return None
    if "prompt" not in raw and "completion" not in raw:
        return None
    return Pricing(
        prompt_price=prompt,
        completion_price=completion,
        cache_read_price=cache_read,
        cache_write_price=cache_write,
    )
