"""Alert-history credentials: hashes only, scoped, fail closed, fresh within 30 s."""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from pydantic import ValidationError

from serving.config import app_config
from serving.config.app_config_registry import static_entries
from serving.config.settings import get_settings
from serving.observability.alert_history.wire import (
    EventIn,
    IngestRequest,
    SourceStatusIn,
    to_record,
)
from serving.servers.routers import alert_history_auth as auth
from tests.fixtures.app_config_store import FakeAppConfigStore, row

READER_TOKEN = "ahr_synthetic_reader_token_0123456789abcdef"
WRITER_TOKEN = "ahw_synthetic_writer_token_0123456789abcdef"
LATER = "2099-01-01T00:00:00Z"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def reader(**over):
    return {
        "id": "reader-1",
        "sha256": _sha(READER_TOKEN),
        "deployment_ids": ["test-deployment"],
        "target_environments": ["test"],
        "expires_at": LATER,
        **over,
    }


def writer(**over):
    return {
        "id": "writer-1",
        "sha256": _sha(WRITER_TOKEN),
        "source": "status_monitor",
        "source_namespace": "test-monitor",
        "deployment_id": "test-deployment",
        "target_environments": ["test"],
        "expires_at": LATER,
        **over,
    }


@pytest.fixture
def config_env(monkeypatch, tmp_path):
    for entry in static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    (tmp_path / "models.yaml").write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    auth._CACHE.clear()
    yield monkeypatch
    auth._CACHE.clear()
    app_config.reset_state()
    get_settings.cache_clear()


def _set(monkeypatch, readers, writers):
    monkeypatch.setenv(auth.READ_SETTING, json.dumps(readers))
    monkeypatch.setenv(auth.WRITE_SETTING, json.dumps(writers))


def test_lists_parse_and_hold_only_digests(config_env):
    _set(config_env, [reader()], [writer()])
    creds = auth.load_credentials()
    assert creds.readers[0].scope().deployment_ids == ("test-deployment",)
    assert creds.writers[0].source == "status_monitor"
    assert READER_TOKEN not in repr(creds) and WRITER_TOKEN not in repr(creds)


@pytest.mark.parametrize(
    "readers,writers",
    [
        ("not json", []),
        ([reader(sha256="short")], []),
        ([reader(expires_at="2099-01-01T00:00:00")], []),  # no offset
        ([], [writer(source="backend")]),  # reserved: cannot impersonate the gateway
        ([reader()], [writer(sha256=_sha(READER_TOKEN))]),  # one token, two kinds
        ([], [writer(), writer(id="writer-2", sha256="b" * 64, deployment_id="other")]),
        ([reader(extra="x")], []),
    ],
)
def test_bad_configuration_fails_closed(config_env, readers, writers):
    config_env.setenv(
        auth.READ_SETTING, readers if isinstance(readers, str) else json.dumps(readers)
    )
    config_env.setenv(auth.WRITE_SETTING, json.dumps(writers))
    with pytest.raises(auth.CredentialsUnavailable):
        auth.load_credentials()


def test_writer_dependency_scopes_and_separates_kinds(config_env, monkeypatch):
    from serving.observability.alert_history import runtime as runtime_mod

    class _Enabled:
        enabled = True

    monkeypatch.setattr(runtime_mod, "_RUNTIME", _Enabled())
    _set(
        config_env,
        [reader()],
        [writer(), writer(id="old", sha256="c" * 64, expires_at="2001-01-01T00:00:00Z")],
    )
    assert auth.require_history_writer(f"Bearer {WRITER_TOKEN}").source_namespace == "test-monitor"
    with pytest.raises(Exception) as denied:
        auth.require_history_writer(f"Bearer {READER_TOKEN}")
    assert denied.value.status_code == 403
    with pytest.raises(Exception) as bad:
        auth.require_history_writer("Bearer " + "z" * 40)
    assert bad.value.status_code == 401


def test_stale_configuration_refuses_tokens(config_env, monkeypatch):
    _set(config_env, [reader()], [writer()])
    monkeypatch.setattr(app_config, "seconds_since_read", lambda: 31.0)
    with pytest.raises(auth.CredentialsUnavailable):
        auth.load_credentials()
    monkeypatch.setattr(app_config, "seconds_since_read", lambda: 29.0)
    assert auth.load_credentials().writers


@pytest.mark.asyncio
async def test_freshness_follows_the_applied_snapshot_not_any_read(config_env, monkeypatch):
    """A revoked credential cannot outlive a configuration that stopped refreshing."""
    clock = [1000.0]
    monkeypatch.setattr(app_config.time, "monotonic", lambda: clock[0])
    store = FakeAppConfigStore([row(auth.WRITE_SETTING, json.dumps([writer()]), secret=True)])
    await app_config.load(store)
    app_config.attach(store)
    assert app_config.seconds_since_read() == 0.0

    # The table is unreachable from now on: every refresh fails.
    async def down(**options):
        raise ConnectionError("synthetic outage")

    monkeypatch.setattr(store, "fetch_all", down)
    clock[0] += 25
    with pytest.raises(ConnectionError):
        await app_config.refresh()
    assert auth.load_credentials().writers  # 25 s: still inside the bound
    clock[0] += 6
    with pytest.raises(ConnectionError):
        await app_config.refresh()
    with pytest.raises(auth.CredentialsUnavailable):
        auth.load_credentials()


