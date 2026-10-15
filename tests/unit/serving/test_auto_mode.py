"""The protocol side of auto mode (``serving/auto_mode.py``).

How a request is described to selection, which session it belongs to, how it is
rewritten for the chosen model, and how the ``auto:`` section is loaded. The
handlers that use these are covered in ``tests/servers/test_auto_mode_surfaces.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from routing.model_selection import (
    Candidate,
    Decision,
    RequestFeatures,
    SessionAction,
    TurnKind,
)
from serving.adapters.base import ModelConfig
from serving.auto_mode import (
    AutoSelection,
    anthropic_features,
    apply_to_anthropic_body,
    apply_to_chat_request,
    candidate_info,
    load_auto_mode,
    openai_features,
    session_key,
    strip_foreign_thinking,
)
from serving.utils.session_identity import SessionIdentity


def _cfg(**overrides) -> ModelConfig:
    return ModelConfig(
        **{"id": "m", "name": "m", "provider": "p", "base_url": "http://test", **overrides}
    )


def _selection(
    candidate: Candidate = Candidate("m"),
    *,
    action: SessionAction = SessionAction.NEW,
    turn: TurnKind = TurnKind.NEW_TURN,
    switches: int = 0,
) -> AutoSelection:
    decision = Decision(
        profile="balance",
        ranked=(candidate,),
        dropped=(),
        session_action=action,
        next_state=None,
        profile_version="v1",
        scorer_version="static-1",
    )
    return AutoSelection(
        requested="auto-balance",
        decision=decision,
        candidate=candidate,
        model_id=candidate.model_id,
        turn=turn,
        prompt_tokens=10,
        session_source=None,
        switches=switches,
    )


# -- What a model can serve -------------------------------------------------------


def test_a_model_with_several_endpoints_offers_what_they_all_do():
    info = candidate_info(
        [
            _cfg(
                context_length=200_000,
                max_output_length=32_000,
                input_modalities=["text", "image"],
                supports_tools=True,
                supported_params=["reasoning_effort"],
                reasoning_efforts=["low", "high", "max"],
            ),
            _cfg(
                context_length=128_000,
                max_output_length=16_000,
                input_modalities=["text"],
                supports_tools=True,
                supported_params=["reasoning_effort"],
                reasoning_efforts=["low", "high"],
            ),
        ]
    )

    assert info.context_length == 128_000
    assert info.max_output_length == 16_000
    assert info.input_modalities == frozenset({"text"})
    assert info.supports_tools
    assert info.reasoning_efforts == frozenset({"low", "high"})


def test_efforts_count_only_where_the_model_takes_the_parameter():
    """Declared efforts without ``reasoning_effort`` in supported_params are never sent."""
    info = candidate_info([_cfg(supported_params=["temperature"], reasoning_efforts=["high"])])

    assert info.reasoning_efforts == frozenset()


# -- Describing a request ---------------------------------------------------------


def _openai(messages, **overrides) -> RequestFeatures:
    return openai_features(
        messages,
        **{
            "tools": None,
            "response_format": None,
            "max_tokens": None,
            "required_modalities": frozenset(),
            **overrides,
        },
    )


def test_a_chat_whose_last_message_is_a_tool_result_is_in_its_tool_loop():
    loop = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]

    assert _openai(loop).turn is TurnKind.TOOL_CONTINUATION
    assert _openai([*loop, {"role": "user", "content": "thanks"}]).turn is TurnKind.NEW_TURN


def test_a_chat_request_carries_its_tools_limit_and_media():
    features = _openai(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f"}}],
        max_tokens=256,
        required_modalities=frozenset({"image"}),
    )

    assert features.uses_tools
    assert features.max_tokens == 256
    assert features.input_modalities == frozenset({"text", "image"})
    assert features.prompt_tokens > 0


def _anthropic(messages, *, side_call=False, **body) -> RequestFeatures:
    return anthropic_features(
        {"messages": messages, "max_tokens": 1024, **body}, prompt_tokens=10, side_call=side_call
    )


_TOOL_RESULT = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}


@pytest.mark.parametrize(
    ("last", "turn"),
    [
        pytest.param([_TOOL_RESULT], TurnKind.TOOL_CONTINUATION, id="tool-result"),
        pytest.param(
            [_TOOL_RESULT, {"type": "text", "text": "<system-reminder>...</system-reminder>"}],
            TurnKind.TOOL_CONTINUATION,
            id="tool-result-with-reminder",
        ),
        pytest.param([{"type": "text", "text": "next"}], TurnKind.NEW_TURN, id="text-blocks"),
        pytest.param("next", TurnKind.NEW_TURN, id="plain-text"),
    ],
)
def test_an_anthropic_request_is_in_its_tool_loop_while_it_returns_a_tool_result(last, turn):
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f"}]},
        {"role": "user", "content": last},
    ]

    assert _anthropic(messages).turn is turn


def test_a_safety_check_is_a_side_call_wherever_it_falls():
    assert _anthropic([{"role": "user", "content": "x"}], side_call=True).turn is (
        TurnKind.SIDE_CALL
    )


def test_an_image_counts_inside_a_tool_result_too():
    """That is how a coding agent reads a screenshot."""
    image = {"type": "image", "source": {"type": "base64", "data": "AAAA"}}
    screenshot = {"type": "tool_result", "tool_use_id": "t1", "content": [image]}

    assert _anthropic([{"role": "user", "content": [screenshot]}]).input_modalities == (
        frozenset({"text", "image"})
    )
    assert _anthropic([{"role": "user", "content": "hi"}]).input_modalities == frozenset({"text"})


def test_an_anthropic_request_carries_its_tools_and_limit():
    features = _anthropic([{"role": "user", "content": "hi"}], tools=[{"name": "f"}])

    assert features.uses_tools
    assert features.max_tokens == 1024


# -- Sessions ---------------------------------------------------------------------


def test_a_session_is_keyed_by_caller_declared_session_and_opening():
    declared = SessionIdentity("s-1", "header")
    user = {"user_id": "u-1"}

    key, source = session_key(user, declared, "fp")

    assert source == "declared"
    assert key != session_key({"user_id": "u-2"}, declared, "fp")[0]
    assert key != session_key(user, SessionIdentity("s-2", "header"), "fp")[0]
    assert key != session_key(user, declared, "other-fp")[0]
    assert key != session_key({**user, "agent_grant_id": "g-1"}, declared, "fp")[0]


def test_without_a_declared_session_the_opening_identifies_the_conversation():
    key, source = session_key({"user_id": "u-1"}, None, "fp")

    assert source == "conversation"
    assert key == session_key({"user_id": "u-1"}, None, "fp")[0]


def test_with_neither_there_is_no_session():
    assert session_key({"user_id": "u-1"}, None, None) is None


# -- Rewriting the request --------------------------------------------------------


def test_a_chat_request_takes_the_model_and_the_candidates_effort():
    payload = SimpleNamespace(model="auto-balance", reasoning_effort=None)
    selection = _selection(Candidate("m", "high"))

    apply_to_chat_request(selection, payload)

    assert (payload.model, payload.reasoning_effort) == ("m", "high")
    assert selection.effort_applied
    assert selection.log_metadata()["effort_applied"] == "high"


def test_a_chat_request_keeps_the_callers_own_effort():
    payload = SimpleNamespace(model="auto-balance", reasoning_effort="low")
    selection = _selection(Candidate("m", "high"))

    apply_to_chat_request(selection, payload)

    assert payload.reasoning_effort == "low"
    assert not selection.effort_applied


def test_an_anthropic_body_gets_the_effort_where_claude_code_puts_its_own():
    body = {"model": "auto-balance", "output_config": {"format": {"type": "json"}}}

    apply_to_anthropic_body(_selection(Candidate("m", "high")), body)

    assert body == {"model": "m", "output_config": {"format": {"type": "json"}, "effort": "high"}}


@pytest.mark.parametrize(
    "own",
    [{"output_config": {"effort": "low"}}, {"reasoning_effort": "low"}],
    ids=["output-config", "top-level"],
)
def test_an_anthropic_body_keeps_the_callers_own_effort(own):
    body = {"model": "auto-balance", **own}
    selection = _selection(Candidate("m", "high"))

    apply_to_anthropic_body(selection, body)

    assert body == {"model": "m", **own}
    assert not selection.effort_applied


def _thinking(text: str) -> dict:
    return {"type": "thinking", "thinking": text, "signature": ""}


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [_thinking("a"), {"type": "tool_use", "id": "t1"}]},
        {"role": "user", "content": [_TOOL_RESULT]},
        {"role": "assistant", "content": [_thinking("b"), {"type": "tool_use", "id": "t2"}]},
        {"role": "user", "content": [_TOOL_RESULT]},
    ]


def test_thinking_is_left_alone_while_the_session_has_had_one_model():
    body = {"messages": _history()}

    assert strip_foreign_thinking(_selection(switches=0), body) == 0
    assert body["messages"] == _history()


def test_after_a_switch_the_thinking_in_the_history_is_removed():
    body = {"messages": _history()}
    selection = _selection(action=SessionAction.FORCED_SWITCH, turn=TurnKind.TOOL_CONTINUATION)
    selection.switches = 1

    assert strip_foreign_thinking(selection, body) == 2
    assert [m["content"] for m in body["messages"] if m["role"] == "assistant"] == [
        [{"type": "tool_use", "id": "t1"}],
        [{"type": "tool_use", "id": "t2"}],
    ]


def test_the_current_models_last_thinking_stays_while_its_tool_loop_goes_on():
    body = {"messages": _history()}
    selection = _selection(action=SessionAction.KEEP, turn=TurnKind.TOOL_CONTINUATION, switches=1)

    assert strip_foreign_thinking(selection, body) == 1
    assert body["messages"][3]["content"][0] == _thinking("b")


def test_an_assistant_message_of_thinking_alone_is_dropped():
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "x"}]},
            {"role": "user", "content": "again"},
        ]
    }

    assert strip_foreign_thinking(_selection(switches=1), body) == 1
    assert [m["role"] for m in body["messages"]] == ["user", "user"]


# -- Loading the section ----------------------------------------------------------


_SECTION = """
models: []
auto:
  profiles:
    balance:
      candidates:
        - {model: known}
        - {model: missing}
