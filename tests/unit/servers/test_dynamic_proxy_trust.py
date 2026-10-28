"""Request-boundary tests for dynamically discovered proxy identities."""

from __future__ import annotations

import ipaddress
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from serving.config.settings import Settings
from serving.servers.middleware.proxy_trust import KubernetesProxyTrustMiddleware
from serving.utils.request_ip import ClientIpInfo, get_client_ip_info, preset_client_ip_info

if TYPE_CHECKING:
    from starlette.requests import Request


def _settings(**overrides):
    # Synthetic public-looking peers exercise the socket-fallback regression;
    # every transport below is mocked, so these are not deployment addresses.
    return Settings(
        **{
            "trust_proxy_headers": True,
            "trust_kubernetes_proxy_discovery": True,
            "kubernetes_proxy_namespace": "inference-test",
            "kubernetes_proxy_service_name": "frontend",
            "kubernetes_proxy_service_uid": "service-one",
            "kubernetes_proxy_workload_uid": "workload-one",
            "trusted_proxies": "11.0.0.21/32",
            **overrides,
        }
    )


def _snapshot(*ips, expires_at=None):
    return SimpleNamespace(
        networks=tuple(ipaddress.ip_network(ip) for ip in ips),
        expires_at=expires_at if expires_at is not None else time.monotonic() + 1,
    )


class _Discovery:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.closed = False

    async def get_snapshot(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self):
        self.closed = True


def _app(monkeypatch, settings, provider, endpoint=None):
    monkeypatch.setattr("serving.servers.middleware.proxy_trust.get_settings", lambda: settings)
    monkeypatch.setattr("serving.utils.request_ip.get_settings", lambda: settings)

    async def identity(request: Request):
        info = get_client_ip_info(request)
        return JSONResponse(
            {"ip": info.client_ip, "source": info.source, "resolved": info.resolved}
        )

    app = Starlette(routes=[Route("/identity", endpoint or identity, methods=["GET", "POST"])])
    return KubernetesProxyTrustMiddleware(app, discovery_factory=lambda config: provider)


async def _request(app, peer="11.0.0.21"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 12345)), base_url="http://gateway"
    ) as client:
        return await client.get("/identity", headers={"x-forwarded-for": "6.6.6.6, 8.8.8.8"})


@pytest.mark.asyncio
async def test_replacement_uses_new_pod_and_retires_old_address(monkeypatch):
    discovery = _Discovery(_snapshot("11.0.0.21/32"))
    app = _app(monkeypatch, _settings(), discovery)
    assert (await _request(app)).json() == {
        "ip": "8.8.8.8",
        "source": "x-forwarded-for",
        "resolved": True,
    }

    discovery.result = _snapshot("11.0.0.22/32")
    assert (await _request(app, "11.0.0.22")).json()["ip"] == "8.8.8.8"
    retired = (await _request(app, "11.0.0.21")).json()
    assert retired["ip"] == "unknown"
    assert retired["resolved"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("must not log this"), "expired", "empty"])
async def test_discovery_failure_never_reuses_static_or_previous_trust(
    monkeypatch, caplog, failure
):
    discovery = _Discovery(_snapshot("11.0.0.21/32"))
    app = _app(monkeypatch, _settings(), discovery)
    assert (await _request(app)).json()["ip"] == "8.8.8.8"
    if failure == "expired":
        discovery.result = _snapshot("11.0.0.21/32", expires_at=time.monotonic() - 1)
    elif failure == "empty":
        discovery.result = _snapshot()
    else:
        discovery.result = failure
    body = (await _request(app)).json()
    assert body["ip"] == "unknown"
    assert body["resolved"] is False
    assert "must not log this" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dynamic,master,expected", [(False, True, "8.8.8.8"), (True, False, "unknown")]
)
async def test_legacy_and_master_switch_do_not_contact_control_plane(
    monkeypatch, dynamic, master, expected
):
    discovery = _Discovery(RuntimeError("discovery must not run"))
    app = _app(
        monkeypatch,
        _settings(trust_kubernetes_proxy_discovery=dynamic, trust_proxy_headers=master),
        discovery,
    )
    assert (await _request(app)).json()["ip"] == expected
    assert discovery.calls == 0


