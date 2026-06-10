"""Quota pools for RouteWise S_Q routing.

Every quota provider must expose a queryable usage API: the route declares a
``quota_source`` and the provider-reported usage is the truth source for the
pool. There is deliberately no locally-accounted fallback — counting an
externally enforced quota in-process means guessing the provider's reset
semantics (fixed window? rolling? session-anchored?), and a wrong guess
either strands quota or overruns it. Providers without a usage API should be
modeled as on-demand instead.

:class:`QuotaPool` reads the latest snapshot from
:class:`~routing.routewise.quota_snapshot.ProviderQuotaSnapshotStore` and
applies optimistic local increments between refreshes. Window semantics
(daily, 5-hour, weekly, ...) live entirely on the provider side; the pool
only consumes ``used / limit / reset_at``.

The shadow-price math itself lives in
:func:`effective_cost.quota_shadow_price_usd`, which reads ``L/U`` from the
workload :class:`CostEnvelopeEstimator`; pools only own depletion accounting.

Quota is counted in **requests** (not tokens), matching the online knapsack
formulation in the paper: each request routed to S_Q consumes one slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from .candidates import QuotaPolicy, QuotaSource
    from .quota_snapshot import ProviderQuotaSnapshotStore

logger = get_logger(__name__)


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

    def _snapshot(self):
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
