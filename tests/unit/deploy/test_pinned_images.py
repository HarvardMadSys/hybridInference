"""What the deploy builds and runs from is an exact release, not whatever is newest.

A moving tag (``python:3.12-slim``, ``postgres:16``) or an unpinned installer
(``pip install uv``, ``uvx pip-audit``) changes the gateway image, the
database or the code a self-hosted CI runner executes, with no change in this
repository: whatever the registry or the index serves at that moment is what
runs. Parsed from the files, so no Docker or network is needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DOCKER = REPO / "deploy" / "docker"
CI = REPO / ".github" / "workflows" / "ci.yml"
DOCKERFILES = sorted(DOCKER.glob("Dockerfile.*"))
COMPOSE_FILES = sorted(DOCKER.glob("docker-compose*.yml"))

# How many version components name one release of each image; an image not
# listed here must be pinned by digest.
_RELEASE_PARTS = {"python": 3, "node": 3, "postgres": 2, "dpage/pgadmin4": 2}


def _is_exact(image: str) -> bool:
    if re.search(r"@sha256:[0-9a-f]{64}$", image):
        return True
    name, _, tag = image.partition(":")
    parts = _RELEASE_PARTS.get(name)
    return parts is not None and re.match(rf"\d+(?:\.\d+){{{parts - 1}}}(?:-|$)", tag) is not None


def _from_images(dockerfile: Path) -> list[str]:
    """Each FROM's image, leaving out ``scratch`` and earlier stages."""
    stages: set[str] = set()
    images = []
    for line in dockerfile.read_text().splitlines():
        match = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.IGNORECASE)
        if match is None:
            continue
        image, alias = match.groups()
        if image != "scratch" and image not in stages:
            images.append(image)
        if alias:
            stages.add(alias)
    return images


def test_the_rule_tells_a_release_from_a_moving_tag() -> None:
    assert _is_exact("python:3.12.16-slim")
    assert _is_exact("postgres:16.15")
    assert _is_exact("node:22-alpine@sha256:" + "0" * 64)
    assert not _is_exact("python:3.12-slim")
    assert not _is_exact("postgres:16")
    assert not _is_exact("node:22-alpine")
    assert not _is_exact("busybox:1.36.1")


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_every_base_image_is_an_exact_release(dockerfile: Path) -> None:
    images = _from_images(dockerfile)

    assert images, f"{dockerfile.name}: the FROM scan went blind"
    for image in images:
        assert _is_exact(image), f"{dockerfile.name}: {image}"


def test_both_backend_stages_run_one_python_release() -> None:
    """The virtualenv built in the first stage runs on the second's interpreter."""
    images = _from_images(DOCKER / "Dockerfile.backend")

    assert len(images) == 2 and len(set(images)) == 1, images
    assert images[0].startswith("python:3.12."), images


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda path: path.name)
def test_every_compose_image_is_an_exact_release(compose: Path) -> None:
    for name, service in yaml.safe_load(compose.read_text())["services"].items():
        image = service.get("image")
        if image is None or image.startswith("${"):
            continue  # built here, or a digest the deploy supplies
        assert _is_exact(image), f"{compose.name}: {name}: {image}"


def test_the_backend_image_installs_ci_s_uv_release_by_hash() -> None:
    folded = (DOCKER / "Dockerfile.backend").read_text().replace("\\\n", " ")
    (install,) = [line for line in folded.splitlines() if "pip install" in line]
    for flag in ("--require-hashes", "--only-binary=:all:", "-r /tmp/uv-requirements.txt"):
        assert flag in install, install
    assert "COPY deploy/docker/uv-requirements.txt /tmp/uv-requirements.txt" in folded

    requirements = (DOCKER / "uv-requirements.txt").read_text().replace("\\\n", " ")
    (pin,) = [line for line in requirements.splitlines() if line and not line.startswith("#")]
    version = re.match(r"uv==(\d+\.\d+\.\d+) ", pin)
    assert version is not None, pin
    assert version.group(1) == yaml.safe_load(CI.read_text())["env"]["UV_VERSION"]
    # Every file of the release, so whichever wheel pip picks is checked.
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", pin)) >= 2, pin


def test_ci_audits_with_an_exact_pip_audit_release() -> None:
    workflow = yaml.safe_load(CI.read_text())
    scripts = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps") or []
    ]
    audits = [script for script in scripts if "pip-audit" in script]

    assert re.fullmatch(r"\d+\.\d+\.\d+", workflow["env"]["PIP_AUDIT_VERSION"])
    assert audits
    for script in audits:
        assert '--from "pip-audit==${PIP_AUDIT_VERSION}"' in script, script


def test_dependabot_proposes_updates_to_the_pinned_images() -> None:
    """Exact pins go stale by hand; this keeps tags, and digests once pinned, current."""
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    watched = {(update["package-ecosystem"], update["directory"]) for update in config["updates"]}

    assert {("docker", "/deploy/docker"), ("docker-compose", "/deploy/docker")} <= watched
