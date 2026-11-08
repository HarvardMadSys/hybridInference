"""Independent download interruption regression against real PostgreSQL."""

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from serving.batch import delivery
from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import BatchStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_upload_download_does_not_silently_truncate_after_read_lease_expires(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = f"review-delivery-{uuid.uuid4().hex}"
    file_id = None
    stream = None
    clock = [1000.0]
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    try:
        chunks = [f"chunk-{i}\n".encode() for i in range(5)]
        payload = b"".join(chunks)
        file_id = await files.create_upload(
            user_id=owner,
            filename="read.jsonl",
            purpose="batch",
            max_total_bytes=1000,
            max_uploads_per_hour=10,
        )
        for seq, chunk in enumerate(chunks):
            await files.append_chunk(
                file_id=file_id,
                user_id=owner,
                seq=seq,
                data=chunk,
                max_total_bytes=1000,
            )
        await files.finish_upload(
            file_id=file_id,
            user_id=owner,
            sha256=hashlib.sha256(payload).hexdigest(),
            line_count=5,
            retention_seconds=3600,
            check_owner=False,
        )
        stream = delivery.stream_upload(store=store, file_store=files, file_id=file_id)
        received = await anext(stream)
        assert await files.delete_file(file_id, user_id=owner)
        # The consumer is suspended in ASGI send. Move both the lease and
        # monotonic clock past its TTL without making the regression wait.
        clock[0] += delivery.READ_LEASE_SECONDS + 1
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE batch_read_leases SET expires_at = now() - interval '1 second' "
                "WHERE file_id = $1",
                file_id,
            )
            await conn.execute(
                "UPDATE batch_files SET read_lease_until = now() - interval '1 second' "
                "WHERE id = $1",
                file_id,
            )
        await files.collect_garbage()
        assert not await files.upload_exists(file_id), "The stale download's data must be collected"
        with pytest.raises(delivery.DownloadInterrupted):
            async for part in stream:
                received += part
    finally:
        if stream is not None:
            await stream.aclose()
        async with pool.acquire() as conn:
            if file_id is not None:
                await conn.execute("DELETE FROM batch_read_leases WHERE file_id = $1", file_id)
            await conn.execute("DELETE FROM batch_files WHERE user_id = $1", owner)
        await pool.close()


