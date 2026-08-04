"""``/health/deep`` must degrade while an upstream refuses the gateway's key.

Regression for a production outage: a local inference proxy answered 401 to
100% of requests for about an hour and ``/health/deep`` reported
``status: "healthy"`` with HTTP 200 the whole time. It degraded only on an open
circuit or availability below 0.9 — the two aggregate signals that the
client-error exemption had already suppressed for that failure — and the health
snapshot exposed nothing else a consumer could have noticed.
"""

from types import SimpleNamespace
from typing import Any

from fastapi import Response

from serving.servers.routers.health import _endpoint_is_degraded, deep_health


class _StubRouter:
    """Router exposing a fixed health snapshot and no routes."""

    def __init__(self, provider_status: dict[str, dict[str, Any]]) -> None:
        self.routes: dict[str, Any] = {}
        self._provider_status = provider_status

    def get_provider_status(self) -> dict[str, dict[str, Any]]:
        return self._provider_status


def _healthy_endpoint(**overrides: Any) -> dict[str, Any]:
    """Return a snapshot entry matching ``EndpointHealthRegistry.snapshot()``."""
    return {
        "availability": 1.0,
        "circuit_state": "closed",
        "last_error_status": None,
        "consecutive_auth_rejections": 0,
        **overrides,
    }


def test_endpoint_is_degraded_covers_each_signal():
    assert _endpoint_is_degraded(_healthy_endpoint()) is False
    assert _endpoint_is_degraded(_healthy_endpoint(circuit_state="open")) is True
    assert _endpoint_is_degraded(_healthy_endpoint(availability=0.5)) is True
    # A single auth rejection is enough: every request to the endpoint is being
    # refused for a reason no caller can affect.
    assert (
        _endpoint_is_degraded(
            _healthy_endpoint(consecutive_auth_rejections=1, last_error_status=401)
        )
        is True
    )
    # A recovered endpoint keeps its diagnostic breadcrumb without degrading.
    assert _endpoint_is_degraded(_healthy_endpoint(last_error_status=401)) is False
    # Snapshots from a router that predates these keys must not degrade either.
    assert _endpoint_is_degraded({"availability": 1.0, "circuit_state": "closed"}) is False


async def test_deep_health_degrades_on_auth_rejections():
    """The exact outage state: closed circuit, undecayed availability, all 401."""
    router = _StubRouter(
        {
            "diffusiongemma:local-8002": _healthy_endpoint(
                availability=0.95,
                circuit_state="closed",
                last_error_status=401,
                consecutive_auth_rejections=2,
            )
        }
    )
    response = Response()

    body = await deep_health(
        response,
        router_exec=router,
        services=SimpleNamespace(routing_manager=None),
        op_store=None,
        log_store=None,
    )

    assert body["status"] == "degraded"
    # Degraded is a body-level signal; the probe contract keeps HTTP 200 so a
    # load balancer does not tear down a gateway that can still serve.
    assert response.status_code == 200
    endpoint = body["providers"]["diffusiongemma:local-8002"]
    assert endpoint["consecutive_auth_rejections"] == 2
    assert endpoint["last_error_status"] == 401


async def test_deep_health_stays_healthy_without_auth_rejections():
    router = _StubRouter({"diffusiongemma:local-8002": _healthy_endpoint()})
    response = Response()

    body = await deep_health(
        response,
        router_exec=router,
        services=SimpleNamespace(routing_manager=None),
        op_store=None,
        log_store=None,
    )

    assert body["status"] == "healthy"
    assert response.status_code == 200
