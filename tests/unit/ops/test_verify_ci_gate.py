"""Tests for the fail-closed CI Gate verifier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.ci.classify_changes import classify
from ops.ci.verify_ci_gate import (
    BOOLEAN_OUTPUTS,
    REQUIRED_JOBS,
    parse_classification,
    verify_gate,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER = REPO_ROOT / "ops/ci/verify_ci_gate.py"


def _classification(*, matrix: tuple[str, ...] = (), **enabled: bool) -> str:
    payload = {name: str(enabled.get(name, False)).lower() for name in BOOLEAN_OUTPUTS}
    payload["docker_matrix"] = json.dumps(matrix, separators=(",", ":"))
    return json.dumps(payload)


def _results(**overrides: str) -> str:
    payload = dict.fromkeys(REQUIRED_JOBS, "skipped")
    payload.update({"changes": "success", "security": "success"})
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.parametrize(
    "files",
    [
        ["docs/developer/architecture.md"],
        ["tests/unit/test_router.py"],
        ["apps/frontend/src/app/page.tsx"],
        ["apps/backend/routing/routers.py"],
        ["apps/backend/serving/config/settings.py"],
        ["services/status-monitor-worker/src/index.ts"],
        ["services/alert-control-plane-worker/src/index.ts"],
        [".dockerignore"],
        ["unknown/file.txt"],
    ],
)
def test_classifier_outputs_satisfy_gate_contract(files: list[str]) -> None:
    parse_classification(json.dumps(classify(files).as_workflow_outputs()))


def test_pr_docs_only_requires_only_changes_and_security() -> None:
    verify_gate(
        "pull_request",
        _classification(security_only=True),
        _results(),
    )


def test_pr_backend_source_requires_backend_tests_and_affected_docker() -> None:
    verify_gate(
        "pull_request",
        _classification(backend=True, python_tests=True, matrix=("backend",)),
        _results(**{"backend-quality": "success", "test": "success", "docker-build": "success"}),
    )


def test_pr_tests_only_runs_backend_without_docker() -> None:
    verify_gate(
        "pull_request",
        _classification(backend=True, python_tests=True),
        _results(**{"backend-quality": "success", "test": "success"}),
    )


def test_pr_alert_only_runs_alert_checks_without_app_images() -> None:
    verify_gate(
        "pull_request",
        _classification(alert_control_plane=True, python_tests=True),
        _results(**{"alert-control-plane-check": "success", "test": "success"}),
    )


def test_pr_status_monitor_runs_python_contract_tests_without_app_images() -> None:
    verify_gate(
        "pull_request",
        _classification(status_monitor=True, python_tests=True),
        _results(test="success"),
    )


def test_pr_frontend_dockerfile_runs_frontend_python_tests_and_image() -> None:
    verify_gate(
        "pull_request",
        _classification(frontend=True, python_tests=True, matrix=("frontend",)),
        _results(**{"frontend-quality": "success", "test": "success", "docker-build": "success"}),
    )


def test_pr_docker_shared_runs_python_tests_and_all_images() -> None:
    verify_gate(
        "pull_request",
        _classification(
            docker_shared=True,
            python_tests=True,
            matrix=("frontend", "backend", "oncall"),
        ),
        _results(test="success", **{"docker-build": "success"}),
    )


def test_pr_full_requires_all_application_jobs_and_images() -> None:
    verify_gate(
        "pull_request",
        _classification(full=True, python_tests=True, matrix=("frontend", "backend", "oncall")),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "alert-control-plane-check": "success",
                "test": "success",
                "docker-build": "success",
            }
        ),
    )


def test_push_requires_all_app_jobs_and_skips_docker() -> None:
    verify_gate(
        "push",
        _classification(frontend=True, matrix=("frontend",)),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "alert-control-plane-check": "success",
                "test": "success",
            }
        ),
    )


@pytest.mark.parametrize("event_name", ["schedule", "workflow_dispatch"])
def test_schedule_and_manual_require_all_app_jobs_and_docker(event_name: str) -> None:
    verify_gate(
        event_name,
        _classification(full=True, python_tests=True, matrix=("frontend", "backend", "oncall")),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "alert-control-plane-check": "success",
                "test": "success",
                "docker-build": "success",
            }
        ),
    )


def test_schedule_rejects_partial_docker_plan() -> None:
    with pytest.raises(ValueError, match="full three-image"):
        verify_gate(
            "schedule",
            _classification(frontend=True, matrix=("frontend",)),
            _results(
                **{
                    "backend-quality": "success",
                    "frontend-quality": "success",
                    "alert-control-plane-check": "success",
                    "test": "success",
                    "docker-build": "success",
                }
            ),
        )


@pytest.mark.parametrize("bad_result", ["failure", "cancelled", "", "skipped"])
def test_required_success_rejects_non_success(bad_result: str) -> None:
    with pytest.raises(ValueError, match="changes"):
        verify_gate(
            "pull_request",
            _classification(security_only=True),
            _results(changes=bad_result),
        )


def test_should_skip_rejects_unexpected_success() -> None:
    with pytest.raises(ValueError, match="frontend-quality"):
        verify_gate(
            "pull_request",
            _classification(backend=True, python_tests=True),
            _results(
                **{
                    "backend-quality": "success",
                    "frontend-quality": "success",
                    "test": "success",
                }
            ),
        )


def test_should_run_rejects_skipped() -> None:
    with pytest.raises(ValueError, match="backend-quality"):
        verify_gate(
            "pull_request",
            _classification(backend=True, python_tests=True),
            _results(test="success"),
        )


def test_missing_job_result_is_rejected() -> None:
    results = json.loads(_results())
    del results["security"]
    with pytest.raises(ValueError, match="missing"):
        verify_gate(
            "pull_request",
            _classification(security_only=True),
            json.dumps(results),
        )


@pytest.mark.parametrize(
    "classification",
    [
        {"security_only": "yes", "docker_matrix": "[]"},
        {"security_only": [], "docker_matrix": "[]"},
        {"security_only": "true", "docker_matrix": "not-json"},
        {"backend": "true", "docker_matrix": '["backend","backend"]'},
        {"backend": "true", "frontend": "true", "docker_matrix": '["backend","frontend"]'},
        {"full": "true", "docker_matrix": '["frontend"]'},
    ],
)
def test_malformed_classification_is_rejected(classification: dict[str, object]) -> None:
    payload: dict[str, object] = dict.fromkeys(BOOLEAN_OUTPUTS, "false")
    payload.update(classification)
    with pytest.raises(ValueError):
        parse_classification(json.dumps(payload))


def test_missing_classification_output_is_rejected() -> None:
    payload = json.loads(_classification(security_only=True))
    del payload["status_monitor"]
    with pytest.raises(ValueError, match="missing"):
        parse_classification(json.dumps(payload))


def test_validate_cli_writes_canonical_github_outputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output.txt"
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "validate-classification",
            "--classification-json",
            _classification(backend=True, python_tests=True, matrix=("backend",)),
            "--github-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    written = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert written["backend"] == "true"
    assert written["docker_matrix"] == '["backend"]'


def test_verify_cli_fails_closed(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFIER),
            "verify-gate",
            "--event-name",
            "pull_request",
            "--classification-json",
            _classification(security_only=True),
            "--job-results-json",
            _results(security="failure"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "security" in result.stderr
