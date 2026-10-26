"""Malformed JSON Unicode must fail file validation, not strand its work lease."""

import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from serving.batch import validation
from serving.batch.config import BatchConfig
from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import BatchStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_unpaired_surrogate_custom_id_fails_batch_validation(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = f"review-unicode-{uuid.uuid4().hex}"
    await store.initialize()
    try:
        raw = (
            json.dumps(
                {
                    "custom_id": "\ud800",
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": "review-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                }
            )
            + "\n"
        ).encode("ascii")
        file_id = await files.create_upload(
            user_id=owner,
            filename="invalid.jsonl",
            purpose="batch",
            max_total_bytes=1000,
            max_uploads_per_hour=10,
        )
        await files.append_chunk(
            file_id=file_id, user_id=owner, seq=0, data=raw, max_total_bytes=1000
        )
        await files.finish_upload(
            file_id=file_id,
            user_id=owner,
            sha256=hashlib.sha256(raw).hexdigest(),
            line_count=1,
            retention_seconds=3600,
            check_owner=False,
        )
        batch = await store.create_batch(
            batch_id=f"batch_{uuid.uuid4().hex}",
            user_id=owner,
            key_hash=None,
            endpoint="/v1/chat/completions",
            model_ids=[],
            items=[],
            metadata=None,
            completion_window_seconds=3600,
            api_surface="openai_file",
            phase="validating",
            input_file_id=file_id,
        )
        await store.claim_batch_work(
            phase="validating", worker_id="review-validator", lease_seconds=60
        )
        monkeypatch.setattr(
            validation, "authenticate_stored_key", AsyncMock(return_value={"user_id": owner})
        )
        result = await validation.validate_file_batch(
            store=store,
            file_store=files,
            batch=batch,
            worker_id="review-validator",
            services=SimpleNamespace(),
            cfg=BatchConfig(),
            lease_seconds=60,
        )
        assert result == "failed"
        row = await store.get_batch(batch["id"])
        assert row["validation_error"]["data"][0]["line"] == 1
        assert await store.list_results(batch["id"]) == []
    finally:
        await store.delete_user_batches(owner)
        await pool.close()