@pytest.mark.asyncio
async def test_stream_keeps_admitted_identity_after_snapshot_changes(monkeypatch):
    discovery = _Discovery(_snapshot("11.0.0.21/32"))

    async def stream(request: Request):
        admitted = get_client_ip_info(request)

        async def chunks():
            yield admitted.client_ip + "\n"
            request.state._hybrid_inference_dynamic_proxy_networks = ()
            discovery.result = _snapshot()
            assert get_client_ip_info(request) is admitted
            yield get_client_ip_info(request).client_ip + "\n"

        return StreamingResponse(chunks(), media_type="text/plain")

    app = _app(monkeypatch, _settings(), discovery, endpoint=stream)
    response = await _request(app)
    assert response.text == "8.8.8.8\n8.8.8.8\n"


def test_batch_handler_request_preserves_internal_identity_in_dynamic_mode(monkeypatch, caplog):
    from serving.batch.dispatch import _request as batch_request

    settings = _settings()
    monkeypatch.setattr("serving.utils.request_ip.get_settings", lambda: settings)
    # Batch dispatch calls the handler directly with this Request, without
    # entering the network ASGI middleware. Its provenance is deliberately
    # unknown, but does not indicate failed proxy discovery.
    request = batch_request(None, b"{}", "batch-test", "/v1/chat/completions", None)
    info = get_client_ip_info(request)

    assert request.client is None
    assert info.source == "batch"
    assert info.client_ip == info.peer_ip == "unknown"
    assert info.resolved is False
    assert info.trusted_proxy_headers is False
    assert get_client_ip_info(request) is info
    assert "client_ip_resolution_unresolved" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preset",
    [
        ClientIpInfo("6.6.6.6", "6.6.6.6", "socket", False, True),
        ClientIpInfo("unknown", "unknown", "batch", False, False),
        {"client_ip": "6.6.6.6", "resolved": True},
    ],
)
async def test_network_admission_replaces_any_preexisting_identity(monkeypatch, preset):
    discovery = _Discovery(_snapshot("11.0.0.21/32"))
    app = _app(monkeypatch, _settings(), discovery)

    async def seeded_scope(scope, receive, send):
        # Internal code can seed scope state; HTTP headers cannot. A network
        # admission must still derive identity from its fresh proxy snapshot,
        # even if a caller left a typed internal identity or malformed value.
        state = scope.setdefault("state", {})
        if isinstance(preset, ClientIpInfo):
            preset_client_ip_info(state, preset)
        else:
            state["_hybrid_inference_client_ip_info"] = preset
        await app(scope, receive, send)

    response = await _request(seeded_scope)
    assert response.json() == {"ip": "8.8.8.8", "source": "x-forwarded-for", "resolved": True}
    assert discovery.calls == 1


@pytest.mark.asyncio
async def test_shutdown_releases_client(monkeypatch):
    discovery = _Discovery(_snapshot("11.0.0.21/32"))
    app = _app(monkeypatch, _settings(), discovery)
    await _request(app)
    await app.aclose()
    assert discovery.closed


@pytest.mark.asyncio
async def test_liveness_does_not_wait_for_discovery(monkeypatch):
    settings = _settings(kubernetes_proxy_max_age_seconds=5)
    monkeypatch.setattr("serving.servers.middleware.proxy_trust.get_settings", lambda: settings)
    monkeypatch.setattr("serving.utils.request_ip.get_settings", lambda: settings)
    discovery = _Discovery(RuntimeError("control plane unavailable"))
    inner = Starlette(routes=[Route("/health", lambda request: JSONResponse({"status": "ok"}))])
    app = KubernetesProxyTrustMiddleware(inner, discovery_factory=lambda config: discovery)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.get("/health")).status_code == 200
    assert discovery.calls == 0


