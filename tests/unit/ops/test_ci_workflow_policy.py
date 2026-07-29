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


def test_docs_only_filters_apply_to_push_but_not_pull_requests() -> None:
    triggers = _triggers(_workflow("ci.yml"))
    ignored = [
        "docs/**",
        "distributions/freeinference/content/docs/**",
        "**/*.md",
        "LICENSE",
        ".gitignore",
    ]

    assert triggers["push"]["paths-ignore"] == ignored
    assert "paths-ignore" not in triggers["pull_request"]
    assert triggers["schedule"]


def test_python_tests_signal_controls_only_the_pytest_job() -> None:
    jobs = _workflow("ci.yml")["jobs"]

    assert "needs.changes.outputs.python_tests == 'true'" in jobs["test"]["if"]
    assert "needs.changes.outputs.backend == 'true'" in jobs["backend-quality"]["if"]
