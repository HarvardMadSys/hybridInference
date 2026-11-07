"""What one attempt at a batch item was billed, observed where it was billed.

Every inference surface already charges its caller at one point of its own:
``CostTracker.schedule_increment`` for chat completions (and the Responses API,
which delegates to it), ``embeddings._schedule_cost_increment`` and the
Anthropic surface's success logging. A batch attempt runs through the same
protocol executions, so it is charged there, once per execution, and nowhere
else. This module only *watches* those points: the batch dispatcher opens an
:class:`AttemptObserver` around the execution (:func:`observing`), and each
billing point reports what it charged to :func:`observe_billing`. Outside a
batch attempt there is no observer and the call does nothing.

An attempt the observer saw billed has a **known** cost (zero for a free
model). A successful attempt with no report -- pricing missing, or no log
store to bill through -- has an **unknown** cost, as has one abandoned
mid-flight; neither is recorded as zero.

The executions also tag their log rows with the batch, item and attempt
(``ExecutionContext.batch``), so an ``api_logs`` row and the attempt record
point at each other through the request id the observer captures.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class AttemptObserver:
    """Collects what one batch attempt's handler billed."""

    #: Each billing report: ``(cost_usd or None, usage, request_id)``.
    reports: list[tuple[float | None, dict[str, Any] | None, str | None]] = field(
        default_factory=list
    )
    #: The ``api_logs`` request id the handler logged under, when it said.
    request_id: str | None = None

    @property
    def billed(self) -> bool:
        """Whether any billing point ran for this attempt."""
        return bool(self.reports)

    @property
    def cost_known(self) -> bool:
        """Whether every billing report carried a cost."""
        return self.billed and all(cost is not None for cost, _, _ in self.reports)

    @property
    def cost_usd(self) -> float | None:
        """The total billed, or None when it is not (entirely) known."""
        if not self.cost_known:
            return None
        return float(sum(cost or 0.0 for cost, _, _ in self.reports))

    @property
    def usage(self) -> dict[str, Any] | None:
        """The usage the last billing report carried."""
        for _, usage, _ in reversed(self.reports):
            if usage is not None:
                return usage
        return None


_OBSERVER: ContextVar[AttemptObserver | None] = ContextVar("batch_attempt_observer", default=None)


@contextmanager
def observing() -> Iterator[AttemptObserver]:
    """Observe the billing points the enclosed handler call reaches."""
    observer = AttemptObserver()
    token = _OBSERVER.set(observer)
    try:
        yield observer
    finally:
        _OBSERVER.reset(token)


def observe_billing(
    *,
    cost_usd: float | None,
    usage: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Report what a billing point charged (None: it could not price the request).

    Called by the billing points of the inference handlers; a no-op outside a
    batch attempt. Never charges anything itself.
    """
    observer = _OBSERVER.get()
    if observer is None:
        return
    observer.reports.append((None if cost_usd is None else float(cost_usd), usage, request_id))
    if request_id and observer.request_id is None:
        observer.request_id = request_id


def note_request_id(request_id: str) -> None:
    """Tell the attempt which ``api_logs`` request id its handler logs under."""
    observer = _OBSERVER.get()
    if observer is not None and observer.request_id is None:
        observer.request_id = request_id
