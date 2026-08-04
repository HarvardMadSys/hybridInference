from __future__ import annotations

import importlib
import json
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


MODEL_NAME = "manual-test-model"


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
                    "container": "manual-test-sglang",
                    "gpu_index": "0",
                    "backend_port": backend_port,
                    "model_dir": "/tmp/manual-test-model",
                    "served_name": MODEL_NAME,
                    "max_model_len": 2048,
                    "mem_fraction": "0.80",
                }
            }
        )
    )
    monkeypatch.setenv("MODELS_CONFIG", str(config_path))
    monkeypatch.setenv("LOCAL_API_KEY", "manual-secret")
    monkeypatch.setenv("HEALTH_TIMEOUT", "0.2")
    monkeypatch.setenv("HEALTH_INTERVAL", "0.01")

    sys.modules.pop("ops.local_deployment_proxy.local_deployment_proxy", None)
    return importlib.import_module("ops.local_deployment_proxy.local_deployment_proxy")


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
            "container": "manual-test-sglang",
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
            "repo_id": "BAAI/bge-m3",
            "revision": "main",
            "local_dir": str(model_dir),
            "ignore_patterns": ["onnx/*"],
        }
        (model_dir / "config.json").write_text('{"model_type": "xlm-roberta"}')

    snapshot_download = Mock(side_effect=download)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-sglang",
            "model_dir": str(model_dir),
            "hf_repo": "BAAI/bge-m3",
            "hf_revision": "main",
            "hf_ignore_patterns": ["onnx/*"],
        },
    )

    backend._ensure_model_dir()

    snapshot_download.assert_called_once()
    assert (model_dir / "config.json").is_file()


def test_installed_huggingface_model_skips_download(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "installed-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"model_type": "xlm-roberta"}')
    (model_dir / ".download_complete").touch()
    snapshot_download = Mock()
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-sglang",
            "model_dir": str(model_dir),
            "hf_repo": "BAAI/bge-m3",
        },
    )

    backend._ensure_model_dir()

    snapshot_download.assert_not_called()


def test_partial_huggingface_download_is_redownloaded(monkeypatch: Any, tmp_path: Path) -> None:
    # config.json present but no .download_complete sentinel means a prior
    # download was interrupted, so snapshot_download must run again.
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "partial-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"model_type": "xlm-roberta"}')
    snapshot_download = Mock()
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-sglang",
            "model_dir": str(model_dir),
            "hf_repo": "BAAI/bge-m3",
        },
    )

    backend._ensure_model_dir()

    snapshot_download.assert_called_once()
    assert (model_dir / ".download_complete").is_file()


def test_string_ignore_patterns_is_wrapped_in_a_list(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "string-ignore-model"

    def download(**kwargs: Any) -> None:
        assert kwargs["ignore_patterns"] == ["onnx/*"]
        (model_dir / "config.json").write_text('{"model_type": "xlm-roberta"}')

    snapshot_download = Mock(side_effect=download)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-sglang",
            "model_dir": str(model_dir),
            "hf_repo": "BAAI/bge-m3",
            "hf_ignore_patterns": "onnx/*",
        },
    )

    backend._ensure_model_dir()

    snapshot_download.assert_called_once()


