"""The cap on batches one user creates in an hour (``BATCH_MAX_CREATES_PER_HOUR``)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from serving.batch import admission
from serving.batch.config import DEFAULT_MAX_CREATES_PER_HOUR, BatchConfig, batch_config
from serving.storage.batch_store import BatchLimitExceeded


def test_the_cap_defaults_to_sixty_and_reads_its_setting(monkeypatch):
    monkeypatch.delenv("BATCH_MAX_CREATES_PER_HOUR", raising=False)
    assert batch_config().max_creates_per_hour == DEFAULT_MAX_CREATES_PER_HOUR == 60
    monkeypatch.setenv("BATCH_MAX_CREATES_PER_HOUR", "500")
    assert batch_config().max_creates_per_hour == 500
    # Below the minimum the default stands, as for the other caps.
    monkeypatch.setenv("BATCH_MAX_CREATES_PER_HOUR", "0")
    assert batch_config().max_creates_per_hour == 60


def test_a_create_over_the_hourly_cap_is_a_429_naming_it():
    refused = admission.limit_error(BatchLimitExceeded("batches_per_hour", limit=60, current=60))
    assert refused.status_code == 429
    assert "at most 60 may be created in an hour" in refused.detail
    assert refused.headers is None


def test_the_429_says_when_the_cap_frees_a_slot():
    refused = admission.limit_error(
        BatchLimitExceeded("batches_per_hour", limit=60, current=60, retry_after=1234.2)
    )
    assert refused.status_code == 429
    assert refused.headers == {"Retry-After": "1235"}
    assert refused.detail.endswith("try again in 1235 seconds")
    # A slot about to free is still a whole second away.
    almost = BatchLimitExceeded("batches_per_hour", limit=60, current=60, retry_after=0.01)
    assert admission.limit_error(almost).headers == {"Retry-After": "1"}


async def test_the_precheck_refuses_at_the_hourly_cap_before_the_body_is_read():
    over = BatchLimitExceeded("batches_per_hour", limit=60, current=60, retry_after=59.5)
    store = SimpleNamespace(
        count_active_batches=AsyncMock(return_value=0),
        precheck_creates_per_hour=AsyncMock(side_effect=over),
    )
    with pytest.raises(HTTPException) as refused:
        await admission.precheck_caps(store, "u", BatchConfig())
    assert (refused.value.status_code, refused.value.headers) == (429, {"Retry-After": "60"})
    store.precheck_creates_per_hour.assert_awaited_once_with("u", DEFAULT_MAX_CREATES_PER_HOUR)

    store.precheck_creates_per_hour = AsyncMock(return_value=None)
    await admission.precheck_caps(store, "u", BatchConfig())
