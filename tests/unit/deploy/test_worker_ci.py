"""CI contract for Cloudflare Worker checks."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def test_ci_runs_both_worker_suites_without_deploying():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["worker-check"]
    entries = job["strategy"]["matrix"]["include"]

    assert {entry["path"] for entry in entries} == {
        "services/status-monitor-worker",
        "services/alert-relay-worker",
    }
    command_steps = [step for step in job["steps"] if step.get("run")]
    commands = [step["run"] for step in command_steps]
    assert commands == ["npm ci", "npm run typecheck", "npm test"]
    assert all(step["working-directory"] == "${{ matrix.path }}" for step in command_steps)
    assert all("deploy" not in command for command in commands)


def test_relay_scripts_use_copied_ignored_wrangler_config():
    package = json.loads(
        (ROOT / "services/alert-relay-worker/package.json").read_text(encoding="utf-8")
    )

    assert "--config wrangler.toml" in package["scripts"]["dev"]
    assert "--config wrangler.toml" in package["scripts"]["migrate:local"]
    assert "wrangler.toml.example" not in json.dumps(package["scripts"])


def test_queue_retry_policy_has_one_source_of_runtime_configuration():
    worker_root = ROOT / "services/alert-relay-worker"
    wrangler = (worker_root / "wrangler.toml.example").read_text(encoding="utf-8")
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (worker_root / "src").glob("*.ts")
    )

    assert "max_retries = 2" in wrangler
    assert "MAX_QUEUE_DELIVERY_ATTEMPTS = 3" in source
    assert "QUEUE_MAX_ATTEMPTS" not in wrangler + source


def test_relay_has_no_fixed_workflow_ref_configuration():
    worker_root = ROOT / "services/alert-relay-worker"
    checked = "\n".join(
        [
            (worker_root / "wrangler.toml.example").read_text(encoding="utf-8"),
            (worker_root / "README.md").read_text(encoding="utf-8"),
            *[path.read_text(encoding="utf-8") for path in (worker_root / "src").glob("*.ts")],
        ]
    )

    assert "GITHUB_WORKFLOW_REF" not in checked
