"""Routing strategies for request distribution.

Provides:
- FixedRouter: Weighted random routing with automatic fallback
- RouteConfig, RoutingObservation, ProviderPinError
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from routing.protocols import RoutingRequestOptions
    from serving.adapters.base import BaseAdapter

from routing.backends import LeafBackend
from routing.dispatch import EndpointBinding, binding_for_adapter, execution_adapter
from routing.endpoint_health import DispatchClaim, EndpointHealthRegistry, _http_status_of
from routing.endpoints import endpoint_id_for_adapter
from routing.prefill_load import (
    PrefillLease,
    PrefillLoadTracker,
    conversation_fingerprint,
    estimate_prefill_tokens,
    priority_for_prefill,
    prompt_anchor,
)
from routing.route_table import EffectiveRoute, RouteTableSnapshot
from routing.streaming import has_non_empty_content
from routing.telemetry import failed_attempt, routing_chunk
from serving.exceptions import operator_safe_error
from serving.utils import context as req_ctx
from serving.utils.logging import get_logger

logger = get_logger(__name__)
_LEGACY_ROUTING_OPTION_UNSET = object()

# ============================================================================
# Exceptions
# ============================================================================


class ProviderPinError(ValueError):
    """Raised when a pinned provider is not found or disabled for a model."""


class AllCircuitsOpenError(RuntimeError):
    """Raised when all provider circuits are open (full outage)."""


class TargetUnavailableError(RuntimeError):
    """Raised when a dispatch required an endpoint that cannot be used right now.

    Distinct from a dispatch failure: nothing was sent upstream, so there is no
    upstream fault to report and no health sample to record. The circuit is open,
    or a concurrent request holds the endpoint's half-open probe. A caller that
    planned one candidate per attempt -- the hybrid layer does -- reads this as
    "skip this candidate and keep going", which is what the single router's own
    fallback loop does when a claim is refused.
    """


@runtime_checkable
class ManagedRouter(Protocol):
    """Router with async lifecycle hooks managed by application bootstrap."""

    async def start(self) -> bool:
        """Start router-owned background work.

        Returns True when this call activated the router and False when it was
        already running, so a wrapper can tell whether stopping it is its own
        to do.
        """
        ...

    async def stop(self) -> None:
        """Stop router-owned background work."""
        ...


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class RouteConfig:
    """Weighted adapter list for a model."""

    adapters: list[tuple[BaseAdapter, float]]
    raw_adapters: list[tuple[BaseAdapter, float, str]] | None = None
    canonical_model_id: str | None = None
    admin_only: bool = False
    required_role: str = "free"
    published: bool = True


@dataclass(kw_only=True)
class RoutingObservation:
    """Observation from a completed request, for online learning routers.

    RouteWiseRouter overrides record_observation() to update its cost model;
    FixedRouter ignores observations (no-op). All fields are keyword-only so
    request correlation, terminal disposition, and strategy-owned metadata stay
    explicit at construction sites. Supplied ``strategy_metadata`` is borrowed
    from its caller; observations and router consumers treat it as read-only.
    """

    model_id: str
    endpoint_id: str
    ttft_ms: float | None
    total_latency_ms: float
    token_count: int
    success: bool
    request_id: str | None = None
    terminal: bool = True
    prompt_tokens: int = 0
    completion_tokens: int = 0
    strategy_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Affinity:
    """Per-user provider pin for one model. TTL is monotonic time."""

    endpoint_id: str
    expires_at: float


# ============================================================================
# Helpers
# ============================================================================


def current_affinity_key() -> str | None:
    """Return the caller identity for this request, or None.

    Read independently of ``AFFINITY_ENABLED``: prefill accounting uses it to
    recognize a warm continuation, which stays useful even where sticky routing
    itself is switched off.
    """
    return req_ctx.get().get("affinity_key") or None


AFFINITY_TTL_SECONDS: float = 300.0
AFFINITY_SWEEP_THRESHOLD: int = 1000
AFFINITY_ENABLED: bool = os.environ.get("ROUTING_AFFINITY_ENABLED", "1") != "0"

# Why a route's effective weight stops matching the weight its configuration
# asked for. Reported separately, never merged, because each names a different
# owner and a different remediation -- see ``FixedRouter.get_route_exclusions``.
#: A ``provider_weight_overrides`` row set this (model, endpoint) pair's weight.
EXCLUSION_WEIGHT_OVERRIDE = "weight_override"
#: A ``disabled_providers`` row zeroes every adapter carrying that provider label.
EXCLUSION_PROVIDER_DISABLED = "provider_disabled"
#: ``RoutingManager`` reweighted the route from ``routing.yaml``'s local/remote
#: split. Only reachable where no weight-override resolver is attached: with one,
#: selection reads the registration weights and the manager's pass is inert.
EXCLUSION_ROUTING_YAML = "routing_yaml"
#: No runtime mechanism is involved: the model registry itself declares weight 0.
EXCLUSION_CONFIGURED_ZERO = "configured_zero"

#: Tolerance for comparing two weight shares. The configured and effective sides
#: are normalized by separate divisions, so an unchanged route can land a few
#: ULPs apart; without a tolerance every even split would be reported as
#: diverged.
_WEIGHT_SHARE_EPSILON = 1e-9


def _shares_differ(left: float, right: float) -> bool:
    """Return whether two normalized weight shares differ meaningfully."""
    return abs(left - right) > _WEIGHT_SHARE_EPSILON


def _weight_shares(weights: Sequence[float]) -> list[float]:
    """Return weights as shares of their own total, or all-zero when it is zero.

    Reporting only. Registration weights and selection weights live on different
    scales (see ``FixedRouter.describe_route_weights``); this is what makes the
    two comparable without either side having to be renormalized in place.
    """
    total = sum(weights)
    if total <= 0:
        return [0.0 for _ in weights]
    return [float(weight) / total for weight in weights]


# HTTP statuses that describe the *request the client sent*, as opposed to the
# state of whichever route happened to answer it. Only these may displace the
# primary route's error when a request is reported to the caller — see
# ``_select_surfaced_error``.
#
# Deliberately excluded, though they are all 4xx:
#   401 / 403 / 407 — a rejected credential is this gateway's own
#       misconfiguration, not anything in the caller's payload. 403 is also what
#       several providers return for "you've reached your concurrent request
#       limit", which clears on its own within seconds.
#   408 / 429 — upstream slowness and overload.
# Promoting any of those over a primary transport failure would relabel a
# recoverable capacity blip on one route as a terminal, non-retryable client
# error, and would tell users their key was revoked when it was not.
_REQUEST_DESCRIBING_STATUSES = frozenset({400, 404, 413, 422})


@dataclass(frozen=True)
class _RouteAttempt:
    """One dispatched route and the exception it raised.

    ``failed_attempt()`` renders an attempt down to a dict of strings and drops
    the exception object, so the ``failed_attempts`` telemetry list it builds
    cannot answer "which of these should the caller actually be told about?".
    This keeps the exceptions themselves, in dispatch order, alongside it.
    """

    adapter: BaseAdapter
    error: BaseException


def _describes_request(exc: BaseException) -> bool:
    """Return whether ``exc`` reports something wrong with the request itself.

    Borrows ``endpoint_health``'s status extraction rather than duck-typing the
    exception again here: the breaker already decides what counts as a client
    error from the same attributes, and two readers that disagree about where an
    upstream's status lives would classify the same failure two ways.
    """
    return _http_status_of(exc) in _REQUEST_DESCRIBING_STATUSES


def describes_request_error(exc: BaseException) -> bool:
    """Return whether ``exc`` reports something wrong with the request itself.

    Public because the rule now has two callers: the single-router fallback loop
    below, and the hybrid layer, which drives candidates drawn from more than one
    execution domain and therefore has to arrive at the same answer about which
    failure the caller is told.
    """
    return _describes_request(exc)


def select_surfaced_error(errors: Sequence[BaseException]) -> int:
    """Return the index of the error the caller should be told about.

    ``errors[0]`` is the primary attempt and stays the default: a request is
    normally reported the way the route chosen for it reported it. That default
    is overridden in exactly one case -- the primary's failure says nothing about
    the request while a later attempt's does. See
    ``_REQUEST_DESCRIBING_STATUSES`` for why only those statuses qualify.
    """
    if _describes_request(errors[0]):
        return 0
    for index, error in enumerate(errors[1:], start=1):
        if _describes_request(error):
            return index
    return 0


def _select_surfaced_error(attempts: Sequence[_RouteAttempt]) -> _RouteAttempt:
    """Choose which of several failed attempts the caller is told about.

    ``attempts[0]`` is the primary pick and remains the default: a request is
    normally reported the way the route chosen for it reported it, and every
    other attempt is preserved either way as ``failed_attempts`` telemetry.

    That default is overridden in exactly one case — the primary's failure says
    nothing about the request while some fallback's does. A dead endpoint raises
    a bare ``ClientConnectorError`` carrying no HTTP status at all, which the
    classifier in ``completions_stream`` defaults to 500 and renders as
    "Internal server error": a *retryable* status for a request that can never
    succeed, hiding the 400 a live route had already returned explaining what is
    malformed in the payload. Which of the two answers a user got came down to
    the weighted coin flip at selection time.

    Only request-describing statuses can win that way; see
    ``_REQUEST_DESCRIBING_STATUSES`` for why a fallback's 403 or 429 must not.
    """
    return attempts[select_surfaced_error([attempt.error for attempt in attempts])]


def _raise_surfaced_error(
    attempts: Sequence[_RouteAttempt],
    failed_attempts: list[dict[str, str]],
) -> NoReturn:
    """Raise the error the caller is told about, once every route has failed.

    Guarantees the raised exception carries ``_routing``. The error-log path
    reads ``exc._routing`` to attribute the failure to a real upstream; with it
    absent, ``completions_stream`` falls back to the provider it last saw on the
    wire — the final fallback *attempted*, not the one being reported — or to
    the ``"router"`` sentinel, which the provider-performance aggregations drop
    outright, taking cost accounting with them. The primary's error is given a
    block by its caller before fallback even begins, but a fallback's error has
    none, so surfacing one without this would reintroduce exactly the
    misattribution that block exists to prevent.

    Raises rather than returning the exception so the ``raise`` happens inside
    the caller's ``except`` block: Python then chains implicitly, giving the
    surfaced fallback error the primary's failure as its ``__context__`` and
    leaving a re-raised primary error's chain untouched. Returning it and
    writing ``raise ... from primary_error`` at the call site would instead
    claim a direct causal link that does not exist (two routes failed
    independently) and, when the primary is the one selected, make it its own
    cause.
    """
    selected = _select_surfaced_error(attempts)
    exc = selected.error
    routing = getattr(exc, "_routing", None)
    if isinstance(routing, dict):
        # ``failed_attempts`` is stored by reference, so the primary's block
        # already reflects every attempt appended after it was built; a block
        # attached further upstream (a nested router, an adapter) does not.
        routing.setdefault("failed_attempts", failed_attempts)
    else:
        routing = {
            "provider": selected.adapter.config.provider,
            "base_url": selected.adapter.config.base_url,
            "endpoint_id": endpoint_id_for_adapter(selected.adapter),
            "failed_attempts": failed_attempts,
        }
        exc._routing = routing  # type: ignore[attr-defined]
    if selected is not attempts[0]:
        # Same marker the success path puts on a response served by a fallback,
        # and it matters more here: this block's provider is the route that
        # produced the status being reported, not the route the request was
        # actually sent to. Without it an ``api_logs`` row reads as if routing
        # had picked this endpoint. ``setdefault`` so a block built further
        # upstream keeps whatever it already decided.
        routing.setdefault("fallback", True)
    raise exc


# ============================================================================
# FixedRouter
# ============================================================================


def adapter_supports_modalities(adapter: BaseAdapter, required: frozenset[str] | None) -> bool:
    """Return True if the adapter's route accepts every required input modality.

    ``required`` is the set of non-text modalities a request needs (e.g.
    ``{"image"}``). Each route inherits the model-level ``input_modalities``
    unless a route-level override narrows it, so this lets the router skip a
    fallback that cannot accept the media even when the model as a whole
    advertises that modality.
    """
    if not required:
        return True
    supported = set(getattr(adapter.config, "input_modalities", None) or ["text"])
    return required <= supported


def adapter_in_endpoint_scope(adapter: BaseAdapter, scope: frozenset[str] | None) -> bool:
    """Return whether ``adapter`` is inside an explicit endpoint/provider set.

    ``None`` means "no scope declared" and admits everything. An empty set
    admits nothing, which is the right reading for a backend that was told it
    owns no endpoint here.
    """
    if scope is None:
        return True
    return (
        endpoint_id_for_adapter(adapter) in scope
        or getattr(adapter.config, "provider", None) in scope
    )


class FixedRouter:
    """Weighted random routing with automatic fallback.

    Drop-in replacement for RouteExecutor. Selects adapters via weighted
    random selection and tries remaining adapters on failure.

    Args:
        params: Optional Pydantic ``FixedParams`` (passed by the strategy
            registry).  ``None`` keeps existing call-site behavior.
            ``params.local_fraction`` is currently informational; the existing
            weighted-random selection over ``routes`` is unchanged.
    """

    def __init__(
        self,
        params: Any = None,
        weight_override_resolver: Any | None = None,
        disabled_provider_resolver: Any | None = None,
        health_registry: EndpointHealthRegistry | None = None,
    ) -> None:
        self._health_registry = (
            health_registry if health_registry is not None else EndpointHealthRegistry()
        )
        self._lock = threading.RLock()
        self._affinity: dict[tuple[str, str], _Affinity] = {}
        self.routes: dict[str, RouteConfig] = {}
        # Keep the validated params accessible for future use (e.g. honoring
        # local_fraction in adapter selection).  Today FixedRouter ignores it
        # because per-route weights already encode local-vs-remote balance.
        self.params = params
        self.weight_override_resolver = weight_override_resolver
        # Admin kill switch: adapters whose provider is disabled are forced to
        # weight 0 so the existing ``weight > 0`` gates in selection and every
        # fallback loop skip them without any per-call-site change.
        self.disabled_provider_resolver = disabled_provider_resolver
        # In-flight prefill per endpoint. Weighted-random balances request
        # counts, which lets one mega-prefill monopolize a replica while its
        # siblings idle; this is the signal that lets selection see that.
        self._prefill_load = PrefillLoadTracker()
        # Divergence set last announced by ``log_route_weight_divergence``.
        # None (not an empty set) means "never reported", which is what lets a
        # gateway with no overrides boot silently.
        self._logged_weight_divergence: frozenset[tuple[Any, ...]] | None = None

    @property
    def prefill_load(self) -> PrefillLoadTracker:
        """Return the per-endpoint in-flight prefill tracker."""
        return self._prefill_load

    @property
    def endpoint_health_registry(self) -> EndpointHealthRegistry:
        """Return the process-scoped endpoint-health collaborator."""
        return self._health_registry

    @staticmethod
    def _resolve_pin_provider(
        routing_options: RoutingRequestOptions | None,
        params: dict[str, Any],
    ) -> str | None:
        """Resolve the explicit pin without forwarding router controls upstream."""
        option_pin = routing_options.pin_provider if routing_options is not None else None
        legacy_pin = params.pop("pin_provider", _LEGACY_ROUTING_OPTION_UNSET)
        if legacy_pin is _LEGACY_ROUTING_OPTION_UNSET:
            return option_pin
        if legacy_pin is None:
            return option_pin
        # One-release compatibility for direct FixedRouter callers. The public
        # serving path uses RoutingRequestOptions and never enters this branch.
        if option_pin is not None:
            raise TypeError("pin_provider was supplied both directly and in routing_options")
        if not isinstance(legacy_pin, str):
            raise TypeError("pin_provider must be a string or None")
        return legacy_pin

    def _ensure_health(self, endpoint_id: str) -> None:
        self._health_registry.ensure(endpoint_id)

    def _on_success(self, endpoint_id: str) -> None:
        self._health_registry.record_success(endpoint_id)

    def _on_failure(
        self,
        endpoint_id: str,
        *,
        reason: str = "error",
        detail: str | None = None,
        exc: BaseException | None = None,
    ) -> None:
        # A failing endpoint has most likely lost its prefix cache (restart,
        # OOM, a container replaced under the same id), so the hints describing
        # it stop being evidence. See PrefillLoadTracker.forget_endpoint.
        self._prefill_load.forget_endpoint(endpoint_id)
        self._health_registry.record_failure(
            endpoint_id,
            reason=reason,
            detail=detail,
            exc=exc,
        )

    def _drop_affinity(self, model_id: str) -> None:
        """Drop affinity entry for the current request's affinity_key + model.

        No-op if affinity_key is missing from req_ctx or no entry exists.
        """
        affinity_key = req_ctx.get().get("affinity_key")
        if not affinity_key:
            return
        with self._lock:
            self._affinity.pop((affinity_key, model_id), None)

    def _maybe_sweep_affinity_locked(self, now: float) -> None:
        """Drop expired affinity entries. Caller must hold self._lock."""
        if len(self._affinity) <= AFFINITY_SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of provider availability, circuit state and exclusions.

        The exclusion half is merged in -- and endpoints that appear *only* as
        excluded are added to the map -- because a zero-weighted route is never
        dispatched to, so it never reaches the health registry at all. Without
        it a model down to its last live route reads here exactly like a model
        that only ever had one: the health snapshot can only describe endpoints
        traffic has already been sent to.
        """
        status = self._health_registry.snapshot()
        for fact in self.get_route_exclusions():
            entry = status.setdefault(fact["endpoint_id"], {})
            models = entry.setdefault("excluded_from_models", [])
            if fact["model_id"] not in models:
                models.append(fact["model_id"])
            reasons = entry.setdefault("exclusion_reasons", [])
            for reason in fact["reasons"]:
                if reason not in reasons:
                    reasons.append(reason)
        for entry in status.values():
            # One endpoint can be excluded from several models for several
            # reasons; sort so the payload is stable across calls (and so tests
            # do not depend on dict iteration order).
            if "excluded_from_models" in entry:
                entry["excluded_from_models"].sort()
                entry["exclusion_reasons"].sort()
        return status

    def describe_route_weights(self) -> list[dict[str, Any]]:
        """Explain every published route's effective weight and what changed it.

        One entry per (model, endpoint) pair, carrying the weight the model
        registry asked for, the weight routing actually uses, and the mechanism
        behind any difference. Both weights are reported as **shares of the
        model's route table** -- each side normalized over that model's routes --
        because the two are kept on different scales internally (registration
        weights before normalization, selection weights after), and a report that
        mixed the scales would flag every multi-route model as diverged.

        This deliberately *mirrors* ``_get_effective_adapters`` instead of
        calling it. That method may await an async-only override resolver, which
        raises when called from inside a running event loop -- and this runs on
        ``/health/deep``, inside exactly such a loop. A diagnostic must never be
        the thing that takes the status endpoint down.
        ``test_describe_route_weights_agrees_with_selection`` pins the mirror
        against drift.

        Known limit: an override resolver that exposes only the async
        ``get_for_model`` cannot be read from here, so its overrides are reported
        as absent. That under-reports rather than raises, which is the right
        failure for a status surface; every resolver the gateway constructs
        exposes the sync snapshot.
        """
        disabled_resolver = self.disabled_provider_resolver
        facts: list[dict[str, Any]] = []
        for model_id, route in self._published_routes_snapshot():
            overrides = self._weight_override_snapshot(model_id)
            # What the model registry asked for, before anything at runtime.
            registered = route.raw_adapters or [
                (adapter, weight, endpoint_id_for_adapter(adapter))
                for adapter, weight in route.adapters
            ]
            # Where selection starts from, mirroring ``_get_effective_adapters``:
            # the pre-normalization registration weights when a readable override
            # resolver can rewrite them, otherwise the live normalized list --
            # which is also the one ``RoutingManager.apply()`` mutates in place.
            if overrides is None or not route.raw_adapters:
                baseline = [
                    (adapter, weight, endpoint_id_for_adapter(adapter))
                    for adapter, weight in route.adapters
                ]
                overrides = {}
            else:
                baseline = list(route.raw_adapters)
            # Keyed by adapter, not by position: ``RoutingManager.apply()``
            # rebuilds ``route.adapters`` covered-first, so it can reorder the
            # list relative to ``raw_adapters``.
            registered_shares = _weight_shares([weight for _, weight, _ in registered])
            registered_share = {
                adapter: share
                for (adapter, _weight, _endpoint_id), share in zip(
                    registered, registered_shares, strict=False
                )
            }
            baseline_share = _weight_shares([weight for _, weight, _ in baseline])
            effective_weights: list[float] = []
            per_route_reasons: list[list[str]] = []
            for index, (adapter, base_weight, endpoint_id) in enumerate(baseline):
                configured = registered_share.get(adapter, 0.0)
                reasons: list[str] = []
                # Attribution is by *mechanism*, never by comparing the route's
                # own configured and effective shares. Zeroing one route raises
                # every sibling's share, and blaming the survivor for the share
                # it inherited is how a report turns one operator action into a
                # line per route.
                if configured <= 0:
                    reasons.append(EXCLUSION_CONFIGURED_ZERO)
                elif _shares_differ(baseline_share[index], configured):
                    # Selection's starting weights are not the registry's, and
                    # no runtime override has been applied yet: what rewrote
                    # them is RoutingManager applying routing.yaml's local/remote
                    # split. Unreachable with a weight resolver attached, where
                    # selection reads the registration weights and the manager's
                    # pass is inert.
                    reasons.append(EXCLUSION_ROUTING_YAML)
                weight = float(base_weight)
                override = overrides.get(endpoint_id)
                if override is not None and float(override) != weight:
                    weight = float(override)
                    reasons.append(EXCLUSION_WEIGHT_OVERRIDE)
                if disabled_resolver is not None and disabled_resolver.is_disabled(
                    adapter.config.provider
                ):
                    weight = 0.0
                    reasons.append(EXCLUSION_PROVIDER_DISABLED)
                effective_weights.append(weight)
                per_route_reasons.append(reasons)
            effective_share = _weight_shares(effective_weights)
            for index, (adapter, _base_weight, endpoint_id) in enumerate(baseline):
                facts.append(
                    {
                        "model_id": model_id,
                        "endpoint_id": endpoint_id,
                        "provider": adapter.config.provider,
                        "base_url": adapter.config.base_url,
                        "configured_weight": registered_share.get(adapter, 0.0),
                        "effective_weight": effective_share[index],
                        "reasons": per_route_reasons[index],
                    }
                )
        return facts

    def get_route_exclusions(self) -> list[dict[str, Any]]:
        """Return the published routes automatic selection can never pick.

        A zero effective weight is skipped both by weighted selection and by
        every fallback loop (``if adapter == primary or weight <= 0``), so these
        routes are configured capacity that does not exist. The cause labels are
        kept separate rather than merged into one "disabled" flag: a
        ``provider_weight_overrides`` row is per (model, endpoint) and is undone
        in the admin console's routing-weights view, a ``disabled_providers``
        row is a provider-wide kill switch undone in the providers view, a
        ``routing.yaml`` split is undone in the overlay's routing file, and a
        weight of 0 in the model registry is undone in its models file.
        Different owners, different fixes.
        """
        return [fact for fact in self.describe_route_weights() if fact["effective_weight"] <= 0]

    def log_route_weight_divergence(self) -> None:
        """Log every route whose effective weight differs from its configured one.

        Called once at startup and again after each override / disabled-provider
        reload, so an operator reading the boot log can see the runtime weight
        table rather than having to query the operational store to discover that
        a model with six configured routes has one live one.

        Self-deduplicating on the divergence set: the refresh loops call this
        every time a reload reports a change, and a weight that has been zero
        for 71 days must not print a line per reload.
        """
        # A route the registry itself weights at zero is configuration, not
        # divergence, so it is reported by ``get_route_exclusions`` but never
        # announced here -- the boot log would otherwise carry a permanent line
        # for every deliberately parked route.
        diverged = [
            fact
            for fact in self.describe_route_weights()
            if set(fact["reasons"]) - {EXCLUSION_CONFIGURED_ZERO}
        ]
        signature = frozenset(
            (
                fact["model_id"],
                fact["endpoint_id"],
                fact["effective_weight"],
                tuple(fact["reasons"]),
            )
            for fact in diverged
        )
        with self._lock:
            previous = self._logged_weight_divergence
            if signature == previous:
                return
            self._logged_weight_divergence = signature
        if not diverged:
            # ``previous`` is None on the first call, so a gateway that boots
            # with no overrides at all stays silent instead of announcing that
            # nothing changed.
            if previous:
                logger.info(
                    "route_weight_divergence_cleared",
                    extra={"event": "route_weight_divergence_cleared"},
                )
            return
        for fact in diverged:
            zeroed = fact["effective_weight"] <= 0
            # A route zeroed at runtime is removed capacity and is what the
            # RCA had to find by hand; a route merely re-weighted is still
            # routable, so it is reported but not as a warning.
            log = logger.warning if zeroed else logger.info
            # The verb says what happened to the weight, never which mechanism
            # did it -- "overridden" is also the name of one of the four causes,
            # so a provider-disabled route announced as "overridden" would send
            # an operator to the wrong admin tab. The cause is in the brackets.
            log(
                "Route %s at runtime: %s via %s is %.4g (config: %.4g) [%s]",
                "zeroed" if zeroed else "re-weighted",
                fact["model_id"],
                fact["endpoint_id"],
                fact["effective_weight"],
                fact["configured_weight"],
                ", ".join(fact["reasons"]) or "unknown",
                extra={
                    "event": "route_weight_zeroed" if zeroed else "route_weight_overridden",
                    "model_id": fact["model_id"],
                    "endpoint_id": fact["endpoint_id"],
                    "provider": fact["provider"],
                    "configured_weight": fact["configured_weight"],
                    "effective_weight": fact["effective_weight"],
                    "reason": ", ".join(fact["reasons"]) or "unknown",
                },
            )

    def _published_routes_snapshot(self) -> list[tuple[str, RouteConfig]]:
        """Return one (canonical model id, route) pair per published route.

        Aliases share a ``RouteConfig`` by reference, so reporting per route key
        would list the same endpoints once per alias.
        """
        with self._lock:
            items = list(self.routes.items())
        seen_canonical_ids: set[str] = set()
        snapshot: list[tuple[str, RouteConfig]] = []
        for route_key, route in items:
            if not route.published:
                continue
            canonical_model_id = route.canonical_model_id or route_key
            if canonical_model_id in seen_canonical_ids:
                continue
            seen_canonical_ids.add(canonical_model_id)
            snapshot.append((canonical_model_id, route))
        return snapshot

    def _weight_override_snapshot(self, model_id: str) -> dict[str, float] | None:
        """Return the synchronous weight-override snapshot for one model.

        ``None`` means "no overrides are readable from here": either no resolver
        is attached -- the case ``_get_effective_adapters`` itself falls back to
        the route's own weights for -- or the one attached offers only the async
        ``get_for_model`` shape, which a diagnostic must not await (see
        ``describe_route_weights``). Reporting the second as "no overrides" can
        understate an override that is in fact set; every resolver the gateway
        constructs exposes the sync snapshot, and under-reporting beats a status
        endpoint that raises.
        """
        resolver = self.weight_override_resolver
        if resolver is None:
            return None
        get_snapshot = getattr(resolver, "get_snapshot_for_model", None)
        if get_snapshot is None:
            return None
        return dict(get_snapshot(model_id))

    def record_observation(self, obs: RoutingObservation) -> None:
        """Ignore observations because fixed routing has no online-learning state."""
        return None

    def iter_effective_routes(self) -> tuple[EffectiveRoute, ...]:
        """Return a stable effective-route snapshot built under the route lock."""
        with self._lock:
            return self._effective_routes_locked()

    def snapshot_for_transition(self, model_id: str) -> RouteTableSnapshot:
        """Capture published routes plus one staged canonical model privately."""
        with self._lock:
            routes = self._effective_routes_locked(include_unpublished_model_id=model_id)
            canonical_ids = {
                route_key: route.canonical_model_id or route_key
                for route_key, route in self.routes.items()
                if route.published or (route.canonical_model_id or route_key) == model_id
            }
        return RouteTableSnapshot(routes, canonical_ids)

    def _effective_routes_locked(
        self,
        *,
        include_unpublished_model_id: str | None = None,
    ) -> tuple[EffectiveRoute, ...]:
        snapshot: list[EffectiveRoute] = []
        seen_canonical_ids: set[str] = set()
        for route_key, route in self.routes.items():
            canonical_model_id = route.canonical_model_id or route_key
            if not route.published and canonical_model_id != include_unpublished_model_id:
                continue
            if canonical_model_id in seen_canonical_ids:
                continue
            seen_canonical_ids.add(canonical_model_id)
            snapshot.append(
                EffectiveRoute(
                    route_key=route_key,
                    canonical_model_id=canonical_model_id,
                    adapters=tuple(self._get_effective_adapters(route_key, route)),
                )
            )
        return tuple(snapshot)

    def canonical_id(self, model_id: str) -> str:
        """Resolve aliases through the route table without exposing mutable routes."""
        with self._lock:
            route = self.routes.get(model_id)
            return route.canonical_model_id if route and route.canonical_model_id else model_id

    def _apply_disabled_providers(
        self, adapters: list[tuple[BaseAdapter, float]]
    ) -> list[tuple[BaseAdapter, float]]:
        """Force weight 0 for adapters whose provider is admin-disabled."""
        resolver = self.disabled_provider_resolver
        if resolver is None:
            return adapters
        is_disabled = resolver.is_disabled
        return [
            (adapter, 0.0 if is_disabled(adapter.config.provider) else weight)
            for adapter, weight in adapters
        ]

    def _get_effective_adapters(
        self, model_id: str, route: RouteConfig
    ) -> list[tuple[BaseAdapter, float]]:
        """Return raw route weights with runtime overrides applied when available."""
        resolver = self.weight_override_resolver
        raw_adapters = route.raw_adapters
        override_model_id = route.canonical_model_id or model_id
        if resolver is None or not raw_adapters:
            return self._apply_disabled_providers(route.adapters)

        get_snapshot = getattr(resolver, "get_snapshot_for_model", None)
        if get_snapshot is not None:
            overrides = get_snapshot(override_model_id)
            return self._apply_disabled_providers(
                [
                    (adapter, float(overrides.get(endpoint_id, raw_weight)))
                    for adapter, raw_weight, endpoint_id in raw_adapters
                ]
            )

        result = resolver.get_for_model(override_model_id)
        if isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                overrides = asyncio.run(result)
            else:
                raise RuntimeError(
                    "FixedRouter cannot await weight overrides during active event-loop selection"
                ) from None
        else:
            overrides = result

        return self._apply_disabled_providers(
            [
                (adapter, float(overrides.get(endpoint_id, raw_weight)))
                for adapter, raw_weight, endpoint_id in raw_adapters
            ]
        )

    def register_route(
        self,
        model_id: str,
        adapters_with_weights: list[tuple[BaseAdapter, float]],
        *,
        aliases: list[str] | None = None,
        admin_only: bool = False,
        required_role: str = "free",
        published: bool = True,
    ) -> None:
        """Register a weighted route for a model.

        Args:
            model_id: Model identifier (canonical).
            adapters_with_weights: List of (adapter, weight) tuples.
                Weights will be normalized to sum to 1.0.
            aliases: Optional alias model IDs that share the same RouteConfig.
                Updates to the canonical route automatically apply to aliases.
            admin_only: If True, only admin users may access this route.
                Deprecated: use required_role="admin" instead.
            required_role: Minimum role required to access this model
                (free/pro/internal/admin).
            published: Whether request selection may use this route. Runtime
                model creation stages an unpublished route until its durable
                strategy transition commits.
        """
        total_weight = sum(weight for _, weight in adapters_with_weights)
        if total_weight <= 0:
            return
        raw_adapters = [
            (adapter, float(weight), endpoint_id_for_adapter(adapter))
            for adapter, weight in adapters_with_weights
        ]
        normalized = [(adapter, weight / total_weight) for adapter, weight in adapters_with_weights]
        # Backward compat: admin_only=True implies required_role="admin"
        effective_role = required_role
        if admin_only and effective_role == "free":
            effective_role = "admin"
        route_cfg = RouteConfig(
            adapters=normalized,
            raw_adapters=raw_adapters,
            canonical_model_id=model_id,
            admin_only=admin_only,
            required_role=effective_role,
            published=published,
        )
        with self._lock:
            self.routes[model_id] = route_cfg
            for alias in aliases or []:
                self.routes[alias] = route_cfg  # shared reference, not a copy

    def eligible_adapters(
        self,
        model_id: str,
        *,
        endpoint_scope: frozenset[str] | None = None,
    ) -> list[tuple[BaseAdapter, float]]:
        """Return the adapters automatic routing may dispatch to, in route order.

        Applies the same two admission rules ``_select_adapter`` uses: an
        adapter whose provider is admin-disabled carries a runtime weight of 0,
        and an adapter whose circuit is open is not admitted. Exposed for
        surfaces that pick an adapter themselves instead of calling into the
        router -- ``/v1/messages`` forwards an Anthropic-native body this router
        has no method for -- so their pick honors the same rules.

        Returns an empty list when the model has no route, the route is
        unpublished, or nothing is admitted; that last case is what
        ``_select_adapter`` reports as ``AllCircuitsOpenError``.
        """
        route = self.routes.get(model_id)
        if not route or not route.published or not route.adapters:
            return []
        effective = self._get_effective_adapters(model_id, route)
        with self._lock:
            snapshot = list(effective)
        return [
            (adapter, weight)
            for adapter, weight in snapshot
            if weight > 0
            and adapter_in_endpoint_scope(adapter, endpoint_scope)
            and self._health_registry.allow_request(endpoint_id_for_adapter(adapter))
        ]

    def select_adapter(
        self,
        model_id: str,
        *,
        required_modalities: frozenset[str] | None = None,
        prefill_tokens: int = 0,
        preferred_endpoint_id: str | None = None,
        endpoint_scope: frozenset[str] | None = None,
    ) -> BaseAdapter | None:
        """Return the adapter automatic routing would choose, without dispatching.

        This is the one selection algorithm in the package: the hybrid layer's
        global policy asks this to learn which endpoint a set of weights picks,
        and the execution path asks it again -- over a range the policy may have
        narrowed -- when it commits. Neither copy exists, and no dispatch claim
        is taken here, so asking is free of side effects.

        Args:
            model_id: Model identifier.
            required_modalities: Non-text input modalities the request needs.
            prefill_tokens: Estimated prompt size, used to steer away from
                saturated endpoints.
            preferred_endpoint_id: A target the caller wants honored if this
                route can serve it. Unlike ``pin_provider`` it does not turn the
                selection into a single-candidate dispatch: the result is still
                an ordinary selection subject to the caller's fallback loop.

        Returns:
            Selected adapter or None if no route configured / no match.
        """
        return self._select_adapter(
            model_id,
            required_modalities=required_modalities,
            prefill_tokens=prefill_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            endpoint_scope=endpoint_scope,
        )

    def preferred_endpoint_for_provider(
        self,
        model_id: str,
        provider: str,
        *,
        required_modalities: frozenset[str] | None = None,
        prefill_tokens: int = 0,
    ) -> str | None:
        """Return one admitted endpoint of ``provider``, or None if it has none.

        A provider label can cover several endpoints, so a policy target naming
        a provider still has to be resolved to one of them before it can be
        preferred. Resolution runs through the ordinary selection rather than
        picking the first route entry, so weights, circuit admission and the
        prefill-aware draw decide among the provider's endpoints exactly as they
        would have without a target.
        """
        route = self.routes.get(model_id)
        if not route or not route.published or not route.adapters:
            return None
        effective = self._get_effective_adapters(model_id, route)
        candidates = [
            endpoint_id_for_adapter(adapter)
            for adapter, _weight in effective
            if adapter.config.provider == provider
        ]
        if not candidates:
            return None
        try:
            for endpoint_id in candidates:
                selected = self._select_adapter(
                    model_id,
                    required_modalities=required_modalities,
                    prefill_tokens=prefill_tokens,
                    preferred_endpoint_id=endpoint_id,
                )
                if selected is None:
                    continue
                # ``_select_adapter`` degrades to an ordinary draw when the
                # preferred endpoint is not admitted, so it can hand back a
                # candidate of some other provider. This method promises one of
                # ``provider``'s own endpoints, so an unhonored preference is
                # reported as "no admitted candidate" rather than as a target the
                # caller never named.
                resolved = endpoint_id_for_adapter(selected)
                if resolved == endpoint_id:
                    return resolved
        except AllCircuitsOpenError:
            return None
        return None

    def _select_adapter(
        self,
        model_id: str,
        *,
        pin_provider: str | None = None,
        required_modalities: frozenset[str] | None = None,
        prefill_tokens: int = 0,
        exclude: set[str] | None = None,
        preferred_endpoint_id: str | None = None,
        endpoint_scope: frozenset[str] | None = None,
        require_target: bool = False,
    ) -> BaseAdapter | None:
        """Select an adapter using weighted random selection with optional affinity.

        Args:
            model_id: Model identifier.
            pin_provider: Optional provider/endpoint_id to pin to. Overrides
                affinity and disables fallback at the call site.
            required_modalities: Non-text input modalities the request needs.
                Routes that do not declare all of them are excluded so media is
                never dispatched to a route that cannot handle it.
            prefill_tokens: Estimated prompt size, used to steer away from
                endpoints already saturated with prefill and to keep concurrent
                mega-prefills spread across replicas. Zero keeps the historical
                pure weighted-random behavior.
            exclude: Endpoint ids this request has already been refused a
                dispatch claim for, so a reselection does not hand back the
                endpoint whose half-open probe another caller is holding.
            preferred_endpoint_id: Target to honor when this route can serve it.
                Preferred over affinity (the caller asked for it now), but unlike
                ``pin_provider`` it stays a normal selection: the caller's
                fallback loop still applies if this attempt fails, and an
                out-of-range target degrades to ordinary selection instead of
                failing the request.
            endpoint_scope: Candidate range for this dispatch. Narrows the route
                before modality filtering and before the health/affinity gates,
                so ``exclude`` and the fallback loop inherit the same range.
            require_target: When True, ``preferred_endpoint_id`` is the only
                endpoint this selection may return. An endpoint that is not
                admissible raises :class:`TargetUnavailableError` instead of
                being replaced by the ordinary draw, so a caller that planned one
                candidate per attempt is told which candidate it lost.

        Returns:
            Selected adapter or None if no route configured / no match.
        """
        route = self.routes.get(model_id)
        if not route or not route.published or not route.adapters:
            return None

        effective = self._get_effective_adapters(model_id, route)
        if endpoint_scope is not None:
            # A backend that owns one execution domain narrows the route here,
            # so its fallback candidates are inside the domain too.
            effective = [
                (adapter, weight)
                for adapter, weight in effective
                if adapter_in_endpoint_scope(adapter, endpoint_scope)
            ]
        if required_modalities:
            effective = [
                (adapter, weight)
                for adapter, weight in effective
                if adapter_supports_modalities(adapter, required_modalities)
            ]
            if not effective:
                raise AllCircuitsOpenError(
                    f"No route for model {model_id} accepts input modalities "
                    f"{sorted(required_modalities)}"
                )
        if endpoint_scope is not None and not effective:
            raise AllCircuitsOpenError(
                f"No route for model {model_id} inside the dispatch scope: {sorted(endpoint_scope)}"
            )

        if pin_provider:
            for adapter, weight in effective:
                if weight <= 0:
                    continue
                eid = endpoint_id_for_adapter(adapter)
                if adapter.config.provider == pin_provider or eid == pin_provider:
                    return adapter
            return None

        with self._lock:
            snapshot = list(effective)

        # ``allow_request`` is asked of every candidate here and answers for at
        # most one dispatch, so it must stay a pure query -- the commit, and the
        # half-open probe claim that goes with it, happen in
        # ``_select_and_claim_adapter`` once this returns.
        allowed: list[tuple[BaseAdapter, float]] = [
            (adapter, weight)
            for adapter, weight in snapshot
            if weight > 0
            and endpoint_id_for_adapter(adapter) not in (exclude or ())
            and self._health_registry.allow_request(endpoint_id_for_adapter(adapter))
        ]

        if not allowed:
            provider_names = [endpoint_id_for_adapter(adapter) for adapter, _weight in snapshot]
            raise AllCircuitsOpenError(
                f"All provider circuits are open for model {model_id}: {provider_names}"
            )

        affinity_key: str | None = None
        if AFFINITY_ENABLED:
            affinity_key = req_ctx.get().get("affinity_key") or None

        # Set when a pin is dropped for backlog, so selection does not simply
        # hand the caller straight back to the endpoint it was moved off.
        avoid_endpoint_id: str | None = None
        # A required target outranks affinity. Affinity remembers what served
        # this conversation last, and honoring it here would hand back an
        # endpoint the caller has explicitly ruled out for this dispatch -- the
        # planned candidate would be silently replaced by the one the plan has
        # already left behind. The entry is left alone rather than dropped: it is
        # still the right answer for the next ordinary selection.
        if affinity_key and not require_target:
            now = time.monotonic()
            with self._lock:
                entry = self._affinity.get((affinity_key, model_id))
                if entry is not None and entry.expires_at > now:
                    for adapter, _w in allowed:
                        if endpoint_id_for_adapter(adapter) == entry.endpoint_id:
                            # A pin is worth honoring for its prefix-cache hit,
                            # but not into a queue: one high-volume key holding
                            # a binding is exactly how a single replica ends up
                            # absorbing every request while its siblings idle.
                            # Falling through re-runs selection and re-pins to
                            # whatever it picks, so the caller rebuilds locality
                            # on an endpoint that can actually serve it.
                            if self._prefill_load.should_keep_affinity(
                                entry.endpoint_id, affinity_key=affinity_key
                            ):
                                entry.expires_at = now + AFFINITY_TTL_SECONDS
                                return adapter
                            avoid_endpoint_id = entry.endpoint_id
                            break
                    else:
                        del self._affinity[(affinity_key, model_id)]
                elif entry is not None:
                    del self._affinity[(affinity_key, model_id)]

        # A caller-supplied target outranks affinity: it was asked for now,
        # while affinity only remembers what served this conversation last. It
        # is still an ordinary selection -- the caller's fallback loop applies
        # if this endpoint fails -- so it differs from ``pin_provider``, which
        # collapses the route to one candidate and disables fallback.
        if preferred_endpoint_id:
            targeted = [
                (adapter, weight)
                for adapter, weight in allowed
                if endpoint_id_for_adapter(adapter) == preferred_endpoint_id
            ]
            if targeted:
                return self._record_affinity_and_return(
                    model_id,
                    self._weighted_draw(
                        model_id,
                        targeted,
                        prefill_tokens=prefill_tokens,
                        affinity_key=affinity_key,
                        avoid_endpoint_id=None,
                    ),
                    affinity_key,
                )
            if require_target:
                # Falling through would hand back a different endpoint: the
                # caller asked for this one, and a substituted candidate can
                # reorder a plan that has already tried the substitute.
                raise TargetUnavailableError(
                    f"endpoint {preferred_endpoint_id!r} is not admissible for model {model_id}"
                )

        chosen = self._weighted_draw(
            model_id,
            allowed,
            prefill_tokens=prefill_tokens,
            affinity_key=affinity_key,
            avoid_endpoint_id=avoid_endpoint_id,
        )
        return self._record_affinity_and_return(model_id, chosen, affinity_key)

    def _record_affinity_and_return(
        self,
        model_id: str,
        chosen: BaseAdapter,
        affinity_key: str | None,
    ) -> BaseAdapter:
        """Remember ``chosen`` for this conversation and return it."""
        if affinity_key:
            now = time.monotonic()
            with self._lock:
                self._affinity[(affinity_key, model_id)] = _Affinity(
                    endpoint_id=endpoint_id_for_adapter(chosen),
                    expires_at=now + AFFINITY_TTL_SECONDS,
                )
                self._maybe_sweep_affinity_locked(now)
        return chosen

    def _weighted_draw(
        self,
        model_id: str,
        allowed: list[tuple[BaseAdapter, float]],
        *,
        prefill_tokens: int,
        affinity_key: str | None,
        avoid_endpoint_id: str | None,
    ) -> BaseAdapter:
        """Draw one adapter from ``allowed`` by weight, prefill-aware.

        Extracted so the preferred-target branch and ordinary selection run the
        *same* draw, including its degrade-to-weighted-draw guard.
        """
        total_allowed = sum(w for _, w in allowed)
        pool = (
            [(a, w / total_allowed) for a, w in allowed]
            if abs(total_allowed - 1.0) > 1e-9
            else allowed
        )

        # Prefill-aware selection is an optimization over a plain weighted draw,
        # so its internal errors must not be fatal. This call sits outside the
        # try/except that owns adapter fallback (see chat_completion and
        # stream_chat_completion), which means an exception raised here is
        # terminal: no fallback adapter is tried, no _on_failure is recorded, and
        # the request is logged under the "router" sentinel with no upstream
        # attribution. An IndexError in select_index reached production exactly
        # that way and failed ~700 requests a day for 18 days. Degrade to the
        # weighted draw the tracker is an improvement on, and log loudly.
        try:
            index = self._prefill_load.select_index(
                [endpoint_id_for_adapter(adapter) for adapter, _w in pool],
                [weight for _a, weight in pool],
                prefill_tokens,
                random.random,
                affinity_key,
                avoid_endpoint_id,
            )
        except Exception:
            logger.error(
                f"Prefill-aware selection failed for model {model_id}; "
                f"falling back to a weighted draw over {len(pool)} route(s)",
                exc_info=True,
            )
            # Still honor the dropped pin: handing the caller straight back to
            # the endpoint it was just moved off is the one outcome selection
            # had already ruled out.
            candidates = [
                i
                for i, (adapter, _w) in enumerate(pool)
                if endpoint_id_for_adapter(adapter) != avoid_endpoint_id
            ] or list(range(len(pool)))
            # allowed[] is filtered on weight > 0, so these are all positive;
            # the guard is here because a fallback path must never itself raise.
            fallback_weights = [max(pool[i][1], 0.0) for i in candidates]
            index = (
                random.choices(candidates, weights=fallback_weights)[0]
                if sum(fallback_weights) > 0
                else random.choice(candidates)
            )
        return pool[index][0]

    def _select_and_claim_adapter(
        self,
        model_id: str,
        *,
        pin_provider: str | None = None,
        required_modalities: frozenset[str] | None = None,
        prefill_tokens: int = 0,
        preferred_endpoint_id: str | None = None,
        endpoint_scope: frozenset[str] | None = None,
        require_target: bool = False,
    ) -> tuple[BaseAdapter | None, DispatchClaim | None]:
        """Select an adapter and claim the dispatch slot for its endpoint.

        ``_select_adapter`` filters on a pure ``allow_request``, which cannot
        tell two concurrent callers apart: on a half-open circuit both would be
        handed the same recovering endpoint, which is the stampede the probe
        exists to prevent. The claim is therefore taken here, once, on the single
        adapter selection actually committed to -- covering every path an adapter
        leaves ``_select_adapter`` by, affinity and a scheduling policy's
        preferred target included.

        A refused claim means another request holds the probe, not that the
        endpoint is out: drop it for *this* selection only and reselect over the
        rest. The loop is bounded by the route size because each pass excludes at
        least one more endpoint id, and an emptied pool raises
        ``AllCircuitsOpenError`` from ``_select_adapter`` exactly as a fully open
        route already does.

        Returns the claim alongside the adapter because the caller owes it back:
        every dispatch path here releases it in a ``finally``, which is the only
        unwind a cancelled coroutine or an abandoned SSE generator still runs.

        An explicit pin bypasses admission entirely, as it always has, so it
        neither consults nor spends a probe -- and holds no claim to release,
        which is what keeps pinned traffic from freeing somebody else's.

        ``require_target`` makes the preferred endpoint the only acceptable
        candidate: when it cannot be admitted, or when its probe is already held,
        this raises :class:`TargetUnavailableError` rather than reselecting over
        the rest -- reselecting is how a planned candidate silently becomes a
        different one.
        """
        if pin_provider:
            return (
                self._select_adapter(
                    model_id,
                    pin_provider=pin_provider,
                    required_modalities=required_modalities,
                    prefill_tokens=prefill_tokens,
                ),
                None,
            )

        route = self.routes.get(model_id)
        attempts = len(route.adapters) if route and route.adapters else 1

        exclude: set[str] = set()
        # A scheduling policy's preferred endpoint is tried first, exactly once,
        # and as an ordinary dispatch: it takes the same half-open probe claim as
        # any other automatic pick. Only ``pin_provider`` -- returned above --
        # overrides admission. Skipping the claim here would let every
        # concurrent request into an endpoint whose circuit is recovering, which
        # is the stampede the probe exists to prevent, and the preference is the
        # normal path rather than an exception: a policy that can name an
        # endpoint names one on every request.
        if preferred_endpoint_id and attempts > 0:
            preferred = self._select_adapter(
                model_id,
                required_modalities=required_modalities,
                prefill_tokens=prefill_tokens,
                preferred_endpoint_id=preferred_endpoint_id,
                endpoint_scope=endpoint_scope,
                require_target=require_target,
            )
            if preferred is not None:
                preferred_endpoint = endpoint_id_for_adapter(preferred)
                claim = self._health_registry.begin_dispatch(preferred_endpoint)
                if claim is not None:
                    return preferred, claim
                if require_target:
                    # Another request holds this endpoint's probe. The caller
                    # named one candidate per attempt, so reselecting here would
                    # dispatch an endpoint its plan has not reached yet.
                    raise TargetUnavailableError(
                        f"endpoint {preferred_endpoint!r} is already probed for model {model_id}"
                    )
                # Otherwise drop the preference for this selection only and let
                # the ordinary loop reselect over the rest, which is what a
                # refused claim does for every other candidate.
                exclude.add(preferred_endpoint)

        for _ in range(attempts):
            adapter = self._select_adapter(
                model_id,
                required_modalities=required_modalities,
                prefill_tokens=prefill_tokens,
                exclude=exclude,
                endpoint_scope=endpoint_scope,
            )
            if adapter is None:
                return None, None
            endpoint_id = endpoint_id_for_adapter(adapter)
            claim = self._health_registry.begin_dispatch(endpoint_id)
            if claim is not None:
                return adapter, claim
            exclude.add(endpoint_id)
        # Every endpoint on the route refused a claim. Same disposition as an
        # all-open route: the caller's request cannot be placed right now.
        raise AllCircuitsOpenError(
            f"All provider circuits are open or probing for model {model_id}: {sorted(exclude)}"
        )

    def bind_execution(
        self,
        adapter: BaseAdapter,
        routing_options: RoutingRequestOptions | None,
        model_id: str,
        claim: DispatchClaim | None,
    ) -> LeafBackend:
        """Return the leaf one attempt executes through, releasing ``claim`` on refusal.

        A binding this router cannot honor is a composition error, so it must not
        reach the failure accounting -- but the selection already took a dispatch
        claim for this endpoint, and a claim that is never handed back keeps a
        recovering endpoint out of rotation until its deadline. Releasing it here
        is the only unwind this path has.
        """
        try:
            return self._leaf_for(
                self.binding_for(execution_adapter(adapter, routing_options), model_id)
            )
        except BaseException:
            self._health_registry.end_dispatch(claim)
            raise

    def binding_for(self, adapter: BaseAdapter, model_id: str) -> EndpointBinding:
        """Return the execution binding for an adapter this router has committed to.

        Taken after selection and admission, so the binding names the endpoint
        that is about to run rather than one that might be chosen. It holds the
        adapter itself: execution then runs exactly that object, and a route edit
        cannot redirect a request that is already in flight.
        """
        return binding_for_adapter(adapter, model_id=model_id)

    @staticmethod
    def _leaf_for(binding: EndpointBinding) -> LeafBackend:
        """Return the leaf that executes ``binding``."""
        return LeafBackend.for_binding(binding)

    def _dispatch_priority(
        self,
        endpoint_id: str,
        prefill_tokens: int,
        affinity_key: str | None,
        fingerprint: str | None = None,
        messages: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """Scheduling priority for one dispatch, ranked on *this* endpoint's work.

        Deliberately the un-cached estimate rather than the prompt size: the
        fleet runs above 90% prefix-cache hit, so ranking on the total would
        stamp a warm 500k-token continuation -- a few thousand delta tokens of
        actual prefill -- as an elephant and have the upstream schedule it last
        and preempt it, which is the opposite of what its cost deserves.

        Priority does NOT agree with routing by construction. Three callers read
        ``uncached_estimate`` at three strictnesses, deliberately: this method
        passes ``fingerprint`` *and* ``messages``, so a fork that shares only the
        opening fails the anchor check; ``acquire`` passes ``fingerprint`` alone,
        enough to keep one caller's unrelated conversations from inheriting each
        other's prefix on the admission gate; ``select_index`` passes neither,
        because a mis-estimated *load* skews one draw and self-corrects. So
        priority and the elephant count can still disagree about a fork or a
        retry, and selection can disagree with both.

        Per endpoint, because the discount is: a prefix resident on the replica
        the caller has been talking to is not resident on a fallback that has
        never seen this conversation, and that fallback really is facing the
        cold prefill.
        """
        return priority_for_prefill(
            self._prefill_load.uncached_estimate(
                endpoint_id,
                prefill_tokens,
                affinity_key,
                fingerprint=fingerprint,
                messages=messages,
            )
        )

    async def chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Execute chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            routing_options: Router-owned controls such as an explicit provider pin
                and required input modalities.
            **params: Additional parameters for the adapter.

        Returns:
            Chat completion response with routing metadata.

        Raises:
            ValueError: If no route configured for model.
        """
        pin_provider = self._resolve_pin_provider(routing_options, params)
        preferred_endpoint_id = (
            routing_options.preferred_endpoint_id if routing_options is not None else None
        )
        endpoint_scope = routing_options.endpoint_scope if routing_options is not None else None
        allow_fallback = routing_options.allow_fallback if routing_options is not None else True
        require_target = routing_options.require_target if routing_options is not None else False
        required_modalities = (
            routing_options.required_modalities if routing_options is not None else frozenset()
        )
        prefill_tokens = estimate_prefill_tokens(
            messages,
            tools=params.get("tools"),
            response_format=params.get("response_format"),
        )
        affinity_key = current_affinity_key()
        # Which conversation this is, so a caller's unrelated prompt cannot
        # inherit another's warm-prefix discount (see _dispatch_priority).
        fingerprint = conversation_fingerprint(
            messages,
            tools=params.get("tools"),
            response_format=params.get("response_format"),
        )
        # Proof, for the next turn, that it really contains this prompt.
        anchor = prompt_anchor(messages)
        primary, primary_claim = self._select_and_claim_adapter(
            model_id,
            pin_provider=pin_provider,
            required_modalities=required_modalities,
            prefill_tokens=prefill_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            endpoint_scope=endpoint_scope,
            require_target=require_target,
        )
        if not primary:
            if pin_provider:
                raise ProviderPinError(
                    f"Pinned provider '{pin_provider}' not found for model {model_id}"
                )
            raise ValueError(f"No route configured for model {model_id}")
        # The adapter this dispatch runs comes from the caller's binding when it
        # committed to one, and the leaf is built here -- before the attempt --
        # so a binding that cannot be honored is refused as a composition error
        # instead of being recorded as a provider failure.
        leaf = self.bind_execution(primary, routing_options, model_id, primary_claim)
        execution = leaf.adapter
        try:
            endpoint_id = endpoint_id_for_adapter(primary)
            with req_ctx.push(
                model=model_id,
                provider=execution.config.provider,
                **{
                    req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                        endpoint_id, prefill_tokens, affinity_key, fingerprint, messages
                    )
                },
            ):
                self._ensure_health(endpoint_id)
                lease = self._prefill_load.acquire(
                    endpoint_id,
                    prefill_tokens,
                    affinity_key=affinity_key,
                    fingerprint=fingerprint,
                    anchor=anchor,
                )
                try:
                    resp = await leaf.chat_completion(messages, **params)
                    # A returned response proves this endpoint finished
                    # prefilling this prompt, which is what makes its prefix
                    # safe to remember.
                    self._prefill_load.release(lease, prefill_confirmed=True)
                finally:
                    self._prefill_load.release(lease)
                self._on_success(endpoint_id)
            # Preserve adapter-set _routing if present;
            # only set default routing if the adapter didn't provide one.
            if "_routing" not in resp:
                resp["_routing"] = {
                    "provider": execution.config.provider,
                    "base_url": execution.config.base_url,
                }
            # Always inject endpoint_id so observation keys match latency profiles.
            resp["_routing"].setdefault("endpoint_id", endpoint_id_for_adapter(primary))
            return resp
        except Exception as primary_error:
            # Record failure for primary endpoint before attempting fallback
            self._on_failure(
                endpoint_id_for_adapter(primary),
                reason="chat_exception",
                detail=operator_safe_error(primary_error),
                exc=primary_error,
            )
            failed_attempts = [failed_attempt(execution, primary_error)]
            # Kept in step with ``failed_attempts`` because that list holds only
            # rendered strings; ``_raise_surfaced_error`` below needs the exception
            # objects to decide which failure the caller is told about.
            attempts = [_RouteAttempt(primary, primary_error)]
            # Attach routing to the surfaced error so the error-log path can
            # attribute the failure to the real upstream instead of the "router"
            # sentinel — mirrors the success-path resp["_routing"] injection.
            # Covers the pin-mode re-raise below; the all-providers-failed exit
            # may surface a fallback's error instead, and ``_raise_surfaced_error``
            # attaches that one's block. ``failed_attempts`` is stored by
            # reference either way, so whichever block is surfaced reflects every
            # fallback attempt appended after this point.
            if not hasattr(primary_error, "_routing"):
                primary_error._routing = {  # type: ignore[attr-defined]
                    "provider": execution.config.provider,
                    "base_url": execution.config.base_url,
                    "endpoint_id": endpoint_id_for_adapter(primary),
                    "failed_attempts": failed_attempts,
                }
            # Pin mode: never fallback — the caller explicitly requested this
            # provider, so a silent switch would produce misleading results.
            if pin_provider:
                raise primary_error
            if not allow_fallback:
                # The caller owns the candidate order and dispatches one attempt
                # per candidate, so walking the rest of the route here would try
                # candidates out of the order it planned -- and would try the
                # same candidate twice, once here and once from the caller.
                raise primary_error
            self._drop_affinity(model_id)
            route = self.routes[model_id]
            for adapter, weight in self._get_effective_adapters(model_id, route):
                if adapter == primary or weight <= 0:
                    continue
                if not adapter_in_endpoint_scope(adapter, endpoint_scope):
                    # The dispatch scope bounds fallback too: a backend that owns
                    # one execution domain must not walk out of it when its
                    # preferred attempt fails. Selection is already narrowed;
                    # this loop reads the raw route, so it needs the same gate.
                    continue
                endpoint_id = endpoint_id_for_adapter(adapter)
                if not adapter_supports_modalities(adapter, required_modalities):
                    continue
                # Resolved before the claim so a binding this router cannot honor
                # costs no admission slot.
                execution = execution_adapter(adapter, routing_options)
                leaf = self._leaf_for(self.binding_for(execution, model_id))
                # Fallback is still automatic routing, so it must honor the
                # same shared circuit eligibility as the initial selection.
                # Explicit pinning returned above and remains the sole circuit
                # override. This is a dispatch point, not an enumeration: the
                # loop calls the adapter in this same iteration, so it claims
                # the half-open probe rather than merely querying admission.
                fallback_claim = self._health_registry.begin_dispatch(endpoint_id)
                if fallback_claim is None:
                    continue
                try:
                    with req_ctx.push(
                        model=model_id,
                        provider=execution.config.provider,
                        **{
                            req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                                endpoint_id, prefill_tokens, affinity_key, fingerprint, messages
                            )
                        },
                    ):
                        self._ensure_health(endpoint_id)
                        lease = self._prefill_load.acquire(
                            endpoint_id,
                            prefill_tokens,
                            affinity_key=affinity_key,
                            fingerprint=fingerprint,
                            anchor=anchor,
                        )
                        try:
                            resp = await leaf.chat_completion(messages, **params)
                            self._prefill_load.release(lease, prefill_confirmed=True)
                        finally:
                            self._prefill_load.release(lease)
                        self._on_success(endpoint_id)
                    if "_routing" not in resp:
                        resp["_routing"] = {
                            "provider": adapter.config.provider,
                            "base_url": adapter.config.base_url,
                            "fallback": True,
                        }
                    resp["_routing"].setdefault("endpoint_id", endpoint_id_for_adapter(adapter))
                    resp["_routing"].setdefault("failed_attempts", failed_attempts)
                    return resp
                except Exception as fallback_error:
                    self._on_failure(
                        endpoint_id,
                        reason="chat_exception",
                        detail=operator_safe_error(fallback_error),
                        exc=fallback_error,
                    )
                    failed_attempts.append(failed_attempt(execution, fallback_error))
                    attempts.append(_RouteAttempt(execution, fallback_error))
                    continue
                finally:
                    self._health_registry.end_dispatch(fallback_claim)
            _raise_surfaced_error(attempts, failed_attempts)
        finally:
            # Every exit, cancellation included: a claim the request keeps is a
            # cooldown the endpoint spends invisible to selection. Released after
            # the fallback loop rather than inside it because the loop never
            # revisits ``primary`` -- holding it that long costs nothing, and an
            # early release would let a second probe start while this one is
            # still on the wire.
            self._health_registry.end_dispatch(primary_claim)

    async def stream_chat_completion(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        routing_options: RoutingRequestOptions | None = None,
        **params: Any,
    ) -> AsyncIterator[Any]:
        """Stream chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            routing_options: Router-owned controls such as an explicit provider pin
                and required input modalities.
            **params: Additional parameters for the adapter.

        Yields:
            SSE chunks from the adapter.

        Raises:
            ValueError: If no route configured for model.
        """
        pin_provider = self._resolve_pin_provider(routing_options, params)
        preferred_endpoint_id = (
            routing_options.preferred_endpoint_id if routing_options is not None else None
        )
        endpoint_scope = routing_options.endpoint_scope if routing_options is not None else None
        allow_fallback = routing_options.allow_fallback if routing_options is not None else True
        require_target = routing_options.require_target if routing_options is not None else False
        required_modalities = (
            routing_options.required_modalities if routing_options is not None else frozenset()
        )
        prefill_tokens = estimate_prefill_tokens(
            messages,
            tools=params.get("tools"),
            response_format=params.get("response_format"),
        )
        affinity_key = current_affinity_key()
        # Which conversation this is, so a caller's unrelated prompt cannot
        # inherit another's warm-prefix discount (see _dispatch_priority).
        fingerprint = conversation_fingerprint(
            messages,
            tools=params.get("tools"),
            response_format=params.get("response_format"),
        )
        # Proof, for the next turn, that it really contains this prompt.
        anchor = prompt_anchor(messages)
        primary, primary_claim = self._select_and_claim_adapter(
            model_id,
            pin_provider=pin_provider,
            required_modalities=required_modalities,
            prefill_tokens=prefill_tokens,
            preferred_endpoint_id=preferred_endpoint_id,
            endpoint_scope=endpoint_scope,
            require_target=require_target,
        )
        if not primary:
            if pin_provider:
                raise ProviderPinError(
                    f"Pinned provider '{pin_provider}' not found for model {model_id}"
                )
            raise ValueError(f"No route configured for model {model_id}")
        leaf = self.bind_execution(primary, routing_options, model_id, primary_claim)
        execution = leaf.adapter
        chunks_yielded = False
        lease: PrefillLease | None = None
        try:
            primary_endpoint_id = endpoint_id_for_adapter(primary)
            with req_ctx.push(
                model=model_id,
                provider=execution.config.provider,
                **{
                    req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                        primary_endpoint_id, prefill_tokens, affinity_key, fingerprint, messages
                    )
                },
            ):
                # Emit synthetic _routing chunk so completions.py can recover
                # the upstream provider/base_url/endpoint_id for DB logging.
                # Without this, req_ctx.push() inside this block is invisible
                # to the parent coroutine when the stream is consumed via an
                # asyncio.create_task reader, and api_logs ends up with
                # provider="router" and cost_usd=NULL.
                first = True
                # Charged before the first yield so the lease brackets the whole
                # upstream interaction: a generator abandoned after the routing
                # chunk still unwinds through this method's finally.
                lease = self._prefill_load.acquire(
                    primary_endpoint_id,
                    prefill_tokens,
                    affinity_key=affinity_key,
                    fingerprint=fingerprint,
                    anchor=anchor,
                )
                yield routing_chunk(execution)
                async for chunk in leaf.stream_chat_completion(messages, **params):
                    if first and has_non_empty_content(chunk):
                        # Providers may emit keep-alives or empty terminal chunks.
                        first = False
                        # Consider first non-empty token as a success signal for availability.
                        self._on_success(primary_endpoint_id)
                        # First token means the prompt is resident and this
                        # endpoint is decoding, not prefilling. Holding the
                        # lease for the whole stream would let a long cheap
                        # decode read as prefill pressure and push traffic away
                        # from an endpoint that is no longer busy prefilling.
                        self._prefill_load.release(lease, prefill_confirmed=True)
                    yield chunk
                    chunks_yielded = True
            return
        except Exception as primary_error:
            # Primary is done either way; release before the fallback attempts
            # so its backlog does not shadow it for the rest of this request.
            self._prefill_load.release(lease)
            self._on_failure(
                endpoint_id_for_adapter(primary),
                reason="stream_exception",
                detail=operator_safe_error(primary_error),
                exc=primary_error,
            )
            failed_attempts = [failed_attempt(execution, primary_error)]
            # Kept in step with ``failed_attempts`` because that list holds only
            # rendered strings; ``_raise_surfaced_error`` below needs the exception
            # objects to decide which failure the caller is told about.
            attempts = [_RouteAttempt(primary, primary_error)]
            # Attach routing to the surfaced error so the error-log path can
            # attribute the failure to the real upstream. Unlike the
            # non-streaming twin below, this generator never gets a chance to
            # set resp["_routing"] on success, so the consumer instead tracks
            # provider via in-band routing_chunk SSE events -- but those are
            # emitted before each fallback attempt even starts (so req_ctx is
            # visible across the asyncio.create_task reader boundary), and
            # the consumer keeps overwriting its provider with the latest one
            # seen. When every attempt fails without yielding content, that
            # leaves the last (lowest-priority) fallback attributed instead
            # of the attempt whose error is what's actually surfaced below.
            # Setting exc._routing here mirrors chat_completion's pattern and
            # takes priority over the consumer's SSE-derived guess. Covers the
            # two re-raise points below that are pinned to primary (pin mode,
            # and a stream already committed to primary); the all-providers-
            # failed exit may surface a fallback's error instead, and
            # ``_raise_surfaced_error`` attaches that one's block.
            # failed_attempts is stored by reference either way, so whichever
            # block is surfaced reflects every fallback attempt appended after
            # this point.
            if not hasattr(primary_error, "_routing"):
                primary_error._routing = {  # type: ignore[attr-defined]
                    "provider": execution.config.provider,
                    "base_url": execution.config.base_url,
                    "endpoint_id": endpoint_id_for_adapter(primary),
                    "failed_attempts": failed_attempts,
                }
            # Pin mode: never fallback — re-raise immediately.
            if pin_provider:
                raise primary_error
            if not allow_fallback:
                # The caller owns the candidate order and dispatches one attempt
                # per candidate, so walking the rest of the route here would try
                # candidates out of the order it planned -- and would try the
                # same candidate twice, once here and once from the caller.
                raise primary_error
            self._drop_affinity(model_id)
            # Once any chunk has been yielded to the client the SSE stream
            # has committed to a single provider. Falling back here would
            # produce a corrupt response: duplicate role/system events from
            # the second provider, mid-message provider switch, and
            # mismatched token-usage totals. Re-raise instead so the caller
            # closes the stream — the partial response is the lesser harm.
            if chunks_yielded:
                raise primary_error
            route = self.routes[model_id]
            for adapter, weight in self._get_effective_adapters(model_id, route):
                if adapter == primary or weight <= 0:
                    continue
                if not adapter_in_endpoint_scope(adapter, endpoint_scope):
                    # Same domain bound as the non-streaming fallback loop: a
                    # scoped dispatch must not leave its domain on failure.
                    continue
                adapter_endpoint_id = endpoint_id_for_adapter(adapter)
                if not adapter_supports_modalities(adapter, required_modalities):
                    continue
                # Resolved before the claim, for the same reason as the
                # non-streaming loop: an unusable binding costs no probe slot.
                execution = execution_adapter(adapter, routing_options)
                leaf = self._leaf_for(self.binding_for(execution, model_id))
                # Synthetic routing chunks are emitted only after circuit
                # admission so an open automatic fallback is never exposed as
                # an attempted upstream. Explicit pinning returned above. The
                # claim is taken here because this loop dispatches in the same
                # iteration, and handed back by this generator's outermost
                # ``finally`` -- the one unwind a client disconnect still runs.
                fallback_claim = self._health_registry.begin_dispatch(adapter_endpoint_id)
                if fallback_claim is None:
                    continue
                try:
                    with req_ctx.push(
                        model=model_id,
                        provider=execution.config.provider,
                        **{
                            req_ctx.UPSTREAM_PRIORITY: self._dispatch_priority(
                                adapter_endpoint_id,
                                prefill_tokens,
                                affinity_key,
                                fingerprint,
                                messages,
                            )
                        },
                    ):
                        yield routing_chunk(
                            execution,
                            fallback=True,
                            failed_attempts=failed_attempts,
                        )
                        first = True
                        lease = self._prefill_load.acquire(
                            adapter_endpoint_id,
                            prefill_tokens,
                            affinity_key=affinity_key,
                            fingerprint=fingerprint,
                            anchor=anchor,
                        )
                        async for chunk in leaf.stream_chat_completion(messages, **params):
                            if first and has_non_empty_content(chunk):
                                first = False
                                self._on_success(adapter_endpoint_id)
                                self._prefill_load.release(lease, prefill_confirmed=True)
                            yield chunk
                            chunks_yielded = True
                    return
                except Exception as fallback_error:
                    self._on_failure(
                        adapter_endpoint_id,
                        reason="stream_exception",
                        detail=operator_safe_error(fallback_error),
                        exc=fallback_error,
                    )
                    failed_attempts.append(failed_attempt(execution, fallback_error))
                    attempts.append(_RouteAttempt(execution, fallback_error))
                    # Once this fallback provider's bytes reached the client the
                    # SSE stream has committed to it (same invariant as the
                    # primary path above). Re-raise instead of splicing yet
                    # another provider into the same response.
                    if chunks_yielded:
                        raise
                    continue
                finally:
                    # Covers the attempt that never reached a first token.
                    self._prefill_load.release(lease)
                    # Per attempt, not once at the end: the loop overwrites
                    # ``fallback_claim`` on every iteration, so a claim left for
                    # the outer unwind would be the last one only, and every
                    # earlier attempt's probe would sit out its whole deadline.
                    self._health_registry.end_dispatch(fallback_claim)
            _raise_surfaced_error(attempts, failed_attempts)
        finally:
            # Backstop for every exit this generator has: an upstream error
            # before the first token, and — the case that actually leaks in
            # production — a client disconnect, which closes the generator
            # mid-stream and would otherwise strand the lease forever, making
            # the endpoint look permanently busy to every later request.
            self._prefill_load.release(lease)
            # Same unwind, same reason, for the probe: GeneratorExit and
            # CancelledError miss the ``except Exception`` above, and a stream
            # that yields nothing but keep-alives never records a success, so
            # this is the only release an abandoned probe gets. Without it one
            # hung-up client costs a *healthy* single-route model a full cooldown
            # of 503s.
            self._health_registry.end_dispatch(primary_claim)
