"""The execution context every protocol's execution function is called with.

An online route and the batch dispatcher both hand a protocol's execution
function the same things, as values rather than as an HTTP request:

* who is asking -- the authenticated caller, as a read-only view;
* where the request came from -- :class:`ClientOrigin`: the client address,
  user agent and referer, the route path, and the few request headers a
  protocol reads, with Starlette's header semantics kept intact;
* the admission it runs under -- :class:`Admission`, through which a request
  first admitted against a virtual (auto) model is counted on the concrete
  model that serves it. Online, that is the tracker the concurrency dependency
  holds; a batch item holds none;
* which batch attempt it is, if any -- :class:`BatchAttribution`;
* whether the gateway's own request deadline has fired, for the one place that
  tells a timeout from a client leaving.

Nothing here skips authentication, model access, billing, logging or
admission: there is no field that could.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from starlette.datastructures import Headers

from serving.utils.session_identity import _AGENT_SESSION_HEADERS, CANONICAL_SESSION_HEADER

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from serving.batch.interactive_load import InteractiveLoadTracker
    from serving.utils.request_ip import ClientIpInfo

#: The request headers an execution function may read, compared lower-cased.
#: Every other header (credentials included) stays with the HTTP route.
ORIGIN_HEADERS: frozenset[str] = frozenset(
    {
        CANONICAL_SESSION_HEADER.lower(),
        *_AGENT_SESSION_HEADERS,
        # Forwarded upstream by the Messages surface.
        "anthropic-beta",
        # The Chat serializer's reasoning mode.
        "x-reasoning-passthrough",
        # Admin provider pinning on the Chat surface.
        "x-route-pin",
        # The synthetic-probe marker, honoured only for a trusted caller.
        "x-probe",
        # Per-call intent for served-identity headers; authorization remains
        # derived from the server-side caller context.
        "x-fi-served-identity",
    }
)


def origin_headers(raw: list[tuple[bytes, bytes]]) -> Headers:
    """The :data:`ORIGIN_HEADERS` entries of *raw*, as an immutable ``Headers``.

    The entries are kept in order and byte for byte, names included, so every
    lookup -- case-insensitive ``get``, the first value of a repeated header,
    ``getlist`` -- answers exactly as the request's own headers would.
    """
    return Headers(
        raw=[
            (name, value) for name, value in raw if name.lower().decode("latin-1") in ORIGIN_HEADERS
        ]
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientOrigin:
    """Where a request came from, as values the HTTP route or the worker supplies.

    ``client_ip`` resolves the client address on first use, as reading it off
    the request does: resolution logs degraded provenance, so it must happen
    exactly where it did before, not up front.
    """

    resolve_ip: Callable[[], ClientIpInfo]
    user_agent: str | None
    referer: str | None
    route_path: str
    #: Whether the request carried an ``Authorization`` header at all.
    authorization_header: bool
    headers: Headers

    @property
    def ip(self) -> ClientIpInfo:
        """The resolved client address."""
        return self.resolve_ip()

    def without(self, name: str) -> ClientOrigin:
        """This origin with every entry of header *name* removed, in any case."""
        lowered = name.lower().encode("latin-1")
        kept = [(k, v) for k, v in self.headers.raw if k.lower() != lowered]
        return ClientOrigin(
            resolve_ip=self.resolve_ip,
            user_agent=self.user_agent,
            referer=self.referer,
            route_path=self.route_path,
            authorization_header=self.authorization_header,
            headers=Headers(raw=kept),
        )

    @classmethod
    def for_batch(cls, *, path: str, ip: ClientIpInfo, user_agent: str) -> ClientOrigin:
        """The origin of a request the batch worker runs: no peer, no headers."""
        return cls(
            resolve_ip=lambda: ip,
            user_agent=user_agent,
            referer=None,
            route_path=path,
            authorization_header=False,
            headers=Headers(raw=[]),
        )


@dataclass(frozen=True, slots=True)
class BatchAttribution:
    """Which batch, item and attempt a request runs for. Plain data, not identity."""

    batch_id: str
    item_id: int | None
    attempt: int | None

    def log_metadata(self) -> dict[str, Any]:
        """The ``api_logs`` metadata tying a row to this attempt (unset values left out)."""
        out: dict[str, Any] = {"batch_job_id": self.batch_id}
        if self.item_id is not None:
            out["batch_item_id"] = self.item_id
        if self.attempt is not None:
            out["batch_attempt"] = self.attempt
        return out


class Admission(Protocol):
    """The admission a request runs under, as far as an execution function needs it."""

    def retarget(self, canonical_model_id: str) -> None:
        """Count the request on *canonical_model_id*, the model that serves it."""


class InteractiveAdmission:
    """The online admission ``enforce_user_concurrency`` holds for one request.

    Only retargets the load tracker; acquiring and releasing the user slot and
    the tracker stay with the dependency, which releases them once the response
    (a stream included) has been sent.
    """

    __slots__ = ("_tracker",)

    def __init__(self, tracker: InteractiveLoadTracker) -> None:
        """Wrap the dependency's tracker."""
        self._tracker = tracker

    def retarget(self, canonical_model_id: str) -> None:
        """Move this request's interactive-load count to *canonical_model_id*."""
        self._tracker.retarget(canonical_model_id)


class UntrackedAdmission:
    """No interactive-load tracker: a batch item, or a route whose dependency is replaced.

    A batch item is not interactive traffic, so retargeting it counts nothing,
    exactly as it never found a tracker before.
    """

    __slots__ = ()

    def retarget(self, canonical_model_id: str) -> None:
        """Do nothing: there is no interactive load to move."""


UNTRACKED = UntrackedAdmission()


def admission_from(slot: object) -> Admission:
    """The admission a route's concurrency dependency yielded, else :data:`UNTRACKED`."""
    return slot if isinstance(slot, InteractiveAdmission) else UNTRACKED


def read_only(user_ctx: Mapping[str, Any]) -> Mapping[str, Any]:
    """A read-only snapshot of an authenticated caller's context."""
    return MappingProxyType(dict(user_ctx))


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionContext:
    """Everything an execution function is told about the request besides its body."""

    #: The authenticated caller (``verify_api_key`` / ``authenticate_stored_key``).
    user_ctx: Mapping[str, Any]
    origin: ClientOrigin
    admission: Admission
    batch: BatchAttribution | None
    #: Online: whether ``TimeoutMiddleware``'s deadline has fired. None in Batch.
    timeout_probe: Callable[[], bool] | None

    def batch_log_metadata(self) -> dict[str, Any]:
        """The batch attribution for a log row, or nothing outside a batch."""
        return self.batch.log_metadata() if self.batch is not None else {}
