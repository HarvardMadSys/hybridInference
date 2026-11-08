"""``/user/recent-requests`` against PostgreSQL: whatever is asked, it reads the window only.

The window is the account's newest ``RECENT_REQUESTS_WINDOW`` rows, made small
here so a test can fill it: a page lies within it, the total counts no further,
and a filter -- a model nobody used, a key used only long ago -- matches among
those rows only, instead of reading the account's whole history.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from serving.servers.routers import user_routes
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

WINDOW = 100
HISTORY = 250


@pytest_asyncio.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(user_routes, "RECENT_REQUESTS_WINDOW", WINDOW)
    user_routes._RECENT_REQUESTS_COUNT_CACHE.clear()
    pool = await postgres.create_pool(min_size=1, max_size=4)
    users: list[str] = []
    try:
        yield pool, users
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM api_logs WHERE user_id = ANY($1::text[])", users)
        await pool.close()
        user_routes._RECENT_REQUESTS_COUNT_CACHE.clear()


async def _history(pool, users: list[str], rows: int) -> tuple[str, list[dict]]:
    """*rows* requests of a new account, newest first: models, keys and an auto model."""
    user = f"u-recent-{uuid.uuid4().hex[:10]}"
    users.append(user)
    now = datetime.now(UTC)
    made = []
    for i in range(rows):
        metadata: dict = {}
        if i % 10 == 0:
            metadata["api_key_id"] = "7"
        if i % 25 == 0:
            metadata["auto"] = {"requested": "auto-x"}
        made.append(
            {
                "request_id": f"{user}-{i:04d}",
                "model_id": "m-a" if i % 2 else "m-b",
                "timestamp": now - timedelta(minutes=i),
                "metadata": metadata,
            }
        )
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO api_logs (request_id, model_id, provider, user_id, timestamp, metadata, "
            "status_code) VALUES ($1, $2, 'p', $3, $4, $5::jsonb, 200)",
            [
                (r["request_id"], r["model_id"], user, r["timestamp"], json.dumps(r["metadata"]))
                for r in made
            ],
        )
    return user, made


def _shown(row: dict) -> str:
    return (row["metadata"].get("auto") or {}).get("requested") or row["model_id"]


async def _page(pool, user, *, offset=0, limit=50, model_id=None, key_prefix=None):
    keys = MagicMock()
    keys.list_account_keys = AsyncMock(return_value=[{"id": 7, "key_prefix": "hyi-ci000000"}])
    return await user_routes.get_recent_requests(
        limit=limit,
        offset=offset,
        model_id=model_id,
        key_prefix=key_prefix,
        current_user={"user_id": user},
        db_logger=SimpleNamespace(pool=pool),
        op_store=keys,
    )


async def test_a_long_history_is_listed_over_its_window(pool):
    pool, users = pool
    user, made = await _history(pool, users, HISTORY)

    first = await _page(pool, user, limit=20)
    last = await _page(pool, user, offset=WINDOW - 10, limit=50)

    assert (first.total, first.total_capped) == (WINDOW, True)
    assert [r.request_id for r in first.requests] == [r["request_id"] for r in made[:20]]
    assert [r.request_id for r in last.requests] == [
        r["request_id"] for r in made[WINDOW - 10 : WINDOW]
    ]
    with pytest.raises(HTTPException) as refused:
        await _page(pool, user, offset=WINDOW)
    assert refused.value.status_code == 400


async def test_a_short_history_is_counted_exactly(pool):
    pool, users = pool
    user, _ = await _history(pool, users, 30)

    page = await _page(pool, user)

    assert (page.total, page.total_capped) == (30, False)
    assert len(page.requests) == 30


@pytest.mark.parametrize(
    ("filters", "matches"),
    [
        ({"key_prefix": "hyi-ci000000"}, lambda r: r["metadata"].get("api_key_id") == "7"),
        ({"model_id": "M-A"}, lambda r: _shown(r) == "m-a"),
        ({"model_id": "auto-x"}, lambda r: _shown(r) == "auto-x"),
        # A substring of the model shown, never a pattern.
        ({"model_id": "m-"}, lambda r: _shown(r).startswith("m-")),
        ({"model_id": "m-%"}, lambda r: False),
        ({"model_id": "zz-no-such-model"}, lambda r: False),
    ],
)
async def test_a_filter_matches_among_the_window_only(pool, filters, matches):
    pool, users = pool
    user, made = await _history(pool, users, HISTORY)
    expected = [r["request_id"] for r in made[:WINDOW] if matches(r)]

    page = await _page(pool, user, limit=200, **filters)

    assert (page.total, page.total_capped) == (len(expected), True)
    assert [r.request_id for r in page.requests] == expected


async def test_its_statements_read_no_further_than_the_window(pool):
    """A filter nothing matches reads the window's rows, not the account's history.

    Through ``idx_api_logs_user_cost`` newest first, whatever the planner would
    otherwise estimate for the account (here, with no statistics, it read all of
    the account's rows to sort them).
    """
    pool, users = pool
    user, _ = await _history(pool, users, HISTORY)
    where_sql, params = user_routes._build_user_recent_requests_filters(
        user, "zz-no-such-model", None
    )
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        await conn.execute(user_routes._RECENT_REQUESTS_READ_SETTINGS)
        plan = await conn.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT request_id FROM "
            f"{user_routes._recent_rows_sql(user_routes._RECENT_ROW_COLUMNS)} "
            f"WHERE {where_sql} ORDER BY {user_routes._RECENT_ORDER_SQL} LIMIT 50",
            *params,
        )

    def scans(node):
        if "Index Name" in node or node.get("Node Type") == "Seq Scan":
            yield node
        for child in node.get("Plans", ()):
            yield from scans(child)

    (scan,) = scans(json.loads(plan)[0]["Plan"])
    assert scan.get("Index Name") == "idx_api_logs_user_cost"
    assert scan["Actual Rows"] <= WINDOW
