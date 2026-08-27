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
        "rag-index.yml",
    ],
)
def test_retired_workflows_are_absent(workflow_name: str) -> None:
    assert not (WORKFLOWS / workflow_name).exists()


def test_manual_candidate_cannot_be_mistaken_for_lock_input() -> None:
    steps = _workflow("build-candidates.yml")["jobs"]["build"]["steps"]
    tags = next(
        step["with"]["tags"] for step in steps if step.get("name") == "Build and push backend"
    )
    summary = next(step["run"] for step in steps if step.get("name") == "Candidate summary")

    assert "manual-${{ github.sha }}" in tags
    assert "dev-" not in tags
    assert "Diagnostic only: upstream.lock never consumes manual-* tags" in summary
    assert "automatic dev CI multi-arch candidate" in summary
    assert "Deploy Staging by Digest" not in summary


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


def test_backend_matrix_keeps_the_fast_stage_one_smoke() -> None:
    """The full transition supplements rather than replaces the quick smoke."""
    steps = _workflow("ci.yml")["jobs"]["docker-build"]["steps"]
    by_name = {step.get("name"): step for step in steps}

    assert by_name["Start runnable router example"]["run"] == ("make up DISTRIBUTION=example")
    assert "make smoke DISTRIBUTION=example" in by_name["Smoke runnable router example"]["run"]
    assert by_name["Start runnable router example"]["if"] == "matrix.image == 'backend'"
    assert by_name["Smoke runnable router example"]["if"] == "matrix.image == 'backend'"


