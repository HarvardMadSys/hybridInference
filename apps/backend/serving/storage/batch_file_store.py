"""Files for batch inference (``/v1/files``, ``purpose=batch``).

Two kinds of file share ``batch_files`` (created with the rest of the batch
schema by :class:`serving.storage.batch_store.BatchStore`):

- **Uploads** (``kind = 'upload'``): a user's JSONL input. The bytes live in
  ``batch_file_chunks`` as ordered chunks, so no request ever holds a whole file
  in memory or in one transaction. An upload is ``uploading`` until its last
  chunk is written and its size and SHA-256 are committed with the switch to
  ``ready``; only a ready upload can start a batch, and a broken upload is
  never usable. Stored bytes never change.
- **Outputs** (``kind = 'output'`` / ``'error'``): manifests of a sealed batch's
  results. They hold no bytes: a download renders them again from the batch's
  immutable items with the renderer version the manifest records, and the
  manifest's size and SHA-256 say exactly what that rendering produces.

Deleting a file hides it at once (``deleted_at``); a deleted output is never
re-created, and its batch keeps naming it. The bytes of an upload go later,
from :meth:`BatchFileStore.collect_garbage`, once no batch still validating
reads it and no download holds a read lease on it. Uploads expire after their
retention the same way. Until its bytes are actually gone, a deleted upload
still counts against its owner's storage cap: what the cap bounds is what is
stored.
"""

from __future__ import annotations

import secrets
from typing import Any

from serving.storage.batch_store import _CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, _rowcount


class BatchFileQuotaExceeded(RuntimeError):
    """An upload would exceed the user's storage or upload-rate cap."""

    def __init__(self, kind: str, *, limit: int, current: int) -> None:
        self.kind = kind
        self.limit = limit
        self.current = current
        super().__init__(f"{kind}: {current} exceeds {limit}")


class BatchFileOwnerUnavailable(RuntimeError):
    """The uploading account is no longer active (or is being hard-deleted)."""


#: Size of a stored chunk of an upload.
CHUNK_BYTES = 1024 * 1024

#: Deleted uploads examined per collection.
_GC_LIMIT = 200

#: An upload not finished in this long is abandoned and collected.
_STALE_UPLOAD_SECONDS = 3600

_FILE_COLUMNS = (
    "id, user_id, purpose, kind, filename, status, bytes, sha256, line_count, batch_id, "
    "generation, renderer_version, created_at, ready_at, deleted_at, expires_at"
)


def new_file_id() -> str:
    """Return a new file id, shaped like OpenAI's."""
    return f"file-{secrets.token_hex(12)}"


