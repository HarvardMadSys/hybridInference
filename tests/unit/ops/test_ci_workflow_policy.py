"""Regression tests for CI filtering and CD trigger safety."""

import os
import re
import subprocess
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
        "publish-console-precheck",
        "publish-console-arm64",
        "publish-console-amd64",
        "publish-console",
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


@pytest.mark.parametrize("image", ["backend", "console"])
def test_publish_stitch_requires_both_native_halves(image: str) -> None:
    """The dev-<sha> tag is the completeness signal; only the stitch mints it.

    The stitch must gate on BOTH arch results explicitly — with !cancelled()
    suppressing the implicit success(), nothing else stops it from tagging a
    half-published candidate whose other half failed.
    """
    job = _workflow("ci.yml")["jobs"][f"publish-{image}"]

    assert set(job["needs"]) == {
        "ci-gate",
        f"publish-{image}-precheck",
        f"publish-{image}-arm64",
        f"publish-{image}-amd64",
    }
    assert f"needs.publish-{image}-arm64.result == 'success'" in job["if"]
    assert f"needs.publish-{image}-amd64.result == 'success'" in job["if"]


@pytest.mark.parametrize("image", ["backend", "console"])
def test_publish_existence_verdict_has_a_single_source(image: str) -> None:
    """One Packages API query, shared by every publish job.

    Independent per-job existence queries can disagree only through a
    transient API error (nothing mints the tag between them), and a
    disagreement wedges the pipeline: one side builds, the other skips,
    and the stitch has neither a digest pair nor a verdict it trusts.
    Every downstream job must therefore read the precheck's output, and no
    publish job other than the precheck may query the Packages API itself.
    """
    jobs = _workflow("ci.yml")["jobs"]
    verdict = f"needs.publish-{image}-precheck.outputs.existing"

    for name in (f"publish-{image}-arm64", f"publish-{image}-amd64"):
        assert f"publish-{image}-precheck" in jobs[name]["needs"], name
        gated = [step for step in jobs[name]["steps"] if step.get("if") == f"{verdict} == ''"]
        assert gated, f"{name}: no step obeys the precheck verdict"
        assert not any(
            "/packages/container/" in str(step.get("run", "")) for step in jobs[name]["steps"]
        ), f"{name}: runs its own existence query"

    stitch = next(step for step in jobs[f"publish-{image}"]["steps"] if step.get("id") == "stitch")
    assert stitch["env"]["EXISTING"] == "${{ " + verdict + " }}"


@pytest.mark.parametrize("image", ["backend", "console"])
def test_publish_precheck_discards_packages_api_error_payload(image: str) -> None:
    """A Packages API error body must never masquerade as an image digest.

    ``gh api`` writes its JSON error body to stdout. The first console publish
    therefore treated a 404 ``Package not found`` body as a non-empty digest
    when ``|| true`` erased the command failure, and skipped both native builds.
    ``pipefail`` plus an explicit failure branch must clear that payload.
    """
    job = _workflow("ci.yml")["jobs"][f"publish-{image}-precheck"]
    lookup = next(step for step in job["steps"] if step.get("id") == "existing")["run"]

    assert "set -euo pipefail" in lookup
    assert 'if ! digest="$(gh api ' in lookup
    assert "|| true" not in lookup
    assert 'digest=""' in lookup
    assert "Package lookup failed" in lookup


def test_console_candidate_build_has_no_distribution_identity_inputs() -> None:
    """The canonical console is the white-label artifact W7 promises."""
    jobs = _workflow("ci.yml")["jobs"]

    for arch in ("arm64", "amd64"):
        job = jobs[f"publish-console-{arch}"]
        step = next(item for item in job["steps"] if item.get("id") == "push")
        build = step["with"]
        args = str(build["build-args"])

        assert build["file"] == "deploy/docker/Dockerfile.frontend"
        assert "hybridinference-console" in build["outputs"]
        assert "NEXT_PUBLIC_API_BASE=" in args
        assert "NEXT_PUBLIC_BUILD_SHA=${{ github.sha }}" in args
        assert "NEXT_PUBLIC_BUILD_TIMESTAMP=${{ github.event.head_commit.timestamp }}" in args
        assert "NEXT_PUBLIC_APP_NAME" not in args
        assert "NEXT_PUBLIC_TEAM_JSON" not in args
        assert "NEXT_PUBLIC_SPONSORS_JSON" not in args
        assert "AGENT_WEB_INTERNAL_URL" not in args
        assert "AGENT_CONTROL_PLANE_INTERNAL_URL" not in args


