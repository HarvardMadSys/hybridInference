#!/usr/bin/env python3
"""Multi-model reverse proxy that lazily starts/stops vLLM containers on DGX Spark.

The proxy listens on ``LISTEN_PORT`` and routes requests to the correct vLLM
backend based on the ``model`` field in the request body.  Each model has its
own Docker container, GPU assignment, idle timer, and health-check lifecycle.

When the first request for a model arrives its container is started; after
``IDLE_TIMEOUT`` seconds of inactivity the container is stopped.  The proxy
stays alive so callers always see an open port.

Usage:
    python spark_idle_proxy/spark_idle_proxy.py

Environment variables
---------------------
LISTEN_PORT    : Port the proxy binds to                  (default 8002)
IDLE_TIMEOUT   : Seconds of inactivity before stopping    (default 1200 = 20 min)
HEALTH_TIMEOUT : Max seconds to wait for backend startup  (default 900)
HEALTH_INTERVAL: Seconds between health-check polls       (default 10)
MODELS_CONFIG  : Path to a JSON config file               (see models.json)
LOCAL_API_KEY  : Optional API key for inbound auth
HF_TOKEN       : Passed into vLLM containers for gated HF downloads
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("spark_proxy")

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8002"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1200"))
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "900"))
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "10"))
LOCAL_API_KEY = os.environ.get("LOCAL_API_KEY", "freeinference_api").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()

VLLM_CONTAINER_PORT = 8000
DEFAULT_DOCKER_IMAGE = "vllm/vllm-openai:cu130-nightly"

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
    """Return the index of the GPU with the lowest memory utilization."""
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
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        log.warning("nvidia-smi failed (%s) — defaulting to GPU 0.", exc)
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
    """Manages the lifecycle of a single vLLM Docker container."""

    def __init__(self, model_name: str, config: dict[str, Any]) -> None:
        self.model_name = model_name
        self.config = config
        self.container: str = config["container"]
        self.backend_port: int = int(config.get("backend_port", 18003))
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._state: str = "stopped"
        self._last_activity: float = 0.0
        self._active_requests: int = 0
        self._watcher_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._start_error: Exception | None = None
        self._current_gpu: str | None = None

    @property
    def state(self) -> str:
        """Return the current backend lifecycle phase."""
        with self._lock:
            return self._state

    def touch(self) -> None:
        """Record activity to reset the idle timer."""
        with self._lock:
            self._last_activity = time.monotonic()

    def begin_request(self) -> None:
        """Mark an in-flight proxy request so idle shutdown waits for it."""
        with self._lock:
            self._active_requests += 1
            self._last_activity = time.monotonic()

    def end_request(self) -> None:
        """Clear an in-flight proxy request."""
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_activity = time.monotonic()

    def ensure_running(self) -> None:
        """Start the container if needed and block until it is healthy."""
        while True:
            should_start = False
            wait_for_stop = False
            with self._lock:
                if self._state == "ready":
                    self._last_activity = time.monotonic()
                    return
                if self._state == "stopping":
                    wait_for_stop = True
                elif self._state == "starting":
                    pass
                else:
                    self._state = "starting"
                    self._ready_event.clear()
                    self._start_error = None
                    should_start = True

            if should_start:
                with self._lifecycle_lock:
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
                        return
                    except Exception as exc:
                        self._remove_container()
                        with self._lock:
                            self._state = "stopped"
                            self._current_gpu = None
                            self._start_error = exc
                        self._ready_event.set()
                        raise

            if wait_for_stop:
                with self._lifecycle_lock:
                    pass
                continue

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
        pinned = self.config.get("gpu_index")
        if pinned not in (None, ""):
            log.info("[%s] Using pinned GPU %s", self.model_name, pinned)
            self._current_gpu = str(pinned)
            return self._current_gpu
        used_gpus = set()
        for mgr in _backends.values():
            if mgr is self:
                continue
            if mgr.state in ("starting", "ready") and mgr._current_gpu is not None:
                used_gpus.add(mgr._current_gpu)
        log.info(
            "[%s] Auto-selecting GPU (excluding %s)",
            self.model_name,
            sorted(used_gpus) if used_gpus else "none",
        )
        gpu = _pick_free_gpu(exclude=used_gpus)
        self._current_gpu = gpu
        return gpu

    def _serve_hf_repo(self) -> bool:
        """Serve by Hugging Face repo id with a cache mount instead of ``/model``."""
        if self.config.get("serve_hf_repo"):
            return True
        model_dir = Path(self.config["model_dir"])
        # HF hub snapshots use out-of-tree blob symlinks; mounting only the
        # snapshot dir breaks config resolution inside the container.
        return model_dir.name.startswith("snapshots")

    def _serve_model_path(self) -> str:
        if self._serve_hf_repo():
            repo = self.config.get("hf_repo")
            if repo:
                return str(repo)
        model_dir = Path(self.config["model_dir"])
        if (model_dir / "config.json").is_file():
            return "/model"
        repo = self.config.get("hf_repo")
        if repo:
            return str(repo)
        return "/model"

    def _uses_image_entrypoint(self) -> bool:
        """Return True when the image already runs ``vllm serve`` as its entrypoint."""
        configured = self.config.get("docker_serve_via_entrypoint")
        if configured is not None:
            return bool(configured)
        image = str(self.config.get("docker_image", DEFAULT_DOCKER_IMAGE))
        return image.startswith("vllm/vllm-openai")

    def _build_vllm_serve_args(self, *, include_prefix: bool = True) -> list[str]:
        args: list[str] = []
        if include_prefix:
            args += ["vllm", "serve"]
        args += [
            self._serve_model_path(),
            "--served-model-name",
            self.config.get("served_name", self.model_name),
            "--host",
            "0.0.0.0",
            "--port",
            str(VLLM_CONTAINER_PORT),
            "--max-model-len",
            str(self.config.get("max_model_len", 131072)),
            "--gpu-memory-utilization",
            str(self.config.get("gpu_memory_utilization", 0.85)),
            "--max-num-seqs",
            str(self.config.get("max_num_seqs", 4)),
        ]
        if self.config.get("trust_remote_code"):
            args.append("--trust-remote-code")
        if self.config.get("enable_auto_tool_choice"):
            args.append("--enable-auto-tool-choice")
        tcp = self.config.get("tool_call_parser")
        if tcp:
            args += ["--tool-call-parser", str(tcp)]
        rp = self.config.get("reasoning_parser")
        if rp:
            args += ["--reasoning-parser", str(rp)]
        for extra in self.config.get("vllm_extra_args", []):
            args.append(str(extra))
        return args

    def _docker_env_args(self) -> list[str]:
        env: dict[str, str] = {}
        if HF_TOKEN:
            env["HF_TOKEN"] = HF_TOKEN
        for key, val in self.config.get("docker_env", {}).items():
            env[str(key)] = str(val)
        args: list[str] = []
        for key, val in env.items():
            args += ["-e", f"{key}={val}"]
        return args

    def _start_container(self) -> None:
        gpu = self._resolve_gpu()
        self._ensure_model_dir()
        subprocess.run(
            ["sudo", "docker", "rm", "-f", self.container],
            check=False,
            capture_output=True,
        )
        hf_cache = self.config.get("hf_cache_dir")
        if self._serve_hf_repo():
            if not hf_cache:
                raise RuntimeError(
                    f"[{self.model_name}] serve_hf_repo requires hf_cache_dir to be set"
                )
            volume_args = ["-v", f"{hf_cache}:/root/.cache/huggingface"]
        else:
            volume_args = ["-v", f"{self.config['model_dir']}:/model:ro"]
            if hf_cache:
                volume_args += ["-v", f"{hf_cache}:/root/.cache/huggingface"]
        cmd = [
            "sudo",
            "docker",
            "run",
            "-d",
            "--name",
            self.container,
            "--ipc=host",
            "--gpus",
            f"device={gpu}",
            "--shm-size",
            "16g",
            "-p",
            f"{self.backend_port}:{VLLM_CONTAINER_PORT}",
            *volume_args,
            *self._docker_env_args(),
            self.config.get("docker_image", DEFAULT_DOCKER_IMAGE),
            *self._build_vllm_serve_args(include_prefix=not self._uses_image_entrypoint()),
        ]
        log.info("Running: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True)

    def _hf_cache_has_repo(self, hf_cache: Path, repo_id: str) -> bool:
        slug = "models--" + str(repo_id).replace("/", "--")
        return (hf_cache / "hub" / slug).is_dir()

    def _download_hf_repo(self, repo_id: str, *, cache_dir: Path | None, local_dir: Path) -> None:
        revision = self.config.get("hf_revision")
        ignore_patterns = self.config.get("hf_ignore_patterns")
        if isinstance(ignore_patterns, str):
            ignore_patterns = [ignore_patterns]
        elif ignore_patterns:
            ignore_patterns = list(ignore_patterns)
        else:
            ignore_patterns = None
        try:
            from huggingface_hub import snapshot_download

            kwargs: dict[str, Any] = {
                "repo_id": str(repo_id),
                "revision": str(revision) if revision else None,
                "ignore_patterns": ignore_patterns,
            }
            if cache_dir is not None:
                kwargs["cache_dir"] = str(cache_dir)
            else:
                kwargs["local_dir"] = str(local_dir)
            snapshot_download(**kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"[{self.model_name}] failed to download Hugging Face model {repo_id}: {exc}"
            ) from exc

    def _ensure_model_dir(self) -> None:
        if self._serve_hf_repo():
            hf_cache = Path(self.config.get("hf_cache_dir", ""))
            repo_id = self.config.get("hf_repo", "")
            if not hf_cache or not repo_id:
                raise RuntimeError(
                    f"[{self.model_name}] serve_hf_repo requires hf_cache_dir and hf_repo"
                )
            hf_cache.mkdir(parents=True, exist_ok=True)
            if self._hf_cache_has_repo(hf_cache, str(repo_id)):
                log.info(
                    "[%s] Using cached Hugging Face weights for %s",
                    self.model_name,
                    repo_id,
                )
                return
            log.info(
                "[%s] Downloading %s into Hugging Face cache %s …",
                self.model_name,
                repo_id,
                hf_cache,
            )
            self._download_hf_repo(str(repo_id), cache_dir=hf_cache, local_dir=hf_cache)
            if not self._hf_cache_has_repo(hf_cache, str(repo_id)):
                raise RuntimeError(
                    f"[{self.model_name}] Hugging Face cache missing for {repo_id}: {hf_cache}"
                )
            log.info("[%s] Downloaded %s into HF cache.", self.model_name, repo_id)
            return

        model_dir = Path(self.config["model_dir"])
        config_path = model_dir / "config.json"
        sentinel_path = model_dir / ".download_complete"
        repo_id = self.config.get("hf_repo")

        if config_path.is_file() and (not repo_id or sentinel_path.is_file()):
            return

        if not repo_id:
            raise RuntimeError(f"[{self.model_name}] model_dir missing config.json: {model_dir}")

        model_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "[%s] Downloading %s from Hugging Face to %s …", self.model_name, repo_id, model_dir
        )
        self._download_hf_repo(str(repo_id), cache_dir=None, local_dir=model_dir)

        if not config_path.is_file():
            raise RuntimeError(
                f"[{self.model_name}] Hugging Face download completed without config.json: {model_dir}"
            )
        sentinel_path.touch()
        log.info("[%s] Downloaded %s.", self.model_name, repo_id)

    def _remove_container(self) -> None:
        subprocess.run(
            ["sudo", "docker", "rm", "-f", self.container],
            check=False,
            capture_output=True,
        )

    def _stop_container(self) -> None:
        log.info("[%s] Stopping container %s …", self.model_name, self.container)
        self._remove_container()
        with self._lock:
            if self._state == "stopping" or self._state in ("ready", "starting"):
                self._state = "stopped"
            self._current_gpu = None
        self._ready_event.set()
        log.info("[%s] Container %s stopped.", self.model_name, self.container)

    def _container_running(self) -> bool:
        result = subprocess.run(
            ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _container_logs_tail(self, lines: int = 20) -> str:
        result = subprocess.run(
            ["sudo", "docker", "logs", "--tail", str(lines), self.container],
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout + result.stderr).strip()

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
            if not self._container_running():
                logs = self._container_logs_tail()
                raise RuntimeError(
                    f"[{self.model_name}] Backend container {self.container} exited "
                    f"before becoming healthy. Recent logs:\n{logs}"
                )
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
                if idle_for < IDLE_TIMEOUT:
                    continue
                if self._active_requests > 0:
                    continue
                self._state = "stopping"
            with self._lifecycle_lock:
                with self._lock:
                    if self._state != "stopping":
                        continue
                    if self._active_requests > 0:
                        self._state = "ready"
                        continue
                self._stop_container()
                return


def _copy_stream(resp: Any, wfile: Any, *, on_chunk: Callable[[], None] | None = None) -> None:
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        wfile.write(chunk)
        wfile.flush()
        if on_chunk is not None:
            on_chunk()


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
    '"content":"⏳ The model is starting up — first load on Spark can take '
    'several minutes. Please wait…"},"finish_reason":null}]}\n\n'
)
WARMUP_THINKING_SSE_DONE = (
    'data: {"id":"warmup","object":"chat.completion.chunk",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
)


class ProxyHandler(BaseHTTPRequestHandler):
    """Forwards requests to the correct vLLM backend based on model name."""

    def _check_api_key(self) -> bool:
        if not LOCAL_API_KEY:
            return True

        auth = self.headers.get("Authorization", "")
        auth = auth[7:] if auth.startswith("Bearer ") else self.headers.get("X-API-Key", "")

        if auth != LOCAL_API_KEY:
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
        if body is None:
            return
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
            except Exception as exc:
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
                "owned_by": "vllm",
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

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "Invalid Content-Length header")
            return None
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
            except Exception as exc:
                log.error("[%s] Backend failed to start: %s", backend.model_name, exc)
                return
            log.info("[%s] Backend ready — client should retry.", backend.model_name)

        if hasattr(backend, "_lock"):
            with backend._lock:
                spawn_warmup = backend._state == "stopped"
        else:
            spawn_warmup = backend.state == "stopped"
        if spawn_warmup:
            threading.Thread(target=_warm_up, daemon=True).start()

    def _forward_with_body(self, backend: BackendManager, body: bytes) -> None:
        target = f"http://localhost:{backend.backend_port}{self.path}"
        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        req = Request(target, data=body if body else None, headers=headers, method=self.command)
        backend.begin_request()
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
                    _copy_stream(resp, self.wfile, on_chunk=backend.touch)
                else:
                    self.wfile.write(resp.read())
        except HTTPError as exc:
            self.send_response(exc.code)
            for key, val in exc.headers.items():
                if key.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(exc.read())
        except URLError as exc:
            self.send_error(502, f"Backend error: {exc}")
        except Exception as exc:
            self.send_error(500, str(exc))
        finally:
            backend.end_request()

    def do_GET(self) -> None:
        """Handle GET by proxying to the matching backend."""
        self._proxy()

    def do_POST(self) -> None:
        """Handle POST by proxying to the matching backend."""
        self._proxy()

    def do_PUT(self) -> None:
        """Handle PUT by proxying to the matching backend."""
        self._proxy()

    def do_DELETE(self) -> None:
        """Handle DELETE by proxying to the matching backend."""
        self._proxy()

    def do_PATCH(self) -> None:
        """Handle PATCH by proxying to the matching backend."""
        self._proxy()

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS by proxying to the matching backend."""
        self._proxy()

    def do_HEAD(self) -> None:
        """Handle HEAD by proxying to the matching backend."""
        self._proxy()

    def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
        """Route stdlib HTTP server logs through the module logger."""
        log.info(fmt, *args)


def _shutdown_proxy(server: ThreadingHTTPServer) -> None:
    log.info("Shutting down proxy …")
    server.shutdown()
    server.server_close()
    for mgr in _backends.values():
        if mgr.state != "stopped":
            with mgr._lifecycle_lock:
                mgr._stop_container()


def main() -> None:
    """Start the proxy HTTP server and serve until interrupted."""
    models = list(_backends.keys())
    log.info(
        "spark idle proxy listening on :%d  (%d models: %s)  (idle timeout %ds)",
        LISTEN_PORT,
        len(models),
        ", ".join(models) if models else "none",
        IDLE_TIMEOUT,
    )
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)

    def _handle_signal(signum: int, _frame: object | None) -> None:
        log.info("Received signal %s", signum)
        _shutdown_proxy(server)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    server.serve_forever()


if __name__ == "__main__":
    main()
