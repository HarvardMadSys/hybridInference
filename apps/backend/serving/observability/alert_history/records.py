"""One history fact, its stable identity and its canonical digest.

A :class:`HistoryRecord` is built once, at the point the fact happens, with its
``origin_record_id`` already fixed. Retries resend the same object, so the
``payload_digest`` computed here is what tells a duplicate (a lost commit
response) from a conflict (two different facts claiming one identity).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from typing import Any

SCHEMA_VERSION = 1

#: Initial bounds from the design. One record, serialized, may not exceed this.
MAX_RECORD_BYTES = 64 * 1024
#: Longest single fact value kept; longer values are cut and flagged.
MAX_FACT_STRING = 500
MAX_FACTS = 32
MAX_TITLE = 300
MAX_BODY = 8000
MAX_REQUEST_REFS = 5

#: Every kind a record may have. ``history_conflict`` is written only by the
#: store, for the losing side of an identity collision.
KINDS = frozenset(
    {
        "condition_triggered",
        "condition_ended",
        "notification_decision",
        "suppression_summary",
        "delivery_attempt",
        "delivery_result",
        "resolution_retry_terminated",
        "history_gap",
    }
)
STORE_KINDS = KINDS | {"history_conflict"}

END_BASES = frozenset({"observed_healthy", "no_samples", "source_closed", "superseded", "unknown"})
DECISION_REASONS = frozenset(
    {
        "cooldown",
        "type_muted",
        "snoozed",
        "in_flight",
        "no_channel",
        "channel_config_invalid",
        "policy_disabled",
        "rate_limited",
        "resolution_silenced",
        "source_suppressed",
        "superseded_by_new_episode",
        "max_attempts",
        "max_age",
        "other",
    }
)
OUTCOMES = frozenset({"accepted", "rejected", "not_sent", "unknown"})
CHANNELS = frozenset({"slack", "wecom", "email", "webhook", "other"})
ORIGINS = frozenset({"live", "test", "channel_import"})
SEVERITIES = frozenset({"critical", "error", "warn", "info"})


def utc_iso(value: dt.datetime) -> str:
    """Render ``value`` as UTC with microseconds: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def clean_text(value: str) -> str:
    """Drop the one character PostgreSQL text and JSONB cannot hold."""
    return value.replace("\x00", "�")


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One history fact as it is written to ``alert_history_events``."""

    source: str
    source_namespace: str
    origin_record_id: str
    kind: str
    occurred_at: dt.datetime
    deployment_id: str
    target_environment: str
    origin: str = "live"
    schema_version: int = SCHEMA_VERSION
    source_instance_id: str | None = None
    observation_scope: str | None = None
    process_id: str | None = None
    producer_seq: int | None = None
    episode_id: str | None = None
    source_incident_id: str | None = None
    generation: int | None = None
    source_event_id: str | None = None
    action_id: str | None = None
    attempt_id: str | None = None
    alert_type: str | None = None
    alert_key: str | None = None
    severity: str | None = None
    rule_id: str | None = None
    rule_version: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    model_id: str | None = None
    endpoint_id: str | None = None
    #: ``(request_id, owner_digest)`` pairs. Only the backend writes these: it
    #: alone knows who owns the request, and the owner's digest is also in
    #: ``subject_digests`` so an erasure removes the reference with the row.
    request_refs: tuple[tuple[str, str], ...] = ()
    end_basis: str | None = None
    decision_reason: str | None = None
    count: int | None = None
    bucket_start: dt.datetime | None = None
    bucket_end: dt.datetime | None = None
    channel: str | None = None
    outcome: str | None = None
    error_code: str | None = None
    duration_ms: int | None = None
    message: dict[str, Any] | None = None
    #: Erasure-fence digests of every account the record is about.
    subject_digests: tuple[str, ...] = ()
    #: What capture could not keep, e.g. ``omitted:affected_callers``.
    limitations: tuple[str, ...] = ()
    external_ref: dict[str, Any] | None = None
    audit_ref: str | None = None
    association_incomplete: bool = False
    #: Which writer credential submitted it; not part of the fact.
    credential_id: str | None = None
    payload_digest: str = ""
    size: int = 0

    def content(self) -> dict[str, Any]:
        """The fact in canonical form: absent/empty fields dropped, UTC timestamps."""
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in _NOT_CONTENT:
                continue
            value = getattr(self, f.name)
            if value is None or value == () or value == {}:
                continue
            if f.name == "association_incomplete" and not value:
                continue
            out[f.name] = _canonical(value)
        return out


_NOT_CONTENT = frozenset({"credential_id", "payload_digest", "size"})


def _canonical(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return utc_iso(value)
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    return value


def canonical_json(content: dict[str, Any]) -> str:
    """Serialize ``content`` the one way the digest and the size are taken from."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def finalize(record: HistoryRecord) -> HistoryRecord:
    """Return ``record`` with its digest and serialized size filled in."""
    text = canonical_json(record.content())
    encoded = text.encode("utf-8")
    return replace(
        record,
        payload_digest=hashlib.sha256(encoded).hexdigest(),
        size=len(encoded),
    )
