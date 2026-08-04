from __future__ import annotations

import importlib
import json
import logging
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


def _no_such_container() -> Any:
    """Fake ``subprocess.run`` result standing in for a container that is absent.

    ``docker inspect`` exits non-zero when the name does not exist, which the
    proxy reads as "no container, therefore no owner". Stubs that return ``None``
    instead are not standing in for anything docker can do.
    """
    return SimpleNamespace(returncode=1, stdout="", stderr="")


def _container_mutations(run: Mock) -> list[list[str]]:
    """Return the mocked docker commands that would actually change something.

    The proxy reads a container's ownership labels with ``docker inspect`` before
    it starts or stops anything, so "no container was touched" is a claim about
    ``run``/``rm``/``stop``/``kill`` — not about the call count.
    """
    mutating: list[list[str]] = []
    for call in run.call_args_list:
        cmd = call.args[0] if call.args else call.kwargs.get("args")
        if not isinstance(cmd, list) or "docker" not in cmd:
            continue
        rest = cmd[cmd.index("docker") + 1 :]
        if rest and rest[0] in {"run", "rm", "stop", "kill"}:
            mutating.append(cmd)
    return mutating


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

    def contention_error(self) -> None:
        """No sibling proxy holds this container name."""
        return None

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
    run = Mock(return_value=_no_such_container())
    monkeypatch.setattr(proxy.subprocess, "run", run)

    with pytest.raises(RuntimeError, match=r"model_dir missing config\.json"):
        backend._start_container()

    assert _container_mutations(run) == []


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
    run = Mock(return_value=_no_such_container())
    monkeypatch.setattr(proxy.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="failed to download Hugging Face model"):
        backend._start_container()

    assert _container_mutations(run) == []


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

    def record_run(command: list[str], **_: Any) -> Any:
        commands.append(command)
        # Non-zero only for the `docker inspect` of a container that is not there.
        # `docker run` has to answer 0, because the start path now reads its exit
        # status: an exit 1 with no output is not something docker does, and read
        # as a real launch failure it would abort this test before the assertions.
        if "inspect" in command:
            return _no_such_container()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

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

    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)
    monkeypatch.setattr(backend, "_running_container_gpu", lambda: "3")
    monkeypatch.setattr(backend, "_wait_healthy", lambda: None)
    # Running, and unlabelled: a container started before ownership labels
    # existed. Reading it as this proxy's own is what keeps this path working
    # across the upgrade.
    monkeypatch.setattr(backend, "_inspect_container", lambda: (True, {}))
    start = Mock()
    monkeypatch.setattr(backend, "_start_container", start)

    backend.ensure_running()

    # The already-running container is adopted, not torn down and reloaded.
    start.assert_not_called()
    assert backend.state == "ready"
    assert backend._current_gpu == "3"


# ── Container ownership ────────────────────────────────────────────────────
#
# Several units run this same script with different MODELS_CONFIG values, and a
# container name is the only thing the lifecycle code used to key on: `docker rm
# -f <name>` before every start, another on the idle path, and an adoption check
# that asked only "is something running under this name and answering on this
# port?". Two processes that resolve the same name therefore destroyed and
# re-adopted each other's backend. These cover the labels that fix it.


class _FakeDocker:
    """Stand-in for the ``docker`` CLI: answers ``inspect``, records the rest.

    Models three things about real docker that the ownership guard's correctness
    rests on, none of which a command recorder alone would express:

    * **container names are unique.** ``docker run --name X`` fails when X exists,
      and fails at *create* time, before anything is started or any device is
      claimed. That refusal is the only atomic claim on a name there is, so the
      fake answers a real conflict message rather than pretending the launch
      worked — otherwise a proxy that skipped its removal would look successful
      here and silently fail to replace a stale container on a real host.
    * **containers have ids, and removal is by whatever you name.** ``docker rm -f``
      is given a target; the fake honours it, so a removal aimed at a container
      that is gone misses (as docker's "No such container" does) instead of
      clearing whatever holds the name now. Without this a test cannot see the
      difference between removing "the container I inspected" and removing
      "whatever answers to this name", which is the whole of the race fix.
    * **a removed container stops answering.** The GPU reuse fix rests on reading
      the outgoing container's devices *before* the removal; a fake that kept
      answering afterwards would let that read move below the ``rm`` with the
      suite still green, where real docker returns nothing and the backend
      migrates.
    * **a container has a state word, not just a running flag.** ``.State.Status``
      distinguishes a container that has *finished* from one that has not started
      *yet*: `docker run -d` reserves the name and writes the labels at create time
      and starts the container after, so an ordinary sibling launch is observable as
      ``created`` with ``Running`` false. A fake that only flipped a boolean could
      not express that state at all, which is how "not running" came to be treated
      as "a corpse, reclaim it" with the suite green.

    ``status`` defaults to whatever ``running`` implies, so a test says nothing about
    it unless the distinction is the point.
    """

    def __init__(
        self,
        *,
        labels: dict[str, str] | None = None,
        exists: bool = True,
        running: bool = True,
        status: str | None = None,
        omit_status: bool = False,
        gpu: str = "0",
        container_id: str = "cid-original",
        on_run: Any = None,
    ) -> None:
        self.labels = labels
        self.exists = exists
        self.running = running
        self.status = status
        # A daemon that answers the inspect without a state word at all, for the
        # fallback that keeps an unreadable reading from wedging a name.
        self.omit_status = omit_status
        self.gpu = gpu
        self.container_id = container_id
        # Called with (fake, attempt_number) just before each `docker run` is
        # answered, so a test can let a sibling proxy win the name in the one
        # window `docker run` itself is the guard for. Returning a result object
        # overrides the answer, which is how an unrelated launch failure is
        # injected.
        self.on_run = on_run
        # Called with (fake, target) just before each `docker rm` is answered, for
        # the mirror-image window: the container was replaced after its labels were
        # read but before the removal landed. Whether the removal then hits is the
        # whole point of naming an id, so the fake has to be able to be changed
        # underneath one. Returning a result object overrides the answer, which is
        # how a removal docker refuses — an overlay2 mount that is busy, a
        # CUDA-wedged process in D state — is injected while the container stays.
        self.on_rm: Any = None
        self.runs = 0
        self.name: str | None = None
        self.commands: list[list[str]] = []

    def _verb(self, command: list[str]) -> str:
        if "docker" not in command:
            return ""
        rest = command[command.index("docker") + 1 :]
        return rest[0] if rest else ""

    def _name_conflict(self) -> Any:
        """The exact refusal docker answers for a name that is already taken."""
        return SimpleNamespace(
            returncode=125,
            stdout="",
            stderr=(
                f"docker: Error response from daemon: Conflict. The container name "
                f'"/{self.name or "container"}" is already in use by container '
                f'"{self.container_id}". You have to remove (or rename) that container '
                f"to be able to reuse that name.\n"
            ),
        )

    def run(self, command: list[str], **_: Any) -> Any:
        self.commands.append(list(command))
        # Learn the container name from whichever argument carries it, so removal
        # by name and removal by id can be told apart without every call site
        # having to declare the name twice.
        if "--name" in command:
            self.name = command[command.index("--name") + 1]
        elif "inspect" in command:
            self.name = command[-1]
        verb = self._verb(command)
        if verb == "run":
            self.runs += 1
            if self.on_run is not None:
                override = self.on_run(self, self.runs)
                if override is not None:
                    return override
            if self.exists:
                return self._name_conflict()
            self.exists = True
            self.running = True
            # A launched container is running, whatever state a test set up for the
            # one that was there before it.
            self.status = None
            self.labels = _labels_from_run(command) or None
            self.container_id = f"cid-launched-{self.runs}"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if verb in {"rm", "kill"}:
            if self.on_rm is not None:
                override = self.on_rm(self, command[-1])
                if override is not None:
                    return override
            target = command[-1]
            if not self.exists or target not in {self.container_id, self.name}:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr=f"Error: No such container: {target}"
                )
            self.exists = False
            self.running = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "inspect" not in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if not self.exists:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
        fmt = command[command.index("-f") + 1]
        if ".State.Running" in fmt or ".Config.Labels" in fmt:
            # The proxy reads identity, liveness, state word and ownership with one
            # inspect, so the answer is the JSON object its format string builds.
            # Rendered here the way docker would: an unlabelled container reports
            # null, not {}.
            payload = {
                "id": self.container_id,
                "running": self.running,
                "status": self.status or ("running" if self.running else "exited"),
                "labels": self.labels,
            }
            if self.omit_status:
                del payload["status"]
            body = json.dumps(payload)
        else:
            # The device request, as docker stores it. `--gpus '"device=2,3"'` is
            # ONE request whose DeviceIDs is ["2", "3"] — that single-request parse
            # is what the quoting exists for — so a multi-GPU `gpu` here must not
            # be flattened into one id, or the fake would hide the very bug that
            # the separator-less Go template caused.
            body = (
                json.dumps(
                    [
                        {
                            "Driver": "",
                            "Count": 0,
                            "DeviceIDs": self.gpu.split(","),
                            "Capabilities": [["gpu"]],
                            "Options": {},
                        }
                    ]
                )
                if self.gpu
                else "null"
            )
        return SimpleNamespace(returncode=0, stdout=body + "\n", stderr="")

    @property
    def mutations(self) -> list[list[str]]:
        """Commands that would actually change a container's existence."""
        return [
            c
            for c in self.commands
            if "docker" in c
            and c[c.index("docker") + 1 :][:1]
            and c[c.index("docker") + 1] in {"run", "rm", "stop", "kill"}
        ]

    @property
    def state_inspects(self) -> list[list[str]]:
        """Inspects that read liveness and/or ownership labels.

        Counted in tests: both facts must come back from *one* daemon round trip.
        Excludes the device-request inspect, which asks a different question.
        """
        return [
            c
            for c in self.commands
            if "inspect" in c
            and any(".State.Running" in tok or ".Config.Labels" in tok for tok in c)
        ]

    def index_of(self, predicate: Any) -> int:
        """Position of the first recorded command matching ``predicate``, or -1.

        Lets a test assert on *ordering* between two docker calls, which some of
        them depend on and none of them could previously see.
        """
        for i, command in enumerate(self.commands):
            if predicate(command):
                return i
        return -1


