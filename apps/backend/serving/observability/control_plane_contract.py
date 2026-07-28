"""Dormant Python representation of the canonical alert control-plane contract."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
_UNSAFE_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]"
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|authorization|cookie|password|secret|token)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bhyi-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[oprsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|gsk|xai|rk)[-_][A-Za-z0-9._-]{6,}", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{10,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", re.IGNORECASE),
)
_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reveal|print|repeat|expose)\s+(?:the\s+)?(?:system|developer)\s+prompt\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:system|developer|assistant)\s+(?:prompt|message)\s*:", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+(?:chatgpt|codex|an?\s+ai)\b", re.IGNORECASE),
    re.compile(r"<\|(?:system|assistant|developer|tool)\|>", re.IGNORECASE),
    re.compile(r"\b(?:begin|end)\s+(?:system|developer|instructions?)\b", re.IGNORECASE),
)

ProviderFailureReason = Literal[
    "authentication",
    "availability_below_threshold",
    "connection_refused",
    "error",
    "rate_limited",
    "timeout",
    "unknown",
    "upstream_error",
]
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

#: Listing addresses only makes sense for a metric whose response is to block
#: them. Mirrors ``ADDRESS_BEARING_METRICS`` in the TypeScript validator.
_ADDRESS_BEARING_METRICS = frozenset({"auth_failure_count"})
_MAX_SOURCE_ADDRESSES = 5
#: A bare implementation label. The general untrusted-text rules are not enough
#: here: a credential-free DSN like ``postgres://localhost/app`` passes all of
#: them, so the shape itself is constrained.
_DEPENDENCY_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _safe_untrusted_text(value: str, *, field: str, max_length: int) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field} is too long")
    if _UNSAFE_CONTROL_RE.search(normalized):
        raise ValueError(f"{field} contains a control character")
    if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"{field} contains secret material")
    if _EMAIL_RE.search(normalized) or _contains_ip_address(normalized):
        raise ValueError(f"{field} contains a user or network identifier")
    if any(pattern.search(normalized) for pattern in _PROMPT_INJECTION_PATTERNS):
        raise ValueError(f"{field} contains control instructions")
    return normalized


_IPV4_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}$")


def _contains_ip_address(value: str) -> bool:
    for token in re.split(r"[^0-9A-Fa-f:.]+", value):
        token = token.strip("[]")
        if not token or ("." not in token and ":" not in token):
            continue
        candidates = [token]
        # A bare IPv4 host:port stays one token (":8000" is not split off), so
        # ip_address rejects it and the identifier would slip through. IPv6
        # literals use brackets, which the split above already strips to the host.
        ipv4_port = _IPV4_PORT_RE.match(token)
        if ipv4_port is not None:
            candidates.append(ipv4_port.group(1))
        for candidate in candidates:
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return True
    return False


class ProviderCircuitContext(BaseModel):
    """Typed context accepted for ``provider_circuit_open`` events."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    availability: float | None = Field(default=None, ge=0, le=1, strict=True)
    error: str | None = None
    affected_users: int | None = Field(default=None, ge=0, le=1_000_000_000, strict=True)
    consecutive_failures: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
        strict=True,
    )
    final_failure_count: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000,
        strict=True,
    )
    outage_duration_ms: int | None = Field(
        default=None,
        ge=0,
        le=365 * 24 * 60 * 60 * 1_000,
        strict=True,
    )
    reason: ProviderFailureReason | None = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        """Reject unsafe provider labels before persistence."""
        return _safe_untrusted_text(value, field="context.provider", max_length=256)

    @field_validator("error")
    @classmethod
    def validate_error(cls, value: str | None) -> str | None:
        """Reject unsafe upstream error text before persistence."""
        if value is None:
            return None
        return _safe_untrusted_text(value, field="context.error", max_length=2_000)


