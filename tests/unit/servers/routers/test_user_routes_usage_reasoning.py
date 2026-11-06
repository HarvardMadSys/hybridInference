"""/user/usage says "unknown" for reasoning no request reported, never 0."""

from unittest.mock import AsyncMock

import pytest

from serving.servers.routers import user_routes

pytestmark = pytest.mark.asyncio

_BASE = {"cost_usd": 0.0, "requests": 3, "prompt_tokens": 30, "completion_tokens": 9}


async def _usage(monkeypatch, window: dict) -> dict:
    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = {"quota_daily_cost_usd": 5.0}
    op_store.get_user_cost_today.return_value = 0.0
    log_store = AsyncMock()
    log_store.get_user_usage_detail.return_value = {"today": window, "month": window}

    async def _cap(role, rt, *, is_admin=False):
        return 1

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _cap)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)
    resp = await user_routes.get_usage(
        period="today",
        current_user={"user_id": "u1", "role": "free"},
        op_store=op_store,
        log_store=log_store,
    )
    return resp.model_dump(mode="json")["usage"]


@pytest.mark.parametrize(
    ("reasoning", "known", "expected"),
    [
        (None, 0, (None, 0)),  # no request reported a count
        (0, 3, (0, 3)),  # every request reported 0
        (40, 2, (40, 2)),  # partial: 2 of 3 requests
        (90, 3, (90, 3)),  # exact
    ],
    ids=["unknown", "zero", "partial", "exact"],
)
async def test_reasoning_unknown_zero_partial_and_exact(monkeypatch, reasoning, known, expected):
    usage = await _usage(
        monkeypatch,
        {**_BASE, "reasoning_tokens": reasoning, "reasoning_tokens_known_requests": known},
    )

    assert (usage["reasoning_tokens"], usage["reasoning_tokens_known_requests"]) == expected
    assert usage["requests"] == 3


async def test_a_window_without_reasoning_fields_reads_as_unknown(monkeypatch):
    usage = await _usage(monkeypatch, dict(_BASE))

    assert usage["reasoning_tokens"] is None
    assert usage["reasoning_tokens_known_requests"] == 0


async def test_a_user_without_a_key_reports_reasoning_as_unknown(monkeypatch):
    op_store = AsyncMock()
    op_store.get_active_key_by_account.return_value = None

    async def _cap(role, rt, *, is_admin=False):
        return 1

    monkeypatch.setattr(user_routes, "get_user_concurrency_for_role", _cap)
    monkeypatch.setattr(user_routes, "get_runtime_settings_instance", lambda: None)
    resp = await user_routes.get_usage(
        period="today",
        current_user={"user_id": "u1", "role": "free"},
        op_store=op_store,
        log_store=None,
    )

    assert resp.usage.reasoning_tokens is None
    assert resp.usage.reasoning_tokens_known_requests == 0
