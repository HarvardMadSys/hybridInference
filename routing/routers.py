"""Routing strategies for request distribution.

This module provides all routing implementations:
- BaseRouter: Abstract base with infrastructure (circuit breaker, health, fallback)
- FixedRouter: Weighted random routing
- NimbusRouter: SLO-aware hybrid routing

All routers handle request distribution across multiple model adapters with
health tracking, circuit breakers, and automatic fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from serving.adapters.base import BaseAdapter
    from serving.config.settings import Settings

from routing.outsourcing import OutsourcingEngine, SimpleFLOPCalculator
from routing.outsourcing.adapters import SGLangWaitingQueueAdapter
from routing.outsourcing_integration import OutsourcingRouter
from serving.observability.metrics import (
    API_FALLBACKS,
    API_TTFT,
    CIRCUIT_OPEN_TOTAL,
    CIRCUIT_STATE,
    PROVIDER_AVAILABILITY,
    PROVIDER_LATENCY,
    STREAMING_INTERRUPTION,
    normalize_model_label,
    normalize_provider_label,
)
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)


# ============================================================================
# Circuit Breaker and Health Tracking
# ============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class _CircuitBreaker:
    """Per-provider circuit breaker with exponential backoff.

    State transitions:
    - CLOSED -> OPEN: When availability drops below threshold
    - OPEN -> HALF_OPEN: After cooldown period
    - HALF_OPEN -> CLOSED: On successful request
    - HALF_OPEN -> OPEN: On failed request
    """

    def __init__(
        self,
        provider: str,
        failure_threshold: float = 0.5,
        initial_cooldown: float = 5.0,
        max_cooldown: float = 60.0,
    ):
        self.provider = provider
        self.state = CircuitState.CLOSED
        self.failure_threshold = failure_threshold
        self.initial_cooldown = initial_cooldown
        self.max_cooldown = max_cooldown
        self.current_cooldown = initial_cooldown
        self.opened_at: float | None = None

    def allow_request(self) -> bool:
        """Check if request should be allowed through."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            # Check if cooldown period has passed
            if self.opened_at and (time.time() - self.opened_at) >= self.current_cooldown:
                self.state = CircuitState.HALF_OPEN
                _safe_set_circuit_state(self.provider, self.state.value)
                logger.info(f"Circuit breaker {self.provider} -> HALF_OPEN (testing recovery)")
                return True
            return False
        # HALF_OPEN: allow one request to test
        return True

    def on_success(self) -> None:
        """Record successful request."""
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self.current_cooldown = self.initial_cooldown
            _safe_set_circuit_state(self.provider, self.state.value)
            logger.info(f"Circuit breaker {self.provider} -> CLOSED (recovered)")

    def on_failure(self, *, availability: float, reason: str = "error") -> None:
        """Record failed request and potentially open circuit."""
        if self.state == CircuitState.CLOSED:
            if availability < self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.time()
                _safe_set_circuit_state(self.provider, self.state.value)
                CIRCUIT_OPEN_TOTAL.labels(provider=normalize_provider_label(self.provider)).inc()
                logger.warning(
                    f"Circuit breaker {self.provider} -> OPEN "
                    f"(availability={availability:.2f}, reason={reason}, "
                    f"cooldown={self.current_cooldown}s)"
                )
        elif self.state == CircuitState.HALF_OPEN:
            # Failed during recovery test
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            # Exponential backoff
            self.current_cooldown = min(self.current_cooldown * 2, self.max_cooldown)
            _safe_set_circuit_state(self.provider, self.state.value)
            logger.warning(
                f"Circuit breaker {self.provider} -> OPEN "
                f"(recovery failed, new cooldown={self.current_cooldown}s)"
            )


class _ProviderHealth:
    """Track provider health using EWMA (Exponentially Weighted Moving Average)."""

    def __init__(self, provider: str, alpha: float = 0.1):
        self.provider = provider
        self.alpha = alpha
        self.availability = 1.0

    def record(self, success: bool) -> None:
        """Update availability using EWMA."""
        value = 1.0 if success else 0.0
        self.availability = self.alpha * value + (1 - self.alpha) * self.availability


def _has_non_empty_content(chunk: str | dict) -> bool:
    r"""Check if SSE chunk has non-empty content (for TTFT measurement).

    The streaming protocol emits lines like "data: {json}\n\n" and a
    terminal "data: [DONE]\n\n". We only consider a chunk as the first
    token when the JSON "choices[0].delta.content" is a non-empty string.

    Args:
        chunk: Either a dict or an SSE-formatted string (e.g., "data: {...}")

    Returns:
        True if chunk contains non-empty content
    """
    try:
        if not chunk:
            return False

        # If chunk is a string (SSE format), parse it
        if isinstance(chunk, str):
            # Skip [DONE] markers
            if "[DONE]" in chunk:
                return False
            # Parse SSE format: "data: {...}"
            if not chunk.startswith("data: "):
                return True  # Non-standard; assume content

            import json

            payload = chunk[6:].strip()  # Skip "data: " prefix
            chunk_data = json.loads(payload)
        else:
            chunk_data = chunk

        # Now check for content in the parsed data
        choices = chunk_data.get("choices") or []
        if not choices:
            return False

        delta = choices[0].get("delta") or {}
        # Any of content / tool_calls / reasoning_content counts as
        # first model output (i.e. prefill is done).
        content = delta.get("content")
        if isinstance(content, str) and len(content) > 0:
            return True
        if delta.get("tool_calls"):
            return True
        return bool(delta.get("reasoning_content"))
    except Exception:
        # Be conservative and treat as content to avoid missing TTFT altogether
        return True