class MetricThresholdContext(BaseModel):
    """Typed context accepted for ``metric_threshold_breach`` events.

    Every field is a number or a closed enum by design. This type replaces
    backend alerts that embedded source IPs, key prefixes, user ids, and
    pre-formatted rate strings, so there is deliberately no free-text field for
    those to move into.
    """

    model_config = ConfigDict(extra="forbid")

    metric: BreachedMetric
    observed: float = Field(ge=0, le=1e12)
    threshold: float = Field(ge=0, le=1e12)
    window_sec: int | None = Field(default=None, ge=1, le=31 * 24 * 60 * 60, strict=True)
    scope: BreachScope | None = None
    subject: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_RE.pattern,
    )
    source_addresses: list[str] | None = None
    distinct_sources: int | None = Field(default=None, ge=0, le=1_000_000_000, strict=True)
    top_source_share: float | None = Field(default=None, ge=0, le=1)
    sample_count: int | None = Field(default=None, ge=0, le=1_000_000_000, strict=True)

    @field_validator("source_addresses")
    @classmethod
    def validate_source_addresses(cls, values: list[str] | None) -> list[str] | None:
        """Require real, unique IP addresses — the one typed exception to the rule."""
        if values is None:
            return None
        if not 1 <= len(values) <= _MAX_SOURCE_ADDRESSES:
            raise ValueError(
                f"context.source_addresses must contain 1 to {_MAX_SOURCE_ADDRESSES} addresses"
            )
        normalized = [str(ipaddress.ip_address(value)) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("context.source_addresses must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_addresses_are_scoped(self) -> MetricThresholdContext:
        """Keep addresses on the one metric whose response is to block them."""
        if self.source_addresses is not None and self.metric not in _ADDRESS_BEARING_METRICS:
            raise ValueError(f"context.source_addresses is not allowed for metric {self.metric}")
        return self


class DependencyUnavailableContext(BaseModel):
    """Typed context accepted for ``dependency_unavailable`` events."""

    model_config = ConfigDict(extra="forbid")

    dependency: UnavailableDependency
    backend: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=_DEPENDENCY_BACKEND_RE.pattern,
    )
    #: Replaces the backend's free-text ``error``, which is where a DSN or host
    #: would otherwise reach Slack, while keeping the triage signal.
    reason: DependencyFailureReason | None = None


class _AlertEventBase(BaseModel):
    """Fields every canonical event carries, whatever its type."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_RE.pattern)
    fingerprint: str
    status: Literal["firing", "resolved"]
    severity: Literal["critical", "error", "warn", "info"]
    title: str
    occurred_at: str
    summary: str
    evidence_refs: list[str] = Field(max_length=20)

    @field_validator("event_id", mode="before")
    @classmethod
    def normalize_event_id(cls, value: object) -> object:
        """Match the TypeScript parser's NFC and surrounding-space normalization."""
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value).strip()
        return value

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        """Validate the stable incident fingerprint."""
        return _safe_untrusted_text(value, field="fingerprint", max_length=512)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Validate the untrusted alert title."""
        return _safe_untrusted_text(value, field="title", max_length=500)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        """Validate the untrusted alert summary."""
        return _safe_untrusted_text(value, field="summary", max_length=4_000)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: str) -> str:
        """Require a real RFC 3339 timestamp and normalize it to UTC."""
        if not _RFC3339_RE.fullmatch(value):
            raise ValueError("occurred_at must be an ISO timestamp")
        # datetime.fromisoformat on Python 3.10 (a supported runtime) rejects
        # 7-9 digit fractional seconds. Truncate to microseconds before parsing;
        # the value is normalized to milliseconds below regardless, so this only
        # widens which producers parse, without changing the stored timestamp.
        candidate = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("occurred_at must be an ISO timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        normalized = parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")
        return normalized.replace("+00:00", "Z")

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, values: list[str]) -> list[str]:
        """Restrict evidence to unique repository-relative POSIX paths."""
        normalized: list[str] = []
        for value in values:
            reference = _safe_untrusted_text(
                value,
                field="evidence_refs",
                max_length=500,
            )
            path = PurePosixPath(reference)
            if (
                path.is_absolute()
                or reference.startswith("~")
                or "\\" in reference
                or "?" in reference
                or "#" in reference
                or any(part in {"", ".", ".."} for part in reference.split("/"))
                or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", reference)
                or re.search(r"%(?:2e|2f|5c)", reference, re.IGNORECASE)
            ):
                raise ValueError("evidence_refs must contain repository-relative paths")
            normalized.append(reference)
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs must not contain duplicates")
        return normalized


class ProviderCircuitAlertEvent(_AlertEventBase):
    """A provider's circuit breaker opened or closed."""

    alert_type: Literal["provider_circuit_open"]
    context: ProviderCircuitContext


class MetricThresholdAlertEvent(_AlertEventBase):
    """A gateway metric crossed, or fell back below, its configured threshold."""

    alert_type: Literal["metric_threshold_breach"]
    context: MetricThresholdContext

    @model_validator(mode="after")
    def validate_breach_is_coherent(self) -> MetricThresholdAlertEvent:
        """A firing breach whose observed value is under the threshold is a bug.

        Rendering it would put "observed 3, threshold 10" on a card that claims
        the threshold was crossed, so it is rejected at the contract rather than
        surfaced to on-call.
        """
        if self.status == "firing" and self.context.observed < self.context.threshold:
            raise ValueError(
                "firing metric_threshold_breach requires observed to be at or above threshold"
            )
        return self


class DependencyUnavailableAlertEvent(_AlertEventBase):
    """A store the gateway depends on failed, or passed, its health check."""

    alert_type: Literal["dependency_unavailable"]
    context: DependencyUnavailableContext


#: Discriminated on ``alert_type``, so a payload is validated against exactly
#: one context shape rather than whichever union member happens to accept it.
ControlPlaneAlertEvent = Annotated[
    ProviderCircuitAlertEvent | MetricThresholdAlertEvent | DependencyUnavailableAlertEvent,
    Field(discriminator="alert_type"),
]

_EVENT_ADAPTER: TypeAdapter[
    ProviderCircuitAlertEvent | MetricThresholdAlertEvent | DependencyUnavailableAlertEvent
] = TypeAdapter(ControlPlaneAlertEvent)


def parse_control_plane_alert_event(
    payload: object,
    *,
    now: dt.datetime | None = None,
    max_future_skew: dt.timedelta = dt.timedelta(minutes=5),
) -> ProviderCircuitAlertEvent | MetricThresholdAlertEvent | DependencyUnavailableAlertEvent:
    """Parse a producer event and enforce the future-clock-skew boundary."""
    event = _EVENT_ADAPTER.validate_python(payload)
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    if max_future_skew < dt.timedelta(0):
        raise ValueError("max_future_skew must not be negative")
    current = current.astimezone(dt.timezone.utc)
    occurred = dt.datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    if occurred > current + max_future_skew:
        raise ValueError("occurred_at is too far in the future")
    return event