def test_both_console_platforms_scan_the_published_payload_for_identity() -> None:
    """Build arguments are policy; inspect the actual bytes before stitching."""
    jobs = _workflow("ci.yml")["jobs"]

    for arch in ("arm64", "amd64"):
        job = jobs[f"publish-console-{arch}"]
        scan = next(
            step
            for step in job["steps"]
            if step.get("name", "").startswith("Refuse distribution identity")
        )
        command = scan["run"]

        assert scan["env"]["DIGEST"] == "${{ steps.push.outputs.digest }}"
        assert "docker pull" in command
        assert "docker run --rm" in command
        assert "freeinference" in command
        assert "http://web:3000" in command
        assert "http://control-plane:8000" in command


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


def test_site_ui_signal_reaches_the_container_job_and_the_gate() -> None:
    workflow = _workflow("ci.yml")
    jobs = workflow["jobs"]
    changes = jobs["changes"]
    validate = next(step for step in changes["steps"] if step.get("id") == "validate")
    gate = next(
        step
        for step in jobs["ci-gate"]["steps"]
        if step.get("name") == "Verify exact required CI job results"
    )

    assert changes["outputs"]["site_ui"] == "${{ steps.validate.outputs.site_ui }}"
    assert '"site_ui":' in validate["env"]["CLASSIFICATION_JSON"]
    assert '"site_ui":' in gate["env"]["CLASSIFICATION_JSON"]
    assert "needs.changes.outputs.site_ui == 'true'" in jobs["site-ui-containers"]["if"]


def test_site_ui_container_job_cannot_strand_containers() -> None:
    """Per-run names and ports, and a teardown that runs whatever failed.

    On a self-hosted runner a fixed container name left by a failed run made
    every later run fail at `docker run --name`, and the removal ran only when
    every step before it had passed.
    """
    job = _workflow("ci.yml")["jobs"]["site-ui-containers"]
    steps = job["steps"]
    by_name = {step.get("name"): step for step in steps}
    runs = "\n".join(str(step.get("run", "")) for step in steps)

    assert job["env"]["SITE_UI_RUN"] == "site-ui-${{ github.run_id }}-${{ github.run_attempt }}"
    assert '--name "${SITE_UI_RUN}-example"' in runs
    assert "13001" not in runs
    assert '"127.0.0.1:${SITE_UI_PORT}:3001"' in runs

    teardown = by_name["Remove this run's Site UI containers and images"]
    assert teardown["if"] == "always()"
    assert 'docker rm -f "${SITE_UI_RUN}-example"' in teardown["run"]
    assert '"${SITE_UI_RUN}-neutral:ci" "${SITE_UI_RUN}-example:ci"' in teardown["run"]
    assert steps[-1] is teardown


def test_w5c_retired_services_and_ci_wiring_are_absent() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    retired_paths = (
        ROOT / "services/status-monitor-worker",
        ROOT / "services/alert-control-plane-worker",
        ROOT / "services/freeinference-harness",
    )

    assert all(not path.exists() for path in retired_paths)
    assert "alert-control-plane-check" not in jobs
    assert "alert-control-plane-check" not in jobs["ci-gate"]["needs"]
    assert "status_monitor" not in jobs["changes"]["outputs"]
    assert "alert_control_plane" not in jobs["changes"]["outputs"]


