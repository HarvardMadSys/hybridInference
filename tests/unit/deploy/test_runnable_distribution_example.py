"""Contracts for the public, runnable distribution example."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from routing.executor import RouteExecutor
from serving.config.distribution import load_distribution_config
from serving.servers.registry import register_from_models_yaml

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "distributions" / "example"
FAKE_PROVIDER = EXAMPLE / "fixtures" / "fake-openai-provider"
BASE_COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
EXAMPLE_COMPOSE = EXAMPLE / "deploy" / "docker-compose.yml"
DEMO_COMPOSE = EXAMPLE / "deploy" / "docker-compose.demo.yml"
DEMO_MANIFEST = EXAMPLE / "distribution.demo.yaml"
FULL_SMOKE = EXAMPLE / "full_smoke.py"
ACTIVE_CI = REPO / ".github" / "workflows" / "ci.yml"
TUTORIAL = REPO / "docs" / "developer" / "router-tutorial.md"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
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
    assert config.site.public_base_url == "http://localhost:18080"
    assert config.features.public_signup is False
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


def test_demo_manifest_enables_signup_without_changing_stage_one() -> None:
    stage_one = load_distribution_config(EXAMPLE / "distribution.yaml")
    demo = load_distribution_config(DEMO_MANIFEST)

    assert stage_one.features.public_signup is False
    assert demo.distribution.id == "example"
    assert demo.site.public_base_url == "http://localhost:13001"
    assert demo.features.public_signup is True
    assert Path(demo.paths.models) == (EXAMPLE / "config" / "models.yaml").resolve()
    assert Path(demo.paths.routing) == (EXAMPLE / "config" / "routing.yaml").resolve()


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
    assert (fake_context / fake_build["dockerfile"]).resolve() == (FAKE_PROVIDER / "Dockerfile")
    # The neutral image must carry no distribution content -- the example
    # included. It reads its config through the read-only distributions/ mount
    # like any other overlay, which is the whole reason it lives there.
    backend_dockerfile = (REPO / "deploy" / "docker" / "Dockerfile.backend").read_text()
    assert "distributions" not in backend_dockerfile

    environment = backend["environment"]
    for name in (
        "EXAMPLE_UPSTREAM_BASE_URL",
        "EXAMPLE_UPSTREAM_API_KEY",
        "EXAMPLE_UPSTREAM_MODEL",
    ):
        assert environment[name] == f"${{{name}}}"
    assert environment["DB_ENABLED"] == "false"
    assert environment["USER_AUTH_ENABLED"] == "false"


def test_demo_compose_is_an_explicit_full_local_third_layer() -> None:
    demo = yaml.safe_load(DEMO_COMPOSE.read_text())
    backend = demo["services"]["backend"]["environment"]

    assert backend["DB_ENABLED"] == "true"
    assert backend["DB_STORE_FULL_CONTENT"] == "false"
    assert backend["USER_AUTH_ENABLED"] == "true"
    assert backend["DISTRIBUTION_CONFIG_PATH"] == (
        "/app/distributions/example/distribution.demo.yaml"
    )
    assert backend["SIGNUP_ENABLED"] == "true"
    assert backend["SIGNUP_REQUIRE_EMAIL_VERIFICATION"] == "false"
    assert backend["ADMIN_EMAILS"] == "admin@local.dev"
    assert backend["REFRESH_TOKEN_COOKIE_NAME"] == "hybridinference_example_refresh"
    assert backend["JWT_SECRET_KEY"].startswith("${EXAMPLE_DEMO_JWT_SECRET:-LOCAL-ONLY-")
    assert backend["API_KEY_SECRET"].startswith("${EXAMPLE_DEMO_API_KEY_SECRET:-LOCAL-ONLY-")
    assert backend["SITE_PUBLIC_BASE_URL"] == "http://localhost:${FRONTEND_PORT:-13001}"

    frontend = demo["services"]["frontend"]
    assert frontend["container_name"] == (
        "${EXAMPLE_FRONTEND_CONTAINER_NAME:-hybridinference-example-frontend}"
    )
    assert frontend["build"]["args"]["NEXT_PUBLIC_API_BASE"] == ""
    assert frontend["build"]["args"]["BACKEND_INTERNAL_URL"] == "http://backend:8080"
    assert demo["services"]["postgres"]["container_name"] == (
        "${EXAMPLE_POSTGRES_CONTAINER_NAME:-hybridinference-example-postgres}"
    )
    assert demo["services"]["postgres"]["volumes"] == [
        "example_postgres_data:/var/lib/postgresql/data"
    ]
    assert demo["volumes"] == {"example_postgres_data": {"driver": "local"}}


def test_three_layer_demo_compose_is_isolated_and_shell_overridable() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")

    env = os.environ.copy()
    env.update(
        {
            "BACKEND_PORT": "28080",
            "FRONTEND_PORT": "23001",
            "DB_PORT": "25432",
            "EXAMPLE_BACKEND_CONTAINER_NAME": "demo-backend-test",
            "EXAMPLE_PROVIDER_CONTAINER_NAME": "demo-provider-test",
            "EXAMPLE_FRONTEND_CONTAINER_NAME": "demo-frontend-test",
            "EXAMPLE_POSTGRES_CONTAINER_NAME": "demo-postgres-test",
            "EXAMPLE_UPSTREAM_BASE_URL": "http://host.docker.internal:28001/v1",
            "EXAMPLE_UPSTREAM_API_KEY": "shell-local-key",
            "EXAMPLE_UPSTREAM_MODEL": "shell-local-model",
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
            "-f",
            str(DEMO_COMPOSE),
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
    services = rendered["services"]
    assert services["backend"]["container_name"] == "demo-backend-test"
    assert services["example-provider"]["container_name"] == "demo-provider-test"
    assert services["frontend"]["container_name"] == "demo-frontend-test"
    assert services["postgres"]["container_name"] == "demo-postgres-test"

    for service, published, target in (
        ("backend", "28080", 8080),
        ("frontend", "23001", 3001),
        ("postgres", "25432", 5432),
    ):
        port = services[service]["ports"][0]
        assert port["host_ip"] == "127.0.0.1"
        assert port["published"] == published
        assert port["target"] == target

    backend_env = services["backend"]["environment"]
    assert backend_env["DB_ENABLED"] == "true"
    assert backend_env["DB_STORE_FULL_CONTENT"] == "false"
    assert backend_env["USER_AUTH_ENABLED"] == "true"
    assert backend_env["ADMIN_EMAILS"] == "admin@local.dev"
    assert backend_env["REFRESH_TOKEN_COOKIE_NAME"] == "hybridinference_example_refresh"
    assert backend_env["SITE_PUBLIC_BASE_URL"] == "http://localhost:23001"
    assert backend_env["FRONTEND_URL"] == "http://localhost:23001"
    assert backend_env["EXAMPLE_UPSTREAM_BASE_URL"] == ("http://host.docker.internal:28001/v1")
    assert backend_env["EXAMPLE_UPSTREAM_API_KEY"] == "shell-local-key"
    assert backend_env["EXAMPLE_UPSTREAM_MODEL"] == "shell-local-model"
    assert backend_env["JWT_SECRET_KEY"].startswith("LOCAL-ONLY-")
    assert backend_env["API_KEY_SECRET"].startswith("LOCAL-ONLY-")
    assert services["frontend"]["build"]["args"]["NEXT_PUBLIC_API_BASE"] == ""
    assert services["frontend"]["build"]["args"]["BACKEND_INTERNAL_URL"] == ("http://backend:8080")
    assert services["frontend"]["environment"]["REFRESH_TOKEN_COOKIE_NAME"] == (
        "hybridinference_example_refresh"
    )

    backend_mounts = {mount["target"]: mount for mount in services["backend"]["volumes"]}
    assert backend_mounts["/app/var/data"]["type"] == "tmpfs"
    postgres_mounts = services["postgres"]["volumes"]
    assert postgres_mounts == [
        {
            "type": "volume",
            "source": "example_postgres_data",
            "target": "/var/lib/postgresql/data",
            "volume": {},
        }
    ]
    assert rendered["volumes"] == {
        "example_postgres_data": {
            "name": "hybridinference-example_example_postgres_data",
            "driver": "local",
        }
    }


def test_shell_can_override_the_example_upstream_in_compose() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")

    env = os.environ.copy()
    env.update(
        {
            "BACKEND_PORT": "28080",
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
    assert rendered["services"]["backend"]["ports"][0]["published"] == "28080"
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
    backend_env_lines = backend_env_file.read_text().splitlines()
    assert "BACKEND_PORT=18080" in backend_env_lines
    assert "FRONTEND_HOST=127.0.0.1" in backend_env_lines
    assert "FRONTEND_PORT=13001" in backend_env_lines
    assert "DB_PORT=15432" in backend_env_lines

    smoke = _make_dry_run("smoke", "DISTRIBUTION=example")
    assert '--base-url "http://localhost:18080"' in smoke

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
    assert published_port["published"] == "18080"
    assert published_port["target"] == 8080


def test_example_writes_nothing_into_the_checkout() -> None:
    """A missing bind-mount source is created as root and outlives the run.

    `var/**` is gitignored, so the example's first run created a root-owned
    var/data that `actions/checkout --clean` could not remove, failing every
    later job on that runner during checkout. A public clone would reproduce
    the same failure if the example depended on a missing bind-mount source.
    """
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
            str(EXAMPLE / "deploy" / "backend.env"),
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

    mounts = json.loads(proc.stdout)["services"]["backend"]["volumes"]
    by_target = {mount["target"]: mount for mount in mounts}

    assert by_target["/app/var/data"]["type"] == "tmpfs", (
        "var/** is gitignored, so a bind mount here is created by Docker as "
        "root and left in the checkout for actions/checkout to trip over"
    )
    # The overlay lives under distributions/, so that mount is how the backend
    # reads its config -- it must stay a bind, and stay read-only.
    assert by_target["/app/distributions"]["type"] == "bind"

    for mount in mounts:
        if mount["type"] != "bind":
            continue
        # Whatever still binds must exist in a fresh clone, or Docker creates it.
        assert Path(mount["source"]).exists(), mount["source"]
        assert mount.get("read_only") is True, f"{mount['target']} is writable"


def test_smoke_url_follows_the_distribution_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a distribution's port must move the smoke client with it.

    The port used to be declared twice — once for Compose to publish and once
    as a Make default for the smoke URL — so changing the distribution moved
    only one of them and `make smoke` probed a port nobody had published.
    """
    for name in ("BACKEND_PORT", "SMOKE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    sandbox = tmp_path / "repo"
    deploy = sandbox / "distributions" / "example" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    shutil.copy2(EXAMPLE_COMPOSE, deploy / "docker-compose.yml")
    (deploy / "backend.env").write_text("BACKEND_PORT=24242\n")

    smoke = _make_dry_run("smoke", "DISTRIBUTION=example", cwd=sandbox)

    assert '--base-url "http://localhost:24242"' in smoke


def test_make_selection_preserves_default_and_none_semantics(tmp_path: Path) -> None:
    """The example shares a root with real overlays but is never auto-selected.

    Both live under distributions/ now, so nothing about the path distinguishes
    them; the EXAMPLE_OVERLAY marker does. Without it, a second directory here
    would make a bare `make up` ambiguous and refuse to start anything.
    """
    sandbox = tmp_path / "repo"
    (sandbox / "distributions" / "acme" / "deploy").mkdir(parents=True)
    example = sandbox / "distributions" / "example"
    (example / "deploy").mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    (sandbox / "distributions" / "acme" / "deploy" / "backend.env").write_text("SITE_NAME=Acme\n")
    (example / "deploy" / "backend.env").write_text("SITE_NAME=Example\n")
    (example / "EXAMPLE_OVERLAY").write_text("teaching artifact\n")

    default = _make_dry_run("ps", cwd=sandbox)
    assert "Using distribution 'acme'" in default
    assert "distributions/acme/deploy/backend.env" in default
    assert "distributions/example/deploy/backend.env" not in default

    neutral = _make_dry_run("ps", "DISTRIBUTION=none", cwd=sandbox)
    assert "Using distribution" not in neutral
    assert "distributions/acme/deploy/backend.env" not in neutral
    assert "distributions/example/deploy/docker-compose.yml" not in neutral

    makefile = (sandbox / "Makefile").read_text()
    assert (
        "DISTRIBUTION_COMPOSE_FILE := $(if $(DISTRIBUTION_PATH),"
        "$(wildcard $(DISTRIBUTION_PATH)/deploy/docker-compose.yml),)"
    ) in makefile


def test_example_make_contract_is_backend_only_and_race_free(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    (sandbox / "distributions" / "example" / "deploy").mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    shutil.copy2(
        EXAMPLE / "deploy" / "backend.env",
        sandbox / "distributions" / "example" / "deploy" / "backend.env",
    )
    shutil.copy2(
        EXAMPLE_COMPOSE,
        sandbox / "distributions" / "example" / "deploy" / "docker-compose.yml",
    )
    shutil.copy2(
        EXAMPLE / "EXAMPLE_OVERLAY",
        sandbox / "distributions" / "example" / "EXAMPLE_OVERLAY",
    )
    (sandbox / ".env").write_text("EXAMPLE_UPSTREAM_MODEL=must-not-win\n")

    output = _make_dry_run("up", "DISTRIBUTION=example", cwd=sandbox)
    commands = [line for line in output.splitlines() if line.startswith("docker compose")]
    compose = (
        "docker compose -f deploy/docker/docker-compose.yml "
        "-f distributions/example/deploy/docker-compose.yml "
        "--env-file distributions/example/deploy/backend.env  "
    )
    assert commands == [
        f"{compose} up -d --wait example-provider",
        f"{compose} up -d --no-deps backend",
    ]
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
        assert "distributions/example/deploy/docker-compose.yml" in target_output


def test_distribution_path_cannot_be_detached_from_the_named_distribution(
    tmp_path: Path,
) -> None:
    """A command-line path override must not cross the example/deploy boundary."""
    sandbox = tmp_path / "repo"
    for name in ("example", "deployment"):
        deploy = sandbox / "distributions" / name / "deploy"
        deploy.mkdir(parents=True)
        (deploy / "backend.env").write_text(f"SITE_NAME={name}\n")
        (deploy / "docker-compose.yml").write_text("services: {}\n")
    (sandbox / "distributions" / "example" / "EXAMPLE_OVERLAY").write_text("teaching artifact\n")
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")

    example = _make_dry_run(
        "up",
        "DISTRIBUTION=example",
        "DISTRIBUTION_PATH=distributions/deployment",
        cwd=sandbox,
    )
    assert "distributions/example/deploy/backend.env" in example
    assert "distributions/example/deploy/docker-compose.yml" in example
    assert "distributions/deployment" not in example
    assert "docker volume" not in example

    deployment = _make_dry_run(
        "up",
        "DISTRIBUTION=deployment",
        "DISTRIBUTION_PATH=distributions/example",
        cwd=sandbox,
    )
    assert "distributions/deployment/deploy/backend.env" in deployment
    assert "distributions/deployment/deploy/docker-compose.yml" in deployment
    assert "distributions/example" not in deployment
    assert "docker volume" in deployment

    neutral = _make_dry_run(
        "up",
        "DISTRIBUTION=",
        "DISTRIBUTION_PATH=distributions/example",
        "_IS_EXAMPLE=yes",
        cwd=sandbox,
    )
    assert "distributions/example" not in neutral
    assert "docker volume" in neutral


def test_demo_make_contract_uses_the_third_layer_without_down_or_forced_build(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "repo"
    deploy = sandbox / "distributions" / "example" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    shutil.copy2(EXAMPLE / "deploy" / "backend.env", deploy / "backend.env")
    shutil.copy2(EXAMPLE_COMPOSE, deploy / "docker-compose.yml")
    shutil.copy2(DEMO_COMPOSE, deploy / "docker-compose.demo.yml")
    shutil.copy2(
        EXAMPLE / "EXAMPLE_OVERLAY",
        sandbox / "distributions" / "example" / "EXAMPLE_OVERLAY",
    )
    shutil.copy2(FULL_SMOKE, sandbox / "distributions" / "example" / "full_smoke.py")

    output = _make_dry_run("demo", "DISTRIBUTION=example", cwd=sandbox)
    commands = [
        line
        for line in output.splitlines()
        if line.startswith("env COMPOSE_PROFILES= docker compose")
    ]
    assert len(commands) == 3
    assert all("-f distributions/example/deploy/docker-compose.demo.yml" in cmd for cmd in commands)
    # Regression: promotion must never recreate an already-running provider or
    # frontend. Compose's own divergence check recreated an unchanged provider
    # in CI after an uncached image build, so the keep is pinned explicitly.
    assert commands[0].endswith("up -d --no-recreate --wait example-provider postgres")
    assert commands[1].endswith("up -d --no-deps --force-recreate --wait backend")
    assert commands[2].endswith("up -d --no-deps --no-recreate --wait frontend")
    assert " down" not in output
    assert "--build" not in output
    assert (
        "example-provider"
        not in (sandbox / "Makefile").read_text().split("demo:", 1)[1].split("demo-smoke:", 1)[0]
    )

    smoke = _make_dry_run("demo-smoke", "DISTRIBUTION=example", cwd=sandbox)
    assert 'full_smoke.py" --base-url "http://localhost:13001"' in smoke
    assert "--recreate-command env COMPOSE_PROFILES= docker compose" in smoke
    assert "-f distributions/example/deploy/docker-compose.demo.yml" in smoke
    assert smoke.rstrip().endswith("up -d --no-deps --force-recreate --wait backend")
    assert "demo-reset" not in smoke
    assert " down" not in smoke

    overridden = _make_dry_run(
        "demo-smoke",
        "DISTRIBUTION=example",
        "FRONTEND_PORT=23001",
        cwd=sandbox,
    )
    assert '--base-url "http://localhost:23001"' in overridden

    down = _make_dry_run("demo-down", "DISTRIBUTION=example", cwd=sandbox)
    assert down.rstrip().endswith("down")
    assert "--volumes" not in down

    reset = _make_dry_run("demo-reset", "DISTRIBUTION=example", cwd=sandbox)
    assert reset.rstrip().endswith("down --volumes --remove-orphans")

    state_file = "/tmp/example-reset-state.json"
    stateful_smoke = _make_dry_run(
        "demo-smoke",
        "DISTRIBUTION=example",
        f"DEMO_SMOKE_STATE_FILE={state_file}",
        cwd=sandbox,
    )
    assert f'--write-reset-state "{state_file}"' in stateful_smoke

    resumed_smoke = _make_dry_run(
        "demo-smoke",
        "DISTRIBUTION=example",
        f"DEMO_SMOKE_EXPECT_STATE_FILE={state_file}",
        cwd=sandbox,
    )
    assert f'--expect-existing-state "{state_file}"' in resumed_smoke


def test_demo_lifecycle_is_declared_by_artifacts_not_a_known_distribution_name(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "repo"
    deploy = sandbox / "distributions" / "future-project" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(REPO / "Makefile", sandbox / "Makefile")
    shutil.copy2(EXAMPLE / "deploy" / "backend.env", deploy / "backend.env")
    shutil.copy2(EXAMPLE_COMPOSE, deploy / "docker-compose.yml")
    shutil.copy2(DEMO_COMPOSE, deploy / "docker-compose.demo.yml")

    output = _make_dry_run("demo", "DISTRIBUTION=future-project", cwd=sandbox)

    assert "docker-compose.demo.yml" in output
    assert "full local demo requires DISTRIBUTION=example" not in output


def test_stage_one_smoke_enforces_its_site_config_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_module("_runnable_example_stage_one_site_config", EXAMPLE / "smoke.py")
    base_url = "http://localhost:28080"
    site = {
        "distribution": {"id": "example"},
        "features": {"public_signup": False},
        "site": {"public_base_url": base_url},
    }
    responses = {
        "/health": {"status": "healthy"},
        "/site-config": site,
        "/v1/models": {"data": [{"id": smoke.EXPECTED_MODEL}]},
        "/v1/chat/completions": {"choices": [{"message": {"content": smoke.EXPECTED_CONTENT}}]},
    }
    monkeypatch.setattr(
        smoke,
        "_request_json",
        lambda request_base_url, path, payload=None: responses[path],
    )

    smoke._check(base_url)

    site["features"]["public_signup"] = True
    with pytest.raises(RuntimeError, match="unexpectedly advertises public signup"):
        smoke._check(base_url)

    site["features"]["public_signup"] = False
    site["site"]["public_base_url"] = "http://localhost:18080"
    with pytest.raises(RuntimeError, match="advertises the wrong public URL"):
        smoke._check(base_url)


def test_full_smoke_enforces_its_site_config_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_smoke = _load_module("_runnable_example_full_site_config", FULL_SMOKE)
    base_url = "http://localhost:23001"
    site = {
        "distribution": {"id": "example"},
        "features": {"public_signup": True},
        "site": {"public_base_url": base_url},
    }
    responses = {
        "/health": {
            "status": "healthy",
            "database_configured": True,
            "database_connected": True,
        },
        "/site-config": site,
        "/v1/models": {"data": [{"id": full_smoke.EXPECTED_MODEL}]},
    }
    monkeypatch.setattr(
        full_smoke,
        "_request_json",
        lambda request_base_url, path, **kwargs: (200, responses[path]),
    )
    monkeypatch.setattr(full_smoke, "_request_page", lambda request_base_url, path: None)

    full_smoke._check_public_surface(base_url)

    site["features"]["public_signup"] = False
    with pytest.raises(full_smoke.SmokeError, match="does not expose signup"):
        full_smoke._check_public_surface(base_url)

    site["features"]["public_signup"] = True
    site["site"]["public_base_url"] = "http://localhost:13001"
    with pytest.raises(full_smoke.SmokeError, match="publishes the wrong frontend URL"):
        full_smoke._check_public_surface(base_url)


def test_full_smoke_accepts_the_admin_stats_hourly_row_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_smoke = _load_module("_runnable_example_full_admin_stats", FULL_SMOKE)
    responses = {
        "/admin/stats": {"period_hours": 24, "filters": {}, "stats": []},
        "/internal/playground/models": {"models": [{"id": full_smoke.EXPECTED_MODEL}]},
    }
    monkeypatch.setattr(
        full_smoke,
        "_request_json",
        lambda base_url, path, **kwargs: (200, responses[path]),
    )

    full_smoke._check_admin_surfaces("http://localhost:13001", "jwt")

    responses["/admin/stats"]["stats"] = {}
    with pytest.raises(full_smoke.SmokeError, match="stats API is unavailable"):
        full_smoke._check_admin_surfaces("http://localhost:13001", "jwt")


def test_full_smoke_logs_in_before_it_bootstraps_the_local_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_login", FULL_SMOKE)
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            (401, {}),
            (201, {}),
            (200, {"access_token": "jwt", "user": {"id": "user"}}),
        ]
    )

    def fake_request(base_url: str, path: str, **kwargs):
        calls.append((kwargs.get("method", "GET"), path))
        return next(responses)

    monkeypatch.setattr(full_smoke, "_request_json", fake_request)
    _, existed = full_smoke._login_or_signup(
        "http://localhost:13001",
        "not-printed",
        expect_existing=False,
        cookie_jar=full_smoke.CookieJar(),
    )

    assert existed is False
    assert calls == [
        ("POST", "/auth/login"),
        ("POST", "/auth/signup"),
        ("POST", "/auth/login"),
    ]

    calls.clear()
    monkeypatch.setattr(
        full_smoke,
        "_request_json",
        lambda base_url, path, **kwargs: (
            calls.append((kwargs.get("method", "GET"), path))
            or (200, {"access_token": "jwt", "user": {"id": "user"}})
        ),
    )
    _, existed = full_smoke._login_or_signup(
        "http://localhost:13001",
        "not-printed",
        expect_existing=False,
        cookie_jar=full_smoke.CookieJar(),
    )

    assert existed is True
    assert calls == [("POST", "/auth/login")]


def test_full_smoke_proves_an_unconfigured_email_is_not_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_smoke = _load_module("_runnable_example_non_admin", FULL_SMOKE)
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            (401, {}),
            (201, {}),
            (
                200,
                {
                    "access_token": "viewer-jwt",
                    "user": {
                        "email": full_smoke.NON_ADMIN_EMAIL,
                        "role": "free",
                        "is_admin": False,
                    },
                },
            ),
            (403, {"detail": "Admin access required"}),
        ]
    )

    def fake_request(base_url: str, path: str, **kwargs):
        calls.append((kwargs.get("method", "GET"), path))
        return next(responses)

    monkeypatch.setattr(full_smoke, "_request_json", fake_request)
    monkeypatch.setattr(full_smoke, "_assert_example_refresh_cookie", lambda *args: None)

    full_smoke._check_non_admin_account(
        "http://localhost:13001",
        expect_existing=False,
    )

    assert calls == [
        ("POST", "/auth/login"),
        ("POST", "/auth/signup"),
        ("POST", "/auth/login"),
        ("GET", "/admin/stats"),
    ]


