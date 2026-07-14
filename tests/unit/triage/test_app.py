"""Tests for relay authentication and HTTP acknowledgement."""

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from serving.triage.app import create_app
from serving.triage.config import TriageSettings
from serving.triage.models import AlertEvent, SubmitAlertResponse


class FakeService:
    running = True

    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def submit(self, event: AlertEvent) -> SubmitAlertResponse:
        self.events.append(event)
        return SubmitAlertResponse(
            accepted=True,
            duplicate=False,
            fingerprint=event.fingerprint,
            slack_thread_ts="123.45",
        )


def payload() -> dict[str, object]:
    return {
        "version": "1",
        "alert_id": "alert-1",
        "fingerprint": "test:alert",
        "source": "test",
        "status": "firing",
        "severity": "error",
        "title": "Failure",
        "environment": "test",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "summary": "Failure",
        "context": {},
        "slack_text": "Failure",
    }


def test_alert_endpoint_requires_bearer_token(tmp_path):
    settings = TriageSettings(
        relay_token=SecretStr("expected-token"),
        state_dir=tmp_path,
        repository_path=tmp_path,
    )
    service = FakeService()
    with TestClient(create_app(settings, service=service)) as client:
        unauthorized = client.post("/v1/alerts", json=payload())
        accepted = client.post(
            "/v1/alerts",
            json=payload(),
            headers={"Authorization": "Bearer expected-token"},
        )

    assert unauthorized.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["slack_thread_ts"] == "123.45"
    assert len(service.events) == 1


def test_configured_relay_protects_process_before_starting_worker(tmp_path):
    settings = TriageSettings(
        relay_token=SecretStr("relay-secret"),
        slack_bot_token=SecretStr("slack-secret"),
        slack_channel_id="C0123456789",
        codex_api_key=SecretStr("service-secret"),
        state_dir=tmp_path,
        repository_path=tmp_path,
    )

    with (
        patch("serving.triage.app.protect_process_secrets") as protect,
        TestClient(create_app(settings)) as client,
    ):
        assert client.get("/healthz").json()["ready"] is True

    protect.assert_called_once_with()