def test_auto_gpu_selection_excludes_gpu_held_by_running_backend(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # A backend already running on its auto-selected GPU must be excluded so a
    # second auto-selecting backend does not collide on the same device.
    proxy = _load_proxy(monkeypatch, tmp_path)
    running = proxy.BackendManager(
        "running-model", {"container": "running", "model_dir": "/tmp/running"}
    )
    with running._lock:
        running._state = "ready"
    running._current_gpu = "2"
    starting = proxy.BackendManager(
        "starting-model", {"container": "starting", "model_dir": "/tmp/starting"}
    )
    proxy._backends = {"running-model": running, "starting-model": starting}

    captured: dict[str, Any] = {}

    def fake_pick(exclude: set[str] | None = None) -> str:
        captured["exclude"] = exclude
        return "0"

    monkeypatch.setattr(proxy, "_pick_free_gpu", fake_pick)

    assert starting._resolve_gpu() == "0"
    assert captured["exclude"] == {"2"}
    assert starting._current_gpu == "0"


def test_colocate_group_follows_sibling_onto_its_gpu(monkeypatch: Any, tmp_path: Path) -> None:
    # A backend sharing a colocate_group with an already-running backend must
    # land on that sibling's GPU instead of auto-picking a different one.
    proxy = _load_proxy(monkeypatch, tmp_path)
    running = proxy.BackendManager(
        "running-model",
        {"container": "running", "model_dir": "/tmp/running", "colocate_group": "primary"},
    )
    with running._lock:
        running._state = "ready"
    running._current_gpu = "2"
    starting = proxy.BackendManager(
        "starting-model",
        {"container": "starting", "model_dir": "/tmp/starting", "colocate_group": "primary"},
    )
    proxy._backends = {"running-model": running, "starting-model": starting}

    def fail_pick(exclude: set[str] | None = None) -> str:
        raise AssertionError("should colocate without auto-picking a free GPU")

    monkeypatch.setattr(proxy, "_pick_free_gpu", fail_pick)

    assert starting._resolve_gpu() == "2"
    assert starting._current_gpu == "2"


def test_huggingface_download_failure_prevents_container_start(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "failed-model"
    snapshot_download = Mock(side_effect=OSError("network unavailable"))
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-sglang",
            "gpu_index": "0",
            "model_dir": str(model_dir),
            "hf_repo": "BAAI/bge-m3",
        },
    )
    run = Mock()
    monkeypatch.setattr(proxy.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="failed to download Hugging Face model"):
        backend._start_container()

    run.assert_not_called()


def test_embedding_container_uses_embedding_runtime_flags(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    model_dir = tmp_path / "embedding-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text('{"model_type": "xlm-roberta"}')
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "manual-test-sglang",
            "gpu_index": "0",
            "backend_port": 18080,
            "model_dir": str(model_dir),
            "served_name": MODEL_NAME,
            "max_model_len": 8192,
            "mem_fraction": "0.12",
            "is_embedding": True,
            "attention_backend": "torch_native",
            "disable_radix_cache": True,
        },
    )
    commands: list[list[str]] = []

    def record_run(command: list[str], **_: Any) -> None:
        commands.append(command)

    monkeypatch.setattr(proxy.subprocess, "run", record_run)

    backend._start_container()

    launch_command = commands[-1]
    assert "--is-embedding" in launch_command
    assert launch_command[-3:] == [
        "--attention-backend",
        "torch_native",
        "--disable-radix-cache",
    ]


def test_wait_healthy_fails_fast_when_container_exits(monkeypatch: Any, tmp_path: Path) -> None:
    # HEALTH_TIMEOUT is 0.2s in the test config but a crashed container should be
    # detected on the first poll, long before the timeout, and the raised error
    # must surface the container logs so the operator can see *why* it died.
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]

    def refuse(*_: Any, **__: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(proxy, "urlopen", refuse)
    monkeypatch.setattr(backend, "_container_running", lambda: False)
    monkeypatch.setattr(backend, "_container_logs_tail", lambda: "CUDA out of memory")

    with pytest.raises(RuntimeError, match=r"(?s)exited.*CUDA out of memory"):
        backend._wait_healthy()


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
                "owned_by": "sglang",
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


def test_mark_stopped_only_resets_a_ready_backend(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]

    with backend._lock:
        backend._state = "ready"
        backend._current_gpu = "0"
    backend.mark_stopped()
    assert backend.state == "stopped"
    assert backend._current_gpu is None

    # A restart already in flight (state "starting") must not be knocked back to
    # "stopped" by another thread racing on the same dead backend.
    with backend._lock:
        backend._state = "starting"
    backend.mark_stopped()
    assert backend.state == "starting"


def test_ensure_running_adopts_healthy_running_container(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]

    monkeypatch.setattr(backend, "_container_running", lambda: True)
    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)
    monkeypatch.setattr(backend, "_running_container_gpu", lambda: "3")
    monkeypatch.setattr(backend, "_wait_healthy", lambda: None)
    start = Mock()
    monkeypatch.setattr(backend, "_start_container", start)

    backend.ensure_running()

    # The already-running container is adopted, not torn down and reloaded.
    start.assert_not_called()
    assert backend.state == "ready"
    assert backend._current_gpu == "3"


