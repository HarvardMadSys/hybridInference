"""Attach a fresh, control-plane-verified proxy set before resolving identity."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from starlette.requests import Request

from serving.config.settings import get_settings
from serving.utils.kubernetes_proxy_trust import KubernetesProxyConfig, KubernetesProxyDiscovery
from serving.utils.logging import get_logger
from serving.utils.request_ip import DYNAMIC_PROXY_NETWORKS_STATE_KEY, get_client_ip_info

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

logger = get_logger(__name__)
_IP_INFO_STATE_KEY = "_hybrid_inference_client_ip_info"
_EXPIRES_STATE_KEY = "_hybrid_inference_dynamic_proxy_expires_at"


def proxy_discovery_ready(request: Request) -> bool:
    """Whether this probe observed nonempty, unexpired proxy membership."""
    networks = getattr(request.state, DYNAMIC_PROXY_NETWORKS_STATE_KEY, ())
    deadline = getattr(request.state, _EXPIRES_STATE_KEY, 0.0)
    return bool(networks) and time.monotonic() < deadline


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
    ) -> None:
        self.app = app
        self._factory = discovery_factory or KubernetesProxyDiscovery
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

        settings = get_settings()
        if not settings.trust_kubernetes_proxy_discovery:
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state[DYNAMIC_PROXY_NETWORKS_STATE_KEY] = ()
        state[_EXPIRES_STATE_KEY] = 0.0
        state.pop(_IP_INFO_STATE_KEY, None)
        # Liveness must not depend on the control plane or wait for its lock.
        # Readiness below /health/ready still requires fresh discovery.
        if settings.trust_proxy_headers and scope.get("path") not in ("/health", "/health/"):
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
            try:
                discovery = await self._provider(config)
                snapshot = await discovery.get_snapshot()
                if time.monotonic() >= snapshot.expires_at:
                    raise ValueError("expired proxy snapshot")
                state[DYNAMIC_PROXY_NETWORKS_STATE_KEY] = snapshot.networks
                state[_EXPIRES_STATE_KEY] = snapshot.expires_at
            except Exception:
                # No stale-set fallback, exception text, credentials, or API body.
                # A missing authority is an unresolved identity, not a Pod client.
                if not self._failed:
                    logger.warning("kubernetes_proxy_discovery_unavailable")
                self._failed = True
            else:
                if self._failed:
                    logger.info("kubernetes_proxy_discovery_recovered")
                self._failed = False

        get_client_ip_info(Request(scope))
        await self.app(scope, receive, send)
