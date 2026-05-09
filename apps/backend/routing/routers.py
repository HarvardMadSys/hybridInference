"""Routing strategies for request distribution.

Provides:
- BaseRouter: Abstract base with shared infrastructure (circuit breaker, EWMA health)
- FixedRouter: Weighted random routing with automatic fallback
- RouteConfig, RoutingObservation, ProviderPinError
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from serving.adapters.base import BaseAdapter

from serving.observability.alerts import AlertSeverity, alert_slack
from serving.observability.metrics import (
    API_FALLBACKS,
    API_TTFT,
    CIRCUIT_OPEN_TOTAL,
    CIRCUIT_STATE,
    PROVIDER_AVAILABILITY,
    PROVIDER_LATENCY,
    ROUTING_AFFINITY,
    STREAMING_INTERRUPTION,
    normalize_model_label,
    normalize_provider_label,
)
from serving.utils import context as req_ctx

# Strong references to fire-and-forget Slack alert tasks. asyncio holds only
# weak refs to scheduled tasks, so without this set the GC may cancel an alert
# mid-flight (e.g. when the breaker that scheduled it is dropped). Tasks
# remove themselves via add_done_callback once they finish.
_ALERT_TASKS: set[asyncio.Task[bool]] = set()

# ============================================================================
# Exceptions
# ============================================================================


class ProviderPinError(ValueError):
    """Raised when a pinned provider is not found or disabled for a model."""


class AllCircuitsOpenError(RuntimeError):
    """Raised when all provider circuits are open (full outage)."""


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class RouteConfig:
    """Weighted adapter list for a model."""

    adapters: list[tuple[BaseAdapter, float]]
    admin_only: bool = False
    required_role: str = "free"


@dataclass
class RoutingObservation:
    """Observation from a completed request, for online learning routers.

    RouteWiseRouter overrides record_observation() to update its cost model;
    FixedRouter ignores observations (no-op).
    """

    model_id: str
    endpoint_id: str
    ttft_ms: float | None
    total_latency_ms: float
    token_count: int
    success: bool
    quota_committed: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    selected_tier: str | None = None
    sc_committed: bool = False
    hedged: bool = False
    backup_won: bool = False
    lp_status: str | None = None


@dataclass
class _Affinity:
    """Per-user provider pin for one model. TTL is monotonic time."""

    endpoint_id: str
    expires_at: float


# ============================================================================
# Helpers
# ============================================================================


AFFINITY_TTL_SECONDS: float = 300.0
AFFINITY_SWEEP_THRESHOLD: int = 1000
AFFINITY_ENABLED: bool = os.environ.get("ROUTING_AFFINITY_ENABLED", "1") != "0"


def _get_endpoint_id(adapter: BaseAdapter) -> str:
    """Get the unique endpoint identifier for health tracking.

    Uses endpoint_id if set, otherwise falls back to provider.
    """
    return getattr(adapter.config, "endpoint_id", None) or adapter.config.provider


def _routing_chunk(adapter: BaseAdapter, *, fallback: bool = False) -> str:
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
    return f"data: {json.dumps({'choices': [], '_routing': routing})}\n\n"


def _has_non_empty_content(chunk: Any) -> bool:
    r"""Return True if the SSE ``chunk`` carries a non-empty delta (content or tool_calls).

    The streaming protocol emits lines like ``"data: {json}\n\n"`` and a
    terminal ``"data: [DONE]\n\n"``. We consider a chunk as having started
    output when delta.content is a non-empty string **or** delta.tool_calls
    is a non-empty list.
    """
    try:
        if not isinstance(chunk, str | bytes):
            return True  # Unknown type; assume it carries content
        s = chunk.decode() if isinstance(chunk, bytes) else chunk
        if "[DONE]" in s:
            return False
        prefix = "data: "
        if not s.startswith(prefix):
            return True  # Non-standard; assume content
        import json as _json

        payload = s[len(prefix) :].strip()
        obj = _json.loads(payload)
        choices = obj.get("choices") or []
        if not choices:
            return False
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and len(content) > 0:
            return True
        tool_calls = delta.get("tool_calls")
        return isinstance(tool_calls, list) and len(tool_calls) > 0
    except Exception:
        # Be conservative and treat as content to avoid missing TTFT altogether
        return True


def _safe_set_availability(provider: str, value: float) -> None:
    with contextlib.suppress(Exception):
        PROVIDER_AVAILABILITY.labels(provider=normalize_provider_label(provider)).set(value)


def _reason_str(s: str) -> str:
    return s if s and len(s) < 64 else "error"


# ============================================================================
# Health Tracking
# ============================================================================


class _ProviderHealth:
    """Track provider availability via exponentially weighted counters."""

    def __init__(self, provider: str, alpha: float | None = None) -> None:
        self.provider = provider
        env_alpha = os.getenv("ROUTER_HEALTH_EWMA_ALPHA")
        self.alpha = (
            float(env_alpha) if env_alpha is not None else (alpha if alpha is not None else 0.1)
        )
        self.ewma_success = 1.0
        self.ewma_total = 1.0
        self._lock = threading.Lock()

    def record(self, success: bool) -> None:
        inc_s = 1.0 if success else 0.0
        with self._lock:
            self.ewma_success = (1 - self.alpha) * self.ewma_success + self.alpha * inc_s
            self.ewma_total = (1 - self.alpha) * self.ewma_total + self.alpha * 1.0

    @property
    def availability(self) -> float:
        if self.ewma_total <= 0:
            return 1.0
        return max(0.0, min(1.0, self.ewma_success / self.ewma_total))


class _CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _CircuitBreaker:
    """Simple circuit breaker per provider.

    - Open when consecutive failures exceed threshold or availability too low.
    - Remain open for a cooldown, then transition to HALF_OPEN to allow a trial.
    - On trial success, close; on failure, reopen and reset cooldown.
    """

    def __init__(
        self,
        provider: str,
        *,
        failure_threshold: int | None = None,
        cooldown_seconds: float | None = None,
        min_availability: float | None = None,
    ) -> None:
        self.provider = provider
        self.state = _CircuitState.CLOSED
        # Read configuration from environment with sensible defaults.
        self.failure_threshold = int(
            os.getenv(
                "CIRCUIT_FAILURE_THRESHOLD",
                str(failure_threshold if failure_threshold is not None else 3),
            )
        )
        self.cooldown_seconds = float(
            os.getenv(
                "CIRCUIT_COOLDOWN_SECONDS",
                str(cooldown_seconds if cooldown_seconds is not None else 30.0),
            )
        )
        self.min_availability = float(
            os.getenv(
                "CIRCUIT_MIN_AVAILABILITY",
                str(min_availability if min_availability is not None else 0.7),
            )
        )
        self.consecutive_failures = 0
        self.last_opened: float | None = None
        self._lock = threading.Lock()
        CIRCUIT_STATE.labels(provider=normalize_provider_label(provider)).set(0)

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == _CircuitState.CLOSED:
                return True
            if self.state == _CircuitState.OPEN:
                if self.last_opened is None:
                    return False
                if (time.perf_counter() - self.last_opened) >= self.cooldown_seconds:
                    # Move to half-open for a trial request.
                    self.state = _CircuitState.HALF_OPEN
                    CIRCUIT_STATE.labels(provider=normalize_provider_label(self.provider)).set(0)
                    return True
                return False
            # HALF_OPEN allows a single trial at a time; conservative approach: allow.
            return True

    def on_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            if self.state in (_CircuitState.OPEN, _CircuitState.HALF_OPEN):
                self.state = _CircuitState.CLOSED
                CIRCUIT_STATE.labels(provider=normalize_provider_label(self.provider)).set(0)

    def on_failure(self, *, availability: float | None = None, reason: str = "error") -> None:
        with self._lock:
            self.consecutive_failures += 1
            trip = False
            if self.consecutive_failures >= self.failure_threshold:
                trip = True
            if availability is not None and availability < self.min_availability:
                trip = True
            if trip:
                prev_state = self.state
                self.state = _CircuitState.OPEN
                self.last_opened = time.perf_counter()
                CIRCUIT_STATE.labels(provider=normalize_provider_label(self.provider)).set(1)
                CIRCUIT_OPEN_TOTAL.labels(
                    provider=normalize_provider_label(self.provider), reason=reason
                ).inc()
                # Fire-and-forget Slack alert on CLOSED→OPEN or HALF_OPEN→OPEN.
                if prev_state in (_CircuitState.CLOSED, _CircuitState.HALF_OPEN):
                    try:
                        task = asyncio.ensure_future(
                            alert_slack(
                                AlertSeverity.ERROR,
                                "Provider circuit opened",
                                {
                                    "provider": self.provider,
                                    "consecutive_failures": self.consecutive_failures,
                                    "availability": (
                                        f"{availability:.2f}" if availability is not None else "n/a"
                                    ),
                                    "reason": reason or "unknown",
                                },
                                dedupe_key=f"circuit_open:{self.provider}",
                                cooldown_sec=300,
                            )
                        )
                    except RuntimeError:
                        # No running event loop (e.g., unit test outside
                        # pytest-asyncio). Best-effort alert; skip silently.
                        pass
                    else:
                        # Keep a strong reference until the task finishes so
                        # the GC cannot cancel it mid-flight.
                        _ALERT_TASKS.add(task)
                        task.add_done_callback(_ALERT_TASKS.discard)


# ============================================================================
# BaseRouter
# ============================================================================


class BaseRouter:
    """Abstract base router with circuit breaker, health tracking, and metrics.

    Provides shared infrastructure for all routing strategies:
    - Circuit breaker protection per provider/endpoint
    - EWMA health tracking
    - Provider availability metrics

    Subclasses implement their own chat_completion / stream_chat_completion
    using the shared infrastructure methods.
    """

    def __init__(self) -> None:
        self._health: dict[str, _ProviderHealth] = {}
        self._circuits: dict[str, _CircuitBreaker] = {}
        self._lock = threading.RLock()
        self._affinity: dict[tuple[str, str], _Affinity] = {}

    def _ensure_health(self, endpoint_id: str) -> None:
        with self._lock:
            if endpoint_id not in self._health:
                self._health[endpoint_id] = _ProviderHealth(endpoint_id)
            if endpoint_id not in self._circuits:
                self._circuits[endpoint_id] = _CircuitBreaker(endpoint_id)

    def _on_success(self, endpoint_id: str) -> None:
        with self._lock:
            self._ensure_health(endpoint_id)
            self._health[endpoint_id].record(True)
            avail = self._health[endpoint_id].availability
            self._circuits[endpoint_id].on_success()
        _safe_set_availability(endpoint_id, avail)

    def _on_failure(self, endpoint_id: str, *, reason: str = "error") -> None:
        with self._lock:
            self._ensure_health(endpoint_id)
            self._health[endpoint_id].record(False)
            avail = self._health[endpoint_id].availability
            self._circuits[endpoint_id].on_failure(availability=avail, reason=_reason_str(reason))
        _safe_set_availability(endpoint_id, avail)

    def _drop_affinity(self, model_id: str) -> None:
        """Drop affinity entry for the current request's affinity_key + model.

        No-op if affinity_key is missing from req_ctx or no entry exists.
        Emits a `dropped_error` metric event when an entry is removed.
        """
        affinity_key = req_ctx.get().get("affinity_key")
        if not affinity_key:
            return
        with self._lock:
            removed = self._affinity.pop((affinity_key, model_id), None)
        if removed is not None:
            ROUTING_AFFINITY.labels(
                event="dropped_error",
                model=normalize_model_label(model_id),
            ).inc()

    def _maybe_sweep_affinity_locked(self, now: float) -> None:
        """Drop expired affinity entries. Caller must hold self._lock."""
        if len(self._affinity) <= AFFINITY_SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of provider availability and circuit state."""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for provider, h in self._health.items():
                state = (
                    self._circuits.get(provider).state if provider in self._circuits else "closed"
                )
                out[provider] = {
                    "availability": h.availability,
                    "circuit_state": state,
                }
        return out

    def record_observation(self, obs: RoutingObservation) -> None:
        """Record a routing observation. No-op by default; override in online learning routers."""

    # ------------------------------------------------------------------
    # Adapter execution helpers (overridable by subclasses)
    # ------------------------------------------------------------------

    async def _execute_adapter(
        self,
        adapter: BaseAdapter,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        """Execute a request through an adapter with monitoring.

        Provides a single-adapter execution path with context, health checks,
        and latency tracking. Subclasses (e.g. RouteWiseRouter) can override
        to add slot lifecycle management.
        """
        endpoint_id = _get_endpoint_id(adapter)
        with req_ctx.push(model=model_id, provider=adapter.config.provider):
            self._ensure_health(endpoint_id)
            started = time.perf_counter()
            resp = await adapter.chat_completion(messages, **params)
            PROVIDER_LATENCY.labels(
                provider=normalize_provider_label(endpoint_id),
                model=normalize_model_label(model_id),
                operation="chat_completion",
            ).observe(time.perf_counter() - started)
            self._on_success(endpoint_id)
        return resp

    async def _execute_stream_adapter(
        self,
        adapter: BaseAdapter,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Execute a streaming request through an adapter with monitoring.

        Parallel to ``_execute_adapter`` for streaming. Subclasses can
        override for per-adapter instrumentation (e.g. S_C slot cleanup).
        """
        endpoint_id = _get_endpoint_id(adapter)
        with req_ctx.push(model=model_id, provider=adapter.config.provider):
            self._ensure_health(endpoint_id)
            first = True
            started = time.perf_counter()
            async for chunk in adapter.stream_chat_completion(messages, **params):
                if first and _has_non_empty_content(chunk):
                    first = False
                    API_TTFT.labels(
                        provider=normalize_provider_label(endpoint_id),
                        model=normalize_model_label(model_id),
                    ).observe(time.perf_counter() - started)
                    self._on_success(endpoint_id)
                yield chunk

    # ------------------------------------------------------------------
    # Chat completion with fallback (used by RouteWiseRouter via super())
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        """Execute chat completion with automatic fallback.

        Subclass-agnostic orchestration: calls ``_select_adapter`` (subclass)
        then ``_execute_adapter`` (overridable) with fallback logic.
        """
        context = {
            "messages": messages,
            "params": params,
            "request_id": params.get("request_id"),
        }
        primary = self._select_adapter(model_id, context)
        if not primary:
            raise ValueError(f"No route configured for model {model_id}")

        last_attempted = primary
        try:
            try:
                resp = await self._execute_adapter(primary, model_id, messages, **params)
                if "_routing" not in resp:
                    resp["_routing"] = {
                        "provider": primary.config.provider,
                        "base_url": primary.config.base_url,
                    }
                # Always inject endpoint_id so observation keys match latency profiles.
                resp["_routing"].setdefault(
                    "endpoint_id", getattr(primary.config, "endpoint_id", None)
                )
                return resp
            except Exception as primary_error:
                self._on_failure(_get_endpoint_id(primary), reason=primary_error.__class__.__name__)
                fallback_adapters = self._get_fallback_adapters(model_id, primary)
                for adapter in fallback_adapters:
                    last_attempted = adapter
                    try:
                        resp = await self._execute_adapter(adapter, model_id, messages, **params)
                        if "_routing" not in resp:
                            resp["_routing"] = {
                                "provider": adapter.config.provider,
                                "base_url": adapter.config.base_url,
                                "fallback": True,
                            }
                        resp["_routing"].setdefault(
                            "endpoint_id",
                            getattr(adapter.config, "endpoint_id", None),
                        )
                        API_FALLBACKS.labels(
                            from_provider=normalize_provider_label(_get_endpoint_id(primary)),
                            to_provider=normalize_provider_label(_get_endpoint_id(adapter)),
                            reason=primary_error.__class__.__name__,
                        ).inc()
                        return resp
                    except Exception:
                        self._on_failure(_get_endpoint_id(adapter), reason="chat_exception")
                        continue
                raise primary_error
        except BaseException as e:
            if not hasattr(e, "_routing"):
                e._routing = {  # type: ignore[attr-defined]
                    "provider": last_attempted.config.provider,
                    "base_url": last_attempted.config.base_url,
                    "endpoint_id": getattr(last_attempted.config, "endpoint_id", None),
                }
            raise

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Stream chat completion with automatic fallback.

        Subclass-agnostic orchestration: calls ``_select_adapter`` (subclass)
        then ``_execute_stream_adapter`` (overridable) with fallback logic.
        """
        context = {
            "messages": messages,
            "params": params,
            "request_id": params.get("request_id"),
        }
        primary = self._select_adapter(model_id, context)
        if not primary:
            raise ValueError(f"No route configured for model {model_id}")

        last_attempted = primary
        try:
            try:
                yield _routing_chunk(primary)
                async for chunk in self._execute_stream_adapter(
                    primary, model_id, messages, **params
                ):
                    yield chunk
                return
            except Exception as primary_error:
                self._on_failure(_get_endpoint_id(primary), reason="stream_exception")
                fallback_adapters = self._get_fallback_adapters(model_id, primary)
                for adapter in fallback_adapters:
                    last_attempted = adapter
                    try:
                        yield _routing_chunk(adapter, fallback=True)
                        async for chunk in self._execute_stream_adapter(
                            adapter, model_id, messages, **params
                        ):
                            yield chunk
                        API_FALLBACKS.labels(
                            from_provider=normalize_provider_label(_get_endpoint_id(primary)),
                            to_provider=normalize_provider_label(_get_endpoint_id(adapter)),
                            reason=primary_error.__class__.__name__,
                        ).inc()
                        return
                    except Exception:
                        self._on_failure(_get_endpoint_id(adapter), reason="stream_exception")
                        continue
                raise primary_error
        except BaseException as e:
            if not hasattr(e, "_routing"):
                e._routing = {  # type: ignore[attr-defined]
                    "provider": last_attempted.config.provider,
                    "base_url": last_attempted.config.base_url,
                    "endpoint_id": getattr(last_attempted.config, "endpoint_id", None),
                }
            raise

    def _select_adapter(
        self, model_id: str, context: dict[str, Any] | None = None, **kwargs: Any
    ) -> BaseAdapter | None:
        """Select an adapter for the given model. Override in subclasses."""
        return None

    def _get_fallback_adapters(
        self, model_id: str, failed_adapter: BaseAdapter
    ) -> list[BaseAdapter]:
        """Return fallback adapters after primary failure. Override in subclasses."""
        return []


# ============================================================================
# FixedRouter
# ============================================================================


class FixedRouter(BaseRouter):
    """Weighted random routing with automatic fallback.

    Drop-in replacement for RouteExecutor. Selects adapters via weighted
    random selection and tries remaining adapters on failure.
    """

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, RouteConfig] = {}

    def register_route(
        self,
        model_id: str,
        adapters_with_weights: list[tuple[BaseAdapter, float]],
        *,
        aliases: list[str] | None = None,
        admin_only: bool = False,
        required_role: str = "free",
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
        """
        total_weight = sum(weight for _, weight in adapters_with_weights)
        if total_weight <= 0:
            return
        normalized = [(adapter, weight / total_weight) for adapter, weight in adapters_with_weights]
        # Backward compat: admin_only=True implies required_role="admin"
        effective_role = required_role
        if admin_only and effective_role == "free":
            effective_role = "admin"
        route_cfg = RouteConfig(
            adapters=normalized,
            admin_only=admin_only,
            required_role=effective_role,
        )
        self.routes[model_id] = route_cfg
        for alias in aliases or []:
            self.routes[alias] = route_cfg  # shared reference, not a copy

    def _select_adapter(  # type: ignore[override]
        self, model_id: str, *, pin_provider: str | None = None
    ) -> BaseAdapter | None:
        """Select an adapter using weighted random selection with optional affinity.

        Args:
            model_id: Model identifier.
            pin_provider: Optional provider/endpoint_id to pin to. Overrides affinity.

        Returns:
            Selected adapter or None if no route configured / no match.
        """
        route = self.routes.get(model_id)
        if not route or not route.adapters:
            return None

        if pin_provider:
            for adapter, weight in route.adapters:
                if weight <= 0:
                    continue
                eid = _get_endpoint_id(adapter)
                if adapter.config.provider == pin_provider or eid == pin_provider:
                    return adapter
            return None

        with self._lock:
            snapshot: list[tuple[BaseAdapter, float, _CircuitBreaker]] = []
            for adapter, weight in route.adapters:
                endpoint_id = _get_endpoint_id(adapter)
                cb = self._circuits.get(endpoint_id)
                if not cb:
                    cb = self._circuits[endpoint_id] = _CircuitBreaker(endpoint_id)
                snapshot.append((adapter, weight, cb))

        allowed: list[tuple[BaseAdapter, float]] = [
            (adapter, weight)
            for (adapter, weight, cb) in snapshot
            if weight > 0 and cb.allow_request()
        ]

        if not allowed:
            provider_names = [_get_endpoint_id(a) for a, _w, _cb in snapshot]
            raise AllCircuitsOpenError(
                f"All provider circuits are open for model {model_id}: {provider_names}"
            )

        affinity_key: str | None = None
        model_label = normalize_model_label(model_id)
        if AFFINITY_ENABLED:
            affinity_key = req_ctx.get().get("affinity_key") or None

        if affinity_key:
            now = time.monotonic()
            with self._lock:
                entry = self._affinity.get((affinity_key, model_id))
                if entry is not None and entry.expires_at > now:
                    for adapter, _w in allowed:
                        if _get_endpoint_id(adapter) == entry.endpoint_id:
                            entry.expires_at = now + AFFINITY_TTL_SECONDS
                            ROUTING_AFFINITY.labels(event="hit", model=model_label).inc()
                            return adapter
                    del self._affinity[(affinity_key, model_id)]
                    ROUTING_AFFINITY.labels(event="dropped_unavailable", model=model_label).inc()
                elif entry is not None:
                    del self._affinity[(affinity_key, model_id)]
                    ROUTING_AFFINITY.labels(event="expired", model=model_label).inc()
                else:
                    ROUTING_AFFINITY.labels(event="miss", model=model_label).inc()

        total_allowed = sum(w for _, w in allowed)
        pool = (
            [(a, w / total_allowed) for a, w in allowed]
            if abs(total_allowed - 1.0) > 1e-9
            else allowed
        )

        rand = random.random()
        cumulative = 0.0
        chosen: BaseAdapter | None = None
        for adapter, weight in pool:
            cumulative += weight
            if rand <= cumulative:
                chosen = adapter
                break
        if chosen is None:
            chosen = pool[-1][0]

        if affinity_key:
            now = time.monotonic()
            with self._lock:
                self._affinity[(affinity_key, model_id)] = _Affinity(
                    endpoint_id=_get_endpoint_id(chosen),
                    expires_at=now + AFFINITY_TTL_SECONDS,
                )
                self._maybe_sweep_affinity_locked(now)
            ROUTING_AFFINITY.labels(event="created", model=model_label).inc()

        return chosen

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        pin_provider: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Execute chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            pin_provider: Optional provider name to force routing to.
            **params: Additional parameters for the adapter.

        Returns:
            Chat completion response with routing metadata.

        Raises:
            ValueError: If no route configured for model.
        """
        primary = self._select_adapter(model_id, pin_provider=pin_provider)
        if not primary:
            if pin_provider:
                raise ProviderPinError(
                    f"Pinned provider '{pin_provider}' not found for model {model_id}"
                )
            raise ValueError(f"No route configured for model {model_id}")
        try:
            with req_ctx.push(model=model_id, provider=primary.config.provider):
                endpoint_id = _get_endpoint_id(primary)
                self._ensure_health(endpoint_id)
                started = time.perf_counter()
                resp = await primary.chat_completion(messages, **params)
                PROVIDER_LATENCY.labels(
                    provider=normalize_provider_label(endpoint_id),
                    model=normalize_model_label(model_id),
                    operation="chat_completion",
                ).observe(time.perf_counter() - started)
                self._on_success(endpoint_id)
            # Preserve adapter-set _routing if present;
            # only set default routing if the adapter didn't provide one.
            if "_routing" not in resp:
                resp["_routing"] = {
                    "provider": primary.config.provider,
                    "base_url": primary.config.base_url,
                }
            # Always inject endpoint_id so observation keys match latency profiles.
            resp["_routing"].setdefault("endpoint_id", _get_endpoint_id(primary))
            return resp
        except Exception as primary_error:
            # Record failure for primary endpoint before attempting fallback
            self._on_failure(_get_endpoint_id(primary), reason="chat_exception")
            # Pin mode: never fallback — the caller explicitly requested this
            # provider, so a silent switch would produce misleading results.
            if pin_provider:
                raise primary_error
            self._drop_affinity(model_id)
            route = self.routes[model_id]
            for adapter, weight in route.adapters:
                if adapter == primary or weight <= 0:
                    continue
                try:
                    with req_ctx.push(model=model_id, provider=adapter.config.provider):
                        endpoint_id = _get_endpoint_id(adapter)
                        self._ensure_health(endpoint_id)
                        started = time.perf_counter()
                        resp = await adapter.chat_completion(messages, **params)
                        PROVIDER_LATENCY.labels(
                            provider=normalize_provider_label(endpoint_id),
                            model=normalize_model_label(model_id),
                            operation="chat_completion",
                        ).observe(time.perf_counter() - started)
                        self._on_success(endpoint_id)
                    if "_routing" not in resp:
                        resp["_routing"] = {
                            "provider": adapter.config.provider,
                            "base_url": adapter.config.base_url,
                            "fallback": True,
                        }
                    resp["_routing"].setdefault("endpoint_id", _get_endpoint_id(adapter))
                    API_FALLBACKS.labels(
                        from_provider=normalize_provider_label(_get_endpoint_id(primary)),
                        to_provider=normalize_provider_label(_get_endpoint_id(adapter)),
                        reason=primary_error.__class__.__name__,
                    ).inc()
                    return resp
                except Exception:
                    self._on_failure(endpoint_id, reason="chat_exception")
                    continue
            raise primary_error

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        pin_provider: str | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Stream chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            pin_provider: Optional provider name to force routing to.
            **params: Additional parameters for the adapter.

        Yields:
            SSE chunks from the adapter.

        Raises:
            ValueError: If no route configured for model.
        """
        primary = self._select_adapter(model_id, pin_provider=pin_provider)
        if not primary:
            if pin_provider:
                raise ProviderPinError(
                    f"Pinned provider '{pin_provider}' not found for model {model_id}"
                )
            raise ValueError(f"No route configured for model {model_id}")
        chunks_yielded = False
        try:
            with req_ctx.push(model=model_id, provider=primary.config.provider):
                # Emit synthetic _routing chunk so completions.py can recover
                # the upstream provider/base_url/endpoint_id for DB logging.
                # Without this, req_ctx.push() inside this block is invisible
                # to the parent coroutine when the stream is consumed via an
                # asyncio.create_task reader, and api_logs ends up with
                # provider="router" and cost_usd=NULL.
                yield _routing_chunk(primary)
                first = True
                started = time.perf_counter()
                primary_endpoint_id = _get_endpoint_id(primary)
                async for chunk in primary.stream_chat_completion(messages, **params):
                    if first and _has_non_empty_content(chunk):
                        # Observe TTFT only when the first non-empty content arrives.
                        # Providers may emit keep-alives or empty terminal chunks.
                        first = False
                        API_TTFT.labels(
                            provider=normalize_provider_label(primary_endpoint_id),
                            model=normalize_model_label(model_id),
                        ).observe(time.perf_counter() - started)
                        # Consider first non-empty token as a success signal for availability.
                        self._on_success(primary_endpoint_id)
                    yield chunk
                    chunks_yielded = True
            return
        except Exception as primary_error:
            # record streaming interruption for primary provider
            STREAMING_INTERRUPTION.labels(
                model=model_id,
                provider=normalize_provider_label(_get_endpoint_id(primary)),
                stage="adapter_stream",
            ).inc()
            self._on_failure(_get_endpoint_id(primary), reason="stream_exception")
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
            for adapter, weight in route.adapters:
                if adapter == primary or weight <= 0:
                    continue
                try:
                    with req_ctx.push(model=model_id, provider=adapter.config.provider):
                        yield _routing_chunk(adapter, fallback=True)
                        first = True
                        started = time.perf_counter()
                        adapter_endpoint_id = _get_endpoint_id(adapter)
                        async for chunk in adapter.stream_chat_completion(messages, **params):
                            if first and _has_non_empty_content(chunk):
                                first = False
                                API_TTFT.labels(
                                    provider=normalize_provider_label(adapter_endpoint_id),
                                    model=normalize_model_label(model_id),
                                ).observe(time.perf_counter() - started)
                                self._on_success(adapter_endpoint_id)
                            yield chunk
                    API_FALLBACKS.labels(
                        from_provider=normalize_provider_label(_get_endpoint_id(primary)),
                        to_provider=normalize_provider_label(adapter_endpoint_id),
                        reason=primary_error.__class__.__name__,
                    ).inc()
                    return
                except Exception:
                    STREAMING_INTERRUPTION.labels(
                        model=model_id,
                        provider=normalize_provider_label(adapter_endpoint_id),
                        stage="adapter_stream",
                    ).inc()
                    self._on_failure(adapter_endpoint_id, reason="stream_exception")
                    continue
            raise primary_error
