"""/v1/messages/count_tokens is bounded: its own per-user slots, and counting off the loop."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from serving.adapters.anthropic_translator import anthropic_request_to_openai
from serving.servers.concurrency import (
    COUNT_TOKENS_USER_CONCURRENCY_LIMIT,
    UserConcurrencyLimiter,
    static_limits_provider,
)
from serving.servers.routers import anthropic_messages
from serving.utils.tokens import estimate_prompt_tokens, estimate_text_tokens
from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

MODEL = "glm-4.7"
AUTH = {"Authorization": f"Bearer {ANTHROPIC_TEST_API_KEY}"}


def _body(text: str = "Count the tokens in this sentence please.") -> dict:
    return {
        "model": MODEL,
        "system": [{"type": "text", "text": "You are helpful."}],
        "messages": [{"role": "user", "content": text}],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file from the local filesystem.",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
    }


@pytest.mark.asyncio
async def test_the_count_is_unchanged(anthropic_test_client):
    body = _body()
    oai_messages, oai_params = anthropic_request_to_openai(body)
    expected = estimate_prompt_tokens(oai_messages) + estimate_text_tokens(
        json.dumps(oai_params["tools"])
    )

    response = await anthropic_test_client.post(
        "/v1/messages/count_tokens", json=body, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"input_tokens": expected}


@pytest.mark.asyncio
async def test_counting_runs_off_the_event_loop(anthropic_test_client, monkeypatch):
    loop_thread = threading.get_ident()
    threads: list[int] = []
    count = anthropic_messages.estimate_prompt_tokens_bounded

    def spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return count(*args, **kwargs)

    monkeypatch.setattr(anthropic_messages, "estimate_prompt_tokens_bounded", spy)
    response = await anthropic_test_client.post(
        "/v1/messages/count_tokens", json=_body(), headers=AUTH
    )

    assert response.status_code == 200
    assert threads and threads[0] != loop_thread


@pytest.mark.asyncio
async def test_count_tokens_takes_a_per_user_slot_of_its_own(
    anthropic_test_client, anthropic_app_services, monkeypatch
):
    limiter = UserConcurrencyLimiter(
        static_limits_provider({"free": 1, "pro": 1, "internal": 1, "admin": 1})
    )
    anthropic_app_services.user_concurrency_limiter = limiter
    release = threading.Event()
    started = threading.Semaphore(0)

    def held(body):
        started.release()
        release.wait(timeout=5)
        return 1

    monkeypatch.setattr(anthropic_messages, "_count_request_tokens", held)

    async def count():
        return await anthropic_test_client.post(
            "/v1/messages/count_tokens", json=_body(), headers=AUTH
        )

    in_flight = [asyncio.create_task(count()) for _ in range(COUNT_TOKENS_USER_CONCURRENCY_LIMIT)]
    deadline = asyncio.get_running_loop().time() + 5
    while limiter._slots.get("test-user:count_tokens") is None or (
        limiter._slots["test-user:count_tokens"].in_use < COUNT_TOKENS_USER_CONCURRENCY_LIMIT
    ):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)

    try:
        refused = await count()
        assert refused.status_code == 429
        assert refused.json()["type"] == "error"
        assert refused.json()["error"]["type"] == "rate_limit_error"
        # The inference budget (a limit of 1 here) was never touched.
        assert "test-user" not in limiter._slots
    finally:
        release.set()
    for task in in_flight:
        assert (await task).status_code == 200
    assert limiter._slots["test-user:count_tokens"].in_use == 0


@pytest.mark.asyncio
async def test_a_huge_prompt_is_counted_within_the_tokenizer_budget(
    anthropic_test_client, monkeypatch
):
    seen: dict[str, int] = {}
    count = anthropic_messages.estimate_prompt_tokens_bounded

    def spy(messages, *, max_chars, extra_texts=()):
        seen["max_chars"] = max_chars
        return count(messages, max_chars=max_chars, extra_texts=extra_texts)

    monkeypatch.setattr(anthropic_messages, "estimate_prompt_tokens_bounded", spy)
    response = await anthropic_test_client.post(
        "/v1/messages/count_tokens", json=_body("word " * 600_000), headers=AUTH
    )

    assert response.status_code == 200
    assert seen["max_chars"] == anthropic_messages.COUNT_TOKENS_MAX_CHARS
    # Three million characters, sampled: still close to their ~600k tokens.
    assert 550_000 < response.json()["input_tokens"] < 650_000


@pytest.mark.asyncio
async def test_a_lower_per_user_override_caps_count_tokens_too():
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from serving.servers.concurrency import enforce_count_tokens_concurrency

    limiter = UserConcurrencyLimiter(static_limits_provider({"free": 3}))
    user = {"user_id": "u1", "role": "free", "max_concurrent_requests": 1}
    request = MagicMock()
    request.url.path = "/v1/messages/count_tokens"

    first = enforce_count_tokens_concurrency(request, user, limiter)
    await first.__anext__()
    second = enforce_count_tokens_concurrency(request, user, limiter)
    with pytest.raises(HTTPException) as refused:
        await second.__anext__()
    assert refused.value.status_code == 429
    assert refused.value.detail["error"]["limit"] == 1
    await first.aclose()
    assert limiter._slots["u1:count_tokens"].in_use == 0
