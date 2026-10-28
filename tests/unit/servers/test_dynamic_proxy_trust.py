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
