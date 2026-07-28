"""Build canonical control-plane events from the gateway's alert inputs.

This is the producer half of the backend migration. Today every ``alert_slack``
call site hands a ``dict[str, Any]`` whose values are interpolated verbatim into
a Slack message, which is how source addresses, API key prefixes, and
pre-formatted prose reach the channel. The canonical contract rejects free text
for exactly that reason, so the migration restructures what the alerts carry
rather than loosening the contract:

* values on-call acts on survive as *typed* fields — ``source_addresses`` must
  parse as IP addresses, ``subject`` is a bounded identifier — which is a
  stricter posture than the free text it replaces, not a weaker one
* incidental values become numbers (``observed``/``threshold``/``window_sec``)
  that the renderer formats with units, so the card reads at least as well as
  the ``"12.3% (45 of 366 requests, last 300s)"`` line it replaces
* credential material (key prefixes) and unbounded prose (top paths, status
  code lists) are dropped deliberately; both remain in the gateway's own logs
  and dashboard

Builders here mirror the TypeScript validator's rules so a malformed event
fails in-process, with a stack trace pointing at the call site, instead of
being rejected at ingress where the producer only learns a status code.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import uuid
from typing import Any, Literal

BreachedMetric = Literal[
    "auth_failure_count",
    "failed_request_rate",
    "http_5xx_rate",
    "latency_p95_ms",
    "prefix_cache_pending_evictions",
    "provider_hourly_spend",
    "tracked_task_failure_rate",
    "user_daily_cost",
]
BreachScope = Literal["gateway", "provider", "task", "user"]
UnavailableDependency = Literal["log_store", "operational_store"]
DependencyFailureReason = Literal[
    "authentication",
    "connection_refused",
    "health_check_failed",
    "timeout",
    "unknown",
]
AlertStatus = Literal["firing", "resolved"]

#: Listing addresses only makes sense where blocking them is the response. The
#: typed-IP field is an exception to the contract's no-network-identifier rule,
#: so it stays scoped rather than available to every metric.
ADDRESS_BEARING_METRICS: frozenset[str] = frozenset({"auth_failure_count"})

MAX_SOURCE_ADDRESSES = 5
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SUBJECT_MAX_LENGTH = 128
#: A bare implementation label. General text checks are not enough: a
#: credential-free DSN like ``postgres://localhost/app`` passes all of them,
#: which is exactly the connection-string leak this field exists to prevent.
_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
#: The contract's numeric bound. Matches ``boundedNumber`` in the validator.
_MAX_METRIC_VALUE = 1e12

#: Severity per metric, preserving what each backend rule sends today so the
#: migration does not silently downgrade or inflate an alert's urgency.
_METRIC_SEVERITY: dict[str, str] = {
    "auth_failure_count": "warn",
    "failed_request_rate": "error",
    "http_5xx_rate": "error",
    "latency_p95_ms": "warn",
    "prefix_cache_pending_evictions": "warn",
    "provider_hourly_spend": "warn",
    "tracked_task_failure_rate": "error",
    "user_daily_cost": "warn",
}

_METRIC_TITLE: dict[str, str] = {
    "auth_failure_count": "Auth failure spike",
    "failed_request_rate": "Failed-request rate exceeded",
    "http_5xx_rate": "5xx rate exceeded",
    "latency_p95_ms": "p95 latency exceeded",
    "prefix_cache_pending_evictions": "RouteWise pending prefix-cache entries leaking",
    "provider_hourly_spend": "Provider hourly spend exceeded budget",
    "tracked_task_failure_rate": "Tracked-task failure rate exceeded",
    "user_daily_cost": "User cost overrun",
}


class ControlPlaneEventError(ValueError):
    """Raised when an alert cannot be expressed in the canonical contract."""


def _occurred_at(moment: dt.datetime | None) -> str:
    value = moment or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise ControlPlaneEventError("occurred_at must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _subject(value: str | None) -> str | None:
    """Bound the scoped identifier; free text belongs nowhere in this contract."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > _SUBJECT_MAX_LENGTH or not _IDENTIFIER_RE.match(text):
        raise ControlPlaneEventError(f"subject is not a bounded identifier: {value!r}")
    return text


def _source_addresses(values: list[str] | None, metric: str) -> list[str] | None:
    """Validate each entry as a real address, so nothing else can ride along."""
    if values is None:
        return None
    if metric not in ADDRESS_BEARING_METRICS:
        raise ControlPlaneEventError(f"source_addresses is not allowed for metric {metric}")
    if not values or len(values) > MAX_SOURCE_ADDRESSES:
        raise ControlPlaneEventError(
            f"source_addresses must contain 1 to {MAX_SOURCE_ADDRESSES} addresses"
        )
    normalized: list[str] = []
    for value in values:
        try:
            normalized.append(str(ipaddress.ip_address(str(value).strip())))
        except ValueError as error:
            raise ControlPlaneEventError(f"not an IP address: {value!r}") from error
    if len(set(normalized)) != len(normalized):
        raise ControlPlaneEventError("source_addresses must not contain duplicates")
    return normalized


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def metric_threshold_fingerprint(metric: str, subject: str | None = None) -> str:
    """Incident key for a threshold breach, scoped per subject so each resolves alone."""
    return f"gateway:{metric}:{subject}" if subject else f"gateway:{metric}"


