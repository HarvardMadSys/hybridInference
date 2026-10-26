"""Independent review regressions for the approved history failure boundaries.

These cases were reproduced while the first implementation was being written.
They assert capture and shutdown behavior, using synthetic data and no network.
"""

import asyncio
import datetime as dt
import time

import pytest
from pydantic import ValidationError

from serving.observability.alert_history.capture import BackendCapture, BackendIdentity
from serving.observability.alert_history.queue import HistoryQueue, HistoryWriter
from serving.observability.alert_history.wire import EventIn, to_record


async def test_stop_budget_includes_the_already_running_database_call():
    entered = asyncio.Event()

    class StalledStore:
        async def heartbeat(self, heartbeat):
            entered.set()
            await asyncio.Event().wait()

        async def write_batch(self, records, heartbeat):
            return 0

    queue = HistoryQueue()
    writer = HistoryWriter(
        queue,
        StalledStore(),
        heartbeat=lambda: {},
        batch_timeout_sec=0.5,
        flush_interval_sec=0.01,
    )
    writer.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    started = time.monotonic()
    await writer.stop(drain_budget_sec=0.03)
    # Allow ample scheduler overhead, but not the unrelated 500 ms DB timeout.
    assert time.monotonic() - started < 0.25, (
        "The shutdown budget must include an in-flight write/heartbeat; "
        "do not wait for the DB timeout before starting that budget"
    )


@pytest.mark.parametrize("field", ["reason", "note", "model"])
def test_allowlisted_text_fields_cannot_persist_credential_fragments(field):
    records = []
    identity = BackendIdentity(
        source_namespace="review-test",
        deployment_id="review-test",
        target_environment="test",
        source_instance_id="review-instance",
        process_id="review-process",
        started_at=dt.datetime.now(dt.UTC),
    )
    capture = BackendCapture(
        identity,
        lambda record: records.append(record) or True,
        fence_secret="synthetic-review-fence",
    )
    sentinel = "SYNTHETIC-REVIEW-CREDENTIAL"
    capture.on_breach(
        "review_rule",
        severity="error",
        title="Synthetic alert",
        kind="state",
        context={field: f"Authorization: Bearer {sentinel}", "count": 4},
    )
    assert len(records) == 1
    assert records[0].kind == "condition_triggered"
    assert records[0].facts["count"] == 4
    assert sentinel not in str(records[0].content()), (
        "A safe field name does not make arbitrary string values safe; "
        "redact or omit credential-shaped text before offering history"
    )


@pytest.mark.parametrize("location", ["facts", "message"])
def test_external_writer_cannot_bypass_snapshot_redaction(location):
    sentinel = "SYNTHETIC-INGEST-CREDENTIAL"
    payload = {
        "origin_record_id": "review:1",
        "kind": "delivery_result",
        "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        "target_environment": "test",
        "outcome": "rejected",
    }
    if location == "facts":
        payload["facts"] = {"authorization": f"Bearer {sentinel}"}
    else:
        payload["message"] = {
            "title": "Synthetic delivery",
            "body": f"Authorization: Bearer {sentinel}",
            "format": "plain",
            "format_version": "review-1",
            "redacted": True,
            "truncated": False,
        }
    try:
        event = EventIn.model_validate(payload)
        record = to_record(
            event,
            source="status_monitor",
            source_namespace="review-test",
            deployment_id="review-test",
            credential_id="review-writer",
        )
    except ValidationError:
        return  # Rejecting unsafe input is also an acceptable fail-closed outcome.
    assert sentinel not in str(record.content()), (
        "The ingestion boundary must reject or sanitize credentials; "
        "a producer-controlled redacted flag cannot make the content safe"
    )