def test_full_smoke_reuses_the_exact_key_after_backend_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_persistence", FULL_SMOKE)
    state = full_smoke.DemoState(
        access_token="original-jwt",
        api_key="original-api-key",
        key_prefix="original-pre",
        user_id="original-user",
        completion_id="chatcmpl-original",
        request_id="original-request",
        refresh_cookies=full_smoke.CookieJar(),
    )
    used_keys: list[str] = []

    monkeypatch.setattr(full_smoke, "_wait_for_public_surface", lambda *args: None)

    def fake_request(base_url: str, path: str, **kwargs):
        if path == "/auth/refresh":
            return 200, {"access_token": "refreshed-jwt"}
        return 200, {
            "keys": [
                {
                    "status": "active",
                    "api_key": state.api_key,
                    "key_prefix": state.key_prefix,
                }
            ]
        }

    monkeypatch.setattr(full_smoke, "_request_json", fake_request)
    monkeypatch.setattr(full_smoke, "_assert_example_refresh_cookie", lambda *args: None)
    monkeypatch.setattr(full_smoke, "_login", lambda *args: (200, {}))
    monkeypatch.setattr(
        full_smoke,
        "_validate_admin_login",
        lambda login: ("new-jwt", state.user_id),
    )
    monkeypatch.setattr(full_smoke, "_check_admin_surfaces", lambda *args: None)
    monkeypatch.setattr(full_smoke, "_check_current_admin", lambda *args, **kwargs: state.user_id)
    monkeypatch.setattr(full_smoke, "_wait_for_history", lambda *args, **kwargs: state.request_id)
    monkeypatch.setattr(full_smoke, "_completion", lambda base_url, key: used_keys.append(key))

    full_smoke._verify_after_recreate(
        "http://localhost:13001",
        "not-printed",
        state,
        time.monotonic() + 10,
    )

    assert used_keys == [state.api_key]


