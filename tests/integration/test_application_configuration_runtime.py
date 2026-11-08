"""Two real gateway processes: empty setup, Admin configuration, streaming and convergence."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]
ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.timeout(90)
async def test_database_configuration_across_real_processes(tmp_path):
    held = threading.Event()
    release = threading.Event()
    revocation_probes = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = body["model"]
            if body["messages"][-1]["content"] in ("disabled-probe", "deleted-probe"):
                revocation_probes.append(model)
            if body["messages"][-1]["content"] == "hold":
                held.set()
                release.wait(30)
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunk = {
                    "id": "local-smoke",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": model}, "finish_reason": None}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "id": "local-smoke",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": model},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 2,
                                "completion_tokens": 2,
                                "total_tokens": 4,
                            },
                        }
                    ).encode()
                )

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    processes = []
    logs = []
    ports = [free_port(), free_port()]
    async with mig.scratch_database() as database:
        try:
            await mig.upgrade(database)
            env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
            env.update(
                {
                    "PYTHONPATH": str(ROOT / "apps/backend"),
                    **postgres.target().app_env(),
                    "DB_NAME": database,
                    "DB_ENABLED": "true",
                    "USER_AUTH_ENABLED": "true",
                    "SIGNUP_ENABLED": "false",
                    "SIGNUP_REQUIRE_EMAIL_VERIFICATION": "false",
                    "SIGNUP_ADMIN_NOTIFY_ENABLED": "false",
                    "COOKIE_SECURE": "false",
                }
            )

            def start(index, previous_root=None):
                log = (tmp_path / f"gateway-{index}-{len(logs)}.log").open("wb")
                logs.append(log)
                # Accelerate the existing, injectable poll interval for this
                # acceptance test; production retains its ten-second default.
                code = (
                    "from serving.config import routing_sync; "
                    "from functools import partial; "
                    "routing_sync.RoutingSynchronizer=partial(routing_sync.RoutingSynchronizer,interval=0.1); "
                    "import uvicorn; "
                    f"uvicorn.run('serving.servers.app:app',host='127.0.0.1',port={ports[index]},proxy_headers=False)"
                )
                process_env = env
                if previous_root is not None:
                    previous = Path(previous_root).resolve()
                    process_env = {**env, "PYTHONPATH": str(previous / "apps/backend")}
                    code = (
                        "import serving.servers.bootstrap as bootstrap; "
                        f"assert bootstrap.__file__.startswith({str(previous)!r}); "
                        "import uvicorn; "
                        f"uvicorn.run('serving.servers.app:app',host='127.0.0.1',port={ports[index]},proxy_headers=False)"
                    )
                return subprocess.Popen(
                    [sys.executable, "-c", code], cwd=ROOT, env=process_env, stdout=log, stderr=log
                )

            async def ready(client):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        if (await client.get("/health/ready")).status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                raise AssertionError("isolated gateway did not become ready")

            processes.extend(start(index) for index in range(2))
            async with (
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}", timeout=20) as first,
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{ports[1]}", timeout=20) as second,
            ):
                await asyncio.gather(ready(first), ready(second))
                assert (await first.get("/v1/models")).json()["data"] == []
                assert (await first.get("/site-config")).json()["configuration"][
                    "incomplete"
                ] is True
                async with mig.connected(database) as conn:
                    code = await conn.fetchval(
                        "SELECT value FROM site_settings WHERE key='setup_code'"
                    )
                password = "Local-runtime-acceptance-password-42"
                setup = await first.post(
                    "/auth/setup/admin",
                    json={"setup_code": code, "login_name": "local-operator", "password": password},
                )
                assert setup.status_code == 200, setup.text
                auth = {"Authorization": "Bearer " + setup.json()["access_token"]}
                base_url = f"http://127.0.0.1:{upstream.server_port}/v1"
                catalog = {
                    "models": [
                        {
                            "id": strategy + "-chat",
                            "name": strategy,
                            "provider": "openai_compat",
                            "provider_model_id": "before",
                            "base_url": base_url,
                            "api_key": "${LOCAL_UPSTREAM_KEY}",
                            "required_role": "admin",
                            "router": strategy,
                            **(
                                {
                                    "router_params": {
                                        "budget_alpha": 0,
                                        "routewise_probe_enabled": False,
                                    }
                                }
                                if strategy == "routewise"
                                else {}
                            ),
                            "route": [
                                {"kind": "openai_compat", "provider_type": "on_demand", "weight": 1}
                            ],
                        }
                        for strategy in ("fixed", "routewise")
                    ]
                }
                saved = await first.patch(
                    "/admin/config",
                    headers=auth,
                    json={
                        "values": {
                            "MODEL_CATALOG": json.dumps(catalog),
                            "LOCAL_UPSTREAM_KEY": "local-upstream-test-key",
                        },
                        "secrets": {"LOCAL_UPSTREAM_KEY": True},
                        "password": password,
                    },
                )
                assert saved.status_code == 200, saved.text
                assert "MODEL_CATALOG" in saved.json()["pending_restart"]
                # Apply the explicitly marked catalog restart in both workers.
                for process in processes:
                    process.terminate()
                for process in processes:
                    await asyncio.to_thread(process.wait, 10)
                processes[:] = [start(index) for index in range(2)]
                await asyncio.gather(ready(first), ready(second))
                key = await first.post("/user/api-keys", headers=auth, json={})
                assert key.status_code == 201, key.text
                inference_auth = {"Authorization": "Bearer " + key.json()["api_key"]}
                provider_key = await first.post(
                    "/admin/provider-keys",
                    headers=auth,
                    json={
                        "provider": "openai_compat",
                        "api_key": "runtime-provider-test-key",
                        "label": "local runtime",
                    },
                )
                assert provider_key.status_code == 201, provider_key.text
                provider_key_id = provider_key.json()["key"]["id"]
                created = await first.post(
                    "/admin/routing/provider-route-models",
                    headers=auth,
                    json={
                        "model_id": "runtime-chat",
                        "strategy": "routewise",
                        "required_role": "admin",
                        "route_type": "on_demand",
                        "upstream_provider": "openai_compat",
                        "base_url": base_url,
                        "provider_model_id": "before",
                        "api_key_id": provider_key_id,
                        "pricing": {"prompt": "0", "completion": "0"},
                        "capabilities": {
                            "context_length": 8192,
                            "max_output_length": 4096,
                            "input_modalities": ["text"],
                            "supports_tools": False,
                        },
                    },
                )
                assert created.status_code == 200, created.text
                deadline = time.monotonic() + 10
                while True:
                    observed = await second.post(
                        "/v1/chat/completions",
                        headers=inference_auth,
                        json={
                            "model": "runtime-chat",
                            "messages": [{"role": "user", "content": "ready"}],
                            "max_tokens": 4,
                        },
                    )
                    if observed.status_code == 200:
                        break
                    assert time.monotonic() < deadline, observed.text
                    await asyncio.sleep(0.1)
                for strategy in ("fixed", "routewise", "runtime"):
                    model = strategy + "-chat"
                    held.clear()
                    release.clear()
                    pending = asyncio.create_task(
                        second.post(
                            "/v1/chat/completions",
                            headers=inference_auth,
                            json={
                                "model": model,
                                "messages": [{"role": "user", "content": "hold"}],
                                "stream": True,
                                "max_tokens": 4,
                            },
                        )
                    )
                    try:
                        assert await asyncio.to_thread(held.wait, 10), (
                            "inference did not reach the local provider"
                        )
                        listed = await first.get(
                            f"/admin/routing/provider-routes/{model}", headers=auth
                        )
                        route_id = listed.json()["routes"][0]["route_id"]
                        if strategy == "runtime":
                            edited = await first.patch(
                                f"/admin/routing/provider-route-candidates/{model}/{route_id}",
                                headers=auth,
                                json={
                                    "upstream_provider": "openai_compat",
                                    "base_url": base_url,
                                    "provider_model_id": "after",
                                    "api_key_id": provider_key_id,
                                },
                            )
                        else:
                            edited = await first.put(
                                f"/admin/routing/provider-routes/{model}/{route_id}",
                                headers=auth,
                                json={
                                    "upstream_provider": "openai_compat",
                                    "base_url": base_url,
                                    "provider_model_id": "after",
                                },
                            )
                        assert edited.status_code == 200, edited.text
                        deadline = time.monotonic() + 10
                        while True:
                            response = await second.post(
                                "/v1/chat/completions",
                                headers=inference_auth,
                                json={
                                    "model": model,
                                    "messages": [{"role": "user", "content": "new"}],
                                    "max_tokens": 4,
                                },
                            )
                            if (
                                response.status_code == 200
                                and response.json()["choices"][0]["message"]["content"] == "after"
                            ):
                                break
                            assert time.monotonic() < deadline, response.text
                            await asyncio.sleep(0.1)
                    finally:
                        release.set()
                    streamed = await pending
                    assert streamed.status_code == 200 and '"content": "before"' in streamed.text
                ordinary = await first.get("/admin/config/backup", headers=auth)
                assert "local-upstream-test-key" not in ordinary.text
                full = await first.post(
                    "/admin/config/backup",
                    headers=auth,
                    json={"include_secrets": True, "password": password},
                )
                assert full.status_code == 200, full.text
                assert any(
                    row["key"] == "MODEL_CATALOG" and row["value"]
                    for row in full.json()["app_config"]
                )
                preview = await first.post(
                    "/admin/config/restore/preview", headers=auth, json=full.json()
                )
                assert preview.status_code == 200, preview.text
                restored = await first.post(
                    "/admin/config/restore",
                    headers=auth,
                    json={
                        **full.json(),
                        "password": password,
                        "confirm_security_changes": preview.json()["security_changes"],
                    },
                )
                assert restored.status_code == 200, restored.text

                async def runtime_response(message):
                    return await second.post(
                        "/v1/chat/completions",
                        headers=inference_auth,
                        json={
                            "model": "runtime-chat",
                            "messages": [{"role": "user", "content": message}],
                            "max_tokens": 4,
                        },
                    )

                async def await_runtime_availability(message, *, available):
                    deadline = time.monotonic() + 10
                    while True:
                        response = await runtime_response(message)
                        if (response.status_code == 200) == available:
                            return
                        assert time.monotonic() < deadline, response.text
                        await asyncio.sleep(0.1)

                # Revocation on one worker must reach the other worker's
                # existing pinned adapter; an acknowledged refresh cannot leave
                # a deleted/disabled credential capable of upstream I/O.
                disabled = await first.post(
                    f"/admin/provider-keys/{provider_key_id}/disable", headers=auth
                )
                assert disabled.status_code == 200, disabled.text
                await await_runtime_availability("disabled-probe", available=False)
                before = len(revocation_probes)
                assert (await runtime_response("disabled-probe")).status_code != 200
                assert len(revocation_probes) == before
                enabled = await first.post(
                    f"/admin/provider-keys/{provider_key_id}/enable", headers=auth
                )
                assert enabled.status_code == 200, enabled.text
                await await_runtime_availability("reenabled-probe", available=True)
                deleted = await first.delete(
                    f"/admin/provider-keys/{provider_key_id}", headers=auth
                )
                assert deleted.status_code == 200, deleted.text
                await await_runtime_availability("deleted-probe", available=False)
                before = len(revocation_probes)
                assert (await runtime_response("deleted-probe")).status_code != 200
                assert len(revocation_probes) == before
                previous_root = os.environ.get("PREVIOUS_GATEWAY_SOURCE")
                if previous_root:
                    processes[1].terminate()
                    await asyncio.to_thread(processes[1].wait, 10)
                    processes[1] = start(1, previous_root=previous_root)
                    await ready(second)
                    for model in ("fixed-chat", "routewise-chat"):
                        response = await second.post(
                            "/v1/chat/completions",
                            headers=inference_auth,
                            json={
                                "model": model,
                                "messages": [{"role": "user", "content": "old-image"}],
                                "max_tokens": 4,
                            },
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()["choices"][0]["message"]["content"] == "after"

        finally:
            release.set()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(process.wait, 10)
            for stream in logs:
                stream.close()
            upstream.shutdown()
            upstream.server_close()
