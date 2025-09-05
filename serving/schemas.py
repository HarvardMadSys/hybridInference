"""Pydantic schemas for API requests and responses.

These types model the OpenAI-compatible chat completion APIs and the
models listing endpoint. We keep them permissive enough to interoperate
with upstream providers while validating required fields and ranges.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ResponseFormat(BaseModel):
    type: Optional[str] = None
    # Some providers carry a JSON schema for guided decoding
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias="schema")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False

    # Sampling / decoding params
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(default=None, ge=0)
    min_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Limits and stopping
    max_tokens: Optional[int] = Field(default=None, ge=1)
    stop: Optional[Union[str, List[str]]] = None
    seed: Optional[int] = None

    # Penalties
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None

    # Tools / structured output
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[ResponseFormat] = None

    class Config:
        extra = "ignore"


# Response models

class ChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: Optional[str] = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: Optional[str] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Optional[Usage] = None


class ModelItem(BaseModel):
    id: str
    name: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str
    input_modalities: List[str]
    output_modalities: List[str]
    quantization: str
    context_length: int
    max_output_length: int
    pricing: Dict[str, str]
    supported_sampling_parameters: List[str] = []
    supported_features: List[str] = []
    openrouter: Optional[Dict[str, Any]] = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelItem]


# Error schemas for documenting non-2xx responses
class ErrorDetail(BaseModel):
    type: Optional[str] = None
    message: str
    code: Optional[int] = None
    # Optional rate limit / routing metadata
    model: Optional[str] = None
    retry_after: Optional[int] = None
    tokens_requested: Optional[int] = None
    queue_size: Optional[int] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
