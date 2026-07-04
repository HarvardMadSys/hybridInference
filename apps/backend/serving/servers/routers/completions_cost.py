"""Pricing lookup with caching and asynchronous cost-counter increments.

Replaces two duplicated concerns from the prior monolithic completions.py:

- ``get_pricing_for_provider`` and the four inline pricing-lookup blocks
  scattered across the streaming success / streaming error / non-streaming /
  non-streaming-cost paths. Now a single ``PricingLookup.for_routing`` call
  cached by ``endpoint_id`` (fallback ``(provider, base_url)``).
- ``_schedule_cost_increment`` — moved into ``CostTracker.schedule_increment``
  with a typed ``Pricing`` input and byte-for-byte cost-math parity vs.
  ``serving.storage.utils.calculate_cost`` (covered by an explicit
  parametric equivalence test).

The handler keeps a single source of truth for the raw adapter pricing dict
(needed by ``LogStore.log_request`` for the api_logs row) by way of
``PricingLookup.raw_dict_for_routing``: same cache, dict-shaped output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.observability.tracked_tasks import tracked_task
from serving.servers.routers.routing_info import Pricing, RoutingInfo
from serving.storage.utils import billable_output_tokens
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    import asyncio

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
        self._typed_cache: dict[tuple[str, str | None], Pricing | None] = {}

    # -- public API ----------------------------------------------------------

    def for_routing(self, routing: RoutingInfo) -> Pricing | None:
        """Return the typed :class:`Pricing` for *routing*, or ``None``.

        Resolution order:

        1. Adapter-emitted dict in ``routing.extra["pricing"]`` (some
           adapters override their registered pricing per-call).
        2. Registry walk by ``routing.endpoint_id`` / ``(provider, base_url)``
           — typed result is cached so float parsing only happens once.
        """
        embedded = self._embedded_pricing_dict(routing)
        if embedded is not None:
            return _pricing_from_dict(embedded)

        key = self._cache_key(routing)
        if key is not None and key in self._typed_cache:
            return self._typed_cache[key]

        raw = self._lookup_raw_dict(routing)
        typed = _pricing_from_dict(raw) if raw is not None else None
        if key is not None:
            self._typed_cache[key] = typed
        return typed

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
            if base_url and getattr(config, "base_url", None) and config.base_url != base_url:
                continue
            pricing = getattr(config, "pricing", None)
            if isinstance(pricing, dict) and pricing:
                return pricing
            return None
        return None


def _pricing_from_dict(raw: dict[str, Any]) -> Pricing | None:
    """Convert an adapter pricing dict to typed :class:`Pricing`.

    Returns ``None`` when both ``prompt`` and ``completion`` are absent, or
    when any value fails to parse as a float. A dict with only one of the two
    keys present is accepted (the missing key defaults to 0).
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


class CostTracker:
    """Compute upstream cost from token usage and fire-and-forget the DB increment.

    Replaces the module-level ``_schedule_cost_increment`` helper from
    completions.py. The cost-math is centralized in ``_compute_cost`` and
    is byte-for-byte equivalent to ``serving.storage.utils.calculate_cost``
    given the same usage and the typed :class:`Pricing` parsed from the
    same adapter dict.
    """

    def __init__(self, *, op_store: Any, pricing: PricingLookup) -> None:
        """Construct with the operational store and pricing lookup.

        Args:
            op_store: Any object exposing
                ``async increment_user_cost(user_id, cost_usd)``.
                ``None``-tolerant: if there's no operational store
                configured the increment is skipped silently.
            pricing: The shared :class:`PricingLookup` instance. Used
                only as a fallback when ``routing.pricing`` is ``None``.
        """
        self._op_store = op_store
        self._pricing = pricing
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def schedule_increment(
        self,
        *,
        user_id: str,
        routing: RoutingInfo,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> RoutingInfo:
        """Compute cost, schedule the DB increment, return enriched routing.

        Returns a *new* :class:`RoutingInfo` (frozen dataclass) with
        ``upstream_cost_usd`` populated when pricing is available; the
        increment is dispatched as a background task so the response path
        is never blocked. If pricing is unavailable, returns ``routing``
        unchanged.

        The increment is skipped when the computed cost is non-positive
        (matches the prior ``_schedule_cost_increment`` semantics — auth
        failures and 4xx error paths never reach this method, but freebie
        models with zero pricing legitimately produce zero cost).
        """
        pricing = routing.pricing or self._pricing.for_routing(routing)
        if pricing is None:
            return routing

        cost = self._compute_cost(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            pricing=pricing,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )

        # Reflect cost back into routing regardless of whether we schedule
        # the increment, so the log payload sees a consistent value.
        import dataclasses as _dc

        enriched = _dc.replace(routing, pricing=pricing, upstream_cost_usd=cost)

        if cost is None or cost <= 0 or self._op_store is None:
            return enriched

        async def _increment() -> None:
            try:
                await self._op_store.increment_user_cost(user_id, cost)
            except Exception as exc:
                logger.warning(f"Failed to increment cost counter for {user_id}: {exc}")
                raise  # let tracked_task record the failure

        task = tracked_task(_increment(), name="cost_increment")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return enriched

    @staticmethod
    def _compute_cost(
        *,
        prompt_tokens: int,
        completion_tokens: int,
        pricing: Pricing,
        total_tokens: int | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> float | None:
        """Mirror :func:`serving.storage.utils.calculate_cost` byte-for-byte.

        Cache-read / cache-write tokens are subtracted from the billable
        prompt total only when the adapter configures a non-zero cache
        rate (otherwise they fall through to the regular prompt rate, the
        same behavior as the dict-based path).
        """
        try:
            prompt = float(prompt_tokens or 0)
            completion = float(completion_tokens or 0)
            reasoning = float(reasoning_tokens or 0)
            total = float(total_tokens) if total_tokens is not None else None
            cache_r = float(cache_read_tokens or 0)
            cache_w = float(cache_write_tokens or 0)

            prompt_p = float(pricing.prompt_price)
            completion_p = float(pricing.completion_price)
            cache_r_p = float(pricing.cache_read_price)
            cache_w_p = float(pricing.cache_write_price)

            billable_prompt = prompt
            if cache_r_p > 0:
                billable_prompt -= cache_r
            if cache_w_p > 0:
                billable_prompt -= cache_w
            billable_prompt = max(billable_prompt, 0.0)

            output = billable_output_tokens(
                prompt_tokens=prompt,
                completion_tokens=completion,
                reasoning_tokens=reasoning,
                total_tokens=total,
            )

            return (
                (billable_prompt * prompt_p / 1_000_000)
                + (output * completion_p / 1_000_000)
                + (cache_r * cache_r_p / 1_000_000)
                + (cache_w * cache_w_p / 1_000_000)
            )
        except (ValueError, TypeError):
            return None
