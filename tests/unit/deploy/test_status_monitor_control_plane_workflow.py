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

    deploy = steps["Deploy"]["run"]
    assert "WRANGLER_OUTPUT_FILE_PATH" in deploy
    assert 'item.type === "deploy"' in deploy
    assert 'item.worker_name === "freeinference-monitor"' in deploy
    assert "version_id=${versionId}" in deploy

    attest = steps["Attest the exact deployed Worker version"]["run"]
    assert "alert-control-plane-deployment-attestation" in attest
    assert 'service: "status-monitor"' in attest
    assert 'source: "status-monitor"' in attest
    assert 'principal: "staging-monitor"' in attest
    assert "deployment_id: deploymentId" in attest
    assert "deployment_sha: sha" in attest
    assert "producer_token" not in attest


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


def test_status_monitor_caller_bindings_stay_legacy_owned():
    config = tomllib.loads((MONITOR / "wrangler.toml").read_text())
    assert config["vars"]["ALERT_DEFAULT_OWNER"] == "legacy"
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


def test_status_monitor_pins_remote_binding_capable_wrangler():
    package = yaml.safe_load((MONITOR / "package.json").read_text())
    assert package["devDependencies"]["wrangler"] == "4.114.0"
