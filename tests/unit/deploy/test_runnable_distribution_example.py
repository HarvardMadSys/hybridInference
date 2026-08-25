"""Contracts for the public, runnable distribution example."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from routing.executor import RouteExecutor
from serving.config.distribution import load_distribution_config
from serving.servers.registry import register_from_models_yaml

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "examples" / "distributions" / "example"
BASE_COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
EXAMPLE_COMPOSE = EXAMPLE / "deploy" / "docker-compose.yml"
ACTIVE_CI = REPO / ".github" / "workflows" / "ci.yml"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_dry_run(*args: str, cwd: Path = REPO) -> str:
    env = os.environ.copy()
    env.pop("DISTRIBUTION", None)
    proc = subprocess.run(
        ["make", "--no-print-directory", "-n", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_example_manifest_and_config_are_self_contained() -> None:
    config = load_distribution_config(EXAMPLE / "distribution.yaml")
    assert config.distribution.id == "example"
    assert config.site.public_base_url == "http://localhost:8080"
    assert Path(config.paths.models) == (EXAMPLE / "config" / "models.yaml").resolve()
    assert Path(config.paths.routing) == (EXAMPLE / "config" / "routing.yaml").resolve()

    models_text = (EXAMPLE / "config" / "models.yaml").read_text()
    assert "${VAR:-default}" not in models_text
    models = yaml.safe_load(models_text)["models"]
    route = models[0]["route"][0]
    assert models[0]["id"] == "example-chat"
    assert "stream" not in models[0]["supported_params"]
    assert route["base_url"] == "${EXAMPLE_UPSTREAM_BASE_URL}"
    assert route["api_key"] == "${EXAMPLE_UPSTREAM_API_KEY}"
    assert route["provider_model_id"] == "${EXAMPLE_UPSTREAM_MODEL}"


def test_manifest_only_reference_has_no_dangling_config_paths() -> None:
    reference = load_distribution_config(REPO / "config" / "examples" / "distribution.example.yaml")
    assert Path(reference.paths.models).is_file()
    assert Path(reference.paths.routing).is_file()

    readme = (REPO / "README.md").read_text()
    assert "make up DISTRIBUTION=example" in readme
    assert "make smoke DISTRIBUTION=example" in readme


def test_example_registry_loads_with_its_fake_upstream_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_UPSTREAM_BASE_URL", "http://example-provider:8351")
    monkeypatch.setenv("EXAMPLE_UPSTREAM_API_KEY", "example-local-key")
    monkeypatch.setenv("EXAMPLE_UPSTREAM_MODEL", "example-upstream")

    router = RouteExecutor()
    count, infos = register_from_models_yaml(router, EXAMPLE / "config" / "models.yaml")

    assert count == 1
    assert [info.model_id for info in infos] == ["example-chat"]
    adapter = router.routes["example-chat"].adapters[0][0]
    assert adapter.config.base_url == "http://example-provider:8351"
    assert adapter.config.api_key == "example-local-key"
    assert adapter.config.provider_model_id == "example-upstream"


def test_example_compose_resolves_public_paths_and_forwards_upstream_env() -> None:
    base = yaml.safe_load(BASE_COMPOSE.read_text())
    override = yaml.safe_load(EXAMPLE_COMPOSE.read_text())
    backend = override["services"]["backend"]

    assert override["name"] == "hybridinference-example"
    assert backend["container_name"] == (
        "${EXAMPLE_BACKEND_CONTAINER_NAME:-hybridinference-example-backend}"
    )
    assert override["services"]["example-provider"]["container_name"] == (
        "${EXAMPLE_PROVIDER_CONTAINER_NAME:-hybridinference-example-provider}"
    )
    assert base["services"]["backend"]["ports"] == [
        "${BACKEND_HOST:-127.0.0.1}:${BACKEND_PORT:-8080}:8080"
    ]
    assert base["services"]["backend"]["env_file"] == "${BACKEND_ENV_FILE:-../../.env}"
    fake_build = override["services"]["example-provider"]["build"]
    fake_context = (BASE_COMPOSE.parent / fake_build["context"]).resolve()
    assert fake_context == REPO
    assert (fake_context / fake_build["dockerfile"]).resolve() == (
        REPO / "examples" / "support" / "Dockerfile.openai-compat-fake"
    )
    backend_dockerfile = (REPO / "deploy" / "docker" / "Dockerfile.backend").read_text()
    assert "COPY examples/distributions/example/distribution.yaml" in backend_dockerfile
    assert "COPY examples/distributions/example/config/" in backend_dockerfile
    assert "COPY examples/ examples/" not in backend_dockerfile

    environment = backend["environment"]
    for name in (
        "EXAMPLE_UPSTREAM_BASE_URL",
        "EXAMPLE_UPSTREAM_API_KEY",
        "EXAMPLE_UPSTREAM_MODEL",
    ):
        assert environment[name] == f"${{{name}}}"
    assert environment["DB_ENABLED"] == "false"
    assert environment["USER_AUTH_ENABLED"] == "false"


def test_shell_can_override_the_example_upstream_in_compose() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")

    env = os.environ.copy()
    env.update(
        {
            "BACKEND_PORT": "18080",
            "EXAMPLE_UPSTREAM_BASE_URL": "https://api.example.test/v1",
            "EXAMPLE_UPSTREAM_API_KEY": "explicit-shell-key",
            "EXAMPLE_UPSTREAM_MODEL": "real-upstream-model",
        }
    )
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(EXAMPLE_COMPOSE),
            "--env-file",
            str(EXAMPLE / "deploy" / "backend.env"),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 and "compose is not a docker command" in proc.stderr.lower():
        pytest.skip("Docker Compose is not installed")
    assert proc.returncode == 0, proc.stderr
    rendered = json.loads(proc.stdout)
    assert rendered["name"] == "hybridinference-example"
    assert rendered["services"]["backend"]["container_name"] == ("hybridinference-example-backend")
    assert rendered["services"]["backend"]["ports"][0]["published"] == "18080"
    backend_env = rendered["services"]["backend"]["environment"]
    assert backend_env["EXAMPLE_UPSTREAM_BASE_URL"] == "https://api.example.test/v1"
    assert backend_env["EXAMPLE_UPSTREAM_API_KEY"] == "explicit-shell-key"
    assert backend_env["EXAMPLE_UPSTREAM_MODEL"] == "real-upstream-model"


def test_example_checked_in_port_defaults_match_the_smoke_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backend.env supplies Compose's port; Make supplies the matching smoke URL."""
    for name in ("BACKEND_ENV_FILE", "BACKEND_HOST", "BACKEND_PORT", "SMOKE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    backend_env_file = EXAMPLE / "deploy" / "backend.env"
    assert "BACKEND_PORT=8080" in backend_env_file.read_text().splitlines()

    smoke = _make_dry_run("smoke", "DISTRIBUTION=example")
    assert '--base-url "http://localhost:8080"' in smoke

    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")

    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(EXAMPLE_COMPOSE),
            "--env-file",
            str(backend_env_file),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 and "compose is not a docker command" in proc.stderr.lower():
        pytest.skip("Docker Compose is not installed")
    assert proc.returncode == 0, proc.stderr
    published_port = json.loads(proc.stdout)["services"]["backend"]["ports"][0]
    assert published_port["host_ip"] == "127.0.0.1"
    assert published_port["published"] == "8080"
    assert published_port["target"] == 8080


def test_make_selection_preserves_default_and_none_semantics(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    (sandbox / "distributions" / "acme" / "deploy").mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    (sandbox / "distributions" / "acme" / "deploy" / "backend.env").write_text("SITE_NAME=Acme\n")

    default = _make_dry_run("ps", cwd=sandbox)
    assert "Using distribution 'acme'" in default
    assert "distributions/acme/deploy/backend.env" in default
    assert "examples/distributions/example/deploy/docker-compose.yml" not in default

    neutral = _make_dry_run("ps", "DISTRIBUTION=none", cwd=sandbox)
    assert "Using distribution" not in neutral
    assert "distributions/acme/deploy/backend.env" not in neutral
    assert "examples/distributions/example/deploy/docker-compose.yml" not in neutral

    makefile = (sandbox / "Makefile").read_text()
    assert (
        "DISTRIBUTION_COMPOSE_FILE := $(if $(DISTRIBUTION_PATH),"
        "$(wildcard $(DISTRIBUTION_PATH)/deploy/docker-compose.yml),)"
    ) in makefile


def test_example_make_contract_is_backend_only_and_race_free(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    (sandbox / "examples" / "distributions" / "example" / "deploy").mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    shutil.copy2(
        EXAMPLE / "deploy" / "backend.env",
        sandbox / "examples" / "distributions" / "example" / "deploy" / "backend.env",
    )
    shutil.copy2(
        EXAMPLE_COMPOSE,
        sandbox / "examples" / "distributions" / "example" / "deploy" / "docker-compose.yml",
    )
    (sandbox / ".env").write_text("EXAMPLE_UPSTREAM_MODEL=must-not-win\n")

    output = _make_dry_run("up", "DISTRIBUTION=example", cwd=sandbox)
    commands = [line for line in output.splitlines() if line.startswith("docker compose")]
    assert len(commands) == 2
    assert commands[0].endswith("up -d --wait example-provider")
    assert commands[1].endswith("up -d --no-deps backend")
    assert "--env-file .env" not in output
    assert "docker volume" not in output

    build = _make_dry_run("build", "DISTRIBUTION=example", cwd=sandbox)
    assert "up -d --wait example-provider" in build
    assert "up -d --build --no-deps backend" in build
    assert "docker volume" not in build

    invalid_build = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "build",
            "DISTRIBUTION=example",
            "s=frontend",
        ],
        cwd=sandbox,
        env={key: value for key, value in os.environ.items() if key != "DISTRIBUTION"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert invalid_build.returncode != 0
    assert "The runnable example can build only backend or example-provider" in invalid_build.stderr
    assert "up -d --build --no-deps frontend" not in invalid_build.stdout

    for target in ("down", "logs"):
        target_output = _make_dry_run(target, "DISTRIBUTION=example", cwd=sandbox)
        assert "examples/distributions/example/deploy/docker-compose.yml" in target_output


def test_fake_provider_builds_a_deterministic_non_streaming_completion() -> None:
    fake = _load_module(
        "_runnable_example_fake", REPO / "examples" / "support" / "openai_compat_fake.py"
    )
    payload = {
        "model": "example-upstream",
        "messages": [{"role": "user", "content": "hello"}],
    }
    first = fake.build_completion(payload)
    second = fake.build_completion(payload)

    assert first == second
    assert first["created"] == 0
    assert first["model"] == "example-upstream"
    assert first["choices"][0]["message"]["content"] == "RUNNABLE_EXAMPLE_OK"


def test_fake_provider_builds_deterministic_openai_sse_frames() -> None:
    fake = _load_module(
        "_runnable_example_fake_stream", REPO / "examples" / "support" / "openai_compat_fake.py"
    )
    payload = {
        "model": "example-upstream",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }

    frames = fake.build_stream_frames(payload)

    assert frames == fake.build_stream_frames(payload)
    assert all(frame.startswith(b"data: ") and frame.endswith(b"\n\n") for frame in frames)
    assert frames[-1] == b"data: [DONE]\n\n"

    role, content, finish = [
        json.loads(frame.removeprefix(b"data: ").removesuffix(b"\n\n")) for frame in frames[:-1]
    ]
    assert role["id"] == content["id"] == finish["id"] == "chatcmpl-runnable-example"
    assert role["object"] == content["object"] == finish["object"] == "chat.completion.chunk"
    assert role["created"] == content["created"] == finish["created"] == 0
    assert role["model"] == content["model"] == finish["model"] == "example-upstream"
    assert role["choices"] == [
        {
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }
    ]
    assert content["choices"] == [
        {
            "index": 0,
            "delta": {"content": "RUNNABLE_EXAMPLE_OK"},
            "finish_reason": None,
        }
    ]
    assert finish["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


def test_fake_provider_stream_response_uses_sse_headers_and_flushes_each_frame() -> None:
    fake = _load_module(
        "_runnable_example_fake_handler", REPO / "examples" / "support" / "openai_compat_fake.py"
    )
    payload = {"model": "example-upstream", "stream": True}

    class RecordingWriter:
        def __init__(self) -> None:
            self.body = bytearray()
            self.flush_count = 0

        def write(self, data: bytes) -> int:
            self.body.extend(data)
            return len(data)

        def flush(self) -> None:
            self.flush_count += 1

    statuses: list[int] = []
    headers: list[tuple[str, str]] = []
    ended_headers: list[bool] = []
    writer = RecordingWriter()
    handler = object.__new__(fake.FakeHandler)
    handler.wfile = writer
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.append((name, value))
    handler.end_headers = lambda: ended_headers.append(True)

    handler._send_stream(payload)

    frames = fake.build_stream_frames(payload)
    assert statuses == [200]
    assert dict(headers) == {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    assert ended_headers == [True]
    assert handler.close_connection is True
    assert bytes(writer.body) == b"".join(frames)
    assert writer.flush_count == len(frames)


@pytest.mark.parametrize("error_type", [BrokenPipeError, ConnectionResetError])
def test_fake_provider_stream_response_ignores_client_disconnects(
    error_type: type[OSError],
) -> None:
    fake = _load_module(
        "_runnable_example_fake_disconnect",
        REPO / "examples" / "support" / "openai_compat_fake.py",
    )

    class DisconnectingWriter:
        def write(self, data: bytes) -> int:
            raise error_type

        def flush(self) -> None:
            raise AssertionError("flush must not run after a failed write")

    handler = object.__new__(fake.FakeHandler)
    handler.wfile = DisconnectingWriter()
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None

    handler._send_stream({"model": "example-upstream", "stream": True})

    assert handler.close_connection is True


def test_active_ci_runs_the_documented_example_contract() -> None:
    """Works in both trees: public export replaces this same workflow path."""
    workflow = ACTIVE_CI.read_text()
    readme = (EXAMPLE / "README.md").read_text()
    for command in (
        "make up DISTRIBUTION=example",
        "make smoke DISTRIBUTION=example",
        "make down DISTRIBUTION=example",
    ):
        assert command in workflow
        assert command in readme
    assert 'BACKEND_PORT: "0"' in workflow
    assert "github.run_id" in workflow
    assert "docker image rm" in workflow
