"""Routing strategies for request distribution.

Provides:
- FixedRouter: Weighted random routing with automatic fallback
- RouteConfig, RoutingObservation, ProviderPinError
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from routing.protocols import RoutingRequestOptions
    from serving.adapters.base import BaseAdapter

from routing.endpoint_health import EndpointHealthRegistry
from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import (
    PrefillLease,
    PrefillLoadTracker,
    conversation_fingerprint,
    estimate_prefill_tokens,
    priority_for_prefill,
)
from routing.route_table import EffectiveRoute, RouteTableSnapshot
from routing.streaming import has_non_empty_content
from routing.telemetry import failed_attempt, routing_chunk
from serving.exceptions import operator_safe_error
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)
_LEGACY_ROUTING_OPTION_UNSET = object()

# ============================================================================
# Exceptions
# ============================================================================


class ProviderPinError(ValueError):
    """Raised when a pinned provider is not found or disabled for a model."""


class AllCircuitsOpenError(RuntimeError):
    """Raised when all provider circuits are open (full outage)."""


@runtime_checkable
class ManagedRouter(Protocol):
    """Router with async lifecycle hooks managed by application bootstrap."""

    async def start(self) -> None:
        """Start router-owned background work."""
        ...

    async def stop(self) -> None:
        """Stop router-owned background work."""
        ...


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class RouteConfig:
    """Weighted adapter list for a model."""

    adapters: list[tuple[BaseAdapter, float]]
    raw_adapters: list[tuple[BaseAdapter, float, str]] | None = None
    canonical_model_id: str | None = None
    admin_only: bool = False
    required_role: str = "free"
    published: bool = True


@dataclass(kw_only=True)
class RoutingObservation:
    """Observation from a completed request, for online learning routers.

    RouteWiseRouter overrides record_observation() to update its cost model;
    FixedRouter ignores observations (no-op). All fields are keyword-only so
    request correlation, terminal disposition, and strategy-owned metadata stay
    explicit at construction sites. Supplied ``strategy_metadata`` is borrowed
    from its caller; observations and router consumers treat it as read-only.
    """

    model_id: str
    endpoint_id: str
    ttft_ms: float | None
    total_latency_ms: float
    token_count: int
    success: bool
    request_id: str | None = None
    terminal: bool = True
    prompt_tokens: int = 0
    completion_tokens: int = 0
    strategy_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Affinity:
    """Per-user provider pin for one model. TTL is monotonic time."""

    endpoint_id: str
    expires_at: float


# ============================================================================
# Helpers
# ============================================================================


def current_affinity_key() -> str | None:
    """Return the caller identity for this request, or None.

    Read independently of ``AFFINITY_ENABLED``: prefill accounting uses it to
    recognize a warm continuation, which stays useful even where sticky routing
    itself is switched off.
    """
    return req_ctx.get().get("affinity_key") or None


AFFINITY_TTL_SECONDS: float = 300.0
AFFINITY_SWEEP_THRESHOLD: int = 1000
AFFINITY_ENABLED: bool = os.environ.get("ROUTING_AFFINITY_ENABLED", "1") != "0"


# ============================================================================
# FixedRouter
# ============================================================================


def adapter_supports_modalities(adapter: BaseAdapter, required: frozenset[str] | None) -> bool:
    """Return True if the adapter's route accepts every required input modality.

    ``required`` is the set of non-text modalities a request needs (e.g.
    ``{"image"}``). Each route inherits the model-level ``input_modalities``
    unless a route-level override narrows it, so this lets the router skip a
    fallback that cannot accept the media even when the model as a whole
    advertises that modality.
    """
    if not required:
        return True
    supported = set(getattr(adapter.config, "input_modalities", None) or ["text"])
    return required <= supported


class FixedRouter:
    """Weighted random routing with automatic fallback.

    Drop-in replacement for RouteExecutor. Selects adapters via weighted
    random selection and tries remaining adapters on failure.

    Args:
        params: Optional Pydantic ``FixedParams`` (passed by the strategy
            registry).  ``None`` keeps existing call-site behavior.
            ``params.local_fraction`` is currently informational; the existing
            weighted-random selection over ``routes`` is unchanged.
    """

    def __init__(
        self,
        params: Any = None,
        weight_override_resolver: Any | None = None,
        disabled_provider_resolver: Any | None = None,
        health_registry: EndpointHealthRegistry | None = None,
    ) -> None:
        self._health_registry = (
            health_registry if health_registry is not None else EndpointHealthRegistry()
        )
        self._lock = threading.RLock()
        self._affinity: dict[tuple[str, str], _Affinity] = {}
        self.routes: dict[str, RouteConfig] = {}
        # Keep the validated params accessible for future use (e.g. honoring
        # local_fraction in adapter selection).  Today FixedRouter ignores it
        # because per-route weights already encode local-vs-remote balance.
        self.params = params
        self.weight_override_resolver = weight_override_resolver
        # Admin kill switch: adapters whose provider is disabled are forced to
        # weight 0 so the existing ``weight > 0`` gates in selection and every
        # fallback loop skip them without any per-call-site change.
        self.disabled_provider_resolver = disabled_provider_resolver
        # In-flight prefill per endpoint. Weighted-random balances request
        # counts, which lets one mega-prefill monopolize a replica while its
        # siblings idle; this is the signal that lets selection see that.
        self._prefill_load = PrefillLoadTracker()

    @property
    def prefill_load(self) -> PrefillLoadTracker:
        """Return the per-endpoint in-flight prefill tracker."""
        return self._prefill_load

    @property
    def endpoint_health_registry(self) -> EndpointHealthRegistry:
        """Return the process-scoped endpoint-health collaborator."""
        return self._health_registry

    @staticmethod
    def _resolve_pin_provider(
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> str | None:
        """Resolve the explicit pin without forwarding router controls upstream."""
        option_pin = routing_options.pin_provider if routing_options is not None else None
        legacy_pin = params.pop("pin_provider", _LEGACY_ROUTING_OPTION_UNSET)
        if legacy_pin is _LEGACY_ROUTING_OPTION_UNSET:
            return option_pin
        if legacy_pin is None:
            return option_pin
        # One-release compatibility for direct FixedRouter callers. The public
        # serving path uses RoutingRequestOptions and never enters this branch.
        if option_pin is not None:
            raise TypeError("pin_provider was supplied both directly and in routing_options")
        if not isinstance(legacy_pin, str):
            raise TypeError("pin_provider must be a string or None")
        return legacy_pin

    def _ensure_health(self, endpoint_id: str) -> None:
        self._health_registry.ensure(endpoint_id)

    def _on_success(self, endpoint_id: str) -> None:
        self._health_registry.record_success(endpoint_id)

    def _on_failure(
        self,
        endpoint_id: str,
        *,
        reason: str = "error",
        detail: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._health_registry.record_failure(
            endpoint_id,
            reason=reason,
            detail=detail,
            exc=exc,
        )

    def _drop_affinity(self, model_id: str) -> None:
        """Drop affinity entry for the current request's affinity_key + model.

        No-op if affinity_key is missing from req_ctx or no entry exists.
        """
        affinity_key = req_ctx.get().get("affinity_key")
        if not affinity_key:
            return
        with self._lock:
            self._affinity.pop((affinity_key, model_id), None)

    def _maybe_sweep_affinity_locked(self, now: float) -> None:
        """Drop expired affinity entries. Caller must hold self._lock."""
        if len(self._affinity) <= AFFINITY_SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of provider availability and circuit state."""
        return self._health_registry.snapshot()

    def record_observation(self, obs: RoutingObservation) -> None:
        """Ignore observations because fixed routing has no online-learning state."""
        return None

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        """Return a stable effective-route snapshot built under the route lock."""
        with self._lock:
            return self._effective_routes_locked()

    def snapshot_for_transition(self, model_id: str) -> RouteTableSnapshot:
        """Capture published routes plus one staged canonical model privately."""
        with self._lock:
            routes = self._effective_routes_locked(include_unpublished_model_id=model_id)
            canonical_ids = {
                route_key: route.canonical_model_id or route_key
                for route_key, route in self.routes.items()
                if route.published or (route.canonical_model_id or route_key) == model_id
            }
        return RouteTableSnapshot(routes, canonical_ids)

    def _effective_routes_locked(
        self,
        *,
        include_unpublished_model_id: str | None = None,
    ) -> tuple[EffectiveRoute, ...]:
        snapshot: list[EffectiveRoute] = []
        seen_canonical_ids: set[str] = set()
        for route_key, route in self.routes.items():
            canonical_model_id = route.canonical_model_id or route_key
            if not route.published and canonical_model_id != include_unpublished_model_id:
                continue
            if canonical_model_id in seen_canonical_ids:
                continue
            seen_canonical_ids.add(canonical_model_id)
            snapshot.append(
                EffectiveRoute(
                    route_key=route_key,
                    canonical_model_id=canonical_model_id,
                    adapters=tuple(self._get_effective_adapters(route_key, route)),
                )
            )
        return tuple(snapshot)

    def canonical_id(self, model_id: str) -> str:
        """Resolve aliases through the route table without exposing mutable routes."""
        with self._lock:
            route = self.routes.get(model_id)
            return route.canonical_model_id if route and route.canonical_model_id else model_id

    def _apply_disabled_providers(
        self, adapters: list[tuple[BaseAdapter, float]]
    ) -> list[tuple[BaseAdapter, float]]:
        """Force weight 0 for adapters whose provider is admin-disabled."""
        resolver = self.disabled_provider_resolver
        if resolver is None:
            return adapters
        is_disabled = resolver.is_disabled
        return [
            (adapter, 0.0 if is_disabled(adapter.config.provider) else weight)
            for adapter, weight in adapters
        ]

    def _get_effective_adapters(
        self, model_id: str, route: RouteConfig
    ) -> list[tuple[BaseAdapter, float]]:
        """Return raw route weights with runtime overrides applied when available."""
        resolver = self.weight_override_resolver
        raw_adapters = route.raw_adapters
        override_model_id = route.canonical_model_id or model_id
        if resolver is None or not raw_adapters:
            return self._apply_disabled_providers(route.adapters)

        get_snapshot = getattr(resolver, "get_snapshot_for_model", None)
        if get_snapshot is not None:
            overrides = get_snapshot(override_model_id)
            return self._apply_disabled_providers(
                [
                    (adapter, float(overrides.get(endpoint_id, raw_weight)))
                    for adapter, raw_weight, endpoint_id in raw_adapters
                ]
            )

        result = resolver.get_for_model(override_model_id)
        if isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                overrides = asyncio.run(result)
            else:
                raise RuntimeError(
                    "FixedRouter cannot await weight overrides during active event-loop selection"
                ) from None
        else:
            overrides = result

        return self._apply_disabled_providers(
            [
                (adapter, float(overrides.get(endpoint_id, raw_weight)))
                for adapter, raw_weight, endpoint_id in raw_adapters
            ]
        )

    def register_route(
        self,
        model_id: str,
        adapters_with_weights: list[tuple[BaseAdapter, float]],
        *,
        aliases: list[str] | None = None,
        admin_only: bool = False,
        required_role: str = "free",
        published: bool = True,
    ) -> None:
        """Register a weighted route for a model.

        Args:
            model_id: Model identifier (canonical).
            adapters_with_weights: List of (adapter, weight) tuples.
                Weights will be normalized to sum to 1.0.
            aliases: Optional alias model IDs that share the same RouteConfig.
                Updates to the canonical route automatically apply to aliases.
            admin_only: If True, only admin users may access this route.
                Deprecated: use required_role="admin" instead.
            required_role: Minimum role required to access this model
                (free/pro/internal/admin).
            published: Whether request selection may use this route. Runtime
                model creation stages an unpublished route until its durable
                strategy transition commits.
        """
        total_weight = sum(weight for _, weight in adapters_with_weights)
        if total_weight <= 0:
            return
        raw_adapters = [
            (adapter, float(weight), endpoint_id_for_adapter(adapter))
            for adapter, weight in adapters_with_weights
        ]
        normalized = [(adapter, weight / total_weight) for adapter, weight in adapters_with_weights]
        # Backward compat: admin_only=True implies required_role="admin"
        effective_role = required_role
        if admin_only and effective_role == "free":
            effective_role = "admin"
        route_cfg = RouteConfig(
            adapters=normalized,
            raw_adapters=raw_adapters,
            canonical_model_id=model_id,
            admin_only=admin_only,
            required_role=effective_role,
            published=published,
        )
        with self._lock:
            self.routes[model_id] = route_cfg
            for alias in aliases or []:
                self.routes[alias] = route_cfg  # shared reference, not a copy

    def eligible_adapters(self, model_id: str) -> list[tuple[BaseAdapter, float]]:
        """Return the adapters automatic routing may dispatch to, in route order.

        Applies the same two admission rules ``_select_adapter`` uses: an
        adapter whose provider is admin-disabled carries a runtime weight of 0,
        and an adapter whose circuit is open is not admitted. Exposed for
        surfaces that pick an adapter themselves instead of calling into the
        router -- ``/v1/messages`` forwards an Anthropic-native body this router
        has no method for -- so their pick honors the same rules.

        Returns an empty list when the model has no route, the route is
        unpublished, or nothing is admitted; that last case is what
        ``_select_adapter`` reports as ``AllCircuitsOpenError``.
        """
        route = self.routes.get(model_id)
        if not route or not route.published or not route.adapters:
            return []
        effective = self._get_effective_adapters(model_id, route)
        with self._lock:
            snapshot = list(effective)
        return [
            (adapter, weight)
            for adapter, weight in snapshot
            if weight > 0 and self._health_registry.allow_request(endpoint_id_for_adapter(adapter))
        ]

    def _select_adapter(
        self,
        model_id: str,
        *,
        pin_provider: str | None = None,
        required_modalities: frozenset[str] | None = None,
        prefill_tokens: int = 0,
    ) -> BaseAdapter | None:
        """Select an adapter using weighted random selection with optional affinity.

        Args:
            model_id: Model identifier.
            pin_provider: Optional provider/endpoint_id to pin to. Overrides affinity.
            required_modalities: Non-text input modalities the request needs.
                Routes that do not declare all of them are excluded so media is
                never dispatched to a route that cannot handle it.
            prefill_tokens: Estimated prompt size, used to steer away from
                endpoints already saturated with prefill and to keep concurrent
                mega-prefills spread across replicas. Zero keeps the historical
                pure weighted-random behavior.

        Returns:
            Selected adapter or None if no route configured / no match.
        """
        route = self.routes.get(model_id)
        if not route or not route.published or not route.adapters:
            return None

        effective = self._get_effective_adapters(model_id, route)
        if required_modalities:
            effective = [
                (adapter, weight)
                for adapter, weight in effective
                if adapter_supports_modalities(adapter, required_modalities)
            ]
            if not effective:
                raise AllCircuitsOpenError(
                    f"No route for model {model_id} accepts input modalities "
                    f"{sorted(required_modalities)}"
                )

        if pin_provider:
            for adapter, weight in effective:
                if weight <= 0:
                    continue
                eid = endpoint_id_for_adapter(adapter)
                if adapter.config.provider == pin_provider or eid == pin_provider:
                    return adapter
            return None

        with self._lock:
            snapshot = list(effective)

        allowed: list[tuple[BaseAdapter, float]] = [
            (adapter, weight)
            for adapter, weight in snapshot
            if weight > 0 and self._health_registry.allow_request(endpoint_id_for_adapter(adapter))
        ]

        if not allowed:
            provider_names = [endpoint_id_for_adapter(adapter) for adapter, _weight in snapshot]
            raise AllCircuitsOpenError(
                f"All provider circuits are open for model {model_id}: {provider_names}"
            )

        affinity_key: str | None = None
        if AFFINITY_ENABLED:
            affinity_key = req_ctx.get().get("affinity_key") or None

        # Set when a pin is dropped for backlog, so selection does not simply
        # hand the caller straight back to the endpoint it was moved off.
        avoid_endpoint_id: str | None = None
        if affinity_key:
            now = time.monotonic()
            with self._lock:
                entry = self._affinity.get((affinity_key, model_id))
                if entry is not None and entry.expires_at > now:
                    for adapter, _w in allowed:
                        if endpoint_id_for_adapter(adapter) == entry.endpoint_id:
                            # A pin is worth honoring for its prefix-cache hit,
                            # but not into a queue: one high-volume key holding
                            # a binding is exactly how a single replica ends up
                            # absorbing every request while its siblings idle.
                            # Falling through re-runs selection and re-pins to
                            # whatever it picks, so the caller rebuilds locality
                            # on an endpoint that can actually serve it.
                            if self._prefill_load.should_keep_affinity(
                                entry.endpoint_id, affinity_key=affinity_key
                            ):
                                entry.expires_at = now + AFFINITY_TTL_SECONDS
                                return adapter
                            avoid_endpoint_id = entry.endpoint_id
                            break
                    else:
                        del self._affinity[(affinity_key, model_id)]
                elif entry is not None:
                    del self._affinity[(affinity_key, model_id)]

        total_allowed = sum(w for _, w in allowed)
        pool = (
            [(a, w / total_allowed) for a, w in allowed]
            if abs(total_allowed - 1.0) > 1e-9
            else allowed
        )

        chosen = pool[
            self._prefill_load.select_index(
                [endpoint_id_for_adapter(adapter) for adapter, _w in pool],
                [weight for _a, weight in pool],
                prefill_tokens,
                random.random,
                affinity_key,
                avoid_endpoint_id,
            )
        ][0]

        if affinity_key:
            now = time.monotonic()
            with self._lock:
                self._affinity[(affinity_key, model_id)] = _Affinity(
                    endpoint_id=endpoint_id_for_adapter(chosen),
                    expires_at=now + AFFINITY_TTL_SECONDS,
                )
                self._maybe_sweep_affinity_locked(now)

        return chosen

    def _dispatch_priority(
        self,
        endpoint_id: str,
        prefill_tokens: int,
        affinity_key: str | None,
        fingerprint: str | None = None,
    ) -> int:
        """Scheduling priority for one dispatch, ranked on *this* endpoint's work.

        Deliberately the un-cached estimate rather than the prompt size: the
        fleet runs above 90% prefix-cache hit, so ranking on the total would
        stamp a warm 500k-token continuation -- a few thousand delta tokens of
        actual prefill -- as an elephant and have the upstream schedule it last
        and preempt it, which is the opposite of what its cost deserves. The
        same discount decides selection and the elephant limit, so priority
        agrees with routing by construction.

        Per endpoint, because the discount is: a prefix resident on the replica
        the caller has been talking to is not resident on a fallback that has
        never seen this conversation, and that fallback really is facing the
        cold prefill.
        """
        return priority_for_prefill(
            self._prefill_load.uncached_estimate(
                endpoint_id, prefill_tokens, affinity_key, fingerprint=fingerprint
            )
        )

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Execute chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            routing_options: Router-owned controls such as an explicit provider pin
                and required input modalities.
            **params: Additional parameters for the adapter.

        Returns:
            Chat completion response with routing metadata.

        Raises:
            ValueError: If no route configured for model.
        """
        pin_provider = self._resolve_pin_provider(routing_options, params)
        required_modalities = (
            routing_options.required_modalities if routing_options is not None else frozenset()
        )
        prefill_tokens = estimate_prefill_tokens(messages)
        affinity_key = current_affinity_key()
        # Which conversation this is, so a caller's unrelated prompt cannot
        # inherit another's warm-prefix discount (see _dispatch_priority).
        fingerprint = conversation_fingerprint(messages)
        primary = self._select_adapter(
            model_id,
            pin_provider=pin_provider,
            required_modalities=required_modalities,
            prefill_tokens=prefill_tokens,
        )
        if not primary:
            if pin_provider:
                raise ProviderPinError(
                    f"Pinned provider '{pin_provider}' not found for model {model_id}"
                )
            raise ValueError(f"No route configured for model {model_id}")
        try:
            endpoint_id = endpoint_id_for_adapter(primary)
            with req_ctx.push(
                model=model_id,
                provider=primary.config.provider,
                **{
                    req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                        endpoint_id, prefill_tokens, affinity_key, fingerprint
                    )
                },
            ):
                self._ensure_health(endpoint_id)
                lease = self._prefill_load.acquire(
                    endpoint_id,
                    prefill_tokens,
                    affinity_key=affinity_key,
                    fingerprint=fingerprint,
                )
                try:
                    resp = await primary.chat_completion(messages, **params)
                    # A returned response proves this endpoint finished
                    # prefilling this prompt, which is what makes its prefix
                    # safe to remember.
                    self._prefill_load.release(lease, prefill_confirmed=True)
                finally:
                    self._prefill_load.release(lease)
                self._on_success(endpoint_id)
            # Preserve adapter-set _routing if present;
            # only set default routing if the adapter didn't provide one.
            if "_routing" not in resp:
                resp["_routing"] = {
                    "provider": primary.config.provider,
                    "base_url": primary.config.base_url,
                }
            # Always inject endpoint_id so observation keys match latency profiles.
            resp["_routing"].setdefault("endpoint_id", endpoint_id_for_adapter(primary))
            return resp
        except Exception as primary_error:
            # Record failure for primary endpoint before attempting fallback
            self._on_failure(
                endpoint_id_for_adapter(primary),
                reason="chat_exception",
                detail=operator_safe_error(primary_error),
                exc=primary_error,
            )
            failed_attempts = [failed_attempt(primary, primary_error)]
            # Attach routing to the surfaced error so the error-log path can
            # attribute the failure to the real upstream instead of the "router"
            # sentinel — mirrors the success-path resp["_routing"] injection.
            # The outer error handler covers both re-raise points below
            # (pin mode and all-providers-failed); ``failed_attempts`` is stored
            # by reference so it reflects any fallback attempts appended before
            # ``primary_error`` is finally re-raised.
            if not hasattr(primary_error, "_routing"):
                primary_error._routing = {  # type: ignore[attr-defined]
                    "provider": primary.config.provider,
                    "base_url": primary.config.base_url,
                    "endpoint_id": endpoint_id_for_adapter(primary),
                    "failed_attempts": failed_attempts,
                }
            # Pin mode: never fallback — the caller explicitly requested this
            # provider, so a silent switch would produce misleading results.
            if pin_provider:
                raise primary_error
            self._drop_affinity(model_id)
            route = self.routes[model_id]
            for adapter, weight in self._get_effective_adapters(model_id, route):
                if adapter == primary or weight <= 0:
                    continue
                endpoint_id = endpoint_id_for_adapter(adapter)
                if not adapter_supports_modalities(adapter, required_modalities):
                    continue
                # Fallback is still automatic routing, so it must honor the
                # same shared circuit eligibility as the initial selection.
                # Explicit pinning returned above and remains the sole circuit
                # override.
                if not self._health_registry.allow_request(endpoint_id):
                    continue
                try:
                    with req_ctx.push(
                        model=model_id,
                        provider=adapter.config.provider,
                        **{
                            req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                                endpoint_id, prefill_tokens, affinity_key, fingerprint
                            )
                        },
                    ):
                        self._ensure_health(endpoint_id)
                        lease = self._prefill_load.acquire(
                            endpoint_id,
                            prefill_tokens,
                            affinity_key=affinity_key,
                            fingerprint=fingerprint,
                        )
                        try:
                            resp = await adapter.chat_completion(messages, **params)
                            self._prefill_load.release(lease, prefill_confirmed=True)
                        finally:
                            self._prefill_load.release(lease)
                        self._on_success(endpoint_id)
                    if "_routing" not in resp:
                        resp["_routing"] = {
                            "provider": adapter.config.provider,
                            "base_url": adapter.config.base_url,
                            "fallback": True,
                        }
                    resp["_routing"].setdefault("endpoint_id", endpoint_id_for_adapter(adapter))
                    resp["_routing"].setdefault("failed_attempts", failed_attempts)
                    return resp
                except Exception as fallback_error:
                    self._on_failure(
                        endpoint_id,
                        reason="chat_exception",
                        detail=operator_safe_error(fallback_error),
                        exc=fallback_error,
                    )
                    failed_attempts.append(failed_attempt(adapter, fallback_error))
                    continue
            raise primary_error

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Stream chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            routing_options: Router-owned controls such as an explicit provider pin
                and required input modalities.
            **params: Additional parameters for the adapter.

        Yields:
            SSE chunks from the adapter.

        Raises:
            ValueError: If no route configured for model.
        """
        pin_provider = self._resolve_pin_provider(routing_options, params)
        required_modalities = (
            routing_options.required_modalities if routing_options is not None else frozenset()
        )
        prefill_tokens = estimate_prefill_tokens(messages)
        affinity_key = current_affinity_key()
        # Which conversation this is, so a caller's unrelated prompt cannot
        # inherit another's warm-prefix discount (see _dispatch_priority).
        fingerprint = conversation_fingerprint(messages)
        primary = self._select_adapter(
            model_id,
            pin_provider=pin_provider,
            required_modalities=required_modalities,
            prefill_tokens=prefill_tokens,
        )
        if not primary:
            if pin_provider:
                raise ProviderPinError(
                    f"Pinned provider '{pin_provider}' not found for model {model_id}"
                )
            raise ValueError(f"No route configured for model {model_id}")
        chunks_yielded = False
        lease: PrefillLease | None = None
        try:
            primary_endpoint_id = endpoint_id_for_adapter(primary)
            with req_ctx.push(
                model=model_id,
                provider=primary.config.provider,
                **{
                    req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                        primary_endpoint_id, prefill_tokens, affinity_key, fingerprint
                    )
                },
            ):
                # Emit synthetic _routing chunk so completions.py can recover
                # the upstream provider/base_url/endpoint_id for DB logging.
                # Without this, req_ctx.push() inside this block is invisible
                # to the parent coroutine when the stream is consumed via an
                # asyncio.create_task reader, and api_logs ends up with
                # provider="router" and cost_usd=NULL.
                first = True
                # Charged before the first yield so the lease brackets the whole
                # upstream interaction: a generator abandoned after the routing
                # chunk still unwinds through this method's finally.
                lease = self._prefill_load.acquire(
                    primary_endpoint_id,
                    prefill_tokens,
                    affinity_key=affinity_key,
                    fingerprint=fingerprint,
                )
                yield routing_chunk(primary)
                async for chunk in primary.stream_chat_completion(messages, **params):
                    if first and has_non_empty_content(chunk):
                        # Providers may emit keep-alives or empty terminal chunks.
                        first = False
                        # Consider first non-empty token as a success signal for availability.
                        self._on_success(primary_endpoint_id)
                        # First token means the prompt is resident and this
                        # endpoint is decoding, not prefilling. Holding the
                        # lease for the whole stream would let a long cheap
                        # decode read as prefill pressure and push traffic away
                        # from an endpoint that is no longer busy prefilling.
                        self._prefill_load.release(lease, prefill_confirmed=True)
                    yield chunk
                    chunks_yielded = True
            return
        except Exception as primary_error:
            # Primary is done either way; release before the fallback attempts
            # so its backlog does not shadow it for the rest of this request.
            self._prefill_load.release(lease)
            self._on_failure(
                endpoint_id_for_adapter(primary),
                reason="stream_exception",
                detail=operator_safe_error(primary_error),
                exc=primary_error,
            )
            failed_attempts = [failed_attempt(primary, primary_error)]
            # Attach routing to the surfaced error so the error-log path can
            # attribute the failure to the real upstream. Unlike the
            # non-streaming twin below, this generator never gets a chance to
            # set resp["_routing"] on success, so the consumer instead tracks
            # provider via in-band routing_chunk SSE events -- but those are
            # emitted before each fallback attempt even starts (so req_ctx is
            # visible across the asyncio.create_task reader boundary), and
            # the consumer keeps overwriting its provider with the latest one
            # seen. When every attempt fails without yielding content, that
            # leaves the last (lowest-priority) fallback attributed instead
            # of primary, whose error is what's actually re-raised below.
            # Setting exc._routing here mirrors chat_completion's pattern and
            # takes priority over the consumer's SSE-derived guess. Covers
            # all three re-raise points below (pin mode, a stream already
            # committed to primary, and all-providers-failed); failed_attempts
            # is stored by reference so it reflects any fallback attempts
            # appended before primary_error is finally re-raised.
            if not hasattr(primary_error, "_routing"):
                primary_error._routing = {  # type: ignore[attr-defined]
                    "provider": primary.config.provider,
                    "base_url": primary.config.base_url,
                    "endpoint_id": endpoint_id_for_adapter(primary),
                    "failed_attempts": failed_attempts,
                }
            # Pin mode: never fallback — re-raise immediately.
            if pin_provider:
                raise primary_error
            self._drop_affinity(model_id)
            # Once any chunk has been yielded to the client the SSE stream
            # has committed to a single provider. Falling back here would
            # produce a corrupt response: duplicate role/system events from
            # the second provider, mid-message provider switch, and
            # mismatched token-usage totals. Re-raise instead so the caller
            # closes the stream — the partial response is the lesser harm.
            if chunks_yielded:
                raise primary_error
            route = self.routes[model_id]
            for adapter, weight in self._get_effective_adapters(model_id, route):
                if adapter == primary or weight <= 0:
                    continue
                adapter_endpoint_id = endpoint_id_for_adapter(adapter)
                if not adapter_supports_modalities(adapter, required_modalities):
                    continue
                # Synthetic routing chunks are emitted only after circuit
                # admission so an open automatic fallback is never exposed as
                # an attempted upstream. Explicit pinning returned above.
                if not self._health_registry.allow_request(adapter_endpoint_id):
                    continue
                try:
                    with req_ctx.push(
                        model=model_id,
                        provider=adapter.config.provider,
                        **{
                            req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                                adapter_endpoint_id, prefill_tokens, affinity_key, fingerprint
                            )
                        },
                    ):
                        yield routing_chunk(
                            adapter,
                            fallback=True,
                            failed_attempts=failed_attempts,
                        )
                        first = True
                        lease = self._prefill_load.acquire(
                            adapter_endpoint_id,
                            prefill_tokens,
                            affinity_key=affinity_key,
                            fingerprint=fingerprint,
                        )
                        async for chunk in adapter.stream_chat_completion(messages, **params):
                            if first and has_non_empty_content(chunk):
                                first = False
                                self._on_success(adapter_endpoint_id)
                                self._prefill_load.release(lease, prefill_confirmed=True)
                            yield chunk
                            chunks_yielded = True
                    return
                except Exception as fallback_error:
                    self._on_failure(
                        adapter_endpoint_id,
                        reason="stream_exception",
                        detail=operator_safe_error(fallback_error),
                        exc=fallback_error,
                    )
                    failed_attempts.append(failed_attempt(adapter, fallback_error))
                    # Once this fallback provider's bytes reached the client the
                    # SSE stream has committed to it (same invariant as the
                    # primary path above). Re-raise instead of splicing yet
                    # another provider into the same response.
                    if chunks_yielded:
                        raise
                    continue
                finally:
                    # Covers the attempt that never reached a first token.
                    self._prefill_load.release(lease)
            raise primary_error
        finally:
            # Backstop for every exit this generator has: an upstream error
            # before the first token, and — the case that actually leaks in
            # production — a client disconnect, which closes the generator
            # mid-stream and would otherwise strand the lease forever, making
            # the endpoint look permanently busy to every later request.
            self._prefill_load.release(lease)
