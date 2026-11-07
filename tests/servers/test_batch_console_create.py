"""The console create routes' boundary: session tokens only, and what a batch may be copied as."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving.servers.deps import AppServices
from serving.servers.routers import batch_console, batch_console_create
from serving.servers.routers.batch_console import batch_detail
from serving.servers.routers.batch_console_create import credential_revision


class _Untouchable:
    """A store that fails the test if anything reaches it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"store.{name} was reached without a session")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.state.services = AppServices(
        router=_Untouchable(),
        batch_store=_Untouchable(),
        batch_file_store=_Untouchable(),
        operational_store=None,
    )
    app.include_router(batch_console.router)
    app.include_router(batch_console_create.router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/user/batches"),
        ("POST", "/user/batch-preflight"),
        ("POST", "/user/batch-files"),
        ("DELETE", "/user/batch-files/file-x"),
        ("GET", "/user/batch-create/context"),
        ("GET", "/user/batch-submissions/console-x"),
        ("GET", "/user/batches/batch_x/unfinished-requests"),
    ],
)
def test_a_cookie_alone_is_not_a_session(client, method, path):
    # A cross-site form post carries cookies, never the Authorization header.
    response = client.request(
        method,
        path,
        cookies={"refresh_token": "anything", "access_token": "anything"},
        headers={"Idempotency-Key": "console-x", "Origin": "https://elsewhere.example"},
        json={"source_file_id": "file-x", "phase": "identify"},
    )
    assert response.status_code == 401


class _Files:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows

    async def get_file(self, file_id, *, user_id=None, include_deleted=False):
        row = self.rows.get(file_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return row


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "batch_1",
        "user_id": "alice",
        "api_surface": "openai_file",
        "target": "chat",
        "endpoint": "/v1/chat/completions",
        "status": "completed",
        "phase": "sealed",
        "model_ids": ["m"],
        "input_file_id": "file-in",
        "created_via": None,
        "console_spec": None,
    }
    row.update(overrides)
    return row


def _file(file_id: str, *, owner: str = "alice", **overrides: Any) -> dict[str, Any]:
    row = {
        "id": file_id,
        "user_id": owner,
        "kind": "upload",
        "status": "ready",
        "deleted_at": None,
        "expires_at": datetime.now(UTC) + timedelta(days=3),
        "derived_from_file_id": None,
        "filename": "in.jsonl",
        "sha256": "0" * 64,
        "bytes": 10,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "files", "copy"),
    [
        (_row(), {"file-in": _file("file-in")}, {"state": "ready", "reason": None}),
        (
            _row(target="embeddings", endpoint="/v1/embeddings"),
            {"file-in": _file("file-in")},
            {"state": "ready", "reason": None},
        ),
        (
            _row(),
            {"file-in": _file("file-in", deleted_at=datetime.now(UTC))},
            {"state": "reupload", "reason": "source_unavailable"},
        ),
        (
            _row(),
            {"file-in": _file("file-in", expires_at=datetime.now(UTC) - timedelta(seconds=1))},
            {"state": "reupload", "reason": "source_unavailable"},
        ),
        (_row(), {}, {"state": "reupload", "reason": "source_unavailable"}),
        # Another user's file under the same id is not this batch's source.
        (
            _row(),
            {"file-in": _file("file-in", owner="bob")},
            {"state": "reupload", "reason": "source_unavailable"},
        ),
        (
            _row(api_surface="openai_inline", input_file_id=None),
            {},
            {"state": "unavailable", "reason": "inline_input"},
        ),
        (
            _row(target="messages", endpoint="/v1/messages"),
            {"file-in": _file("file-in")},
            {"state": "unavailable", "reason": "unsupported_target"},
        ),
        (
            _row(api_surface="anthropic", target="messages", input_file_id=None),
            {},
            {"state": "unavailable", "reason": "inline_input"},
        ),
        (
            _row(target="responses", endpoint="/v1/responses"),
            {"file-in": _file("file-in")},
            {"state": "unavailable", "reason": "unsupported_target"},
        ),
        # A console batch copies from its source, never from its stored copy.
        (
            _row(
                created_via="console",
                input_file_id="file-derived",
                console_spec={"source_file_id": "file-src"},
            ),
            {
                "file-derived": _file("file-derived", derived_from_file_id="file-src"),
                "file-src": _file("file-src"),
            },
            {"state": "ready", "reason": None},
        ),
        (
            _row(
                created_via="console",
                input_file_id="file-derived",
                console_spec={"source_file_id": "file-src"},
            ),
            {"file-derived": _file("file-derived", derived_from_file_id="file-src")},
            {"state": "reupload", "reason": "source_unavailable"},
        ),
    ],
)
async def test_what_a_batch_may_be_copied_as(row, files, copy):
    detail = await batch_detail(row, file_store=_Files(files), owner="alice")
    assert detail["copy"] == copy


def test_a_credential_revision_changes_with_the_key_and_reveals_nothing():
    first = credential_revision(7, "hash-a")
    assert first == credential_revision(7, "hash-a")
    assert first != credential_revision(7, "hash-b")  # regenerated in place
    assert first != credential_revision(8, "hash-a")  # another key row
    assert "hash-a" not in first and len(first) == 24
