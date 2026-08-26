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


@pytest.mark.parametrize(
    "workflow_name",
    [
        "deploy.yml",
        "deploy-rollback.yml",
        "deploy-staging.yml",
        "deploy-staging-digest.yml",
    ],
)
def test_legacy_deploy_workflows_are_retired(workflow_name: str) -> None:
    assert not (WORKFLOWS / workflow_name).exists()


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


@pytest.mark.parametrize(
    "job_name",
    [
        "publish-backend-precheck",
        "publish-backend-arm64",
        "publish-backend-amd64",
        "publish-backend",
    ],
)
def test_publish_gate_survives_skipped_ancestors(job_name: str) -> None:
    """Publishing must depend on ci-gate's verdict, not on ancestor luck.

    A job `if` without a status function gets an implicit success() that
    evaluates the whole transitive needs chain; docker-build — an ancestor
    through ci-gate — is legitimately skipped on many pushes, and that
    implicit check silently skipped publishing (observed on dev@04b4f305:
    gate green, publish skipped). !cancelled() suppresses the implicit
    check so the explicit conditions are the only gate — on every job of
    the publish pipeline, arch halves and stitch alike.
    """
    job = _workflow("ci.yml")["jobs"][job_name]

    assert "ci-gate" in job["needs"]
    assert "!cancelled()" in job["if"]
    assert "github.event_name == 'push'" in job["if"]
    assert "github.ref == 'refs/heads/dev'" in job["if"]
    assert "needs.ci-gate.result == 'success'" in job["if"]


def test_publish_stitch_requires_both_native_halves() -> None:
    """The dev-<sha> tag is the completeness signal; only the stitch mints it.

    The stitch must gate on BOTH arch results explicitly — with !cancelled()
    suppressing the implicit success(), nothing else stops it from tagging a
    half-published candidate whose other half failed.
    """
    job = _workflow("ci.yml")["jobs"]["publish-backend"]

    assert set(job["needs"]) == {
        "ci-gate",
        "publish-backend-precheck",
        "publish-backend-arm64",
        "publish-backend-amd64",
    }
    assert "needs.publish-backend-arm64.result == 'success'" in job["if"]
    assert "needs.publish-backend-amd64.result == 'success'" in job["if"]


def test_publish_existence_verdict_has_a_single_source() -> None:
    """One Packages API query, shared by every publish job.

    Independent per-job existence queries can disagree only through a
    transient API error (nothing mints the tag between them), and a
    disagreement wedges the pipeline: one side builds, the other skips,
    and the stitch has neither a digest pair nor a verdict it trusts.
    Every downstream job must therefore read the precheck's output, and no
    publish job other than the precheck may query the Packages API itself.
    """
    jobs = _workflow("ci.yml")["jobs"]
    verdict = "needs.publish-backend-precheck.outputs.existing"

    for name in ("publish-backend-arm64", "publish-backend-amd64"):
        assert "publish-backend-precheck" in jobs[name]["needs"], name
        gated = [step for step in jobs[name]["steps"] if step.get("if") == f"{verdict} == ''"]
        assert gated, f"{name}: no step obeys the precheck verdict"
        assert not any(
            "/packages/container/" in str(step.get("run", "")) for step in jobs[name]["steps"]
        ), f"{name}: runs its own existence query"

    stitch = next(step for step in jobs["publish-backend"]["steps"] if step.get("id") == "stitch")
    assert stitch["env"]["EXISTING"] == "${{ " + verdict + " }}"


def test_publish_builds_are_native_never_emulated() -> None:
    """Candidate builds run natively per arch and are stitched afterwards.

    qemu-user cannot run uv's static binary: the emulated amd64 half of a
    combined multi-platform build segfaulted `uv sync` instantly (observed
    on dev@2e4edfa4, exit code 139). Scoped to ci.yml — the dispatch-only
    Build Candidate Images tool takes an operator-chosen platform input and
    is that operator's judgment call.
    """
    jobs = _workflow("ci.yml")["jobs"]

    for name, job in jobs.items():
        for step in job.get("steps") or []:
            assert "setup-qemu-action" not in str(step.get("uses", "")), name
            platforms = str((step.get("with") or {}).get("platforms", ""))
            assert "," not in platforms, f"{name}: emulated multi-platform build"


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
