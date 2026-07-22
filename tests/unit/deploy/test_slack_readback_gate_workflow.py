from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def _workflow() -> dict:
    path = ROOT / ".github/workflows/slack-readback-gate.yml"
    return yaml.safe_load(path.read_text())


def test_slack_readback_gate_is_manual_serial_and_read_only():
    workflow = _workflow()
    trigger = workflow.get("on") or workflow.get(True)

    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"channel_id", "sink_id", "confirmation"}
    assert all(specification["required"] is True for specification in inputs.values())
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "slack-readback-gate",
        "cancel-in-progress": False,
    }


def test_slack_readback_gate_keeps_the_token_step_scoped():
    workflow = _workflow()
    job = workflow["jobs"]["verify"]
    assert job["timeout-minutes"] == 20

    steps = {step["name"]: step for step in job["steps"]}
    live_step = steps["Run target-workspace live gate"]
    assert live_step["env"]["SLACK_BOT_TOKEN"] == ("${{ secrets.CODEX_ONCALL_SLACK_BOT_TOKEN }}")
    assert live_step["env"]["SLACK_CHANNEL_ID"] == "${{ inputs.channel_id }}"
    assert live_step["env"]["SLACK_SINK_ID"] == "${{ inputs.sink_id }}"
    assert "npm run test:slack-live" in live_step["run"]

    for name, step in steps.items():
        if name != "Run target-workspace live gate":
            assert "SLACK_BOT_TOKEN" not in step.get("env", {})
        assert "secrets." not in step.get("run", "")
        assert "inputs." not in step.get("run", "")