class BatchFileStore:
    """Uploads, their chunks, and the output manifests of sealed batches."""

    def __init__(self, pool: Any) -> None:
        """Wrap the asyncpg pool the batch store uses."""
        self._pool = pool

    async def create_upload(
        self,
        *,
        user_id: str,
        filename: str,
        purpose: str,
        max_total_bytes: int,
        max_uploads_per_hour: int,
    ) -> str:
        """Open an upload; its bytes follow with :meth:`append_chunk`.

        Raises:
            BatchFileQuotaExceeded: The user already stores ``max_total_bytes``
                or started ``max_uploads_per_hour`` uploads in the last hour.
        """
        file_id = new_file_id()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, user_id)
            usage = await conn.fetchrow(
                """
                SELECT COALESCE(sum(bytes), 0) AS stored,
                       count(*) FILTER (WHERE created_at > now() - interval '1 hour') AS recent
                FROM batch_files
                WHERE user_id = $1 AND kind = 'upload'
                """,
                user_id,
            )
            if max_uploads_per_hour > 0 and usage["recent"] >= max_uploads_per_hour:
                raise BatchFileQuotaExceeded(
                    "uploads_per_hour", limit=max_uploads_per_hour, current=usage["recent"]
                )
            if usage["stored"] >= max_total_bytes:
                raise BatchFileQuotaExceeded(
                    "stored_bytes", limit=max_total_bytes, current=int(usage["stored"])
                )
            await conn.execute(
                """
                INSERT INTO batch_files (id, user_id, purpose, kind, filename, status)
                VALUES ($1, $2, $3, 'upload', $4, 'uploading')
                """,
                file_id,
                user_id,
                purpose,
                filename,
            )
        return file_id

    async def append_chunk(
        self, *, file_id: str, user_id: str, seq: int, data: bytes, max_total_bytes: int
    ) -> None:
        """Store the next chunk of an upload, within the user's storage cap.

        Raises:
            BatchFileQuotaExceeded: The chunk would take the user past the cap.
            LookupError: The upload is gone or no longer uploading.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_CLAIM_LOCK_SQL, _USER_ADMISSION_LOCK_CLASS, user_id)
            stored = await conn.fetchval(
                "SELECT COALESCE(sum(bytes), 0) FROM batch_files "
                "WHERE user_id = $1 AND kind = 'upload'",
                user_id,
            )
            if stored + len(data) > max_total_bytes:
                raise BatchFileQuotaExceeded(
                    "stored_bytes", limit=max_total_bytes, current=int(stored) + len(data)
                )
            result = await conn.execute(
                "UPDATE batch_files SET bytes = bytes + $3 "
                "WHERE id = $1 AND user_id = $2 AND status = 'uploading'",
                file_id,
                user_id,
                len(data),
            )
            if _rowcount(result) == 0:
                raise LookupError(file_id)
            await conn.execute(
                "INSERT INTO batch_file_chunks (file_id, seq, data) VALUES ($1, $2, $3)",
                file_id,
                seq,
                data,
            )

    async def finish_upload(
        self,
        *,
        file_id: str,
        user_id: str,
        sha256: str,
        line_count: int,
        retention_seconds: int,
        check_owner: bool,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Commit an upload's size and digest and make it usable.

        Raises:
            BatchFileOwnerUnavailable: The account went inactive (or into hard
                deletion) while uploading; the upload is left to collection.
            LookupError: The upload is gone or no longer uploading.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            if check_owner:
                owner = await conn.fetchrow(
                    "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR SHARE",
                    user_id,
                )
                if owner is None or owner["status"] != "active" or owner["hard_delete_pending"]:
                    raise BatchFileOwnerUnavailable(user_id)
            row = await conn.fetchrow(
                f"""
                UPDATE batch_files
                SET status = 'ready', sha256 = $3, line_count = $4, ready_at = now(),
                    expires_at = now() + make_interval(secs => $5),
                    filename = COALESCE($6, filename)
                WHERE id = $1 AND user_id = $2 AND status = 'uploading'
                RETURNING {_FILE_COLUMNS}
                """,
                file_id,
                user_id,
                sha256,
                line_count,
                float(retention_seconds),
                filename[:255] if filename else None,
            )
            if row is None:
                raise LookupError(file_id)
        return dict(row)

    async def abort_upload(self, file_id: str) -> None:
        """Drop an upload that failed half-way, with whatever chunks it stored."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM batch_files WHERE id = $1 AND status = 'uploading'", file_id
            )

    async def get_file(
        self, file_id: str, *, user_id: str | None = None, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        """A file row, or None when absent, deleted (unless asked), or not *user_id*'s."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_FILE_COLUMNS} FROM batch_files WHERE id = $1 "
                "AND ($2::text IS NULL OR user_id = $2) "
                "AND ($3 OR (deleted_at IS NULL AND (expires_at IS NULL OR expires_at > now())))",
                file_id,
                user_id,
                include_deleted,
            )
        return dict(row) if row is not None else None

    async def list_files(
        self,
        *,
        user_id: str,
        limit: int,
        after: str | None = None,
        purpose: str | None = None,
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        """A user's visible files, newest first (or oldest first with ``ascending``)."""
        order = "ASC" if ascending else "DESC"
        comparison = ">" if ascending else "<"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_FILE_COLUMNS} FROM batch_files AS f
                WHERE f.user_id = $1 AND f.status = 'ready' AND f.deleted_at IS NULL
                  AND (f.expires_at IS NULL OR f.expires_at > now())
                  AND ($3::text IS NULL OR f.purpose = $3)
                  AND ($4::text IS NULL OR (f.created_at, f.id) {comparison} (
                      SELECT created_at, id FROM batch_files WHERE id = $4 AND user_id = $1))
                ORDER BY f.created_at {order}, f.id {order}
                LIMIT $2
                """,
                user_id,
                limit,
                purpose,
                after,
            )
        return [dict(row) for row in rows]

    async def delete_file(self, file_id: str, *, user_id: str | None) -> bool:
        """Hide a file for good; its bytes are collected later. True when it was visible."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE batch_files SET deleted_at = now() WHERE id = $1 "
                "AND ($2::text IS NULL OR user_id = $2) AND deleted_at IS NULL "
                "AND status = 'ready'",
                file_id,
                user_id,
            )
        return _rowcount(result) > 0

    async def read_chunks(
        self, file_id: str, *, after_seq: int = -1, limit: int = 8
    ) -> list[tuple[int, bytes]]:
        """The next chunks of an upload, in order."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, data FROM batch_file_chunks WHERE file_id = $1 AND seq > $2 "
                "ORDER BY seq LIMIT $3",
                file_id,
                after_seq,
                limit,
            )
        return [(row["seq"], bytes(row["data"])) for row in rows]

    async def collect_garbage(self) -> dict[str, int]:
        """Remove abandoned uploads and the bytes of deleted or expired ones.

        An upload's bytes stay while a batch still validating reads it, and
        while a download holds a live read lease on it.

        Returns:
            ``{"abandoned": n, "collected": n}``.
        """
        async with self._pool.acquire() as conn:
            abandoned = await conn.execute(
                "DELETE FROM batch_files WHERE status = 'uploading' "
                "AND created_at < now() - make_interval(secs => $1)",
                float(_STALE_UPLOAD_SECONDS),
            )
            await conn.execute(
                "UPDATE batch_files SET deleted_at = now() WHERE kind = 'upload' "
                "AND deleted_at IS NULL AND expires_at IS NOT NULL AND expires_at <= now()"
            )
            candidates = await conn.fetch(
                "SELECT id FROM batch_files WHERE kind = 'upload' AND deleted_at IS NOT NULL "
                "ORDER BY deleted_at, id LIMIT $1",
                _GC_LIMIT,
            )
        collected = 0
        for row in candidates:
            if await self.collect_upload(row["id"]):
                collected += 1
        return {"abandoned": _rowcount(abandoned), "collected": collected}

    async def collect_upload(self, file_id: str) -> bool:
        """Delete one deleted upload's bytes if nothing still needs them; True when deleted.

        Lock first, then check, in two statements. The candidate list was read
        earlier, and a ``DELETE`` whose ``NOT EXISTS`` was evaluated on that
        older snapshot would miss a batch that pinned the input since (a new
        job row does not make Postgres re-check the file row). So the row is
        locked ``FOR UPDATE`` -- skipped while a create holds it ``FOR KEY
        SHARE`` or a download renews its lease -- and the pins are read by a
        new statement, which sees everything committed before the lock.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchval(
                "SELECT id FROM batch_files WHERE id = $1 AND kind = 'upload' "
                "AND deleted_at IS NOT NULL FOR UPDATE SKIP LOCKED",
                file_id,
            )
            if locked is None:
                return False
            result = await conn.execute(
                """
                DELETE FROM batch_files AS f
                WHERE f.id = $1
                  AND NOT EXISTS (
                      SELECT 1 FROM batch_jobs AS j
                      WHERE j.input_file_id = f.id AND j.phase = 'validating'
                  )
                  AND (f.read_lease_until IS NULL OR f.read_lease_until <= now())
                  AND NOT EXISTS (
                      SELECT 1 FROM batch_read_leases AS l
                      WHERE l.file_id = f.id AND l.expires_at > now()
                  )
                """,
                file_id,
            )
        return _rowcount(result) > 0

    async def upload_exists(self, file_id: str) -> bool:
        """Whether an upload's bytes are still stored (deleted-but-pinned included)."""
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT TRUE FROM batch_files WHERE id = $1 AND kind = 'upload'", file_id
                )
            )
