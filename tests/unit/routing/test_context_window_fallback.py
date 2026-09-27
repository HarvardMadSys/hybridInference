"""Fallback after an upstream refuses a prompt as too long for its context window.

A 400 usually carries the answering server's own verdict, so FixedRouter tries
the next route after one. A context-window refusal is the exception: a route
whose configured window is no wider refuses the same prompt, but only after
receiving and tokenizing all of it. Seen in production with one client whose
prompt had grown to 605,732 tokens against a 262,144-token model: each request
was sent whole to every route in turn and took ~9 s to fail, and the client
re-sent each one twice more.

The rule under test is narrow in both directions. Only a window refusal ends the
walk, and only for routes whose configured window is no wider than the widest
one that refused. Every other 400, and every wider route, is tried as before.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from routing.routers import FixedRouter, exceeds_context_window
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.adapters.openai_compat import UpstreamStreamError
from serving.http import _upstream_status_error

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_MESSAGES = [{"role": "user", "content": "hi"}]

# The body SGLang returned for the production prompt, verbatim.
_SGLANG_REFUSAL_BODY = (
    '{"object":"error","message":"The input (605732 tokens) is longer than the '
    'model\'s context length (262144 tokens).","type":"BadRequestError",'
    '"param":null,"code":400}'
)

# A 400 that says nothing about length. Another server may well accept the same
# request, so it must keep falling back.
_TOOL_SCHEMA_400 = "Tool 0 function has invalid 'parameters' schema"

_CONTENT_CHUNK = 'data: {"choices": [{"index": 0, "delta": {"content": "ok"}}]}\n\n'


def _cfg(provider: str, context_length: int) -> ModelConfig:
    return ModelConfig(
        id="m",
        name="m",
        provider=provider,
        base_url="http://test",
        context_length=context_length,
        max_output_length=4096,
    )


class _UpstreamStatusError(RuntimeError):
    """An upstream rejection carrying its HTTP status, as adapters raise them."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class _TransportError(RuntimeError):
    """A connection failure to a dead endpoint: no HTTP status anywhere on it."""


class _Adapter(BaseAdapter):
    """Adapter that fails with ``error`` when given one and answers otherwise."""

    def __init__(self, config: ModelConfig, error: BaseException | None = None) -> None:
        super().__init__(config)
        self.error = error
        self.calls = 0

    async def chat_completion(self, messages: list[dict[str, Any]], **params) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params
    ) -> AsyncGenerator[str, None]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        yield _CONTENT_CHUNK


def _refusal(window: int = 262144) -> _UpstreamStatusError:
    return _UpstreamStatusError(
        400,
        f"The input (605732 tokens) is longer than the model's context length ({window} tokens).",
    )


def _router(primary: BaseAdapter, *backups: BaseAdapter) -> FixedRouter:
    """Route ``m`` over every adapter, with selection pinned to ``primary``.

    Backups are tried in the order given: with no weight-override resolver
    installed, the fallback loop walks the route in registration order.
    """
    router = FixedRouter()
    router.register_route("m", [(primary, 0.9), *((backup, 0.1) for backup in backups)])
    router._select_adapter = lambda model_id, **kw: primary  # type: ignore[assignment]
    return router


async def _run(router: FixedRouter, *, stream: bool) -> Any:
    if stream:
        return [chunk async for chunk in router.stream_chat_completion("m", messages=_MESSAGES)]
    return await router.chat_completion("m", messages=_MESSAGES)


def _upstream_response(status: int) -> SimpleNamespace:
    """An upstream error response as ``_upstream_status_error`` reads it.

    ``request_info`` carries a ``real_url`` because whether ``serving.http`` is
    bound to the real aiohttp or to the unit-tier stub depends on import order
    across the suite, and the real ``ClientResponseError.__str__`` reads it.
    """
    return SimpleNamespace(
        status=status,
        reason="Bad Request",
        request_info=SimpleNamespace(real_url="http://127.0.0.1:8001/v1/chat/completions"),
        history=(),
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Recognising a refusal
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        # SGLang, prompt alone over the window
        "The input (605732 tokens) is longer than the model's context length (262144 tokens).",
        # SGLang, prompt plus max_tokens over the window
        "Requested token count exceeds the model's maximum context length of 262144 tokens. "
        "You requested a total of 270000 tokens: 261808 tokens from the input messages and "
        "8192 tokens for the completion.",
        # vLLM
        "This model's maximum context length is 131072 tokens. However, you requested "
        "140000 tokens (139000 in the messages, 1000 in the completion).",
        "The decoder prompt (length 140000) is longer than the maximum model length of 131072.",
        # OpenAI, by code alone
        '{"error": {"message": "Request too large.", "code": "context_length_exceeded"}}',
        # Anthropic
        "prompt is too long: 208000 tokens > 200000 maximum",
        # Gemini
        "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576).",
        # Moonshot
        "Invalid request: Your request exceeded model token limit: 262144",
        # xAI
        "This model's maximum prompt length is 131072 but the request contains 140000 tokens.",
    ],
)
def test_recognises_each_upstreams_wording(message):
    assert exceeds_context_window(_UpstreamStatusError(400, message))


