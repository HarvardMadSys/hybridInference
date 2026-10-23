"""Unit tests for the batch request/response schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from serving.batch_schemas import (
    MAX_BATCH_REQUESTS,
    BatchCreateRequest,
    BatchRequestItem,
)


def test_create_requires_at_least_one_request() -> None:
    with pytest.raises(ValidationError):
        BatchCreateRequest(requests=[])


def test_custom_id_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        BatchRequestItem(custom_id="", body={"model": "m"})


def test_item_defaults_match_openai_shape() -> None:
    item = BatchRequestItem(custom_id="a", body={"model": "m"})
    assert item.method == "POST"
    assert item.url == "/v1/chat/completions"


def test_create_rejects_more_than_max_requests() -> None:
    too_many = [
        {"custom_id": f"c{i}", "body": {"model": "m"}} for i in range(MAX_BATCH_REQUESTS + 1)
    ]
    with pytest.raises(ValidationError):
        BatchCreateRequest(requests=too_many)
