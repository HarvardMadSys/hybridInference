"""Admin -> Batches -> Model queue: an on-demand host's queue, read from its status address.

The host (for example an HPC on-demand proxy) answers ``GET <status url>`` with
its models and their scheduler jobs. These pin how the gateway reads it: only
when configured, signed with the right key, following no redirect, reporting
every failure in the response instead of raising, and naming this gateway's
models and their waiting batch requests for each served model.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin
from serving.servers.routers.admin import model_queue

if TYPE_CHECKING:
    from collections.abc import Iterator

URL = "/admin/batch/model-queue"
HOST_STATUS: dict[str, Any] = {
    "object": "model_queue",
    "cluster": "Cluster A",
    "scheduler": "slurm",
    "generated_at": 1_800_000_100,
    "refreshed_at": 1_800_000_050,
    "started_at": 1_799_990_000,
    "poll_interval_seconds": 60,
    "policy": {
        "pending_idle_cancel_seconds": 86400,
        "running_idle_stop_seconds": 900,
        "renew_before_seconds": 1200,
        "node_avoid_seconds": 21600,
    },
    "models": [
        {
            "model": "meta-llama/Llama-3.1-8B-Instruct",
            "job_name": "od-llama31-8b",
            "state": "queued",
            "last_request_at": 1_800_000_000,
            "walltime_seconds": 3600,
            "startup_estimate_seconds": 600,
            "partitions": "gpu,gpu_requeue",
            "constraint": "a100|h100",
            "avoided_nodes": ["node-7"],
            "jobs": [
                {
                    "job_id": "1001",
                    "state": "PENDING",
                    "reason": "Priority",
                    "partition": "gpu_requeue",
                    "nodes": None,
                    "submitted_at": 1_799_993_000,
                    "started_at": None,
                    "expected_start_at": 1_800_002_000,
                    "ends_at": None,
                }
            ],
            # A field this gateway does not know is ignored.
            "tunnel_port": 8021,
        },
        {"model": "Qwen/Qwen3-8B", "state": "idle", "jobs": []},
    ],
    "failed_nodes": [
        {
            "node": "node-7",
            "jobs": ["od-llama31-8b"],
            "last_failed_at": 1_799_999_000,
            "avoided_until": 1_800_020_600,
            "avoided_by_all": False,
        }
    ],
}


class _Host:
    """A status address on 127.0.0.1 answering ``status``/``body`` and recording headers."""

    def __init__(self) -> None:
        self.status = 200
        self.body: bytes = json.dumps(HOST_STATUS).encode()
        self.headers: dict[str, str] = {}
        self.location: str | None = None
        self.requests: list[dict[str, str]] = []

    @contextmanager
    def serve(self) -> Iterator[str]:
        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                host.requests.append(dict(self.headers))
                self.send_response(host.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(host.body)))
                if host.location:
                    self.send_header("Location", host.location)
                self.end_headers()
                self.wfile.write(host.body)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()


def _route(model_id: str, base_url: str, provider_model_id: str | None) -> Any:
    config = SimpleNamespace(id=model_id, base_url=base_url, provider_model_id=provider_model_id)
    return SimpleNamespace(adapters=((SimpleNamespace(config=config), 1.0, "primary"),))


def _app(*, routes: dict[str, Any] | None = None, batch_store: Any = None) -> FastAPI:
    app = FastAPI(title="Admin model queue test")
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=SimpleNamespace(routes=routes or {}), batch_store=batch_store
    )
    app.dependency_overrides[verify_admin_access] = lambda: "admin-1"
    app.include_router(admin.router)
    return app


async def _get(app: FastAPI) -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(URL)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    """The gateway's settings as config_value reads them; empty unless a test sets one."""
    values: dict[str, str] = {}
    monkeypatch.setattr(
        model_queue, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


async def test_nothing_is_fetched_until_an_address_is_set(settings):
    body = await _get(_app())

    assert body["configured"] is False
    assert body["error"] is None
    assert body["models"] == []


async def test_the_queue_names_each_models_gateway_models_and_waiting_batch_requests(settings):
    host = _Host()
    batch_store = MagicMock()
    batch_store.model_stats = AsyncMock(
        return_value={
            "llama-3.1-8b": {"queued": 120, "running": 0},
            # Queued under an alias of the same route.
            "llama-8b": {"queued": 5, "running": 1},
            "qwen3-8b": {"queued": 7, "running": 0},
        }
    )
    with host.serve() as base:
        settings["MODEL_QUEUE_STATUS_URL"] = f"{base}/status"
        settings["LOCAL_API_KEY"] = "local-key"
        on_host = _route("llama-3.1-8b", f"{base}/v1", "meta-llama/Llama-3.1-8B-Instruct")
        routes = {
            "llama-3.1-8b": on_host,
            "llama-8b": on_host,
            # The same served name on another host is not this queue's.
            "llama-elsewhere": _route(
                "llama-elsewhere", "https://api.example.com/v1", "meta-llama/Llama-3.1-8B-Instruct"
            ),
            # Served under its own id.
            "Qwen/Qwen3-8B": _route("Qwen/Qwen3-8B", base, None),
        }
        body = await _get(_app(routes=routes, batch_store=batch_store))

    # Signed with LOCAL_API_KEY while the status address has no key of its own.
    assert host.requests[0]["Authorization"] == "Bearer local-key"
    assert body["configured"] is True
    assert body["error"] is None
    assert body["cluster"] == "Cluster A"
    assert body["refreshed_at"] == 1_800_000_050
    assert body["policy"]["running_idle_stop_seconds"] == 900
    llama, qwen = body["models"]
    assert llama["model"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert llama["state"] == "queued"
    assert llama["gateway_models"] == ["llama-3.1-8b"]
    assert (llama["batch_queued"], llama["batch_running"]) == (125, 1)
    assert llama["jobs"][0]["reason"] == "Priority"
    assert llama["jobs"][0]["expected_start_at"] == 1_800_002_000
    assert "tunnel_port" not in llama
    assert qwen["gateway_models"] == ["Qwen/Qwen3-8B"]
    assert qwen["batch_queued"] == 0
    assert body["failed_nodes"][0]["node"] == "node-7"


async def test_the_status_addresses_own_key_wins(settings):
    host = _Host()
    with host.serve() as base:
        settings["MODEL_QUEUE_STATUS_URL"] = f"{base}/status"
        settings["MODEL_QUEUE_STATUS_API_KEY"] = "status-key"
        settings["LOCAL_API_KEY"] = "local-key"
        await _get(_app())

    assert host.requests[0]["Authorization"] == "Bearer status-key"


async def test_without_a_batch_store_the_counts_are_unknown_not_zero(settings):
    host = _Host()
    with host.serve() as base:
        settings["MODEL_QUEUE_STATUS_URL"] = f"{base}/status"
        body = await _get(_app())

    assert host.requests[0].get("Authorization") is None
    assert {m["batch_queued"] for m in body["models"]} == {None}


async def test_a_batch_store_that_fails_leaves_the_queue_shown(settings):
    host = _Host()
    batch_store = MagicMock()
    batch_store.model_stats = AsyncMock(side_effect=RuntimeError("database gone"))
    with host.serve() as base:
        settings["MODEL_QUEUE_STATUS_URL"] = f"{base}/status"
        body = await _get(_app(batch_store=batch_store))

    assert body["error"] is None
    assert len(body["models"]) == 2
    assert {m["batch_queued"] for m in body["models"]} == {None}


@pytest.mark.parametrize(
    ("status", "body", "location", "expected"),
    [
        (401, b"{}", None, "refused the key (HTTP 401)"),
        (403, b"{}", None, "refused the key (HTTP 403)"),
        (502, b"{}", None, "answered HTTP 502"),
        # Not followed: the key would go wherever it points.
        (302, b"", "http://127.0.0.1:9/elsewhere", "answered HTTP 302"),
        (200, b"<html>", None, "something other than JSON"),
        (200, b"[1, 2]", None, "something other than a queue"),
        (200, json.dumps({"models": "none"}).encode(), None, "cannot read"),
    ],
)
async def test_a_host_that_cannot_be_read_is_reported_not_raised(
    settings, status, body, location, expected
):
    host = _Host()
    host.status, host.body, host.location = status, body, location
    with host.serve() as base:
        settings["MODEL_QUEUE_STATUS_URL"] = f"{base}/status"
        result = await _get(_app())

    assert result["configured"] is True
    assert expected in result["error"]
    assert result["models"] == []
    assert len(host.requests) == 1


async def test_an_oversized_status_is_refused(settings, monkeypatch):
    monkeypatch.setattr(model_queue, "_MAX_STATUS_BYTES", 64)
    host = _Host()
    with host.serve() as base:
        settings["MODEL_QUEUE_STATUS_URL"] = f"{base}/status"
        result = await _get(_app())

    assert "more than" in result["error"]


async def test_an_unreachable_host_is_reported(settings):
    # Port 9 (discard) on loopback: nothing listens there in the test container.
    settings["MODEL_QUEUE_STATUS_URL"] = "http://127.0.0.1:9/status"
    result = await _get(_app())

    assert "could not be reached" in result["error"]


@pytest.mark.parametrize("url", ["ftp://host/status", "not a url", "file:///etc/passwd"])
async def test_only_an_http_address_is_fetched(settings, url):
    settings["MODEL_QUEUE_STATUS_URL"] = url
    result = await _get(_app())

    assert result["error"] == "MODEL_QUEUE_STATUS_URL is not an http or https address."


def test_the_settings_are_registered():
    from serving.config.app_config_registry import static_entry

    url = static_entry("MODEL_QUEUE_STATUS_URL")
    key = static_entry("MODEL_QUEUE_STATUS_API_KEY")
    # Read live: a change applies to the next page load.
    assert url is not None and url.category == "providers" and not url.secret
    assert not url.restart_required
    assert key is not None and key.secret and key.fallback_key == "LOCAL_API_KEY"
    assert not key.restart_required