def _labelled_backend(
    proxy: Any,
    tmp_path: Path,
    name: str = "owned-model",
    **extra: Any,
) -> Any:
    """Build a backend over a model dir complete enough for ``_start_container``.

    Reuses the directory when called repeatedly with the same ``name``, so two
    backends built from configs differing in one key really do differ in one key —
    a distinct ``model_dir`` would change the profile hash by itself.

    Pass ``key=None`` to drop a default key from the config rather than override
    it — ``gpu_index=None`` gives the auto-selecting shape the real profiles ship
    (``models.json`` and ``models.rtx6000.json`` omit it), not a config carrying
    an empty string.
    """
    model_dir = tmp_path / name
    model_dir.mkdir(exist_ok=True)
    (model_dir / "config.json").write_text('{"model_type": "llama"}')
    config = {
        "container": "contended-sglang",
        "gpu_index": "0",
        "backend_port": 18099,
        "model_dir": str(model_dir),
        "served_name": MODEL_NAME,
        **extra,
    }
    return proxy.BackendManager(MODEL_NAME, {k: v for k, v in config.items() if v is not None})


def _labels_from_run(command: list[str]) -> dict[str, str]:
    """Extract ``--label k=v`` pairs from a ``docker run`` command."""
    out: dict[str, str] = {}
    for i, token in enumerate(command):
        if token == "--label":
            key, _, value = command[i + 1].partition("=")
            out[key] = value
    return out