def test_w5d_retired_ops_assets_are_absent() -> None:
    """The distribution repository owns the db toolkit, proxies, and units.

    Production cron and the staging proxy unit already execute the consuming
    repository's copies, so these upstream copies must not come back. The
    backend-coupled analysis remnant under ops/db/ stays deliberately.
    """
    retired_paths = (
        ROOT / "ops/lib",
        ROOT / "ops/h200_idle_proxy",
        ROOT / "ops/spark_idle_proxy",
        ROOT / "ops/local_deployment_proxy",
        ROOT / "ops/db/backup.sh",
        ROOT / "ops/db/restore.sh",
        ROOT / "ops/db/export_logs.py",
        ROOT / "ops/db/analysis/trace_viewer",
        ROOT / "deploy/systemd/spark_idle_proxy.service",
        ROOT / "deploy/systemd/spark_idle_tunnel@.service",
        ROOT / "deploy/systemd/h200_idle_proxy.service",
        ROOT / "deploy/systemd/h200_idle_proxy_b.service",
        ROOT / "deploy/systemd/h200_idle_tunnel@.service",
        ROOT / "deploy/systemd/h200_idle_tunnel_b@.service",
        ROOT / "deploy/systemd/h200_ondemand_proxy.service",
        ROOT / "deploy/systemd/h200_ondemand_tunnel@.service",
        ROOT / "deploy/systemd/local_deployment_proxy.service",
        ROOT / "deploy/systemd/local_deployment_tunnel@.service",
    )

    assert all(not path.exists() for path in retired_paths)
    kept_remnant = (
        ROOT / "ops/db/analysis/user_automation_score.py",
        ROOT / "ops/db/analysis/user_prompt_sample.py",
        ROOT / "ops/db/analysis/geo_hourly_export.py",
        ROOT / "ops/db/backfill_num_user_turns.py",
    )
    assert all(path.exists() for path in kept_remnant)


def test_backend_matrix_keeps_the_fast_stage_one_smoke() -> None:
    """The full transition supplements rather than replaces the quick smoke."""
    steps = _workflow("ci.yml")["jobs"]["docker-build"]["steps"]
    by_name = {step.get("name"): step for step in steps}

    assert by_name["Start runnable router example"]["run"] == ("make up DISTRIBUTION=example")
    assert "make smoke DISTRIBUTION=example" in by_name["Smoke runnable router example"]["run"]
    assert by_name["Start runnable router example"]["if"] == "matrix.image == 'backend'"
    assert by_name["Smoke runnable router example"]["if"] == "matrix.image == 'backend'"


def test_compose_jobs_cannot_strand_docker_volumes() -> None:
    """Both Compose stacks name a project per run, on runners that never forget.

    A volume the project declares but no service mounts survives
    `down --volumes`, so CI stranded one per run until the disk filled. The
    teardown removes whatever still carries the run's project label and fails
    the job, and each run reaps what a hard-cancelled predecessor left behind.
    """
    workflow = _workflow("ci.yml")

    for job, teardown_name in (
        ("docker-build", "Stop runnable router example"),
        ("tutorial-e2e", "Reset tutorial E2E"),
    ):
        by_name = {step.get("name"): step for step in workflow["jobs"][job]["steps"]}

        reaper = by_name["Reap Compose volumes stranded by older CI runs"]["run"]
        assert "'name=^hi-tutorial-'" in reaper
        assert "'name=^hi-example-'" in reaper
        # Far past this workflow's longest job timeout, so the reaper can never
        # reach a sibling job running concurrently on the same runner.
        assert "6 * 60 * 60" in reaper
        assert 'docker volume rm "${volume}"' in reaper

        teardown = by_name[teardown_name]
        assert str(teardown["if"]).startswith("always()")
        assert "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" in teardown["run"]
        assert "xargs -r docker volume rm" in teardown["run"]
        assert "left volumes behind" in teardown["run"]
        assert "exit 1" in teardown["run"]

    timeouts = [workflow["jobs"][job]["timeout-minutes"] for job in ("tutorial-e2e",)]
    assert max(timeouts) * 60 < 6 * 60 * 60


