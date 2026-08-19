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


def test_no_trigger_is_path_filtered_so_every_sha_gets_a_run() -> None:
    """A path filter here withholds the run itself, not just the jobs.

    Pushes carried a docs-only `paths-ignore` until #1283 made every green dev
    SHA publish a backend candidate for the distribution repo's bump bot. A
    filtered push creates no workflow run at all, so nothing downstream can
    rescue it — not a job-level condition, and not the staging deploy that
    hangs off this workflow's completion. Cheapness is the per-job change
    classification's job, which is asserted separately.
    """
    triggers = _triggers(_workflow("ci.yml"))

    for event in ("push", "pull_request"):
        assert "paths-ignore" not in (triggers[event] or {}), event
        assert "paths" not in (triggers[event] or {}), event

    assert triggers["schedule"]


def test_publish_gate_survives_skipped_ancestors() -> None:
    """Publishing must depend on ci-gate's verdict, not on ancestor luck.

    A job `if` without a status function gets an implicit success() that
    evaluates the whole transitive needs chain; docker-build — an ancestor
    through ci-gate — is legitimately skipped on many pushes, and that
    implicit check silently skipped publishing (observed on dev@04b4f305:
    gate green, publish skipped). !cancelled() suppresses the implicit
    check so the explicit conditions are the only gate.
    """
    job = _workflow("ci.yml")["jobs"]["publish-backend"]

    assert job["needs"] == ["ci-gate"]
    assert "!cancelled()" in job["if"]
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