def test_full_smoke_reset_state_is_private_and_old_credentials_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_reset", FULL_SMOKE)
    state = full_smoke.DemoState(
        access_token="original-jwt",
        api_key="original-api-key",
        key_prefix="original-pre",
        user_id="original-user",
        completion_id="chatcmpl-original",
        request_id="original-request",
        refresh_cookies=full_smoke.CookieJar(),
    )
    state_file = tmp_path / "reset-state.json"
    full_smoke._write_reset_state(str(state_file), "LocalDemo1", state)

    assert state_file.stat().st_mode & 0o777 == 0o600
    assert state_file.read_text()

    calls: list[tuple[str, str | None, tuple[int, ...]]] = []
    monkeypatch.setattr(full_smoke, "_wait_for_public_surface", lambda *args: None)
    monkeypatch.setattr(full_smoke, "_login", lambda *args: (401, {}))

    def rejected_request(base_url: str, path: str, **kwargs):
        calls.append((path, kwargs.get("bearer"), kwargs.get("expected_statuses", (200,))))
        return 401, {}

    monkeypatch.setattr(full_smoke, "_request_json", rejected_request)
    full_smoke._verify_after_reset(
        "http://localhost:13001",
        str(state_file),
        time.monotonic() + 10,
    )

    assert calls == [
        ("/v1/chat/completions", state.api_key, (401,)),
    ]
    assert not state_file.exists()


