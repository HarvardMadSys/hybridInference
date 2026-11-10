"""Batch failures after inference and during exports retain their final outcome."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from serving.batch import delivery, dispatch
from serving.batch.attempts import observe_billing
from serving.servers.routers import anthropic_messages
from tests.unit.serving.test_batch_dispatch import _body, _owner, _setup, _Upstream


@pytest.mark.parametrize(
    ("target", "bad_value"),
    [
        ("messages", float("nan")),
        *[(target, "\ud800") for target in ("chat", "embeddings", "messages", "responses")],
    ],
)
async def test_unrenderable_answer_is_final_and_keeps_its_billing(monkeypatch, target, bad_value):
    app, services = _setup(_Upstream())
    calls = 0

    async def served(*_args: Any, **_kwargs: Any):
        nonlocal calls
        calls += 1
        observe_billing(
            cost_usd=0.25,
            usage={"input_tokens": 12, "output_tokens": 3},
            request_id="already-billed",
        )
        body = {"type": "message", "content": [{"type": "text", "text": bad_value}]}
        return anthropic_messages.MessagesResponse(body) if target == "messages" else body

    monkeypatch.setitem(dispatch._EXECUTORS, target, served)
    result = await dispatch.dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=_body(),
        batch_id="batch-billed",
        target=target,
        protocol_profile="2023-06-01",
    )

    assert calls == 1
    assert result.response is None
    assert result.status_code == 502
    assert result.error is not None
    assert result.retryable is False
    assert result.cost_status == "billed"
    assert result.cost_usd == 0.25
    assert result.request_id == "already-billed"
    assert (result.prompt_tokens, result.completion_tokens) == (12, 3)


async def test_request_export_fails_if_erasure_removes_its_next_page():
    store = AsyncMock()
    store.take_read_lease.return_value = "read-lease"
    store.request_page.side_effect = [
        ([{"id": 1, "custom_id": "one", "request": {"model": "m"}}], True),
        ([], False),
    ]
    store.batch_intact.return_value = False

    stream = delivery.stream_requests(store=store, batch_id="erased", url="/v1/embeddings")
    assert b'"custom_id":"one"' in await anext(stream)
    with pytest.raises(delivery.DownloadInterrupted):
        await anext(stream)
    store.drop_read_lease.assert_awaited_once_with("read-lease")


@pytest.mark.parametrize("kind", ["results", "requests"])
async def test_download_cannot_finish_after_losing_lease_during_its_final_send(monkeypatch, kind):
    keepers = []
    real_keeper = delivery.LeaseKeeper

    def remember_keeper(*args):
        keeper = real_keeper(*args)
        keepers.append(keeper)
        return keeper

    monkeypatch.setattr(delivery, "LeaseKeeper", remember_keeper)
    store = AsyncMock()
    store.take_read_lease.return_value = "read-lease"
    store.batch_intact.return_value = True
    if kind == "results":
        store.result_page.return_value = (
            [{"id": 1, "custom_id": "one", "status": "completed", "response": {"ok": True}}],
            False,
        )
        stream = delivery.stream_lines(store=store, batch_id="b", kind="all")
    else:
        store.request_page.return_value = (
            [{"id": 1, "custom_id": "one", "request": {"model": "m"}}],
            False,
        )
        stream = delivery.stream_requests(store=store, batch_id="b", url="/v1/embeddings")

    assert await anext(stream)
    # The client stalls in its final ASGI send long enough for renewal to fail.
    keepers[0].lost = True
    with pytest.raises(delivery.DownloadInterrupted):
        await anext(stream)
    store.drop_read_lease.assert_awaited_once_with("read-lease")