def test_dead_ready_backend_self_heals_and_retries(monkeypatch: Any, tmp_path: Path) -> None:
    RecordingBackendHandler.requests = []

    with _serve(RecordingBackendHandler) as backend_port:
        proxy = _load_proxy(monkeypatch, tmp_path, backend_port=backend_port)
        backend = proxy._backends[MODEL_NAME]
        with backend._lock:
            backend._state = "ready"

        # First forward attempt fails as if the backend port were dead; the
        # retry (after the restart) goes through to the live backend.
        real_urlopen = proxy.urlopen
        calls = {"n": 0}

        def flaky_urlopen(req: Any, *a: Any, **k: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise proxy.URLError("connection refused")
            return real_urlopen(req, *a, **k)

        monkeypatch.setattr(proxy, "urlopen", flaky_urlopen)
        monkeypatch.setattr(backend, "alive", lambda: False)
        ensure = Mock()
        monkeypatch.setattr(backend, "ensure_running", ensure)

        with _serve(proxy.ProxyHandler) as proxy_port:
            status, _, body = _request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                method="POST",
                headers={"Authorization": "Bearer manual-secret"},
                body={"model": MODEL_NAME, "messages": [{"role": "user", "content": "ping"}]},
            )

    assert status == 200
    assert json.loads(body) == {"ok": True, "proxied_path": "/v1/chat/completions"}
    ensure.assert_called_once()
    assert calls["n"] == 2  # failed once, restarted, retried once


def test_models_endpoint_reports_dead_ready_backend_as_not_loaded(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]
    with backend._lock:
        backend._state = "ready"
    # The proxy still believes it is ready, but the container is gone.
    monkeypatch.setattr(backend, "_container_running", lambda: False)

    with _serve(proxy.ProxyHandler) as proxy_port:
        _, _, body = _request(f"http://127.0.0.1:{proxy_port}/v1/models")

    assert json.loads(body)["data"][0]["status"] == "not_loaded"


def test_vllm_embedding_uses_pooling_runner_without_kv_cache(
    monkeypatch: Any, tmp_path: Path
) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "bge-m3-vllm",
            "engine": "vllm",
            "gpu_index": "0",
            "backend_port": 18012,
            "model_dir": "/tmp/bge-m3",
            "served_name": MODEL_NAME,
            "max_model_len": 8192,
            "mem_fraction": "0.10",
            "is_embedding": True,
        },
    )

    cmd = backend._vllm_run_cmd("0")

    # Pooling runner selects the embedding path (vLLM >= 0.20); the removed
    # --task embed and the inapplicable KV-cache flag must not be present.
    assert cmd[cmd.index("--runner") + 1] == "pooling"
    assert "--task" not in cmd
    assert "--kv-cache-dtype" not in cmd


def test_vllm_generation_keeps_kv_cache_dtype(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "qwen-vllm",
            "engine": "vllm",
            "gpu_index": "0",
            "backend_port": 18001,
            "model_dir": "/tmp/qwen",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.80",
            "kv_cache_dtype": "fp8",
        },
    )

    cmd = backend._vllm_run_cmd("0")

    assert cmd[cmd.index("--kv-cache-dtype") + 1] == "fp8"
    assert "--runner" not in cmd
    # Caching flags are added regardless of KV-cache dtype (fp8 makes them a
    # no-op for hits, but the launch must not silently drop them).
    assert "--enable-prefix-caching" in cmd
    assert "--enable-prompt-tokens-details" in cmd


def test_vllm_generation_default_enables_prefix_cache_reporting(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Without an explicit kv_cache_dtype, a generation model uses the default
    (bf16) KV cache and enables prefix caching + cached_tokens reporting."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "qwen-vllm",
            "engine": "vllm",
            "gpu_index": "0",
            "backend_port": 18001,
            "model_dir": "/tmp/qwen",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.80",
        },
    )

    cmd = backend._vllm_run_cmd("0")

    assert "--enable-prefix-caching" in cmd
    assert "--enable-prompt-tokens-details" in cmd
    # No explicit opt-in → no fp8 KV cache (which would zero out cache hits).
    assert "--kv-cache-dtype" not in cmd
    assert "--runner" not in cmd


