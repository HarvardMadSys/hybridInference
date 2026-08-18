import re
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

ROOT = Path(__file__).parents[3]
WORKFLOW_PATH = ROOT / ".github/workflows/deploy-status-monitor.yml"
MONITOR = ROOT / "services/status-monitor-worker"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_status_monitor_deploy_identity_is_pinned_and_fail_closed():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}

    job = workflow["jobs"]["deploy"]
    assert job["runs-on"] == ["self-hosted", "Linux", "deploy-edge"]
    assert job["environment"] == "staging"
    steps = {step["name"]: step for step in job["steps"]}
    assert steps["Check out"]["with"]["persist-credentials"] is False

    upload = steps["Upload the Worker version without activating it"]["run"]
    assert "WRANGLER_OUTPUT_FILE_PATH" in upload
    # `versions upload` creates a version without serving it, which is what lets
    # attestation and the cutover gate run while the old version is still live.
    assert "wrangler versions upload" in upload
    assert 'item.type === "version-upload"' in upload
    assert "item.worker_name === process.env.WORKER_NAME" in upload
    assert "version_id=${versionId}" in upload

    attest = steps["Attest the exact deployed Worker version"]["run"]
    assert "alert-control-plane-deployment-attestation" in attest
    assert 'service: "status-monitor"' in attest
    assert 'source: "status-monitor"' in attest
    assert "deployment_id: deploymentId" in attest
    assert "deployment_sha: sha" in attest
    assert "producer_token" not in attest
    # Target and principal are resolved from the config being shipped, never
    # written here: a literal is what let #1252 move the gateway and leave the
    # environment every page reported behind.
    assert "target_environment: process.env.TARGET_ENVIRONMENT" in attest
    assert "principal: process.env.TARGET_PRINCIPAL" in attest
    assert '"staging-monitor"' not in attest


def test_status_monitor_deploys_both_instances_from_resolved_config():
    """Each instance's identity comes from the config it ships, not the matrix.

    The matrix carries only which wrangler environment to read. Target,
    principal, Worker name and database are all resolved from that
    environment's own config, so the pair that #1252 let drift apart cannot.
    """
    job = _workflow()["jobs"]["deploy"]

    # One failing must not cancel the other's cutover: they are separate
    # deployments with separate databases and separate incident namespaces.
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == [
        {"instance": "production", "wrangler_env": ""},
        {"instance": "staging", "wrangler_env": "staging"},
    ]

    steps = {step["name"]: step for step in job["steps"]}
    resolve = steps["Resolve this instance's deploy parameters"]["run"]
    assert "scripts/deploy-parameters.ts" in resolve

    attest = steps["Attest the exact deployed Worker version"]["env"]
    assert attest["TARGET_ENVIRONMENT"] == "${{ steps.target.outputs.targetEnvironment }}"
    assert attest["TARGET_PRINCIPAL"] == "${{ steps.target.outputs.principal }}"

    # Each instance locks, gates and migrates its own database — a shared name
    # here would have one instance's cutover block the other's probing.
    for name in (
        "Apply D1 migrations",
        "Acquire the cutover lock",
        "Require no incident in flight before cutting over",
        "Release the cutover lock",
    ):
        assert steps[name]["env"]["DATABASE_NAME"] == "${{ steps.target.outputs.databaseName }}", (
            name
        )


