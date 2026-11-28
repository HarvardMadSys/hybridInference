"""A batch request nested deeper than the gateway reads, or than a recursive step allows.

``json.loads`` accepts thousands of levels of nesting, but the walk that looks
for a NUL before a request is stored recursed twice per level, so a request a
few hundred levels deep holding a NUL raised ``RecursionError``: the validator
kept the batch's lease and tried it again on every expiry. A create body or a
line of an input file now nests at most ``MAX_JSON_DEPTH`` levels, refused by
line or with a 400 past it. The walk no longer recurses, a request the store
still cannot encode is refused by name, and an admission check that recurses
out is refused and logged.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from serving.batch import admission, console_prepare, targets, validation
from serving.batch.config import BatchConfig
from serving.storage import batch_store
from serving.storage.batch_store import BatchItemUnstorable, BatchStore

#: Deeper than the recursive walk reached, on every supported interpreter
#: (3.11's parser counts against the same limit, so ``json.loads`` refuses
#: some thousand levels there; 600 parses and encodes everywhere). The store
#: also encodes what no depth limit bounds, such as an upstream's response.
_STORABLE_DEPTH = 600

_LIMIT = admission.MAX_JSON_DEPTH


def _nested(depth: int, leaf: Any) -> Any:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def _line(levels: int, leaf: str = "", custom_id: str = "r1") -> bytes:
    """A chat line nesting arrays and objects exactly *levels* deep, *leaf* innermost.

    Written as text, so a line past what ``json`` itself reads can be built.
    """
    lists = levels - 2  # the line and its body are the first two levels
    line = json.dumps(
        {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {"model": "m", "metadata": "DEEP"},
        }
    )
    return line.replace('"DEEP"', "[" * lists + leaf + "]" * lists).encode()


# -- the depth limit ---------------------------------------------------------------


def test_nesting_is_measured_without_recursing():
    assert _LIMIT == 128  # the documented limit
    assert not admission.nests_too_deeply(_nested(_LIMIT, "x"))
    assert admission.nests_too_deeply(_nested(_LIMIT + 1, "x"))
    assert admission.nests_too_deeply(_nested(100_000, {"k": "v"}))
    objects: Any = "x"
    for _ in range(_LIMIT):
        objects = {"k": objects}
    assert not admission.nests_too_deeply(objects)
    assert admission.nests_too_deeply([objects])
    assert not admission.nests_too_deeply([[0] * 1000 for _ in range(1000)])
    assert not admission.nests_too_deeply("x")


@pytest.mark.parametrize("levels", [_LIMIT + 1, 100_000], ids=["past-the-limit", "past-json"])
def test_a_line_nested_past_the_limit_is_refused_by_line(levels):
    with pytest.raises(validation._Invalid) as refused:
        validation._parse_line(3, _line(levels), "/v1/chat/completions")
    assert refused.value.entry == {
        "code": "invalid_json_line",
        "message": "Line 3 nests arrays and objects more than 128 levels deep.",
        "param": None,
        "line": 3,
    }
    assert validation._parse_line(3, _line(_LIMIT), "/v1/chat/completions")["custom_id"] == "r1"


@pytest.mark.parametrize("levels", [_LIMIT + 1, 100_000], ids=["past-the-limit", "past-json"])
def test_the_console_preflight_refuses_the_same_lines(levels):
    with pytest.raises(console_prepare._LineProblem) as refused:
        console_prepare.parse_line(_line(levels), 1)
    assert (refused.value.code, refused.value.message) == (
        "invalid_json_line",
        "The line nests arrays and objects more than 128 levels deep",
    )
    assert console_prepare.parse_line(_line(_LIMIT), 1)["custom_id"] == "r1"


# -- what the store encodes --------------------------------------------------------


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
    # An item that failed admission is stored with its request too.
    refused = {**item, "error": {"code": "invalid_request", "message": "no", "status": 400}}
    for items in ([item], [refused]):
        with pytest.raises(BatchItemUnstorable) as created:
            await store.create_batch(
                batch_id="batch_x",
                user_id="u",
                key_hash=None,
                endpoint="/v1/chat/completions",
                model_ids=["m"],
                items=items,
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
async def test_a_check_that_recurses_too_deeply_refuses_the_item_and_is_logged(monkeypatch, caplog):
    def recurse(body: Any) -> str | None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(targets, "chat_limit_problem", recurse)
    with caplog.at_level(logging.WARNING, logger=targets.__name__):
        canonical, problem = await targets.check_item(targets.TARGET_CHAT, {"model": "m"}, None)
    assert canonical is None
    assert (problem.code, problem.status, problem.param) == ("invalid_request", 400, "body")
    assert "nested too deeply" in problem.message
    [record] = [record for record in caplog.records if record.name == targets.__name__]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None and record.exc_info[0] is RecursionError


# -- the file validator ------------------------------------------------------------


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


def _chat_line(custom_id: str) -> bytes:
    return json.dumps(
        {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {"model": "m"},
        }
    ).encode()


async def _validate(monkeypatch, store: _Store, *lines: bytes) -> str:
    monkeypatch.setattr(
        validation, "authenticate_stored_key", AsyncMock(return_value={"user_id": "u"})
    )
    monkeypatch.setattr(validation, "admit_item", AsyncMock(return_value="m"))
    return await validation.validate_file_batch(
        store=store,
        file_store=_Upload(b"\n".join(lines)),
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
async def test_a_line_at_the_limit_holding_a_nul_is_validated(monkeypatch):
    store = _Store()
    deep = _line(_LIMIT, leaf='"a\\u0000b"', custom_id="r2")
    assert await _validate(monkeypatch, store, _chat_line("r1"), deep) == "published"
    assert [item["line_no"] for item in store.staged] == [1, 2]
    assert store.staged[1]["request"]["metadata"] == _nested(_LIMIT - 2, "a\x00b")
    assert store.failed is None


@pytest.mark.asyncio
async def test_a_line_nested_past_the_limit_fails_validation_by_line(monkeypatch):
    """A real deep line, not a mocked encoder: nothing past the parse sees it."""
    store = _Store()
    deep = _line(5_000, leaf='"a\\u0000b"', custom_id="r2")
    assert await _validate(monkeypatch, store, _chat_line("r1"), deep) == "failed"
    assert store.failed["data"] == [
        {
            "code": "invalid_json_line",
            "message": "Line 2 nests arrays and objects more than 128 levels deep.",
            "param": None,
            "line": 2,
        }
    ]


@pytest.mark.asyncio
async def test_a_line_the_store_cannot_encode_fails_validation_by_line(monkeypatch):
    store = _Store(stage_error=BatchItemUnstorable("r2", 2))
    lines = (_chat_line("r1"), _chat_line("r2"))
    assert await _validate(monkeypatch, store, *lines) == "failed"
    assert store.failed["data"] == [
        {
            "code": "invalid_json_line",
            "message": "Line 2: body is nested too deeply to store.",
            "param": "body",
            "line": 2,
        }
    ]