def test_health_endpoint_returns_200_without_api_key(monkeypatch: Any, tmp_path: Path) -> None:
    # The routing HealthMonitor probes GET /health with no API key and expects
    # 200; a 401 there marks every local model unhealthy.
    proxy = _load_proxy(monkeypatch, tmp_path)

    with _serve(proxy.ProxyHandler) as proxy_port:
        status, headers, body = _request(f"http://127.0.0.1:{proxy_port}/health")

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"status": "ok"}


# ── Hardware profile selection + tensor parallelism ────────────────────────


def _gpu_query_result(stdout: str) -> Any:
    """Build a fake completed-process for a mocked nvidia-smi call."""
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_detect_profile_selects_h200_for_four_h200s(monkeypatch: Any, tmp_path: Path) -> None:
    # A box with 4x H200 must serve the DeepSeek-V4-Flash-0731 (TP=2 on 2,3) profile.
    proxy = _load_proxy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy.subprocess,
        "run",
        lambda *a, **k: _gpu_query_result("NVIDIA H200\nNVIDIA H200\nNVIDIA H200\nNVIDIA H200\n"),
    )
    assert proxy._detect_profile_config().name == "models.h200.json"


def test_h200_profile_uses_tp2_on_gpus_2_and_3() -> None:
    """Canonical H200 profile shards DeepSeek-V4-Flash-0731 with TP=2 on GPUs 2,3.

    The earlier profile needed PP=3 across GPUs 0,2,3 because 273 GiB of FP8
    weights do not fit at TP=2 (and TP=3 is illegal — 64 attention heads are not
    divisible by 3). The 0731 release ships FP4 experts at ~156 GiB, so it fits on
    two GPUs and frees a third. Must stay in step with the dedicated
    ``ops/h200_idle_proxy`` profile, which is what actually runs on h200a/h200b.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ops"
    profile = json.loads((root / "local_deployment_proxy" / "models.h200.json").read_text())
    dedicated = json.loads((root / "h200_idle_proxy" / "models.json").read_text())

    model = profile["deepseek-v4-flash"]
    assert model["tensor_parallel_size"] == 2
    assert "pipeline_parallel_size" not in model
    assert model["gpu_index"] == "2,3"
    # DSpark needs the official checkpoint: the NVFP4 conversion excludes mtp.*, so
    # the draft-expert scales are dropped at load and accept length collapses to 1.0.
    assert model["hf_repo"] == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert model["speculative_algorithm"] == "DSPARK"
    assert model["moe_runner_backend"] == "marlin"

    for key in ("model_dir", "hf_repo", "gpu_index", "speculative_algorithm", "sglang_image"):
        assert model[key] == dedicated["deepseek-v4-flash"][key], (
            f"models.h200.json and h200_idle_proxy/models.json disagree on {key!r}"
        )


def test_h200_profiles_use_deepseek_v4_parsers() -> None:
    """Both H200 configs must use the DeepSeek-V4 parser pairing.

    The ``deepseek-r1`` reasoning parser assumes the whole generation is
    reasoning until a ``</think>`` close tag; requests here do not enable
    thinking, so that tag never appears and every completion returned empty
    ``content`` with the full answer classified as ``reasoning_content``.
    The ``deepseekv3`` tool-call parser does not recognize V4's DSML tool-call
    markup, so ``tool_calls`` stays null and agentic clients cannot run tools.
    """
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    for cfg_path in (
        repo / "ops" / "local_deployment_proxy" / "models.h200.json",
        repo / "ops" / "h200_idle_proxy" / "models.json",
    ):
        model = json.loads(cfg_path.read_text())["deepseek-v4-flash"]
        assert model["reasoning_parser"] == "deepseek-v4", cfg_path
        assert model["tool_call_parser"] == "deepseekv4", cfg_path


def _load_smoke_test() -> Any:
    """Load ops/local_deployment_proxy/smoke_test.py as a throwaway module."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "ops" / "local_deployment_proxy" / "smoke_test.py"
    spec = importlib.util.spec_from_file_location("_smoke_test_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_chat_rejects_reasoning_only_answers(monkeypatch: Any) -> None:
    """An answer that only lands in ``reasoning_content`` must fail loudly.

    The pre-fix smoke test fell back to ``reasoning_content`` when ``content``
    was empty and passed — masking the misconfigured reasoning parser that
    left every standard client (which reads ``content``) with empty replies.
    """
    smoke = _load_smoke_test()
    body = json.dumps({"choices": [{"message": {"content": "", "reasoning_content": "OK"}}]})
    monkeypatch.setattr(smoke, "request", lambda *a, **k: (200, body, 0.1))
    assert smoke.check_chat("http://proxy", "key", "deepseek-v4-flash", 5, False) is False


def test_smoke_chat_accepts_content_answers(monkeypatch: Any) -> None:
    smoke = _load_smoke_test()
    body = json.dumps({"choices": [{"message": {"content": "hello", "reasoning_content": ""}}]})
    monkeypatch.setattr(smoke, "request", lambda *a, **k: (200, body, 0.1))
    assert smoke.check_chat("http://proxy", "key", "deepseek-v4-flash", 5, False) is True


def test_sglang_pipeline_parallel_sets_pp_size_and_ipc(monkeypatch: Any, tmp_path: Path) -> None:
    # A pipeline-parallel sglang backend gets --pp-size, --ipc=host, and a quoted
    # multi-GPU device list; --tp stays at its (1) default.
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "ds-sglang",
            "engine": "sglang",
            "gpu_index": "0,2,3",
            "pipeline_parallel_size": 3,
            "backend_port": 18003,
            "model_dir": "/tmp/ds",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.90",
        },
    )

    cmd = backend._sglang_run_cmd("0,2,3")

    assert cmd[cmd.index("--pp-size") + 1] == "3"
    assert cmd[cmd.index("--tp") + 1] == "1"
    assert cmd[cmd.index("--gpus") + 1] == '"device=0,2,3"'
    assert "--ipc=host" in cmd