@pytest.mark.unit
def test_the_parameter_name_alone_is_not_a_refusal():
    """A 400 naming the ``context_length`` parameter is about the request's fields, not its size."""
    assert not exceeds_context_window(
        _UpstreamStatusError(400, "Unrecognized request argument supplied: context_length")
    )


@pytest.mark.unit
def test_reads_the_body_of_a_failed_upstream_response():
    """``serving.http`` keeps the body on ``error_body``; the message is only the HTTP reason."""
    exc = _upstream_status_error(_upstream_response(400), _SGLANG_REFUSAL_BODY)

    assert "context length" not in str(exc)
    assert exceeds_context_window(exc)


@pytest.mark.unit
def test_reads_an_in_band_stream_error_frame():
    """A 200 stream that ends in an error frame carries the upstream's text as its message."""
    exc = UpstreamStreamError(
        "The input (605730 tokens) is longer than the model's context length (262144 tokens).",
        400,
    )

    assert exceeds_context_window(exc)


@pytest.mark.unit
def test_other_400s_are_not_refusals():
    assert not exceeds_context_window(_UpstreamStatusError(400, _TOOL_SCHEMA_400))


@pytest.mark.unit
@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_window_wording_under_a_status_that_does_not_describe_the_request(status):
    """The status gate comes first: an overloaded or failing route is never a refusal."""
    assert not exceeds_context_window(_UpstreamStatusError(status, _SGLANG_REFUSAL_BODY))


@pytest.mark.unit
def test_statusless_failure_is_not_a_refusal():
    assert not exceeds_context_window(_TransportError("context length"))


# ---------------------------------------------------------------------------
# The fallback walk, streaming and not
# ---------------------------------------------------------------------------

_PATHS = pytest.mark.parametrize("stream", [True, False], ids=["stream", "chat"])


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_route_with_the_same_window_is_not_sent_the_prompt(stream):
    """The incident in one test. The backup would answer, which proves it was never asked."""
    primary_error = _refusal()
    primary = _Adapter(_cfg("local-a", 262144), primary_error)
    backup = _Adapter(_cfg("remote-b", 262144))
    router = _router(primary, backup)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _run(router, stream=stream)

    assert exc_info.value is primary_error
    assert backup.calls == 0
    # A route that was never contacted is not an attempt.
    assert [a["provider"] for a in exc_info.value._routing["failed_attempts"]] == ["local-a"]


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_route_with_a_narrower_window_is_not_sent_the_prompt(stream):
    primary = _Adapter(_cfg("local-a", 262144), _refusal())
    backup = _Adapter(_cfg("remote-b", 131072))
    router = _router(primary, backup)

    with pytest.raises(_UpstreamStatusError):
        await _run(router, stream=stream)

    assert backup.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_route_with_a_wider_window_still_gets_the_prompt(stream):
    primary = _Adapter(_cfg("local-a", 131072), _refusal(131072))
    backup = _Adapter(_cfg("remote-b", 262144))
    router = _router(primary, backup)

    result = await _run(router, stream=stream)

    assert backup.calls == 1
    if stream:
        assert _CONTENT_CHUNK in result
    else:
        assert result["_routing"]["endpoint_id"] == "remote-b"


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_other_400s_still_fall_back(stream):
    primary = _Adapter(_cfg("local-a", 262144), _UpstreamStatusError(400, _TOOL_SCHEMA_400))
    backup = _Adapter(_cfg("remote-b", 262144))
    router = _router(primary, backup)

    await _run(router, stream=stream)

    assert backup.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_a_fallbacks_refusal_also_ends_the_walk(stream):
    """The window that refused need not be the primary's."""
    primary = _Adapter(_cfg("dead-local", 262144), _TransportError("Cannot connect to host"))
    first = _Adapter(_cfg("local-b", 262144), _refusal())
    second = _Adapter(_cfg("remote-c", 262144))
    router = _router(primary, first, second)

    with pytest.raises(_UpstreamStatusError) as exc_info:
        await _run(router, stream=stream)

    assert first.calls == 1
    assert second.calls == 0
    # Surfacing is unchanged: the refusal describes the request, so it is the one reported.
    assert exc_info.value._routing["provider"] == "local-b"
    assert [a["provider"] for a in exc_info.value._routing["failed_attempts"]] == [
        "dead-local",
        "local-b",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@_PATHS
async def test_an_unknown_window_skips_nothing(stream):
    """With no usable configured window there is nothing to compare, so the old walk stands."""
    primary = _Adapter(_cfg("local-a", 262144), _refusal())
    backup_cfg = _cfg("remote-b", 262144)
    backup_cfg.context_length = None  # type: ignore[assignment]
    backup = _Adapter(backup_cfg)
    router = _router(primary, backup)

    await _run(router, stream=stream)

    assert backup.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_skipped_route_is_logged(caplog):
    primary = _Adapter(_cfg("local-a", 262144), _refusal())
    backup = _Adapter(_cfg("remote-b", 262144))
    router = _router(primary, backup)

    with (
        caplog.at_level(logging.INFO, logger="routing.routers"),
        pytest.raises(_UpstreamStatusError),
    ):
        await _run(router, stream=True)

    (record,) = [
        r for r in caplog.records if getattr(r, "event", None) == "context_window_fallback_skipped"
    ]
    assert record.model_id == "m"
    assert record.endpoint_id == "remote-b"
    assert record.context_length == 262144
    assert record.refused_context_length == 262144
