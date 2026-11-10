"""A deploy's image pin belongs to the deployment it was recorded for.

`make deploy` and `make rollback` record the images they started in the
deployment's own state directory. That deployment's later Compose commands start
those images; another distribution, the example, a second Compose project or a
source build must never pick them up.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ARTIFACTS = "-f deploy/docker/docker-compose.artifacts.yml"
PIN = (
    f"BACKEND_IMAGE=registry.example/backend@sha256:{'a' * 64}\n"
    f"FRONTEND_IMAGE=registry.example/frontend@sha256:{'b' * 64}\n"
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A checkout with one deployment overlay, the example, and acme pinned."""
    shutil.copy2(REPO / "Makefile", tmp_path / "Makefile")
    acme = tmp_path / "distributions" / "acme" / "deploy"
    acme.mkdir(parents=True)
    (acme / "backend.env").write_text("SITE_NAME=Acme\n")
    example = tmp_path / "distributions" / "example"
    (example / "deploy").mkdir(parents=True)
    (example / "deploy" / "backend.env").write_text("SITE_NAME=Example\n")
    (example / "EXAMPLE_OVERLAY").write_text("teaching artifact\n")
    pin = tmp_path / ".ops" / "acme" / "images.env"
    pin.parent.mkdir(parents=True)
    pin.write_text(PIN)
    return tmp_path


def _make(sandbox: Path, *args: str, dry_run: bool = True, **env: str):
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DISTRIBUTION", "COMPOSE_PROJECT_NAME", "MAKEFLAGS")
    }
    return subprocess.run(
        ["make", "--no-print-directory", *(["-n"] if dry_run else []), *args],
        cwd=sandbox,
        env={**environment, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _compose_lines(sandbox: Path, *args: str, **env: str) -> list[str]:
    proc = _make(sandbox, *args, **env)
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if "docker compose" in line]


@pytest.mark.parametrize("target", ["up", "migrate", "restart", "ps", "down"])
def test_the_pinned_deployment_keeps_starting_its_pinned_images(sandbox: Path, target: str):
    lines = _compose_lines(sandbox, target, "DISTRIBUTION=acme")
    assert lines
    for line in lines:
        assert ARTIFACTS in line
        assert "--env-file .ops/acme/images.env" in line


@pytest.mark.parametrize(
    ("selection", "env"),
    [
        ("DISTRIBUTION=example", {}),
        ("DISTRIBUTION=none", {}),
        ("DISTRIBUTION=acme", {"COMPOSE_PROJECT_NAME": "acme-staging"}),
    ],
    ids=["example", "neutral", "another-project"],
)
def test_no_other_deployment_picks_up_the_pin(sandbox: Path, selection: str, env: dict):
    """The example used to start production's pinned images through a shared pin."""
    output = _make(sandbox, "up", selection, **env)
    assert output.returncode == 0, output.stderr
    assert "images.env" not in output.stdout
    assert ARTIFACTS not in output.stdout


def test_a_source_build_refuses_while_the_deployment_is_pinned(sandbox: Path):
    """`make build` would tag a checkout build as a digest it is not, or swap artifacts."""
    # Not a dry run: the refusal must stop make before any Docker command runs.
    refused = _make(sandbox, "build", "DISTRIBUTION=acme", dry_run=False)
    assert refused.returncode != 0
    assert ".ops/acme/images.env" in refused.stderr
    assert "remove that file to return to source builds" in refused.stderr
    assert "docker" not in refused.stdout

    (sandbox / ".ops" / "acme" / "images.env").unlink()
    build = _compose_lines(sandbox, "build", "DISTRIBUTION=acme")
    assert any(line.endswith(" build") for line in build), build
    assert not any(ARTIFACTS in line for line in build)


def test_deploy_rollback_and_backups_use_the_deployment_record(sandbox: Path):
    deploy = _make(sandbox, "deploy", "DISTRIBUTION=acme").stdout
    rollback = _make(sandbox, "rollback", "DISTRIBUTION=acme").stdout
    backup = _make(sandbox, "backup", "DISTRIBUTION=acme").stdout
    assert '--state ".ops/acme"' in deploy
    assert '--state ".ops/acme"' in rollback
    assert '--backup ".ops/acme/gateway.dump"' in backup

    staged = _make(sandbox, "rollback", "DISTRIBUTION=acme", COMPOSE_PROJECT_NAME="acme-staging")
    assert '--state ".ops/acme/acme-staging"' in staged.stdout


def test_the_build_context_leaves_out_lifecycle_state_and_dumps():
    """`make build` sends the checkout to Docker; a dump in it carries every stored secret."""
    ignored = (REPO / ".dockerignore").read_text().splitlines()
    for pattern in (".ops*", "*.dump", "**/*.dump"):
        assert pattern in ignored