@pytest.mark.parametrize("engine", ["sglang", "vllm"])
def test_start_container_stamps_owner_and_profile_labels(
    monkeypatch: Any, tmp_path: Path, engine: str
) -> None:
    """Every container this proxy launches must say who owns it.

    Without these two labels the name is the only ownership token there is, and a
    sibling proxy that resolves the same name cannot tell "my backend" from
    "someone else's" — which is what let it ``docker rm -f`` a container another
    process was loading.

    Parametrized over both engines on purpose: the run commands are built
    separately, and ``bge-m3`` ships with ``"engine": "vllm"``, so an unstamped
    vLLM launch would be read as every proxy's own and keep the exact cross-kill
    semantics the labels remove. Dropping the stamp from ``_vllm_run_cmd`` used to
    leave the whole suite green.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path, engine=engine)
    fake = _FakeDocker(exists=False)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    backend._start_container()

    launched = [c for c in fake.mutations if c[c.index("docker") + 1] == "run"]
    assert len(launched) == 1
    image = "vllm/vllm-openai:latest" if engine == "vllm" else "lmsysorg/sglang:latest"
    assert image in launched[0]
    labels = _labels_from_run(launched[0])
    assert labels[proxy.OWNER_LABEL] == proxy.PROXY_OWNER == f"port-{proxy.LISTEN_PORT}"
    assert labels[proxy.PROFILE_LABEL] == backend._profile


def test_start_container_refuses_to_replace_another_proxys_container(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The unconditional ``docker rm -f`` must not fire on a foreign container.

    This is the collision the two H200 units would produce if both ran on one
    box: whichever started last destroyed the other's backend by name.

    ``running=True`` is load-bearing, not scenery: the refusal is gated on the
    foreign container being alive, since an exited one holds nothing and must be
    reclaimable — see
    ``test_exited_foreign_container_is_reclaimed_not_refused_forever``.

    Asserts *where* the refusal happens, not only that one happens. ``_start_container``
    keeps this check at the top as the early, cheap refusal — the one that saves a
    several-hundred-GiB download that was going to be thrown away — while
    ``_clear_container_name`` re-takes the same decision before the removal and
    raises the same message. So a test that only matched the message passed with the
    early check deleted, and the whole property it exists for was pinned by nothing:
    the unguarded path reads the container's devices, resolves a GPU (which records
    ``_current_gpu``, so sibling backends exclude a device this one never gets) and
    runs ``_ensure_model_dir`` before refusing — on every retried request.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(labels={proxy.OWNER_LABEL: "port-8003"}, running=True)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def must_not_be_reached() -> None:
        raise AssertionError("refused too late: the download was already being paid for")

    monkeypatch.setattr(backend, "_ensure_model_dir", must_not_be_reached)

    with pytest.raises(RuntimeError, match=r"Refusing to replace container contended-sglang"):
        backend._start_container()

    # Nothing destroyed, nothing launched — and the message names both sides so
    # an operator can tell which unit to reconfigure.
    assert fake.mutations == []
    # Refused before any GPU bookkeeping: no device read of the foreign container,
    # and no device claimed on its behalf.
    assert fake.index_of(lambda c: any("DeviceRequests" in tok for tok in c)) == -1, fake.commands
    assert backend._current_gpu is None


def test_exited_foreign_container_is_reclaimed_not_refused_forever(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A foreign *corpse* must not wedge the name. Nothing else would clear it.

    ``bench_decode.sh`` has the operator hand-start this very container with
    ``owner=manual``; a ``docker stop`` afterwards (rather than ``docker rm``)
    leaves it present but exited. It holds no GPU and serves no traffic, yet no
    path in this module removes a foreign container — ``_stop_container`` declines
    one too — so refusing it would 502 the model on that node until an operator
    removed the corpse by hand. Reclaim it instead.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(labels={proxy.OWNER_LABEL: "manual"}, exists=True, running=False)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    backend._start_container()

    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]


def test_foreign_container_created_but_not_started_is_not_reclaimed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A container that has not started yet is not a corpse, and is not reclaimable.

    ``docker run -d`` creates the container and starts it in two steps: the name is
    reserved and the ``--label`` owner stamp is written at create, the process starts
    after (nvidia-container hooks, device injection — not instantaneous). For that
    whole window docker reports ``Status: created`` with ``Running: false``, so a
    reclaim gated on liveness alone destroys a stranger's container at t=0 — with no
    operator and no stale label reading involved, on an ordinary sibling launch.

    Refuse it instead, and say which state it was in: a foreign container is
    reclaimed only once it has genuinely exited. Refused as early as a live one,
    too — the check at the top of the start path applies the same rule, so a
    collision visible up front still costs no download.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(
        labels={proxy.OWNER_LABEL: "port-8003"},
        exists=True,
        running=False,
        status="created",
        container_id="cid-sibling-created",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def must_not_be_reached() -> None:
        raise AssertionError("refused too late: the download was already being paid for")

    monkeypatch.setattr(backend, "_ensure_model_dir", must_not_be_reached)

    with pytest.raises(RuntimeError, match=r"Refusing to replace container") as raised:
        backend._start_container()

    assert fake.mutations == []
    assert backend._current_gpu is None
    assert fake.exists is True
    assert fake.container_id == "cid-sibling-created"
    # The state is named, so the operator is not sent looking for a live backend.
    assert "'created'" in str(raised.value)
    # Reaches the client through send_error, which encodes the status line latin-1.
    str(raised.value).encode("ascii")


def test_a_siblings_container_created_during_the_download_is_not_destroyed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The same, in the window the removal actually runs in.

    The check at the top of the start path cannot see this one: the sibling's
    ``docker run`` happens while ``_ensure_model_dir`` is fetching weights, so the
    only reading that matters is the one ``_clear_container_name`` takes immediately
    before its ``docker rm -f``. That is the reading which must not treat ``created``
    as a corpse — and it is reachable by an ordinary sibling launch, not by an
    operator, which is what made this worth narrowing rather than documenting.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(exists=False)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def sibling_creates_but_has_not_started_it_yet() -> None:
        fake.exists = True
        fake.running = False
        fake.status = "created"
        fake.container_id = "cid-sibling-created"
        fake.labels = {proxy.OWNER_LABEL: "port-8003"}

    monkeypatch.setattr(backend, "_ensure_model_dir", sibling_creates_but_has_not_started_it_yet)

    with pytest.raises(RuntimeError, match=r"Refusing to replace container"):
        backend._start_container()

    assert fake.mutations == []
    assert fake.exists is True
    assert fake.container_id == "cid-sibling-created"


def test_an_inspect_without_a_state_word_still_reclaims_a_foreign_corpse(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """An unreadable state word must not be able to wedge a name.

    The status check narrows an existing removal rather than adding one, so where
    docker does not report a state word the previous rule stands — the same
    direction ``_inspect_state`` already takes when the id is unreadable (degrade to
    the name) or the whole inspect fails (read as "nothing there"). Refusing on a
    reading we cannot interpret would turn a docker oddity into a permanently
    unstartable model.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(
        labels={proxy.OWNER_LABEL: "manual"},
        exists=True,
        running=False,
        omit_status=True,
        container_id="cid-stateless",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    backend._start_container()

    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]
    assert fake.mutations[0][-1] == "cid-stateless"