def dependency_fingerprint(dependency: str) -> str:
    """Incident key for a dependency outage."""
    return f"gateway:dependency:{dependency}"


def _metric_value(value: float, field: str) -> float:
    """Coerce a metric number, refusing what the contract will not accept.

    ``float("nan")`` survives every comparison — including the firing coherence
    check, whose ``<`` is false for NaN — so without this an event this module
    claims to have validated is dropped at ingress instead of at the call site.
    """
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ControlPlaneEventError(f"{field} must be a finite number")
    if not 0 <= number <= _MAX_METRIC_VALUE:
        raise ControlPlaneEventError(f"{field} is outside the contract's range")
    return number


def _bounded_int(value: int | None, field: str, low: int, high: int) -> int | None:
    """Check an optional integer against the contract's own bounds.

    The validator rejects these, so letting them through would move the failure
    to ingress — where the producer only learns a status code — and defeat the
    reason this module restates the rules.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlPlaneEventError(f"{field} must be an integer")
    if not low <= value <= high:
        raise ControlPlaneEventError(f"{field} must be between {low} and {high}")
    return value


def _bounded_ratio(value: float | None, field: str) -> float | None:
    """Check an optional 0..1 ratio, refusing the non-finite values too."""
    if value is None:
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ControlPlaneEventError(f"{field} must be a finite number")
    if not 0.0 <= number <= 1.0:
        raise ControlPlaneEventError(f"{field} must be between 0 and 1")
    return number


def _backend(value: str | None) -> str | None:
    """Validate the store's implementation label, or reject it here."""
    if value is None:
        return None
    if not _BACKEND_RE.fullmatch(value):
        raise ControlPlaneEventError(
            "backend must be a bare lowercase label, not a connection string"
        )
    return value


def build_metric_threshold_event(
    *,
    metric: BreachedMetric,
    status: AlertStatus,
    observed: float,
    threshold: float,
    window_sec: int | None = None,
    scope: BreachScope | None = None,
    subject: str | None = None,
    source_addresses: list[str] | None = None,
    distinct_sources: int | None = None,
    top_source_share: float | None = None,
    sample_count: int | None = None,
    occurred_at: dt.datetime | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one ``metric_threshold_breach`` event.

    ``status`` comes from the caller's transition tracker, not from the metric:
    a resolved event legitimately reports an observed value below its threshold,
    while a firing one may not — the control plane rejects that as incoherent,
    so it is checked here where the stack trace is useful.
    """
    if metric not in _METRIC_SEVERITY:
        raise ControlPlaneEventError(f"unsupported metric: {metric!r}")
    if status == "firing" and observed < threshold:
        raise ControlPlaneEventError("a firing breach must report observed at or above threshold")

    normalized_subject = _subject(subject)
    context = _drop_none(
        {
            "metric": metric,
            "observed": _metric_value(observed, "observed"),
            "threshold": _metric_value(threshold, "threshold"),
            "window_sec": _bounded_int(window_sec, "window_sec", 1, 31 * 24 * 60 * 60),
            "scope": scope,
            "subject": normalized_subject,
            "source_addresses": _source_addresses(source_addresses, metric),
            "distinct_sources": _bounded_int(
                distinct_sources, "distinct_sources", 0, 1_000_000_000
            ),
            "top_source_share": _bounded_ratio(top_source_share, "top_source_share"),
            "sample_count": _bounded_int(sample_count, "sample_count", 0, 1_000_000_000),
        }
    )
    title = _METRIC_TITLE[metric]
    if normalized_subject:
        title = f"{title} ({normalized_subject})"
    return {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "alert_type": "metric_threshold_breach",
        "fingerprint": metric_threshold_fingerprint(metric, normalized_subject),
        "status": status,
        "severity": _METRIC_SEVERITY[metric] if status == "firing" else "info",
        "title": title,
        "occurred_at": _occurred_at(occurred_at),
        "summary": (
            f"{title} crossed its configured threshold."
            if status == "firing"
            else f"{title} returned below its configured threshold."
        ),
        "context": context,
        "evidence_refs": [],
    }


def build_dependency_unavailable_event(
    *,
    dependency: UnavailableDependency,
    status: AlertStatus,
    backend: str | None = None,
    reason: DependencyFailureReason | None = None,
    occurred_at: dt.datetime | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one ``dependency_unavailable`` event.

    ``reason`` replaces the raw health-check error the backend sends today,
    which is where a DSN or host would otherwise reach Slack.
    """
    if dependency not in ("log_store", "operational_store"):
        raise ControlPlaneEventError(f"unsupported dependency: {dependency!r}")
    backend = _backend(backend)
    label = dependency.replace("_", " ")
    return {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "alert_type": "dependency_unavailable",
        "fingerprint": dependency_fingerprint(dependency),
        "status": status,
        "severity": "critical" if status == "firing" else "info",
        "title": f"Database disconnected: {label}",
        "occurred_at": _occurred_at(occurred_at),
        "summary": (
            f"The {label} failed its health check."
            if status == "firing"
            else f"The {label} passed its health check again."
        ),
        "context": _drop_none(
            {
                "dependency": dependency,
                "backend": backend,
                # A resolved event carries no failure cause, mirroring the
                # firing-only fields the contract rejects on the other types.
                "reason": reason if status == "firing" else None,
            }
        ),
        "evidence_refs": [],
    }
