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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType


class InteractiveLoadTracker:
    """One request's live model, which may be learned after admission.

    Auto-model requests enter the HTTP concurrency dependency before the auto
    selector has chosen a concrete model. A tracker therefore starts with an
    optional target and can be retargeted, without a gap or an ``await``, once
    dispatch knows which canonical model will actually serve the request.
    """

    def __init__(self, load: InteractiveLoad, model_id: str | None) -> None:
        self._load = load
        self._model_id = model_id
        self._active = False

    def __enter__(self) -> InteractiveLoadTracker:
        if self._active:
            raise RuntimeError("an interactive-load tracker cannot be entered twice")
        self._active = True
        self._load._increment(self._model_id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._load._decrement(self._model_id)
        self._active = False

    def retarget(self, model_id: str | None) -> None:
        """Move this request's count to *model_id*, if it is currently active."""
        if model_id == self._model_id:
            return
        if self._active:
            self._load._decrement(self._model_id)
            self._load._increment(model_id)
        self._model_id = model_id


class InteractiveLoad:
    """In-flight interactive requests, counted per canonical model id."""

    def __init__(self) -> None:
        """Start with nothing in flight."""
        self._in_flight: dict[str, int] = {}

    def track(self, model_id: str | None) -> InteractiveLoadTracker:
        """Return a context manager counting one request on *model_id*.

        ``None`` starts uncounted. The returned tracker can later be retargeted
        when an auto-model selector learns the concrete canonical model.
        """
        return InteractiveLoadTracker(self, model_id)

    def _increment(self, model_id: str | None) -> None:
        if model_id is not None:
            self._in_flight[model_id] = self._in_flight.get(model_id, 0) + 1

    def _decrement(self, model_id: str | None) -> None:
        if model_id is None:
            return
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