def test_full_smoke_reset_state_cannot_be_written_inside_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_reset_path", FULL_SMOKE)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(full_smoke.SmokeError, match="outside the checkout"):
        full_smoke._state_path(str(REPO / "reset-state.json"))


def test_full_smoke_reset_state_is_exclusive_and_rejects_symlinks_and_broad_modes(
    tmp_path: Path,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_reset_security", FULL_SMOKE)
    state = full_smoke.DemoState(
        access_token="jwt",
        api_key="api-key",
        key_prefix="prefix",
        user_id="user",
        completion_id="completion",
        request_id="request",
        refresh_cookies=full_smoke.CookieJar(),
    )
    state_file = tmp_path / "state.json"
    full_smoke._write_reset_state(str(state_file), "LocalDemo1", state)

    with pytest.raises(full_smoke.SmokeError, match="could not create"):
        full_smoke._write_reset_state(str(state_file), "LocalDemo1", state)

    state_file.chmod(0o644)
    with pytest.raises(full_smoke.SmokeError, match="private regular file"):
        full_smoke._read_reset_state(str(state_file))

    dangling_target = tmp_path / "dangling-target.json"
    symlink = tmp_path / "state-link.json"
    symlink.symlink_to(dangling_target)
    with pytest.raises(full_smoke.SmokeError, match="could not create"):
        full_smoke._write_reset_state(str(symlink), "LocalDemo1", state)
    assert not dangling_target.exists()

    private_target = tmp_path / "private-target.json"
    full_smoke._write_reset_state(str(private_target), "LocalDemo1", state)
    read_link = tmp_path / "read-link.json"
    read_link.symlink_to(private_target)
    with pytest.raises(full_smoke.SmokeError, match="could not open"):
        full_smoke._read_reset_state(str(read_link))
    assert private_target.exists()


def test_full_smoke_reset_state_is_removed_when_reset_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_reset_cleanup", FULL_SMOKE)
    state = full_smoke.DemoState(
        access_token="jwt",
        api_key="api-key",
        key_prefix="prefix",
        user_id="user",
        completion_id="completion",
        request_id="request",
        refresh_cookies=full_smoke.CookieJar(),
    )
    state_file = tmp_path / "state.json"
    full_smoke._write_reset_state(str(state_file), "LocalDemo1", state)
    monkeypatch.setattr(full_smoke, "_wait_for_public_surface", lambda *args: None)
    monkeypatch.setattr(full_smoke, "_login", lambda *args: (200, {}))

    with pytest.raises(full_smoke.SmokeError, match="account survived"):
        full_smoke._verify_after_reset(
            "http://localhost:13001",
            str(state_file),
            time.monotonic() + 10,
        )

    assert not state_file.exists()


def test_full_smoke_validates_stream_ids_content_and_done() -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_sse", FULL_SMOKE)
    events = [
        json.dumps(
            {
                "id": "chatcmpl-one",
                "object": "chat.completion.chunk",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            }
        ),
        # Playground route metadata is an SSE event, but deliberately not an
        # OpenAI completion chunk and therefore does not carry a completion id.
        json.dumps({"choices": [], "_playground_route": {"provider": "example"}}),
        json.dumps(
            {
                "id": "chatcmpl-one",
                "object": "chat.completion.chunk",
                "choices": [{"delta": {"content": "RUNNABLE_EXAMPLE_OK"}}],
            }
        ),
        "[DONE]",
    ]

    full_smoke._assert_sse_contract(events, path="/stream")

    mismatched = events.copy()
    mismatched[2] = mismatched[2].replace("chatcmpl-one", "chatcmpl-two")
    with pytest.raises(full_smoke.SmokeError, match="changed completion id"):
        full_smoke._assert_sse_contract(mismatched, path="/stream")


def test_full_smoke_waits_for_a_new_request_history_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_smoke = _load_module("_runnable_example_full_smoke_history", FULL_SMOKE)
    responses = iter(
        [
            {"requests": [{"request_id": "old", "model_id": "example-chat", "status_code": 200}]},
            {
                "requests": [
                    {"request_id": "new", "model_id": "example-chat", "status_code": 200},
                    {"request_id": "old", "model_id": "example-chat", "status_code": 200},
                ]
            },
        ]
    )
    monkeypatch.setattr(
        full_smoke,
        "_request_json",
        lambda *args, **kwargs: (200, next(responses)),
    )
    monkeypatch.setattr(full_smoke.time, "sleep", lambda seconds: None)

    request_id = full_smoke._wait_for_history(
        "http://localhost:13001",
        "jwt",
        time.monotonic() + 10,
        excluded_request_ids={"old"},
    )

    assert request_id == "new"


def test_full_smoke_covers_the_full_ui_contract_without_printing_secrets() -> None:
    source = FULL_SMOKE.read_text()

    for contract in (
        "EXAMPLE_DEMO_ADMIN_PASSWORD",
        "EXAMPLE_DEMO_EXPECT_EXISTING",
        "admin@local.dev",
        "viewer@local.dev",
        "hybridinference_example_refresh",
        "/auth/login",
        "/auth/signup",
        '"/",',
        '"/login",',
        '"/signup",',
        "/user/me",
        "/user/api-keys/all",
        "/admin/stats",
        "/internal/playground/models",
        "/internal/playground/chat",
        "/user/recent-requests",
        "/dashboard/admin",
        "/dashboard/playground",
        "--recreate-command",
        "--write-reset-state",
        "--expect-existing-state",
        "--verify-reset-state",
        "EXAMPLE_FULL_SMOKE_OK",
        "EXAMPLE_RESET_SMOKE_OK",
        "excluded_request_ids=prior_request_ids",
    ):
        assert contract in source
    assert source.count("print(") == 2
    assert "print(SUCCESS_MARKER)" in source
    assert "print(RESET_SUCCESS_MARKER)" in source


def test_fake_provider_builds_a_deterministic_non_streaming_completion() -> None:
    fake = _load_module("_runnable_example_fake", FAKE_PROVIDER / "server.py")
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


def test_fake_provider_can_enforce_stage_three_model_key_and_response() -> None:
    fake = _load_module("_runnable_example_fake_stage_three", FAKE_PROVIDER / "server.py")
    handler = object.__new__(fake.FakeHandler)
    handler.path = "/v1/chat/completions"
    handler.expected_model = "host-fixture-model"
    handler.expected_api_key = "local-placeholder"
    handler.response_text = "STAGE3_HOST_FIXTURE_OK"
    responses: list[tuple[int, dict]] = []
    handler._send_json = lambda status, payload: responses.append((status, payload))

    handler.headers = {"Authorization": "Bearer wrong"}
    handler._read_json = lambda: {"model": "host-fixture-model"}
    handler.do_POST()
    assert responses[-1][0] == 401

    handler.headers = {"Authorization": "Bearer local-placeholder"}
    handler._read_json = lambda: {"model": "wrong-model"}
    handler.do_POST()
    assert responses[-1][0] == 400

    handler._read_json = lambda: {"model": "host-fixture-model"}
    handler.do_POST()
    assert responses[-1][0] == 200
    assert responses[-1][1]["choices"][0]["message"]["content"] == ("STAGE3_HOST_FIXTURE_OK")


def test_fake_provider_builds_deterministic_openai_sse_frames() -> None:
    fake = _load_module("_runnable_example_fake_stream", FAKE_PROVIDER / "server.py")
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
    fake = _load_module("_runnable_example_fake_handler", FAKE_PROVIDER / "server.py")
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
        FAKE_PROVIDER / "server.py",
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
    """The one repository CI executes the same contract the tutorial teaches."""
    workflow = ACTIVE_CI.read_text()
    readme = (EXAMPLE / "README.md").read_text()
    for command in (
        "make up DISTRIBUTION=example",
        "make smoke DISTRIBUTION=example",
        "make down DISTRIBUTION=example",
    ):
        assert command in workflow
        assert command in readme
    assert "Select runnable example port" in workflow
    assert 'echo "BACKEND_PORT=${port}" >> "${GITHUB_ENV}"' in workflow
    assert 'test "${port}" = "${BACKEND_PORT}"' in workflow
    assert 'SMOKE_BASE_URL="http://localhost:${port}" make smoke DISTRIBUTION=example' in workflow
    assert "github.run_id" in workflow
    assert "docker image rm" in workflow


def test_router_tutorial_teaches_what_the_example_actually_serves() -> None:
    """The walkthrough's model id and sentinel must track the shipped example."""
    tutorial = TUTORIAL.read_text()
    compact = "".join(tutorial.split())
    linear = " ".join(tutorial.replace("\\\n", " ").split())

    for command in (
        "make up DISTRIBUTION=example",
        "make smoke DISTRIBUTION=example",
        "make demo DISTRIBUTION=example",
        "make demo-smoke DISTRIBUTION=example",
        "make demo-down DISTRIBUTION=example",
        "make demo-reset DISTRIBUTION=example",
    ):
        assert command in tutorial

    models = yaml.safe_load((EXAMPLE / "config" / "models.yaml").read_text())["models"]
    assert f'"model":"{models[0]["id"]}"' in compact

    fake = _load_module("_runnable_example_fake_tutorial", FAKE_PROVIDER / "server.py")
    assert fake.RESPONSE_TEXT in tutorial

    # The troubleshooting path preserves the same linear Stage 1 -> Stage 2
    # journey while carrying every port to the commands that consume it.
    assert (
        "BACKEND_PORT=28080 FRONTEND_PORT=23001 DB_PORT=25432 make up DISTRIBUTION=example"
    ) in linear
    assert "BACKEND_PORT=28080 make smoke DISTRIBUTION=example" in linear
    assert (
        "BACKEND_PORT=28080 FRONTEND_PORT=23001 DB_PORT=25432 make demo DISTRIBUTION=example"
    ) in linear
    assert "FRONTEND_PORT=23001" in linear
    assert "make demo-smoke DISTRIBUTION=example" in linear

    # The walkthrough's URLs must address both ports the example publishes.
    env_defaults = dict(
        line.split("=", 1)
        for line in (EXAMPLE / "deploy" / "backend.env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert f"localhost:{env_defaults['BACKEND_PORT']}/health" in tutorial
    assert f"localhost:{env_defaults['FRONTEND_PORT']}/signup" in tutorial


def test_router_tutorial_is_reachable_in_the_developer_toctree() -> None:
    """The canonical developer guide must link the tutorial."""
    slug = TUTORIAL.stem
    index = REPO / "docs" / "developer" / "index.rst"
    entries = [line.strip() for line in index.read_text().splitlines()]
    assert slug in entries, f"{index} does not list {slug}"
