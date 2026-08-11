from __future__ import annotations

import pytest
from pydantic import ValidationError

from serving.schemas import ChatCompletionRequest, ModelList
from serving.schemas_admin import UpdateUserRequest


@pytest.mark.unit
def test_chat_completion_request_basic_and_extra_ignored():
    req = ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
            ],
            "temperature": 0.7,
            "extra_field": "ignored",
        }
    )
    assert req.model == "m"
    assert req.temperature == 0.7
    # Ensure extra not present on instance
    assert not hasattr(req, "extra_field")


@pytest.mark.unit
def test_chat_completion_request_preserves_assistant_reasoning_content():
    req = ChatCompletionRequest.model_validate(
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "user", "content": "inspect this"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "I should inspect the file first.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            ],
        }
    )

    dumped = [message.model_dump() for message in req.messages]
    assert dumped[1]["reasoning_content"] == "I should inspect the file first."


@pytest.mark.unit
def test_chat_completion_request_accepts_developer_role_as_system():
    """OpenAI's developer role is accepted and folded to system.

    Clients written against the reasoning-model spec (pi, for one) put their
    instructions under ``developer``; sglang answers the role itself with
    "Unexpected message role", so only ``system`` may leave the gateway.
    """
    req = ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "developer", "content": "instructions"},
                {"role": "user", "content": "u"},
            ],
        }
    )

    assert req.messages[0].role == "system"
    assert [m.model_dump()["role"] for m in req.messages] == ["system", "user"]


@pytest.mark.unit
def test_developer_message_merges_with_an_existing_system_message():
    """system + developer becomes one leading system message, not two.

    Relabelling alone would trade "Unexpected message role" for "System
    message must be at the beginning" — both 400s from the same local server.
    """
    req = ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "developer", "content": "prefer tables"},
                {"role": "user", "content": "u"},
            ],
        }
    )

    assert [m.role for m in req.messages] == ["system", "user"]
    assert req.messages[0].content == "be terse\n\nprefer tables"


@pytest.mark.unit
def test_developer_message_after_a_turn_is_hoisted_to_the_front():
    """A developer message mid-conversation still leaves as a leading system."""
    req = ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "developer", "content": "now be terse"},
                {"role": "user", "content": "second"},
            ],
        }
    )

    assert [m.role for m in req.messages] == ["system", "user", "assistant", "user"]
    assert req.messages[0].content == "now be terse"


@pytest.mark.unit
def test_requests_without_a_developer_role_are_left_alone():
    """Several system messages are forwarded as-is when nobody said developer.

    That shape is what this gateway already sends today, and hoisting one out
    of the middle of a conversation would change what the prompt means. The
    new role is no reason to rewrite traffic that never used it.
    """
    messages = [
        {"role": "user", "content": "first"},
        {"role": "system", "content": "mid-conversation instruction"},
        {"role": "system", "content": "another"},
        {"role": "user", "content": "second"},
    ]

    req = ChatCompletionRequest.model_validate({"model": "m", "messages": messages})

    assert [m.role for m in req.messages] == ["user", "system", "system", "user"]


@pytest.mark.unit
def test_chat_completion_request_rejects_unknown_role():
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {"model": "m", "messages": [{"role": "moderator", "content": "u"}]}
        )


@pytest.mark.unit
def test_chat_completion_request_invalid_ranges():
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {"model": "m", "messages": [{"role": "user", "content": "u"}], "temperature": 5}
        )
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(
            {"model": "m", "messages": [{"role": "user", "content": "u"}], "top_p": 2}
        )


@pytest.mark.unit
def test_model_list_schema_roundtrip():
    data = {
        "object": "list",
        "data": [
            {
                "id": "m",
                "name": "M",
                "object": "model",
                "created": 1,
                "owned_by": "p",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "quantization": "bf16",
                "context_length": 1,
                "max_output_length": 1,
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_sampling_parameters": ["temperature"],
                "supported_features": ["tools"],
            }
        ],
    }
    obj = ModelList.model_validate(data)
    assert obj.object == "list"
    assert obj.data[0].id == "m"


@pytest.mark.unit
def test_update_user_request_accepts_pro_role():
    """The admin update-user schema must accept the 'pro' role added in Task 1."""
    # Should not raise for any valid role value
    UpdateUserRequest(role="pro")
    UpdateUserRequest(role="free")
    UpdateUserRequest(role="internal")
    UpdateUserRequest(role="admin")
    UpdateUserRequest(role=None)


@pytest.mark.unit
def test_update_user_request_rejects_invalid_role():
    """UpdateUserRequest must reject unknown role strings."""
    with pytest.raises(ValidationError):
        UpdateUserRequest(role="superuser")
