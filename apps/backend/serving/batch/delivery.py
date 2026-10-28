"""Sealing file batches, and streaming result files without holding them in memory.

**Sealing.** Once every item of a file batch is final the store freezes its
final status (``pending_terminal_status``) and the batch enters ``sealing``. A
worker takes the batch's sealing lease (which bumps its ``seal_generation``),
renders the output and error files once from the immutable items with the
current renderer (:mod:`serving.batch.renderers`), keeping only each file's
size, SHA-256 and line count, and publishes those manifests together with the
final status in one transaction, guarded by the generation. A sealer that dies
leaves its lease to run out and the next one starts again with a newer
generation; inference never runs again, and a stale sealer cannot publish. A
file kind with no line gets no file (``output_file_id`` stays null when every
request failed); a batch whose input was invalid gets neither.

**Downloads.** A result file is rendered again on every download, in pages of
items, from the same items with the renderer version its manifest names, so the
bytes and their digest match the manifest whenever it is downloaded. While a
download runs it holds a read lease on the batch, renewed as it goes, which
defers the physical deletion a user's delete or the retention purge would
otherwise do. The lease is renewed by a task of its own, so a slow reader
keeps it, for at most :data:`MAX_DOWNLOAD_SECONDS`. Only a permanent user
deletion removes the rows under a running download; a download that finds its
data gone, its lease lost or its bytes off the manifest fails (the client sees
fewer bytes than ``Content-Length``) rather than end as if complete.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from typing import TYPE_CHECKING, Any

from serving.batch.renderers import (
    FINAL_ITEM_STATUSES,
    OUTPUT_STATUSES,
    RENDERER_VERSION,
    result_line,
)
from serving.storage.batch_file_store import new_file_id
from serving.storage.batch_store import terminal_event
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = get_logger(__name__)

#: Items read per page while sealing or downloading.
PAGE = 200
#: How long a download's read lease lasts without renewal.
READ_LEASE_SECONDS = 120
_RENEW_EVERY = 30.0
#: The longest one download may hold its lease.
MAX_DOWNLOAD_SECONDS = 2 * 3600


class DownloadInterrupted(RuntimeError):
    """The rows a download was reading were removed under it (a permanent user deletion)."""


def _wanted(kind: str) -> Callable[[dict[str, Any]], bool]:
    if kind == "output":
        return lambda item: item.get("status") in OUTPUT_STATUSES
    if kind == "error":
        return lambda item: item.get("status") in FINAL_ITEM_STATUSES - OUTPUT_STATUSES
    return lambda item: item.get("status") in FINAL_ITEM_STATUSES


async def seal_file_batch(
    *, store: Any, batch: dict[str, Any], worker_id: str, lease_seconds: int
) -> dict[str, Any] | None:
    """Render a leased sealing batch's manifests and publish them; None when it lost the lease."""
    batch_id = batch["id"]
    generation = int(batch["seal_generation"])
    digests = {kind: hashlib.sha256() for kind in ("output", "error")}
    sizes = {"output": 0, "error": 0}
    lines = {"output": 0, "error": 0}
    after = 0
    last_renew = time.monotonic()
    while True:
        items = await store.iter_items(batch_id, after=after, limit=PAGE)
        if not items:
            break
        for item in items:
            after = item["id"]
            kind = "output" if item.get("status") in OUTPUT_STATUSES else "error"
            line = result_line(item, RENDERER_VERSION)
            digests[kind].update(line)
            sizes[kind] += len(line)
            lines[kind] += 1
        if time.monotonic() - last_renew > _RENEW_EVERY:
            last_renew = time.monotonic()
            if not await store.extend_batch_work(
                batch_id=batch_id, worker_id=worker_id, lease_seconds=lease_seconds, phase="sealing"
            ):
                return None
    files = [
        {
            "id": new_file_id(),
            "kind": kind,
            "filename": f"{batch_id}_{kind}.jsonl",
            "bytes": sizes[kind],
            "sha256": digests[kind].hexdigest(),
            "line_count": lines[kind],
            "renderer_version": RENDERER_VERSION,
        }
        for kind in ("output", "error")
        if lines[kind]
    ]
    final = batch.get("pending_terminal_status") or "completed"
    sealed = await store.commit_seal(
        batch_id=batch_id,
        worker_id=worker_id,
        generation=generation,
        files=files,
        event=terminal_event(batch_id, final, api_surface=batch.get("api_surface")),
    )
    if sealed is not None:
        logger.info(
            "batch %s sealed as %s (%d output, %d error lines)",
            batch_id,
            final,
            lines["output"],
            lines["error"],
            extra={"event": "batch_sealed"},
        )
    return sealed


