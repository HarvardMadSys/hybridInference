"""A /v1/messages body the gateway cannot translate is the caller's 400, not an endpoint failure.

Regression: a text block whose ``text`` is a number made the Anthropic -> OpenAI
translation raise ``TypeError`` inside ``adapter.messages()``, before anything was
sent upstream. The surface's catch-all answered it 502 and charged it to the
endpoint's circuit breaker, so three such requests from one caller opened the
circuit, and every other caller of the model got 503 until it recovered -- with
not one request having reached the endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from routing.endpoint_health import _CircuitState
from routing.endpoints import endpoint_id_for_adapter

# The OpenAI-backed model of the anthropic compat fixtures: requests to it are
# translated, which is where the failure happened.
TRANSLATED_MODEL = "glm-4.7"
_TEXT_NOT_A_STRING = [{"role": "user", "content": [{"type": "text", "text": 123}]}]


def _auth() -> dict[str, str]:
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _body(**overrides) -> dict:
    body = {
        "model": TRANSLATED_MODEL,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(overrides)
    return body


@pytest.fixture
def quiet_alerts(monkeypatch):
    """Keep a tripped breaker from trying to page Slack during a test."""
    from unittest.mock import AsyncMock

    from serving.observability import alerts

    monkeypatch.setattr(alerts, "alert_slack", AsyncMock())


@pytest.fixture
def no_log_store(monkeypatch):
    """Swap DB logging for an in-memory capture so tests stay unit-fast."""
    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(amod, "_schedule_log_store_task", lambda log_store, **kwargs: None)


@pytest.fixture
def upstream(monkeypatch) -> list[str]:
    """Answer every upstream call with a short completion, and count the calls."""
    from serving.http import AsyncHTTPClient

    sent: list[str] = []
    completion = {
        "id": "c1",
        "object": "chat.completion",
        "model": TRANSLATED_MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    async def fake_json_post(self, url, **kwargs):
        sent.append(url)
        return completion

    async def fake_stream_post(self, url, **kwargs):
        sent.append(url)
        yield 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}'
        yield "data: [DONE]"

    monkeypatch.setattr(AsyncHTTPClient, "json_post", fake_json_post)
    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_json_post)
    monkeypatch.setattr(AsyncHTTPClient, "stream_post", fake_stream_post)
    return sent


def _endpoint_id(router) -> str:
    adapter, _weight = router.routes[TRANSLATED_MODEL].adapters[0]
    return endpoint_id_for_adapter(adapter)


def _circuit(router) -> dict:
    return router.endpoint_health_registry.snapshot().get(_endpoint_id(router), {})


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"messages": _TEXT_NOT_A_STRING}, "messages[0].content[0].text"),
        ({"system": [{"type": "text", "text": 5}]}, "system[0].text"),
        (
            {
                "messages": [
                    {"role": "system", "content": [{"type": "text", "text": ["x"]}]},
                    {"role": "user", "content": "hi"},
                ]
            },
            "messages[0].content[0].text",
        ),
    ],
    ids=["message-block", "system-block", "inline-system-block"],
)
async def test_a_text_block_that_is_not_a_string_is_refused_before_dispatch(
    anthropic_test_client,
    anthropic_compat_router,
    monkeypatch,
    quiet_alerts,
    no_log_store,
    upstream,
    stream,
    overrides,
    field,
):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")

    for _ in range(5):
        r = await anthropic_test_client.post(
            "/v1/messages", json=_body(stream=stream, **overrides), headers=_auth()
        )
        assert r.status_code == 400
        error = r.json()["error"]
        assert error["type"] == "invalid_request_error"
        assert f"{field} must be a string" in error["message"]

    assert upstream == []
    assert _circuit(anthropic_compat_router).get("circuit_state", _CircuitState.CLOSED) == (
        _CircuitState.CLOSED
    )
    good = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert good.status_code == 200
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_body_the_translation_cannot_read_is_never_charged(
    anthropic_test_client,
    anthropic_compat_router,
    monkeypatch,
    caplog,
    quiet_alerts,
    no_log_store,
    upstream,
    stream,
):
    """Whatever shape gets past the up-front check, the translation's error is a 400.

    The up-front check names the shape that was seen; this is the guarantee for
    the next one, so it is driven with that check switched off.
    """
    from serving.servers.routers import anthropic_messages as amod

    monkeypatch.setattr(amod, "_shape_error", lambda body: None)
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")

    for _ in range(5):
        with caplog.at_level(logging.WARNING, logger="serving.adapters.base"):
            r = await anthropic_test_client.post(
                "/v1/messages",
                json=_body(stream=stream, messages=_TEXT_NOT_A_STRING),
                headers=_auth(),
            )
        if stream:
            # Committed before the adapter ran, so the refusal is in-band.
            assert r.status_code == 200
            events = [
                json.loads(line[len("data: ") :])
                for line in r.text.splitlines()
                if line.startswith("data: ")
            ]
            assert events[-1]["type"] == "error"
            assert events[-1]["error"]["type"] == "invalid_request_error"
        else:
            assert r.status_code == 400
            assert r.json()["error"]["type"] == "invalid_request_error"

    assert upstream == []
    circuit = _circuit(anthropic_compat_router)
    assert circuit["circuit_state"] == _CircuitState.CLOSED
    assert circuit["availability"] == 1.0
    # Each one is logged under its own event, so a run of them stands out.
    logged = [r for r in caplog.records if r.getMessage() == "untranslatable_request"]
    assert len(logged) == 5
    assert {(r.model_id, r.error_type) for r in logged} == {(TRANSLATED_MODEL, "TypeError")}
    good = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
    assert good.status_code == 200
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_circuit_opened_through_messages_names_its_callers(
    anthropic_test_client, anthropic_compat_router, monkeypatch, quiet_alerts, no_log_store
):
    """The affected-caller list of a circuit-open alert covers this surface's callers.

    Read off the request context, which only the chat surface used to fill in:
    a trip on an endpoint serving Claude Code traffic listed nobody.
    """
    import aiohttp

    from serving.http import AsyncHTTPClient

    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "3")
    # One test key sends every request, and one caller alone cannot open a
    # circuit (tests/unit/routing/test_caller_repeat_breaker.py); with callers
    # not told apart, this reads the surface's health wiring on its own.
    monkeypatch.setenv("CIRCUIT_CALLER_WINDOW_SECONDS", "0")

    async def fake_post(self, url, **kwargs):
        raise aiohttp.ClientResponseError(
            request_info=None, history=None, status=502, message="bad gateway"
        )

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)

    for _ in range(3):
        r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())
        assert r.status_code == 502

    registry = anthropic_compat_router.endpoint_health_registry
    endpoint_id = _endpoint_id(anthropic_compat_router)
    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    assert dict(registry._circuits[endpoint_id]._affected_callers) == {"test-user": 3}
    await asyncio.sleep(0)