@pytest.mark.asyncio
async def test_a_discarded_read_does_not_refresh_the_clock(config_env, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(app_config.time, "monotonic", lambda: clock[0])
    store = FakeAppConfigStore([row("SITE_NAME", "a")])
    await app_config.load(store)
    app_config.attach(store)
    original = store.fetch_all

    async def overtaken(**options):
        rows = await original(**options)
        # Another apply lands while this read is in flight, so its result is
        # thrown away; it confirms nothing about the snapshot in force.
        app_config._STATE.generation += 1
        clock[0] += 40
        return rows

    monkeypatch.setattr(store, "fetch_all", overtaken)
    await app_config.refresh()
    assert app_config.seconds_since_read() == 40.0


# -- ingestion boundary ------------------------------------------------------------


def _event(**over):
    return {
        "origin_record_id": "inc:i1:g1:triggered",
        "kind": "condition_triggered",
        "occurred_at": "2026-10-26T08:15:30.125Z",
        "target_environment": "test",
        **over,
    }


@pytest.mark.parametrize(
    "event",
    [
        _event(source="backend"),
        _event(source_namespace="other"),
        _event(deployment_id="other"),
        _event(occurred_at=1_790_000_000),
        _event(occurred_at="2026-10-26T08:15:30"),
        _event(origin_record_id="x" * 201),
        _event(origin_record_id="sk-synthetic0123456789"),
        _event(action_id="someone@example.com"),
        _event(kind="free_text"),
        _event(facts={"nested": {"a": 1}}),
        _event(facts={"n": float("inf")}),
        _event(outcome="accepted"),  # outcome only on delivery_result
        _event(kind="delivery_result"),  # result needs an outcome
        _event(origin="channel_import"),
    ],
)
def test_ingest_validation_refuses(event):
    with pytest.raises(ValidationError):
        IngestRequest.model_validate({"schema_version": 1, "events": [event]})


def test_ingest_redacts_labels_and_keeps_typed_facts():
    event = EventIn.model_validate(
        _event(
            facts={
                "failed_probes": 3,
                "ratio": 0.5,
                "ok": False,
                "provider": "prov see https://x.example/a?token=s",
                "status": "see https://x.example/a?token=s",  # prose under a code name
                "note": "free text is not a fact",
            },
            model_id="model-1 Authorization: Bearer synthetic-123456",
        )
    )
    record = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert record.facts["failed_probes"] == 3 and record.facts["ok"] is False
    assert record.facts["ratio"] == 0.5
    assert "token=s" not in record.facts["provider"]
    assert "status" not in record.facts and "note" not in record.facts
    assert "synthetic-123456" not in record.model_id
    assert {"redacted:provider", "redacted:model_id", "omitted:status", "omitted:note"} <= set(
        record.limitations
    )
    again = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert again.payload_digest == record.payload_digest  # resend is a duplicate


#: Codes the status monitor and the alert control plane actually send, from
#: their technical field inventory (synthetic values of the real shapes).
MONITOR_CODES = {
    "reason": "discovery_failed",
    "handoff_outcome": "accepted",
    "control_plane_action": "opened",
    "lifecycle_state": "opening",
    "monitor_incident_key": "status-monitor:cycle",
    "end_trigger": "catalog_departure",
    "recovery_notification": "none",
    "action_type": "notify_resolved",
    "commit": "stale_result_not_applied",
    "observation_basis": "observed_healthy",
    "incident_state": "not_committed",
    "notification": "not_delivered",
    "record_role": "evaluation",
    "delivery_owner": "control-plane",
    "transport": "control_plane_rpc",
    "transition": "resolved",
}


@pytest.mark.parametrize("name,value", sorted(MONITOR_CODES.items()))
def test_recognized_codes_are_kept(name, value):
    event = EventIn.model_validate(
        _event(kind="delivery_result", outcome="unknown", facts={name: value, "http_status": 500})
    )
    record = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert record.facts == {name: value, "http_status": 500}
    assert not [n for n in record.limitations if n.endswith(name)]


def test_outcome_unknown_keeps_what_explains_it():
    # A control-plane handoff: the intermediary accepted, the channel result is
    # reported elsewhere. Every fact needed to read "unknown" correctly stays.
    facts = {
        "transport": "control_plane_rpc",
        "delivery_owner": "control-plane",
        "transition": "firing",
        "handoff_outcome": "accepted",
        "control_plane_action": "opened",
        "lifecycle_state": "opening",
        "monitor_incident_key": "status-monitor:model:4f2185d2122b562b",
        "occurrence_count": 1,
        "state_version": 1,
        "commit": "unknown",
    }
    event = EventIn.model_validate(
        _event(
            kind="delivery_result",
            outcome="unknown",
            channel="other",
            error_code="control_plane_handoff_accepted",
            facts=facts,
        )
    )
    record = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert record.facts == facts


@pytest.mark.parametrize(
    "value", ["Applied OK", "applied\nnext", "x" * 101, "sk-synthetic0123456789", ""]
)
def test_a_code_name_cannot_carry_prose(value):
    event = EventIn.model_validate(_event(facts={"commit": value, "count": 1}))
    record = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert record.facts == {"count": 1}
    assert "omitted:commit" in record.limitations


def test_events_without_an_instance_are_kept_but_marked():
    event = EventIn.model_validate(_event())
    record = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert record.association_incomplete is True
    assert "no_source_instance" in record.limitations
    with_instance = to_record(
        EventIn.model_validate(_event(source_instance_id="status-monitor:inv-1")),
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert with_instance.association_incomplete is False


def _status(**over):
    return {
        "source_instance_id": "status-monitor:inv-1",
        "target_environment": "test",
        "observed_at": "2026-10-26T08:14:41.925Z",
        "started_at": "2026-10-26T08:14:41.919Z",
        "pending_count": 0,
        **over,
    }


def test_final_snapshot_is_accepted():
    status = SourceStatusIn.model_validate(
        _status(
            stopped_at="2026-10-26T08:14:41.925Z", stop_reason="invocation_complete", lost_at_stop=2
        )
    )
    assert status.stop_reason == "invocation_complete" and status.lost_at_stop == 2


@pytest.mark.parametrize(
    "over",
    [
        {"stopped_at": "2026-10-26T08:14:41.925Z"},  # not all three
        {"stop_reason": "invocation_complete", "lost_at_stop": 0},
        {
            "stopped_at": "2026-10-26T08:14:41.925Z",
            "stop_reason": "invocation_complete",
            "lost_at_stop": 0,
            "pending_count": 3,
        },  # finished with work queued
        {
            "stopped_at": "2026-10-26T08:14:41.925Z",
            "stop_reason": "invocation_complete",
            "lost_at_stop": 0,
            "pending_count": None,
        },  # backlog unknown
        {
            "stopped_at": "2026-10-26T08:14:41.925",
            "stop_reason": "invocation_complete",
            "lost_at_stop": 0,
        },  # naive timestamp
        {
            "stopped_at": "2026-10-26T08:14:40.000Z",
            "stop_reason": "invocation_complete",
            "lost_at_stop": 0,
        },  # before started_at
        {"stopped_at": "2026-10-26T08:14:41.925Z", "stop_reason": "shutdown", "lost_at_stop": 0},
        {
            "stopped_at": "2026-10-26T08:14:41.925Z",
            "stop_reason": "invocation_complete",
            "lost_at_stop": -1,
        },
        {
            "stopped_at": "2026-10-26T08:14:41.925Z",
            "stop_reason": "invocation_complete",
            "lost_at_stop": 10**9 + 1,
        },
    ],
)
def test_incoherent_final_snapshots_are_refused(over):
    with pytest.raises(ValidationError):
        SourceStatusIn.model_validate(_status(**over))


def test_rules_snapshot_bounds_match_the_monitor():
    SourceStatusIn.model_validate(_status(rules_snapshot={f"k{n}": n for n in range(64)}))
    with pytest.raises(ValidationError):
        SourceStatusIn.model_validate(_status(rules_snapshot={f"k{n}": n for n in range(65)}))
    with pytest.raises(ValidationError):  # 16 KiB canonical JSON, like the monitor
        SourceStatusIn.model_validate(
            _status(rules_snapshot={f"k{n}": "x" * 400 for n in range(42)})
        )


def test_writer_cadence_is_bounded():
    assert auth.WriterCredential.model_validate(writer()).stale_after_sec == 180
    assert (
        auth.WriterCredential.model_validate(writer(stale_after_sec=1800)).stale_after_sec == 1800
    )
    for bad in (59, 86_401):
        with pytest.raises(ValidationError):
            auth.WriterCredential.model_validate(writer(stale_after_sec=bad))


def test_ingest_drops_secret_named_facts():
    event = EventIn.model_validate(_event(facts={"api_key": "abc", "count": 2}))
    record = to_record(
        event,
        source="status_monitor",
        source_namespace="ns",
        deployment_id="dep",
        credential_id="w",
    )
    assert record.facts == {"count": 2}
    assert "omitted:api_key" in record.limitations


def test_expired_and_aware_timestamps():
    with pytest.raises(ValidationError):
        auth.WriterCredential.model_validate(writer(expires_at="2026-01-01"))
    assert auth.WriterCredential.model_validate(writer()).expires_at > dt.datetime.now(dt.UTC)
