from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy/docker/docker-compose.yml").read_text())


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/codex-triage.yml").read_text())


def test_triage_service_is_an_opt_in_hardened_container():
    service = _compose()["services"]["codex-triage"]

    assert service["profiles"] == ["triage"]
    assert service["build"]["dockerfile"] == "deploy/docker/Dockerfile.triage"
    assert "secrets" not in service["build"]
    assert "args" not in service["build"]
    assert service["env_file"] == ["${CODEX_TRIAGE_ENV_FILE:-../../.env.triage}"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["volumes"] == ["codex_triage_data:/var/lib/codex-triage"]
    assert service["ports"] == ["127.0.0.1:${CODEX_TRIAGE_PORT:-8091}:8091"]


def test_triage_service_uses_internal_fixed_runtime_paths():
    environment = _compose()["services"]["codex-triage"]["environment"]

    assert environment == {
        "CODEX_TRIAGE_STATE_DIR": "/var/lib/codex-triage",
        "HOME": "/var/lib/codex-triage",
    }


def test_triage_image_is_a_slim_relay_without_codex():
    dockerfile = (ROOT / "deploy/docker/Dockerfile.triage").read_text()
    dockerignore = (ROOT / "deploy/docker/Dockerfile.triage.dockerignore").read_text()

    # Codex, Node, and the repository snapshot now live in the GitHub Actions
    # workflow; the relay image must not grow them back.
    assert "@openai/codex" not in dockerfile
    assert "FROM node" not in dockerfile
    assert "npm install" not in dockerfile
    assert "repository-snapshot" not in dockerfile
    assert "--no-install-package routewise" in dockerfile
    assert "COPY apps/backend/serving /app/apps/backend/serving" in dockerfile
    assert "USER triage" in dockerfile
    assert ".env.*" in dockerignore
    assert "tests/fixtures" in dockerignore


def test_analysis_workflow_is_dispatch_triggered_and_least_privilege():
    workflow = _workflow()

    # PyYAML parses the `on:` key as boolean True (YAML 1.1).
    trigger = workflow.get("on") or workflow.get(True)
    assert trigger["repository_dispatch"]["types"] == ["codex-triage"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "fingerprint" in workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is False

    job = workflow["jobs"]["triage"]
    assert job["timeout-minutes"] <= 20
    steps = {step.get("name", ""): step for step in job["steps"]}
    # Untrusted payload must reach disk via env indirection, never shell
    # interpolation of ${{ github.event... }} inside run scripts.
    payload_step = steps["Write dispatch payload"]
    assert "TRIAGE_PAYLOAD" in payload_step["env"]
    for step in job["steps"]:
        assert "github.event" not in step.get("run", "")


def test_systemd_deployment_was_removed():
    assert not (ROOT / "deploy/systemd/codex-alert-triage.service").exists()
