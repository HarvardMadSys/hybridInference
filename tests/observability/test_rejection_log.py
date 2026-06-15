"""Tests for the rejection_log helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.observability.rejection_log import (
    INFERENCE_PATH_PREFIXES,
    log_rejection,
)


def _fake_request(
    path: str = "/v1/chat/completions", headers: dict[str, str] | None = None
) -> MagicMock:
    """Minimal Request-shaped mock with the URL path the helper reads.

    ``request.app.state.services`` is pinned to ``None`` so the helper's
    auto-resolution falls through; tests that want services-backed
    behaviour pass ``log_store`` / ``runtime_settings`` as explicit kwargs.
    """
    req = MagicMock()
    req.url.path = path
    # Simulate the headers FastAPI requests expose; remote-IP helper reads them.
    req.headers = {"x-forwarded-for": "203.0.113.5", **(headers or {})}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    # Pin services so MagicMock's auto-attribute creation doesn't shadow the
    # explicit-kwargs path.
    req.app.state.services = None
    return req


def _runtime_with(values: dict[str, bool]) -> MagicMock:
    """RuntimeSettings mock whose get_bool resolves per-key from *values*."""
    rs = MagicMock()
    rs.get_bool = AsyncMock(side_effect=lambda key: values.get(key, False))
    return rs


@pytest.fixture
def fake_log_store():
    store = MagicMock()
    store.log_request = AsyncMock(return_value=None)
    return store


@pytest.fixture
def runtime_on():
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=True)
    return rs


@pytest.fixture
def runtime_off():
    rs = MagicMock()
    rs.get_bool = AsyncMock(return_value=False)
    return rs


@pytest.mark.asyncio
async def test_inference_prefixes_set():
    """The path-filter list covers all five inference routes."""
    assert "/v1/chat/completions" in INFERENCE_PATH_PREFIXES
    assert "/v1/completions" in INFERENCE_PATH_PREFIXES
    assert "/v1/embeddings" in INFERENCE_PATH_PREFIXES
    assert "/completion" in INFERENCE_PATH_PREFIXES
    assert "/anthropic/v1/messages" in INFERENCE_PATH_PREFIXES


@pytest.mark.asyncio
async def test_toggle_off_does_not_log(fake_log_store, runtime_off):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_off,
        request=_fake_request(),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
    )
    fake_log_store.log_request.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_on_writes_row(fake_log_store, runtime_on):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/chat/completions"),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
        model_id="gpt-4",
    )
    fake_log_store.log_request.assert_awaited_once()
    kwargs = fake_log_store.log_request.await_args.kwargs
    assert kwargs["model_id"] == "gpt-4"
    assert kwargs["provider"] == ""
    assert kwargs["prompt"] == ""
    assert kwargs["response"] is None
    assert kwargs["usage"] is None
    assert kwargs["latency_ms"] == 0
    assert kwargs["status_code"] == 429
    assert kwargs["error"] == "concurrency_limit_exceeded"
    md = kwargs["metadata"]
    assert md["rejection"] is True
    assert md["reason"] == "limit=1 role=free"
    assert md["route"] == "/v1/chat/completions"
    assert md["role"] == "free"
    assert md["user_id"] == "u1"
    # Chat rejections are not tagged as embeddings.
    assert "request_type" not in md


@pytest.mark.asyncio
async def test_embedding_rejection_is_tagged(fake_log_store, runtime_on):
    """Embedding rejections carry request_type so they match success-path
    tagging and stay out of chat-performance aggregates.
    """
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/embeddings"),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
        model_id="bge-m3",
    )
    fake_log_store.log_request.assert_awaited_once()
    md = fake_log_store.log_request.await_args.kwargs["metadata"]
    assert md["request_type"] == "embedding"


@pytest.mark.asyncio
async def test_log_store_none_is_noop(runtime_on):
    # Should not raise even though log_store is None.
    await log_rejection(
        log_store=None,
        runtime_settings=runtime_on,
        request=_fake_request(),
        status_code=429,
        error_code="quota_exceeded",
        reason="quota=1.0 spent=1.5",
        user={"user_id": "u1", "role": "free"},
    )
    runtime_on.get_bool.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_settings_none_is_noop(fake_log_store):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=None,
        request=_fake_request(),
        status_code=429,
        error_code="quota_exceeded",
        reason="x",
        user={"user_id": "u1"},
    )
    fake_log_store.log_request.assert_not_called()


@pytest.mark.asyncio
async def test_non_inference_path_is_noop(fake_log_store, runtime_on):
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/admin/settings"),
        status_code=401,
        error_code="auth_invalid",
        reason="missing key",
        user=None,
    )
    fake_log_store.log_request.assert_not_called()
    # We didn't even need to read the toggle.
    runtime_on.get_bool.assert_not_called()


@pytest.mark.asyncio
async def test_unauthenticated_user_is_not_persisted(fake_log_store, runtime_on):
    """401 auth challenges are not written to api_logs."""
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/chat/completions"),
        status_code=401,
        error_code="auth_missing",
        reason="no header",
        user=None,
    )
    fake_log_store.log_request.assert_not_called()
    runtime_on.get_bool.assert_not_called()


@pytest.mark.asyncio
async def test_log_store_failure_is_swallowed(fake_log_store, runtime_on, caplog):
    fake_log_store.log_request = AsyncMock(side_effect=RuntimeError("db down"))
    # Helper must not propagate the exception.
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request(),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="x",
        user={"user_id": "u1", "role": "free"},
    )
    # Spot-check: an error-level log was emitted.
    assert any("rejection_log_failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_synthetic_probe_rejection_suppressed_when_probe_logging_off(fake_log_store):
    """A probe rejected at the gate is not persisted while log_synthetic_probes
    is off, even though log_rejected_requests is on.
    """
    rs = _runtime_with({"log_rejected_requests": True, "log_synthetic_probes": False})
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=rs,
        request=_fake_request("/v1/embeddings", headers={"x-probe": "synthetic"}),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
    )
    fake_log_store.log_request.assert_not_called()


@pytest.mark.asyncio
async def test_synthetic_probe_rejection_logged_when_probe_logging_on(fake_log_store):
    """When log_synthetic_probes is on, a rejected probe is still persisted."""
    rs = _runtime_with({"log_rejected_requests": True, "log_synthetic_probes": True})
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=rs,
        request=_fake_request("/v1/embeddings", headers={"x-probe": "synthetic"}),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
    )
    fake_log_store.log_request.assert_awaited_once()