def test_auto_gpu_selection_spans_tp_times_pp_devices(monkeypatch: Any, tmp_path: Path) -> None:
    # An unpinned backend sharded by both tp and pp must claim tp*pp GPUs.
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "ds",
            "model_dir": "/tmp/ds",
            "tensor_parallel_size": 2,
            "pipeline_parallel_size": 3,
        },
    )
    proxy._backends = {MODEL_NAME: backend}

    captured: dict[str, Any] = {}

    def fake_pick_many(count: int, exclude: set[str] | None = None) -> str:
        captured["count"] = count
        return ",".join(str(i) for i in range(count))

    monkeypatch.setattr(proxy, "_pick_free_gpus", fake_pick_many)

    backend._resolve_gpu()
    assert captured["count"] == 6


def test_detect_profile_selects_rtx6000(monkeypatch: Any, tmp_path: Path) -> None:
    # A single RTX PRO 6000 must serve the Qwen profile.
    proxy = _load_proxy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy.subprocess,
        "run",
        lambda *a, **k: _gpu_query_result("NVIDIA RTX PRO 6000 Blackwell Max-Q\n"),
    )
    assert proxy._detect_profile_config().name == "models.rtx6000.json"


def test_detect_profile_falls_back_when_two_h200s(monkeypatch: Any, tmp_path: Path) -> None:
    # Fewer than 4 H200s is not the DeepSeek deployment — use the default.
    proxy = _load_proxy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy.subprocess,
        "run",
        lambda *a, **k: _gpu_query_result("NVIDIA H200\nNVIDIA H200\n"),
    )
    assert proxy._detect_profile_config().name == "models.json"


