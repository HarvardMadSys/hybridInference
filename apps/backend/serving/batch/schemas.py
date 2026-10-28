"""Request and response bodies of the batch API (``/v1/batches``).

A batch has one target (``/v1/chat/completions``, ``/v1/embeddings``,
``/v1/messages`` or ``/v1/responses``) and its requests arrive in one of two
ways, both through ``POST /v1/batches``:

- **inline**: a ``requests`` array whose entries have the shape of the lines of
  an OpenAI batch input file (``custom_id``, ``method``, ``url``, ``body``).
  Results are read a page at a time (``/results``) or as one JSONL export;
- **file**: OpenAI's own shape, ``input_file_id`` of an uploaded JSONL file plus
  ``endpoint`` and ``completion_window``. Results arrive as output and error
  files (``/v1/files``).

The two are exclusive. A ``/v1/messages`` batch -- our extension of the OpenAI
shape -- also names its ``anthropic_version``, in either form. Responses are
OpenAI-shaped batch objects, plus fields of ours: ``anthropic_version``,
``cost_usd`` (what the batch's finished attempts were billed so far),
``cost_unknown_attempts`` (attempts whose cost is unknown and so not in
``cost_usd``), ``usage`` and ``error`` (why the whole batch failed, e.g.
``owner_unauthorized``).

Only the envelope is validated here. Whether each ``body`` is a request the
caller may send is decided by the target (``serving.batch.targets``), which
knows the route table and the caller.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The endpoint inline batches target unless their requests name another.
CHAT_COMPLETIONS_URL = "/v1/chat/completions"

#: The completion window file batches accept (OpenAI's only value).
FILE_COMPLETION_WINDOW = "24h"

#: Longest ``custom_id`` accepted. It is indexed (unique within a batch), and an
#: index entry must stay well under Postgres's limit of about 2.7 KB.
MAX_CUSTOM_ID_LENGTH = 512

# OpenAI's limits on ``metadata``. They also keep a batch row small: a listing
# returns the metadata of up to 100 batches at once.
MAX_METADATA_PAIRS = 16
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_VALUE_LENGTH = 512

#: Prefix of a result id; the rest is the request's position in the store.
RESULT_ID_PREFIX = "batch_req_"

BatchStatus = Literal[
    "validating",
    "in_progress",
    "finalizing",
    "completed",
    "failed",
    "expired",
    "cancelling",
    "cancelled",
]

BatchRequestStatus = Literal["queued", "running", "completed", "failed", "cancelled", "expired"]


class BatchRequestItem(BaseModel):
    """One request of a batch, shaped like a line of an OpenAI batch input file."""

    model_config = ConfigDict(extra="ignore")

    custom_id: str = Field(min_length=1, max_length=MAX_CUSTOM_ID_LENGTH)
    method: Literal["POST"] = "POST"
    url: str = CHAT_COMPLETIONS_URL
    body: dict[str, Any]

    @field_validator("custom_id")
    @classmethod
    def _storable_custom_id(cls, value: str) -> str:
        # A text column cannot hold the NUL character.
        if "\x00" in value:
            raise ValueError("must not contain a NUL character")
        return value


class BatchCreateRequest(BaseModel):
    """``POST /v1/batches`` body, inline or from a file (exactly one of the two).

    The number of requests has no static ceiling here: ``BATCH_MAX_REQUESTS`` is
    a live setting, checked by the router.
    """

    model_config = ConfigDict(extra="ignore")

    requests: list[BatchRequestItem] | None = Field(default=None, min_length=1)
    input_file_id: str | None = Field(default=None, min_length=1, max_length=128)
    endpoint: str | None = None
    completion_window: str | None = None
    metadata: dict[str, str] | None = None
    #: The Anthropic API version a ``/v1/messages`` batch is checked and run
    #: under; required for such a batch and refused on any other.
    anthropic_version: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def _one_mode(self) -> BatchCreateRequest:
        if self.requests is None and self.input_file_id is None:
            raise ValueError(
                "requests: Field required (or input_file_id, for a batch from an uploaded file)"
            )
        if self.requests is not None and self.input_file_id is not None:
            raise ValueError("send either requests (inline) or input_file_id (file), not both")
        if self.input_file_id is not None and (
            self.endpoint is None or self.completion_window is None
        ):
            raise ValueError("a file batch needs endpoint and completion_window")
        return self

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > MAX_METADATA_PAIRS:
            raise ValueError(f"at most {MAX_METADATA_PAIRS} key-value pairs are allowed")
        for key, item in value.items():
            if len(key) > MAX_METADATA_KEY_LENGTH:
                raise ValueError(f"keys are at most {MAX_METADATA_KEY_LENGTH} characters")
            if len(item) > MAX_METADATA_VALUE_LENGTH:
                raise ValueError(f"values are at most {MAX_METADATA_VALUE_LENGTH} characters")
        return value


class BatchRequestCounts(BaseModel):
    """How many of a batch's requests there are, and how many have finished."""

    total: int = 0
    completed: int = 0
    failed: int = 0