def test_status_monitor_cutover_is_ordered_and_mutually_exclusive():
    """The activation sequence, pinned in order.

    `target_environment` and `principal` are incident-route material, so
    activating a version that changes either moves every incident to a new
    Durable Object. Anything that lets a probe cycle run across that move splits
    one outage between two objects and strands the first half open.
    """
    names = [step["name"] for step in _workflow()["jobs"]["deploy"]["steps"]]

    def order(name: str) -> int:
        assert name in names, f"missing step: {name}"
        return names.index(name)

    assert (
        order("Apply D1 migrations")
        < order("Upload the Worker version without activating it")
        < order("Attest the exact deployed Worker version")
        < order("Acquire the cutover lock")
        < order("Require no incident in flight before cutting over")
        < order("Activate the attested version")
        < order("Release the cutover lock")
    )

    steps = {step["name"]: step for step in _workflow()["jobs"]["deploy"]["steps"]}

    # Held across the gate and the activation, so no cycle can start between the
    # answer and the change it authorises; released whatever happens, so a
    # failed cutover cannot wedge probing behind a lock nobody owns.
    lock = steps["Acquire the cutover lock"]["run"]
    assert "cycle_lock" in lock
    assert "CAST(meta.value AS INTEGER) <" in lock
    assert steps["Release the cutover lock"]["if"].startswith("always()")

    # The lock has to outlive the job it protects. A TTL below `timeout-minutes`
    # lets a slow cutover keep running after its own lock expires, so a cron
    # takes it back mid-flight and the exclusion lapses exactly when it is being
    # relied on.
    ttl_seconds = int(re.search(r"\+ (\d+)\) \* 1000", lock).group(1))
    assert ttl_seconds > _workflow()["jobs"]["deploy"]["timeout-minutes"] * 60

    gate = steps["Require no incident in flight before cutting over"]["run"]
    assert "alert_state" in gate
    assert "cycle_alert" in gate
    assert "alert_delivery_owner:v1:%" in gate
    assert "alert_delivery_pending:v1:%" in gate

    # The gate blocks only when route material actually moves. Principal is
    # derived from the target, so an unchanged target means the incoming version
    # addresses every incident object exactly as the running one does. Enforcing
    # regardless would make the monitor undeployable during any outage — when a
    # fix is most likely needed — and deadlocks re-attestation, because a pending
    # transition cannot drain until the running version is attested and attesting
    # requires this deploy.
    assert "last_cycle_target_environment" in gate
    assert "deployedTarget === incomingTarget" in gate
    assert (
        steps["Require no incident in flight before cutting over"]["env"]["TARGET_ENVIRONMENT"]
        == "${{ steps.target.outputs.targetEnvironment }}"
    )

    # The cleanup exists for a version that was attested but never went live, so
    # it must precede every post-activation step. Below one, a failure there
    # would retire the registration of a version that is serving — the Worker
    # keeps running while the control plane stops trusting it, and alerting goes
    # silent.
    # Conditioned on the activation's own outcome, not on `failure()` alone.
    # Ordering cannot carry this by itself: releasing the lock, applying
    # triggers and the binding gate all have to run after activation, and a
    # failure in any of them makes `failure()` true. Retiring then would
    # deregister a version that is serving.
    retire = steps["Retire the attested version if it never went live"]["if"]
    assert "failure()" in retire
    assert "steps.activate.outcome != 'success'" in retire
    assert steps["Activate the attested version"]["id"] == "activate"

    # Triggers are Worker-level settings and no part of a version, so the
    # versions flow does not carry them; without this step a cron change would
    # upload, attest and activate cleanly while the schedule stayed put.
    assert "wrangler triggers deploy" in steps["Apply trigger changes"]["run"]


def test_status_monitor_service_binding_gate_is_local_and_non_public():
    gate = tomllib.loads(
        (MONITOR / "wrangler.service-binding-gate.toml").read_text(),
    )
    assert gate["workers_dev"] is False
    assert gate["preview_urls"] is False
    assert gate["services"] == [
        {
            "binding": "ALERT_CONTROL_PLANE",
            "service": "freeinference-alert-control-plane-staging",
            "entrypoint": "StatusMonitorProducerEntrypoint",
            "remote": True,
        },
    ]

    steps = {step["name"]: step for step in _workflow()["jobs"]["deploy"]["steps"]}
    live = steps["Run the non-public Service Binding RPC gate"]
    command = live["run"]
    assert "--ip 127.0.0.1" in command
    assert "--config wrangler.service-binding-gate.toml" in command
    assert "--remote" not in command
    assert "--tunnel" not in command
    assert "SLACK_" not in str(live)


def test_status_monitor_caller_bindings_cut_over_individual_model_alerts():
    config = tomllib.loads((MONITOR / "wrangler.toml").read_text())
    assert config["vars"]["ALERT_DEFAULT_OWNER"] == "control-plane"
    assert config["version_metadata"] == {"binding": "CF_VERSION_METADATA"}
    assert config["services"] == [
        {
            "binding": "ALERT_CONTROL_PLANE",
            "service": "freeinference-alert-control-plane-staging",
            "entrypoint": "StatusMonitorProducerEntrypoint",
        },
    ]

    index_source = (MONITOR / "src/index.ts").read_text()
    assert "await runAlerts(env, config, results)" in index_source
    assert "submitPendingCanonicalEvent" not in index_source

    alerts_source = (MONITOR / "src/alerts.ts").read_text()
    assert "submitPendingCanonicalEvent" in alerts_source
    assert 'fingerprint.startsWith("status-monitor:storm:")' in alerts_source
    assert "runCycleAlert" in alerts_source


def test_status_monitor_pins_remote_binding_capable_wrangler():
    package = yaml.safe_load((MONITOR / "package.json").read_text())
    assert package["devDependencies"]["wrangler"] == "4.114.0"
