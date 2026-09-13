"""Unit tests for ``GET /admin/upstream-concurrency``.

The endpoint is a window onto live in-process state, so the tests drive the real
limiter rather than a stub: a bucket only exists once traffic has created one,
and the numbers the view reports have to be the ones the AIMD controller is
actually enforcing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.adapters.upstream_limiter import (
    UpstreamConcurrencyLimiter,
    key_fingerprint,
    reset_upstream_limiter,
)
from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers.admin.upstream_concurrency import router

PROVIDER = "zai"
KEY_A = "sk-aaa-secret-value"
KEY_B = "sk-bbb-secret-value"
REMOTE = "https://api.z.ai/v4"
LOCAL = "http://localhost:12003/v1"


@pytest.fixture
def limiter():
    """Install a limiter with small, explicit tunables as the process singleton."""
    instance = UpstreamConcurrencyLimiter(
        initial_limit=2,
        max_limit=4,
        probe_success_interval=3,
        acquire_timeout=0.05,
    )
    reset_upstream_limiter(instance)
    yield instance
    reset_upstream_limiter()


@pytest.fixture
def app():
    application = FastAPI()
    # ``verify_admin_access`` resolves the operational store off app state, so
    # the container has to exist even for the unauthenticated case.
    application.state.services = AppServices(router=MagicMock())
    application.include_router(router)
    return application


async def _get(app, *, as_admin: bool = True):
    if as_admin:
        app.dependency_overrides[verify_admin_access] = lambda: "admin-id"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/admin/upstream-concurrency")


# ----------------------------------------------------------------------- auth


@pytest.mark.asyncio
async def test_requires_admin(app, limiter) -> None:
    response = await _get(app, as_admin=False)
    assert response.status_code == 401


# ----------------------------------------------------------------------- shape


@pytest.mark.asyncio
async def test_reports_the_effective_config(app, limiter) -> None:
    response = await _get(app)

    assert response.status_code == 200
    assert response.json()["config"] == {
        "enabled": True,
        "initial_limit": 2,
        "max_limit": 4,
        "probe_success_interval": 3,
        "acquire_timeout_sec": 0.05,
    }


@pytest.mark.asyncio
async def test_no_traffic_means_no_buckets(app, limiter) -> None:
    """An idle gateway has created no buckets; the view says so rather than erroring."""
    response = await _get(app)

    assert response.status_code == 200
    assert response.json()["buckets"] == []


@pytest.mark.asyncio
async def test_bucket_carries_every_field_the_dashboard_renders(app, limiter) -> None:
    slot = await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)

    response = await _get(app)

    assert response.status_code == 200
    assert response.json()["buckets"] == [
        {
            "provider": PROVIDER,
            "key_fingerprint": key_fingerprint(KEY_A),
            "limit": 2,
            "in_flight": 1,
            "waiting": 0,
            "successes_since_probe": 0,
            "probing": False,
        }
    ]
    slot.release(status_code=200)


# ------------------------------------------------------------------ liveness


@pytest.mark.asyncio
async def test_a_429_is_visible_as_a_lowered_limit(app, limiter) -> None:
    (await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)).release(status_code=429)

    bucket = (await _get(app)).json()["buckets"][0]

    assert bucket["limit"] == 1
    assert bucket["successes_since_probe"] == 0


@pytest.mark.asyncio
async def test_successes_and_the_probe_they_earn_are_visible(app, limiter) -> None:
    for _ in range(2):
        (await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)).release(status_code=200)

    # Two of the three successes the probe interval asks for.
    partway = (await _get(app)).json()["buckets"][0]
    assert partway["limit"] == 2
    assert partway["successes_since_probe"] == 2
    assert partway["probing"] is False

    (await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)).release(status_code=200)

    probed = (await _get(app)).json()["buckets"][0]
    assert probed["limit"] == 3
    assert probed["successes_since_probe"] == 0
    assert probed["probing"] is True


@pytest.mark.asyncio
async def test_queued_waiters_are_counted(app, limiter) -> None:
    """A saturated bucket shows the queue depth behind it, not just the limit."""
    held = [await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE) for _ in range(2)]
    queued = asyncio.ensure_future(limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE))
    # Let the acquire coroutine reach the waiter queue before snapshotting.
    await asyncio.sleep(0)

    bucket = (await _get(app)).json()["buckets"][0]
    assert bucket["in_flight"] == 2
    assert bucket["waiting"] == 1

    # Releasing a held slot hands it to the queued waiter, which unblocks it
    # well inside the 50ms acquire timeout.
    held[0].release(status_code=200)
    (await queued).release(status_code=200)
    held[1].release(status_code=200)


@pytest.mark.asyncio
async def test_each_provider_key_pair_is_its_own_row(app, limiter) -> None:
    await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)
    await limiter.acquire(PROVIDER, KEY_B, base_url=REMOTE)
    await limiter.acquire("chutes", KEY_A, base_url=REMOTE)

    buckets = (await _get(app)).json()["buckets"]

    # Sorted by (provider, fingerprint) so a polling table keeps its row order.
    assert [b["provider"] for b in buckets] == ["chutes", PROVIDER, PROVIDER]
    assert buckets[1:] == sorted(buckets[1:], key=lambda b: b["key_fingerprint"])
    assert len({b["key_fingerprint"] for b in buckets}) == 2


@pytest.mark.asyncio
async def test_local_endpoints_never_appear(app, limiter) -> None:
    """Local inference servers are exempt from the limiter, so they have no bucket."""
    await limiter.acquire(PROVIDER, KEY_A, base_url=LOCAL)

    assert (await _get(app)).json()["buckets"] == []


# ------------------------------------------------------------------- secrecy


@pytest.mark.asyncio
async def test_the_raw_key_never_leaves_the_process(app, limiter) -> None:
    await limiter.acquire(PROVIDER, KEY_A, base_url=REMOTE)
    await limiter.acquire(PROVIDER, KEY_B, base_url=REMOTE)

    response = await _get(app)

    body = response.text
    for key in (KEY_A, KEY_B):
        assert key not in body
        # Not even a prefix an attacker could pivot from.
        assert key[:8] not in body
    assert key_fingerprint(KEY_A) in body
