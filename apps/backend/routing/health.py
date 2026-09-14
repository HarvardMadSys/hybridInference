"""Simple health-check loop for deployment endpoints."""

# mypy: disable-error-code=no-any-unimported
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import aiohttp

from serving.utils.logging import get_logger

logger = get_logger(__name__)


class HealthMonitor:
    """Health monitoring for deployment endpoints.

    Periodically checks endpoint health via GET /health requests.

    The verdict is advisory. ``RoutingManager.apply()`` is the only reader, it
    runs once during bootstrap -- synchronously, before the prober task this
    class starts has had a single chance to run -- so no dispatch decision has
    ever been made from ``_status``. Until that changes (gating needs hysteresis
    and a guard against zeroing a model's last route, which is a routing change,
    not a reporting one), the probe's job is to *say* what it found: transitions
    are logged, and ``status_snapshot`` publishes the map on ``/routing``.
    """

    def __init__(self, timeout_s: int, interval_s: int) -> None:
        """Initialize health monitor.

        Args:
            timeout_s: Request timeout in seconds.
            interval_s: Check interval in seconds (0 to disable).
        """
        self.timeout_s = timeout_s
        self.interval_s = interval_s
        self._status: dict[str, bool] = {}
        self._task: asyncio.Task | None = None

    def is_healthy(self, endpoint: str) -> bool:
        """Check if an endpoint is healthy.

        Args:
            endpoint: Endpoint URL to check.

        Returns:
            True if healthy or unknown, False if known unhealthy.
        """
        return self._status.get(endpoint, True)

    def status_snapshot(self) -> dict[str, bool]:
        """Return a detached copy of the per-endpoint probe verdicts.

        Empty until the prober has completed its first pass, which is also the
        state ``is_healthy`` answers ``True`` from.
        """
        return dict(self._status)

    async def _check_once(self, session: Any, endpoint: str) -> bool:
        """Perform a single health check against the origin's /health path.

        This ignores any API prefix (e.g., "/v1") present in the endpoint URL
        to avoid 404s such as "/v1/health".
        """
        try:
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(endpoint)
            # Always probe the origin root at /health.
            health_url = urlunparse((parsed.scheme, parsed.netloc, "/health", "", "", ""))
            async with session.get(
                health_url, timeout=aiohttp.ClientTimeout(total=self.timeout_s)
            ) as resp:
                return bool(resp.status == 200)
        except Exception:
            return False

    async def _run(self, endpoints: list[str]) -> None:
        if self.interval_s <= 0:
            return
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                results = await asyncio.gather(
                    *(self._check_once(session, ep) for ep in endpoints),
                    return_exceptions=True,
                )
                for ep, ok in zip(endpoints, results, strict=False):
                    healthy = bool(ok) if not isinstance(ok, Exception) else False
                    self._record(ep, healthy)
                await asyncio.sleep(self.interval_s)

    def _record(self, endpoint: str, healthy: bool) -> None:
        """Store one probe verdict, logging only when it changes.

        Transitions only. A permanently dead endpoint probed every 60s would
        otherwise emit 1440 identical lines a day, which is how a signal becomes
        something operators filter out. The first pass compares against the
        optimistic default ``is_healthy`` already returns, so a first probe that
        succeeds is silent and a first probe that fails is not.
        """
        previous = self._status.get(endpoint, True)
        self._status[endpoint] = healthy
        if healthy == previous:
            return
        if healthy:
            logger.warning(
                "Health probe recovered: %s now answers /health (advisory only; "
                "routing does not consult this verdict)",
                endpoint,
                extra={
                    "event": "endpoint_probe_recovered",
                    "endpoint": endpoint,
                    "status": "healthy",
                },
            )
            return
        logger.warning(
            "Health probe failing: %s did not answer /health within %ss (advisory only; "
            "routing does not consult this verdict, so the endpoint keeps taking traffic)",
            endpoint,
            self.timeout_s,
            extra={
                "event": "endpoint_probe_failed",
                "endpoint": endpoint,
                "status": "unhealthy",
            },
        )

    def start(self, endpoints: list[str]) -> None:
        """Start health monitoring for given endpoints.

        Args:
            endpoints: List of endpoint URLs to monitor.
        """
        if self.interval_s <= 0 or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(endpoints))

    async def shutdown(self) -> None:
        """Stop health monitoring and cleanup resources."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