def test_our_own_container_is_cleared_whatever_state_it_is_in(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The narrowing applies to *foreign* containers only.

    Refusing our own container because it is ``created`` rather than ``exited``
    would wedge this proxy's own name with nobody to take it from: a launch of ours
    that the daemon left half-made is exactly what the removal before ``docker run``
    is for. ``removing`` and ``dead`` are reclaimable for either owner — both are
    finished containers, and a name held by one is a name no proxy can use.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    for status in ("created", "removing", "dead", "exited"):
        backend = _labelled_backend(proxy, tmp_path)
        fake = _FakeDocker(
            labels={proxy.OWNER_LABEL: proxy.PROXY_OWNER, proxy.PROFILE_LABEL: "0000stale"},
            exists=True,
            running=False,
            status=status,
            container_id=f"cid-mine-{status}",
        )
        monkeypatch.setattr(proxy.subprocess, "run", fake.run)

        backend._start_container()

        verbs = [c[c.index("docker") + 1] for c in fake.mutations]
        assert verbs == ["rm", "run"], status
        assert fake.mutations[0][-1] == f"cid-mine-{status}", status


def test_foreign_container_still_loading_is_not_torn_down(monkeypatch: Any, tmp_path: Path) -> None:
    """The sharp edge: a container mid-load fails the health probe.

    DeepSeek-V4-Flash takes ~11 minutes cold. A sibling proxy asked for the same
    name eight minutes in would find it running but unhealthy, decline to adopt
    it, and fall straight through to ``docker rm -f`` — killing a load that was
    nearly done, so the first proxy's ``_wait_healthy`` raised and the model fell
    back to a paid remote route. Refuse loudly instead.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(labels={proxy.OWNER_LABEL: "port-8003"}, running=True)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    # Still loading: the backend port is not answering yet.
    monkeypatch.setattr(backend, "_backend_healthy", lambda: False)

    with pytest.raises(RuntimeError, match=r"Refusing to adopt container contended-sglang"):
        backend.ensure_running()

    assert fake.mutations == []
    assert backend.state == "stopped"


def test_sibling_container_created_during_the_download_is_not_destroyed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The ownership check must not authorise a removal minutes later.

    This is the cross-kill on the path where it is *most* likely, not least: two
    units brought up together by a reboot both find no container, so a check made
    once at the top of ``_start_container`` passes for both. Whatever the slower
    one does next takes a long time — ``_ensure_model_dir`` can be a
    several-hundred-GiB ``snapshot_download`` — and by the time it reached its
    unconditional ``docker rm -f <name>`` the faster one's container was running
    under that name. It was destroyed without its labels ever being looked at
    again, which is the exact outcome the labels were added to prevent.

    So the decision to remove has to be re-taken immediately before the removal,
    not once at the top.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(exists=False)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def sibling_wins_while_we_download() -> None:
        fake.exists = True
        fake.running = True
        fake.container_id = "cid-sibling"
        fake.labels = {proxy.OWNER_LABEL: "port-8003", proxy.PROFILE_LABEL: "siblingprofil"}

    monkeypatch.setattr(backend, "_ensure_model_dir", sibling_wins_while_we_download)

    with pytest.raises(RuntimeError, match=r"Refusing to replace container contended-sglang"):
        backend._start_container()

    # Nothing removed, nothing launched, and the sibling's backend is still up.
    assert fake.mutations == []
    assert fake.running is True
    assert fake.container_id == "cid-sibling"


def test_sibling_corpse_created_during_the_download_is_reclaimed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Re-checking must not make a foreign *corpse* unreclaimable either.

    The refusal above is gated on liveness, and the re-check has to keep that
    gate: an exited container holds no GPU and serves no traffic, and nothing in
    this module removes a foreign one, so a proxy that declined it would 502 the
    model on that node until an operator cleared the name by hand.

    Removing it names the container id the labels came from, not the name — the
    ownership decision and the removal are one docker call apart, and if the
    container judged removable is replaced even in that gap the removal has to
    miss rather than land on whatever holds the name now.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(exists=False)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def sibling_corpse_appears_while_we_download() -> None:
        fake.exists = True
        fake.running = False
        fake.container_id = "cid-sibling-corpse"
        fake.labels = {proxy.OWNER_LABEL: "manual"}

    monkeypatch.setattr(backend, "_ensure_model_dir", sibling_corpse_appears_while_we_download)

    backend._start_container()

    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]
    assert fake.mutations[0][-1] == "cid-sibling-corpse"
    assert fake.labels is not None
    assert fake.labels[proxy.OWNER_LABEL] == proxy.PROXY_OWNER


def test_losing_the_docker_run_name_race_to_a_live_sibling_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The last window is closed by docker, not by another look before the rm.

    Re-checking ownership immediately before the removal narrows the race to the
    gap between that inspect and the launch, and no amount of looking closes a
    gap. Container names are unique, though, so ``docker run --name`` fails when
    the name is taken — the one atomic claim on it available. That refusal has to
    be read as contention and send the ownership decision round again, which then
    finds the sibling alive and refuses.

    A proxy that ignored the exit status instead reported a successful start,
    never launched a backend, and left the gateway waiting on a health check that
    could not pass — while the sibling's container, the one actually holding the
    name, kept running unnoticed.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)

    def sibling_takes_the_name(fake: Any, attempt: int) -> None:
        if attempt == 1:
            fake.exists = True
            fake.running = True
            fake.container_id = "cid-sibling"
            fake.labels = {proxy.OWNER_LABEL: "port-8003"}

    fake = _FakeDocker(exists=False, on_run=sibling_takes_the_name)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    with pytest.raises(RuntimeError, match=r"Refusing to replace container contended-sglang"):
        backend._start_container()

    # One launch attempt, refused by docker; no second one, and no removal.
    assert fake.runs == 1
    assert [c[c.index("docker") + 1] for c in fake.mutations] == ["run"]
    assert fake.running is True
    assert fake.container_id == "cid-sibling"


def test_losing_the_name_race_to_a_sibling_that_then_exits_relaunches(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Contention is not a verdict: the retry re-decides, and may reclaim.

    A name conflict says only "someone got here first". If that someone has since
    exited — a benchmark container stopped, a sibling whose load OOMed — its
    corpse is reclaimable, so the retry removes it by id and launches. Failing the
    request instead would leave the model down until the next one, having proved
    only that a race existed.

    The retry also waits first. Docker frees a name as the removal completes inside
    the daemon, which can be after ``docker rm -f`` has returned, so an instant
    re-run can collide with the release it is waiting for and burn the only retry
    there is on docker's own asynchrony.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    slept: list[float] = []
    monkeypatch.setattr(proxy.time, "sleep", slept.append)

    def sibling_takes_the_name_then_dies(fake: Any, attempt: int) -> None:
        if attempt == 1:
            fake.exists = True
            fake.running = False
            fake.container_id = "cid-sibling"
            fake.labels = {proxy.OWNER_LABEL: "port-8003"}

    fake = _FakeDocker(exists=False, on_run=sibling_takes_the_name_then_dies)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    backend._start_container()

    assert fake.runs == 2
    # One pause, between the two attempts, and a real one.
    assert slept == [proxy._NAME_CONFLICT_RETRY_DELAY]
    assert proxy._NAME_CONFLICT_RETRY_DELAY > 0
    assert [c[c.index("docker") + 1] for c in fake.mutations] == ["run", "rm", "run"]
    # The removal names the corpse's id, so it cannot have been aimed at whatever
    # else might by then answer to the container name.
    assert fake.mutations[1][-1] == "cid-sibling"
    assert fake.running is True
    assert fake.labels is not None
    assert fake.labels[proxy.OWNER_LABEL] == proxy.PROXY_OWNER


def test_repeatedly_losing_the_name_race_gives_up_instead_of_looping(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A name taken twice over is a collision, not a race worth re-running.

    The retry exists for one lost race. Retrying without a bound would re-run the
    ownership decision — and, on a real host, whatever ``_ensure_model_dir`` and
    ``docker run`` cost — against a process that keeps taking the name, so the
    proxy would spin instead of reporting. Give up with a diagnosis the caller can
    turn into a 502.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)

    def always_beaten_to_it(fake: Any, attempt: int) -> None:
        fake.exists = True
        fake.running = False
        fake.container_id = f"cid-rival-{attempt}"
        fake.labels = {proxy.OWNER_LABEL: "port-8003"}

    fake = _FakeDocker(exists=False, on_run=always_beaten_to_it)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    with pytest.raises(RuntimeError, match=r"Gave up starting container") as raised:
        backend._start_container()

    assert fake.runs == 2
    # The text reaches the client through ``send_error``, which puts it in the HTTP
    # status line and encodes that latin-1: one em dash there raises
    # UnicodeEncodeError mid-response and the client sees a dropped connection
    # instead of the diagnosis.
    str(raised.value).encode("ascii")


def test_a_removal_docker_refuses_is_reported_not_blamed_on_a_sibling(
    monkeypatch: Any, tmp_path: Path, caplog: Any
) -> None:
    """A name this proxy could not free itself is not contention, and must not read as it.

    ``docker rm -f`` failing while the container survives is an ordinary docker
    failure mode — an overlay2 or cgroup mount that is busy, a removal already in
    progress, a CUDA-wedged process in D state — and a wedged backend is precisely
    when this proxy is trying to replace one. The removal's result used to be
    discarded, so the failure was never logged, and the ``docker run`` that then hit
    the container still holding the name was read as contention: the proxy gave up
    accusing "a second unit running this script with a different MODELS_CONFIG" of
    starting containers in a loop, while the container in question was its own and
    the one message that explained anything had been thrown away.

    So: report the removal failure, and diagnose the give-up from the owner just
    read rather than by assuming a sibling.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    monkeypatch.setattr(proxy.time, "sleep", lambda _s: None)
    busy = (
        'Error response from daemon: container cid-mine: driver "overlay2" failed to '
        "remove root filesystem: device or resource busy"
    )
    fake = _FakeDocker(
        labels={proxy.OWNER_LABEL: proxy.PROXY_OWNER, proxy.PROFILE_LABEL: "0000stale"},
        running=True,
        container_id="cid-mine",
    )
    # Refused by the daemon, and the container stays exactly where it was.
    fake.on_rm = lambda _f, _target: SimpleNamespace(returncode=1, stdout="", stderr=busy)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match=r"Gave up starting container") as raised,
    ):
        backend._start_container()

    message = str(raised.value)
    # The container that actually holds the name, and what docker said about it.
    assert "cid-mine" in message
    assert "device or resource busy" in message
    assert "sudo docker rm -f contended-sglang" in message
    # Not a sibling's fault, and not described as one.
    assert "second unit" not in message
    assert "in a loop" not in message
    message.encode("ascii")
    # The failure itself reaches the journal, at a level this module logs.
    failed = [r for r in caplog.records if busy in r.getMessage() and r.levelno >= logging.WARNING]
    assert failed, [r.getMessage() for r in caplog.records]
    assert proxy.log.isEnabledFor(failed[0].levelno)
    # And the backend it could not remove is still running: nothing was destroyed on
    # the strength of a removal that did not happen.
    assert fake.exists is True
    assert fake.running is True
    assert fake.container_id == "cid-mine"


