#!/usr/bin/env python3
"""Multi-model reverse proxy that lazily starts/stops sglang containers.

The proxy listens on ``LISTEN_PORT`` and routes requests to the correct
sglang backend based on the ``model`` field in the request body.  Each model
has its own Docker container, GPU, idle timer, and health-check lifecycle.

When the first request for a model arrives its container is started; after
``IDLE_TIMEOUT`` seconds of inactivity the container is stopped.  The proxy
stays alive so callers always see an open port.

Usage:
    # Foreground (Ctrl-C to quit):
    python sglang_idle_proxy/sglang_idle_proxy.py

    # Background:
    nohup python sglang_idle_proxy/sglang_idle_proxy.py &

    # Custom settings via environment:
    LISTEN_PORT=9000 IDLE_TIMEOUT=600 python sglang_idle_proxy/sglang_idle_proxy.py

Environment variables
---------------------
LISTEN_PORT    : Port the proxy binds to                  (default 8001)
IDLE_TIMEOUT   : Seconds of inactivity before stopping    (default 1200 = 20 min)
HEALTH_TIMEOUT : Max seconds to wait for backend startup  (default 600)
HEALTH_INTERVAL: Seconds between health-check polls       (default 10)
MODELS_CONFIG  : Path to a JSON config file               (see below)

Model configuration
-------------------
Models are defined in a JSON file (default: ``models.json`` next to this
script).  Each key is a model name that clients send in the ``model`` field.
Example::

    {
        "Qwen/Qwen3.6-35B-A3B-FP8": {
            "container": "qwen36-sglang",
            "gpu_index": "1",
            "backend_port": 18001,
            "model_dir": "/scratch/juncheng/models/Qwen3.6-35B-A3B-FP8",
            "served_name": "Qwen/Qwen3.6-35B-A3B-FP8",
            "max_model_len": 135168,
            "mem_fraction": "0.90",
            "tool_call_parser": "qwen3_coder"
        },
        "zai-org/GLM-4.7-Flash": {
            "container": "glm47-sglang",
            "gpu_index": "2",
            "backend_port": 18002,
            "model_dir": "/netscratch/juncheng/models/GLM-4.7-Flash",
            "served_name": "zai-org/GLM-4.7-Flash",
            "max_model_len": 131072,
            "mem_fraction": "0.90",
            "tool_call_parser": "glm47"
        }
    }

``gpu_index`` can be omitted to auto-pick the least-used GPU.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sglang_proxy")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8001"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1200"))
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "600"))
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "10"))
FREEINFERENCE_API_KEY = os.environ.get("FREEINFERENCE_API_KEY", "").strip()

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _SCRIPT_DIR / "models.json"
MODELS_CONFIG = os.environ.get("MODELS_CONFIG", str(DEFAULT_CONFIG_PATH))


def _load_models_config() -> dict[str, dict[str, Any]]:
    p = Path(MODELS_CONFIG)
    if not p.exists():
        log.warning("Models config not found at %s — proxy will have no backends.", p)
        return {}
    with open(p) as f:
        cfg = _json.load(f)
    log.info("Loaded %d model(s) from %s", len(cfg), p)
    for name, mc in cfg.items():
        log.info(
            "  %s → container=%s  gpu=%s  port=%s",
            name,
            mc.get("container"),
            mc.get("gpu_index", "auto"),
            mc.get("backend_port"),
        )
    return cfg


MODELS_CONFIG_DATA = _load_models_config()


def _pick_free_gpu(exclude: set[str] | None = None) -> str:
    """Return the index of the GPU with the lowest memory utilization.

    Prefers GPUs with memory utilization < 20%; falls back to the least-used.
    """
    exclude = exclude or set()
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        log.warning("nvidia-smi not found — defaulting to GPU 0.")
        return "0"
    best_idx = "0"
    best_usage = 1.0
    free_idx = "0"
    free_usage = 1.0
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        idx, used, total = parts[0], float(parts[1]), float(parts[2])
        if idx in exclude:
            continue
        usage = used / total if total > 0 else 1.0
        log.info("GPU %s: %.0f / %.0f MiB (%.0f%%)", idx, used, total, usage * 100)
        if usage < best_usage:
            best_usage = usage
            best_idx = idx
        if usage < 0.20 and usage < free_usage:
            free_usage = usage
            free_idx = idx
    if free_usage < 1.0:
        log.info(
            "Auto-picked GPU %s (%.0f%% mem used — under 20%% threshold).",
            free_idx,
            free_usage * 100,
        )
        return free_idx
    log.warning(
        "No GPU under 20%% memory utilization — falling back to least-used GPU %s (%.0f%% mem used).",
        best_idx,
        best_usage * 100,
    )
    return best_idx


class BackendManager:
    """Manages the lifecycle of a single sglang Docker container."""

    def __init__(self, model_name: str, config: dict[str, Any]) -> None:
        self.model_name = model_name
        self.config = config
        self.container: str = config["container"]
        self.backend_port: int = int(config.get("backend_port", 18001))
        self._lock = threading.Lock()
        self._state: str = "stopped"
        self._last_activity: float = 0.0
        self._watcher_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._start_error: Exception | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def ensure_running(self) -> None:
        should_start = False
        with self._lock:
            if self._state == "ready":
                self._last_activity = time.monotonic()
                return
            if self._state == "starting":
                pass
            else:
                self._state = "starting"
                self._ready_event.clear()
                self._start_error = None
                should_start = True

        if should_start:
            try:
                log.info("[%s] Starting container %s …", self.model_name, self.container)
                self._start_container()
                self._wait_healthy()
                with self._lock:
                    self._state = "ready"
                    self._last_activity = time.monotonic()
                    if self._watcher_thread is None or not self._watcher_thread.is_alive():
                        self._watcher_thread = threading.Thread(
                            target=self._idle_watcher, daemon=True
                        )
                        self._watcher_thread.start()
                log.info("[%s] Container %s is ready.", self.model_name, self.container)
                self._ready_event.set()
            except Exception as exc:
                with self._lock:
                    self._state = "stopped"
                    self._start_error = exc
                self._ready_event.set()
                raise
        else:
            self._ready_event.wait(timeout=HEALTH_TIMEOUT)
            with self._lock:
                if self._state == "ready":
                    self._last_activity = time.monotonic()
                    return
                if self._start_error is not None:
                    raise self._start_error
            raise RuntimeError(
                f"[{self.model_name}] Backend did not become healthy within {HEALTH_TIMEOUT}s"
            )

    def _resolve_gpu(self) -> str:
        used_gpus = set()
        for mgr in _backends.values():
            if mgr is not self and mgr.state == "ready":
                used_gpus.add(str(mgr.config.get("gpu_index", "")))
        log.info(
            "[%s] Auto-selecting GPU (excluding %s)",
            self.model_name,
            sorted(used_gpus) if used_gpus else "none",
        )
        return _pick_free_gpu(exclude=used_gpus)

    def _start_container(self) -> None:
        gpu = self._resolve_gpu()
        subprocess.run(
            ["sudo", "docker", "rm", "-f", self.container],
            check=False,
            capture_output=True,
        )
        cmd = [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            "--gpus",
            f"device={gpu}",
            "--shm-size",
            "16g",
            "-p",
            f"{self.backend_port}:8001",
            "-v",
            f"{self.config['model_dir']}:/model:ro",
            "lmsysorg/sglang:latest",
            "python3",
            "-m",
            "sglang.launch_server",
            "--model-path",
            "/model",
            "--served-model-name",
            self.config.get("served_name", self.model_name),
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
            "--context-length",
            str(self.config.get("max_model_len", 131072)),
            "--mem-fraction-static",
            str(self.config.get("mem_fraction", "0.90")),
            "--tp",
            "1",
        ]
        tcp = self.config.get("tool_call_parser")
        if tcp:
            cmd += ["--tool-call-parser", tcp]
        log.info("Running: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True)

    def _stop_container(self) -> None:
        log.info("[%s] Stopping container %s …", self.model_name, self.container)
        subprocess.run(
            ["sudo", "docker", "rm", "-f", self.container],
            check=False,
            capture_output=True,
        )
        with self._lock:
            self._state = "stopped"
        log.info("[%s] Container %s stopped.", self.model_name, self.container)

    def _wait_healthy(self) -> None:
        url = f"http://localhost:{self.backend_port}/v1/models"
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            try:
                req = Request(url, method="GET")
                with urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                pass
            time.sleep(HEALTH_INTERVAL)
        raise RuntimeError(
            f"[{self.model_name}] Backend did not become healthy within {HEALTH_TIMEOUT}s"
        )

    def _idle_watcher(self) -> None:
        while True:
            time.sleep(min(10, IDLE_TIMEOUT / 2))
            with self._lock:
                if self._state != "ready":
                    if self._state == "stopped":
                        return
                    continue
                idle_for = time.monotonic() - self._last_activity
            if idle_for >= IDLE_TIMEOUT:
                with self._lock:
                    if self._state != "ready":
                        continue
                self._stop_container()
                return


def _copy_stream(resp: Any, wfile: Any) -> None:
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        wfile.write(chunk)
        wfile.flush()


_backends: dict[str, BackendManager] = {}
for _name, _cfg in MODELS_CONFIG_DATA.items():
    _backends[_name] = BackendManager(_name, _cfg)


def _get_backend(
    body: bytes, request_path: str = "", request_method: str = ""
) -> BackendManager | None:
    """Pick the right backend from the ``model`` field in the request body."""
    try:
        model = _json.loads(body).get("model", "")
    except Exception:
        model = ""
    backend = _backends.get(model)
    if backend is not None:
        log.info("[%s] Request: %s %s", model, request_method, request_path)
    else:
        log.info("[unknown model] Request: %s %s  model=%s", request_method, request_path, model)
    return backend


WARMUP_THINKING_SSE = (
    'data: {"id":"warmup","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{"role":"assistant",'
    '"content":"⏳ The model is starting up — this takes about 120 seconds. '
    'Please wait…"},"finish_reason":null}]}\n\n'
)
WARMUP_THINKING_SSE_DONE = (
    'data: {"id":"warmup","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
)


class ProxyHandler(BaseHTTPRequestHandler):
    """Forwards requests to the correct sglang backend based on model name."""

    def _check_api_key(self) -> bool:
        if not FREEINFERENCE_API_KEY:
            return True

        auth = self.headers.get("Authorization", "")
        auth = auth[7:] if auth.startswith("Bearer ") else self.headers.get("X-API-Key", "")

        if auth != FREEINFERENCE_API_KEY:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Missing or invalid API key")
            return False

        return True

    def _proxy(self) -> None:
        if self.path == "/v1/models" and self.command == "GET":
            self._handle_models_list()
            return

        if not self._check_api_key():
            return

        body = self._read_body()
        backend = _get_backend(body, request_path=self.path, request_method=self.command)
        if backend is None:
            self.send_error(404, f"Unknown model. Available: {list(_backends.keys())}")
            return

        backend.touch()
        if backend.state != "ready":
            is_chat = self.command == "POST" and self.path.startswith("/v1/chat/completions")
            is_stream = False
            if is_chat and body:
                with contextlib.suppress(Exception):
                    is_stream = _json.loads(body).get("stream", False)

            if is_chat and is_stream:
                self._handle_warmup_stream(backend)
                return
            try:
                backend.ensure_running()
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            self._forward_with_body(backend, body)
            return

        self._forward_with_body(backend, body)

    def _handle_models_list(self) -> None:
        """Return a static /v1/models response from config (no backend needed)."""
        models = [
            {
                "id": name,
                "object": "model",
                "owned_by": "sglang",
                "status": "loaded" if mgr.state == "ready" else "not_loaded",
            }
            for name, mgr in _backends.items()
        ]
        payload = _json.dumps({"object": "list", "data": models}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send_sse_chunk(self, data: str) -> None:
        self.wfile.write(data.encode())
        self.wfile.flush()

    def _handle_warmup_stream(self, backend: BackendManager) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        self._send_sse_chunk(WARMUP_THINKING_SSE)
        self._send_sse_chunk(WARMUP_THINKING_SSE_DONE)
        self._send_sse_chunk("data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

        def _warm_up() -> None:
            try:
                backend.ensure_running()
            except RuntimeError as exc:
                log.error("[%s] Backend failed to start: %s", backend.model_name, exc)
                return
            log.info("[%s] Backend ready — client should retry.", backend.model_name)

        threading.Thread(target=_warm_up, daemon=True).start()

    def _forward_with_body(self, backend: BackendManager, body: bytes) -> None:
        target = f"http://localhost:{backend.backend_port}{self.path}"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        req = Request(target, data=body if body else None, headers=headers, method=self.command)
        try:
            with urlopen(req, timeout=300) as resp:
                is_streaming = resp.headers.get("Content-type", "").startswith("text/event-stream")
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(key, val)
                self.end_headers()
                if is_streaming:
                    _copy_stream(resp, self.wfile)
                else:
                    self.wfile.write(resp.read())
        except URLError as exc:
            self.send_error(502, f"Backend error: {exc}")
        except Exception as exc:
            self.send_error(500, str(exc))

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        log.info(fmt, *args)


def main() -> None:
    models = list(_backends.keys())
    log.info(
        "sglang idle proxy listening on :%d  (%d models: %s)  (idle timeout %ds)",
        LISTEN_PORT,
        len(models),
        ", ".join(models) if models else "none",
        IDLE_TIMEOUT,
    )
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down proxy …")
        server.server_close()
        for mgr in _backends.values():
            if mgr.state != "stopped":
                mgr._stop_container()


if __name__ == "__main__":
    main()
