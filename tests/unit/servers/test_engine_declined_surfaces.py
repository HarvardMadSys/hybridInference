"""A decline by an engine's admission process, where the chat surface meets it.

A request the process declines and nothing carries to the cloud -- one pinned to
its route, say -- ends with ``EngineDeclined`` itself. It has no status, so read
like any other error it would be answered 500; it is answered 503. And a
non-streaming request for a model the process answers for goes upstream as a
buffered stream, since the process serves streams only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from serving.adapters.upstream_limiter import (
    EngineDeclined,
    UpstreamConcurrencyLimiter,
    reset_upstream_limiter,
)
from serving.servers.routers import completions
from serving.servers.routers.completions import ChatOptions
from serving.servers.routers.completions_stream import _extract_exception_status_code


@pytest.fixture(autouse=True)
def _limiter():
    reset_upstream_limiter(UpstreamConcurrencyLimiter(initial_limit=8, max_limit=8))
    yield
    reset_upstream_limiter()


def _route(*admissions: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        adapters=[
            (
                SimpleNamespace(
                    config=SimpleNamespace(
                        admission=admission, base_url="http://localhost:18000/v1"
                    )
                ),
                1.0,
            )
            for admission in admissions
        ]
    )


def test_a_decline_in_a_stream_is_answered_503():
    assert _extract_exception_status_code(EngineDeclined("declined_at_cutoff")) == 503


def test_a_model_the_process_answers_for_buffers_its_non_streaming_requests():
    options = ChatOptions(runtime_settings=None, buffers_for_engine_queues=True)

    assert completions._buffers_for_an_engine_queue(options, {}, _route(None, "nimbus"))
    # No process, no engine queue: sent as it was asked.
    assert not completions._buffers_for_an_engine_queue(options, {}, _route(None, None))
    # Only on the routes that may buffer at all.
    plain = ChatOptions(runtime_settings=None)
    assert not completions._buffers_for_an_engine_queue(plain, {}, _route("nimbus"))


def test_a_request_for_json_is_not_buffered_even_then():
    # Only the non-streaming path checks a reply against its format; the nimbus
    # router sends such a request straight to the cloud instead.
    options = ChatOptions(runtime_settings=None, buffers_for_engine_queues=True)
    params = {"response_format": {"type": "json_object"}}

    assert not completions._buffers_for_an_engine_queue(options, params, _route("nimbus"))
