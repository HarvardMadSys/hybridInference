"""Turnstile request construction tests."""

from __future__ import annotations

import pytest

from serving.utils import turnstile


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote_ip,expected_remote_ip",
    [("unknown", None), ("203.0.113.7", "203.0.113.7")],
)
async def test_unresolved_sentinel_is_not_sent_as_turnstile_remote_ip(
    monkeypatch, remote_ip, expected_remote_ip
):
    monkeypatch.setattr(turnstile, "_get_secret_key", lambda: "test-secret")
    posted_data = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"success": True}

    class _Client:
        def __init__(self, *, timeout):
            assert timeout == 5.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, data):
            posted_data.update(data)
            return _Response()

    monkeypatch.setattr(turnstile.httpx, "AsyncClient", _Client)

    assert await turnstile.verify_turnstile_token("valid-token", remote_ip) is True
    if expected_remote_ip is None:
        assert "remoteip" not in posted_data
    else:
        assert posted_data["remoteip"] == expected_remote_ip
