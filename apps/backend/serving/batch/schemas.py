"""Request and response bodies of the batch surface (``/v1/batches``).

The create body is inline: a ``requests`` array whose entries have the shape of
the lines of an OpenAI batch input file (``custom_id``, ``method``, ``url``,
``body``), because the gateway has no Files API. Responses are OpenAI-shaped
batch objects, plus two fields of ours (``cost_usd`` and ``error``); per-request
results are read a page at a time rather than downloaded as an output file.

Only the envelope is validated here. Whether each ``body`` is a chat request the
caller may send is decided by the router, which knows the route table and the
caller.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The one endpoint batch requests may target.
CHAT_COMPLETIONS_URL = "/v1/chat/completions"

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
    """``POST /v1/batches`` body.

    The number of requests has no static ceiling here: ``BATCH_MAX_REQUESTS`` is
    a live setting, checked by the router.
    """

    model_config = ConfigDict(extra="ignore")

    requests: list[BatchRequestItem] = Field(min_length=1)
    metadata: dict[str, str] | None = None

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


class BatchObject(BaseModel):
    """An OpenAI-shaped batch object.

    The file fields are always null: requests arrive inline and results are read
    from ``GET /v1/batches/{id}/results``. ``cost_usd`` (what the batch's
    finished requests cost so far) and ``error`` (why the whole batch failed,
    e.g. ``owner_unauthorized``) are ours.
    """

    id: str
    object: Literal["batch"] = "batch"
    endpoint: str = CHAT_COMPLETIONS_URL
    errors: None = None
    input_file_id: None = None
    completion_window: str
    status: BatchStatus
    output_file_id: None = None
    error_file_id: None = None
    created_at: int
    in_progress_at: int | None = None
    expires_at: int | None = None
    finalizing_at: None = None
    completed_at: int | None = None
    failed_at: int | None = None
    expired_at: int | None = None
    cancelling_at: int | None = None
    cancelled_at: int | None = None
    request_counts: BatchRequestCounts
    metadata: dict[str, Any] | None = None
    cost_usd: float = 0.0
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
