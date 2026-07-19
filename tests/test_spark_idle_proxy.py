from __future__ import annotations

import importlib
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Any, ClassVar


MODEL_NAME = "openai/gpt-oss-20b"


@contextmanager
def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers=headers or {}, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _load_proxy(monkeypatch: Any, tmp_path: Path, *, backend_port: int = 18080) -> Any:
    config_path = tmp_path / "models.json"
    config_path.write_text(
        json.dumps(
            {
                MODEL_NAME: {
                    "container": "manual-test-vllm",
                    "gpu_index": "0",
                    "backend_port": backend_port,
                    "model_dir": "/tmp/manual-test-model",
                    "served_name": MODEL_NAME,
                    "max_model_len": 131072,
                    "gpu_memory_utilization": 0.85,
                    "max_num_seqs": 4,
                    "trust_remote_code": True,
                    "enable_auto_tool_choice": True,
                    "tool_call_parser": "openai",
                    "reasoning_parser": "openai_gptoss",
                }
            }
        )
    )
    monkeypatch.setenv("MODELS_CONFIG", str(config_path))
    monkeypatch.setenv("LOCAL_API_KEY", "manual-secret")
    monkeypatch.setenv("HEALTH_TIMEOUT", "0.2")
    monkeypatch.setenv("HEALTH_INTERVAL", "0.01")

    sys.modules.pop("ops.spark_idle_proxy.spark_idle_proxy", None)
    return importlib.import_module("ops.spark_idle_proxy.spark_idle_proxy")


class RecordingBackendHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        payload = json.dumps({"ok": True, "proxied_path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class WarmupBackend:
    model_name = MODEL_NAME

    def __init__(self) -> None:
        self._state = "stopped"
        self.touched = threading.Event()
        self.ensure_running_called = threading.Event()

    @property
    def state(self) -> str:
        return self._state

    def touch(self) -> None:
        self.touched.set()

    def ensure_running(self) -> None:
        self.ensure_running_called.set()


def test_start_container_rejects_model_dir_without_config(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "empty-model"
    model_dir.mkdir()
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "gpu_index": "0",
            "model_dir": str(model_dir),
        },
    )
    run = Mock()
    monkeypatch.setattr(proxy.subprocess, "run", run)

    with pytest.raises(RuntimeError, match=r"model_dir missing config\.json"):
        backend._start_container()

    run.assert_not_called()


def test_missing_huggingface_model_is_downloaded_on_demand(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "downloaded-model"

    def download(**kwargs: Any) -> None:
        assert kwargs == {
            "repo_id": "openai/gpt-oss-20b",
            "revision": None,
            "local_dir": str(model_dir),
            "ignore_patterns": None,
        }
        (model_dir / "config.json").write_text('{"model_type": "gpt_oss"}')

    snapshot_download = Mock(side_effect=download)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "model_dir": str(model_dir),
            "hf_repo": "openai/gpt-oss-20b",
        },
    )

    backend._ensure_model_dir()

    snapshot_download.assert_called_once()
    assert (model_dir / "config.json").is_file()
    assert (model_dir / ".download_complete").is_file()


def test_serve_hf_repo_mounts_cache_and_serves_repo_id(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    hf_cache = tmp_path / "hf-cache"
    (hf_cache / "hub" / "models--openai--gpt-oss-20b").mkdir(parents=True)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "gpu_index": "0",
            "backend_port": 18080,
            "model_dir": str(hf_cache),
            "hf_repo": MODEL_NAME,
            "hf_cache_dir": str(hf_cache),
            "serve_hf_repo": True,
            "served_name": MODEL_NAME,
            "docker_image": "nvcr.io/nvidia/vllm:26.01-py3",
        },
    )
    commands: list[list[str]] = []

    def record_run(command: list[str], **_: Any) -> SimpleNamespace:
        commands.append(command)
        if "inspect" in command:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", record_run)

    backend._start_container()

    launch_command = commands[-1]
    assert f"{hf_cache}:/root/.cache/huggingface" in launch_command
    assert "/model" not in launch_command
    serve_idx = launch_command.index("serve")
    assert launch_command[serve_idx + 1] == MODEL_NAME