def _safe_set_circuit_state(provider: str, state: str) -> None:
    """Safely set circuit breaker state metric."""
    with contextlib.suppress(Exception):
        CIRCUIT_STATE.labels(provider=normalize_provider_label(provider), state=state).set(
            1 if state == "open" else 0
        )


# ============================================================================
# Helper Functions
# ============================================================================


def _safe_set_availability(provider: str, value: float) -> None:
    """Safely set provider availability metric."""
    with contextlib.suppress(Exception):
        PROVIDER_AVAILABILITY.labels(provider=normalize_provider_label(provider)).set(value)


def _reason_str(s: str) -> str:
    """Sanitize error reason string for metrics."""
    return s if s and len(s) < 64 else "error"


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class RouteConfig:
    """Weighted adapter list for a model."""

    adapters: list[tuple[BaseAdapter, float]]


# ============================================================================
# BaseRouter - Abstract Base Class
# ============================================================================


class BaseRouter(ABC):
    """Abstract base router with circuit breaker, health tracking, and fallback.

    This class provides the infrastructure for safe request execution:
    - Circuit breaker protection
    - Health tracking (EWMA)
    - Automatic fallback (configurable via experiment_mode)
    - Metrics collection

    Subclasses only need to implement:
    - _select_adapter: How to select which adapter to use
    - _get_fallback_adapters: Which adapters to try on failure

    The execution flow (chat_completion, stream_chat_completion) is handled
    by this base class using the Template Method pattern.
    """

    def __init__(self, experiment_mode: bool = False) -> None:
        """Initialize base router with health tracking and circuit breakers.

        Args:
            experiment_mode: If True, disable fallback for clean experimental data.
                           If False, enable fallback for production reliability.
        """
        # Provider health and circuit breakers shared across models
        self._health: dict[str, _ProviderHealth] = {}
        self._circuits: dict[str, _CircuitBreaker] = {}
        # Short critical-section lock protecting shared dictionaries/state changes
        # Use RLock to allow nested locking in helper methods
        self._lock = threading.RLock()
        # Experiment mode controls fallback behavior
        self.experiment_mode = experiment_mode

    @abstractmethod
    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        """Select an adapter for the request.

        This is the ONLY method subclasses must implement for selection logic.
        The selection logic can be simple (weighted random) or complex (SLO-aware
        with queue analysis, FLOP calculations, etc.).

        Args:
            model_id: Model identifier (e.g., "glm-4.6")
            context: Request context containing:
                - messages: Chat messages
                - params: Request parameters
                - request_id: Optional request ID
                - prefill_slo_seconds: Optional SLO requirement

        Returns:
            Selected adapter, or None if no route available
        """
        pass

    @abstractmethod
    def _get_fallback_adapters(
        self,
        model_id: str,
        failed_adapter: BaseAdapter,
    ) -> list[BaseAdapter]:
        """Get fallback adapters when primary fails.

        Args:
            model_id: Model identifier
            failed_adapter: The adapter that just failed

        Returns:
            List of fallback adapters to try (in order)
        """
        pass

    async def _execute_adapter(
        self, adapter: BaseAdapter, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute a request through an adapter with monitoring.

        Helper method to avoid code duplication between primary and fallback execution.

        Args:
            adapter: The adapter to execute
            model_id: Model identifier
            messages: Chat messages
            **params: Additional parameters

        Returns:
            Chat completion response
        """
        with req_ctx.push(model=model_id, provider=adapter.config.provider):
            provider = adapter.config.provider
            self._ensure_health(provider)
            started = time.perf_counter()
            resp = await adapter.chat_completion(messages, **params)
            PROVIDER_LATENCY.labels(
                provider=normalize_provider_label(provider),
                model=normalize_model_label(model_id),
                operation="chat_completion",
            ).observe(time.perf_counter() - started)
            self._on_success(provider)
        return resp

    async def chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute chat completion with automatic fallback.

        This method provides the complete infrastructure:
        1. Select adapter (via _select_adapter)
        2. Check circuit breaker
        3. Execute request
        4. Record metrics
        5. Handle failures with fallback (unless experiment_mode=True)

        Args:
            model_id: Model identifier
            messages: Chat messages in OpenAI format
            **params: Additional parameters for the adapter

        Returns:
            Chat completion response with routing metadata

        Raises:
            ValueError: If no route configured for model
        """
        # Build context for selection
        context = {
            "messages": messages,
            "params": params,
            "request_id": params.get("request_id"),
            "prefill_slo_seconds": params.get("prefill_slo_seconds"),
        }

        # 1. Select adapter (subclass implements this)
        primary = self._select_adapter(model_id, context)
        if not primary:
            raise ValueError(f"No route configured for model {model_id}")

        # 2-5. Execute with infrastructure
        try:
            resp = await self._execute_adapter(primary, model_id, messages, **params)
            resp["_routing"] = {
                "provider": primary.config.provider,
                "base_url": primary.config.base_url,
            }
            return resp
        except Exception as primary_error:
            self._on_failure(primary.config.provider, reason=primary_error.__class__.__name__)

            # Fallback logic (only if not in experiment mode)
            if not self.experiment_mode:
                fallback_adapters = self._get_fallback_adapters(model_id, primary)
                for adapter in fallback_adapters:
                    try:
                        resp = await self._execute_adapter(adapter, model_id, messages, **params)
                        resp["_routing"] = {
                            "provider": adapter.config.provider,
                            "base_url": adapter.config.base_url,
                            "fallback": True,
                        }
                        API_FALLBACKS.labels(
                            from_provider=primary.config.provider,
                            to_provider=adapter.config.provider,
                            reason=primary_error.__class__.__name__,
                        ).inc()
                        return resp
                    except Exception:
                        self._on_failure(adapter.config.provider, reason="chat_exception")
                        continue

            # Experiment mode or all fallbacks failed
            raise primary_error

    async def _execute_stream_adapter(
        self,
        adapter: BaseAdapter,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Execute a streaming request through an adapter with monitoring.

        Parallel to ``_execute_adapter`` for non-streaming.  Subclasses can
        override for per-adapter instrumentation (e.g. shadow queue cleanup).

        Args:
            adapter: The adapter to execute
            model_id: Model identifier
            messages: Chat messages
            **params: Additional parameters

        Yields:
            SSE chunks from the adapter
        """
        with req_ctx.push(model=model_id, provider=adapter.config.provider):
            self._ensure_health(adapter.config.provider)
            first = True
            started = time.perf_counter()
            async for chunk in adapter.stream_chat_completion(messages, **params):
                if first and _has_non_empty_content(chunk):
                    first = False
                    API_TTFT.labels(
                        provider=normalize_provider_label(adapter.config.provider),
                        model=normalize_model_label(model_id),
                    ).observe(time.perf_counter() - started)
                    self._on_success(adapter.config.provider)
                    # Hook: subclasses run logic when prefill completes.
                    self._on_first_token(adapter, model_id, params)
                yield chunk

    def _on_first_token(  # noqa: B027
        self, adapter: BaseAdapter, model_id: str, params: dict[str, Any]
    ) -> None:
        """Hook called when the first content token is received (prefill done).

        BaseRouter no-op.  NimbusRouter overrides to remove from shadow queue.
        """

    async def stream_chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncIterator[Any]:
        """Stream chat completion with automatic fallback.

        Args:
            model_id: Model identifier
            messages: Chat messages in OpenAI format
            **params: Additional parameters for the adapter

        Yields:
            SSE chunks from the adapter

        Raises:
            ValueError: If no route configured for model
        """
        context = {
            "messages": messages,
            "params": params,
            "request_id": params.get("request_id"),
            "prefill_slo_seconds": params.get("prefill_slo_seconds"),
        }

        primary = self._select_adapter(model_id, context)
        if not primary:
            raise ValueError(f"No route configured for model {model_id}")

        try:
            async for chunk in self._execute_stream_adapter(primary, model_id, messages, **params):
                yield chunk
            return
        except Exception as primary_error:
            STREAMING_INTERRUPTION.labels(
                model=model_id,
                provider=primary.config.provider,
                stage="adapter_stream",
            ).inc()
            self._on_failure(primary.config.provider, reason="stream_exception")

            # Fallback (only if not in experiment mode)
            if not self.experiment_mode:
                fallback_adapters = self._get_fallback_adapters(model_id, primary)
                for adapter in fallback_adapters:
                    try:
                        async for chunk in self._execute_stream_adapter(
                            adapter, model_id, messages, **params
                        ):
                            yield chunk
                        API_FALLBACKS.labels(
                            from_provider=primary.config.provider,
                            to_provider=adapter.config.provider,
                            reason=primary_error.__class__.__name__,
                        ).inc()
                        return
                    except Exception:
                        STREAMING_INTERRUPTION.labels(
                            model=model_id,
                            provider=adapter.config.provider,
                            stage="adapter_stream",
                        ).inc()
                        self._on_failure(adapter.config.provider, reason="stream_exception")
                        continue

            # Experiment mode or all fallbacks failed
            raise primary_error

    def _ensure_health(self, provider: str) -> None:
        """Ensure health tracker and circuit breaker exist for provider."""
        with self._lock:
            if provider not in self._health:
                self._health[provider] = _ProviderHealth(provider)
            if provider not in self._circuits:
                self._circuits[provider] = _CircuitBreaker(provider)

    def _on_success(self, provider: str) -> None:
        """Record successful request."""
        with self._lock:
            self._ensure_health(provider)
            self._health[provider].record(True)
            avail = self._health[provider].availability
            self._circuits[provider].on_success()
        _safe_set_availability(provider, avail)

    def _on_failure(self, provider: str, *, reason: str = "error") -> None:
        """Record failed request."""
        with self._lock:
            self._ensure_health(provider)
            self._health[provider].record(False)
            avail = self._health[provider].availability
            self._circuits[provider].on_failure(availability=avail, reason=_reason_str(reason))
        _safe_set_availability(provider, avail)

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all providers.

        Returns:
            Dictionary mapping provider name to status info
        """
        with self._lock:
            return {
                provider: {
                    "availability": health.availability,
                    "circuit_state": self._circuits.get(
                        provider, _CircuitBreaker(provider)
                    ).state.value,
                }
                for provider, health in self._health.items()
            }


# ============================================================================
# FixedRouter - Weighted Random Routing
# ============================================================================


class FixedRouter(BaseRouter):
    """Fixed weighted random routing strategy.

    Selects adapters based on configured weights using weighted random selection.
    Inherits all infrastructure (circuit breaker, health tracking, fallback, metrics)
    from BaseRouter.

    Example:
        >>> router = FixedRouter()
        >>> router.register_route("gpt-4", [
        ...     (openai_adapter, 0.7),
        ...     (azure_adapter, 0.3),
        ... ])
        >>> response = await router.chat_completion("gpt-4", messages)
    """

    def __init__(self, experiment_mode: bool = False) -> None:
        """Initialize fixed router.

        Args:
            experiment_mode: If True, disable fallback for experiments.
        """
        super().__init__(experiment_mode)
        self.routes: dict[str, RouteConfig] = {}

    def register_route(
        self, model_id: str, adapters_with_weights: list[tuple[BaseAdapter, float]]
    ) -> None:
        """Register a weighted route for a model.

        Args:
            model_id: Model identifier
            adapters_with_weights: List of (adapter, weight) tuples.
                Weights will be normalized to sum to 1.0.
        """
        total_weight = sum(weight for _, weight in adapters_with_weights)
        if total_weight <= 0:
            return
        normalized = [(adapter, weight / total_weight) for adapter, weight in adapters_with_weights]
        self.routes[model_id] = RouteConfig(adapters=normalized)

    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        """Select an adapter using weighted random selection.

        This implements the abstract method from BaseRouter.
        Selection considers circuit breaker state - if all adapters have
        open circuit breakers, it still selects from all adapters to allow
        recovery attempts.

        Args:
            model_id: Model identifier
            context: Request context (not used in fixed routing)

        Returns:
            Selected adapter or None if no route configured
        """
        route = self.routes.get(model_id)
        if not route or not route.adapters:
            return None

        # Build a snapshot of (adapter, weight, circuit) under a short lock
        with self._lock:
            snapshot: list[tuple[BaseAdapter, float, Any]] = []
            for adapter, weight in route.adapters:
                provider = adapter.config.provider
                self._ensure_health(provider)
                cb = self._circuits.get(provider)
                snapshot.append((adapter, weight, cb))

        # Filter by circuit breaker state (outside lock to minimize contention)
        allowed: list[tuple[BaseAdapter, float]] = [
            (adapter, weight) for (adapter, weight, cb) in snapshot if cb and cb.allow_request()
        ]

        # If all circuits are open, use all adapters anyway (allow recovery)
        pool = allowed if allowed else [(a, w) for (a, w, _cb) in snapshot]

        # Weighted random selection
        rand = random.random()
        cumulative = 0.0
        for adapter, weight in pool:
            cumulative += weight
            if rand <= cumulative:
                return adapter

        # Fallback to last adapter (should rarely happen due to floating point)
        return pool[-1][0] if pool else None

    def _get_fallback_adapters(
        self,
        model_id: str,
        failed_adapter: BaseAdapter,
    ) -> list[BaseAdapter]:
        """Get fallback adapters when primary fails.

        Returns all other adapters in the route (excluding the failed one).

        Args:
            model_id: Model identifier
            failed_adapter: The adapter that just failed

        Returns:
            List of fallback adapters to try
        """
        route = self.routes.get(model_id)
        if not route:
            return []

        return [adapter for adapter, _ in route.adapters if adapter != failed_adapter]


# ============================================================================
# NimbusRouter - SLO-aware Hybrid Routing
# ============================================================================


class NimbusRouter(BaseRouter):
    """Multi-model hybrid routing manager with full BaseRouter infrastructure.

    Inherits circuit breaker, health tracking, fallback, and metrics from
    BaseRouter. Manages OutsourcingRouter instances for Nimbus-enabled models,
    delegating to FixedRouter for non-Nimbus models.

    Each Nimbus model gets its own OutsourcingRouter (with TreeCache, queue,
    outsourcing engine) for SLO-aware hybrid routing between local SGLang
    and external APIs.
    """

    def __init__(
        self,
        fixed_router: FixedRouter,
        settings: Settings,
        tree_cache_max_size_mb: float = 100.0,
        chars_per_token: float = 4.0,
    ):
        """Initialize Nimbus router for multiple models.

        Args:
            fixed_router: The FixedRouter instance (used to extract adapters and routes)
            settings: Application settings with Nimbus configuration
            tree_cache_max_size_mb: Maximum TreeCache size per model in MB
            chars_per_token: Average characters per token for cache hit estimation
        """
        super().__init__(experiment_mode=getattr(settings, "experiment_mode", False))
        self.fixed_router = fixed_router
        self.settings = settings
        self.tree_cache_max_size_mb = tree_cache_max_size_mb
        self.chars_per_token = chars_per_token
        self.outsourcing_routers: dict[str, OutsourcingRouter] = {}

        # Stores outsourcing decisions keyed by request_id for metadata injection.
        # Using request_id (not model_id) ensures concurrent requests for the same
        # model don't overwrite each other's decisions.
        self._pending_decisions: dict[str, dict[str, Any]] = {}

        self._init_routers()

    def _init_routers(self):
        """Initialize OutsourcingRouter for each enabled model."""
        enabled_models = self.settings.nimbus_enabled_models

        if not enabled_models:
            logger.warning("Nimbus routing enabled but nimbus_enabled_models is empty")
            return

        for model_id in enabled_models:
            if model_id not in self.fixed_router.routes:
                logger.warning(f"Nimbus: Model {model_id} not found in FixedRouter routes")
                continue

            try:
                # Extract local and remote adapters
                local_adapter, remote_adapter = self._extract_adapters(model_id)

                # Create waiting queue adapter
                waiting_queue = SGLangWaitingQueueAdapter(
                    metrics_url=self._get_metrics_url(local_adapter)
                )

                # Get model-specific SLO
                slo_seconds = self._get_model_slo(model_id)

                # Create outsourcing engine
                outsourcing_engine = OutsourcingEngine(
                    waiting_queue=waiting_queue,
                    flop_calculator=SimpleFLOPCalculator(
                        device_tflops=312.0  # A100 default
                    ),
                    prefill_slo_base_seconds=slo_seconds,
                )

                # Create OutsourcingRouter with TreeCache for prefix cache estimation
                self.outsourcing_routers[model_id] = OutsourcingRouter(
                    local_adapter=local_adapter,
                    remote_adapter=remote_adapter,
                    outsourcing_engine=outsourcing_engine,
                    waiting_queue=waiting_queue,
                    model_id=model_id,
                    tree_cache_max_size_mb=self.tree_cache_max_size_mb,
                    chars_per_token=self.chars_per_token,
                )

                logger.info(
                    f"Nimbus: Initialized OutsourcingRouter for {model_id} "
                    f"(SLO={slo_seconds}s, TreeCache={self.tree_cache_max_size_mb}MB)"
                )

            except ValueError as e:
                logger.warning(f"Nimbus: Skipping {model_id}: {e}")

    def _extract_adapters(self, model_id: str) -> tuple[BaseAdapter, BaseAdapter]:
        """Extract local and remote adapters for a model.

        Args:
            model_id: Model identifier

        Returns:
            Tuple of (local_adapter, remote_adapter)

        Raises:
            ValueError: If model doesn't have both local and remote adapters
        """
        route_config = self.fixed_router.routes.get(model_id)
        if not route_config:
            raise ValueError(f"No route config found for {model_id}")

        local_adapter: BaseAdapter | None = None
        remote_adapter: BaseAdapter | None = None

        def _is_local_adapter(adapter: BaseAdapter) -> bool:
            """Best-effort classification of a local adapter."""
            provider = (getattr(adapter.config, "provider", None) or "").lower()
            base_url = (getattr(adapter.config, "base_url", None) or "").lower()

            if provider in {"sglang", "local"}:
                return True
            if any(host in base_url for host in ("localhost", "127.0.0.1", ".local")):
                return True
            return provider == "openai_compat" and any(
                host in base_url for host in ("localhost", "127.0.0.1")
            )

        for adapter, _weight in route_config.adapters:
            if _is_local_adapter(adapter) and not local_adapter:
                local_adapter = adapter
            elif not _is_local_adapter(adapter) and not remote_adapter:
                remote_adapter = adapter

        # Fallback classification when heuristics cannot find clear local/remote.
        if (not local_adapter or not remote_adapter) and len(route_config.adapters) >= 2:
            if not local_adapter:
                local_adapter = route_config.adapters[0][0]
            if not remote_adapter:
                for candidate, _weight in route_config.adapters:
                    if candidate is not local_adapter:
                        remote_adapter = candidate
                        break

        if not local_adapter or not remote_adapter:
            raise ValueError(
                f"Model {model_id} requires both local and remote adapters for Nimbus. "
                f"Found: local={local_adapter is not None}, remote={remote_adapter is not None}"
            )

        return local_adapter, remote_adapter

    def _get_metrics_url(self, local_adapter: BaseAdapter) -> str:
        """Get Prometheus metrics URL from local adapter's base_url.

        Args:
            local_adapter: The local SGLang adapter

        Returns:
            Metrics URL (e.g., "http://localhost:6000/metrics")
        """
        base_url = local_adapter.config.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        return f"{base_url}/metrics"

    def _get_model_slo(self, model_id: str) -> float:
        """Get SLO threshold for a specific model.

        Args:
            model_id: Model identifier

        Returns:
            SLO threshold in seconds
        """
        # Model-specific SLO mapping
        if "glm-4.6" in model_id or "glm" in model_id:
            return self.settings.glm46_slo_seconds
        elif "qwen" in model_id:
            return self.settings.qwen3_slo_seconds
        elif "minimax" in model_id:
            return self.settings.minimax_slo_seconds
        else:
            # Default SLO
            return 2.0

    def _select_adapter(self, model_id: str, context: dict[str, Any]) -> BaseAdapter | None:
        """Select an adapter using outsourcing decision for Nimbus models.

        For Nimbus-enabled models, calls OutsourcingRouter.decide() to determine
        whether to route locally or externally. For other models, delegates to
        FixedRouter._select_adapter().

        Args:
            model_id: Model identifier
            context: Request context with messages, params, request_id, etc.

        Returns:
            Selected adapter, or None if no route available
        """
        outsourcing_router = self.outsourcing_routers.get(model_id)
        if outsourcing_router:
            messages = context.get("messages", [])
            params = context.get("params", {})
            request_id = context.get("request_id")
            prefill_slo_seconds = context.get("prefill_slo_seconds")

            result = outsourcing_router.decide(messages, request_id, prefill_slo_seconds, params)
            # Stash decision keyed by the *caller's* request_id (from params)
            # so chat_completion() / stream_chat_completion() can look it up
            # with the same key.  decide() may echo or generate its own id
            # inside result["request_id"], but the dict key must match the
            # one that NimbusRouter.chat_completion already holds.
            if request_id:
                self._pending_decisions[request_id] = result

            # Emit Prometheus metrics for observability
            self._emit_nimbus_metrics(model_id, result, outsourcing_router)

            return result["adapter"]
        else:
            return self.fixed_router._select_adapter(model_id, context)

    def _get_fallback_adapters(
        self,
        model_id: str,
        failed_adapter: BaseAdapter,
    ) -> list[BaseAdapter]:
        """Get fallback adapters when primary fails.

        For Nimbus models: if local failed, return [remote]; if remote failed,
        return [local]. For other models, delegate to FixedRouter.

        Args:
            model_id: Model identifier
            failed_adapter: The adapter that just failed

        Returns:
            List of fallback adapters to try
        """
        outsourcing_router = self.outsourcing_routers.get(model_id)
        if outsourcing_router:
            if failed_adapter is outsourcing_router.local_adapter:
                return [outsourcing_router.remote_adapter]
            elif failed_adapter is outsourcing_router.remote_adapter:
                return [outsourcing_router.local_adapter]
            else:
                # Unknown adapter; return both as fallbacks
                return [outsourcing_router.local_adapter, outsourcing_router.remote_adapter]
        else:
            return self.fixed_router._get_fallback_adapters(model_id, failed_adapter)

    # ------------------------------------------------------------------
    # Shadow queue lifecycle: prefill-aware removal
    # ------------------------------------------------------------------

    def _remove_from_shadow_queue(
        self,
        adapter: BaseAdapter,
        model_id: str,
        request_id: str | None,
    ) -> None:
        """Remove a request from the shadow queue if it was a local execution.

        Safe to call multiple times (idempotent) and must never raise —
        it runs in finally blocks where an exception would mask the
        original return value or error.
        """
        try:
            if not request_id:
                return
            outsourcing_router = self.outsourcing_routers.get(model_id)
            if not outsourcing_router:
                return
            if adapter is outsourcing_router.local_adapter:
                outsourcing_router.waiting_queue.remove_requests({request_id})
        except Exception:
            logger.debug(
                "Failed to remove request %s from shadow queue (non-fatal)",
                request_id,
                exc_info=True,
            )

    def _is_dry_run_outsource(self, adapter: BaseAdapter, model_id: str) -> bool:
        """Check if this request should be dry-run (fake response) instead of calling remote API."""
        if not self.settings.experiment_dry_run_outsource:
            return False
        outsourcing_router = self.outsourcing_routers.get(model_id)
        if not outsourcing_router:
            return False
        return adapter is outsourcing_router.remote_adapter

    def _make_dry_run_response(self, model_id: str, request_id: str | None) -> dict[str, Any]:
        """Build a fake OpenAI-compatible chat completion response for dry-run outsourcing."""
        import time as _time
        import uuid as _uuid

        return {
            "id": f"chatcmpl-dryrun-{_uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(_time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "[DRY-RUN] Outsourced request simulated",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "_routing": {"provider": "dry_run", "base_url": "dry_run"},
        }

    async def _execute_adapter(
        self,
        adapter: BaseAdapter,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> dict[str, Any]:
        """Execute adapter with shadow queue cleanup on completion.

        For non-streaming requests we don't have a first-token signal,
        so removal happens when the full response returns.  This is a
        slight overcount during decode but acceptable since non-streaming
        is rare for LLM serving.
        """
        if self._is_dry_run_outsource(adapter, model_id):
            await asyncio.sleep(0.1)  # Simulate minimal network latency
            return self._make_dry_run_response(model_id, params.get("request_id"))
        try:
            return await super()._execute_adapter(adapter, model_id, messages, **params)
        finally:
            self._remove_from_shadow_queue(adapter, model_id, params.get("request_id"))

    def _on_first_token(self, adapter: BaseAdapter, model_id: str, params: dict[str, Any]) -> None:
        """Prefill done → remove from shadow queue immediately.

        Called by BaseRouter._execute_stream_adapter() the instant the
        first content token arrives.  This is the correct removal point
        for streaming because, under continuous batching, a request in
        the decode phase no longer blocks other prefills.
        """
        self._remove_from_shadow_queue(adapter, model_id, params.get("request_id"))

    async def _execute_stream_adapter(
        self,
        adapter: BaseAdapter,
        model_id: str,
        messages: list[dict[str, Any]],
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Stream adapter with shadow queue lifecycle tracking.

        Primary removal happens in ``_on_first_token`` (called by super
        when the first content chunk arrives).  The ``finally`` block is
        a safety net for streams that error or get cancelled before
        producing any token — ``_remove_from_shadow_queue`` is idempotent
        so a double-call is harmless.
        """
        if self._is_dry_run_outsource(adapter, model_id):
            import json as _json
            import time as _time
            import uuid as _uuid

            chunk_id = f"chatcmpl-dryrun-{_uuid.uuid4().hex[:12]}"
            created = int(_time.time())
            await asyncio.sleep(0.1)  # Simulate TTFT
            # Role chunk
            yield f'data: {_json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model_id, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})}\n\n'
            # Content chunk
            yield f'data: {_json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model_id, "choices": [{"index": 0, "delta": {"content": "[DRY-RUN] Outsourced request simulated"}, "finish_reason": None}]})}\n\n'
            # Finish chunk
            yield f'data: {_json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model_id, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
            yield "data: [DONE]\n\n"
            return

        try:
            async for chunk in super()._execute_stream_adapter(
                adapter, model_id, messages, **params
            ):
                yield chunk
        finally:
            # Safety net: if first token was never reached (error/cancel),
            # ensure the request doesn't leak in the shadow queue.
            self._remove_from_shadow_queue(adapter, model_id, params.get("request_id"))

    def _emit_nimbus_metrics(
        self,
        model_id: str,
        result: dict[str, Any],
        outsourcing_router: Any,
    ) -> None:
        """Emit Prometheus metrics after an outsourcing decision."""
        from serving.observability.metrics import (
            NIMBUS_EST_TTFT,
            NIMBUS_LOCAL_INFLIGHT,
            NIMBUS_QUEUE_DEPTH,
            NIMBUS_ROUTING_DECISIONS,
            NIMBUS_SLO_VIOLATIONS,
            ROUTING_STRATEGY_SELECTED,
            normalize_model_label,
        )

        model_label = normalize_model_label(model_id)
        decision = result.get("routing_decision", "local")

        NIMBUS_ROUTING_DECISIONS.labels(model=model_label, decision=decision).inc()
        ROUTING_STRATEGY_SELECTED.labels(model=model_label, strategy="nimbus").inc()

        est_ttft = result.get("est_ttft_seconds")
        if est_ttft is not None:
            NIMBUS_EST_TTFT.labels(model=model_label, decision=decision).observe(est_ttft)

        trigger = result.get("trigger", "none")
        if trigger != "none":
            NIMBUS_SLO_VIOLATIONS.labels(model=model_label, trigger=trigger).inc()

        # Queue depth gauge — SGLang's internal pending count
        sglang_port = ""
        with contextlib.suppress(Exception):
            sglang_port = str(outsourcing_router.local_adapter.config.base_url or "")
        NIMBUS_QUEUE_DEPTH.labels(model=model_label, sglang_port=sglang_port).set(
            result.get("sglang_pending_count", 0)
        )

        # Shadow queue depth = in-flight local requests whose prefill has
        # not yet completed (for streaming) or that haven't returned yet
        # (for non-streaming).
        NIMBUS_LOCAL_INFLIGHT.labels(model=model_label).set(result.get("queue_length", 0))

    async def chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute chat completion with BaseRouter infrastructure + outsourcing metadata.

        Uses BaseRouter.chat_completion() for circuit breaker, health tracking,
        fallback, and metrics. Then merges outsourcing decision metadata into the
        response's _routing dict.

        Args:
            model_id: Model identifier
            messages: Chat messages
            **params: Additional parameters

        Returns:
            Chat completion response with merged _routing metadata
        """
        # Ensure request_id exists so _select_adapter() and this method
        # agree on the key used in _pending_decisions.  In production,
        # completions.py always sets it; this handles direct calls (tests).
        # Note: check for falsy (not just missing) to handle explicit None.
        if not params.get("request_id"):
            import uuid as _uuid

            params["request_id"] = f"req-{_uuid.uuid4().hex[:12]}"
        request_id = params["request_id"]

        # BaseRouter.chat_completion() calls _select_adapter() -> decide()
        # and handles circuit breaker, fallback, metrics, and _routing metadata.
        # Always clean up _pending_decisions — use BaseException to also catch
        # asyncio.CancelledError (which is BaseException, not Exception).
        try:
            resp = await super().chat_completion(model_id, messages, **params)
        except BaseException:
            self._pending_decisions.pop(request_id, None)
            raise

        # Merge outsourcing metadata into _routing if available.
        # BaseRouter already set resp["_routing"]["provider"] to the *actual*
        # executing adapter (post-fallback), so we only add outsourcing info.
        decision_info = self._pending_decisions.pop(request_id, None)
        if decision_info and "_routing" in resp:
            resp["_routing"]["outsourcing"] = {
                "decision": decision_info["routing_decision"],
                "request_id": decision_info["request_id"],
                "reason": decision_info["reason"],
                "queue_length": decision_info["queue_length"],
                "model_id": decision_info["model_id"],
                "cached_tokens": decision_info["cached_tokens"],
                "est_ttft_seconds": decision_info.get("est_ttft_seconds"),
                "sglang_pending_count": decision_info.get("sglang_pending_count", 0),
                "observed_ttft": decision_info.get("observed_ttft"),
                "trigger": decision_info.get("trigger", "none"),
            }

        return resp

    async def stream_chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ):
        """Stream chat completion with BaseRouter infrastructure + outsourcing metadata.

        Uses BaseRouter.stream_chat_completion() for circuit breaker, health
        tracking, fallback, TTFT metrics, etc. Injects a routing metadata chunk
        just before the ``[DONE]`` sentinel so it stays within valid SSE ordering.

        Args:
            model_id: Model identifier
            messages: Chat messages
            **params: Additional parameters

        Yields:
            SSE chunks from the adapter with routing metadata before [DONE]
        """
        import json as _json

        # Ensure request_id exists (same logic as chat_completion).
        # Check for falsy (not just missing) to handle explicit None.
        if not params.get("request_id"):
            import uuid as _uuid

            params["request_id"] = f"req-{_uuid.uuid4().hex[:12]}"
        request_id = params["request_id"]

        # Buffer the [DONE] sentinel so we can inject routing metadata before it.
        done_chunk: str | None = None

        try:
            async for chunk in super().stream_chat_completion(model_id, messages, **params):
                # Detect the SSE [DONE] sentinel precisely.  A content chunk
                # whose text happens to contain "[DONE]" must NOT be swallowed.
                # The real sentinel is exactly "data: [DONE]\n\n".
                if isinstance(chunk, str) and chunk.strip() == "data: [DONE]":
                    done_chunk = chunk
                    continue
                yield chunk
        except BaseException:
            self._pending_decisions.pop(request_id, None)
            raise

        # Inject routing metadata chunk (before [DONE]).
        # provider/base_url come from the decision-time adapter.  After a
        # BaseRouter fallback these may not match the actual executor, but
        # fallback is rare (<1%) while provider attribution affects 100% of
        # streaming requests for DB/cost logging.  "Occasionally wrong" beats
        # "always unknown" for observability.
        decision_info = self._pending_decisions.pop(request_id, None)
        if decision_info:
            routing_chunk = {
                "choices": [],
                "_routing": {
                    "provider": decision_info["adapter"].config.provider,
                    "base_url": decision_info["adapter"].config.base_url,
                    "outsourcing": {
                        "decision": decision_info["routing_decision"],
                        "request_id": decision_info["request_id"],
                        "reason": decision_info["reason"],
                        "queue_length": decision_info["queue_length"],
                        "model_id": decision_info["model_id"],
                        "cached_tokens": decision_info["cached_tokens"],
                        "est_ttft_seconds": decision_info.get("est_ttft_seconds"),
                        "sglang_pending_count": decision_info.get("sglang_pending_count", 0),
                        "observed_ttft": decision_info.get("observed_ttft"),
                        "trigger": decision_info.get("trigger", "none"),
                    },
                },
            }
            yield f"data: {_json.dumps(routing_chunk)}\n\n"

        # Now yield the [DONE] sentinel last
        if done_chunk:
            yield done_chunk

    def get_stats(self) -> dict[str, Any]:
        """Get statistics for all managed routers.

        Returns:
            Dictionary mapping model_id to router stats
        """
        return {
            model_id: router.get_stats() for model_id, router in self.outsourcing_routers.items()
        }
