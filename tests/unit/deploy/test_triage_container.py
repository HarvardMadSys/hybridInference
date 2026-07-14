from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy/docker/docker-compose.yml").read_text())


def test_triage_service_is_an_opt_in_hardened_container():
    service = _compose()["services"]["codex-triage"]

    assert service["profiles"] == ["triage"]
    assert service["build"]["dockerfile"] == "deploy/docker/Dockerfile.triage"
    assert "secrets" not in service["build"]
    assert service["env_file"] == ["${CODEX_TRIAGE_ENV_FILE:-../../.env.triage}"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["volumes"] == ["codex_triage_data:/var/lib/codex-triage"]
    assert service["ports"] == ["127.0.0.1:${CODEX_TRIAGE_PORT:-8091}:8091"]


def test_triage_service_uses_internal_fixed_runtime_paths():
    environment = _compose()["services"]["codex-triage"]["environment"]

    assert environment == {
        "CODEX_TRIAGE_REPOSITORY_PATH": "/workspace/repository",
        "CODEX_TRIAGE_STATE_DIR": "/var/lib/codex-triage",
        "CODEX_TRIAGE_CODEX_HOME": "/var/lib/codex-triage/codex-home",
        "CODEX_TRIAGE_CODEX_BINARY": "/usr/local/bin/codex",
        "HOME": "/var/lib/codex-triage",
    }


def test_triage_image_contains_codex_and_a_sanitized_repository_snapshot():
    dockerfile = (ROOT / "deploy/docker/Dockerfile.triage").read_text()
    dockerignore = (ROOT / "deploy/docker/Dockerfile.triage.dockerignore").read_text()

    assert '"@openai/codex@${CODEX_CLI_VERSION}"' in dockerfile
    assert "--no-install-package routewise" in dockerfile
    assert "FROM alpine:3.21 AS repository-snapshot" in dockerfile
    assert "COPY --from=repository-snapshot /repository /workspace/repository" in dockerfile
    assert "USER triage" in dockerfile
    assert ".env.*" in dockerignore
    assert "services/**/node_modules" in dockerignore
    assert "tests/fixtures" in dockerignore


def test_systemd_deployment_was_removed():
    assert not (ROOT / "deploy/systemd/codex-alert-triage.service").exists()