def test_detect_profile_falls_back_without_nvidia_smi(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)

    def boom(*_: Any, **__: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(proxy.subprocess, "run", boom)
    assert proxy._detect_profile_config().name == "models.json"


def test_pick_free_gpus_returns_distinct_least_used(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    # Four idle GPUs with strictly increasing memory use → 0 is least-used.
    rows = "0, 1000, 100000\n1, 2000, 100000\n2, 3000, 100000\n3, 4000, 100000\n"
    monkeypatch.setattr(proxy.subprocess, "run", lambda *a, **k: _gpu_query_result(rows))
    assert proxy._pick_free_gpus(4) == "0,1,2,3"
    assert proxy._pick_free_gpus(2) == "0,1"


def test_vllm_tensor_parallel_spans_multiple_gpus(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "ds-vllm",
            "engine": "vllm",
            "gpu_index": "0,1,2,3",
            "tensor_parallel_size": 4,
            "backend_port": 18001,
            "model_dir": "/tmp/ds",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.90",
        },
    )

    cmd = backend._vllm_run_cmd("0,1,2,3")

    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "4"
    # A multi-GPU device list must be quoted or docker splits it on commas.
    assert cmd[cmd.index("--gpus") + 1] == '"device=0,1,2,3"'
    # Multi-GPU NCCL needs host IPC.
    assert "--ipc=host" in cmd


def test_sglang_tensor_parallel_sets_tp_and_ipc(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "ds-sglang",
            "engine": "sglang",
            "gpu_index": "0,1,2,3",
            "tensor_parallel_size": 4,
            "backend_port": 18001,
            "model_dir": "/tmp/ds",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.90",
        },
    )

    cmd = backend._sglang_run_cmd("0,1,2,3")

    assert cmd[cmd.index("--tp") + 1] == "4"
    # A multi-GPU device list must be quoted or docker splits it on commas.
    assert cmd[cmd.index("--gpus") + 1] == '"device=0,1,2,3"'
    assert "--ipc=host" in cmd


def test_sglang_moe_runner_backend_override(monkeypatch: Any, tmp_path: Path) -> None:
    # NVFP4 / FP4-expert models on SM90 (H200) need --moe-runner-backend marlin;
    # the flag must appear only when the config sets it.
    proxy = _load_proxy(monkeypatch, tmp_path)
    base = {
        "container": "ds-sglang",
        "engine": "sglang",
        "gpu_index": "2,3",
        "tensor_parallel_size": 2,
        "backend_port": 18003,
        "model_dir": "/tmp/ds",
        "served_name": MODEL_NAME,
        "max_model_len": 4096,
        "mem_fraction": "0.90",
    }

    without = proxy.BackendManager(MODEL_NAME, dict(base))._sglang_run_cmd("2,3")
    assert "--moe-runner-backend" not in without

    with_marlin = proxy.BackendManager(
        MODEL_NAME, {**base, "moe_runner_backend": "marlin"}
    )._sglang_run_cmd("2,3")
    assert with_marlin[with_marlin.index("--moe-runner-backend") + 1] == "marlin"


def test_sglang_mtp_algorithm_override(monkeypatch: Any, tmp_path: Path) -> None:
    # DeepSeek-V4-Flash requires EAGLE (not the NEXTN default) for its MTP layer.
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "ds-sglang",
            "engine": "sglang",
            "gpu_index": "2,3",
            "tensor_parallel_size": 2,
            "backend_port": 18003,
            "model_dir": "/tmp/ds",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.90",
            "mtp": True,
            "speculative_algorithm": "EAGLE",
        },
    )

    cmd = backend._sglang_run_cmd("2,3")

    assert cmd[cmd.index("--speculative-algorithm") + 1] == "EAGLE"


def _dspark_base() -> dict[str, Any]:
    return {
        "container": "ds-sglang",
        "engine": "sglang",
        "gpu_index": "2,3",
        "tensor_parallel_size": 2,
        "backend_port": 18003,
        "model_dir": "/tmp/ds",
        "served_name": MODEL_NAME,
        "max_model_len": 4096,
        "mem_fraction": "0.90",
        "mtp": True,
        "speculative_algorithm": "DSPARK",
    }


def test_sglang_dspark_omits_single_mtp_layer_defaults(monkeypatch: Any, tmp_path: Path) -> None:
    # DeepSeek-V4-Flash-0731's DSpark head carries its own block size (gamma) in the
    # checkpoint. Sending the single-MTP-layer defaults would override it --
    # --speculative-num-draft-tokens 2 collapses a 5-token block to 1 -- so none of
    # the step/topk/draft-token flags may be emitted unless explicitly configured.
    proxy = _load_proxy(monkeypatch, tmp_path)

    cmd = proxy.BackendManager(MODEL_NAME, _dspark_base())._sglang_run_cmd("2,3")

    assert cmd[cmd.index("--speculative-algorithm") + 1] == "DSPARK"
    assert "--speculative-num-draft-tokens" not in cmd
    assert "--speculative-num-steps" not in cmd
    assert "--speculative-eagle-topk" not in cmd