class BatchUsage(BaseModel):
    """Tokens the batch's finished requests used, as OpenAI's batch object reports them."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class BatchObject(BaseModel):
    """An OpenAI-shaped batch object.

    An inline batch has no files (``input_file_id``, ``output_file_id`` and
    ``error_file_id`` are null); its results are read from
    ``GET /v1/batches/{id}/results``. A file batch names its input and, once
    sealed, its output and error files; ``errors`` lists what made its input
    invalid. ``anthropic_version`` (a ``/v1/messages`` batch's), ``cost_usd``,
    ``cost_unknown_attempts``, ``results_expires_at`` and ``error`` are ours.
    """

    id: str
    object: Literal["batch"] = "batch"
    endpoint: str = CHAT_COMPLETIONS_URL
    errors: dict[str, Any] | None = None
    input_file_id: str | None = None
    completion_window: str
    status: BatchStatus
    output_file_id: str | None = None
    error_file_id: str | None = None
    created_at: int
    in_progress_at: int | None = None
    expires_at: int | None = None
    finalizing_at: int | None = None
    completed_at: int | None = None
    failed_at: int | None = None
    expired_at: int | None = None
    cancelling_at: int | None = None
    cancelled_at: int | None = None
    request_counts: BatchRequestCounts
    metadata: dict[str, Any] | None = None
    usage: BatchUsage | None = None
    anthropic_version: str | None = None
    cost_usd: float = 0.0
    cost_unknown_attempts: int = 0
    results_expires_at: int | None = None
    error: dict[str, Any] | None = None


class BatchList(BaseModel):
    """A page of the caller's batches, newest first."""

    object: Literal["list"] = "list"
    data: list[BatchObject]
    has_more: bool
    first_id: str | None = None
    last_id: str | None = None


class BatchResultResponse(BaseModel):
    """The response a request received, as an OpenAI batch output line carries it."""

    status_code: int | None = None
    request_id: str | None = None
    body: dict[str, Any] | None = None


class BatchResult(BaseModel):
    """One request of a batch and, once it has finished, its outcome.

    ``response`` is set when the request produced one, ``error`` when it did not
    (``{"code", "message"}``, plus ``"status"`` when an HTTP status caused it).
    Both are null while the request is ``queued`` or ``running``.
    """

    id: str
    custom_id: str
    status: BatchRequestStatus
    response: BatchResultResponse | None = None
    error: dict[str, Any] | None = None


class BatchResultsPage(BaseModel):
    """A page of a batch's results, in submission order."""

    object: Literal["list"] = "list"
    data: list[BatchResult]
    has_more: bool
    first_id: str | None = None
    last_id: str | None = None


class BatchDeleted(BaseModel):
    """Answer to ``DELETE /v1/batches/{id}``."""

    id: str
    object: Literal["batch.deleted"] = "batch.deleted"
    deleted: bool = True