def test_application_installs_discovery_before_request_logging():
    from serving.servers.app import create_app
    from serving.servers.middleware.request_log import RequestLogMiddleware

    middleware = [entry.cls for entry in create_app().user_middleware]
    assert middleware.index(KubernetesProxyTrustMiddleware) < middleware.index(RequestLogMiddleware)


@pytest.mark.asyncio
async def test_readiness_requires_fresh_membership_without_requiring_proxy_pod_ready(monkeypatch):
    from fastapi import FastAPI

    from serving.servers.routers import health

    settings = _settings()
    monkeypatch.setattr("serving.servers.middleware.proxy_trust.get_settings", lambda: settings)
    monkeypatch.setattr("serving.utils.request_ip.get_settings", lambda: settings)
    monkeypatch.setattr(health, "get_settings", lambda: settings)
    monkeypatch.setattr(
        health,
        "_test_store_health",
        AsyncMock(
            return_value={
                "all_healthy": True,
                "database_configured": False,
                "operational_store": None,
                "log_store": None,
            }
        ),
    )
    inner = FastAPI()
    inner.include_router(health.router)
    inner.dependency_overrides[health.get_operational_store] = lambda: None
    inner.dependency_overrides[health.get_log_store] = lambda: None
    discovery = _Discovery(_snapshot())
    app = KubernetesProxyTrustMiddleware(inner, discovery_factory=lambda config: discovery)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503
        discovery.result = _snapshot("11.0.0.21/32")
        assert (await client.get("/health/ready")).status_code == 200
        discovery.result = RuntimeError("API unavailable")
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "proxy_discovery_unavailable"


def _readiness_app(monkeypatch, settings, discovery, stores_healthy, **middleware):
    from fastapi import FastAPI

    from serving.servers.routers import health

    monkeypatch.setattr("serving.servers.middleware.proxy_trust.get_settings", lambda: settings)
    monkeypatch.setattr("serving.utils.request_ip.get_settings", lambda: settings)
    monkeypatch.setattr(health, "get_settings", lambda: settings)
    store_check = AsyncMock(
        return_value={
            "all_healthy": stores_healthy,
            "database_configured": True,
            "operational_store": {"status": "ok" if stores_healthy else "error"},
            "log_store": {"status": "ok"},
        }
    )
    monkeypatch.setattr(health, "_test_store_health", store_check)
    inner = FastAPI()
    inner.include_router(health.router)
    inner.dependency_overrides[health.get_operational_store] = lambda: None
    inner.dependency_overrides[health.get_log_store] = lambda: None

    async def identity(request: Request):
        return JSONResponse({"ip": get_client_ip_info(request).client_ip})

    inner.add_route("/identity", identity)
    app = KubernetesProxyTrustMiddleware(
        inner, discovery_factory=lambda config: discovery, **middleware
    )
    return app, store_check


@pytest.mark.asyncio
@pytest.mark.parametrize("stores_healthy", [True, False], ids=["stores-ok", "stores-down"])
@pytest.mark.parametrize("membership", ["present", "empty", "failure"])
@pytest.mark.parametrize(
    ("dynamic", "master"),
    [(False, False), (False, True), (True, False), (True, True)],
    ids=["static-master-off", "static-master-on", "dynamic-master-off", "dynamic-master-on"],
)
async def test_readiness_requires_membership_only_when_discovery_and_master_are_on(
    monkeypatch, dynamic, master, membership, stores_healthy
):
    discovery = _Discovery(
        {
            "present": _snapshot("11.0.0.21/32"),
            "empty": _snapshot(),
            "failure": RuntimeError("API unavailable"),
        }[membership]
    )
    settings = _settings(trust_kubernetes_proxy_discovery=dynamic, trust_proxy_headers=master)
    app, store_check = _readiness_app(monkeypatch, settings, discovery, stores_healthy)
    required = dynamic and master
    membership_ok = not required or membership == "present"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("11.0.0.21", 12345)),
        base_url="http://gateway",
    ) as client:
        ready = await client.get("/health/ready")
        identity = await client.get("/identity", headers={"x-forwarded-for": "6.6.6.6, 8.8.8.8"})

    if not membership_ok:
        assert ready.status_code == 503
        assert ready.json()["reason"] == "proxy_discovery_unavailable"
    elif stores_healthy:
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
    else:
        # The store checks stay in force whatever the discovery switches say.
        assert ready.status_code == 503
        assert ready.json()["reason"] == "store_degraded"
    assert store_check.await_count == (1 if membership_ok else 0)
    # With the master switch off there is no discovery I/O at all.
    assert discovery.calls == (2 if required else 0)
    expected_ip = {
        (False, False): "11.0.0.21",  # legacy: the routable socket peer
        (False, True): "8.8.8.8",  # legacy: the static trusted proxy
        (True, False): "unknown",  # dynamic + kill switch: no fallback at all
        (True, True): "8.8.8.8" if membership == "present" else "unknown",
    }[(dynamic, master)]
    assert identity.json()["ip"] == expected_ip


