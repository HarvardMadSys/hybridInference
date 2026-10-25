"""Interactive requests in flight per model, for batch work to yield to.

Every inference request holds a per-user concurrency slot while it runs
(:func:`serving.servers.concurrency.enforce_user_concurrency`), and for as long
as it holds it, it is counted here under its model's canonical id. The batch
worker (:mod:`serving.batch.scheduler`) leaves that many of a model's batch
slots to interactive traffic while ``BATCH_YIELD_TO_INTERACTIVE`` is on.

Process-local by design: each gateway process yields to the traffic it serves
itself. No lock, like the per-user limiter: the counts are only touched from
the event loop, and neither entering nor leaving awaits anything.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class InteractiveLoad:
    """In-flight interactive requests, counted per canonical model id."""

    def __init__(self) -> None:
        """Start with nothing in flight."""
        self._in_flight: dict[str, int] = {}

    @contextmanager
    def track(self, model_id: str) -> Iterator[None]:
        """Count one request on *model_id* while the ``with`` block runs."""
        self._in_flight[model_id] = self._in_flight.get(model_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._in_flight.get(model_id, 0) - 1
            if remaining > 0:
                self._in_flight[model_id] = remaining
            else:
                self._in_flight.pop(model_id, None)

    def in_flight(self, model_id: str) -> int:
        """Return how many requests on *model_id* are in flight now."""
        return self._in_flight.get(model_id, 0)


#: The process's count, kept by ``enforce_user_concurrency``.
INTERACTIVE_LOAD = InteractiveLoad()
