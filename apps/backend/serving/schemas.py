"""Pydantic schemas for API requests and responses.

These types model the OpenAI-compatible chat completion APIs and the
models listing endpoint. We keep them permissive enough to interoperate
with upstream providers while validating required fields and ranges.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from serving.responses_translator import merge_leading_system_messages


class ChatMessage(BaseModel):  # type: ignore[no-any-unimported]
    """Single chat message with role and content.

    Supports tool role for tool execution results in function calling flows.

    ``developer`` is accepted on input: OpenAI added it for reasoning models,
    and clients that follow that spec (pi, for one) send instructions under
    it. :class:`ChatCompletionRequest` folds it to ``system`` for the whole
    request — sglang answers a bare ``developer`` message with
    ``Unexpected message role`` (probed against the local Qwen3.6 server), so
    only the classic four roles may leave here.
    """

    role: Literal["system", "developer", "user", "assistant", "tool"]
    # Allow either plain string (OpenAI style) or structured blocks (provider-specific)
    content: Any | None = None
    # For role="tool" messages, associates result back to a prior tool call
    tool_call_id: str | None = None
    # Optional tool name for role="tool" messages
    name: str | None = None
    # DeepSeek requires assistant reasoning history to be echoed in thinking-mode tool flows.
    reasoning_content: str | None = None
    # For assistant messages carrying tool calls in OpenAI format
    tool_calls: list[dict[str, Any]] | None = None

    # Ignore unknown extra fields to be permissive with client payloads
    model_config = ConfigDict(extra="ignore")


class ResponseFormat(BaseModel):  # type: ignore[no-any-unimported]
    """Optional structured output hints for providers."""

    type: str | None = None
    # Some providers carry a JSON schema for guided decoding
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    # OpenAI structured-output schema, nested under ``json_schema`` for
    # ``type: "json_schema"`` (name / schema / strict). Preserved so structured
    # outputs survive validation when forwarded to the provider.
    json_schema: dict[str, Any] | None = None


class ChatCompletionRequest(BaseModel):  # type: ignore[no-any-unimported]
    """OpenAI-compatible chat completions request payload."""

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

    # Reasoning effort: "low", "medium", "high"
    reasoning_effort: str | None = None

    # Reasoning / thinking control (ZAI GLM-4.7/GLM-5, MiniMax M2.5)
    thinking: dict[str, Any] | None = None

    # Tools / structured output
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: ResponseFormat | None = None

    # Pydantic v2 configuration: ignore extra fields in requests
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _fold_developer_messages(self) -> ChatCompletionRequest:
        """Fold ``developer`` messages into a single leading ``system`` one.

        Folding is not a formality: probed against the local sglang server,
        ``developer`` comes back ``Unexpected message role``, and two system
        messages — or one that is not first — come back ``System message must
        be at the beginning``. So a request pairing ``system`` with
        ``developer``, which the OpenAI spec allows, would trade one 400 for
        another if it were only relabelled. The merge is the same one the
        Responses path applies for the same shape and the same reason.

        Scoped to requests that actually used ``developer``. A request that
        already carries several system messages keeps them: it is a shape this
        gateway forwards today, hoisting one out of the middle of a
        conversation would change what the prompt means, and nothing about
        accepting a new role justifies rewriting traffic that never used it.
        """
        if not any(message.role == "developer" for message in self.messages):
            return self
        folded = [
            {**message.model_dump(), "role": "system"}
            if message.role == "developer"
            else message.model_dump()
            for message in self.messages
        ]
        self.messages = [
            ChatMessage.model_validate(message) for message in merge_leading_system_messages(folded)
        ]
        return self


# Response models


class ChoiceMessage(BaseModel):  # type: ignore[no-any-unimported]
    """Assistant message in a completion choice."""

    role: Literal["assistant"] = "assistant"
    content: str | None = ""
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionChoice(BaseModel):  # type: ignore[no-any-unimported]
    """One choice in the chat completion result set."""

    index: int
    message: ChoiceMessage
    finish_reason: str | None = None


class Usage(BaseModel):  # type: ignore[no-any-unimported]
    """Token usage accounting for the request/response.

    Extended to include cache and reasoning tokens for accurate cost tracking.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    # Optional fields for advanced billing
    reasoning_tokens: int | None = None  # For models like DeepSeek-R1
    cache_read_tokens: int | None = None  # Tokens read from cache (cheaper)
    cache_write_tokens: int | None = None  # Tokens written to cache


class ChatCompletionResponse(BaseModel):  # type: ignore[no-any-unimported]
    """OpenAI-compatible chat completions response payload."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage | None = None


class ModelItem(BaseModel):  # type: ignore[no-any-unimported]
    """Model metadata for listing endpoints."""

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


class ModelList(BaseModel):  # type: ignore[no-any-unimported]
    """List of models supported by the server."""

    object: Literal["list"] = "list"
    data: list[ModelItem]


# Error schemas for documenting non-2xx responses
class ErrorDetail(BaseModel):  # type: ignore[no-any-unimported]
    """Error detail payload aligned with OpenAI error shape."""

    type: str | None = None
    message: str
    code: int | None = None
    # Optional rate limit / routing metadata
    model: str | None = None
    retry_after: int | None = None
    tokens_requested: int | None = None
    queue_size: int | None = None


class ErrorResponse(BaseModel):  # type: ignore[no-any-unimported]
    """Top-level error wrapper."""

    error: ErrorDetail


# Embedding schemas


class EmbeddingRequest(BaseModel):  # type: ignore[no-any-unimported]
    """OpenAI-compatible embeddings request payload."""

    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] | None = None
    dimensions: int | None = None

    model_config = ConfigDict(extra="ignore")


class EmbeddingData(BaseModel):  # type: ignore[no-any-unimported]
    """Single embedding result."""

    object: Literal["embedding"] = "embedding"
    index: int
    # list[float] for default float format, str for base64 encoding_format.
    # allow_inf_nan=False rejects NaN/inf: Pydantic permits them by default,
    # but Starlette's JSON serialization (allow_nan=False) would 500 on them
    # after the handler has already logged a billable 200.
    embedding: list[Annotated[float, Field(allow_inf_nan=False)]] | str


class EmbeddingUsage(BaseModel):  # type: ignore[no-any-unimported]
    """Token usage for an embedding request."""

    # Bound to [0, INT4_MAX]: api_logs.prompt_tokens/total_tokens are Postgres
    # INTEGER columns. Negatives would be logged/billed as a successful request
    # while calculate_cost clamps the cost to zero; a value above INT4_MAX would
    # pass validation but fail the background log insert (integer out of range)
    # after the client already got a 200 and the quota was incremented.
    prompt_tokens: int = Field(ge=0, le=2147483647)
    total_tokens: int = Field(ge=0, le=2147483647)


class EmbeddingResponse(BaseModel):  # type: ignore[no-any-unimported]
    """OpenAI-compatible embeddings response payload."""

    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage


# Backward-compatibility aliases for older tests referring to Choice
# New code should import ChatCompletionChoice explicitly.
Choice = ChatCompletionChoice
