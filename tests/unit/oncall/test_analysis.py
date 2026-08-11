"""Tests for the shared analysis helpers and the agent-event groundedness gate."""

import pytest

from serving.oncall.analysis import (
    final_message_text,
    parse_analysis_output,
    render_prompt,
    render_schema,
    validate_agent_events,
)


def _analysis_json() -> str:
    return (
        '{"summary": "s", "classification": "capacity", "confidence": 0.5,'
        ' "impact": "i", "evidence": [], "likely_cause": "c",'
        ' "recommended_actions": ["a"], "issue_recommendation": "none",'
        ' "draft_pr_recommendation": "none"}'
    )


def test_render_prompt_embeds_alert_and_schema():
    prompt = render_prompt({"title": "boom"}, render_schema())
    assert "<untrusted_alert_json>" in prompt
    assert '"title": "boom"' in prompt
    assert '"classification"' in prompt  # the schema went in
    assert "read-only" in prompt


def test_validate_agent_events_requires_one_successful_command():
    validate_agent_events(
        [
            {"event_type": "message", "payload": {"text": "thinking"}},
            {"event_type": "tool_result", "payload": {"exit_code": 0, "content": "ok"}},
        ]
    )


def test_validate_agent_events_rejects_only_failing_commands():
    events = [
        {"event_type": "tool_result", "payload": {"exit_code": 1, "content": "boom"}},
        {"event_type": "tool_result", "payload": {"exit_code": 127, "content": "nope"}},
    ]
    with pytest.raises(ValueError, match="no successful command"):
        validate_agent_events(events)


def test_validate_agent_events_rejects_pure_prose():
    with pytest.raises(ValueError, match="no successful command"):
        validate_agent_events([{"event_type": "message", "payload": {"text": "trust me"}}])


def test_validate_agent_events_tolerates_recovered_errors():
    # A transient mid-run error the agent recovered from does not un-ground
    # the run — the platform's terminal state already says it completed.
    validate_agent_events(
        [
            {"event_type": "error", "payload": {"text": "rate limited"}},
            {"event_type": "tool_result", "payload": {"exit_code": 0, "content": "ok"}},
        ]
    )


def test_final_message_text_takes_the_last_message():
    events = [
        {"event_type": "message", "payload": {"text": "progress note"}},
        {"event_type": "tool_result", "payload": {"exit_code": 0}},
        {"event_type": "message", "payload": {"text": _analysis_json()}},
        {"event_type": "lifecycle", "payload": {"phase": "result"}},
    ]
    assert final_message_text(events) == _analysis_json()


def test_final_message_text_skips_blank_messages():
    events = [
        {"event_type": "message", "payload": {"text": _analysis_json()}},
        {"event_type": "message", "payload": {"text": "   "}},
    ]
    assert final_message_text(events) == _analysis_json()


def test_final_message_text_refuses_a_silent_run():
    with pytest.raises(ValueError, match="no final message"):
        final_message_text([{"event_type": "tool_result", "payload": {"exit_code": 0}}])


def test_parse_analysis_tolerates_commentary_around_one_object():
    analysis = parse_analysis_output(f"Here is my answer:\n```json\n{_analysis_json()}\n```")
    assert analysis.classification == "capacity"


def test_parse_analysis_refuses_two_valid_objects():
    doubled = f"{_analysis_json()}\n{_analysis_json()}"
    with pytest.raises(ValueError, match="multiple valid JSON objects"):
        parse_analysis_output(doubled)
