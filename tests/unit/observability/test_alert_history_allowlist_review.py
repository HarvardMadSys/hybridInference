"""A redaction regex must not turn the history API into a raw-log sink."""

import datetime as dt

import pytest
from pydantic import ValidationError

from serving.observability.alert_history.wire import EventIn, SourceStatusIn, to_record
from serving.servers.routers.alert_history_auth import WriterCredential
from serving.servers.routers.alert_history_ingest import _snapshot


@pytest.mark.parametrize(
    "field", ["prompt", "request_body", "response_body", "raw_log", "arbitrary_extra"]
)
def test_ingest_only_keeps_recognized_fact_fields(field):
    sentinel = "Human conversation text must never become a diagnostic fact"
    try:
        event = EventIn.model_validate(
            {
                "origin_record_id": "review:allowlist",
                "kind": "condition_triggered",
                "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
                "target_environment": "test",
                "facts": {field: sentinel, "count": 4},
            }
        )
        record = to_record(
            event,
            source="status_monitor",
            source_namespace="review",
            deployment_id="review",
            credential_id="review",
        )
    except ValidationError:
        return
    assert record.facts["count"] == 4
    assert sentinel not in str(record.content()), (
        "A bounded/redacted arbitrary field is still not an allowlisted diagnostic fact"
    )


def test_source_rules_snapshot_is_not_an_arbitrary_text_channel():
    sentinel = "Raw conversation content must not be retained in rules metadata"
    credential = WriterCredential(
        id="review",
        sha256="a" * 64,
        expires_at="2099-01-01T00:00:00Z",
        source="status_monitor",
        source_namespace="review",
        deployment_id="review",
        target_environments=["test"],
    )
    try:
        status = SourceStatusIn.model_validate(
            {
                "source_instance_id": "review-instance",
                "target_environment": "test",
                "observed_at": dt.datetime.now(dt.UTC).isoformat(),
                "rules_snapshot": {"failure_threshold": 3, "prompt": sentinel},
            }
        )
        snapshot = _snapshot(status, credential)
    except ValidationError:
        return
    assert sentinel not in str(snapshot)