def test_tutorial_e2e_builds_and_runs_the_exact_linear_transition() -> None:
    workflow = _workflow("ci.yml")
    changes = workflow["jobs"]["changes"]
    job = workflow["jobs"]["tutorial-e2e"]
    steps = job["steps"]
    by_name = {step.get("name"): step for step in steps}

    assert changes["outputs"]["tutorial_e2e"] == "${{ steps.validate.outputs.tutorial_e2e }}"
    assert "needs.changes.outputs.tutorial_e2e == 'true'" in job["if"]
    assert "github.event_name != 'pull_request'" not in job["if"]
    assert job["runs-on"] == ["self-hosted", "Linux", "ARM64", "image-verify-arm64"]

    expected_names = {
        "EXAMPLE_BACKEND_CONTAINER_NAME",
        "EXAMPLE_PROVIDER_CONTAINER_NAME",
        "EXAMPLE_FRONTEND_CONTAINER_NAME",
        "EXAMPLE_POSTGRES_CONTAINER_NAME",
    }
    assert expected_names <= job["env"].keys()
    assert len({job["env"][name] for name in expected_names}) == 4
    assert "BACKEND_PORT" not in job["env"]
    assert "DB_PORT" not in job["env"]
    tutorial_ports = by_name["Select tutorial ports"]["run"]
    assert "BACKEND_PORT=${backend_port}" in tutorial_ports
    assert "FRONTEND_PORT=${frontend_port}" in tutorial_ports
    assert "DB_PORT=${db_port}" in tutorial_ports
    assert "EXAMPLE_RESET_STATE_FILE=${RUNNER_TEMP}/hi-tutorial-" in tutorial_ports

    backend_build = by_name["Build exact tutorial backend image"]["with"]
    frontend_build = by_name["Build exact same-origin tutorial frontend image"]["with"]
    for build in (backend_build, frontend_build):
        assert build["load"] is True
        assert build["push"] is False
    assert backend_build["file"] == "deploy/docker/Dockerfile.backend"
    assert backend_build["tags"] == "${{ env.COMPOSE_PROJECT_NAME }}-backend"
    assert frontend_build["file"] == "deploy/docker/Dockerfile.frontend"
    assert frontend_build["tags"] == "${{ env.COMPOSE_PROJECT_NAME }}-frontend"
    assert "NEXT_PUBLIC_API_BASE=" in frontend_build["build-args"]
    assert "BACKEND_INTERNAL_URL=http://backend:8080" in frontend_build["build-args"]

    stage1 = by_name["Smoke tutorial Stage 1 and record container state"]["run"]
    promote = by_name["Promote Stage 1 to the full local demo"]["run"]
    demo_smoke = by_name["Smoke the full local demo"]["run"]
    stage3 = by_name["Route Stage 3 through a host-side local provider"]["run"]
    resume_smoke = by_name["Preserve state across full stop and resume"]["run"]
    reset_smoke = by_name["Verify destructive reset isolation"]["run"]
    reset = by_name["Reset tutorial E2E"]["run"]
    assert by_name["Start tutorial Stage 1"]["run"] == "make up DISTRIBUTION=example"
    assert "make smoke DISTRIBUTION=example" in stage1
    assert "provider_id=" in stage1
    assert "backend_container_id=" in stage1
    assert "backend_image_id=" in stage1
    assert "Stage 1 unexpectedly started" in stage1
    assert "Stage 1 unexpectedly created the example database volume" in stage1
    assert "name: Reloaded Example Chat" in stage1
    assert "make restart s=backend DISTRIBUTION=example" in stage1
    assert 'test "${backend_image_after}" = "${backend_image_before}"' in stage1
    assert "make demo DISTRIBUTION=example" in promote
    assert 'test "${provider_id}" = "${{ steps.stage1-state.outputs.provider_id }}"' in promote
    assert (
        'test "${backend_container_id}" != '
        '"${{ steps.stage1-state.outputs.backend_container_id }}"' in promote
    )
    assert "grep -qx 'DB_ENABLED=true'" in promote
    assert "grep -qx 'USER_AUTH_ENABLED=true'" in promote
    assert 'DEMO_BASE_URL="http://localhost:${port}"' in demo_smoke
    assert 'DEMO_SMOKE_STATE_FILE="${EXAMPLE_RESET_STATE_FILE}"' in demo_smoke
    assert "make demo-smoke DISTRIBUTION=example" in demo_smoke
    assert "distributions/example/fixtures/fake-openai-provider/server.py" in stage3
    assert "--host 0.0.0.0" in stage3
    assert "--response-text STAGE3_HOST_FIXTURE_OK" in stage3
    assert "--expected-model host-fixture-model" in stage3
    assert "--expected-api-key local-placeholder" in stage3
    assert "trap stop_fixture EXIT" in stage3
    assert 'kill "${fixture_pid}"' in stage3
    assert 'wait "${fixture_pid}"' in stage3
    assert (
        'EXAMPLE_UPSTREAM_BASE_URL="http://host.docker.internal:${host_provider_port}/v1"' in stage3
    )
    assert 'EXAMPLE_UPSTREAM_API_KEY="local-placeholder"' in stage3
    assert 'EXAMPLE_UPSTREAM_MODEL="host-fixture-model"' in stage3
    assert 'EXAMPLE_EXPECTED_CONTENT="STAGE3_HOST_FIXTURE_OK"' in stage3
    assert "make demo DISTRIBUTION=example" in stage3
    assert "EXAMPLE_DEMO_EXPECT_EXISTING=1" in stage3
    assert "make demo-smoke DISTRIBUTION=example" in stage3
    assert "make demo-down DISTRIBUTION=example" in resume_smoke
    assert resume_smoke.index("tutorial-provider.log") < resume_smoke.index(
        "make demo-down DISTRIBUTION=example"
    )
    assert 'docker volume inspect "${volume}"' in resume_smoke
    assert "make demo DISTRIBUTION=example" in resume_smoke
    assert 'DEMO_SMOKE_EXPECT_STATE_FILE="${EXAMPLE_RESET_STATE_FILE}"' in resume_smoke
    assert "make demo-smoke DISTRIBUTION=example" in resume_smoke
    assert "make demo-reset DISTRIBUTION=example" in reset_smoke
    assert reset_smoke.index("tutorial-provider.log") < reset_smoke.index(
        "make demo-reset DISTRIBUTION=example"
    )
    assert '"${COMPOSE_PROJECT_NAME}_example_postgres_data"' in reset_smoke
    assert "docker volume inspect" in reset_smoke
    assert "make demo DISTRIBUTION=example" in reset_smoke
    assert '--verify-reset-state "${EXAMPLE_RESET_STATE_FILE}"' in reset_smoke
    step_names = [step.get("name") for step in steps]
    assert step_names.index("Smoke the full local demo") < step_names.index(
        "Route Stage 3 through a host-side local provider"
    )
    assert step_names.index("Preserve state across full stop and resume") < (
        step_names.index("Route Stage 3 through a host-side local provider")
    )
    assert step_names.index("Route Stage 3 through a host-side local provider") < (
        step_names.index("Verify destructive reset isolation")
    )
    assert "make demo-reset DISTRIBUTION=example" in reset
    assert reset.index('rm -f "${EXAMPLE_RESET_STATE_FILE}"') < reset.index(
        "make demo-reset DISTRIBUTION=example"
    )
    assert "make demo-reset DISTRIBUTION=example || true" not in reset
    assert "tutorial cleanup left ${leaked} behind" in reset
    assert '"${COMPOSE_PROJECT_NAME}_example_postgres_data"' in reset
    assert '"${COMPOSE_PROJECT_NAME}_hybridinference"' in reset
    assert 'rm -f "${EXAMPLE_RESET_STATE_FILE}"' in reset
    assert "test ! -e var" in reset
    assert "git diff --exit-code" in reset
    assert "git status --porcelain --untracked-files=all" in reset
    assert "make down" not in "\n".join(str(step.get("run", "")) for step in steps)

    logs = by_name["Show tutorial E2E logs"]["run"]
    assert all(name in logs for name in expected_names)
    assert "tutorial-host-provider.log" in logs
    for service in ("provider", "postgres", "backend", "frontend"):
        assert f"tutorial-{service}.log" in logs
    assert by_name["Reset tutorial E2E"]["if"] == "always()"

    gate = workflow["jobs"]["ci-gate"]
    assert "tutorial-e2e" in gate["needs"]
    gate_env = next(
        step["env"]
        for step in gate["steps"]
        if step.get("name") == "Verify exact required CI job results"
    )
    assert '"tutorial_e2e":' in gate_env["CLASSIFICATION_JSON"]
    assert '"tutorial-e2e":' in gate_env["JOB_RESULTS_JSON"]