class _FakeExc(Exception):
    pass


@pytest.mark.asyncio
async def test_transition_warning_carries_only_a_fixed_reason(monkeypatch, caplog):
    import logging

    from serving.utils.kubernetes_proxy_trust import KubernetesProxyTrustError
    from serving.utils.logging import JsonFormatter, PlainFormatter

    secret = "Bearer synthetic-secret https://192.0.2.99/api/v1/namespaces/x?raw=1"
    discovery = _Discovery(KubernetesProxyTrustError("api_status_403"))
    app = _app(monkeypatch, _settings(), discovery)
    caplog.set_level(logging.INFO, logger="serving.servers.middleware.proxy_trust")

    def warnings():
        return [
            r for r in caplog.records if r.getMessage() == "kubernetes_proxy_discovery_unavailable"
        ]

    await _request(app)
    await _request(app)
    assert [r.reason for r in warnings()] == ["api_status_403"]  # transition only

    discovery.result = _snapshot("11.0.0.21/32")
    await _request(app)
    assert "kubernetes_proxy_discovery_recovered" in caplog.text

    for failure, reason in (
        (RuntimeError(secret), "discovery_unavailable"),
        (KubernetesProxyTrustError(secret), "discovery_unavailable"),
        (_FakeExc(secret), "discovery_unavailable"),
        (httpx.ConnectError(secret), "api_transport_error"),
        (TimeoutError(secret), "refresh_timeout"),
        (_snapshot("11.0.0.21/32", expires_at=time.monotonic() - 1), "snapshot_expired"),
    ):
        caplog.clear()
        discovery.result = failure
        await _request(app)
        (record,) = warnings()
        assert record.reason == reason
        for formatter in (JsonFormatter(), PlainFormatter()):
            line = formatter.format(record)
            assert reason in line
            assert "synthetic-secret" not in line and "192.0.2.99" not in line
        discovery.result = _snapshot("11.0.0.21/32")
        await _request(app)
    assert "synthetic-secret" not in caplog.text


class _StepClock:
    """A monotonic stand-in that only moves when a test advances it."""

    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def _logged_app(monkeypatch, settings, discovery, clock=None):
    from serving.servers.middleware.request_log import RequestLogMiddleware

    monkeypatch.setattr("serving.servers.middleware.proxy_trust.get_settings", lambda: settings)
    monkeypatch.setattr("serving.utils.request_ip.get_settings", lambda: settings)

    async def ok(request: Request):
        return JSONResponse({"ip": get_client_ip_info(request).client_ip})

    inner = RequestLogMiddleware(Starlette(routes=[Route("/identity", ok), Route("/health", ok)]))
    extra = {} if clock is None else {"clock": clock}
    return KubernetesProxyTrustMiddleware(
        inner, discovery_factory=lambda config: discovery, **extra
    )


def _request_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "http_request"]


