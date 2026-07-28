"""Tests for the C0 runtime-override export tool."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from ops.db.export_runtime_overrides import (
    ROUTEWISE_SETTING_PREFIX,
    SECTIONS,
    build_report,
    collect_sections,
    redact_sensitive_settings,
    split_routewise_settings,
)


def test_redacts_secret_bearing_setting_values() -> None:
    rows = [
        {"key": "broadcast_slack_token", "value": "xoxb-123"},
        {"key": "signup_enabled", "value": "true"},
    ]
    redacted = redact_sensitive_settings(rows)
    assert redacted[0]["value"] == "<redacted>"
    assert redacted[1]["value"] == "true"
    # Input rows are not mutated.
    assert rows[0]["value"] == "xoxb-123"


def test_splits_routewise_model_settings_from_generic_ones() -> None:
    rw_key = f"{ROUTEWISE_SETTING_PREFIX}alpha:qwen3.6-35b"
    rows = [{"key": rw_key, "value": "0.2"}, {"key": "signup_enabled", "value": "true"}]
    routewise, other = split_routewise_settings(rows)
    assert [r["key"] for r in routewise] == [rw_key]
    assert [r["key"] for r in other] == ["signup_enabled"]


@dataclasses.dataclass
class _ProviderRow:
    provider: str
    created_at: datetime


class _StubStore:
    """OperationalStore stand-in covering every exported section."""

    async def list_settings(self):
        return [
            {"key": f"{ROUTEWISE_SETTING_PREFIX}alpha:m1", "value": "0.5"},
            {"key": "smtp_password", "value": "hunter2"},
        ]

    async def list_model_visibility_overrides(self):
        return [{"model_id": "m1", "required_role": "internal"}]

    async def list_disabled_providers(self):
        return [{"provider": "deepseek", "created_at": datetime(2026, 7, 1, tzinfo=UTC)}]

    async def list_model_concurrency_exemptions(self):
        return []

    async def list_all_weight_overrides(self):
        return [{"model_id": "m1", "endpoint_id": "openai:h:443", "weight": 0}]

    async def list_provider_definitions(self):
        raise RuntimeError("relation does not exist")


@pytest.mark.asyncio
async def test_collect_captures_rows_and_per_section_errors() -> None:
    sections = await collect_sections(_StubStore())
    assert set(sections) == set(SECTIONS)
    assert sections["weight_overrides"] == [
        {"model_id": "m1", "endpoint_id": "openai:h:443", "weight": 0}
    ]
    # Datetimes are stringified so the report is JSON-serializable as-is.
    assert isinstance(sections["disabled_providers"][0]["created_at"], str)
    assert sections["provider_definitions"] == {"error": "RuntimeError: relation does not exist"}


@pytest.mark.asyncio
async def test_report_splits_redacts_and_counts() -> None:
    sections = await collect_sections(_StubStore())
    report = build_report(sections, database="user@host:5432/db")

    assert report["counts"]["routewise_model_settings"] == 1
    assert report["counts"]["site_settings"] == 1
    assert report["counts"]["provider_definitions"] == "error"
    assert report["sections"]["site_settings"][0]["value"] == "<redacted>"
    # The whole report must serialize without a custom encoder.
    json.dumps(report)