def test_idle_stop_reports_a_container_it_could_not_remove(
    monkeypatch: Any, tmp_path: Path, caplog: Any
) -> None:
    """The idle path must not log "stopped" over a container that is still running.

    Its ``docker rm -f`` result was discarded too, so a removal the daemon refused
    ended in "Container … stopped." and a released ``_current_gpu`` while the
    container — and its GPUs — were still there. The next auto-selecting start then
    picks against a device that is busy for a backend nothing here believes in.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    busy = "Error response from daemon: removal of container cid-mine is already in progress"
    fake = _FakeDocker(labels=None, running=True, container_id="cid-mine")
    fake.on_rm = lambda _f, _target: SimpleNamespace(returncode=1, stdout="", stderr=busy)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    with backend._lock:
        backend._state = "ready"
        backend._current_gpu = "0"

    with caplog.at_level(logging.WARNING):
        backend._stop_container()

    # Local state is released either way: the alternative is a backend this proxy
    # thinks is ready and cannot reach.
    assert backend.state == "stopped"
    assert backend._current_gpu is None
    said = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert busy in said
    assert "NOT removed" in said
    assert "still held" in said
    # And no "stopped" claim over a live container.
    assert "Container contended-sglang stopped." not in [r.getMessage() for r in caplog.records]


def test_replacing_our_own_stale_container_removes_it_by_id_and_keeps_its_gpu(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The ordinary replace path must survive both halves of the race fix.

    Removing by id rather than by name is what keeps a stale removal from landing
    on a stranger; it must not cost the container we actually mean to replace. And
    the GPU reuse has to keep working through the extra ownership inspect: the
    outgoing container's device request is still read while it is running, so an
    auto-selecting backend does not migrate off the device it already holds (which
    ``nvidia-smi`` reports as busy precisely because that container is on it).
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(
        proxy, tmp_path, name="autoselect", gpu_index=None, colocate_group="primary"
    )
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
        gpu="1",
        container_id="cid-mine",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def fail_pick(exclude: set[str] | None = None) -> str:
        raise AssertionError("must not auto-select while holding a device of its own")

    monkeypatch.setattr(proxy, "_pick_free_gpu", fail_pick)

    backend._start_container()

    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]
    assert fake.mutations[0][-1] == "cid-mine"
    launched = fake.mutations[1]
    assert launched[launched.index("--gpus") + 1] == "device=1"
    assert backend._current_gpu == "1"
    # Still read before the removal, or real docker answers nothing and the
    # backend migrates.
    read_devices = fake.index_of(lambda c: any("DeviceRequests" in tok for tok in c))
    removed = fake.index_of(lambda c: "docker" in c and c[c.index("docker") + 1] == "rm")
    assert 0 <= read_devices < removed, fake.commands


@pytest.mark.parametrize(
    "stderr",
    [
        'docker: Error response from daemon: could not select device driver "" with '
        "capabilities: [[gpu]].",
        "docker: Error response from daemon: driver failed programming external "
        "connectivity on endpoint contended-sglang: Error starting userland proxy: "
        "listen tcp4 0.0.0.0:18099: bind: address already in use.",
    ],
    ids=["no-gpu-driver", "port-already-in-use"],
)
def test_an_unrelated_docker_run_failure_is_not_read_as_contention(
    monkeypatch: Any, tmp_path: Path, caplog: Any, stderr: str
) -> None:
    """A failed launch is a failed launch, and must be reported as one.

    Every ``docker run`` refusal shares one exit status (125), so contention is
    told apart by what docker says, and the phrase has to be attributed to a
    container *name*: a published-port collision says "address already in use"
    about a socket, and reading that as contention would retry a launch that
    cannot succeed and then blame a sibling proxy that does not exist.

    The reason is also logged, which ``check=True`` could not do — it captured the
    output into a ``CalledProcessError`` whose text carries only the exit status,
    so the one line saying *why* the backend never started was thrown away.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(
        exists=False,
        on_run=lambda _fake, _attempt: SimpleNamespace(returncode=125, stdout="", stderr=stderr),
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    with pytest.raises(proxy.subprocess.CalledProcessError):
        backend._start_container()

    # Not retried, so not mistaken for a lost race, and nothing was removed.
    assert fake.runs == 1
    assert [c[c.index("docker") + 1] for c in fake.mutations] == ["run"]
    logged = [r for r in caplog.records if stderr in r.getMessage()]
    assert logged, [r.getMessage() for r in caplog.records]
    assert proxy.log.isEnabledFor(logged[0].levelno), logging.getLevelName(logged[0].levelno)


def test_idle_stop_leaves_another_proxys_container_running(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The idle timer measures this process's traffic, not the owner's.

    So an idle expiry here says nothing about whether the owning proxy is
    streaming from that container right now. Drop local state; leave the
    container to its owner's own watcher.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(labels={proxy.OWNER_LABEL: "port-8003"})
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    with backend._lock:
        backend._state = "ready"
        backend._current_gpu = "0"

    backend._stop_container()

    assert fake.mutations == []
    assert backend.state == "stopped"
    assert backend._current_gpu is None


def test_idle_stop_misses_a_container_replaced_since_the_label_read(
    monkeypatch: Any, tmp_path: Path, caplog: Any
) -> None:
    """The idle path can hold a stale reading too, and it removes things.

    Its premise is that this proxy's own backend has gone quiet, and that reading
    can be stale in the same way the start path's was: the container may have died
    minutes ago and been reclaimed by a sibling, which then launched its own under
    the same name. A ``docker rm -f <name>`` there destroys a live foreign backend
    on the strength of labels that never described it — the same cross-kill,
    arriving through the other door.

    Naming the id the labels came from makes the removal miss instead: docker
    answers "no such container" and the sibling's backend keeps running.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    # Our own container, already dead — what the idle watcher finds for a backend
    # it still believes is ready.
    fake = _FakeDocker(labels=None, running=False, container_id="cid-mine")

    def sibling_reclaims_it_before_the_removal(f: Any, _target: str) -> None:
        f.exists = True
        f.running = True
        f.container_id = "cid-sibling"
        f.labels = {proxy.OWNER_LABEL: "port-8003"}

    fake.on_rm = sibling_reclaims_it_before_the_removal
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    with backend._lock:
        backend._state = "ready"
        backend._current_gpu = "0"

    with caplog.at_level(logging.WARNING):
        backend._stop_container()

    # Local state released either way — that is all this path owes the GPU.
    assert backend.state == "stopped"
    assert backend._current_gpu is None
    # And the sibling's freshly launched backend is untouched.
    assert fake.running is True
    assert fake.container_id == "cid-sibling"
    assert [c[-1] for c in fake.mutations] == ["cid-mine"]
    # "No such container" is this design working, not a failure to report: aiming
    # the removal at an id is what makes a replaced container get missed. Reporting
    # it as a failed removal would put a permanent error in the journal of every
    # deployment whose idle timer fires on an already-dead backend.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [], (
        "a missed removal must not be reported as a failure"
    )


def test_unlabelled_container_counts_as_this_proxys_own(monkeypatch: Any, tmp_path: Path) -> None:
    """UPGRADE RULE: no owner label means "mine", never "someone else's".

    Every container running at the moment this ships is unlabelled. Reading an
    absent label as foreign would make the idle watcher refuse to remove any of
    them, so GPUs would stay held long past ``IDLE_TIMEOUT`` on every deployment
    that upgrades without a cold start.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(labels=None, container_id="cid-legacy")
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    assert backend._container_owner() is None

    backend._stop_container()

    # Removed by the *id* the ownership inspect returned, not by the name: the idle
    # timer can fire on a container that has since been replaced by another proxy
    # reclaiming its corpse, and a removal keyed on the name would take that
    # proxy's live backend down on the strength of labels that never described it.
    removed = [c for c in fake.mutations if c[c.index("docker") + 1] == "rm"]
    assert removed and removed[0][-1] == "cid-legacy"
    assert not fake.exists


def test_own_labelled_container_is_still_adopted_on_restart(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Round-trip: the labels this proxy stamps must read back as its own.

    Restart-and-adopt exists so a proxy restart does not reload weights, and it
    has to survive labelling. Feed the labels straight from the run command back
    through the inspect path.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    stamped = _labels_from_run(["docker", "run", *backend._ownership_label_args()])
    fake = _FakeDocker(labels=stamped, running=True, gpu="2")
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)

    assert backend._adopt_running_container() is True
    assert backend._current_gpu == "2"
    assert fake.mutations == []


def test_own_container_from_a_changed_profile_is_replaced_not_refused(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A profile edit must still take effect on the next start.

    Same owner, different profile hash means "my own config changed on disk"
    (the next ``mem_fraction`` bump, a new ``sglang_image``) — replace the
    container rather than adopt a backend running the previous config. Refusing
    here would wedge every ordinary config change.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)

    # Healthy and running, but not adopted: the config it was launched from is gone.
    assert backend._adopt_running_container() is False

    # And replacing it is allowed — no ForeignContainerError for my own container.
    backend._start_container()
    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]


def test_replacing_own_stale_container_keeps_the_gpu_it_holds(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """An auto-selecting replacement must not migrate to a different device.

    The replacement is resolved while the container it replaces is *still
    running*: ``_start_container`` reads the GPU before the ``docker rm -f``, so
    the backend is down for one docker call rather than for a whole
    ``_ensure_model_dir`` download. That ordering is what makes auto-selection
    wrong here — it asks nvidia-smi for the least-used device and nvidia-smi
    reports the current one as busy, because the container being replaced is
    holding it. So a one-key ``mem_fraction`` edit silently moved the backend to
    another GPU: off the partner it shares a ``colocate_group`` with, or onto a
    device an idle-stopped model is pinned to, which then OOMs when that model
    wakes.

    Both real auto-selecting profiles omit ``gpu_index`` and share a
    ``colocate_group`` (``models.json``, ``models.rtx6000.json``), so this is the
    ordinary case on those nodes, not an exotic one.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(
        proxy, tmp_path, name="autoselect", gpu_index=None, colocate_group="primary"
    )
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
        gpu="1",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def fail_pick(exclude: set[str] | None = None) -> str:
        raise AssertionError("must not auto-select while holding a device of its own")

    monkeypatch.setattr(proxy, "_pick_free_gpu", fail_pick)

    # Healthy but stale, so it is replaced rather than adopted.
    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)
    assert backend._adopt_running_container() is False

    backend._start_container()

    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]
    launched = fake.mutations[1]
    assert launched[launched.index("--gpus") + 1] == "device=1"
    # And recorded, so a colocate partner starting later follows it onto GPU 1.
    assert backend._current_gpu == "1"
    # The ordering is the fix, so pin it directly: the device request is read
    # while the container still exists. Move that read below the `docker rm -f`
    # and real docker answers nothing, `replacing_gpu` is None, and the backend
    # migrates — with every assertion above still satisfied by a fake that kept
    # answering after the removal.
    read_devices = fake.index_of(lambda c: any("DeviceRequests" in tok for tok in c))
    removed = fake.index_of(lambda c: "docker" in c and c[c.index("docker") + 1] == "rm")
    assert 0 <= read_devices < removed, fake.commands


def test_replacing_a_tensor_parallel_container_keeps_all_of_its_gpus(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The device read-back must round-trip a multi-GPU list, not concatenate it.

    ``--gpus '"device=2,3"'`` is stored by docker as one device request whose
    ``DeviceIDs`` is ``["2", "3"]`` — that single-request parse is the entire
    reason ``_docker_gpu_arg`` quotes the list. Reading it back through a Go
    template that ranged over the ids emitted them with no separator, so a
    two-GPU container came back as the token ``"23"``, and both halves of the
    reuse then broke:

    * the device-count guard saw ``len("23".split(",")) == 1 != 2`` and skipped
      reuse, so a ``tensor_parallel_size: 2`` model with no ``gpu_index`` — the
      shape the README documents and ``_pick_free_gpus`` exists to serve —
      auto-selected while the outgoing container still held its devices, and
      migrated. That is the exact bug the reuse was added to prevent.
    * with ``tp`` edited back down to 1 the count matched by accident, so ``"23"``
      was reused verbatim: ``--gpus device=23`` names a device no host has, and
      ``docker run`` fails *after* the ``docker rm -f`` has destroyed the working
      backend.

    Nothing shipped today hits the first case — ``h200_idle_proxy/models.json``
    is the only multi-device profile and it pins ``gpu_index: "2,3"`` — but
    dropping that pin to let the H200 auto-place is a one-key edit.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path, name="tp2", gpu_index=None, tensor_parallel_size=2)
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
        gpu="2,3",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    def fail_pick(exclude: set[str] | None = None) -> str:
        raise AssertionError("must not auto-select while holding devices of its own")

    monkeypatch.setattr(proxy, "_pick_free_gpu", fail_pick)

    backend._start_container()

    launched = fake.mutations[-1]
    # Quoted, because docker splits an unquoted comma list into separate requests.
    assert launched[launched.index("--gpus") + 1] == '"device=2,3"'
    # Recorded in the form the exclusion set splits on, so another auto-selecting
    # backend skips GPU 2 *and* GPU 3 rather than looking for a device "23".
    assert backend._current_gpu == "2,3"


def test_shrinking_tensor_parallel_size_reselects_instead_of_reusing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A device-count change must re-select: the old list no longer fits.

    Editing ``tensor_parallel_size`` from 2 down to 1 leaves a two-device
    container to replace and a one-device shape to launch, so the count guard has
    to reject the old list. The failure mode it protects against is not a
    misplacement but a hard one: reusing ``"2,3"`` for a single-GPU launch hands
    ``docker run`` two devices for a backend that will only shard across one,
    and reusing the concatenated ``"23"`` the old template produced named no
    device at all — either way after the ``docker rm -f`` already took the
    working backend down.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path, name="tp2", gpu_index=None, tensor_parallel_size=1)
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
        gpu="2,3",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(proxy, "_pick_free_gpu", lambda exclude=None: "1")

    backend._start_container()

    launched = fake.mutations[-1]
    assert launched[launched.index("--gpus") + 1] == "device=1"
    assert backend._current_gpu == "1"


def test_a_container_without_a_device_request_reads_back_as_no_gpu(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """``--gpus all`` (or no ``--gpus``) yields no id to reuse, so re-select.

    ``docker inspect`` reports an empty ``DeviceRequests`` as ``null`` and a
    count-based request with ``DeviceIDs: null``; neither names a device, so the
    read-back must answer ``None`` rather than an empty or malformed list that
    would reach ``docker run``.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path, name="nodevice", gpu_index=None)
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
        gpu="",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    assert backend._running_container_gpu() is None

    monkeypatch.setattr(proxy, "_pick_free_gpu", lambda exclude=None: "0")
    backend._start_container()

    launched = fake.mutations[-1]
    assert launched[launched.index("--gpus") + 1] == "device=0"


def test_adopting_a_tensor_parallel_container_records_every_device(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Adoption's GPU bookkeeping has to survive a multi-device backend too.

    ``_current_gpu`` is split on commas to build the exclusion set every other
    auto-selecting backend consults, so an adopted TP backend that recorded the
    concatenated ``"23"`` excluded neither GPU 2 nor GPU 3 and invited a second
    backend onto both.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path, name="tp2adopt", tensor_parallel_size=2)
    stamped = _labels_from_run(["docker", "run", *backend._ownership_label_args()])
    fake = _FakeDocker(labels=stamped, running=True, gpu="2,3")
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)

    assert backend._adopt_running_container() is True
    assert backend._current_gpu == "2,3"
    assert fake.mutations == []