def test_sglang_dspark_passes_explicit_overrides(monkeypatch: Any, tmp_path: Path) -> None:
    # Explicit tuning still wins over checkpoint inference when it is set.
    proxy = _load_proxy(monkeypatch, tmp_path)

    cmd = proxy.BackendManager(
        MODEL_NAME,
        {**_dspark_base(), "speculative_num_draft_tokens": 6, "speculative_dspark_block_size": 5},
    )._sglang_run_cmd("2,3")

    assert cmd[cmd.index("--speculative-num-draft-tokens") + 1] == "6"
    assert cmd[cmd.index("--speculative-dspark-block-size") + 1] == "5"


def test_sglang_non_dspark_mtp_keeps_defaults(monkeypatch: Any, tmp_path: Path) -> None:
    # The DSPARK special case must not change behaviour for EAGLE/NEXTN models.
    proxy = _load_proxy(monkeypatch, tmp_path)

    cmd = proxy.BackendManager(
        MODEL_NAME, {**_dspark_base(), "speculative_algorithm": "EAGLE"}
    )._sglang_run_cmd("2,3")

    assert cmd[cmd.index("--speculative-algorithm") + 1] == "EAGLE"
    assert cmd[cmd.index("--speculative-num-steps") + 1] == "1"
    assert cmd[cmd.index("--speculative-eagle-topk") + 1] == "1"
    assert cmd[cmd.index("--speculative-num-draft-tokens") + 1] == "2"


def test_sglang_cache_dir_and_image_pin(monkeypatch: Any, tmp_path: Path) -> None:
    # A persistent JIT cache mount and a pinned image are both opt-in; DSpark needs
    # sglang >= 0.5.16, so the deployment pins the tag rather than tracking :latest.
    proxy = _load_proxy(monkeypatch, tmp_path)
    base = _dspark_base()

    default = proxy.BackendManager(MODEL_NAME, dict(base))._sglang_run_cmd("2,3")
    assert "lmsysorg/sglang:latest" in default
    assert not any(arg.endswith(":/root/.cache") for arg in default)

    pinned = proxy.BackendManager(
        MODEL_NAME,
        {
            **base,
            "cache_dir": "/var/tmp/sglang-cache/ds",
            "sglang_image": "lmsysorg/sglang:v0.5.16",
        },
    )._sglang_run_cmd("2,3")
    assert "lmsysorg/sglang:v0.5.16" in pinned
    assert "lmsysorg/sglang:latest" not in pinned
    assert "/var/tmp/sglang-cache/ds:/root/.cache" in pinned


def test_sglang_blank_image_falls_back_to_default_tag(monkeypatch: Any, tmp_path: Path) -> None:
    # A key that is present but blank/null must fall back to the default tag. A dict
    # default only covers an absent key, so "" and None would otherwise reach docker
    # as the literal image references "" and "None".
    proxy = _load_proxy(monkeypatch, tmp_path)

    for blank in ("", None):
        cmd = proxy.BackendManager(
            MODEL_NAME, {**_dspark_base(), "sglang_image": blank}
        )._sglang_run_cmd("2,3")
        assert "lmsysorg/sglang:latest" in cmd
        assert "None" not in cmd
        assert "" not in cmd


def test_sglang_skip_server_warmup_opt_in(monkeypatch: Any, tmp_path: Path) -> None:
    proxy = _load_proxy(monkeypatch, tmp_path)
    base = _dspark_base()

    assert "--skip-server-warmup" not in proxy.BackendManager(
        MODEL_NAME, dict(base)
    )._sglang_run_cmd("2,3")
    assert "--skip-server-warmup" in proxy.BackendManager(
        MODEL_NAME, {**base, "skip_server_warmup": True}
    )._sglang_run_cmd("2,3")


