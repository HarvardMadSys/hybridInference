"""Tests for the rejection_log helper."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.observability.rejection_log import (
    INFERENCE_PATH_PREFIXES,
    bounded_enrichment,
    capture_rejected_prompt,
    extract_prompt_from_body,
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
    """The path-filter list covers every inference route.

    Both Anthropic Messages aliases must be present: the handler is
    registered at ``/v1/messages`` and ``/anthropic/v1/messages``, so
    omitting either drops rejections on that route.
    """
    assert "/v1/chat/completions" in INFERENCE_PATH_PREFIXES
    assert "/v1/completions" in INFERENCE_PATH_PREFIXES
    assert "/v1/embeddings" in INFERENCE_PATH_PREFIXES
    assert "/completion" in INFERENCE_PATH_PREFIXES
    assert "/v1/messages" in INFERENCE_PATH_PREFIXES
    assert "/anthropic/v1/messages" in INFERENCE_PATH_PREFIXES


@pytest.mark.asyncio
async def test_root_anthropic_messages_route_is_logged(fake_log_store, runtime_on):
    """Rejections on the root ``/v1/messages`` alias are persisted too."""
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/messages"),
        status_code=404,
        error_code="model_not_found",
        reason="Model 'x' not found",
        user={"user_id": "u1", "role": "free"},
        model_id="x",
        prompt=[{"role": "user", "content": "hi"}],
    )
    fake_log_store.log_request.assert_awaited_once()
    kwargs = fake_log_store.log_request.await_args.kwargs
    assert kwargs["prompt"] == [{"role": "user", "content": "hi"}]


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
async def test_prompt_is_passed_through(fake_log_store, runtime_on):
    """The prompt the caller supplies reaches log_request, so rejected
    requests log the prompt consistently with the success path (the store
    still gates persistence on store_full_content).
    """
    messages = [{"role": "user", "content": "hi"}]
    await log_rejection(
        log_store=fake_log_store,
        runtime_settings=runtime_on,
        request=_fake_request("/v1/chat/completions"),
        status_code=429,
        error_code="concurrency_limit_exceeded",
        reason="limit=1 role=free",
        user={"user_id": "u1", "role": "free"},
        model_id="gpt-4",
        prompt=messages,
    )
    kwargs = fake_log_store.log_request.await_args.kwargs
    assert kwargs["prompt"] == messages


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"messages": [{"role": "user", "content": "hi"}]}, [{"role": "user", "content": "hi"}]),
        ({"input": "embed me"}, "embed me"),
        ({"prompt": "legacy completion"}, "legacy completion"),
        ({"model": "gpt-4"}, ""),
        ({"messages": []}, ""),
        ("not a dict", ""),
        (None, ""),
    ],
)
def test_extract_prompt_from_body(body, expected):
    assert extract_prompt_from_body(body) == expected


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


def _real_request(
    body: bytes,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    chunks: list[dict] | None = None,
):
    """A real Starlette Request whose body arrives over the ASGI receive channel.

    Used instead of a mock because what these tests exercise *is* the body
    read: a mocked ``request.body()`` would not tell us whether the bounds hold.
    """
    from starlette.requests import Request

    if headers is None:
        headers = [(b"content-length", str(len(body)).encode())]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": headers,
        "client": ("203.0.113.9", 40000),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    queue = list(chunks or [{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)  # stalls, like a client that never finishes

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_capture_reads_the_body_of_a_request_rejected_pre_handler():
    """The prompt is recovered even though no handler ever parsed the body."""
    body = json.dumps({"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}).encode()
    got = await capture_rejected_prompt(_real_request(body))
    assert got == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_capture_prefers_an_already_parsed_body():
    """A body a previous dependency cached is reused, not re-read.

    This is the live path on typed-body routes: FastAPI parses a declared body
    model *before* solving dependencies, so ``/v1/embeddings`` reaches a gate
    rejection with the body already on ``request._json``.
    """
    request = _real_request(b"")
    request._json = {"messages": [{"role": "user", "content": "cached"}]}
    assert await capture_rejected_prompt(request) == [{"role": "user", "content": "cached"}]


@pytest.mark.asyncio
async def test_capture_applies_the_size_bound_to_a_cached_body_too():
    """An oversized payload is declined whether or not it was already parsed.

    Checked from the header, never by re-serializing the parsed object: measuring
    it that way would allocate the whole payload again, costing more than the
    bound saves.
    """
    request = _real_request(b"", headers=[(b"content-length", b"9999999")])
    request._json = {"messages": [{"role": "user", "content": "huge"}]}
    assert await capture_rejected_prompt(request, max_body_bytes=1024) == ""


@pytest.mark.asyncio
async def test_capture_uses_a_cached_body_with_no_declared_length_when_raw_bytes_fit():
    """No Content-Length is fine when the raw bytes are there and within the cap.

    The chunked/unknown-length skip exists to bound a read not yet done; an
    already-parsed body is bounded by its actual size instead, so declining it
    would forfeit a free prompt for no gain.
    """
    raw = json.dumps({"messages": [{"role": "user", "content": "chunked but parsed"}]}).encode()
    request = _real_request(b"", headers=[(b"transfer-encoding", b"chunked")])
    request._body = raw
    request._json = json.loads(raw)
    assert await capture_rejected_prompt(request) == [
        {"role": "user", "content": "chunked but parsed"}
    ]


@pytest.mark.asyncio
async def test_capture_bounds_a_cached_body_by_its_raw_bytes():
    """An oversized parsed body is declined even with no Content-Length at all.

    This is the case a header check cannot bound — chunked, or HTTP/2, where
    FastAPI has already populated ``_json``. The prompt would be serialized into
    ``api_logs``, and a blocked caller is subject to no quota, so leaving it
    unbounded is a way for a refused source to grow the database.
    """
    raw = json.dumps({"messages": [{"role": "user", "content": "x" * 5000}]}).encode()
    request = _real_request(b"", headers=[(b"transfer-encoding", b"chunked")])
    request._body = raw
    request._json = json.loads(raw)
    assert await capture_rejected_prompt(request, max_body_bytes=1024) == ""


@pytest.mark.asyncio
async def test_capture_declines_a_cached_body_that_cannot_be_measured():
    """With neither raw bytes nor a declared length, there is no bound to apply."""
    request = _real_request(b"", headers=[(b"transfer-encoding", b"chunked")])
    request._json = {"messages": [{"role": "user", "content": "unmeasurable"}]}
    assert await capture_rejected_prompt(request) == ""


@pytest.mark.asyncio
async def test_capture_skips_a_body_with_no_declared_length():
    """Chunked uploads are skipped: their length is unknown until they finish.

    Reading one to completion would let a rejected client hold the read open for
    as long as it likes — the slowloris foothold these bounds exist to close.
    """
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    request = _real_request(body, headers=[(b"transfer-encoding", b"chunked")])
    assert await capture_rejected_prompt(request) == ""


@pytest.mark.asyncio
async def test_capture_skips_when_transfer_encoding_is_present():
    """``Transfer-Encoding`` disqualifies a body even if a length is declared.

    RFC 9112 says ``Content-Length`` must be ignored when ``Transfer-Encoding``
    is present, so a length alongside it is not a bound worth trusting — and
    trusting it is how a "bounded" read becomes unbounded.
    """
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    request = _real_request(
        body,
        headers=[
            (b"transfer-encoding", b"chunked"),
            (b"content-length", str(len(body)).encode()),
        ],
    )
    assert await capture_rejected_prompt(request) == ""


@pytest.mark.asyncio
async def test_capture_gives_up_instead_of_queueing_when_all_slots_are_busy():
    """Past the concurrency cap, capture returns immediately without reading.

    This is what keeps a blocked flood cheap to shed: a source that declares a
    small body and then stalls can tie up at most the cap, and every request
    beyond it is refused as fast as it was before enrichment existed.
    """
    import serving.observability.rejection_log as mod

    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    exhausted = asyncio.Semaphore(1)
    await exhausted.acquire()  # value now 0 -> locked()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_enrichment_slots", exhausted)
    try:
        assert await capture_rejected_prompt(_real_request(body)) == ""
    finally:
        monkeypatch.undo()

    # With a free slot the same request is captured, so the skip above was the
    # cap talking and not a broken read path.
    assert await capture_rejected_prompt(_real_request(body)) == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_capture_releases_its_slot_after_a_failed_read():
    """A timed-out read must not leak the slot it held."""
    import serving.observability.rejection_log as mod

    stalled = _real_request(
        b"",
        headers=[(b"content-length", b"200")],
        chunks=[{"type": "http.request", "body": b'{"messages":', "more_body": True}],
    )
    assert await capture_rejected_prompt(stalled, timeout_sec=0.01) == ""
    assert not mod._enrichment_slots.locked()
    body = json.dumps({"messages": [{"role": "user", "content": "after"}]}).encode()
    assert await capture_rejected_prompt(_real_request(body)) == [
        {"role": "user", "content": "after"}
    ]


@pytest.mark.asyncio
async def test_bounded_enrichment_runs_work_and_returns_its_value():
    async def work():
        return {"user_id": "u1"}

    assert await bounded_enrichment(work()) == {"user_id": "u1"}


@pytest.mark.asyncio
async def test_bounded_enrichment_declines_without_running_when_budget_is_spent():
    """No slot means the work never runs — the whole point of the budget.

    A blocked source spraying random tokens must not reach the database once the
    budget is spent, since unsuccessful auth lookups are not cached and would
    otherwise hit the shared pool on every single request.
    """
    import serving.observability.rejection_log as mod

    ran = False

    async def work():
        nonlocal ran
        ran = True
        return "should not happen"

    exhausted = asyncio.Semaphore(1)
    await exhausted.acquire()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_enrichment_slots", exhausted)
    try:
        assert await bounded_enrichment(work(), default="gave-up") == "gave-up"
    finally:
        monkeypatch.undo()
    assert ran is False


@pytest.mark.asyncio
async def test_bounded_enrichment_times_out_slow_work():
    """A slow lookup yields the default rather than delaying the rejection."""

    async def slow():
        await asyncio.sleep(5)
        return "too late"

    assert await bounded_enrichment(slow(), default=None, timeout_sec=0.01) is None


@pytest.mark.asyncio
async def test_bounded_enrichment_swallows_failures_and_frees_the_slot():
    import serving.observability.rejection_log as mod

    async def boom():
        raise RuntimeError("db down")

    assert await bounded_enrichment(boom(), default="fallback") == "fallback"
    assert not mod._enrichment_slots.locked()


@pytest.mark.asyncio
async def test_capture_skips_an_oversized_body():
    """A body larger than the cap is skipped rather than buffered."""
    body = json.dumps({"messages": [{"role": "user", "content": "x" * 5000}]}).encode()
    got = await capture_rejected_prompt(_real_request(body), max_body_bytes=1024)
    assert got == ""


@pytest.mark.asyncio
async def test_capture_gives_up_on_a_stalled_body():
    """A trickled body yields no prompt instead of pinning the task."""
    request = _real_request(
        b"",
        headers=[(b"content-length", b"200")],
        chunks=[{"type": "http.request", "body": b'{"messages":', "more_body": True}],
    )
    assert await capture_rejected_prompt(request, timeout_sec=0.01) == ""


@pytest.mark.asyncio
async def test_capture_tolerates_a_non_json_body():
    assert await capture_rejected_prompt(_real_request(b"not json at all")) == ""


@pytest.mark.asyncio
async def test_capture_skips_an_empty_body():
    request = _real_request(b"", headers=[(b"content-length", b"0")])
    assert await capture_rejected_prompt(request) == ""


@pytest.mark.asyncio
async def test_capture_skips_a_malformed_content_length():
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    request = _real_request(body, headers=[(b"content-length", b"not-a-number")])
    assert await capture_rejected_prompt(request) == ""


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
    md = fake_log_store.log_request.await_args.kwargs["metadata"]
    assert md["synthetic_probe"] is True
    assert md["request_type"] == "embedding"
