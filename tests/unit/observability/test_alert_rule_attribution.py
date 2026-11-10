"""What may page the operator: routed failures, and bursts from more than one caller.

``failed_request_rate`` counted every 4xx without upstream attribution, so
anonymous requests for paths that do not exist (404s, and 405/413/422 the
gateway issues itself) paged as a service failure every cooldown.
``client_error_burst`` had no caller dimension, so one key could fire it.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routing.endpoint_health import EndpointHealthRegistry
from serving.observability import alert_rules
from serving.observability.alert_config import ClientErrorBurstConfig, RateRule
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.middleware.request_log import RequestLogMiddleware
from serving.utils import context as req_ctx
from serving.utils.context import MODEL_NOT_FOUND, MODEL_QUEUED

_REQUEST_LOG = "serving.servers.middleware.request_log"


@pytest.fixture
def transitions(monkeypatch) -> list[dict[str, Any]]:
    """Stand in for ``alert_on_transition``; keep each evaluation with its card."""
    calls: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> bool:
        context = kwargs.pop("context")
        calls.append({**kwargs, "context": context() if kwargs["breached"] else None})
        return False

    monkeypatch.setattr(alert_rules, "alert_on_transition", record)
    return calls


# --- failed_request_rate ---------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "counts"),
    [
        # The gateway's own verdicts on a request: anyone can send these.
        ({"status": 404}, False),
        ({"status": 404, "provider": ""}, False),
        ({"status": 405}, False),
        ({"status": 413}, False),
        ({"status": 422}, False),
        ({"status": 401}, False),
        ({"status": 403, "provider": None}, False),
        # Relayed from an upstream that answered a routed request.
        ({"status": 400, "provider": "zai"}, True),
        ({"status": 404, "provider": "zai"}, True),
        ({"status": 401, "provider": "diffusiongemma"}, True),
        ({"status": 422, "provider": "zai"}, True),
        # Still left out, attributed or not.
        ({"status": 429, "provider": "zai"}, False),
        ({"status": 404, "provider": "zai", "client_error_kind": MODEL_NOT_FOUND}, False),
        ({"status": 503, "client_error_kind": MODEL_QUEUED}, False),
        # A 5xx counts whoever answered it.
        ({"status": 500}, True),
        ({"status": 502, "provider": "openai"}, True),
        ({"status": 200, "provider": "openai"}, False),
    ],
)
def test_a_4xx_counts_only_when_an_upstream_returned_it(item, counts):
    assert alert_rules._is_failed_request(item) is counts


def _request_log_lines(requests: list[tuple[str, int]]) -> list[logging.LogRecord]:
    """Run ``requests`` (path, times) through the real middleware; return its lines."""
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, int]:
        return {"ok": 1}

    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    lines: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name == _REQUEST_LOG and record.getMessage() == "http_request":
                lines.append(record)

    capture = _Capture(level=logging.INFO)
    logger = logging.getLogger(_REQUEST_LOG)
    previous = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        client = TestClient(app)
        for path, times in requests:
            for _ in range(times):
                client.get(path)
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)
    return lines


async def test_anonymous_junk_paths_do_not_page(transitions):
    """The reviewer's case: 30 anonymous 404s among 80 good requests (27%)."""
    lines = _request_log_lines([("/ok", 80), ("/v1/x%0A%E2%80%A2%20*Remedy:*", 30)])
    assert {line.status_code for line in lines} == {200, 404}
    rule = alert_rules.FailedRequestRateRule(
        RateRule(window_sec=3600, threshold_pct=10.0, min_samples=100, cooldown_sec=900)
    )

    for line in lines:
        await rule.on_record(line)

    assert transitions, "the rule evaluated the window"
    assert not any(call["breached"] for call in transitions)


async def test_a_routed_upstream_failure_still_pages(transitions):
    rule = alert_rules.FailedRequestRateRule(
        RateRule(window_sec=3600, threshold_pct=10.0, min_samples=10, cooldown_sec=900)
    )
    for status, provider in [(200, "zai")] * 8 + [(404, "zai")] * 3:
        record = logging.LogRecord(_REQUEST_LOG, logging.INFO, "", 0, "http_request", None, None)
        record.status_code, record.provider, record.path = status, provider, "/v1/chat/completions"
        await rule.on_record(record)

    assert transitions[-1]["breached"]
    assert transitions[-1]["context"]["top_providers"] == "zai (3)"


# --- client_error_burst ----------------------------------------------------------


def _client_error(caller: str | None) -> logging.LogRecord:
    record = logging.LogRecord(
        "routing.routers", logging.INFO, "", 0, "client_error_skip_breaker", None, None
    )
    record.event = "client_error_skip_breaker"
    record.endpoint_id = "m:zai-api"
    record.status = 400
    record.caller = caller
    return record


def _burst_rule(**overrides: Any) -> alert_rules.ClientErrorBurstRule:
    config = {"window_sec": 600, "threshold_count": 12, "cooldown_sec": 0, **overrides}
    return alert_rules.ClientErrorBurstRule(ClientErrorBurstConfig(**config))


async def test_one_callers_burst_does_not_page_when_two_are_required(transitions):
    rule = _burst_rule(min_distinct_callers=2)
    for _ in range(40):
        await rule.on_record(_client_error("key-a"))
    assert not any(call["breached"] for call in transitions)

    await rule.on_record(_client_error("key-b"))

    assert transitions[-1]["breached"]
    assert transitions[-1]["context"]["distinct_callers"] == 2


async def test_by_default_one_callers_burst_still_pages(transitions):
    """The poisoned-conversation case the rule was written for, unchanged."""
    rule = _burst_rule()
    for _ in range(13):
        await rule.on_record(_client_error("key-a"))

    assert transitions[-1]["breached"]
    assert transitions[-1]["context"]["distinct_callers"] == 1


async def test_events_with_no_caller_count_as_one(transitions):
    rule = _burst_rule(min_distinct_callers=2)
    for _ in range(20):
        await rule.on_record(_client_error(None))

    assert not any(call["breached"] for call in transitions)


async def test_more_callers_than_the_tally_keeps_still_count_as_many(transitions):
    rule = _burst_rule(min_distinct_callers=5)
    for i in range(alert_rules._MAX_TRACKED_OFFENDERS + 10):
        await rule.on_record(_client_error(f"key-{i}"))

    assert transitions[-1]["breached"]
    cap = alert_rules._MAX_TRACKED_OFFENDERS
    assert transitions[-1]["context"]["distinct_callers"] == f"{cap}+ (capped)"


def test_the_event_names_its_caller_opaquely(caplog):
    """``record_failure`` tags a client error with a digest of the request's credential."""

    class _BadRequest(Exception):
        status = 400

    registry = EndpointHealthRegistry()
    with (
        caplog.at_level(logging.INFO, logger="routing.routers"),
        req_ctx.push(affinity_key="key-hash-of-the-caller"),
    ):
        registry.record_failure("m:zai-api", reason="error", exc=_BadRequest("HTTP 400"))
    registry.record_failure("m:zai-api", reason="error", exc=_BadRequest("HTTP 400"))

    first, second = [r for r in caplog.records if r.getMessage() == "client_error_skip_breaker"]
    assert first.caller == hashlib.sha256(b"key-hash-of-the-caller").hexdigest()[:12]
    assert "key-hash-of-the-caller" not in repr(first.__dict__)
    assert second.caller is None
