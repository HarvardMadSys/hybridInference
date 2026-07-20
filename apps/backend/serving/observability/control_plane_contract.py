"""Dormant Python representation of the canonical alert control-plane contract."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def _contains_ip_address(value: str) -> bool:
    for token in re.split(r"[^0-9A-Fa-f:.]+", value):
        if not token or ("." not in token and ":" not in token):
            continue
        try:
            ipaddress.ip_address(token.strip("[]"))
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


class ControlPlaneAlertEvent(BaseModel):
    """The sole producer wire contract introduced by control-plane Phase 1."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_RE.pattern)
    alert_type: Literal["provider_circuit_open"]
    fingerprint: str
    status: Literal["firing", "resolved"]
    severity: Literal["critical", "error", "warn", "info"]
    title: str
    occurred_at: str
    summary: str
    context: ProviderCircuitContext
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
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def parse_control_plane_alert_event(
    payload: object,
    *,
    now: dt.datetime | None = None,
    max_future_skew: dt.timedelta = dt.timedelta(minutes=5),
) -> ControlPlaneAlertEvent:
    """Parse a producer event and enforce the future-clock-skew boundary."""
    event = ControlPlaneAlertEvent.model_validate(payload)
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