class _LeaseKeeper:
    """Renews a download's read lease on its own schedule, however slowly the client reads.

    A client that stops reading suspends the response generator, so renewing
    between pages would let the lease lapse under a slow reader. This task
    renews every :data:`_RENEW_EVERY` seconds while the download is open, up to
    :data:`MAX_DOWNLOAD_SECONDS`; after that, or when a renewal finds the lease
    or its resource gone, :attr:`lost` is set and the download fails rather
    than end as if complete.
    """

    def __init__(self, store: Any, lease_id: str) -> None:
        self._store = store
        self._lease_id = lease_id
        self.lost = False
        self._task = asyncio.create_task(self._run(), name="batch_read_lease")

    async def _run(self) -> None:
        started = time.monotonic()
        while True:
            await asyncio.sleep(_RENEW_EVERY)
            if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                self.lost = True
                return
            try:
                if not await self._store.renew_read_lease(
                    self._lease_id, ttl_seconds=READ_LEASE_SECONDS
                ):
                    self.lost = True
                    return
            except Exception:
                logger.warning("could not renew a batch read lease", exc_info=True)

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        try:
            await self._store.drop_read_lease(self._lease_id)
        except Exception:
            logger.warning("could not drop a batch read lease", exc_info=True)


async def stream_lines(
    *,
    store: Any,
    batch_id: str,
    kind: str,
    version: int = RENDERER_VERSION,
    expected: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """Yield the lines of one of a batch's result files, a page at a time.

    ``kind`` is ``output`` or ``error`` (a file batch's two files), or ``all``
    (every result, an inline batch's export). With
    ``expected`` (a manifest), the stream's size and digest are checked as it
    goes: more bytes than the manifest, or fewer or different ones at the end,
    fail the download instead of ending it as complete.

    Raises:
        DownloadInterrupted: The batch was removed under the download, its
            read lease was lost, or the rendering does not match its manifest.
    """
    wanted = _wanted(kind)
    try:
        lease = await store.take_read_lease(batch_id=batch_id, ttl_seconds=READ_LEASE_SECONDS)
    except LookupError:
        raise DownloadInterrupted(batch_id) from None
    keeper = _LeaseKeeper(store, lease)
    digest = hashlib.sha256()
    size = 0
    limit = int(expected["bytes"]) if expected is not None else None
    try:
        after = 0
        while True:
            items = await store.iter_items(batch_id, after=after, limit=PAGE)
            if keeper.lost:
                raise DownloadInterrupted(batch_id)
            if not items:
                break
            chunk = bytearray()
            for item in items:
                after = item["id"]
                if wanted(item):
                    chunk += result_line(item, version)
            if chunk:
                digest.update(chunk)
                size += len(chunk)
                if limit is not None and size > limit:
                    _mismatch(batch_id, kind, size, digest, expected)
                    raise DownloadInterrupted(batch_id)
                yield bytes(chunk)
        if not await store.batch_exists(batch_id):
            raise DownloadInterrupted(batch_id)
        if expected is not None and (size != limit or digest.hexdigest() != expected.get("sha256")):
            _mismatch(batch_id, kind, size, digest, expected)
            raise DownloadInterrupted(batch_id)
    finally:
        await keeper.close()


def _mismatch(batch_id: str, kind: str, size: int, digest: Any, expected: dict[str, Any]) -> None:
    logger.error(
        "batch %s %s file rendered %d bytes (%s), manifest says %s bytes (%s)",
        batch_id,
        kind,
        size,
        digest.hexdigest(),
        expected.get("bytes"),
        expected.get("sha256"),
        extra={"event": "batch_manifest_mismatch"},
    )


async def stream_upload(*, store: Any, file_store: Any, file_id: str) -> AsyncIterator[bytes]:
    """Yield an upload's stored bytes in order, under a read lease on the file.

    Raises:
        DownloadInterrupted: The bytes were removed under the download, or the
            lease was lost: the download fails rather than end short.
    """
    upload = await file_store.get_file(file_id, include_deleted=True)
    if upload is None:
        raise DownloadInterrupted(file_id)
    expected = int(upload.get("bytes") or 0)
    try:
        lease = await store.take_read_lease(file_id=file_id, ttl_seconds=READ_LEASE_SECONDS)
    except LookupError:
        raise DownloadInterrupted(file_id) from None
    keeper = _LeaseKeeper(store, lease)
    size = 0
    try:
        after = -1
        while True:
            chunks = await file_store.read_chunks(file_id, after_seq=after, limit=4)
            if keeper.lost:
                raise DownloadInterrupted(file_id)
            if not chunks:
                break
            for seq, data in chunks:
                if seq != after + 1:
                    raise DownloadInterrupted(file_id)
                after = seq
                size += len(data)
                yield data
        if size != expected:
            raise DownloadInterrupted(file_id)
    finally:
        await keeper.close()
