"""Validated contracts for the alert triage relay."""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - Pydantic resolves this annotation at runtime.
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

AlertStatus = Literal["firing", "resolved"]
AlertSeverity = Literal["critical", "error", "warn", "info"]

_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_VALUE_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+")


class AlertEvent(BaseModel):
    """Structured alert accepted from a trusted producer."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["1"] = "1"
    alert_id: str = Field(min_length=1, max_length=128)
    fingerprint: str = Field(min_length=1, max_length=512)
    source: str = Field(min_length=1, max_length=128)
    status: AlertStatus
    severity: AlertSeverity
    title: str = Field(min_length=1, max_length=500)
    environment: str = Field(min_length=1, max_length=128)
    occurred_at: dt.datetime
    summary: str = Field(min_length=1, max_length=4_000)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    slack_text: str = Field(min_length=1, max_length=40_000)
    deployment_sha: str | None = Field(default=None, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    dedupe_window_seconds: int = Field(default=300, ge=0, le=604_800)

    @field_validator("alert_id", "fingerprint", "source", "title", "environment", "summary")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """Reject whitespace-only identifiers and labels."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class TriageAnalysis(BaseModel):
    """Machine-readable Codex investigation result."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    classification: Literal[
        "code_bug",
        "upstream_provider",
        "configuration",
        "capacity",
        "authentication",
        "unknown",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    impact: str
    evidence: list[str] = Field(max_length=10)
    likely_cause: str
    recommended_actions: list[str] = Field(min_length=1, max_length=8)
    issue_recommendation: Literal["none", "create"]
    draft_pr_recommendation: Literal["none", "create"]


class SubmitAlertResponse(BaseModel):
    """Acknowledgement returned after the initial Slack delivery."""

    accepted: bool
    duplicate: bool
    fingerprint: str
    slack_thread_ts: str | None = None


def sanitize_for_agent(value: JsonValue, *, key: str = "", depth: int = 0) -> JsonValue:
    """Redact secrets and bound untrusted context before it reaches Codex."""
    normalized_key = key.lower().replace("-", "_")
    if any(part in normalized_key for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if depth >= 5:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(child_key)[:128]: sanitize_for_agent(
                child_value, key=str(child_key), depth=depth + 1
            )
            for child_key, child_value in list(value.items())[:50]
        }
    if isinstance(value, list):
        return [sanitize_for_agent(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        redacted = _BEARER_RE.sub("Bearer [REDACTED]", value)
        redacted = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
        return redacted[:2_000]
    return value
