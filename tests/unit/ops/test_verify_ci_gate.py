"""Tests for the fail-closed CI Gate verifier."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ops.ci.classify_changes import classify
from ops.ci.verify_ci_gate import (
    BOOLEAN_OUTPUTS,
    REQUIRED_JOBS,
    ClassificationOutputs,
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
        ["docs/user/models.md"],
        ["docs/developer/routing.md", "apps/frontend/src/app/page.tsx"],
        ["tests/unit/test_router.py"],
        ["apps/frontend/src/app/page.tsx"],
        ["apps/backend/routing/routers.py"],
        ["apps/backend/serving/config/settings.py"],
        [".dockerignore"],
        ["distributions/example/frontend/site-ui/client.tsx"],
        ["deploy/docker/site-ui/.built-in-ui"],
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


def test_pr_sphinx_docs_requires_docs_build_alongside_security_only() -> None:
    verify_gate(
        "pull_request",
        _classification(docs=True, security_only=True),
        _results(**{"docs-build": "success"}),
    )


def test_pr_sphinx_docs_rejects_skipped_docs_build() -> None:
    with pytest.raises(ValueError, match="docs-build"):
        verify_gate(
            "pull_request",
            _classification(docs=True, security_only=True),
            _results(),
        )


def test_pr_non_docs_change_rejects_unexpected_docs_build() -> None:
    with pytest.raises(ValueError, match="docs-build"):
        verify_gate(
            "pull_request",
            _classification(backend=True, python_tests=True),
            _results(
                **{
                    "backend-quality": "success",
                    "test": "success",
                    "docs-build": "success",
                }
            ),
        )


def test_pr_backend_source_requires_backend_tests_and_affected_docker() -> None:
    verify_gate(
        "pull_request",
        _classification(
            backend=True,
            python_tests=True,
            tutorial_e2e=True,
            matrix=("backend",),
        ),
        _results(
            **{
                "backend-quality": "success",
                "test": "success",
                "docker-build": "success",
                "tutorial-e2e": "success",
            }
        ),
    )


def test_pr_tests_only_runs_backend_without_docker() -> None:
    verify_gate(
        "pull_request",
        _classification(backend=True, python_tests=True),
        _results(**{"backend-quality": "success", "test": "success"}),
    )


def test_pr_frontend_dockerfile_runs_frontend_python_tests_and_image() -> None:
    verify_gate(
        "pull_request",
        _classification(
            frontend=True,
            site_ui=True,
            python_tests=True,
            tutorial_e2e=True,
            matrix=("frontend",),
        ),
        _results(
            **{
                "frontend-quality": "success",
                "site-ui-containers": "success",
                "test": "success",
                "docker-build": "success",
                "tutorial-e2e": "success",
            }
        ),
    )


def _workflow_gate_results(results: str) -> str:
    """Render the workflow's actual result payload, including omitted dependencies."""
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["ci-gate"]["steps"]
        if step.get("name") == "Verify exact required CI job results"
    )
    values = json.loads(results)
    return re.sub(
        r"\$\{\{ toJSON\(needs\.([a-z0-9-]+)\.result\) \}\}",
        lambda match: json.dumps(values[match.group(1)]),
        step["env"]["JOB_RESULTS_JSON"],
    )


