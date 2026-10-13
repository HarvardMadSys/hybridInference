"""Real application validation must never persist submitted phone OTPs."""

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from serving.servers.app import create_app
from serving.servers.deps import AppServices


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,missing_field",
    [
        ({"phone": "13800138000", "code": "639271", "accepted_tos": True}, "user_name"),
        (
            {
                "phone": "13800138000",
                "code": "639271 ",
                "user_name": "Privacy tester",
                "accepted_tos": True,
            },
            "code",
        ),
    ],
)
async def test_phone_signup_validation_keeps_field_diagnostics_without_otp_input(
    payload, missing_field, caplog
):
    app = create_app()
    app.state.services = AppServices(router=MagicMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/phone/signup", json=payload)
    assert response.status_code == 422
    assert missing_field in response.text
    records = [
        record for record in caplog.records if record.getMessage() == "request_validation_error"
    ]
    assert records
    diagnostics = " ".join(record.detail for record in records)
    assert missing_field in diagnostics
    assert "639271" not in diagnostics
    assert "13800138000" not in diagnostics
    assert "input" not in diagnostics
    assert "ctx" not in diagnostics
    assert "639271" not in response.text