def test_tutorial_e2e_builds_and_runs_the_exact_linear_transition() -> None:
    workflow = _workflow("ci.yml")
    changes = workflow["jobs"]["changes"]
    job = workflow["jobs"]["tutorial-e2e"]
    steps = job["steps"]
    by_name = {step.get("name"): step for step in steps}

    assert changes["outputs"]["tutorial_e2e"] == "${{ steps.validate.outputs.tutorial_e2e }}"
    assert "needs.changes.outputs.tutorial_e2e == 'true'" in job["if"]
    assert "github.event_name != 'pull_request'" not in job["if"]
    # PR events (untrusted after the repo is public) run GitHub-hosted; every
    # trusted event keeps the self-hosted image-verify pool.
    assert job["runs-on"] == (
        "${{ github.event_name == 'pull_request' && 'ubuntu-24.04-arm'"
        ' || fromJSON(\'["self-hosted", "Linux", "ARM64", "image-verify-arm64"]\') }}'
    )

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
    tutorial_shell = "\n".join(str(step.get("run", "")) for step in steps)
    assert "docker inspect " not in tutorial_shell
    assert "docker container inspect" in tutorial_shell
    assert by_name["Start tutorial Stage 1"]["run"] == "make up DISTRIBUTION=example"
    assert "make smoke DISTRIBUTION=example" in stage1
    assert "provider_id=" in stage1
    assert "backend_container_id=" in stage1
    assert "backend_image_id=" in stage1
    assert "Stage 1 unexpectedly started" in stage1
    assert "Stage 1 unexpectedly created the example database volume" in stage1
    assert '"name": "Reloaded Example Chat"' in stage1
    assert "make up DISTRIBUTION=example" in stage1
    assert "make restart s=backend DISTRIBUTION=example" not in stage1
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
    # Stage 2 stored the upstream settings in the example database, where they
    # outrank the environment: Stage 3 changes the stored values, then
    # recreates the backend so the model registry reads them, then smokes.
    assert "--configure-upstream" in stage3
    assert "make demo DISTRIBUTION=example" in stage3
    assert '"http://localhost:${BACKEND_PORT}/routing"' in stage3
    # Read anonymously, /routing must not name the upstream; the smoke's
    # expected content is what proves requests reach the host fixture.
    assert 'if grep -qF "host.docker.internal" <<< "${routing}"; then' in stage3
    assert "anonymous /routing disclosed an upstream address" in stage3
    assert "EXAMPLE_DEMO_EXPECT_EXISTING=1" in stage3
    assert "make demo-smoke DISTRIBUTION=example" in stage3
    assert (
        stage3.index("--configure-upstream")
        < stage3.index("make demo DISTRIBUTION=example")
        < stage3.index("make demo-smoke DISTRIBUTION=example")
    )
    restore_drill = by_name["Restore the previous backend on the migrated database"]["run"]
    assert "make demo-previous DISTRIBUTION=example" in restore_drill
    # The drill rolls back as `make rollback` does: to the recorded image's ID.
    assert "docker container inspect --format '{{.Image}}'" in restore_drill
    assert "docker image inspect --format '{{.Id}}' \"${image}\"" in restore_drill
    assert restore_drill.index("make demo-previous DISTRIBUTION=example") < restore_drill.index(
        "make demo DISTRIBUTION=example"
    )
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
    assert "for network in hybridinference pgadmin; do" in reset
    assert '"${COMPOSE_PROJECT_NAME}_${network}"' in reset
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


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda path: path.name)
def test_every_workflow_states_its_token_permissions(path: Path) -> None:
    """A workflow without a ``permissions`` block gets the repository default.

    That default can be read-write on every scope, and these jobs run on
    self-hosted runners a push reaches.
    """
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert "permissions" in workflow, path.name


def test_ci_is_read_only_unless_a_job_asks_for_more() -> None:
    workflow = _workflow("ci.yml")

    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        scripts = "\n".join(str(step.get("run", "")) for step in job.get("steps") or [])
        logs_in = any(
            "login-action" in str(step.get("uses", "")) for step in job.get("steps") or []
        )
        if "/packages/" in scripts or "ghcr.io" in scripts or logs_in:
            # The Packages API and the registry need the package scope; the
            # token has none of it by default any more.
            assert (job.get("permissions") or {}).get("packages") in ("read", "write"), name


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_commit(path: Path) -> None:
    """A tag can be moved to other code; a commit cannot.

    Each pin names the release it was taken from, so an update reads as one.
    """
    pinned = re.compile(r"^\s*(?:-\s+)?uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*\s*$")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if re.match(r"^\s*(?:-\s+)?uses:", line) and "uses: ./" not in line:
            assert pinned.match(line), f"{path.name}:{number}: {line.strip()}"


