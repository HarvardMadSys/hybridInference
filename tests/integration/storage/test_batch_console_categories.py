"""The console's four batch categories, against Postgres.

The category is what a person sees and filters by on the Batches page:
``processing``, ``completed`` (every request succeeded), ``issues`` (failed,
execution window expired, or completed with an unsuccessful request) and
``cancelled`` (the cancel has finished). It is derived from the status a batch
shows -- never a file batch's stored terminal status while it is still sealing
or draining -- and, for a completed batch, its counts. Python
(``renderers.display_category``) and SQL (``display_category_sql``) must agree,
and the filter must apply before the page is cut. The public API's statuses and
the exact-status filter stay as they were.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.batch.config import batch_config
from serving.batch.renderers import CATEGORIES, display_category, display_status
from serving.servers.deps import AppServices, get_current_user
from serving.servers.routers import batch_console
from serving.servers.routers.batches import batch_object
from serving.storage.batch_store import BatchStore, display_category_sql
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=1, max_size=8)
    await BatchStore(pool).initialize()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def store(pool):
    store = BatchStore(pool)
    users: list[str] = []
    store.test_users = users  # type: ignore[attr-defined]
    try:
        yield store
    finally:
        for user in users:
            await store.delete_user_batches(user)


def _user(store: BatchStore) -> str:
    user = f"cat-{uuid.uuid4().hex[:10]}"
    store.test_users.append(user)  # type: ignore[attr-defined]
    return user


async def _batch(store: BatchStore, user: str, name: str, **state: Any) -> dict[str, Any]:
    """A batch of ``user`` named ``name``, then put in ``state`` (columns of batch_jobs)."""
    model = f"m-{uuid.uuid4().hex[:6]}"
    row = await store.create_batch(
        batch_id=f"batch_{uuid.uuid4().hex}",
        user_id=user,
        key_hash=None,
        endpoint="/v1/chat/completions",
        model_ids=[model],
        items=[
            {
                "custom_id": f"c-{i}",
                "model_id": model,
                "request": {"model": model, "messages": [{"role": "user", "content": "x"}]},
            }
            for i in range(10)
        ],
        metadata=None,
        completion_window_seconds=3600,
        retention_seconds=7 * 86400,
        target="chat",
    )
    columns = {"display_name": name, **state}
    assignments = ", ".join(f"{column} = ${index + 2}" for index, column in enumerate(columns))
    async with store.pool.acquire() as conn:
        await conn.execute(
            f"UPDATE batch_jobs SET {assignments} WHERE id = $1", row["id"], *columns.values()
        )
    found = await store.get_batch(row["id"], user_id=user)
    assert found is not None
    return found


INLINE = {"api_surface": "openai_inline", "phase": "runnable"}
SEALED = {"api_surface": "openai_file", "phase": "sealed"}
COUNTS_OK = {"request_count": 10, "completed_count": 10, "failed_count": 0, "cancelled_count": 0}

#: (name, columns, display status, category)
CASES: list[tuple[str, dict[str, Any], str, str]] = [
    # Every processing stage.
    (
        "validating-file",
        {"api_surface": "openai_file", "phase": "validating", "status": "validating"},
        "validating",
        "processing",
    ),
    ("running", {**INLINE, "status": "in_progress"}, "in_progress", "processing"),
    (
        "finalizing-file",
        {
            "api_surface": "openai_file",
            "phase": "sealing",
            "status": "in_progress",
            "pending_terminal_status": "completed",
        },
        "finalizing",
        "processing",
    ),
    ("cancelling-inline", {**INLINE, "status": "cancelling"}, "cancelling", "processing"),
    (
        "cancelling-file",
        {
            "api_surface": "openai_file",
            "phase": "sealing",
            "status": "cancelling",
            "pending_terminal_status": "cancelled",
        },
        "cancelling",
        "processing",
    ),
    # A file batch expired but still draining its running requests: not final yet.
    (
        "draining-file",
        {"api_surface": "openai_file", "phase": "runnable", "status": "expired"},
        "in_progress",
        "processing",
    ),
    # Completed: all successful, or not.
    ("all-ok-inline", {**INLINE, "status": "completed", **COUNTS_OK}, "completed", "completed"),
    ("all-ok-file", {**SEALED, "status": "completed", **COUNTS_OK}, "completed", "completed"),
    (
        "partial-file",
        {
            **SEALED,
            "status": "completed",
            "request_count": 1000,
            "completed_count": 980,
            "failed_count": 20,
            "cancelled_count": 0,
        },
        "completed",
        "issues",
    ),
    (
        "all-failed-inline",
        {
            **INLINE,
            "status": "completed",
            "request_count": 10,
            "completed_count": 0,
            "failed_count": 10,
            "cancelled_count": 0,
        },
        "completed",
        "issues",
    ),
    # Terminal failures.
    (
        "failed-file",
        {**SEALED, "status": "failed", "request_count": 0, "completed_count": 0, "failed_count": 0},
        "failed",
        "issues",
    ),
    (
        "expired-inline",
        {
            **INLINE,
            "status": "expired",
            "request_count": 10,
            "completed_count": 4,
            "failed_count": 6,
            "expired_count": 6,
        },
        "expired",
        "issues",
    ),
    # Cancelled wins, whatever its requests did.
    (
        "cancelled-mixed",
        {
            **SEALED,
            "status": "cancelled",
            "request_count": 10,
            "completed_count": 5,
            "failed_count": 2,
            "cancelled_count": 3,
        },
        "cancelled",
        "cancelled",
    ),
    # A batch the retired Message Batches API stored is treated alike.
    (
        "legacy-ok",
        {
            "api_surface": "anthropic",
            "target": "messages",
            "phase": "runnable",
            "status": "completed",
            **COUNTS_OK,
        },
        "completed",
        "completed",
    ),
]


async def _sql_category(store: BatchStore, batch_id: str) -> str:
    async with store.pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT {display_category_sql('j')} FROM batch_jobs AS j WHERE j.id = $1", batch_id
        )


async def test_every_state_falls_in_exactly_one_category_alike_in_python_and_sql(store):
    user = _user(store)
    for name, columns, status, category in CASES:
        row = await _batch(store, user, name, **columns)
        assert display_status(row) == status, name
        assert display_category(row) == category, name
        assert await _sql_category(store, row["id"]) == category, name
    assert {case[3] for case in CASES} == set(CATEGORIES)


async def test_the_public_api_status_of_a_partly_failed_batch_is_unchanged(store):
    user = _user(store)
    row = await _batch(
        store,
        user,
        "partial",
        **SEALED,
        status="completed",
        request_count=10,
        completed_count=8,
        failed_count=2,
        cancelled_count=0,
    )
    api = batch_object(row, batch_config())
    assert api.status == "completed"
    assert (api.request_counts.completed, api.request_counts.failed) == (8, 2)


@pytest_asyncio.fixture
async def console(store, monkeypatch):
    monkeypatch.setattr(batch_config_module, "config_value", lambda key, default=None: default)
    app = FastAPI()
    app.state.services = AppServices(router=MagicMock(), batch_store=store)

    async def session(authorization: str | None = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "no session")
        user = authorization.removeprefix("Bearer ")
        return {"user_id": user, "role": "admin" if user.startswith("cat-admin") else "internal"}

    app.dependency_overrides[get_current_user] = session
    app.include_router(batch_console.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://console.test") as c:
        yield c


async def _all_pages(client: AsyncClient, user: str, **params: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    after = None
    while True:
        query = {"limit": 2, **params, **({"after": after} if after else {})}
        page = await client.get(
            "/user/batches", params=query, headers={"Authorization": f"Bearer {user}"}
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert len(body["data"]) <= 2
        rows.extend(body["data"])
        if not body["has_more"]:
            assert body["last_id"] == (rows[-1]["id"] if rows else None)
            return rows
        after = body["last_id"]


async def test_a_category_is_filtered_before_the_page_is_cut(store, console):
    owner, other = _user(store), _user(store)
    admin = f"cat-admin-{uuid.uuid4().hex[:6]}"
    store.test_users.append(admin)  # type: ignore[attr-defined]
    expected: dict[str, list[str]] = {category: [] for category in CATEGORIES}
    # Interleave every case three times, so each category's rows sit between others.
    for round_ in range(3):
        for name, columns, _status, category in CASES:
            row = await _batch(store, owner, f"{name}-{round_}", **columns)
            expected[category].append(row["id"])
            # Another user's batch of the same kind, never listed for the owner.
            await _batch(store, other, f"{name}-{round_}", **columns)
            await _batch(store, admin, f"{name}-{round_}", **columns)

    for category in CATEGORIES:
        rows = await _all_pages(console, owner, category=category)
        assert sorted(row["id"] for row in rows) == sorted(expected[category]), category
        assert {row["category"] for row in rows} == {category}
        assert {row["user_id"] for row in rows} == {owner}
    # An administrator's own list is only theirs, too.
    admin_rows = await _all_pages(console, admin, category="issues")
    assert {row["user_id"] for row in admin_rows} == {admin}

    # With a search, still before the page is cut.
    rows = await _all_pages(console, owner, category="issues", q="partial")
    assert sorted(row["name"] for row in rows) == [
        "partial-file-0",
        "partial-file-1",
        "partial-file-2",
    ]

    # Every row lands in exactly one category: together they are the whole list.
    everything = await _all_pages(console, owner)
    assert sum(len(ids) for ids in expected.values()) == len(everything)

    # The exact status filter is unchanged: completed means the status, all of it.
    completed_status = await _all_pages(console, owner, status="completed")
    assert {row["category"] for row in completed_status} == {"completed", "issues"}

    bad = await console.get(
        "/user/batches", params={"category": "paused"}, headers={"Authorization": f"Bearer {owner}"}
    )
    assert bad.status_code == 400


async def test_every_page_says_which_category_it_applied(store, console):
    owner = _user(store)
    headers = {"Authorization": f"Bearer {owner}"}
    # Nothing at all yet: an empty page still says what it applied.
    empty = (
        await console.get("/user/batches", params={"category": "issues"}, headers=headers)
    ).json()
    assert empty["data"] == [] and empty["category_filter"] is True
    assert empty["category"] == "issues"
    for round_ in range(3):
        await _batch(store, owner, f"ok-{round_}", **INLINE, status="completed", **COUNTS_OK)
    first = (
        await console.get(
            "/user/batches", params={"category": "completed", "limit": 2}, headers=headers
        )
    ).json()
    assert first["has_more"] is True and first["category"] == "completed"
    second = (
        await console.get(
            "/user/batches",
            params={"category": "completed", "limit": 2, "after": first["last_id"]},
            headers=headers,
        )
    ).json()
    assert second["category"] == "completed" and second["category_filter"] is True
    # Unfiltered, and filtered by exact status as before: no category applied.
    for params in ({}, {"status": "completed"}):
        page = (await console.get("/user/batches", params=params, headers=headers)).json()
        assert page["category_filter"] is True and page["category"] is None
