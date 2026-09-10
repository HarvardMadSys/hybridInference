"""Exercise the opt-in quota example over loopback sockets, without a database.

These tests live outside tests/unit because that tier can stub aiohttp.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import re
import select
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from distributions.example import quota_extension
from serving.admin import provider_quotas

_REPO = Path(__file__).resolve().parents[2]
_SERVER = _REPO / "distributions/example/fixtures/fake-openai-provider/server.py"
_CHAT = {"model": "example-upstream", "messages": [{"role": "user", "content": "ping"}]}


@pytest.fixture(scope="module")
def fake_provider():
    spec = importlib.util.spec_from_file_location("_quota_example_fixture", _SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(provider_quotas, "_FETCHERS", {})
    monkeypatch.delenv("EXAMPLE_QUOTA_BASE_URL", raising=False)


@pytest.fixture
def start_server(fake_provider):
    servers = []

    def start(handler=None, *, quota=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler or fake_provider.FakeHandler)
        server.daemon_threads = True
        if quota is not None:
            server.quota_state = quota
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _base_url(server):
    return f"http://127.0.0.1:{server.server_port}"


def _request(port, path="/v1/chat/completions", payload=_CHAT, *, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        connection.request(
            "GET" if payload is None else "POST",
            path,
            body=None if payload is None else body,
            headers=headers or {},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _valid_payload():
    reset_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {"used": 2, "limit": 10, "reset_at": reset_at.isoformat()}


@pytest.fixture
def response_server(fake_provider, start_server, monkeypatch):
    def start(body, *, status=200, content_type="application/json", headers=None, delay=0):
        requests = []

        class Handler(fake_provider.FakeHandler):
            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
                self.close_connection = True

        server = start_server(Handler)
        monkeypatch.setenv("EXAMPLE_QUOTA_BASE_URL", _base_url(server))
        return server, requests

    return start


def _assert_error(results, error):
    assert len(results) == 1
    card = results[0]
    assert card.name == "example_quota"
    assert card.display_name == "Example Quota"
    assert card.ok is False
    assert card.error == error
    assert card.usages == []
    assert card.key_configured is False
    assert card.key_masked == "Local simulation (no credentials)"
    assert card.key_ref is None
    assert card.fetched_at.utcoffset() == timedelta(0)


def test_import_does_not_register_or_make_requests(monkeypatch):
    def unexpected_network(*args, **kwargs):
        pytest.fail("import and registration must not make requests")

    monkeypatch.setattr(quota_extension.aiohttp, "ClientSession", unexpected_network)
    importlib.reload(quota_extension)
    assert provider_quotas.registered_quota_fetchers() == []
    quota_extension.register()
    spec = provider_quotas.quota_fetcher("example_quota")
    assert spec.display_name == "Example Quota"
    assert spec.fetch is quota_extension.fetch
    assert provider_quotas.registered_quota_fetchers() == [spec]
    with pytest.raises(ValueError, match="already registered"):
        quota_extension.register()


async def test_default_url_and_http_safety_options(monkeypatch):
    session_class = MagicMock()
    session = MagicMock()
    session_class.return_value.__aenter__.return_value = session
    session.get.side_effect = OSError("sensitive-detail-must-not-escape")
    monkeypatch.setattr(quota_extension.aiohttp, "ClientSession", session_class)
    _assert_error(await quota_extension.fetch(None, None), "unreachable")
    session.get.assert_called_once_with("http://127.0.0.1:18353/usage", allow_redirects=False)
    options = session_class.call_args.kwargs
    assert 0 < options["timeout"].total <= 3
    assert options["trust_env"] is False
    assert isinstance(options["cookie_jar"], quota_extension.aiohttp.DummyCookieJar)


@pytest.mark.parametrize(
    "base_url,error",
    [
        ("", "not_configured"),
        ("  ", "not_configured"),
        ("localhost:18353", "invalid_base_url"),
        ("file:///usage", "invalid_base_url"),
        ("http://user:sensitive-detail@localhost", "invalid_base_url"),
        ("http://localhost/?token=sensitive-detail", "invalid_base_url"),
        ("http://localhost/#sensitive-detail", "invalid_base_url"),
        ("http://localhost/v1", "invalid_base_url"),
        ("http://localhost:invalid", "invalid_base_url"),
        ("http://localhost:65536", "invalid_base_url"),
        ("http://[invalid", "invalid_base_url"),
        ("http://local\nhost", "invalid_base_url"),
    ],
)
async def test_unconfigured_or_invalid_base_never_makes_requests(monkeypatch, base_url, error):
    session_class = MagicMock(side_effect=AssertionError("must not make a request"))
    monkeypatch.setattr(quota_extension.aiohttp, "ClientSession", session_class)
    monkeypatch.setenv("EXAMPLE_QUOTA_BASE_URL", base_url)
    _assert_error(await quota_extension.fetch(), error)
    session_class.assert_not_called()


@pytest.mark.parametrize("used", [0, 2, 10])
async def test_fetch_and_registered_callback_read_real_fixture(
    fake_provider, start_server, monkeypatch, used
):
    state = fake_provider.QuotaState(limit=10, used=used)
    server = start_server(quota=state)
    monkeypatch.setenv("EXAMPLE_QUOTA_BASE_URL", _base_url(server) + "/")
    quota_extension.register()
    spec = provider_quotas.quota_fetcher("example_quota")
    for results in (
        await quota_extension.fetch(),
        await spec.fetch(None, None),
        await provider_quotas.gather_all(),
    ):
        assert len(results) == 1
        card = results[0]
        assert (card.name, card.display_name) == ("example_quota", "Example Quota")
        assert card.ok is True and card.error is None
        assert card.key_configured is False
        assert card.key_masked == "Local simulation (no credentials)"
        assert card.key_ref is None and card.key_index is None
        assert len(card.usages) == 1
        usage = card.usages[0]
        assert (usage.label, usage.unit, usage.used, usage.limit) == (
            "Daily requests",
            "requests",
            used,
            10,
        )
        assert usage.reset_at.isoformat() == state.snapshot()["reset_at"]
        assert usage.reset_at.utcoffset() == timedelta(0)
        assert card.fetched_at.utcoffset() == timedelta(0)
    assert state.snapshot()["used"] == used


async def test_offset_timestamp_is_normalized_without_credentials(response_server):
    payload = _valid_payload()
    payload["reset_at"] = (
        datetime.fromisoformat(payload["reset_at"])
        .astimezone(timezone(timedelta(hours=8)))
        .isoformat()
    )
    _server, requests = response_server(
        json.dumps(payload).encode(), headers={"Set-Cookie": "simulation=ignored"}
    )
    for _ in range(2):
        result = (await quota_extension.fetch(object(), object()))[0]
        assert result.ok
        assert result.usages[0].reset_at.utcoffset() == timedelta(0)
    assert len(requests) == 2
    for path, headers in requests:
        assert path == "/usage"
        assert "Authorization" not in headers and "Cookie" not in headers


@pytest.mark.parametrize(
    "field,value",
    [
        ("used", -1),
        ("limit", -1),
        ("limit", 0),
        ("used", 11),
        ("used", True),
        ("limit", True),
        ("used", None),
        ("limit", None),
        ("used", "2"),
        ("limit", "10"),
        ("used", 0.5),
        ("limit", 1.5),
        ("used", float("nan")),
        ("limit", float("nan")),
        ("used", float("inf")),
        ("limit", float("inf")),
        ("used", float("-inf")),
        ("limit", 2**53),
        ("limit", 10**400),
        ("reset_at", None),
        ("reset_at", 123),
        ("reset_at", "sensitive-detail"),
        ("reset_at", "2099-01-01T00:00:00"),
        ("reset_at", "2000-01-01T00:00:00Z"),
        ("reset_at", "9999-12-31T00:00:00Z"),
    ],
)
async def test_invalid_usage_never_becomes_a_snapshot(response_server, field, value):
    payload = {**_valid_payload(), field: value}
    response_server(json.dumps(payload).encode())
    _assert_error(await quota_extension.fetch(), "parse_error")


@pytest.mark.parametrize("missing", ["used", "limit", "reset_at"])
async def test_missing_fields_are_errors(response_server, missing):
    payload = _valid_payload()
    del payload[missing]
    response_server(json.dumps(payload).encode())
    _assert_error(await quota_extension.fetch(), "parse_error")


@pytest.mark.parametrize("body", [b"null", b"[]", b"42", b'"sensitive-detail"', b'{"secret":'])
async def test_invalid_json_or_shape_is_an_error(response_server, body):
    response_server(body)
    _assert_error(await quota_extension.fetch(), "parse_error")


async def test_wrong_content_type_is_an_error(response_server):
    response_server(json.dumps(_valid_payload()).encode(), content_type="text/html")
    _assert_error(await quota_extension.fetch(), "parse_error")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
async def test_http_errors_are_sanitized(response_server, status):
    response_server(b"sensitive-detail-must-not-escape", status=status)
    _assert_error(
        await quota_extension.fetch(), "auth_failed" if status in {401, 403} else f"http_{status}"
    )


async def test_redirects_are_not_followed(response_server):
    target, requests = response_server(json.dumps(_valid_payload()).encode())
    response_server(b"", status=302, headers={"Location": f"{_base_url(target)}/usage"})
    _assert_error(await quota_extension.fetch(), "http_302")
    assert requests == []


async def test_timeout_is_bounded_and_structured(response_server, monkeypatch):
    response_server(json.dumps(_valid_payload()).encode(), delay=0.2)
    monkeypatch.setattr(quota_extension, "_TIMEOUT_SECONDS", 0.02)
    _assert_error(await asyncio.wait_for(quota_extension.fetch(), timeout=1), "timeout")


async def test_disconnected_fixture_is_not_zero_quota(fake_provider, start_server, monkeypatch):
    class DisconnectedHandler(fake_provider.FakeHandler):
        def do_GET(self):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)

    server = start_server(DisconnectedHandler)
    monkeypatch.setenv("EXAMPLE_QUOTA_BASE_URL", _base_url(server))
    _assert_error(await quota_extension.fetch(), "unreachable")


async def test_baseline_fixture_has_no_usage_endpoint_or_quota(
    fake_provider, start_server, monkeypatch
):
    server = start_server()
    port = server.server_port
    assert _request(port, "/health", None) == (200, b'{"status":"ok"}')
    assert _request(port, "/v1/models", None) == (
        200,
        b'{"object":"list","data":[{"id":"example-upstream","object":"model"}]}',
    )
    assert _request(port, "/usage", None) == (404, b'{"error":{"message":"Unknown path: /usage"}}')
    for _ in range(4):
        assert _request(port) == (
            200,
            json.dumps(fake_provider.build_completion(_CHAT), separators=(",", ":")).encode(),
        )
    assert _request(port, payload={**_CHAT, "stream": True}) == (
        200,
        b"".join(fake_provider.build_stream_frames(_CHAT)),
    )
    monkeypatch.setenv("EXAMPLE_QUOTA_BASE_URL", _base_url(server))
    _assert_error(await quota_extension.fetch(), "http_404")


async def test_accepted_chat_and_stream_count_once_and_exhaust(
    fake_provider, start_server, monkeypatch
):
    state = fake_provider.QuotaState(limit=3, used=1)
    server = start_server(quota=state)
    port = server.server_port
    for path in ("/health", "/v1/models", "/usage", "/usage"):
        assert _request(port, path, None)[0] == 200
    assert state.snapshot()["used"] == 1
    # A chat probe is an actual simulated call, even with a one-token budget.
    assert _request(port, payload={**_CHAT, "max_tokens": 1})[0] == 200
    assert state.snapshot()["used"] == 2
    assert _request(port, payload={**_CHAT, "stream": True}) == (
        200,
        b"".join(fake_provider.build_stream_frames(_CHAT)),
    )
    assert state.snapshot()["used"] == 3
    for stream in (False, True):
        assert _request(port, payload={**_CHAT, "stream": stream}) == (
            429,
            b'{"error":{"message":"Local simulation daily quota exhausted"}}',
        )
    assert state.snapshot()["used"] == 3
    monkeypatch.setenv("EXAMPLE_QUOTA_BASE_URL", _base_url(server))
    card = (await quota_extension.fetch())[0]
    assert card.ok and card.usages[0].used == card.usages[0].limit == 3


def test_rejected_chat_requests_do_not_consume_quota(fake_provider, start_server):
    handler = type(
        "AuthenticatedFixture",
        (fake_provider.FakeHandler,),
        {"expected_api_key": "local-test", "expected_model": "example-upstream"},
    )
    state = fake_provider.QuotaState(limit=1)
    port = start_server(handler, quota=state).server_port
    auth = {"Authorization": "Bearer local-test"}
    assert _request(port, "/missing")[0] == 404
    assert _request(port, payload=b"invalid", headers=auth)[0] == 400
    assert _request(port, payload=b"[]", headers=auth)[0] == 400
    assert _request(port)[0] == 401
    assert _request(port, payload={"model": "wrong"}, headers=auth)[0] == 400
    assert state.snapshot()["used"] == 0
    assert _request(port, headers=auth)[0] == 200
    assert state.snapshot()["used"] == 1


def test_concurrent_requests_cannot_overspend_or_share_server_state(fake_provider, start_server):
    first = start_server(quota=fake_provider.QuotaState(limit=5, used=1))
    second = start_server(quota=fake_provider.QuotaState(limit=9, used=2))
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: _request(first.server_port)[0], range(16)))
    assert statuses.count(200) == 4
    assert statuses.count(429) == 12
    assert first.quota_state.snapshot()["used"] == 5
    assert json.loads(_request(second.server_port, "/usage", None)[1])["used"] == 2


def test_daily_boundary_resets_on_reads_and_chat_admission(
    fake_provider, start_server, monkeypatch
):
    now = datetime(2026, 9, 10, 23, 59, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(fake_provider, "_utc_now", lambda: now)
    state = fake_provider.QuotaState(limit=1, used=1)
    port = start_server(quota=state).server_port
    assert state.snapshot() == {"used": 1, "limit": 1, "reset_at": "2026-09-11T00:00:00+00:00"}
    assert _request(port)[0] == 429
    now += timedelta(seconds=1)
    assert json.loads(_request(port, "/usage", None)[1]) == {
        "used": 0,
        "limit": 1,
        "reset_at": "2026-09-12T00:00:00+00:00",
    }
    assert _request(port)[0] == 200
    assert state.snapshot()["used"] == 1
    now += timedelta(days=3, hours=1)
    assert _request(port)[0] == 200
    assert state.snapshot() == {"used": 1, "limit": 1, "reset_at": "2026-09-15T00:00:00+00:00"}


@pytest.mark.parametrize(
    "limit,used",
    [(0, 0), (-1, 0), (1, -1), (1, 2), (True, 0), (1, True), (float("inf"), 0), (2**53, 0)],
)
def test_invalid_fixture_state_is_rejected(fake_provider, limit, used):
    with pytest.raises(ValueError):
        fake_provider.QuotaState(limit, used)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--quota-limit", "0"],
        ["--quota-limit", "-1"],
        ["--quota-limit", "nan"],
        ["--quota-limit", "1.5"],
        ["--quota-limit", "1", "--quota-used", "-1"],
        ["--quota-limit", "1", "--quota-used", "2"],
        ["--quota-used", "0"],
    ],
)
def test_invalid_cli_quota_settings_fail_before_listening(arguments):
    result = subprocess.run(
        [sys.executable, str(_SERVER), "--port", "0", *arguments],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "quota" in result.stderr
    assert "listening" not in result.stdout


@pytest.mark.parametrize("used", [None, 1])
def test_cli_quota_mode_is_explicit_and_initial_usage_is_honored(used):
    command = [sys.executable, str(_SERVER), "--port", "0", "--quota-limit", "1"]
    if used is not None:
        command.extend(["--quota-used", str(used)])
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as process:
        try:
            assert select.select([process.stdout], [], [], 5)[0], (
                "fixture did not announce its port"
            )
            banner = process.stdout.readline()
            match = re.fullmatch(
                r"OpenAI-compatible example provider listening on 127\.0\.0\.1:(\d+)\n", banner
            )
            assert match is not None, banner
            port = int(match.group(1))
            status, body = _request(port, "/usage", None)
            assert status == 200
            assert json.loads(body)["used"] == (used or 0)
            assert _request(port)[0] == (429 if used else 200)
            assert _request(port)[0] == 429
        finally:
            process.terminate()
            process.wait(timeout=5)