"""


def _routes(*names: str) -> dict:
    adapter = SimpleNamespace(config=_cfg(context_length=8_000, max_output_length=1_000))
    return {name: SimpleNamespace(adapters=[(adapter, 1.0)]) for name in names}


def test_a_section_loads_and_an_unregistered_candidate_is_only_a_warning(tmp_path, caplog):
    path = tmp_path / "models.yaml"
    path.write_text(_SECTION)

    with caplog.at_level(logging.WARNING):
        auto_mode = load_auto_mode(path, _routes("known"))

    assert auto_mode is not None
    assert auto_mode.config.resolve("auto").name == "balance"
    assert "missing: model is not registered" in caplog.text


def test_a_file_without_a_section_has_no_auto_mode(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text("models: []\n")

    assert load_auto_mode(path, _routes("known")) is None


def test_an_invalid_section_turns_auto_off_and_says_why(tmp_path, caplog):
    path = tmp_path / "models.yaml"
    path.write_text("auto:\n  default_profile: cheap\n  profiles: {}\n")

    with caplog.at_level(logging.ERROR):
        assert load_auto_mode(path, _routes("known")) is None
    assert "Auto mode is off: auto.profiles" in caplog.text


def test_a_tier_named_like_a_registered_model_turns_auto_off(tmp_path, caplog):
    path = tmp_path / "models.yaml"
    path.write_text(_SECTION)

    with caplog.at_level(logging.ERROR):
        assert load_auto_mode(path, _routes("known", "auto-balance")) is None
    assert "'auto-balance' is a registered model name" in caplog.text
