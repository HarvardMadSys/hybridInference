from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy/docker/docker-compose.yml").read_text())


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/codex-oncall.yml").read_text())


def _workflow_v2() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/codex-oncall-v2.yml").read_text())


def test_oncall_service_is_an_opt_in_hardened_container():
    service = _compose()["services"]["codex-oncall"]

    assert service["profiles"] == ["oncall"]
    assert service["build"]["dockerfile"] == "deploy/docker/Dockerfile.oncall"
    assert "secrets" not in service["build"]
    assert "args" not in service["build"]
    assert service["env_file"] == ["${CODEX_ONCALL_ENV_FILE:-../../.env.oncall}"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["volumes"] == ["codex_oncall_data:/var/lib/codex-oncall"]
    assert service["ports"] == ["127.0.0.1:${CODEX_ONCALL_PORT:-8091}:8091"]


def test_oncall_service_uses_internal_fixed_runtime_paths():
    environment = _compose()["services"]["codex-oncall"]["environment"]

    assert environment == {
        "CODEX_ONCALL_STATE_DIR": "/var/lib/codex-oncall",
        "HOME": "/var/lib/codex-oncall",
    }


def test_backend_receives_immutable_deployment_sha_from_build_metadata():
    environment = _compose()["services"]["backend"]["environment"]

    assert environment["DEPLOYMENT_SHA"] == "${BUILD_SHA:-}"


def test_oncall_image_is_a_slim_relay_without_codex():
    dockerfile = (ROOT / "deploy/docker/Dockerfile.oncall").read_text()
    dockerignore = (ROOT / "deploy/docker/Dockerfile.oncall.dockerignore").read_text()

    # Codex, Node, and the repository snapshot now live in the GitHub Actions
    # workflow; the relay image must not grow them back.
    assert "@openai/codex" not in dockerfile
    assert "FROM node" not in dockerfile
    assert "npm install" not in dockerfile
    assert "repository-snapshot" not in dockerfile
    assert "--no-install-package routewise" in dockerfile
    assert "COPY apps/backend/serving /app/apps/backend/serving" in dockerfile
    assert "USER oncall" in dockerfile
    assert ".env.*" in dockerignore
    assert "tests/fixtures" in dockerignore


def test_analysis_workflow_is_dispatch_triggered_and_least_privilege():
    workflow = _workflow()

    # PyYAML parses the `on:` key as boolean True (YAML 1.1).
    trigger = workflow.get("on") or workflow.get(True)
    assert trigger["repository_dispatch"]["types"] == ["codex-oncall"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "fingerprint" in workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is False

    job = workflow["jobs"]["oncall"]
    assert job["timeout-minutes"] <= 20
    steps = {step.get("name", ""): step for step in job["steps"]}
    # Untrusted payload must reach disk via env indirection, never shell
    # interpolation of ${{ github.event... }} inside run scripts.
    payload_step = steps["Write dispatch payload"]
    assert "ONCALL_PAYLOAD" in payload_step["env"]
    for step in job["steps"]:
        assert "github.event" not in step.get("run", "")

    # Secrets stay step-scoped; the model and base URL come from the relay
    # payload, and the wire protocol stays pinned to the Responses API (chat
    # wire support no longer exists in Codex, openai/codex#7782).
    codex_step = steps["Run Codex analysis"]
    assert codex_step["env"]["CODEX_API_KEY"] == "${{ secrets.CODEX_ONCALL_MODEL_API_KEY }}"
    assert "jq -c '.base_url'" in codex_step["run"]
    assert 'wire_api="responses"' in codex_step["run"]
    # The gateway translates Responses to Chat Completions. Applying the final
    # JSON schema to every turn prevents local models from emitting tool calls.
    assert not any(
        line.lstrip().startswith("--output-schema ") for line in codex_step["run"].splitlines()
    )
    assert "CODEX_API_KEY" not in steps["Post analysis to Slack thread"].get("env", {})


def test_v2_analysis_workflow_is_job_only_read_only_and_relay_callback_only():
    workflow = _workflow_v2()
    trigger = workflow.get("on") or workflow.get(True)

    assert list(trigger["workflow_dispatch"]["inputs"]) == ["job_id"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "codex-oncall-v2-${{ inputs.job_id }}",
        "cancel-in-progress": False,
    }
    job = workflow["jobs"]["oncall"]
    assert job["timeout-minutes"] <= 20
    steps = {step.get("name", ""): step for step in job["steps"]}
    step_names = list(steps)
    assert step_names.index("Fetch relay-authenticated job") < step_names.index(
        "Check out trusted analysis ref"
    )

    checkout = steps["Check out trusted analysis ref"]
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["ref"] == "${{ steps.job.outputs.analysis_ref }}"

    source = (ROOT / ".github/workflows/codex-oncall-v2.yml").read_text()
    assert "SLACK" not in source
    assert "danger-full-access" not in source
    assert "--sandbox read-only" in steps["Run read-only Codex analysis"]["run"]
    assert "timeout --signal=TERM --kill-after=30s 10m" in steps[
        "Run read-only Codex analysis"
    ]["run"]
    assert "serving.oncall.gha callback" in steps["Return validated analysis to relay"]["run"]
    assert "/complete" in steps["Return workflow failure to relay"]["run"]
    assert "--retry 3 --retry-all-errors" in steps["Return workflow failure to relay"]["run"]
    assert "RUN_URL" in steps["Return workflow failure to relay"]["env"]
    assert steps["Return workflow failure to relay"]["if"] == "failure()"

    # Untrusted workflow input reaches shell only through a quoted environment
    # variable and is UUID-validated before it is used in a URL.
    for step in job["steps"]:
        assert "inputs.job_id" not in step.get("run", "")
    assert "grep -Eq" in steps["Fetch relay-authenticated job"]["run"]
    assert "grep -Eq" in steps["Return workflow failure to relay"]["run"]


def test_systemd_deployment_was_removed():
    assert not (ROOT / "deploy/systemd/codex-oncall.service").exists()
