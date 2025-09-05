"""Pydantic schemas for API requests and responses.

These types model the OpenAI-compatible chat completion APIs and the
models listing endpoint. We keep them permissive enough to interoperate
with upstream providers while validating required fields and ranges.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ResponseFormat(BaseModel):
    type: str | None = None
    # Some providers carry a JSON schema for guided decoding
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool | None = False

    # Sampling / decoding params
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)

    # Limits and stopping
    max_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    seed: int | None = None

    # Penalties
    frequency_penalty: float | None = None
    presence_penalty: float | None = None

    # Tools / structured output
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: ResponseFormat | None = None

    class Config:
        extra = "ignore"


# Response models


class ChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = ""
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: str | None = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage | None = None


class ModelItem(BaseModel):
    id: str
    name: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str
    input_modalities: list[str]
    output_modalities: list[str]
    quantization: str
    context_length: int
    max_output_length: int
    pricing: dict[str, str]
    supported_sampling_parameters: list[str] = []
    supported_features: list[str] = []
    openrouter: dict[str, Any] | None = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelItem]


# Error schemas for documenting non-2xx responses
class ErrorDetail(BaseModel):
    type: str | None = None
    message: str
    code: int | None = None
    # Optional rate limit / routing metadata
    model: str | None = None
    retry_after: int | None = None
    tokens_requested: int | None = None
    queue_size: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