def test_sglang_mamba_mtp_uses_extra_buffer_and_spec_v2(monkeypatch: Any, tmp_path: Path) -> None:
    # Hybrid Mamba MoE models (Qwen3.5/3.6) must keep the radix (prefix) cache while
    # running MTP spec decoding. sglang disables the radix cache for these unless the
    # Mamba scheduler reserves the extra ping-pong buffer and the v2 speculative path
    # is enabled — so "mtp" + "mamba" together must emit --mamba-scheduler-strategy
    # extra_buffer and export SGLANG_ENABLE_SPEC_V2=1. This pins the Qwen3.6-35B
    # rtx6000/default-profile config so a regression cannot silently drop prefix
    # caching back to zero (the vLLM hybrid-Mamba failure this config replaced).
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "qwen36-sglang",
            "engine": "sglang",
            "gpu_index": "0",
            "backend_port": 18001,
            "model_dir": "/tmp/qwen",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.80",
            "mtp": True,
            "mamba": True,
        },
    )

    cmd = backend._sglang_run_cmd("0")

    # Prefix-cache hits still surface to clients as cached_tokens.
    assert "--enable-cache-report" in cmd
    # MTP spec decoding coexists with the radix cache via the extra_buffer strategy.
    assert cmd[cmd.index("--speculative-algorithm") + 1] == "NEXTN"
    assert cmd[cmd.index("--mamba-scheduler-strategy") + 1] == "extra_buffer"
    # The v2 speculative path is passed to the container as an env var.
    assert backend._docker_env_args() == ["-e", "SGLANG_ENABLE_SPEC_V2=1"]


def test_single_gpu_backend_omits_ipc_host(monkeypatch: Any, tmp_path: Path) -> None:
    # The default TP=1 path must not add --ipc=host (single-GPU, no NCCL).
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy.BackendManager(
        MODEL_NAME,
        {
            "container": "qwen-vllm",
            "engine": "vllm",
            "gpu_index": "0",
            "backend_port": 18001,
            "model_dir": "/tmp/qwen",
            "served_name": MODEL_NAME,
            "max_model_len": 4096,
            "mem_fraction": "0.80",
        },
    )

    cmd = backend._vllm_run_cmd("0")

    assert "--ipc=host" not in cmd
    assert cmd[cmd.index("--tensor-parallel-size") + 1] == "1"


def test_copy_stream_forwards_sse_chunks_as_they_arrive(monkeypatch: Any, tmp_path: Path) -> None:
    # Regression: read(8192) accumulated transfer-encoding chunks until 8 KB or
    # EOF, so sub-8KB completions reached the gateway as one end-of-stream blob
    # (recorded ttft_ms ~= latency_ms). The backend gates its second event on
    # the client flushing the first, so only per-chunk reads complete the
    # handshake and receive both events.
    proxy = _load_proxy(monkeypatch, tmp_path)
    events = (b"data: one\n\n", b"data: two\n\n")
    first_event_flushed = threading.Event()

    class DribbleHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for index, event in enumerate(events):
                if index and not first_event_flushed.wait(timeout=5):
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, fmt: str, *args: object) -> None:
            return

    class RecordingFile:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(bytes(data))

        def flush(self) -> None:
            first_event_flushed.set()

    wfile = RecordingFile()
    with (
        _serve(DribbleHandler) as port,
        urlopen(f"http://127.0.0.1:{port}/stream", timeout=5) as resp,
    ):
        proxy._copy_stream(resp, wfile)

    assert len(wfile.writes) >= 2
    assert b"".join(wfile.writes) == b"".join(events)


def test_warmup_banner_uses_per_model_startup_estimate(monkeypatch: Any, tmp_path: Path) -> None:
    # A flat "about 120 seconds" understates a large MoE cold start by ~10x, which is
    # what callers saw while DeepSeek-V4 was taking 9-14 minutes to reach ready.
    proxy = _load_proxy(monkeypatch, tmp_path)

    default = proxy.BackendManager(MODEL_NAME, {"container": "c", "model_dir": "/tmp/m"})
    assert "about 120 seconds" in proxy._warmup_thinking_sse(default)

    slow = proxy.BackendManager(
        MODEL_NAME, {"container": "c", "model_dir": "/tmp/m", "startup_estimate_seconds": 840}
    )
    banner = proxy._warmup_thinking_sse(slow)
    assert "about 14 minutes" in banner
    assert "120 seconds" not in banner
