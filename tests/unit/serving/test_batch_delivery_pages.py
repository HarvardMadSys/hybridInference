"""Sealing and downloads read results a bounded page at a time and share the loop (H5).

A batch's results can be megabytes each (an embeddings list). Sealing and every
download read them in pages bounded by the size of their stored JSON, read only
the items the file holds, decode and render one item at a time, and give the
event loop a turn between items, so a page of large results never holds the
loop for the whole page. The stores here are stand-ins with the paged API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from serving.batch import delivery
from serving.batch.renderers import FINAL_ITEM_STATUSES, OUTPUT_STATUSES, result_line
from serving.storage import batch_store


def _response(n: int) -> dict[str, Any]:
    return {"object": "list", "data": [{"index": n, "embedding": [n / 7] * 2000}]}


def _row(item_id: int, status: str) -> dict[str, Any]:
    response = _response(item_id) if status == "completed" else None
    return {
        "id": item_id,
        "custom_id": f"req-{item_id}",
        "status": status,
        "status_code": 200 if status == "completed" else None,
        "response": response,
        "error": None if status == "completed" else {"code": "timeout", "message": "slow"},
        "request_id": f"r{item_id}",
    }


class PagedStore:
    """The paged read API of the batch store, over rows in memory."""

    def __init__(self, statuses: list[str]) -> None:
        self.rows = [_row(i, status) for i, status in enumerate(statuses, start=1)]
        self.pages: list[dict[str, Any]] = []
        self.extended = 0

    async def result_page(self, batch_id, *, max_bytes, after=0, limit=200, statuses=None):
        self.pages.append({"max_bytes": max_bytes, "statuses": statuses, "limit": limit})
        rows = [
            r
            for r in self.rows
            if r["id"] > after and (statuses is None or r["status"] in statuses)
        ]
        page, total = [], 0
        for row in rows[:limit]:
            text = json.dumps(row["response"]) if row["response"] is not None else None
            size = len(text or "")
            if page and total + size > max_bytes:
                break
            raw = {k: v for k, v in row.items() if k != "response"}
            page.append({**raw, "response_text": text, "bytes": size})
            total += size
        return page, len(page) < len(rows)

    async def iter_items(self, batch_id, *, after=0, limit=500):
        """The count-only pages an earlier version read, decoded whole."""
        self.pages.append({"max_bytes": None, "statuses": None, "limit": limit})
        return [dict(r) for r in self.rows if r["id"] > after][:limit]

    async def extend_batch_work(self, **_kwargs: Any) -> bool:
        self.extended += 1
        return True

    async def commit_seal(self, *, files, **_kwargs: Any) -> dict[str, Any]:
        self.files = files
        return {"files": files}

    async def take_read_lease(self, **_kwargs: Any) -> str:
        return "lease"

    async def renew_read_lease(self, lease_id: str, *, ttl_seconds: int) -> bool:
        return True

    async def drop_read_lease(self, lease_id: str) -> None:
        return None

    async def batch_exists(self, batch_id: str) -> bool:
        return True


def _expected(store: PagedStore, statuses: frozenset[str]) -> bytes:
    return b"".join(result_line(row) for row in store.rows if row["status"] in statuses)


async def _seal(store: PagedStore) -> dict[str, Any]:
    batch = {"id": "batch_1", "seal_generation": 1, "pending_terminal_status": "completed"}
    return await delivery.seal_file_batch(store=store, batch=batch, worker_id="w", lease_seconds=60)


@pytest.fixture
def ticks():
    """Count the turns the event loop gives other tasks while a test awaits."""
    state = {"n": 0}

    async def tick() -> None:
        while True:
            state["n"] += 1
            await asyncio.sleep(0)

    return state, tick


async def test_sealing_yields_to_the_loop_between_large_results(ticks, monkeypatch):
    monkeypatch.setattr(delivery, "_YIELD_BYTES", 1)
    state, tick = ticks
    store = PagedStore(["completed"] * 20)
    ticker = asyncio.create_task(tick())
    try:
        await asyncio.sleep(0)
        before = state["n"]
        await _seal(store)
        turns = state["n"] - before
    finally:
        ticker.cancel()

    # One page held all twenty; other tasks still ran between its items.
    assert len(store.pages) == 1
    assert turns >= 20


async def test_sealed_manifests_match_what_a_download_renders(monkeypatch):
    monkeypatch.setattr(delivery, "PAGE_BYTES", 50_000)
    store = PagedStore(["completed", "failed", "completed", "expired", "completed", "cancelled"])
    sealed = await _seal(store)
    manifests = {spec["kind"]: spec for spec in sealed["files"]}

    for kind, statuses in (
        ("output", OUTPUT_STATUSES),
        ("error", FINAL_ITEM_STATUSES - OUTPUT_STATUSES),
    ):
        expected = _expected(store, statuses)
        manifest = manifests[kind]
        assert manifest["bytes"] == len(expected)
        assert manifest["sha256"] == hashlib.sha256(expected).hexdigest()
        body = b"".join(
            [
                part
                async for part in delivery.stream_lines(
                    store=store, batch_id="batch_1", kind=kind, expected=manifest
                )
            ]
        )
        assert body == expected


async def test_pages_are_bounded_by_bytes_and_read_only_the_files_items(monkeypatch):
    monkeypatch.setattr(delivery, "PAGE_BYTES", 50_000)
    store = PagedStore(["completed", "failed"] * 5)
    item_bytes = len(json.dumps(_response(1)))

    body = b"".join(
        [part async for part in delivery.stream_lines(store=store, batch_id="b", kind="error")]
    )

    assert body == _expected(store, FINAL_ITEM_STATUSES - OUTPUT_STATUSES)
    assert all(page["statuses"] == FINAL_ITEM_STATUSES - OUTPUT_STATUSES for page in store.pages)
    assert all(page["max_bytes"] == 50_000 for page in store.pages)

    store.pages.clear()
    output = b"".join(
        [part async for part in delivery.stream_lines(store=store, batch_id="b", kind="output")]
    )
    assert output == _expected(store, OUTPUT_STATUSES)
    # Several large results to a page, never all of them at once.
    per_page = 50_000 // item_bytes
    assert len(store.pages) == -(-5 // per_page)


async def test_a_download_sends_large_pages_in_parts(monkeypatch):
    monkeypatch.setattr(delivery, "_SEND_BYTES", 1)
    store = PagedStore(["completed"] * 4)

    parts = [part async for part in delivery.stream_lines(store=store, batch_id="b", kind="all")]

    assert len(parts) == 4
    assert b"".join(parts) == _expected(store, FINAL_ITEM_STATUSES)


def test_encoding_a_result_walks_it_for_nul_only_when_its_text_may_hold_one(monkeypatch):
    walked = []
    real = batch_store._contains_nul

    def contains_nul(value):
        walked.append(value)
        return real(value)

    monkeypatch.setattr(batch_store, "_contains_nul", contains_nul)

    plain = {"data": [0.5] * 10, "text": "no escape here"}
    assert batch_store._dump_json(plain) == json.dumps(plain)
    assert walked == []
    # A literal backslash-u0000 reads like the escape: walked, and stored as it is.
    lookalike = {"text": "\\u0000"}
    assert batch_store._dump_json(lookalike) == json.dumps(lookalike)
    assert walked[0] == lookalike
    # A real NUL still takes the envelope, and round-trips.
    nul = {"text": "a\x00b"}
    stored = batch_store._dump_json(nul)
    assert stored != json.dumps(nul)
    assert batch_store._load_json(stored) == nul
