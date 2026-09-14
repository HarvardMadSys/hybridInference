"""Explicit dispatch instructions shared by a router and the backends it delegates to.

A router decides; a backend provides callable inference capability. Those are two
different jobs, and this module is the seam between them: it names the two things
a router can ask a backend to do, so "execute exactly this endpoint" and "hand
this request to that pool, you choose" cannot be confused with each other.

``ExecuteEndpoint``
    The caller has already chosen. The backend calls that endpoint and reports
    what happened. It may not resample, substitute a different endpoint, or walk
    a route of its own -- a leaf dispatch ends at one endpoint.

``DelegatePool``
    The caller grants selection rights inside a named pool. The backend's router
    chooses within its declared scope, including its own retries and hedging.

The two are not interchangeable, and the direction of the mistake matters: a
pool handed a leaf instruction would silently re-select, and a leaf handed a pool
instruction would have to invent a selection it has no scope for. Both are
refused by :func:`check_dispatch` before any upstream I/O happens.

Compatibility with the request options the older entry points use is deliberate
and one-way: :func:`dispatch_for_attempt` maps the existing
``preferred_endpoint_id`` / ``require_target`` / ``allow_fallback`` combination
onto one of these instructions. It never upgrades a *preference* into a hard
target -- a preference keeps delegating, which is what it has always meant.

Not carried here yet, and named so the gap is visible rather than implied: the
adapter and routing-snapshot fields the design calls for in a binding, and the
end-to-end deadline / attempt budget context. Both belong with the leaf
execution work, because populating them means resolving adapters at bind time and
moving ownership of the upstream attempt -- neither of which this contract
change does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from routing.backends import RoutingBackend

__all__ = [
    "BackendDispatch",
    "DelegatePool",
    "DispatchMismatchError",
    "EndpointBinding",
    "ExecuteEndpoint",
    "accepts_delegation",
    "bound_endpoint",
    "check_dispatch",
    "dispatch_for_attempt",
]


class DispatchMismatchError(RuntimeError):
    """Raised when a backend is handed a dispatch instruction it cannot carry out.

    A composition error, not an upstream failure: nothing has been sent when this
    is raised, so it must never be counted as an attempt or recorded as a
    provider fault. The command that built the composition chose the wrong
    backend for the instruction, or named an endpoint outside that backend's
    declared scope.
    """


@dataclass(frozen=True, slots=True)
class EndpointBinding:
    """One endpoint, resolved, together with the pool it was resolved inside.

    Args:
        endpoint_id: Canonical endpoint id, e.g. ``"combo-model:zai-api"``.
        model_id: Canonical model the endpoint was resolved for.
        pool_id: The pool the binding was resolved inside, when the caller knows
            it. A binding that crosses its pool's scope is the composition's
            mistake, and naming the pool is what lets it be reported as one.

    The adapter and route-snapshot fields the design calls for are deliberately
    absent for now: this phase introduces the instruction boundary without moving
    where an attempt executes, so a binding records *what* was resolved, not a
    direct handle to run it with.
    """

    endpoint_id: str
    model_id: str
    pool_id: str | None = None

    def __post_init__(self) -> None:
        if not self.endpoint_id:
            raise ValueError("EndpointBinding requires a non-empty endpoint_id")
        if not self.model_id:
            raise ValueError("EndpointBinding requires a non-empty model_id")


@dataclass(frozen=True, slots=True)
class ExecuteEndpoint:
    """Call exactly this endpoint; do not choose another."""

    binding: EndpointBinding


@dataclass(frozen=True, slots=True)
class DelegatePool:
    """Serve this request inside ``pool_id``, choosing the endpoint yourself."""

    pool_id: str

    def __post_init__(self) -> None:
        if not self.pool_id:
            raise ValueError("DelegatePool requires a non-empty pool_id")


#: What a router may ask a backend to do.
BackendDispatch = ExecuteEndpoint | DelegatePool


def dispatch_for_attempt(
    *,
    pool_id: str,
    model_id: str,
    endpoint_id: str | None,
    exact: bool,
) -> BackendDispatch:
    """Map one planned attempt onto the instruction that expresses it.

    This is the compatibility adapter between the existing request options and
    the explicit contract. ``exact`` is the caller saying "this endpoint and no
    other", which is what ``preferred_endpoint_id`` plus ``require_target``
    means once a plan has committed to a candidate; anything else delegates, and
    a preference stays a preference.

    ``exact`` without an endpoint is a composition error: a hard target that
    names nothing cannot be expressed, and turning it into a delegation would
    hand over selection the caller meant to keep.
    """
    if exact:
        if not endpoint_id:
            raise DispatchMismatchError(
                "an exact dispatch requires an endpoint_id; the caller asked for one "
                "endpoint to be executed and named none"
            )
        return ExecuteEndpoint(
            EndpointBinding(endpoint_id=endpoint_id, model_id=model_id, pool_id=pool_id)
        )
    return DelegatePool(pool_id=pool_id)


def check_dispatch(backend: RoutingBackend, instruction: BackendDispatch, model_id: str) -> None:
    """Refuse an instruction ``backend`` cannot carry out, before any upstream I/O.

    Read with ``getattr``: a backend that declares no dispatch role -- the
    minimal doubles in tests, and any compatibility wrapper that has not been
    given one -- accepts either instruction, which keeps this check additive. A
    backend that does declare a role is held to it.
    """
    checker = getattr(backend, "check_instruction", None)
    if callable(checker):
        checker(instruction, model_id)


def accepts_delegation(backend: Any) -> bool:
    """Return whether ``backend`` may be handed a pool delegation.

    Defaults to True, which is the compatibility behavior of the existing local
    and cloud wrappers: they delegate to a full router, so a delegation is
    exactly what they expect. A backend that declares ``accepts_delegation =
    False`` -- a leaf, which has one endpoint and no selection -- is refused
    instead, and the caller has to name the endpoint it wants executed.
    """
    declared = getattr(backend, "accepts_delegation", None)
    return True if declared is None else bool(declared)


def bound_endpoint(backend: Any) -> str | None:
    """Return the single endpoint ``backend`` is bound to, if it is bound to one.

    A leaf knows this; a pool does not, which is the whole difference between
    them. Used by the compatibility mapping to turn a domain-level step into an
    exact dispatch when the domain it names is a leaf, rather than dropping a
    step that is perfectly well defined.
    """
    declared = getattr(backend, "endpoint_id", None)
    return declared if isinstance(declared, str) and declared else None


def backend_pool_id(backend: Any) -> str:
    """Return the pool identity a backend delegates inside.

    Defaults to the backend's own name: a backend built without an explicit pool
    is its own pool, which is the identity its instructions have to match.
    """
    declared = getattr(backend, "pool_id", None)
    if isinstance(declared, str) and declared:
        return declared
    return str(getattr(backend, "name", "") or type(backend).__name__)
