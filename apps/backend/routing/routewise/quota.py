"""Provider-reported quota accounting for RouteWise S_Q routing.

Every quota provider must expose a queryable usage API: the route declares a
``quota_source`` and the provider-reported usage is the truth source for the
pool. There is deliberately no locally-accounted fallback — counting an
externally enforced quota in-process means guessing the provider's reset
semantics (fixed window? rolling? session-anchored?), and a wrong guess
either strands quota or overruns it. Providers without a usage API should be
modeled as on-demand instead.

RouteWise request routing is synchronous, so provider quota APIs are never
called on the request path. The pieces fit together as:

- :class:`ProviderQuotaSnapshot` -- one immutable provider-side measurement
  plus the local optimistic increments taken against it.
- :class:`ProviderQuotaSnapshotStore` -- process-wide store refreshed off the
  request path; window semantics (daily, 5-hour, weekly, ...) live entirely
  on the provider side and arrive through these snapshots.
- :class:`QuotaPool` -- the router-facing per-pool handle: one per
  ``quota_pool`` id, reading the store for ``used / limit / reset_at``.

The shadow-price math itself lives in
:func:`effective_cost.quota_shadow_price_usd`, which reads ``L/U`` from the
workload :class:`CostEnvelopeEstimator`; pools only own depletion accounting.

Quota is counted in **requests** (not tokens), matching the online knapsack
formulation in the paper: each request routed to S_Q consumes one slot.
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
    from .candidates import QuotaPolicy, QuotaSource

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
        """Optimistically consume one unit for a quota source.

        The increment is one unit of the snapshot's own accounting: one
        request for count-based usages, one percentage point for
        percent-based ones. Percent-based pools therefore over-consume
        locally on plans larger than ~100 requests per window -- see the
        percent-pool caveat in ``config/models.yaml`` before enabling one.
        """
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


class QuotaPool:
    """Provider-snapshot-backed quota pool.

    The provider's reported usage is the truth source; ``consume`` is an
    optimistic local increment reconciled at the next snapshot refresh. The
    pool is not ready until the first snapshot lands, so candidates are
    skipped rather than priced off invented state.
    """

    def __init__(
        self,
        store: ProviderQuotaSnapshotStore,
        source: QuotaSource,
        *,
        policy: QuotaPolicy,
    ) -> None:
        self._store = store
        self.source = source
        self.policy = policy
        self._limit_mismatch_warned = False

    @property
    def ready(self) -> bool:
        """Whether a provider snapshot has been observed yet."""
        return self._snapshot() is not None

    @property
    def limit(self) -> int:
        """Provider-reported limit when available, else the configured one."""
        snapshot = self._snapshot()
        if snapshot is None:
            return self.policy.limit
        return int(snapshot.limit)

    @property
    def remaining(self) -> int:
        """Requests remaining per the latest snapshot (0 when not ready)."""
        snapshot = self._snapshot()
        return 0 if snapshot is None else snapshot.remaining

    @property
    def used_fraction(self) -> float:
        """Used fraction per the latest snapshot (1.0 when not ready)."""
        snapshot = self._snapshot()
        return 1.0 if snapshot is None else snapshot.used_fraction

    def consume(self) -> bool:
        """Optimistically consume one slot against the snapshot store."""
        return self._store.consume(self.source)

    def _snapshot(self) -> ProviderQuotaSnapshot | None:
        snapshot = self._store.get(self.source)
        if (
            snapshot is not None
            and not self._limit_mismatch_warned
            and abs(snapshot.limit - self.policy.limit) >= 1
        ):
            logger.warning(
                "Quota pool for %s: provider reports limit=%s but route config "
                "declares limit=%d; using the provider-reported limit.",
                self.source,
                snapshot.limit,
                self.policy.limit,
            )
            self._limit_mismatch_warned = True
        return snapshot


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
