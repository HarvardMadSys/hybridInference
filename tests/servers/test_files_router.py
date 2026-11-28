"""Administrators reaching another owner's batch files through ``/v1/files``.

An administrator may read and delete any owner's file by id. Each such access is
audited against the file's owner, and an upload's content -- that owner's
prompts -- downloads only where the request log keeps prompts
(``db_store_full_content``), as on the admin console. A caller's own files are
neither audited nor gated. The stores are in-memory stand-ins for the half of
their contract these routes use.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.servers.auth import verify_batch_management_identity
from serving.servers.deps import AppServices, get_operational_store
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import files

UPLOAD = b'{"custom_id": "q", "body": {"messages": [{"role": "user", "content": "secret"}]}}\n'

USERS = {
    "key-alice": {"user_id": "alice", "role": "internal", "api_key_id": 7},
    "key-root": {"user_id": "root", "role": "admin", "api_key_id": 9},
}


def _file(file_id: str, owner: str, kind: str = "upload", **extra: Any) -> dict[str, Any]:
    return {
        "id": file_id,
        "user_id": owner,
        "purpose": "batch" if kind == "upload" else "batch_output",
        "kind": kind,
        "filename": f"{file_id}.jsonl",
        "status": "ready",
        "bytes": len(UPLOAD) if kind == "upload" else 0,
        "sha256": None,
        "batch_id": None if kind == "upload" else "batch_1",
        "renderer_version": 1,
        "derived_from_file_id": None,
        **extra,
    }


class FakeFileStore:
    def __init__(self) -> None:
        self.files = {
            "file-alice": _file("file-alice", "alice"),
            # A console batch's stored requests: an upload derived from its source.
            "file-derived": _file("file-derived", "alice", derived_from_file_id="file-alice"),
            "file-output": _file("file-output", "alice", kind="output"),
            "file-root": _file("file-root", "root"),
        }

    async def get_file(
        self, file_id: str, *, user_id: str | None = None, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        row = self.files.get(file_id)
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return None
        return dict(row)

    async def delete_file(self, file_id: str, *, user_id: str | None) -> bool:
        if await self.get_file(file_id, user_id=user_id) is None:
            return False
        del self.files[file_id]
        return True

    async def read_chunks(self, file_id: str, *, after_seq: int = -1, limit: int = 8):
        return [(0, UPLOAD)] if after_seq < 0 else []


class FakeBatchStore:
    """The read lease a download holds; nothing here expires it."""

    async def take_read_lease(self, **_kwargs: Any) -> str:
        return "lease"

    async def renew_read_lease(self, lease_id: str, *, ttl_seconds: int) -> bool:
        return True

    async def drop_read_lease(self, lease_id: str) -> None:
        return None


async def _one_output_line(**_kwargs: Any):
    yield b'{"custom_id": "q", "response": {}}\n'


@pytest.fixture
def audits(monkeypatch) -> list[dict[str, Any]]:
    """The audit rows the routes write, one dict of arguments each."""
    rows: list[dict[str, Any]] = []

    async def record(_op_store: Any, **kwargs: Any) -> None:
        rows.append(kwargs)

    monkeypatch.setattr(files, "log_admin_action", record)
    return rows


@pytest.fixture
def file_store() -> FakeFileStore:
    return FakeFileStore()


@pytest.fixture
def app(monkeypatch, file_store) -> FastAPI:
    monkeypatch.setattr(batch_config_module, "config_value", lambda key, default=None: default)
    monkeypatch.setattr(files, "stream_lines", _one_output_line)
    application = FastAPI()
    application.state.services = AppServices(
        router=None,
        batch_store=FakeBatchStore(),
        batch_file_store=file_store,
        log_store=MagicMock(store_full_prompts=False),
    )

    async def identity(authorization: str | None = Header(None)) -> dict[str, Any]:
        key = (authorization or "").removeprefix("Bearer ")
        if key not in USERS:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, **USERS[key]}

    application.dependency_overrides[verify_batch_management_identity] = identity
    # Where an administrator's access to another owner's file is audited.
    audit_log = SimpleNamespace(log_admin_action=AsyncMock())
    application.dependency_overrides[get_operational_store] = lambda: audit_log
    install_error_handlers(application)
    application.include_router(files.router)
    return application


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _keep_prompts(app: FastAPI, enabled: bool) -> None:
    app.state.services.log_store = MagicMock(store_full_prompts=enabled)


@pytest.mark.parametrize("file_id", ["file-alice", "file-derived"])
async def test_another_owners_upload_downloads_only_where_prompts_are_kept(
    app, client, audits, file_id
):
    refused = await client.get(f"/v1/files/{file_id}/content", headers=_auth("key-root"))

    assert refused.status_code == 403
    assert "db_store_full_content is off" in refused.json()["error"]["message"]
    assert audits == []

    _keep_prompts(app, True)
    served = await client.get(f"/v1/files/{file_id}/content", headers=_auth("key-root"))

    assert served.status_code == 200
    assert served.content == UPLOAD
    (row,) = audits
    assert row["action"] == "batch.files.download"
    assert (row["actor"].type, row["actor"].id) == ("admin", "root")
    assert (row["target"].type, row["target"].id) == ("user", "alice")
    assert row["details"] == {"file_id": file_id, "kind": "upload", "api_key_id": 9}


async def test_another_owners_output_file_downloads_audited_whatever_is_kept(client, audits):
    response = await client.get("/v1/files/file-output/content", headers=_auth("key-root"))

    assert response.status_code == 200
    (row,) = audits
    assert row["action"] == "batch.files.download"
    assert (row["target"].type, row["target"].id) == ("user", "alice")
    assert row["details"]["kind"] == "output"


async def test_reading_and_deleting_another_owners_file_is_audited(client, file_store, audits):
    read = await client.get("/v1/files/file-alice", headers=_auth("key-root"))
    deleted = await client.delete("/v1/files/file-alice", headers=_auth("key-root"))

    assert read.json()["id"] == "file-alice"
    assert deleted.json()["deleted"] is True
    assert "file-alice" not in file_store.files
    assert [row["action"] for row in audits] == ["batch.files.read", "batch.files.delete"]
    assert {(row["target"].type, row["target"].id) for row in audits} == {("user", "alice")}
    assert audits[1]["details"] == {"file_id": "file-alice", "kind": "upload", "api_key_id": 9}


async def test_another_owners_file_is_not_deleted_when_the_audit_fails(
    app, file_store, monkeypatch
):
    async def unwritable(_op_store: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the audit log is unavailable")

    monkeypatch.setattr(files, "log_admin_action", unwritable)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.delete("/v1/files/file-output", headers=_auth("key-root"))

    # Audited before it is deleted: an output file is gone for good, so one
    # whose row cannot be written stays.
    assert response.status_code == 500
    assert "file-output" in file_store.files


async def test_a_delete_that_finds_nothing_after_its_audit_is_recorded_as_not_made(
    client, file_store, audits, monkeypatch
):
    async def gone_meanwhile(file_id: str, *, user_id: str | None) -> bool:
        return False

    monkeypatch.setattr(file_store, "delete_file", gone_meanwhile)
    response = await client.delete("/v1/files/file-alice", headers=_auth("key-root"))

    assert response.status_code == 404
    assert [(row["action"], row["success"]) for row in audits] == [
        ("batch.files.delete", True),
        ("batch.files.delete", False),
    ]
    assert audits[1]["details"]["error"] == "not made (HTTP 404)"


async def test_a_deleted_file_is_404_to_an_administrator_and_not_audited(
    client, file_store, audits
):
    file_store.files["file-gone"] = _file("file-gone", "alice", deleted_at="2026-11-01")

    response = await client.delete("/v1/files/file-gone", headers=_auth("key-root"))

    assert response.status_code == 404
    assert audits == []


async def test_another_owners_file_is_not_reached_without_an_audit_log(app, client, file_store):
    app.dependency_overrides[get_operational_store] = lambda: None

    read = await client.get("/v1/files/file-output", headers=_auth("key-root"))
    deleted = await client.delete("/v1/files/file-output", headers=_auth("key-root"))
    own = await client.get("/v1/files/file-root", headers=_auth("key-root"))

    # Nowhere to write its row: neither answered nor deleted. One's own needs none.
    assert (read.status_code, deleted.status_code) == (500, 500)
    assert "file-output" in file_store.files
    assert own.status_code == 200


async def test_an_unknown_file_is_404_and_not_audited(client, audits):
    for response in (
        await client.get("/v1/files/file-missing", headers=_auth("key-root")),
        await client.get("/v1/files/file-missing/content", headers=_auth("key-root")),
        await client.delete("/v1/files/file-missing", headers=_auth("key-root")),
    ):
        assert response.status_code == 404
    assert audits == []


@pytest.mark.parametrize(
    ("key", "file_id"), [("key-alice", "file-alice"), ("key-root", "file-root")]
)
async def test_ones_own_files_are_neither_gated_nor_audited(
    client, file_store, audits, key, file_id
):
    content = await client.get(f"/v1/files/{file_id}/content", headers=_auth(key))
    read = await client.get(f"/v1/files/{file_id}", headers=_auth(key))
    deleted = await client.delete(f"/v1/files/{file_id}", headers=_auth(key))

    assert content.content == UPLOAD
    assert read.status_code == 200
    assert deleted.status_code == 200
    assert audits == []


async def test_an_owner_never_reaches_another_owners_file(client, audits):
    response = await client.get("/v1/files/file-root/content", headers=_auth("key-alice"))

    assert response.status_code == 404
    assert audits == []
