"""Provider quota snapshots for RouteWise S_Q candidates.

RouteWise request routing is synchronous, so provider quota APIs must not be
called on the request path.  This module keeps a small in-memory snapshot of
provider-side quota truth and applies local optimistic increments between
refreshes.

The first production scope is intentionally narrow: Chutes "Daily requests"
quota for one configured key.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from serving.admin.provider_quotas import fetch_chutes, fetch_minimax
from serving.schemas_admin import ProviderQuotaResult
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from .candidates import QuotaSource

QuotaFetcher = Callable[[], Awaitable[list[ProviderQuotaResult]]]
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderQuotaSnapshot:
    """One provider-side quota measurement plus route-time local increments."""

    source: QuotaSource
    used: float
    limit: float
    reset_at: datetime | None
    fetched_at: datetime
    local_increment: int = 0

    @property
    def effective_used(self) -> float:
        """Return provider usage plus local optimistic increments."""
        return min(self.limit, self.used + self.local_increment)

    @property
    def remaining(self) -> int:
        """Return the remaining request quota after local increments."""
        return max(0, int(self.limit - self.effective_used))

    @property
    def used_fraction(self) -> float:
        """Return effective quota usage as a clamped fraction."""
        if self.limit <= 0:
            return 1.0
        return min(max(self.effective_used / self.limit, 0.0), 1.0)


class ProviderQuotaSnapshotStore:
    """Thread-safe RouteWise view of provider quota snapshots."""

    def __init__(
        self,
        *,
        fetchers: dict[str, QuotaFetcher] | None = None,
    ) -> None:
        self._fetchers = fetchers or {"chutes": fetch_chutes, "minimax": fetch_minimax}
        self._snapshots: dict[QuotaSource, ProviderQuotaSnapshot] = {}
        self._local_increments: dict[QuotaSource, int] = {}
        self._lock = threading.Lock()

    def get(self, source: QuotaSource) -> ProviderQuotaSnapshot | None:
        """Return the effective snapshot for a quota source, if available."""
        with self._lock:
            snapshot = self._snapshots.get(source)
            if snapshot is None:
                return None
            return ProviderQuotaSnapshot(
                source=source,
                used=snapshot.used,
                limit=snapshot.limit,
                reset_at=snapshot.reset_at,
                fetched_at=snapshot.fetched_at,
                local_increment=self._local_increments.get(source, 0),
            )

    def consume(self, source: QuotaSource) -> bool:
        """Optimistically consume one request for a quota source."""
        with self._lock:
            snapshot = self._snapshots.get(source)
            if snapshot is None:
                return False
            local_increment = self._local_increments.get(source, 0)
            effective_used = snapshot.used + local_increment
            if effective_used >= snapshot.limit:
                return False
            self._local_increments[source] = local_increment + 1
            return True

    async def refresh_once(self, sources: Iterable[QuotaSource]) -> None:
        """Refresh snapshots for the requested quota sources."""
        unique_sources = {source for source in sources if source.provider in self._fetchers}
        if not unique_sources:
            return

        sources_by_provider: dict[str, list[QuotaSource]] = {}
        for source in unique_sources:
            sources_by_provider.setdefault(source.provider, []).append(source)

        for provider, provider_sources in sources_by_provider.items():
            fetcher = self._fetchers.get(provider)
            if fetcher is None:
                continue
            try:
                results = await fetcher()
            except (asyncio.TimeoutError, OSError):
                logger.warning(
                    "routewise_quota_snapshot_refresh_failed",
                    extra={
                        "event": "routewise_quota_snapshot_refresh_failed",
                        "provider": provider,
                    },
                )
                continue
            except Exception:
                logger.exception(
                    "routewise_quota_snapshot_refresh_failed",
                    extra={
                        "event": "routewise_quota_snapshot_refresh_failed",
                        "provider": provider,
                    },
                )
                continue
            self._store_provider_results(provider_sources, results)

    def _store_provider_results(
        self,
        sources: list[QuotaSource],
        results: list[ProviderQuotaResult],
    ) -> None:
        now = datetime.now(timezone.utc)
        updates: dict[QuotaSource, ProviderQuotaSnapshot] = {}

        for source in sources:
            match = _find_usage(results, source)
            if match is None:
                continue
            used, limit, reset_at = match
            updates[source] = ProviderQuotaSnapshot(
                source=source,
                used=used,
                limit=limit,
                reset_at=reset_at,
                fetched_at=now,
            )

        if not updates:
            return

        with self._lock:
            for source, snapshot in updates.items():
                self._snapshots[source] = snapshot
                self._local_increments[source] = 0


def _find_usage(
    results: list[ProviderQuotaResult],
    source: QuotaSource,
) -> tuple[float, float, datetime | None] | None:
    for result in results:
        if result.name != source.provider or not result.ok:
            continue
        for usage in result.usages:
            if usage.label != source.usage_label or usage.unit != source.unit:
                continue
            if usage.used is None or usage.limit is None or usage.limit <= 0:
                continue
            return float(usage.used), float(usage.limit), usage.reset_at
    return None
