"""The ``POST /internal/alert-history/events`` body, validated strictly.

Every model forbids unknown fields, which is also how a body that tries to
name its own ``source``, ``source_namespace`` or ``deployment_id`` is refused:
those come from the writer credential. Limits are the contract's
(``docs/developer/alert-history.md``, "Ingestion API").
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from serving.observability.alert_history.records import (
    MAX_BODY,
    MAX_FACT_STRING,
    MAX_FACTS,
    MAX_TITLE,
    SCHEMA_VERSION,
    HistoryRecord,
    canonical_json,
    finalize,
)
from serving.observability.alert_history.redaction import (
    credential_shaped_identifier,
    redact_text,
    sanitize_facts,
)

MAX_EVENTS = 50
#: ``source_status.rules_snapshot`` bounds, the same on both sides of the wire.
MAX_RULES_KEYS = 64
MAX_RULES_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024
#: How far from the server clock ``occurred_at`` may be.
MAX_PAST = dt.timedelta(days=400)
MAX_FUTURE = dt.timedelta(minutes=5)

_ID = r"^[A-Za-z0-9._:@/-]{1,200}$"
_LABEL = r"^[a-z0-9_.-]{1,64}$"
_CODE = r"^[a-z0-9_.:-]{1,100}$"


def _rfc3339_text(value: Any) -> Any:
    # Pydantic would also read a number as epoch seconds; the contract is text.
    if not isinstance(value, str):
        raise ValueError("timestamps are RFC 3339 strings")
    return value


Rfc3339 = Annotated[dt.datetime, BeforeValidator(_rfc3339_text)]


def _safe_identifier(value: str) -> str:
    if credential_shaped_identifier(value):
        raise ValueError("identifiers must not carry credentials or addresses")
    return value


IdStr = Annotated[str, Field(pattern=_ID), AfterValidator(_safe_identifier)]
LabelStr = Annotated[str, Field(pattern=_LABEL)]
CodeStr = Annotated[str, Field(pattern=_CODE)]
FactKey = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
FactValue = Annotated[str, Field(max_length=MAX_FACT_STRING)] | bool | int | float | None


def _printable(value: str, limit: int) -> str:
    if len(value) > limit:
        raise ValueError(f"at most {limit} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("control characters are not allowed")
    return value


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL is not allowed")
    return value


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("timestamps need an explicit offset")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def _check_facts(facts: dict[str, Any] | None, *, limit: int = MAX_FACTS) -> dict[str, Any] | None:
    if facts is None:
        return None
    if len(facts) > limit:
        raise ValueError(f"at most {limit} facts")
    for value in facts.values():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("numbers must be finite")
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 2**53:
            raise ValueError("integers must fit in 53 bits")
        if isinstance(value, str):
            _no_nul(value)
    return facts


class MessageSnapshot(_Strict):
    """A redacted copy of the notification text."""

    title: str = Field(max_length=MAX_TITLE)
    body: str = Field(max_length=MAX_BODY)
    format: Literal["slack_mrkdwn", "wecom_markdown", "plain"]
    format_version: str = Field(pattern=r"^[a-z0-9_.-]{1,40}$")
    redacted: bool
    truncated: bool
    omitted_fields: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=32
    )

    @field_validator("title", "body")
    @classmethod
    def _text(cls, value: str) -> str:
        return _no_nul(value)


class ExternalRef(_Strict):
    """A reference into another system, such as a channel message id."""

    system: str = Field(pattern=r"^[a-z0-9_.-]{1,40}$")
    ref: IdStr


class EventIn(_Strict):
    """One history fact from an external source."""

    origin_record_id: IdStr
    kind: Literal[
        "condition_triggered",
        "condition_ended",
        "notification_decision",
        "suppression_summary",
        "delivery_attempt",
        "delivery_result",
        "resolution_retry_terminated",
        "history_gap",
    ]
    occurred_at: Rfc3339
    target_environment: LabelStr
    origin: Literal["live", "test"] | None = None
    source_instance_id: IdStr | None = None
    severity: Literal["critical", "error", "warn", "info"] | None = None
    alert_type: CodeStr | None = None
    rule_id: IdStr | None = Field(default=None, max_length=100)
    rule_version: IdStr | None = Field(default=None, max_length=100)
    source_event_id: IdStr | None = None
    source_incident_id: IdStr | None = None
    generation: int | None = Field(default=None, ge=0, le=2**53)
    action_id: IdStr | None = None
    attempt_id: IdStr | None = None
    observation_scope: LabelStr | None = None
    model_id: str | None = None
    endpoint_id: str | None = None
    end_basis: (
        Literal["observed_healthy", "no_samples", "source_closed", "superseded", "unknown"] | None
    ) = None
    decision_reason: (
        Literal[
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
            "other",
        ]
        | None
    ) = None
    count: int | None = Field(default=None, ge=1, le=10**9)
    bucket_start: Rfc3339 | None = None
    bucket_end: Rfc3339 | None = None
    channel: Literal["slack", "wecom", "email", "webhook", "other"] | None = None
    outcome: Literal["accepted", "rejected", "not_sent", "unknown"] | None = None
    error_code: CodeStr | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    facts: dict[FactKey, FactValue] | None = None
    message: MessageSnapshot | None = None
    external_ref: ExternalRef | None = None

    @field_validator("model_id", "endpoint_id")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return None if value is None else _printable(value, 200)

    @field_validator("occurred_at", "bucket_start", "bucket_end")
    @classmethod
    def _tz(cls, value: dt.datetime | None) -> dt.datetime | None:
        return _aware(value)

    @field_validator("facts")
    @classmethod
    def _facts(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_facts(value)

    @model_validator(mode="after")
    def _kind_fields(self) -> EventIn:
        if self.end_basis is not None and self.kind != "condition_ended":
            raise ValueError("end_basis is only for condition_ended")
        if self.outcome is not None and self.kind != "delivery_result":
            raise ValueError("outcome is only for delivery_result")
        if self.kind == "delivery_result" and self.outcome is None:
            raise ValueError("delivery_result needs an outcome")
        if self.decision_reason is not None and self.kind not in (
            "notification_decision",
            "suppression_summary",
            "resolution_retry_terminated",
        ):
            raise ValueError("decision_reason is only for decisions")
        if (self.bucket_start is None) != (self.bucket_end is None):
            raise ValueError("bucket_start and bucket_end go together")
        if self.bucket_start is not None:
            if self.kind != "suppression_summary":
                raise ValueError("buckets are only for suppression_summary")
            if self.bucket_end is not None and self.bucket_start > self.bucket_end:
                raise ValueError("bucket_start must not be after bucket_end")
        if self.kind == "suppression_summary" and (
            self.count is None or self.decision_reason is None
        ):
            raise ValueError("suppression_summary needs count and decision_reason")
        return self


class SourceStatusIn(_Strict):
    """A source's heartbeat and buffer snapshot."""

    source_instance_id: IdStr
    target_environment: LabelStr
    observed_at: Rfc3339
    release_id: IdStr | None = Field(default=None, max_length=64)
    started_at: Rfc3339 | None = None
    sync_watermark: Rfc3339 | None = None
    pending_count: int | None = Field(default=None, ge=0, le=10**9)
    pending_reported_at: Rfc3339 | None = None
    dropped_count: int | None = Field(default=None, ge=0, le=10**9)
    last_error_code: CodeStr | None = None
    rules_snapshot_hash: str | None = Field(default=None, pattern=r"^[A-Za-z0-9:_.-]{1,100}$")
    rules_snapshot: dict[FactKey, FactValue] | None = None
    #: Final-snapshot fields of a short-lived producer (one invocation). All
    #: three or none, and only with ``pending_count == 0``: the invocation says
    #: it finished, with nothing left queued, knowing it lost ``lost_at_stop``.
    stopped_at: Rfc3339 | None = None
    stop_reason: Literal["invocation_complete"] | None = None
    lost_at_stop: int | None = Field(default=None, ge=0, le=10**9)

    @field_validator(
        "observed_at", "started_at", "sync_watermark", "pending_reported_at", "stopped_at"
    )
    @classmethod
    def _tz(cls, value: dt.datetime | None) -> dt.datetime | None:
        return _aware(value)

    @field_validator("rules_snapshot")
    @classmethod
    def _snapshot(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        value = _check_facts(value, limit=MAX_RULES_KEYS)
        if value is not None and len(canonical_json(value).encode("utf-8")) > MAX_RULES_BYTES:
            raise ValueError(f"rules_snapshot is limited to {MAX_RULES_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _final_snapshot(self) -> SourceStatusIn:
        final = (self.stopped_at, self.stop_reason, self.lost_at_stop)
        if any(v is not None for v in final):
            if any(v is None for v in final):
                raise ValueError("stopped_at, stop_reason and lost_at_stop go together")
            if self.pending_count != 0:
                # A clean completion with records still queued (or an unknown
                # backlog) contradicts itself; report a gap instead.
                raise ValueError("a final snapshot needs pending_count 0")
            if self.started_at is not None and self.stopped_at < self.started_at:
                raise ValueError("stopped_at is before started_at")
        return self


class IngestRequest(_Strict):
    """The whole request body."""

    schema_version: Literal[1]
    events: list[EventIn] = Field(default_factory=list, max_length=MAX_EVENTS)
    source_status: SourceStatusIn | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> IngestRequest:
        if not self.events and self.source_status is None:
            raise ValueError("send at least one event or a source_status")
        return self


def to_record(
    event: EventIn,
    *,
    source: str,
    source_namespace: str,
    deployment_id: str,
    credential_id: str,
) -> HistoryRecord:
    """The stored form of one validated event; the server binds the identity.

    Free text is redacted here whatever the sender says about it: secret-named
    facts are dropped, credential-shaped values in facts, labels and the
    message are replaced, and each change is named in ``limitations``. The
    result is deterministic, so a resend still matches its first digest.
    """
    facts, limitations = sanitize_facts(dict(event.facts or {}))
    if event.source_instance_id is None:
        limitations.append("no_source_instance")
    labels = {}
    for name in ("model_id", "endpoint_id"):
        value = getattr(event, name)
        if value is not None:
            value, changed = redact_text(value)
            if changed:
                limitations.append(f"redacted:{name}")
        labels[name] = value
    message = None
    if event.message is not None:
        message = event.message.model_dump()
        changed_any = False
        for name in ("title", "body"):
            message[name], changed = redact_text(message[name])
            changed_any = changed_any or changed
        omitted = []
        for name in message.get("omitted_fields") or []:
            cleaned, changed = redact_text(name)
            changed_any = changed_any or changed
            omitted.append(cleaned)
        message["omitted_fields"] = omitted
        if changed_any:
            limitations.append("redacted:message")
    return finalize(
        HistoryRecord(
            source=source,
            source_namespace=source_namespace,
            origin_record_id=event.origin_record_id,
            kind=event.kind,
            occurred_at=event.occurred_at.astimezone(dt.UTC),
            deployment_id=deployment_id,
            target_environment=event.target_environment,
            origin=event.origin or "live",
            schema_version=SCHEMA_VERSION,
            source_instance_id=event.source_instance_id,
            observation_scope=event.observation_scope,
            source_incident_id=event.source_incident_id,
            generation=event.generation,
            source_event_id=event.source_event_id,
            action_id=event.action_id,
            attempt_id=event.attempt_id,
            alert_type=event.alert_type,
            severity=event.severity,
            rule_id=event.rule_id,
            rule_version=event.rule_version,
            facts=facts,
            model_id=labels["model_id"],
            endpoint_id=labels["endpoint_id"],
            end_basis=event.end_basis,
            decision_reason=event.decision_reason,
            count=event.count,
            bucket_start=event.bucket_start.astimezone(dt.UTC) if event.bucket_start else None,
            bucket_end=event.bucket_end.astimezone(dt.UTC) if event.bucket_end else None,
            channel=event.channel,
            outcome=event.outcome,
            error_code=event.error_code,
            duration_ms=event.duration_ms,
            message=message,
            external_ref=event.external_ref.model_dump() if event.external_ref else None,
            limitations=tuple(dict.fromkeys(limitations)),
            # A fact a source re-observes across runs is sent without its
            # instance, so a replay stays a duplicate; it is kept, and marked.
            association_incomplete=event.source_instance_id is None,
            credential_id=credential_id,
        )
    )