def test_vllm_openai_image_uses_entrypoint_args_only(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "installed-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"model_type": "gpt_oss"}')
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "gpu_index": "0",
            "backend_port": 18080,
            "model_dir": str(model_dir),
            "served_name": MODEL_NAME,
            "docker_image": "vllm/vllm-openai:cu130-nightly",
            "max_model_len": 131072,
            "gpu_memory_utilization": 0.85,
            "max_num_seqs": 4,
            "trust_remote_code": True,
            "enable_auto_tool_choice": True,
            "tool_call_parser": "openai",
            "reasoning_parser": "openai_gptoss",
        },
    )
    commands: list[list[str]] = []

    def record_run(command: list[str], **_: Any) -> SimpleNamespace:
        commands.append(command)
        if "inspect" in command:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", record_run)

    backend._start_container()

    launch_command = commands[-1]
    assert "--ipc=host" in launch_command
    assert "vllm/vllm-openai:cu130-nightly" in launch_command
    image_idx = launch_command.index("vllm/vllm-openai:cu130-nightly")
    docker_args = launch_command[image_idx + 1 :]
    assert "vllm" not in docker_args
    assert "serve" not in docker_args
    assert "/model" in docker_args
    assert "--gpu-memory-utilization" in launch_command
    assert "--max-num-seqs" in launch_command
    assert "--trust-remote-code" in launch_command
    assert "--enable-auto-tool-choice" in launch_command
    assert launch_command[-4:] == [
        "--tool-call-parser",
        "openai",
        "--reasoning-parser",
        "openai_gptoss",
    ]


def test_models_endpoint_lists_configured_backends_without_starting_them(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)

    with _serve(proxy.ProxyHandler) as proxy_port:
        status, headers, body = _request(f"http://127.0.0.1:{proxy_port}/v1/models")

    payload = json.loads(body)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert payload == {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "owned_by": "vllm",
                "status": "not_loaded",
            }
        ],
    }


def test_ready_backend_request_is_proxied_to_matching_model(
    monkeypatch: Any, tmp_path: Path
) -> None:
    RecordingBackendHandler.requests = []

    with _serve(RecordingBackendHandler) as backend_port:
        proxy = _load_proxy(monkeypatch, tmp_path, backend_port=backend_port)
        backend = proxy._backends[MODEL_NAME]
        with backend._lock:
            backend._state = "ready"
        monkeypatch.setattr(backend, "_container_running", lambda: True)

        with _serve(proxy.ProxyHandler) as proxy_port:
            status, _, body = _request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                method="POST",
                headers={"Authorization": "Bearer manual-secret"},
                body={
                    "model": MODEL_NAME,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

    assert status == 200
    assert json.loads(body) == {"ok": True, "proxied_path": "/v1/chat/completions"}
    assert len(RecordingBackendHandler.requests) == 1
    assert RecordingBackendHandler.requests[0]["path"] == "/v1/chat/completions"
    assert json.loads(RecordingBackendHandler.requests[0]["body"])["model"] == MODEL_NAME
    assert RecordingBackendHandler.requests[0]["authorization"] == "Bearer manual-secret"


def test_serve_hf_repo_downloads_into_cache_when_missing(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    hf_cache = tmp_path / "hf-cache"

    def download(**kwargs: Any) -> None:
        assert kwargs["cache_dir"] == str(hf_cache)
        slug_dir = hf_cache / "hub" / "models--openai--gpt-oss-20b"
        slug_dir.mkdir(parents=True)

    snapshot_download = Mock(side_effect=download)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "hf_repo": MODEL_NAME,
            "hf_cache_dir": str(hf_cache),
            "serve_hf_repo": True,
        },
    )

    backend._ensure_model_dir()

    snapshot_download.assert_called_once()


def test_failed_startup_removes_container(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "installed-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"model_type": "gpt_oss"}')
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "gpu_index": "0",
            "backend_port": 18080,
            "model_dir": str(model_dir),
        },
    )
    commands: list[list[str]] = []

    def record_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        if "docker" in command and "run" in command:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", record_run)

    with pytest.raises(subprocess.CalledProcessError):
        backend.ensure_running()

    assert backend.state == "stopped"
    assert any("rm" in command and "-f" in command for command in commands)


def test_nvidia_smi_failure_falls_back_to_gpu_zero(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)

    def fail_run(*_: Any, **__: Any) -> SimpleNamespace:
        raise subprocess.CalledProcessError(1, ["nvidia-smi"])

    monkeypatch.setattr(proxy.subprocess, "run", fail_run)
    assert proxy._pick_free_gpu() == "0"


def test_invalid_content_length_returns_400(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)

    with _serve(proxy.ProxyHandler) as proxy_port:
        status, _, body = _request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            method="POST",
            headers={
                "Authorization": "Bearer manual-secret",
                "Content-Length": "not-a-number",
            },
            body={"model": MODEL_NAME, "messages": [{"role": "user", "content": "ping"}]},
        )

    assert status == 400
    assert b"Invalid Content-Length" in body


