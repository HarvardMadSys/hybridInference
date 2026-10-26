"""Independent storage-cap regression with pinned, deleted batch inputs."""

import hashlib
import uuid

import pytest
import pytest_asyncio

from serving.storage.batch_file_store import BatchFileQuotaExceeded, BatchFileStore
from serving.storage.batch_store import BatchStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def stores():
    pool = await postgres.create_pool(min_size=1, max_size=6)
    batches = BatchStore(pool)
    await batches.initialize()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE batch_jobs, batch_files, batch_read_leases CASCADE")
    try:
        yield batches, BatchFileStore(pool)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE batch_jobs, batch_files, batch_read_leases CASCADE")
        await pool.close()


async def upload(files, owner, data, limit):
    file_id = await files.create_upload(
        user_id=owner,
        filename="review.jsonl",
        purpose="batch",
        max_total_bytes=limit,
        max_uploads_per_hour=100,
    )
    try:
        await files.append_chunk(
            file_id=file_id,
            user_id=owner,
            seq=0,
            data=data,
            max_total_bytes=limit,
        )
        await files.finish_upload(
            file_id=file_id,
            user_id=owner,
            sha256=hashlib.sha256(data).hexdigest(),
            line_count=1,
            retention_seconds=3600,
            check_owner=False,
        )
    except Exception:
        await files.abort_upload(file_id)
        raise
    return file_id


@pytest.mark.parametrize("pin", ["validation", "download"])
async def test_deleted_but_pinned_upload_still_consumes_storage_quota(stores, pin):
    batches, files = stores
    owner = "review-file-owner"
    payload = b'{"model":"test"}\n'
    cap = len(payload)
    first = await upload(files, owner, payload, cap)
    if pin == "validation":
        await batches.create_batch(
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
            input_file_id=first,
        )
    else:
        await batches.take_read_lease(file_id=first, ttl_seconds=60)
    assert await files.delete_file(first, user_id=owner)
    await files.collect_garbage()
    assert await files.upload_exists(first), "The first upload must remain physically pinned"
    with pytest.raises(BatchFileQuotaExceeded):
        await upload(files, owner, payload, cap)
