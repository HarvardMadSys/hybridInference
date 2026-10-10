"""Pydantic schemas for the batch processing surface.

The create request is inline (hybrid: no Files API): the caller sends a
``requests`` array of ``{custom_id, method, url, body}`` entries, matching the
lines of an OpenAI batch input file. Responses are OpenAI-shaped batch objects,
with per-item results attached under a non-standard ``results`` key so callers
can read them without a file download.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# OpenAI caps a batch at 50,000 requests. Kept as a module constant so the
# router error message and any later config knob share one source.
MAX_BATCH_REQUESTS = 50_000

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


class BatchRequestItem(BaseModel):
    """One request inside a batch, shaped like an OpenAI input-file line."""

    custom_id: str = Field(min_length=1, max_length=512)
    method: Literal["POST"] = "POST"
    url: str = "/v1/chat/completions"
    body: dict[str, Any]

    model_config = ConfigDict(extra="ignore")


class BatchCreateRequest(BaseModel):
    """``POST /v1/batches`` body (inline requests, no file upload)."""

    requests: list[BatchRequestItem] = Field(min_length=1, max_length=MAX_BATCH_REQUESTS)
    metadata: dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


class BatchRequestCounts(BaseModel):
    """Per-status item counts on a batch."""

    total: int = 0
    completed: int = 0
    failed: int = 0


class BatchItemResult(BaseModel):
    """Result of one batch item. Exactly one of ``response``/``error`` is set."""

    custom_id: str
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class BatchObject(BaseModel):
    """OpenAI-shaped Batch object, plus inline ``results`` for our callers."""

    id: str
    object: Literal["batch"] = "batch"
    endpoint: str = "/v1/chat/completions"
    input_file_id: str | None = None
    completion_window: str = "24h"
    status: BatchStatus = "validating"
    output_file_id: str | None = None
    error_file_id: str | None = None
    created_at: int
    in_progress_at: int | None = None
    expires_at: int | None = None
    finalizing_at: int | None = None
    completed_at: int | None = None
    failed_at: int | None = None
    cancelling_at: int | None = None
    cancelled_at: int | None = None
    request_counts: BatchRequestCounts = Field(default_factory=BatchRequestCounts)
    metadata: dict[str, Any] | None = None
    results: list[BatchItemResult] | None = None


class BatchListResponse(BaseModel):
    """Paginated list of batches."""

    object: Literal["list"] = "list"
    data: list[BatchObject] = Field(default_factory=list)
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class BatchDeleteResponse(BaseModel):
    """Result of deleting (purging) a batch."""

    id: str
    object: Literal["batch.deleted"] = "batch.deleted"
    deleted: bool = True
