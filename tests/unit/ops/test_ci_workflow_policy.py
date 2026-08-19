"""Regression tests for CI filtering and CD trigger safety."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted ``on`` key as boolean True.
    return workflow.get("on") or workflow[True]


@pytest.mark.parametrize("name", ["deploy.yml", "deploy-staging.yml"])
def test_cd_accepts_only_manual_or_successful_push_ci(name: str) -> None:
    condition = _workflow(name)["jobs"]["deploy"]["if"]

    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event.workflow_run.event == 'push'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition


def test_push_runs_are_never_path_filtered() -> None:
    """Every dev SHA must produce a workflow run, without exception.

    The distribution repository's bump bot pins exact SHAs and waits for a
    published backend candidate per SHA; a path-filtered push creates no run
    at all, so the publish job could never fire for it and the bot would wait
    forever (policy flipped 2026-08-19 with the publish-backend job — the
    old docs-only paths-ignore is deliberately gone, on pushes and pull
    requests alike).
    """
    triggers = _triggers(_workflow("ci.yml"))

    assert "paths-ignore" not in triggers["push"]
    assert "paths" not in triggers["push"]
    assert "paths-ignore" not in triggers["pull_request"]
    assert triggers["schedule"]


def test_every_green_dev_push_publishes_a_candidate() -> None:
    """The publish job is the reason push filtering is banned; pin its shape."""
    jobs = _workflow("ci.yml")["jobs"]
    job = jobs["publish-backend"]

    assert job["needs"] == ["ci-gate"]
    assert "github.event_name == 'push'" in job["if"]
    assert "github.ref == 'refs/heads/dev'" in job["if"]
    assert "needs.ci-gate.result == 'success'" in job["if"]


def test_python_tests_signal_controls_only_the_pytest_job() -> None:
    jobs = _workflow("ci.yml")["jobs"]

    assert "needs.changes.outputs.python_tests == 'true'" in jobs["test"]["if"]
    assert "needs.changes.outputs.backend == 'true'" in jobs["backend-quality"]["if"]


def test_alert_service_quality_runs_all_checks_in_one_job() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    job = jobs["alert-control-plane-check"]

    assert job["name"] == "Alert Service Quality"
    assert "strategy" not in job
    assert "alert-control-plane-check" in jobs["ci-gate"]["needs"]

    run_steps = [(step.get("name"), step["run"]) for step in job["steps"] if "run" in step]
    assert run_steps == [
        ("Install dependencies", "npm ci"),
        ("Run TypeScript check", "npm run typecheck"),
        ("Run tests", "npm test"),
        (
            "Run Wrangler deployment dry run",
            "npm exec -- wrangler deploy --dry-run --config wrangler.example.toml",
        ),
    ]

    install_step = next(step for step in job["steps"] if step.get("id") == "dependencies")
    assert install_step["run"] == "npm ci"
    assert all(
        step.get("if") == "always() && steps.dependencies.outcome == 'success'"
        for step in job["steps"]
        if step.get("run") in {"npm run typecheck", "npm test"}
        or str(step.get("run", "")).startswith("npm exec -- wrangler")
    )
