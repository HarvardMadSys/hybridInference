"""Attach a fresh, control-plane-verified proxy set before resolving identity."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from starlette.requests import Request

from serving.config.settings import get_settings
from serving.utils.kubernetes_proxy_trust import (
    KubernetesProxyConfig,
    KubernetesProxyDiscovery,
    KubernetesProxyTrustError,
    discovery_failure_code,
)
from serving.utils.logging import get_logger
from serving.utils.request_ip import DYNAMIC_PROXY_NETWORKS_STATE_KEY, get_client_ip_info

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

    from serving.config.settings import Settings

logger = get_logger(__name__)
_IP_INFO_STATE_KEY = "_hybrid_inference_client_ip_info"
_EXPIRES_STATE_KEY = "_hybrid_inference_dynamic_proxy_expires_at"
_DISCOVERY_MS_STATE_KEY = "_hybrid_inference_proxy_discovery_ms"
# A sanity bound for the logged stage time, not a timeout: discovery itself is
# bounded by its own refresh budget and this never shortens a request.
_MAX_DISCOVERY_MS = 600_000.0


def proxy_discovery_required(settings: Settings) -> bool:
    """Whether this request must discover proxy membership.

    Only when discovery is configured *and* the master ``TRUST_PROXY_HEADERS``
    switch is on. With the master switch off no forwarded identity is trusted,
    so membership is neither read nor required for readiness.
    """
    return settings.trust_kubernetes_proxy_discovery and settings.trust_proxy_headers


def proxy_discovery_ready(request: Request) -> bool:
    """Whether this probe observed nonempty, unexpired proxy membership."""
    networks = getattr(request.state, DYNAMIC_PROXY_NETWORKS_STATE_KEY, ())
    deadline = getattr(request.state, _EXPIRES_STATE_KEY, 0.0)
    return bool(networks) and time.monotonic() < deadline


def proxy_discovery_ms(request: Request) -> float | None:
    """Milliseconds this request spent in the discovery stage, if it ran one.

    Covers both lock waits and any refresh, so a cache hit, contention behind
    another request's refresh and a failed refresh are each visible. ``None``
    when discovery is disabled, the master switch is off, or the path is
    liveness.
    """
    value = getattr(request.state, _DISCOVERY_MS_STATE_KEY, None)
    if type(value) is float and 0.0 <= value <= _MAX_DISCOVERY_MS:
        return value
    return None


def _elapsed_ms(started: float, finished: float) -> float:
    elapsed = (finished - started) * 1000
    if not elapsed >= 0.0:  # also rejects NaN from a misbehaving clock
        return 0.0
    return round(min(elapsed, _MAX_DISCOVERY_MS), 3)


class KubernetesProxyTrustMiddleware:
    """Discover exact proxy hosts without buffering a request or its response.

    Discovery never writes the application's persisted proxy configuration.
    Each request gets one identity at admission, including a long-lived stream;
    later refreshes cannot change that request's logging or enforcement identity.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        discovery_factory: Callable | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.app = app
        self._factory = discovery_factory or KubernetesProxyDiscovery
        self._clock = clock
        self._discovery: KubernetesProxyDiscovery | None = None
        self._config: KubernetesProxyConfig | None = None
        self._lock = asyncio.Lock()
        self._failed = False

    async def _provider(self, config: KubernetesProxyConfig) -> KubernetesProxyDiscovery:
        async with self._lock:
            if self._discovery is None or config != self._config:
                previous = self._discovery
                self._discovery = self._factory(config)
                self._config = config
                if previous is not None:
                    await previous.aclose()
            return self._discovery

    async def aclose(self) -> None:
        """Release the per-worker control-plane client during ASGI shutdown."""
        async with self._lock:
            if self._discovery is not None:
                await self._discovery.aclose()
                self._discovery = None
                self._config = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Resolve proxy membership before downstream identity consumers run."""
        if scope["type"] == "lifespan":
            try:
                await self.app(scope, receive, send)
            finally:
                await self.aclose()
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        # A stage timing describes this admission only; never inherit one.
        state.pop(_DISCOVERY_MS_STATE_KEY, None)
        settings = get_settings()
        if not settings.trust_kubernetes_proxy_discovery:
            await self.app(scope, receive, send)
            return

        state[DYNAMIC_PROXY_NETWORKS_STATE_KEY] = ()
        state[_EXPIRES_STATE_KEY] = 0.0
        state.pop(_IP_INFO_STATE_KEY, None)
        # Liveness must not depend on the control plane or wait for its lock.
        # Readiness below /health/ready still requires fresh discovery while
        # the master switch is on; with it off nothing is discovered or needed.
        if proxy_discovery_required(settings) and scope.get("path") not in ("/health", "/health/"):
            config = KubernetesProxyConfig(
                namespace=settings.kubernetes_proxy_namespace,
                service_name=settings.kubernetes_proxy_service_name,
                service_uid=settings.kubernetes_proxy_service_uid,
                workload_uid=settings.kubernetes_proxy_workload_uid,
                api_url=settings.kubernetes_proxy_api_url,
                ca_file=settings.kubernetes_proxy_ca_file,
                token_file=settings.kubernetes_proxy_token_file,
                max_age_seconds=settings.kubernetes_proxy_max_age_seconds,
            )
            started = self._clock()
            try:
                try:
                    discovery = await self._provider(config)
                    snapshot = await discovery.get_snapshot()
                finally:
                    # Both lock waits and any refresh, whether it succeeded or not.
                    state[_DISCOVERY_MS_STATE_KEY] = _elapsed_ms(started, self._clock())
                if time.monotonic() >= snapshot.expires_at:
                    raise KubernetesProxyTrustError("snapshot_expired")
                state[DYNAMIC_PROXY_NETWORKS_STATE_KEY] = snapshot.networks
                state[_EXPIRES_STATE_KEY] = snapshot.expires_at
            except Exception as exc:
                # No stale-set fallback, exception text, credentials, or API body:
                # only a fixed reason code. A missing authority is an unresolved
                # identity, not a Pod client.
                if not self._failed:
                    logger.warning(
                        "kubernetes_proxy_discovery_unavailable",
                        extra={"reason": discovery_failure_code(exc)},
                    )
                self._failed = True
            else:
                if self._failed:
                    logger.info("kubernetes_proxy_discovery_recovered")
                self._failed = False

        get_client_ip_info(Request(scope))
        await self.app(scope, receive, send)
