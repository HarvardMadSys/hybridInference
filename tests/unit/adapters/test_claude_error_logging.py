"""An upstream error on the Vertex Claude adapter is logged without its API key.

The adapter logged the failed request's ``RequestInfo``, whose headers include
the ``api-key`` it authenticates with, at ERROR on every upstream error.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from serving.adapters.base import ModelConfig
from serving.adapters.claude import ClaudeAdapter

pytestmark = pytest.mark.unit

API_KEY = "vertex-secret-key-123"


@pytest.mark.asyncio
async def test_an_upstream_error_is_logged_without_the_api_key(caplog):
    adapter = ClaudeAdapter(
        ModelConfig(
            id="claude-x",
            name="Claude X",
            provider="claude",
            base_url="https://vertex.example.com",
            api_key=API_KEY,
        )
    )
    adapter.http = MagicMock()
    url = URL("https://vertex.example.com/v1:streamRawPredict")
    request_info = aiohttp.RequestInfo(
        url, "POST", CIMultiDictProxy(CIMultiDict({"api-key": API_KEY})), url
    )

    async def failing_stream(*_args, **_kwargs):
        error = aiohttp.ClientResponseError(request_info, (), status=503, message="overloaded")
        error.error_body = '{"error": "overloaded"}'  # type: ignore[attr-defined]
        raise error
        yield  # pragma: no cover - makes this an async generator

    adapter.http.stream_post = failing_stream

    with caplog.at_level(logging.DEBUG), pytest.raises(aiohttp.ClientResponseError):
        async for _ in adapter.stream_chat_completion([{"role": "user", "content": "hi"}]):
            pass

    assert "overloaded" in caplog.text  # the error itself is still logged
    assert API_KEY not in caplog.text
