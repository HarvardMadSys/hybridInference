"""/v1/messages logs and bills an upstream's unreadable usage as 0, not a failure."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers import anthropic_messages as amod

pytestmark = pytest.mark.asyncio


async def test_an_unreadable_usage_is_logged_as_zero_not_raised(caplog):
    log_store = MagicMock()
    log_store.log_request = AsyncMock()

    with caplog.at_level("WARNING", logger="serving.utils.token_utils"):
        amod._schedule_log_store_task(
            log_store,
            request_id="req-usage",
            model_id="claude-x",
            provider="anthropic",
            usage={
                "input_tokens": "lots",
                "output_tokens": "9",
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": -2,
            },
            latency_ms=5,
            status_code=200,
            pricing={"prompt": "0", "completion": "0"},
            metadata={},
            params={},
        )
        for _ in range(20):
            if log_store.log_request.await_count:
                break
            await asyncio.sleep(0.01)

    logged = log_store.log_request.await_args.kwargs["usage"]
    assert (logged["prompt_tokens"], logged["completion_tokens"]) == (0, 9)
    assert logged["total_tokens"] == 9
    assert "Unreadable upstream input_tokens 'lots'" in caplog.text
