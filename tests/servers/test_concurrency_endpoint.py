"""Integration tests verifying enforce_user_concurrency is wired into
the five inference routes (chat completions, /completion alias, legacy
completions, embeddings, anthropic messages).

Strategy: pre-acquire a concurrency slot directly on the limiter before
sending the request, then verify the route returns 429.  This is the
"sequential-equivalent fallback" documented in the task spec.

Why not in-flight pinning?
- The prescribed _SlowRouter.execute() method is never called by the real
  route handlers; they use chat_completion() / stream_chat_completion().
- The handler's synchronous checks (e.g. `model not in router_exec.routes`)
  fire before any awaitable, so an AttributeError on a stub router
  releases the concurrency slot before the second request can race.
- The anthropic messages router resolves the model via router_exec.routes,
  which a stub router cannot supply cleanly.

Pre-acquire is sufficient to verify wiring: the dependency runs before
the handler body, so a pre-filled slot → 429 if and only if the dep is
wired.  A 404/500 instead of 429 would indicate the dep is NOT wired.
"""

from __future__ import annotations

import pytest

from serving.servers.auth import verify_api_key
from serving.servers.concurrency import UserConcurrencyLimiter, static_limits_provider
from serving.servers.deps import get_user_concurrency_limiter

# ---------------------------- helpers ----------------------------------


def _stub_user(user_id: str, role: str, is_admin: bool = False) -> dict:
    return {
        "user_id": user_id,
        "user_name": f"name-{user_id}",
        "role": role,
        "authenticated": True,
        "quota_remaining_cost_usd": 100.0,
        "is_admin": is_admin,
    }


def _make_limiter() -> UserConcurrencyLimiter:
    return UserConcurrencyLimiter(
        static_limits_provider({"free": 1, "pro": 3, "internal": 10, "admin": 10})
    )


# --------------------------- chat completions --------------------------


@pytest.mark.asyncio
async def test_chat_completions_429_when_free_user_at_cap(auth_app, auth_client):
    """enforce_user_concurrency is wired into /v1/chat/completions."""
    user = _stub_user("user-chat-1", "free", is_admin=False)
    limiter = _make_limiter()

    # Pre-fill the slot — limiter is at capacity before the request arrives.
    granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
    assert granted

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter

    try:
        resp = await auth_client.post(
            "/v1/chat/completions",
            json={"model": "stub", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
        # The error middleware in the real app forwards exc.detail directly as
        # the response body when it already contains an "error" key, so the
        # top-level key is "error", not "detail".
        body = resp.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
        assert body["error"]["limit"] == 1
        assert body["error"]["role"] == "free"
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)


# ----------------------------- embeddings ------------------------------


@pytest.mark.asyncio
async def test_embeddings_429_when_free_user_at_cap(auth_app, auth_client):
    """enforce_user_concurrency is wired into /v1/embeddings."""
    user = _stub_user("user-emb-1", "free", is_admin=False)
    limiter = _make_limiter()

    granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
    assert granted

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter

    try:
        resp = await auth_client.post(
            "/v1/embeddings",
            json={"model": "stub", "input": "text"},
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)


# ------------------------- legacy /v1/completions ----------------------


@pytest.mark.asyncio
async def test_legacy_completions_429_when_free_user_at_cap(auth_app, auth_client):
    """enforce_user_concurrency is wired into /v1/completions."""
    user = _stub_user("user-legacy-1", "free", is_admin=False)
    limiter = _make_limiter()

    granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
    assert granted

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter

    try:
        resp = await auth_client.post(
            "/v1/completions",
            json={"model": "stub", "prompt": "hello"},
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)


# ----------------------- /completion (single alias) --------------------


@pytest.mark.asyncio
async def test_single_completion_429_when_free_user_at_cap(auth_app, auth_client):
    """enforce_user_concurrency is wired into /completion (Task 6 addition)."""
    user = _stub_user("user-single-1", "free", is_admin=False)
    limiter = _make_limiter()

    granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
    assert granted

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter

    try:
        resp = await auth_client.post(
            "/completion",
            json={"model": "stub", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)


# --------------------------- anthropic proxy ---------------------------


@pytest.mark.asyncio
async def test_anthropic_messages_429_when_free_user_at_cap(auth_app, auth_client):
    """enforce_user_concurrency is wired into /anthropic/v1/messages."""
    user = _stub_user("user-anth-1", "free", is_admin=False)
    limiter = _make_limiter()

    granted, _, _ = await limiter.try_acquire(user["user_id"], "free", False)
    assert granted

    auth_app.dependency_overrides[verify_api_key] = lambda: user
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter

    try:
        resp = await auth_client.post(
            "/anthropic/v1/messages",
            json={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer hyi-stub"},
        )
        assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)


# ----------------------------- isolation -------------------------------


@pytest.mark.asyncio
async def test_two_users_have_independent_budgets_on_real_route(auth_app, auth_client):
    """User A holding their slot doesn't block user B on /v1/chat/completions."""
    limiter = _make_limiter()
    user_a = _stub_user("user-A", "free", is_admin=False)
    user_b = _stub_user("user-B", "free", is_admin=False)

    # Pre-fill user-A's slot — user-B's slot is still free.
    granted, _, _ = await limiter.try_acquire(user_a["user_id"], "free", False)
    assert granted

    # Override to user-B for the actual request.
    auth_app.dependency_overrides[verify_api_key] = lambda: user_b
    auth_app.dependency_overrides[get_user_concurrency_limiter] = lambda: limiter

    try:
        resp = await auth_client.post(
            "/v1/chat/completions",
            json={"model": "stub", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer hyi-stub"},
        )
        # User-B should NOT get a 429 — their slot is free.
        # The request will likely 404 (unknown model "stub") which is fine.
        assert resp.status_code != 429, (
            f"user-B incorrectly got 429 while only user-A was at cap: {resp.text}"
        )
    finally:
        auth_app.dependency_overrides.pop(verify_api_key, None)
        auth_app.dependency_overrides.pop(get_user_concurrency_limiter, None)