def test_upstream_http_error_is_forwarded(monkeypatch: Any, tmp_path: Path) -> None:
    class ErrorBackendHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.dumps({"error": "context too long"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    with _serve(ErrorBackendHandler) as backend_port:
        proxy = _load_proxy(monkeypatch, tmp_path, backend_port=backend_port)
        backend = proxy._backends[MODEL_NAME]
        with backend._lock:
            backend._state = "ready"
        monkeypatch.setattr(backend, "_container_running", lambda: True)

        with _serve(proxy.ProxyHandler) as proxy_port:
            status, headers, body = _request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                method="POST",
                headers={"Authorization": "Bearer manual-secret"},
                body={
                    "model": MODEL_NAME,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

    assert status == 400
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"error": "context too long"}


def test_mark_dead_resets_ready_state(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]
    with backend._lock:
        backend._state = "ready"
        backend._current_gpu = "0"

    assert backend.mark_dead("connection refused") is True
    assert backend.state == "stopped"
    assert backend._current_gpu is None
    # Second call is a no-op once already stopped.
    assert backend.mark_dead("again") is False


def test_ensure_running_restarts_when_ready_but_container_dead(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "installed-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"model_type": "gpt_oss"}')
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-vllm",
            "gpu_index": "0",
            "backend_port": 18080,
            "model_dir": str(model_dir),
        },
    )
    with backend._lock:
        backend._state = "ready"

    running_checks = {"n": 0}
    start_calls: list[str] = []

    def fake_running() -> bool:
        running_checks["n"] += 1
        # First check (liveness while "ready") says dead; after restart, healthy.
        return running_checks["n"] > 1

    def fake_start() -> None:
        start_calls.append("start")

    def fake_wait() -> None:
        return

    monkeypatch.setattr(backend, "_container_running", fake_running)
    monkeypatch.setattr(backend, "_start_container", fake_start)
    monkeypatch.setattr(backend, "_wait_healthy", fake_wait)

    backend.ensure_running()

    assert start_calls == ["start"]
    assert backend.state == "ready"


def test_connection_refused_restarts_backend_and_retries(monkeypatch: Any, tmp_path: Path) -> None:
    """Ready-but-dead backend: first proxy fails, restart, second attempt succeeds."""
    RecordingBackendHandler.requests = []

    with _serve(RecordingBackendHandler) as backend_port:
        proxy = _load_proxy(monkeypatch, tmp_path, backend_port=backend_port)
        backend = proxy._backends[MODEL_NAME]
        with backend._lock:
            backend._state = "ready"
        # Liveness says alive so we reach the forward path; then the first
        # urlopen fails as if the process just died.
        monkeypatch.setattr(backend, "_container_running", lambda: True)

        attempts = {"n": 0}
        real_urlopen = proxy.urlopen

        def flaky_urlopen(req: Any, timeout: float | None = None) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                from urllib.error import URLError

                raise URLError(ConnectionRefusedError("Connection refused"))
            return real_urlopen(req, timeout=timeout)

        ensure_calls = {"n": 0}

        def counting_ensure() -> None:
            ensure_calls["n"] += 1
            # After mark_dead the state is stopped; pretend restart is instant.
            with backend._lock:
                backend._state = "ready"

        monkeypatch.setattr(proxy, "urlopen", flaky_urlopen)
        monkeypatch.setattr(backend, "ensure_running", counting_ensure)

        with _serve(proxy.ProxyHandler) as proxy_port:
            status, _, body = _request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                method="POST",
                headers={"Authorization": "Bearer manual-secret"},
                body={
                    "model": MODEL_NAME,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

    assert status == 200
    assert json.loads(body) == {"ok": True, "proxied_path": "/v1/chat/completions"}
    # _proxy calls ensure_running once up front, then again after mark_dead.
    assert ensure_calls["n"] >= 2
    assert attempts["n"] == 2
    assert len(RecordingBackendHandler.requests) == 1


def test_streaming_chat_returns_warmup_sse_while_backend_starts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = WarmupBackend()
    proxy._backends = {MODEL_NAME: backend}

    with _serve(proxy.ProxyHandler) as proxy_port:
        status, headers, body = _request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            method="POST",
            headers={"Authorization": "Bearer manual-secret"},
            body={
                "model": MODEL_NAME,
                "stream": True,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )

    assert status == 200
    assert headers["Content-Type"] == "text/event-stream"
    assert b'"id":"warmup"' in body
    assert b"data: [DONE]" in body
    assert backend.touched.is_set()
    assert backend.ensure_running_called.wait(timeout=2)