def test_ci_gate_waits_for_site_ui_container_checks() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    assert "site-ui-containers" in workflow["jobs"]["ci-gate"]["needs"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_frontend_pr_rejects_unsuccessful_site_ui_container_check(result: str) -> None:
    # Exercise the actual workflow payload: a job left out of it was invisible
    # to the verifier, even when both image compatibility builds failed.
    results = _workflow_gate_results(
        _results(
            **{
                "frontend-quality": "success",
                "site-ui-containers": result,
                "docker-build": "success",
            }
        )
    )
    with pytest.raises(ValueError, match=f"site-ui-containers: expected success, got {result}"):
        verify_gate(
            "pull_request",
            _classification(frontend=True, site_ui=True, matrix=("frontend",)),
            results,
        )


def _pr_results_for(outputs: ClassificationOutputs) -> dict[str, str]:
    """The job results a pull request with these outputs produces when all goes well."""
    flags = outputs.booleans

    def ran(selected: bool) -> str:
        return "success" if flags["full"] or selected else "skipped"

    return {
        "changes": "success",
        "security": "success",
        "backend-quality": ran(flags["backend"]),
        "frontend-quality": ran(flags["frontend"]),
        "site-ui-containers": ran(flags["site_ui"]),
        "test": ran(flags["python_tests"]),
        "docs-build": ran(flags["docs"]),
        "docker-build": "success" if outputs.docker_matrix else "skipped",
        "tutorial-e2e": ran(flags["tutorial_e2e"]),
    }


@pytest.mark.parametrize(
    "path",
    [
        "distributions/example/frontend/site-ui/client.tsx",
        "distributions/example/README.md",
        ".dockerignore",
        "deploy/docker/site-ui/.built-in-ui",
        "apps/frontend/scripts/site-ui/prepare-module.mjs",
    ],
)
def test_pr_site_ui_inputs_cannot_skip_the_container_checks(path: str) -> None:
    # From the classifier's real output through the gate. Each path is an input
    # of the Site UI builds. The example and `.dockerignore` are not frontend
    # source, and a PR touching only one of them skipped the job and still
    # passed the gate; the others are pinned so a narrower rule cannot drop them.
    classification = json.dumps(classify([path]).as_workflow_outputs())
    outputs = parse_classification(classification)
    assert outputs.booleans["site_ui"] is True

    expected = _pr_results_for(outputs)
    verify_gate("pull_request", classification, json.dumps(expected))

    expected["site-ui-containers"] = "skipped"
    with pytest.raises(ValueError, match="site-ui-containers: expected success, got skipped"):
        verify_gate("pull_request", classification, json.dumps(expected))


def test_example_module_pr_also_requires_frontend_quality() -> None:
    # The frontend's own tests stage and resolve the example module.
    classification = json.dumps(
        classify(["distributions/example/frontend/site-ui/client.tsx"]).as_workflow_outputs()
    )
    expected = _pr_results_for(parse_classification(classification))
    expected["frontend-quality"] = "skipped"

    with pytest.raises(ValueError, match="frontend-quality: expected success, got skipped"):
        verify_gate("pull_request", classification, json.dumps(expected))


def test_frontend_classification_must_select_the_site_ui_checks() -> None:
    with pytest.raises(ValueError, match="frontend changes must enable site_ui"):
        parse_classification(_classification(frontend=True, matrix=("frontend",)))


def test_docs_only_pr_accepts_legitimately_skipped_site_ui_container_check() -> None:
    verify_gate(
        "pull_request",
        _classification(docs=True, security_only=True),
        _workflow_gate_results(
            _results(**{"docs-build": "success", "site-ui-containers": "skipped"})
        ),
    )


def test_pr_tutorial_change_requires_tutorial_e2e() -> None:
    verify_gate(
        "pull_request",
        _classification(tutorial_e2e=True),
        _results(**{"tutorial-e2e": "success"}),
    )


def test_pr_tutorial_change_rejects_skipped_e2e() -> None:
    with pytest.raises(ValueError, match="tutorial-e2e"):
        verify_gate(
            "pull_request",
            _classification(tutorial_e2e=True),
            _results(),
        )


def test_pr_unrelated_change_rejects_unexpected_tutorial_e2e() -> None:
    with pytest.raises(ValueError, match="tutorial-e2e"):
        verify_gate(
            "pull_request",
            _classification(frontend=True, site_ui=True, matrix=("frontend",)),
            _results(
                **{
                    "frontend-quality": "success",
                    "site-ui-containers": "success",
                    "docker-build": "success",
                    "tutorial-e2e": "success",
                }
            ),
        )


def test_pr_docker_shared_runs_python_tests_and_all_images() -> None:
    verify_gate(
        "pull_request",
        _classification(
            docker_shared=True,
            python_tests=True,
            tutorial_e2e=True,
            matrix=("frontend", "backend"),
        ),
        _results(
            test="success",
            **{"docker-build": "success", "tutorial-e2e": "success"},
        ),
    )


def test_pr_full_requires_all_application_jobs_and_images() -> None:
    verify_gate(
        "pull_request",
        _classification(full=True, python_tests=True, matrix=("frontend", "backend")),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "site-ui-containers": "success",
                "docs-build": "success",
                "test": "success",
                "docker-build": "success",
                "tutorial-e2e": "success",
            }
        ),
    )


def test_push_requires_all_app_jobs_and_skips_docker() -> None:
    verify_gate(
        "push",
        _classification(frontend=True, site_ui=True, matrix=("frontend",)),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "site-ui-containers": "success",
                "docs-build": "success",
                "test": "success",
            }
        ),
    )


def test_push_runs_tutorial_e2e_only_when_classified() -> None:
    verify_gate(
        "push",
        _classification(frontend=True, site_ui=True, tutorial_e2e=True, matrix=("frontend",)),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "site-ui-containers": "success",
                "docs-build": "success",
                "test": "success",
                "tutorial-e2e": "success",
            }
        ),
    )


@pytest.mark.parametrize("event_name", ["schedule", "workflow_dispatch"])
def test_schedule_and_manual_require_all_app_jobs_and_docker(event_name: str) -> None:
    verify_gate(
        event_name,
        _classification(full=True, python_tests=True, matrix=("frontend", "backend")),
        _results(
            **{
                "backend-quality": "success",
                "frontend-quality": "success",
                "site-ui-containers": "success",
                "docs-build": "success",
                "test": "success",
                "docker-build": "success",
                "tutorial-e2e": "success",
            }
        ),
    )


def test_schedule_rejects_partial_docker_plan() -> None:
    with pytest.raises(ValueError, match="full three-image"):
        verify_gate(
            "schedule",
            _classification(frontend=True, site_ui=True, matrix=("frontend",)),
            _results(
                **{
                    "backend-quality": "success",
                    "frontend-quality": "success",
                    "site-ui-containers": "success",
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
    del payload["backend"]
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
