"""Routing backends: the interchangeable execution domains of a hybrid router.

A backend exposes one execution capability -- a local inference fleet or a
cloud provider set -- behind the same request contract as every serving router
(:class:`routing.protocols.RouterProtocol`). The backend owns no scheduling
policy: it answers requests it is given, reports the routing metadata it
already produced, and forwards feedback to the collaborators it wraps.

A backend sits on one side of a dispatch boundary, and the boundary has two
shapes. A :class:`LeafBackend` is the end of the recursion: it executes one bound
endpoint and has no selection of its own. A :class:`TreeBackend` holds a routing
subtree and delegates to a scoped internal router. ``LocalBackend`` and
``CloudBackend`` are deployment-ownership wrappers over that second shape; the
distinction that matters for a dispatch is which of the two a backend is, not
which domain it belongs to.

The implementations live in focused modules; this module is the entry point that
re-exports them, so every existing import keeps resolving:

``backends_contract``
    :class:`RoutingBackend` (the execution-domain contract) and
    :class:`RoutingBackendBase` (shared delegation, lifecycle and route
    introspection).
``backends_leaf``
    :class:`LeafBackend`.
``backends_tree``
    :class:`TreeBackend`.
``backends_domains``
    :class:`LocalBackend`, :class:`CloudBackend` and :class:`FixedCloudBackend`.
``backends_routewise``
    :class:`RouteWiseCloudBackend`.

Architecture status is deliberate:

- ``RoutingBackend``, ``LeafBackend`` and ``TreeBackend`` are the intended
  execution-boundary contract (leaf versus routing subtree).
- ``LocalBackend``, ``CloudBackend`` and ``FixedCloudBackend`` are intended
  deployment-role implementations of that tree boundary, not temporary
  compatibility shims.
- ``RouteWiseCloudBackend`` is the optional scoped compatibility wrapper for
  the concrete hybrid composition. It is not RouteWise's architectural home;
  a ``router: routewise`` model retains its peer router and full candidate pool.

Candidate ranges are explicit construction inputs. A cloud backend built over a
table that also contains local endpoints binds a
:class:`routing.route_scope.RouteScopeView` so primaries, fallbacks and the
router's own background probes cannot reach outside the declared scope. Nothing
here infers ownership from a hostname, URL or provider name.
"""

from __future__ import annotations

from routing.backends_contract import RoutingBackend, RoutingBackendBase
from routing.backends_domains import CloudBackend, FixedCloudBackend, LocalBackend
from routing.backends_leaf import LeafBackend
from routing.backends_routewise import RouteWiseCloudBackend
from routing.backends_tree import TreeBackend

__all__ = [
    "CloudBackend",
    "FixedCloudBackend",
    "LeafBackend",
    "LocalBackend",
    "RouteWiseCloudBackend",
    "RoutingBackend",
    "RoutingBackendBase",
    "TreeBackend",
]
