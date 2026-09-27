"""Catalog of the gateway's Slack alert types, for muting one type at a time.

An alert's *type* is the family its dedupe key belongs to: the part of the key
before the first ``:``. ``circuit_open:zhipu`` and ``circuit_open:kimi`` are one
type, ``circuit_open``; ``auth_ip_blocked`` has no suffix and is its own type.
Every producer already builds its key that way, so the type is derived from the
key rather than threaded through ``alert_on_transition`` as another argument.

This is not the rule names in ``alerts.yaml``. Those name a rule's *config*,
and several differ from the key the rule sends under —
``p95_latency_per_provider`` pages as ``p95_latency:<provider>``,
``user_cost_overrun`` as ``cost_overrun:<user>:<day>`` — and the state alerts
and the DB-query detector have no rule at all. The key is the one identity every
path shares, including the stale sweep, which is handed nothing else.

The catalog is what the admin dashboard lists and what the mute endpoints accept.
A key whose type is not listed here is never muted, only ever sent: a new alert
must be registered before it can be silenced, and
``tests/unit/observability/test_alert_types.py`` fails until it is.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AlertType:
    """One family of Slack alerts, as the dashboard presents it."""

    #: The dedupe-key prefix every alert of this type is sent under.
    id: str
    #: Short name, matching the title the Slack card carries.
    label: str
    #: What an alert of this type means, in one sentence.
    description: str
    #: Section the dashboard lists it under.
    group: str
    #: The shape of its dedupe keys, e.g. ``circuit_open:<provider>``.
    key_pattern: str


ALERT_TYPES: tuple[AlertType, ...] = (
    AlertType(
        id="failed_request_rate",
        label="Failed-request rate exceeded",
        description=(
            "The share of service-side failed requests over the rolling window crossed its "
            "threshold."
        ),
        group="Request errors",
        key_pattern="failed_request_rate",
    ),
    AlertType(
        id="failed_request_rate_db",
        label="Failed-request rate exceeded (DB-query detector)",
        description=(
            "The periodic api_logs query counted more failed requests in its window than "
            "its threshold allows."
        ),
        group="Request errors",
        key_pattern="failed_request_rate_db",
    ),
    AlertType(
        id="fivexx_rate",
        label="5xx rate exceeded",
        description=(
            "The share of requests answered with a 5xx over the rolling window crossed its "
            "threshold."
        ),
        group="Request errors",
        key_pattern="fivexx_rate",
    ),
    AlertType(
        id="client_error_burst",
        label="Client-error burst relayed from upstream",
        description=(
            "Client errors relayed from upstreams, which bypass the circuit breaker, piled "
            "up within the window."
        ),
        group="Request errors",
        key_pattern="client_error_burst",
    ),
    AlertType(
        id="stream_failure_rate",
        label="Streaming failures for a model",
        description="One model's streams keep dying mid-flight.",
        group="Request errors",
        key_pattern="stream_failure_rate:<model>",
    ),
    AlertType(
        id="p95_latency",
        label="p95 latency exceeded for a provider",
        description="One provider's p95 latency over the rolling window exceeded its limit.",
        group="Providers",
        key_pattern="p95_latency:<provider>",
    ),
    AlertType(
        id="circuit_open",
        label="Provider circuit opened",
        description="A provider's circuit breaker tripped on its failure streak or availability.",
        group="Providers",
        key_pattern="circuit_open:<provider>",
    ),
    AlertType(
        id="upstream_auth",
        label="Upstream rejected gateway credential",
        description="An upstream endpoint refused the credential the gateway presents to it.",
        group="Providers",
        key_pattern="upstream_auth:<endpoint>",
    ),
    AlertType(
        id="auth_failure_spike",
        label="Auth failure spike",
        description=(
            "Failed API-key authentications across the gateway exceeded the window "
            "threshold. Off unless enabled in alerts.yaml."
        ),
        group="Auth",
        key_pattern="auth_failure_spike",
    ),
    AlertType(
        id="auth_ip_blocked",
        label="Auth-failure blocklist refusing a source",
        description=(
            "A source crossed the auth-failure threshold and is now refused for the block duration."
        ),
        group="Auth",
        key_pattern="auth_ip_blocked",
    ),
    AlertType(
        id="db_disconnect",
        label="Database disconnected",
        description="The health check lost the operational store or the log store.",
        group="Platform",
        key_pattern="db_disconnect:<store>",
    ),
    AlertType(
        id="tracked_task_failure",
        label="Tracked-task failure rate exceeded",
        description=(
            "A background task type, such as request logging or cost accounting, is failing "
            "at a sustained rate."
        ),
        group="Platform",
        key_pattern="tracked_task_failure:<task>",
    ),
    AlertType(
        id="prefix_cache_pending_leak",
        label="RouteWise pending prefix-cache entries leaking",
        description=(
            "Pending RouteWise prefix-cache entries are being evicted before they are consumed."
        ),
        group="Platform",
        key_pattern="prefix_cache_pending_leak",
    ),
    AlertType(
        id="cost_overrun",
        label="Daily cost quota consumed",
        description="A user used up their daily cost quota.",
        group="Cost",
        key_pattern="cost_overrun:<user>:<day>",
    ),
    AlertType(
        id="provider_spend",
        label="Provider hourly spend exceeded budget",
        description="A provider's spend this hour exceeded the budget configured for it.",
        group="Cost",
        key_pattern="provider_spend:<provider>:<hour>",
    ),
)

_BY_ID: dict[str, AlertType] = {alert_type.id: alert_type for alert_type in ALERT_TYPES}


def alert_type_of(key: str) -> str:
    """Return the type a dedupe key belongs to: its text before the first ``:``."""
    return key.split(":", 1)[0]


def get_alert_type(type_id: str) -> AlertType | None:
    """Return the catalog entry for ``type_id``, or None for an unknown type."""
    return _BY_ID.get(type_id)
