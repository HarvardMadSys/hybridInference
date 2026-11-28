"""A batch request nested deeper than the interpreter's recursion limit allows.

``json.loads`` accepts thousands of levels of nesting, but the walk that looks
for a NUL before a request is stored recursed twice per level, so a request a
few hundred levels deep holding a NUL raised ``RecursionError``: the validator
kept the batch's lease and tried it again on every expiry. The walk no longer
recurses, and a request the store still cannot encode is refused by name.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from serving.batch import admission, targets, validation
from serving.batch.config import BatchConfig
from serving.storage import batch_store
from serving.storage.batch_store import BatchItemUnstorable, BatchStore

#: Deeper than the recursive walk reached, on every supported interpreter
#: (3.11's parser counts against the same limit, so ``json.loads`` refuses
#: some thousand levels there; 600 parses and encodes everywhere).
_STORABLE_DEPTH = 600


def _nested(depth: int, leaf: Any) -> Any:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("depth", [2_000, 100_000])
def test_a_nul_is_found_however_deeply_the_value_nests(depth):
    assert batch_store._contains_nul({"k": _nested(depth, "a\x00b")})
    assert not batch_store._contains_nul({"k": _nested(depth, "ab")})
    assert batch_store._contains_nul(_nested(depth, {"a\x00": 1}))


def test_a_deeply_nested_request_holding_a_nul_is_stored_losslessly():
    request = {"model": "m", "metadata": _nested(_STORABLE_DEPTH, "a\x00b")}
    stored = batch_store._dump_request({"custom_id": "c", "request": request})
    assert batch_store._is_json_envelope(batch_store._load_plain_json(stored))
    assert batch_store._load_json(stored) == request


def _unencodable(monkeypatch) -> None:
    def dump_json(value: Any) -> str | None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(batch_store, "_dump_json", dump_json)


@pytest.mark.asyncio
async def test_the_store_names_the_request_it_cannot_encode_before_writing(monkeypatch):
    _unencodable(monkeypatch)
    store = BatchStore(SimpleNamespace())  # a pool it must never reach
    item = {"custom_id": "deep", "model_id": "m", "request": {}, "line_no": 7}
    with pytest.raises(BatchItemUnstorable) as created:
        await store.create_batch(
            batch_id="batch_x",
            user_id="u",
            key_hash=None,
            endpoint="/v1/chat/completions",
            model_ids=["m"],
            items=[item],
            metadata=None,
            completion_window_seconds=None,
        )
    assert (created.value.custom_id, created.value.line_no) == ("deep", 7)
    assert isinstance(created.value, ValueError)
    with pytest.raises(BatchItemUnstorable) as staged:
        await store.stage_items(
            batch_id="batch_x", worker_id="w", user_id="u", items=[item], max_queued=None
        )
    assert staged.value.line_no == 7


@pytest.mark.asyncio
async def test_an_inline_batch_with_a_request_that_cannot_be_stored_is_a_400():
    store = SimpleNamespace(create_batch=AsyncMock(side_effect=BatchItemUnstorable("deep", None)))
    with pytest.raises(HTTPException) as refused:
        await admission.create(store, user_id="u")
    assert refused.value.status_code == 400
    assert "'deep'" in refused.value.detail and "nested too deeply" in refused.value.detail


@pytest.mark.asyncio
async def test_a_check_that_recurses_too_deeply_refuses_the_item(monkeypatch):
    def recurse(body: Any) -> str | None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(targets, "chat_limit_problem", recurse)
    canonical, problem = await targets.check_item(targets.TARGET_CHAT, {"model": "m"}, None)
    assert canonical is None
    assert (problem.code, problem.status, problem.param) == ("invalid_request", 400, "body")
    assert "nested too deeply" in problem.message


class _Upload:
    """A file store holding one ready upload of *data*."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def get_file(self, file_id: str, **_: Any) -> dict[str, Any]:
        return {"id": file_id, "status": "ready"}

    async def read_chunks(
        self, file_id: str, *, after_seq: int = -1, limit: int = 8
    ) -> list[tuple[int, bytes]]:
        return [(0, self.data)] if after_seq < 0 else []


class _Store:
    """The validator's side of the batch store; staging fails as *stage_error*."""

    def __init__(self, stage_error: Exception | None = None) -> None:
        self.stage_error = stage_error
        self.staged: list[dict[str, Any]] = []
        self.failed: dict[str, Any] | None = None

    async def reset_staged_items(self, batch_id: str, worker_id: str) -> bool:
        return True

    async def stage_items(self, *, items: list[dict[str, Any]], **_: Any) -> bool:
        if self.stage_error is not None:
            raise self.stage_error
        for item in items:
            batch_store._dump_request(item)
        self.staged.extend(items)
        return True

    async def fail_validation(self, *, errors: dict[str, Any], **_: Any) -> None:
        self.failed = errors

    async def publish_validated(self, **_: Any) -> dict[str, Any]:
        return {"status": "in_progress"}


async def _validate(monkeypatch, store: _Store, *bodies: dict[str, Any]) -> str:
    monkeypatch.setattr(
        validation, "authenticate_stored_key", AsyncMock(return_value={"user_id": "u"})
    )
    monkeypatch.setattr(validation, "admit_item", AsyncMock(return_value="m"))
    lines = [
        batch_store.json.dumps(
            {"custom_id": f"r{n}", "method": "POST", "url": "/v1/chat/completions", "body": body}
        )
        for n, body in enumerate(bodies, start=1)
    ]
    return await validation.validate_file_batch(
        store=store,
        file_store=_Upload("\n".join(lines).encode()),
        batch={
            "id": "batch_x",
            "input_file_id": "file_x",
            "user_id": "u",
            "key_hash": None,
            "endpoint": "/v1/chat/completions",
        },
        worker_id="w",
        services=SimpleNamespace(),
        cfg=BatchConfig(),
        lease_seconds=60,
    )


@pytest.mark.asyncio
async def test_a_deeply_nested_line_holding_a_nul_is_validated(monkeypatch):
    store = _Store()
    deep = {"model": "m", "metadata": _nested(_STORABLE_DEPTH, "a\x00b")}
    assert await _validate(monkeypatch, store, {"model": "m"}, deep) == "published"
    assert [item["line_no"] for item in store.staged] == [1, 2]
    assert store.failed is None


@pytest.mark.asyncio
async def test_a_line_the_store_cannot_encode_fails_validation_by_line(monkeypatch):
    store = _Store(stage_error=BatchItemUnstorable("r2", 2))
    assert await _validate(monkeypatch, store, {"model": "m"}, {"model": "m"}) == "failed"
    assert store.failed["data"] == [
        {
            "code": "invalid_json_line",
            "message": "Line 2: body is nested too deeply to store.",
            "param": "body",
            "line": 2,
        }
    ]
