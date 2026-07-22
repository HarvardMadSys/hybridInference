"""Request-local RouteWise decision and trace values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from serving.adapters.base import BaseAdapter


def dedupe_failed_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return attempts in first-seen order with duplicate outcomes removed."""
    seen: set[tuple[str | None, str | None, str | None]] = set()
    result: list[dict[str, Any]] = []
    for attempt in attempts:
        endpoint = attempt.get("endpoint_id") or attempt.get("base_url") or attempt.get("provider")
        key = (
            endpoint if isinstance(endpoint, str) else None,
            attempt.get("error_type") if isinstance(attempt.get("error_type"), str) else None,
            attempt.get("error") if isinstance(attempt.get("error"), str) else None,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(attempt)
    return result


@dataclass(slots=True)
class ProviderReservation:
    """Idempotent ownership of an acquire-time resource release callback."""

    _release_callback: Callable[[], None] | None = None
    released: bool = False

    def release(self) -> None:
        """Release this reservation at most once."""
        if self.released:
            return
        self.released = True
        release_callback = self._release_callback
        self._release_callback = None
        if release_callback is not None:
            release_callback()


@dataclass(slots=True)
class RoutingTrace:
    """Mutable request-local history shared by all RouteWise re-solves."""

    request_id: str | None = None
    failed_attempts: list[dict[str, Any]] = field(default_factory=list)
    routewise_failed_attempts: list[dict[str, Any]] = field(default_factory=list)
    excluded_endpoint_ids: set[str] = field(default_factory=set)
    initial_selected_endpoint: str | None = None
    initial_selected_provider_type: str | None = None
    fallback_policy: str | None = None

    def begin_decision(self, metadata: dict[str, Any]) -> None:
        """Attach cumulative trace state to one newly selected dispatch."""
        selected_endpoint = metadata.get("selected_endpoint")
        selected_provider_type = metadata.get("selected_provider_type")
        if self.initial_selected_endpoint is None and isinstance(selected_endpoint, str):
            self.initial_selected_endpoint = selected_endpoint
        if self.initial_selected_provider_type is None and isinstance(selected_provider_type, str):
            self.initial_selected_provider_type = selected_provider_type
        self.apply_to(metadata)

    def record_failed_attempt(self, attempt: dict[str, Any]) -> None:
        """Record one surfaced execution failure for top-level attribution."""
        self.failed_attempts = dedupe_failed_attempts([*self.failed_attempts, attempt])

    def record_fallback(
        self,
        decision: RoutingDecision,
        attempt: dict[str, Any],
        *,
        fallback_policy: str,
    ) -> None:
        """Carry one retryable failure into the next decision and current metadata."""
        current = decision.metadata.get("failed_attempts")
        self.routewise_failed_attempts = dedupe_failed_attempts(
            [
                *self.routewise_failed_attempts,
                *(current if isinstance(current, list) else []),
                attempt,
            ]
        )
        for failed in self.routewise_failed_attempts:
            endpoint_id = failed.get("endpoint_id")
            if isinstance(endpoint_id, str) and endpoint_id:
                self.excluded_endpoint_ids.add(endpoint_id)
        self.fallback_policy = fallback_policy
        self.apply_to(decision.metadata)

    def apply_to(self, metadata: dict[str, Any]) -> None:
        """Merge cumulative trace fields into decision metadata in place."""
        if self.initial_selected_endpoint is not None:
            metadata["initial_selected_endpoint"] = self.initial_selected_endpoint
        if self.initial_selected_provider_type is not None:
            metadata["initial_selected_provider_type"] = self.initial_selected_provider_type
        if self.fallback_policy is None:
            return
        metadata["failed_attempts"] = list(self.routewise_failed_attempts)
        metadata["fallback_policy"] = self.fallback_policy
        metadata["fallback_attempts"] = len(self.routewise_failed_attempts)
        metadata["fallback_excluded_endpoints"] = sorted(self.excluded_endpoint_ids)


@dataclass(slots=True)
class RoutingDecision:
    """One concrete RouteWise dispatch and its lexically owned resources."""

    adapter: BaseAdapter
    reservation: ProviderReservation
    metadata: dict[str, Any]
    trace: RoutingTrace

    def release(self) -> None:
        """Release dispatch-owned refundable capacity idempotently."""
        self.reservation.release()


__all__ = [
    "ProviderReservation",
    "RoutingDecision",
    "RoutingTrace",
    "dedupe_failed_attempts",
]
