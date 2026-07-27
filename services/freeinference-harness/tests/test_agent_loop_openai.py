"""End-to-end tests: agent-loop executors against the fake provider.

These exercise the exact code path the CLI uses (executor + OpenAI client),
with the fake standing in for the provider. Running the same suite through a
dev gateway (gateway-local target) additionally pins the translation layer.
"""

from __future__ import annotations

import pytest

from freeinference_harness.agent_loop import run_agent_loop_openai
from freeinference_harness.agent_scripts import DS4_MALFORMED_ARGUMENTS
from freeinference_harness.clients.openai_compat import OpenAICompatClient
from freeinference_harness.models import ScenarioConfig, TargetConfig


def _target(base_url: str) -> TargetConfig:
    """Builds a fake-direct target."""
    return TargetConfig(
        name="fake-direct",
        model="agent-loop-fake",
        base_url=base_url,
        api_key="harness-local",
        suite_type="conformance",
        timeout_seconds=20.0,
        sampling_count=1,
    )


def _client(target: TargetConfig) -> OpenAICompatClient:
    """Builds the OpenAI-surface client for a target."""
    return OpenAICompatClient(
        base_url=target.base_url,
        api_key=target.api_key,
        timeout_seconds=target.timeout_seconds,
    )


def _scenario(script_id: str) -> ScenarioConfig:
    """Builds an agent-loop scenario for one script."""
    return ScenarioConfig(
        scenario_id=f"openai_{script_id}",
        scenario_type="agent_loop_openai",
        agent_script=script_id,
    )


@pytest.mark.parametrize(
    "script_id",
    [
        "basic_tool_roundtrip",
        "fragmented_args",
        "ds4_malformed_args",
        "empty_content",
        "rate_limited_then_ok",
        "midstream_disconnect",
    ],
)
def test_agent_loop_scripts_pass_against_fake(fake_base_url, script_id):
    """Every conformance script passes over the fake-direct chain."""
    target = _target(fake_base_url)
    result = run_agent_loop_openai(_client(target), target, _scenario(script_id))
    assert result["status"] == "pass", result["detail"]


def test_ds4_arguments_survive_splicing_verbatim(fake_base_url):
    """The observed spliced arguments are the exact incident bytes."""
    target = _target(fake_base_url)
    result = run_agent_loop_openai(_client(target), target, _scenario("ds4_malformed_args"))
    assert result["status"] == "pass", result["detail"]
    assert result["observed"]["first_tool"]["arguments"] == DS4_MALFORMED_ARGUMENTS


def test_429_retry_is_recorded(fake_base_url):
    """The retry path notes that a 429 was actually observed and retried."""
    target = _target(fake_base_url)
    result = run_agent_loop_openai(_client(target), target, _scenario("rate_limited_then_ok"))
    assert result["status"] == "pass", result["detail"]
    assert result["observed"]["saw_429"] is True
    assert result["observed"]["retried_after_429"] is True


def test_unknown_script_fails_cleanly(fake_base_url):
    """Unknown script ids produce a classified failure, not an exception."""
    target = _target(fake_base_url)
    result = run_agent_loop_openai(_client(target), target, _scenario("no-such-script"))
    assert result["status"] == "fail"
    assert result["failure_type"] == "unknown_agent_script"


def test_cancel_mid_stream_does_not_wedge_the_fake(fake_base_url):
    """Client abort mid-stream leaves the conversation usable."""
    from freeinference_harness.agent_loop import run_agent_loop_cancel

    target = _target(fake_base_url)
    scenario = ScenarioConfig(
        scenario_id="openai_cancel_midstream",
        scenario_type="agent_loop_cancel",
        agent_script="cancel_mid_stream",
    )
    result = run_agent_loop_cancel(target, scenario)
    assert result["status"] == "pass", result["detail"]
    assert result["observed"]["aborted_after_events"] >= 2
    assert "AFTER_CANCEL_OK" in result["observed"]["follow_up"]["content_preview"]


def test_missing_runtime_binary_skips_cleanly(fake_base_url, monkeypatch):
    """Runtime scenarios skip (not fail) when the CLI is not installed."""
    import freeinference_harness.runtime_drivers as drivers

    monkeypatch.setattr(drivers.shutil, "which", lambda _name: None)
    target = _target(fake_base_url)
    scenario = ScenarioConfig(
        scenario_id="runtime_codex_smoke",
        scenario_type="runtime_codex_smoke",
        agent_script="runtime_smoke",
    )
    result = drivers.run_runtime_codex_smoke(target, scenario)
    assert result["status"] == "skip"
    assert result["failure_type"] == "runtime_missing"