def test_colocation_partner_outranks_the_replaced_containers_gpu(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Keeping the old device must not outrank the group it belongs to.

    Ranking guard for the rule above, not a second bug: a ``colocate_group``
    member that has already resolved a device *is* the placement the whole group
    shares, so a backend whose own previous container sat elsewhere has to follow
    the partner rather than pull the group apart — the very outcome the GPU reuse
    exists to prevent.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    partner = proxy.BackendManager(
        "partner-model",
        {"container": "partner", "model_dir": "/tmp/partner", "colocate_group": "primary"},
    )
    with partner._lock:
        partner._state = "ready"
    partner._current_gpu = "0"
    stale = _labelled_backend(
        proxy, tmp_path, name="autoselect", gpu_index=None, colocate_group="primary"
    )
    proxy._backends = {"partner-model": partner, MODEL_NAME: stale}
    fake = _FakeDocker(
        labels={
            proxy.OWNER_LABEL: proxy.PROXY_OWNER,
            proxy.PROFILE_LABEL: "0000staleprofile",
        },
        running=True,
        gpu="1",
    )
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    stale._start_container()

    verbs = [c[c.index("docker") + 1] for c in fake.mutations]
    assert verbs == ["rm", "run"]
    launched = fake.mutations[1]
    assert launched[launched.index("--gpus") + 1] == "device=0"
    assert stale._current_gpu == "0"


def test_adoption_reads_liveness_and_ownership_in_one_inspect(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Both facts come from one ``docker inspect``, and the count is the point.

    Two inspects cost two daemon round trips on the cold-start path — where a
    storm of retries arrives while a multi-minute load runs — and left a window in
    which the container could stop between "is it running?" and "whose is it?",
    long enough to read a live foreign container as an exited one.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    stamped = _labels_from_run(["docker", "run", *backend._ownership_label_args()])
    fake = _FakeDocker(labels=stamped, running=True, gpu="2")
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(backend, "_backend_healthy", lambda: True)

    assert backend._adopt_running_container() is True
    assert backend._current_gpu == "2"

    assert len(fake.state_inspects) == 1


def test_contention_check_costs_one_inspect_per_streaming_request(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The warmup path's contention check must ask docker once, not twice.

    It runs on every streaming request until the backend is ready, so a cold-start
    storm multiplies it. The *uncontended* case only ever cost one call — ``owner
    is None`` short-circuits the liveness check — so the count to pin is the
    contended one, which used to inspect for labels and then again for state.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    fake = _FakeDocker(labels={proxy.OWNER_LABEL: "port-8003"}, running=True)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)

    assert backend.contention_error() is not None

    assert len(fake.state_inspects) == 1


def test_retuning_the_startup_estimate_keeps_the_container_adopted(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A config key that never reaches ``docker run`` must not force a reload.

    ``startup_estimate_seconds`` only words the warmup SSE banner, and exists to
    be re-tuned against measured cold-start times — it is set to 840 on the live
    H200 profile for exactly that reason. Hashing the whole config would make that
    one-key edit plus the routine ``systemctl restart`` ``docker rm -f`` a healthy,
    serving DeepSeek backend and pay a ~14-minute weight reload, during which the
    model fails over to its paid remote route. Nothing about the container is
    stale. Same for the ``hf_*`` keys, which only steer the download.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    running = _labelled_backend(proxy, tmp_path)

    for cosmetic in (
        {"startup_estimate_seconds": 840},
        {"hf_repo": "deepseek-ai/DeepSeek-V4-Flash-0731"},
        {"hf_revision": "main"},
        {"hf_ignore_patterns": ["*.onnx"]},
    ):
        edited = _labelled_backend(proxy, tmp_path, **cosmetic)
        assert edited._profile == running._profile, cosmetic

    # A launch-affecting edit still invalidates it, or the label would be useless.
    bumped = _labelled_backend(proxy, tmp_path, mem_fraction="0.70")
    assert bumped._profile != running._profile

    # End to end: the re-tuned config adopts the container the old one launched.
    stamped = _labels_from_run(["docker", "run", *running._ownership_label_args()])
    retuned = _labelled_backend(proxy, tmp_path, startup_estimate_seconds=900)
    fake = _FakeDocker(labels=stamped, running=True, gpu="2")
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(retuned, "_backend_healthy", lambda: True)

    assert retuned._adopt_running_container() is True
    assert fake.mutations == []


@pytest.mark.parametrize("stream", [False, True])
def test_contended_container_surfaces_as_502_naming_both_owners(
    monkeypatch: Any, tmp_path: Path, stream: bool
) -> None:
    """A collision must be diagnosable from the response, not just the journal.

    The alternative is what happens today: the two proxies take turns destroying
    each other's container and the client sees an opaque startup timeout.

    ``stream: true`` is the shape that matters most and the one that used to lose
    the diagnosis entirely. A streaming chat request on a not-ready backend takes
    the warmup branch, which commits ``200`` and the "starting up" banner *before*
    ``ensure_running`` runs on a background thread — and ``ForeignContainerError``
    is a ``RuntimeError``, so that thread only logged it. Every retry answered 200,
    so the gateway saw success, never opened a circuit and never failed over: the
    endpoint stalled indefinitely behind "please wait about 14 minutes".

    Also pins the message to ASCII. ``send_error`` puts it in the HTTP status
    line, which is encoded latin-1, so a single em dash in the text raises
    ``UnicodeEncodeError`` mid-response and the client gets a dropped connection
    rather than the diagnosis.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]
    fake = _FakeDocker(labels={proxy.OWNER_LABEL: "port-8003"}, running=True)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    monkeypatch.setattr(backend, "_backend_healthy", lambda: False)

    body_json: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "ping"}],
    }
    if stream:
        body_json["stream"] = True

    with _serve(proxy.ProxyHandler) as proxy_port:
        status, _, body = _request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            method="POST",
            headers={"Authorization": "Bearer manual-secret"},
            body=body_json,
        )

    assert status == 502
    assert b"port-8003" in body
    assert proxy.PROXY_OWNER.encode() in body
    # Never the success-shaped warmup banner, which the gateway reads as healthy.
    assert b'"id":"warmup"' not in body
    assert fake.mutations == []


def test_contention_check_reports_what_the_start_path_will_refuse(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The pre-flight check has to agree with the start path, or the 502 is lost.

    ``contention_error`` runs before a streaming response commits its ``200``,
    because ``ensure_running`` then raises on a background thread that can only
    log. So anything the start path refuses must be reported here too: a foreign
    container stuck in ``created`` — its launcher died between docker's create and
    its start — is refused there, and answering "no contention" for it would put
    the endpoint back to answering every request success-shaped forever.

    A foreign *corpse* is the opposite case and must stay silent: the start path
    reclaims it, so reporting it would 502 a request that was going to succeed.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = _labelled_backend(proxy, tmp_path)
    foreign = {proxy.OWNER_LABEL: "port-8003"}

    created = _FakeDocker(labels=foreign, exists=True, running=False, status="created")
    monkeypatch.setattr(proxy.subprocess, "run", created.run)
    error = backend.contention_error()
    assert error is not None
    assert "'created'" in str(error)
    str(error).encode("ascii")

    corpse = _FakeDocker(labels=foreign, exists=True, running=False, status="exited")
    monkeypatch.setattr(proxy.subprocess, "run", corpse.run)
    assert backend.contention_error() is None


def test_warmup_stream_survives_an_unreadable_docker_inspect(
    monkeypatch: Any, tmp_path: Path, caplog: Any
) -> None:
    """A docker hiccup must not 502 every streaming request — but must be visible.

    The contention check runs on the hot warmup path, so it has to fail towards
    "no collision": a ``docker inspect`` that cannot be run is not evidence that
    someone else owns the container, and treating it as such would take a model
    offline for the duration of a transient problem.

    Failing that way is indistinguishable from "nobody else owns this" in the
    return value, though, and the causes are not transient hiccups only: a proxy
    that cannot execute ``sudo docker`` at all answers "no contention" for every
    request it will ever serve. So the reason is logged — and deliberately *not*
    captured at a forced level here. The module configures the root handler at
    ``INFO`` and ships no level knob, so a ``debug`` record would be dropped
    before it reached the journal and this test would pass while the operator it
    exists for saw nothing. Asserting that the module's own logger is enabled for
    the record's level is what makes the coverage real.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    backend = proxy._backends[MODEL_NAME]

    def boom(*_: Any, **__: Any) -> None:
        raise OSError("cannot connect to the Docker daemon")

    monkeypatch.setattr(proxy.subprocess, "run", boom)

    assert backend.contention_error() is None

    logged = [r for r in caplog.records if "cannot connect to the Docker daemon" in r.getMessage()]
    assert logged, [r.getMessage() for r in caplog.records]
    assert "OSError" in logged[0].getMessage()
    assert proxy.log.isEnabledFor(logged[0].levelno), logging.getLevelName(logged[0].levelno)

    # Latched: the check runs on every streaming request until the backend is
    # ready, and a proxy that cannot run docker at all fails identically every
    # time, so one line per request would bury the journal.
    caplog.clear()
    assert backend.contention_error() is None
    assert caplog.records == []

    # An inspect that succeeds re-arms it, so a genuinely intermittent failure is
    # reported again rather than silenced for the life of the process.
    fake = _FakeDocker(labels=None, running=False)
    monkeypatch.setattr(proxy.subprocess, "run", fake.run)
    assert backend.contention_error() is None
    monkeypatch.setattr(proxy.subprocess, "run", boom)
    assert backend.contention_error() is None
    assert [r.getMessage() for r in caplog.records]


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


def test_detect_profile_selects_the_dedicated_h200_profile(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A 4x-H200 box resolves to the dedicated profile, not a copy of it.

    There is one DeepSeek deployment on such a box, so there must be one file
    describing it. A local ``models.h200.json`` mirror is what let the two
    definitions drift to FP8/PP=3 against NVFP4/TP=2 before #1185 reconverged
    them; pointing the detector at the file ``h200_idle_proxy.service`` pins
    makes agreement structural rather than something a partial test guards.
    """
    from pathlib import Path as _Path

    proxy = _load_proxy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy.subprocess,
        "run",
        lambda *a, **k: _gpu_query_result("NVIDIA H200\nNVIDIA H200\nNVIDIA H200\nNVIDIA H200\n"),
    )
    script_dir = _Path(proxy.__file__).resolve().parent
    resolved = proxy._detect_profile_config()
    assert resolved == script_dir.parent / "h200_idle_proxy" / "models.json"
    assert resolved.is_file()
    # No stale duplicate left behind for the next reader to edit by mistake.
    assert not (script_dir / "models.h200.json").exists()


def test_h200_profile_uses_tp2_on_gpus_2_and_3() -> None:
    """Canonical H200 profile shards DeepSeek-V4-Flash-0731 with TP=2 on GPUs 2,3.

    The earlier profile needed PP=3 across GPUs 0,2,3 because 273 GiB of FP8
    weights do not fit at TP=2 (and TP=3 is illegal — 64 attention heads are not
    divisible by 3). The 0731 release ships FP4 experts at ~156 GiB, so it fits on
    two GPUs and frees a third. This is the profile ``h200_idle_proxy.service``
    pins and the one hardware detection resolves to, so it is what actually runs
    on h200a/h200b by either route.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ops"
    profile = json.loads((root / "h200_idle_proxy" / "models.json").read_text())

    model = profile["deepseek-v4-flash"]
    assert model["tensor_parallel_size"] == 2
    assert "pipeline_parallel_size" not in model
    assert model["gpu_index"] == "2,3"
    # DSpark needs the official checkpoint: the NVFP4 conversion excludes mtp.*, so
    # the draft-expert scales are dropped at load and accept length collapses to 1.0.
    assert model["hf_repo"] == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert model["speculative_algorithm"] == "DSPARK"
    assert model["moe_runner_backend"] == "marlin"


def test_h200_profile_uses_deepseek_v4_parsers() -> None:
    """The H200 config must use the DeepSeek-V4 parser pairing.

    The ``deepseek-r1`` reasoning parser assumes the whole generation is
    reasoning until a ``</think>`` close tag; requests here do not enable
    thinking, so that tag never appears and every completion returned empty
    ``content`` with the full answer classified as ``reasoning_content``.
    The ``deepseekv3`` tool-call parser does not recognize V4's DSML tool-call
    markup, so ``tool_calls`` stays null and agentic clients cannot run tools.
    """
    import json
    from pathlib import Path

    cfg_path = Path(__file__).resolve().parents[1] / "ops" / "h200_idle_proxy" / "models.json"
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


# ── LOCAL_API_KEY fails closed ─────────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_local_api_key_still_enforces_auth(
    monkeypatch: Any, tmp_path: Path, blank: str
) -> None:
    """A placeholder ``LOCAL_API_KEY=`` line must not turn request auth off.

    The systemd unit reads the gateway's whole ``.env``, where a bare ``KEY=``
    placeholder is how every other credential in ``.env.example`` is written.
    Read with a two-argument ``os.environ.get(name, default)`` that line is a
    real assignment of ``""`` -- the default never applies -- and an empty key
    used to mean "serve everyone": a root-run listener on 0.0.0.0 that starts and
    stops GPU containers over the Docker socket, reachable through the reverse
    tunnel, with auth off and nothing in the log saying so.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_API_KEY", blank)
    sys.modules.pop("ops.local_deployment_proxy.local_deployment_proxy", None)
    proxy = importlib.import_module("ops.local_deployment_proxy.local_deployment_proxy")

    assert proxy.LOCAL_API_KEY == "freeinference_api"

    with _serve(proxy.ProxyHandler) as proxy_port:
        unauthenticated, _, _ = _request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            method="POST",
            body={"model": MODEL_NAME, "messages": [{"role": "user", "content": "hi"}]},
        )
        wrong_key, _, _ = _request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            method="POST",
            body={"model": MODEL_NAME, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer not-the-key"},
        )

    assert unauthenticated == 401
    assert wrong_key == 401
