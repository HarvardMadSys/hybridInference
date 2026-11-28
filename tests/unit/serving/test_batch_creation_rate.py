"""The cap on batches one user creates in an hour (``BATCH_MAX_CREATES_PER_HOUR``)."""

from __future__ import annotations

from serving.batch import admission
from serving.batch.config import DEFAULT_MAX_CREATES_PER_HOUR, batch_config
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
