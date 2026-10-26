"""Every fact the status monitor and alert control plane emit survives ingestion.

The names and value sets are the monitor's technical schema inventory
(``monitor-fact-inventory.md`` as shared by the coordinator). It lists the
fact names that carry text, the fact names that carry numbers or booleans,
and the names used in ``rules_snapshot``. Values are the inventory's own
closed sets; identifiers are synthetic.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from serving.observability.alert_history.redaction import (
    INGEST_CODE_FACT_KEYS,
    INGEST_IDENTIFIER_FACT_KEYS,
    INGEST_LABEL_FACT_KEYS,
    sanitize_facts,
)
from serving.observability.alert_history.wire import EventIn, SourceStatusIn, to_record

RECEIPT_ACTIONS = [
    "opened",
    "repeated",
    "resolving",
    "orphan_resolution",
    "stale",
    "reopened",
    "quota_suppressed",
    "resolved_suppressed",
    "queued_next_generation",
    "cancelled_next_generation",
]
OUTBOX_STATES = [
    "blocked",
    "pending",
    "claimed",
    "uncertain",
    "completed",
    "cancelled",
    "failed",
    "manual_reconciliation_required",
]
OMISSION = ["missing", "unsafe", "too_long"]

#: Section 1 of the inventory: every string-valued fact key, every value.
STRING_FACTS: dict[str, list[str]] = {
    "action_status": ["failed", "manual_reconciliation_required"],
    "action_status_after": OUTBOX_STATES,
    "action_type": [
        "post_parent",
        "update_parent",
        "post_recovery",
        "post_analysis",
        "reserve_quota",
    ],
    "check": ["send", "reconciliation"],
    "claim_mode": ["execute", "reconcile"],
    "commit": ["applied", "stale_result_not_applied", "unknown"],
    "control_plane_action": RECEIPT_ACTIONS,
    "delivery_owner": ["legacy", "control-plane"],
    "end_trigger": ["catalog_departure"],
    "endpoint_id_omitted": OMISSION,
    "first_lost_at": ["2026-10-26T08:14:41.925Z"],
    "last_lost_at": ["2026-10-26T08:15:02.001Z"],
    "handoff_outcome": ["accepted", "rejected", "not_sent", "unknown"],
    "incident_key_form": ["fingerprint_fnv64"],
    "incident_state": ["committed", "commit_failed", "commit_not_attempted", "not_committed"],
    "lifecycle_action": RECEIPT_ACTIONS,
    "lifecycle_state": ["opening", "firing", "resolving", "suppressed", "resolved"],
    "model_id_omitted": OMISSION,
    "monitor_incident_key": [
        "status-monitor:cycle",
        "fp:4f2185d2122b562b",
        "status-monitor:model:Synthetic-Org/Synthetic-Model-V2.5-Instruct",
    ],
    "notification": ["delivered", "not_delivered", "no_channel", "held_storm_partial", "none"],
    "producer_source": ["status-monitor", "gateway"],
    "reason": [
        "authentication",
        "empty_response",
        "invalid_request",
        "malformed_response",
        "rate_limited",
        "timeout",
        "unknown",
        "upstream_error",
        "account_rejected",
        "discovery_failed",
        "not_configured",
        "availability_below_threshold",
        "connection_refused",
        "error",
    ],
    "record_role": ["evaluation"],
    "recovery_notification": ["none", "pending"],
    "source_event_id_form": ["fnv64"],
    "transition": ["firing", "resolved"],
    "transport": ["control_plane_rpc", "codex_relay", "slack_webhook"],
}

#: Section 2: numeric and boolean keys (any safe name keeps them).
TYPED_FACTS = {
    "active_models": 3,
    "affected_users": 12,
    "attempt": 2,
    "availability": 0.42,
    "cancelled_next_generation": True,
    "claim_epoch": 7,
    "consecutive_failures": 5,
    "executor_threw": True,
    "failure_streak_at_least": 2,
    "failure_threshold": 2,
    "final_failure_count": 9,
    "outage_duration_ms": 120000,
    "http_status": 400,
    "latency_ms": 25,
    "model_count": 6,
    "models_collapsed": 6,
    "observed_cycle_ok": False,
    "occurrence_count": 1,
    "pending_firing": True,
    "pending_resolved": False,
    "pending_materialization": True,
    "pending_transitions": 1,
    "recovered_models": 2,
    "replays_accepted": 1,
    "resolution_epoch": 4,
    "state_version": 1,
    "storm_incident": False,
    "storm_model_count": 8,
    "storm_threshold": 5,
    "threshold_met": True,
}


def _record(facts):
    event = EventIn.model_validate(
        {
            "origin_record_id": "inv:1",
            "kind": "delivery_result",
            "outcome": "unknown",
            "occurred_at": "2026-10-26T08:15:30.125Z",
            "target_environment": "test",
            "source_instance_id": "alert-control-plane:inv-1",
            "facts": facts,
        }
    )
    return to_record(
        event,
        source="alert_control_plane",
        source_namespace="test-cp",
        deployment_id="test-dep",
        credential_id="w",
    )


def test_every_inventory_string_key_is_recognized():
    recognized = INGEST_CODE_FACT_KEYS | INGEST_IDENTIFIER_FACT_KEYS | INGEST_LABEL_FACT_KEYS
    assert set(STRING_FACTS) <= recognized


@pytest.mark.parametrize(
    "name,value", [(name, value) for name, values in STRING_FACTS.items() for value in values]
)
def test_every_inventory_value_survives_exactly(name, value):
    record = _record({name: value})
    assert record.facts == {name: value}
    assert not [note for note in record.limitations if note.endswith(f":{name}")]


def test_typed_facts_survive_with_their_types():
    record = _record(dict(TYPED_FACTS))
    assert record.facts == TYPED_FACTS
    assert record.limitations == ()


def test_a_full_control_plane_attempt_survives_together():
    facts = {
        "action_type": "post_recovery",
        "claim_mode": "reconcile",
        "check": "reconciliation",
        "commit": "stale_result_not_applied",
        "action_status_after": "uncertain",
        "producer_source": "status-monitor",
        "attempt": 3,
        "claim_epoch": 2,
        "executor_threw": True,
        "model_id_omitted": "too_long",
        "endpoint_id_omitted": "unsafe",
        "source_event_id_form": "fnv64",
        "incident_key_form": "fingerprint_fnv64",
    }
    assert _record(facts).facts == facts


def test_a_long_mixed_case_monitor_key_is_kept_exactly():
    key = "status-monitor:model:" + ("Org-Name/Model.Version_" * 10)[: 200 - 21]
    assert len(key) == 200
    assert _record({"monitor_incident_key": key}).facts == {"monitor_incident_key": key}


@pytest.mark.parametrize(
    "value",
    [
        "status-monitor:model:" + "x" * 180,  # 201 characters
        "status-monitor:model:has space",
        "status-monitor:model:someone@example.com",
        "https://status.example/incident/1",
        "status-monitor:model:Bearer-abcdef",
        "status-monitor:model:sk-synthetic0123456789",
        "",
    ],
)
def test_an_identifier_name_refuses_what_is_not_one(value):
    record = _record({"monitor_incident_key": value, "attempt": 1})
    assert record.facts == {"attempt": 1}
    assert "omitted:monitor_incident_key" in record.limitations


@pytest.mark.parametrize(
    "name", ["action_status", "claim_mode", "producer_source", "lifecycle_action"]
)
def test_new_code_names_still_refuse_prose(name):
    facts, notes = sanitize_facts({name: "The executor said: please retry later"})
    assert facts == {} and notes == [f"omitted:{name}"]


def test_unknown_and_secret_names_are_still_dropped():
    facts, notes = sanitize_facts(
        {"prompt": "conversation text", "request_body": "{}", "webhook": 1, "count": 2}
    )
    assert facts == {"count": 2}
    assert set(notes) == {"omitted:prompt", "omitted:request_body", "omitted:webhook"}


def test_rules_snapshot_inventory_survives():
    rules = {
        "failure_threshold": 2,
        "storm_threshold": 5,
        "default_owner": "control-plane",
        "cycle_owner": "legacy",
        "rule_version": "1",
    }
    status = SourceStatusIn.model_validate(
        {
            "source_instance_id": "status-monitor:inv-1",
            "target_environment": "test",
            "observed_at": "2026-10-26T08:14:41.925Z",
            "rules_snapshot": rules,
        }
    )
    kept, notes = sanitize_facts(status.rules_snapshot)
    assert kept == rules and notes == []
    with pytest.raises(ValidationError):
        SourceStatusIn.model_validate(
            {
                "source_instance_id": "status-monitor:inv-1",
                "target_environment": "test",
                "observed_at": "2026-10-26T08:14:41.925Z",
                "rules_snapshot": {f"k{n}": n for n in range(65)},
            }
        )