def _jobs() -> list[tuple[str, str, dict, dict]]:
    out = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            out.append((path.name, name, job, workflow))
    return out


def test_every_registry_login_stays_in_a_docker_config_of_its_own() -> None:
    """The runner account's ``~/.docker/config.json`` is shared by every job on its host.

    Push CI on unreviewed branches runs on the same self-hosted hosts as the
    publish jobs, so a ``packages: write`` login written there could be read
    and reused by other code for as long as the publishing job ran.
    """
    isolate = 'echo "DOCKER_CONFIG=${docker_config}" >> "${GITHUB_ENV}"'
    remove = 'rm -f "${RUNNER_TEMP}/docker-config/config.json"'
    logins = 0
    for workflow, name, job, _ in _jobs():
        steps = job.get("steps") or []
        for index, step in enumerate(steps):
            run = str(step.get("run", ""))
            if "docker/login-action" in str(step.get("uses", "")):
                logins += 1
                earlier = "\n".join(str(s.get("run", "")) for s in steps[:index])
                assert 'docker_config="${RUNNER_TEMP}/docker-config"' in earlier, (workflow, name)
                assert isolate in earlier, (workflow, name)
                assert steps[-1].get("if") == "always()", (workflow, name)
                assert remove in str(steps[-1].get("run", "")), (workflow, name)
            for line in run.splitlines():
                if "docker login" in line:
                    logins += 1
                    # An inline login names its own directory, removed however
                    # the step ends.
                    assert re.search(r'DOCKER_CONFIG="\$\{\w+\}" docker login', line), line
                    assert "trap 'rm -rf" in run, (workflow, name)
    # Six publish jobs, the diagnostic build and the tutorial's restore drill.
    assert logins >= 8


def test_no_checkout_leaves_a_package_token_in_the_workspace() -> None:
    """actions/checkout writes its token into .git/config unless told not to.

    Where the token has a packages scope, that is a registry credential left in
    a persistent runner's workspace for the length of the job; nothing in these
    jobs fetches again after the checkout.
    """
    checkouts = 0
    for workflow, name, job, defaults in _jobs():
        permissions = job.get("permissions", defaults.get("permissions"))
        if isinstance(permissions, dict) and "packages" not in permissions:
            continue
        for step in job.get("steps") or []:
            if "actions/checkout@" in str(step.get("uses", "")):
                checkouts += 1
                assert (step.get("with") or {}).get("persist-credentials") is False, (
                    workflow,
                    name,
                )
    assert checkouts >= 6


def test_no_job_caches_a_venv_that_uv_venv_replaces() -> None:
    """`uv venv` replaces .venv, so a restored one was downloaded and thrown away.

    What keeps the sync fast is the uv cache, setup-uv's ``enable-cache``.
    """
    syncing = 0
    for workflow, name, job, _ in _jobs():
        steps = job.get("steps") or []
        for step in steps:
            if "actions/cache@" in str(step.get("uses", "")):
                paths = str((step.get("with") or {}).get("path", "")).splitlines()
                assert not [p for p in paths if p.strip().rstrip("/").endswith(".venv")], (
                    workflow,
                    name,
                )
        if any("uv venv" in str(step.get("run", "")) for step in steps):
            syncing += 1
            (setup,) = [s for s in steps if "astral-sh/setup-uv@" in str(s.get("uses", ""))]
            assert setup["with"].get("enable-cache") is True, (workflow, name)
    assert syncing >= 3


# The self-hosted runners run under accounts that have no sudo, one account per
# repository, side by side on the same machines. So a step that a self-hosted
# runner can take must not call sudo, and must not use a fixed name in the
# machines' shared /tmp, where a file another account left behind cannot be
# replaced (fs.protected_regular).
SUDO = re.compile(r"(?:^|[\s;&|(!`{])sudo(?=\s|$)")
SHARED_TMP = re.compile(r"(?:^|[\s=:'\"(])/tmp/")
HOSTED_ONLY = "runner.environment == 'github-hosted'"