@pytest.mark.asyncio
async def test_request_log_reports_discovery_stage_time_from_injected_clock(monkeypatch, caplog):
    import json
    import logging

    from serving.utils.logging import JsonFormatter, PlainFormatter

    clock = _StepClock()

    class Timed(_Discovery):
        step = 0.0

        async def get_snapshot(self):
            clock.value += self.step
            return await super().get_snapshot()

    discovery = Timed(_snapshot("11.0.0.21/32"))
    app = _logged_app(monkeypatch, _settings(), discovery, clock)
    caplog.set_level(logging.DEBUG)

    discovery.step = 0.0  # a cache hit costs no stage time
    await _request(app)
    discovery.step = 0.0123  # a refresh or a wait behind one
    await _request(app)
    discovery.step = 1.002  # a failed refresh is reported too
    discovery.result = RuntimeError("timed out")
    await _request(app)

    records = _request_records(caplog)
    assert [r.proxy_discovery_ms for r in records] == [0.0, 12.3, 1002.0]
    # duration_ms keeps its meaning: the stage runs outside the request log.
    assert all(isinstance(r.duration_ms, int) for r in records)
    # Both formatters emit only allowlisted keys; the field must reach the line.
    for record, expected in zip(records, ("0.0", "12.3", "1002.0"), strict=True):
        assert json.loads(JsonFormatter().format(record))["proxy_discovery_ms"] == float(expected)
        assert f"proxy_discovery_ms={expected}" in PlainFormatter().format(record).split()


@pytest.mark.asyncio
async def test_request_log_includes_provider_and_snapshot_lock_waits(monkeypatch, caplog):
    import asyncio
    import logging

    class Serialized(_Discovery):
        def __init__(self, result):
            super().__init__(result)
            self.lock = asyncio.Lock()

        async def get_snapshot(self):
            async with self.lock:  # one refresh at a time, like the real provider
                await asyncio.sleep(0.05)
                return await super().get_snapshot()

    discovery = Serialized(_snapshot("11.0.0.21/32"))
    app = _logged_app(monkeypatch, _settings(), discovery)
    caplog.set_level(logging.DEBUG)

    await asyncio.gather(_request(app), _request(app))
    first, second = sorted(r.proxy_discovery_ms for r in _request_records(caplog))
    assert first >= 50.0
    assert second >= 100.0  # waited for the other request's refresh

    caplog.clear()
    async with app._lock:  # hold the provider lock, e.g. during reconfiguration
        pending = asyncio.ensure_future(_request(app))
        await asyncio.sleep(0.05)
    await pending
    (record,) = _request_records(caplog)
    assert record.proxy_discovery_ms >= 100.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dynamic", "master", "path"),
    [(False, True, "/identity"), (True, False, "/identity"), (True, True, "/health")],
    ids=["disabled", "master-off", "liveness"],
)
async def test_unmeasured_requests_never_inherit_a_stage_time(
    monkeypatch, caplog, dynamic, master, path
):
    import logging

    discovery = _Discovery(_snapshot("11.0.0.21/32"))
    app = _logged_app(
        monkeypatch,
        _settings(trust_kubernetes_proxy_discovery=dynamic, trust_proxy_headers=master),
        discovery,
    )
    caplog.set_level(logging.DEBUG)

    async def seeded(scope, receive, send):
        scope.setdefault("state", {})["_hybrid_inference_proxy_discovery_ms"] = 1234.0
        await app(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seeded, client=("11.0.0.21", 1)),
        base_url="http://gateway",
    ) as client:
        assert (await client.get(path)).status_code == 200
    (record,) = _request_records(caplog)
    assert record.proxy_discovery_ms is None
    assert discovery.calls == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12.5, 12.5),
        (0.0, 0.0),
        (600_000.0, 600_000.0),
        (600_000.5, None),
        (-1.0, None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
        (5, None),
        ("12.5", None),
    ],
)
def test_logged_stage_time_is_a_bounded_number(value, expected):
    from serving.servers.middleware.proxy_trust import _elapsed_ms, proxy_discovery_ms

    request = SimpleNamespace(state=SimpleNamespace(_hybrid_inference_proxy_discovery_ms=value))
    assert proxy_discovery_ms(request) == expected
    assert _elapsed_ms(10.0, 9.0) == 0.0  # a clock that stepped backwards
    assert _elapsed_ms(0.0, float("nan")) == 0.0
    assert _elapsed_ms(0.0, 1e12) == 600_000.0