async def test_suspended_download_renews_lease_and_resource_horizon(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = f"review-heartbeat-{uuid.uuid4().hex}"
    file_id = None
    stream = None
    monkeypatch.setattr(delivery, "_RENEW_EVERY", 0.01)
    monkeypatch.setattr(delivery, "READ_LEASE_SECONDS", 1)
    try:
        payload = b"first\nsecond\n"
        file_id = await files.create_upload(
            user_id=owner,
            filename="heartbeat.jsonl",
            purpose="batch",
            max_total_bytes=1000,
            max_uploads_per_hour=10,
        )
        for seq, chunk in enumerate((b"first\n", b"second\n")):
            await files.append_chunk(
                file_id=file_id,
                user_id=owner,
                seq=seq,
                data=chunk,
                max_total_bytes=1000,
            )
        await files.finish_upload(
            file_id=file_id,
            user_id=owner,
            sha256=hashlib.sha256(payload).hexdigest(),
            line_count=2,
            retention_seconds=3600,
            check_owner=False,
        )
        stream = delivery.stream_upload(store=store, file_store=files, file_id=file_id)
        received = await anext(stream)
        assert await files.delete_file(file_id, user_id=owner)
        async with pool.acquire() as conn:
            initial = await conn.fetchrow(
                "SELECT f.read_lease_until, l.expires_at FROM batch_files f "
                "JOIN batch_read_leases l ON l.file_id = f.id WHERE f.id = $1",
                file_id,
            )
        # Do not advance the generator: renewal must happen while ASGI send
        # is suspended, and remain active beyond the original lease deadline.
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            async with pool.acquire() as conn:
                renewed = await conn.fetchval(
                    "SELECT now() > $2::timestamptz AND f.read_lease_until > now() "
                    "AND l.expires_at > now() AND f.read_lease_until > $2 "
                    "AND l.expires_at > $3 FROM batch_files f "
                    "JOIN batch_read_leases l ON l.file_id = f.id WHERE f.id = $1",
                    file_id,
                    initial["read_lease_until"],
                    initial["expires_at"],
                )
            if renewed:
                break
            assert asyncio.get_running_loop().time() < deadline, "Suspended download did not renew"
            await asyncio.sleep(0.02)
        await files.collect_garbage()
        assert await files.upload_exists(file_id)
        async for part in stream:
            received += part
        assert received == payload
    finally:
        if stream is not None:
            await stream.aclose()
        async with pool.acquire() as conn:
            if file_id is not None:
                await conn.execute("DELETE FROM batch_read_leases WHERE file_id = $1", file_id)
            await conn.execute("DELETE FROM batch_files WHERE user_id = $1", owner)
        await pool.close()


async def test_request_export_does_not_silently_truncate_after_owner_erasure(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=4)
    store = BatchStore(pool)
    owner = f"review-request-export-{uuid.uuid4().hex}"
    batch_id = f"batch_{uuid.uuid4().hex}"
    stream = None
    monkeypatch.setattr(delivery, "PAGE", 1)
    try:
        await store.create_batch(
            batch_id=batch_id,
            user_id=owner,
            key_hash=None,
            endpoint="/v1/embeddings",
            model_ids=["m"],
            items=[
                {"custom_id": str(i), "model_id": "m", "request": {"model": "m", "input": str(i)}}
                for i in range(3)
            ],
            metadata=None,
            completion_window_seconds=None,
            target="embeddings",
        )
        stream = delivery.stream_requests(store=store, batch_id=batch_id, url="/v1/embeddings")
        assert b'"custom_id":"0"' in await anext(stream)
        assert await store.delete_user_batches(owner) == 1
        with pytest.raises(delivery.DownloadInterrupted):
            await anext(stream)
    finally:
        if stream is not None:
            await stream.aclose()
        await store.delete_user_batches(owner)
        await pool.close()


async def test_request_export_fails_while_owner_erasure_is_under_way(monkeypatch):
    # The erasure deletes a batch's items a slice at a time, lowest id first,
    # and its row with the last slice. A download that reads past a committed
    # slice still finds the row: the owner's hard-delete mark is what says
    # pages are missing.
    pool = await postgres.create_pool(min_size=1, max_size=4)
    store = BatchStore(pool)
    owner = f"review-erasure-under-way-{uuid.uuid4().hex}"
    batch_id = f"batch_{uuid.uuid4().hex}"
    stream = None
    monkeypatch.setattr(delivery, "PAGE", 1)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, status) "
                "VALUES ($1, $1 || '@example.com', 'x', 'deleted')",
                owner,
            )
        await store.create_batch(
            batch_id=batch_id,
            user_id=owner,
            key_hash=None,
            endpoint="/v1/embeddings",
            model_ids=["m"],
            items=[
                {"custom_id": str(i), "model_id": "m", "request": {"model": "m", "input": str(i)}}
                for i in range(4)
            ],
            metadata=None,
            completion_window_seconds=None,
            target="embeddings",
        )
        # An owner with an account row and no erasure under way reads whole.
        assert await store.batch_intact(batch_id)

        stream = delivery.stream_requests(store=store, batch_id=batch_id, url="/v1/embeddings")
        received = [await anext(stream)]
        assert b'"custom_id":"0"' in received[0]
        # What the hard delete commits before and in the erasure's first slice:
        # the account marked, then the batch's lowest items gone, its row kept.
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET hard_delete_pending = TRUE WHERE id = $1", owner)
            await conn.execute(
                "DELETE FROM batch_items WHERE id IN ("
                "SELECT id FROM batch_items WHERE batch_id = $1 ORDER BY id LIMIT 2)",
                batch_id,
            )
        assert await store.batch_exists(batch_id)
        with pytest.raises(delivery.DownloadInterrupted):
            async for chunk in stream:
                received.append(chunk)
        # It sent the rows the slice spared, then refused to end as complete.
        assert b'"custom_id":"1"' not in b"".join(received)
        assert b'"custom_id":"3"' in b"".join(received)
    finally:
        if stream is not None:
            await stream.aclose()
        await store.delete_user_batches(owner)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", owner)
        await pool.close()
