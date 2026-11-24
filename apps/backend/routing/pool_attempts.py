"""One attempt delegated to an execution backend, and what a request keeps of them.

A router that composes backends -- :class:`routing.hybrid.HybridRouter`, and the
greedy router's local and cloud pools (:mod:`routing.greedy`) -- runs each
attempt the same way, whatever order it plans them in:

- the instruction is checked before the backend runs, so a mismatched one is a
  composition error and never counted as an attempt (``check_dispatch``);
- an admission refusal (``TargetUnavailableError``, ``AllCircuitsOpenError``)
  means nothing reached an upstream, so it is kept apart from failures;
- a failure is recorded as an attempt, with the endpoint it actually ran;
- an attempt that left a queue unsent -- the caller's predicate says which errors
  mean that -- is neither: nothing was sent, and the caller decides where the
  request goes next;
- a stream is committed at the first chunk the client can see, after which an
  error propagates instead of handing over to another candidate.

The planning stays with each router: which attempt runs next, with which
options, and whether the plan goes on. :class:`AttemptLedger` is the request's
record of what its attempts produced, and the one place the surfaced error is
chosen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

from routing.dispatch import (
    DispatchMismatchError,
    EndpointBinding,
    backend_pool_id,
    check_dispatch,
    dispatch_for_attempt,
)
from routing.hybrid_attempt_history import attach_attempt_history, attempt_record
from routing.hybrid_stream_frames import carries_routing, is_client_visible, tag_backend_chunk
from routing.routers import (
    AllCircuitsOpenError,
    TargetUnavailableError,
    request_refusals_end_fallback,
    select_surfaced_error,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from routing.backends import RoutingBackend
    from routing.decisions import RoutingTarget
    from routing.protocols import RoutingRequestOptions

    #: Feedback attribution: called with the backend's name, the ``_routing``
    #: block an attempt produced (or None) and the endpoint the plan named.
    Remember = Callable[[str, Any, "str | None"], None]
    #: Whether an attempt's error means it left a queue without being sent.
    LeftUnsent = Callable[[BaseException], bool]

__all__ = [
    "AttemptLedger",
    "PoolAttempt",
    "StreamOutcome",
    "chat_attempt",
    "stream_attempt",
]


@dataclass(frozen=True, slots=True)
class PoolAttempt:
    """One dispatch to one backend, as its router planned it.

    Attributes:
        backend: The backend that runs it.
        backend_name: Its name, as routing metadata and feedback record it.
        endpoint_id: The endpoint the plan named, or None for a pool's own pick.
        binding: The execution binding for that endpoint, when the plan names
            one the backend resolved.
        exact: Whether the backend must use ``endpoint_id`` and no other.
        routing_options: The options this attempt is dispatched with.
        target: The policy's target, passed to the backend's own selection.
    """

    backend: RoutingBackend
    backend_name: str
    endpoint_id: str | None
    binding: EndpointBinding | None = None
    exact: bool = False
    routing_options: RoutingRequestOptions | None = None
    target: RoutingTarget | None = None


@dataclass
class AttemptLedger:
    """What one request's attempts have produced so far, in order.

    ``attempts`` and ``errors`` are appended together, one per failed attempt.
    ``refusals`` are admission refusals and ``unsent`` the attempts that left a
    queue unsent: in neither was anything sent, so neither is an attempt record,
    and neither takes part in choosing the error a caller is told about.
    ``dispatched`` is every endpoint a failed attempt actually ran.
    """

    attempts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)
    refusals: list[BaseException] = field(default_factory=list)
    unsent: list[BaseException] = field(default_factory=list)
    dispatched: set[str] = field(default_factory=set)

    def record_failure(
        self,
        backend_name: str,
        endpoint_id: str | None,
        exc: BaseException,
        *,
        ran: bool = True,
    ) -> None:
        """Record one failed attempt; ``ran`` is False for one that produced nothing.

        The endpoint an attempt ran comes from its error's ``_routing`` block when
        it has one: a pool's own pick can differ from the endpoint the plan
        named, and a plan must not dispatch the same endpoint twice.
        """
        self.attempts.append(attempt_record(backend_name, endpoint_id, exc))
        self.errors.append(exc)
        if not ran:
            return
        routing = getattr(exc, "_routing", None)
        observed = routing.get("endpoint_id") if isinstance(routing, dict) else None
        if not isinstance(observed, str) or not observed:
            observed = endpoint_id
        if observed is not None:
            self.dispatched.add(observed)

    def refusals_end_plan(self, model_id: str) -> bool:
        """Return whether enough routes refused the request itself to stop the plan.

        The single router's bound (``request_refusals_end_fallback``), so a
        composed model sends a malformed request to no more routes than the
        shared router would.
        """
        return request_refusals_end_fallback(
            model_id,
            [
                (record.get("endpoint_id"), error)
                for record, error in zip(self.attempts, self.errors, strict=False)
            ],
        )

    def raise_after(
        self,
        nothing_tried: Callable[[], BaseException],
        *,
        mark: Callable[[dict[str, Any]], None] | None = None,
    ) -> NoReturn:
        """Raise the error the caller should be told about, once the plan is over.

        The single-router rule (``select_surfaced_error``) picks among the
        failures, and every attempt is attached to the one raised; ``mark`` then
        edits that error's routing block, when it has one. With no failure, the
        first admission refusal says why nothing could be used, then the first
        attempt that left a queue unsent. ``nothing_tried`` builds the error for a
        plan that produced none of these.
        """
        if self.errors:
            surfaced = self.errors[select_surfaced_error(self.errors)]
            attach_attempt_history(surfaced, self.attempts)
            routing = getattr(surfaced, "_routing", None)
            if mark is not None and isinstance(routing, dict):
                mark(routing)
            raise surfaced
        if self.refusals:
            raise self.refusals[0]
        if self.unsent:
            raise self.unsent[0]
        raise nothing_tried()


def _check(attempt: PoolAttempt, model_id: str) -> None:
    """Refuse a mismatched instruction before the backend can produce any I/O."""
    check_dispatch(
        attempt.backend,
        dispatch_for_attempt(
            pool_id=backend_pool_id(attempt.backend),
            model_id=model_id,
            binding=attempt.binding,
            exact=attempt.exact,
        ),
        model_id,
    )


async def chat_attempt(
    attempt: PoolAttempt,
    *,
    model_id: str,
    messages: list[dict[str, Any]],
    params: dict[str, Any],
    ledger: AttemptLedger,
    remember: Remember | None = None,
    left_unsent: LeftUnsent | None = None,
) -> dict[str, Any] | None:
    """Run one non-streaming attempt; return its response, or None when it did not serve.

    A None says the ledger now holds why: a refusal, an attempt that left a
    queue unsent, or a failure. Feedback attribution (``remember``) hears every
    attempt that reached an upstream, served or failed.

    Raises:
        DispatchMismatchError: The instruction does not fit the backend. Nothing
            was sent, and the request must not go on to another backend.
    """
    _check(attempt, model_id)
    try:
        response = await attempt.backend.chat_completion(
            model_id,
            messages,
            routing_options=attempt.routing_options,
            target=attempt.target,
            **params,
        )
    except (TargetUnavailableError, AllCircuitsOpenError) as exc:
        # Whether an exact binding or a pool delegation, nothing reached an
        # upstream: the endpoint was not admissible or had no capacity left.
        ledger.refusals.append(exc)
        return None
    except DispatchMismatchError:
        # A composition error from a range check inside the backend. Nothing was
        # sent, and it must not fall through to a backend nobody chose.
        raise
    except Exception as exc:
        if left_unsent is not None and left_unsent(exc):
            ledger.unsent.append(exc)
            return None
        if remember is not None:
            remember(attempt.backend_name, getattr(exc, "_routing", None), attempt.endpoint_id)
        ledger.record_failure(attempt.backend_name, attempt.endpoint_id, exc)
        return None
    if remember is not None:
        remember(attempt.backend_name, response.get("_routing"), attempt.endpoint_id)
    return response


@dataclass
class StreamOutcome:
    """How one streaming attempt ended, read once its stream is exhausted.

    ``served`` is True when the attempt streamed its answer through. False means
    it ended before the client saw anything, and the ledger holds why.
    """

    served: bool = False


def _chunk_routing(chunk: Any) -> Any:
    """Return the ``_routing`` block a chunk carries, parsing only a frame that can."""
    payload = chunk if isinstance(chunk, dict) else {}
    if isinstance(chunk, str) and chunk.startswith("data: ") and carries_routing(chunk):
        try:
            payload = json.loads(chunk[6:].strip())
        except ValueError:
            payload = {}
    return payload.get("_routing") if isinstance(payload, dict) else None


async def stream_attempt(
    attempt: PoolAttempt,
    *,
    model_id: str,
    messages: list[dict[str, Any]],
    params: dict[str, Any],
    ledger: AttemptLedger,
    outcome: StreamOutcome,
    lead_frame: Callable[[], str | None] | None = None,
    stamp: Callable[[dict[str, Any]], None] | None = None,
    remember: Remember | None = None,
    left_unsent: LeftUnsent | None = None,
) -> AsyncIterator[Any]:
    """Run one streaming attempt, yielding its chunks tagged with the backend.

    The first chunk is held until the attempt has produced one, and then
    ``lead_frame`` -- the caller's routing frame for this attempt, built only
    now, when the attempt is known to stream -- goes out ahead of it. The
    attempt is committed at the first chunk the client can see, which is not
    the first chunk forwarded: every backend emits a synthetic routing frame
    first, which the serving layer drops before the response leaves the
    gateway. An error before that point leaves the ledger saying why and ends
    the iteration with ``outcome.served`` False, so the caller can try another
    candidate; after it, the error propagates, since restarting elsewhere would
    splice a second answer into one the client already holds.

    ``stamp`` edits the routing block of every frame the backend sends, after
    the backend tag: a backend that moves on to another of its routes sends a
    frame that replaces what the lead frame said, so a caller restates there
    what must survive it.

    Raises:
        DispatchMismatchError: The instruction does not fit the backend.
    """
    _check(attempt, model_id)
    stream = attempt.backend.stream_chat_completion(
        model_id,
        messages,
        routing_options=attempt.routing_options,
        target=attempt.target,
        **params,
    )
    buffered: list[Any] = []
    aclose = getattr(stream, "aclose", None)
    committed = False

    def heard(chunk: Any) -> None:
        if remember is not None:
            remember(attempt.backend_name, _chunk_routing(chunk), None)

    try:
        async for chunk in stream:
            heard(chunk)
            buffered.append(chunk)
            break
        if buffered:
            frame = lead_frame() if lead_frame is not None else None
            if frame is not None:
                yield frame
            for held in buffered:
                committed = committed or is_client_visible(held)
                yield tag_backend_chunk(held, attempt.backend_name, stamp=stamp)
            async for chunk in stream:
                # Once committed, only a frame that can still add an endpoint to
                # the dispatch record is read: a late routing frame may name an
                # attempt (a hedge leg) no earlier frame did.
                if not committed or carries_routing(chunk):
                    heard(chunk)
                committed = committed or is_client_visible(chunk)
                yield tag_backend_chunk(chunk, attempt.backend_name, stamp=stamp)
            outcome.served = True
            return
        # A stream that ended without producing anything never reached the
        # client, so it is a failed attempt and the plan may continue.
        ledger.record_failure(
            attempt.backend_name,
            attempt.endpoint_id,
            RuntimeError("stream produced no output"),
            ran=False,
        )
    except (TargetUnavailableError, AllCircuitsOpenError) as exc:
        if committed:  # pragma: no cover - unavailability precedes any output
            raise
        ledger.refusals.append(exc)
    except DispatchMismatchError:
        raise
    except Exception as exc:
        if not committed and left_unsent is not None and left_unsent(exc):
            ledger.unsent.append(exc)
            return
        if remember is not None:
            remember(attempt.backend_name, getattr(exc, "_routing", None), attempt.endpoint_id)
        if committed:
            raise
        ledger.record_failure(attempt.backend_name, attempt.endpoint_id, exc)
    finally:
        if callable(aclose):
            await aclose()