def _script(step: dict) -> str:
    """A step's shell without its comment lines, which may name sudo freely."""
    lines = str(step.get("run", "")).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _self_hosted_jobs() -> list[tuple[str, str, dict]]:
    """Jobs whose ``runs-on`` can name a self-hosted runner, for any event."""
    return [
        (workflow, name, job)
        for workflow, name, job, _ in _jobs()
        if "self-hosted" in str(job.get("runs-on", ""))
    ]


def test_no_step_a_self_hosted_runner_can_run_calls_sudo() -> None:
    """Their accounts have no sudo, so such a step fails on every trusted event.

    A step that only a hosted runner may take says so in its own condition, as
    a conjunct the whole condition depends on.
    """
    jobs = _self_hosted_jobs()
    for workflow, name, job in jobs:
        for step in job.get("steps") or []:
            if not SUDO.search(_script(step)):
                continue
            condition = str(step.get("if", "")).removeprefix("${{").removesuffix("}}")
            conjuncts = [part.strip() for part in condition.split("&&")]
            assert HOSTED_ONLY in conjuncts, (workflow, name, step.get("name"))
    # The CI jobs a push reaches, the publish jobs, the diagnostic build and
    # the release: the check above looked where it matters.
    assert len(jobs) >= 15


def test_gitleaks_installs_into_runner_temp_and_onto_the_path(tmp_path: Path) -> None:
    """The scanner lands in this runner's own temp, never in /usr/local/bin.

    The step runs with recorders in place of the network, the archive and sudo.
    Afterwards the binary is on GITHUB_PATH for the scans that follow, and
    nothing was written into the working tree the next step scans.
    """
    workflow = _workflow("ci.yml")
    (step,) = [
        step
        for step in workflow["jobs"]["security"]["steps"]
        if step.get("name") == "Install gitleaks"
    ]
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner-temp"
    github_path = tmp_path / "github-path"
    calls = tmp_path / "calls"
    workspace.mkdir()
    runner_temp.mkdir()
    # Functions, not executables on PATH: shell startup cannot reorder them
    # behind the host's real curl, tar or sudo.
    recorders = r"""
uname() { echo x86_64; }
curl() {
  printf '%s\n' "curl $*" >> "${CALLS}"
  url="${*: -1}"
  printf '0000  %s\n' "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" > "${url##*/}"
}
sha256sum() { printf '%s\n' "sha256sum $*" >> "${CALLS}"; }
tar() { printf '%s\n' "tar $*" >> "${CALLS}"; printf '#!/bin/sh\necho 0.0.0\n' > gitleaks; }
sudo() { printf '%s\n' "sudo $*" >> "${CALLS}"; return 1; }
"""
    env = {key: value for key, value in os.environ.items() if key != "BASH_ENV"}
    env.update(
        CALLS=str(calls),
        GITHUB_PATH=str(github_path),
        GITLEAKS_VERSION=str(workflow["env"]["GITLEAKS_VERSION"]),
        RUNNER_TEMP=str(runner_temp),
    )
    subprocess.run(["bash", "-c", recorders + step["run"]], cwd=workspace, env=env, check=True)

    assert github_path.read_text().splitlines() == [str(runner_temp / "bin")]
    installed = runner_temp / "bin" / "gitleaks"
    assert installed.is_file()
    assert os.access(installed, os.X_OK)
    assert list(workspace.iterdir()) == []
    recorded = calls.read_text().splitlines()
    assert [line for line in recorded if line.startswith("tar ")]
    assert not [line for line in recorded if line.startswith("sudo")]


def test_no_self_hosted_step_uses_a_fixed_name_in_the_shared_tmp() -> None:
    """Every runner account on a machine shares its sticky /tmp.

    A file another account left at a fixed name there cannot be replaced, so the
    step fails on whichever account comes second. RUNNER_TEMP belongs to one
    runner process, and mktemp picks a fresh name.
    """
    for workflow, name, job in _self_hosted_jobs():
        for step in job.get("steps") or []:
            assert not SHARED_TMP.search(_script(step)), (workflow, name, step.get("name"))
