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
