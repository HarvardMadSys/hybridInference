"""The same request, online and as a batch item, is served and recorded alike (issue #1690).

One owner, one non-auto model on a single-adapter route, a deterministic
upstream: each target is sent once to its online route and once through
``dispatch_item``, and the two ``api_logs`` rows, the charges and the answers
are compared field by field. What may differ is listed, not waved through: the
request id, timings, the caller's network provenance, the batch attribution,
and the ``stream: false`` a batch item is always run with.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from serving.batch.dispatch import dispatch_item
from serving.servers.auth import verify_api_key
from serving.servers.routers import anthropic_messages, completions, embeddings, responses
from tests.unit.serving.test_batch_targets_dispatch import _BODIES, _owner, _setup

_PATHS = {
    "chat": "/v1/chat/completions",
    "embeddings": "/v1/embeddings",
    "messages": "/v1/messages",
    "responses": "/v1/responses",
}

#: Metadata that legitimately differs between a request from a client and one
#: the worker runs: where it came from, and which batch attempt it was.
_PROVENANCE = frozenset(
    {
        "user_agent",
        "referer",
        "ip",
        "peer_ip",
        "ip_source",
        "x_forwarded_for",
        "x_real_ip",
        "batch_job_id",
        "batch_item_id",
        "batch_attempt",
    }
)

#: Values minted per request or per response.
_MINTED = frozenset({"id", "created", "created_at"})


def _without_minted(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_minted(v) for k, v in value.items() if k not in _MINTED}
    if isinstance(value, list):
        return [_without_minted(v) for v in value]
    return value


async def _settled_rows(services) -> list[dict[str, Any]]:
    for _ in range(200):
        await asyncio.sleep(0)
    return [call.kwargs for call in services.log_store.log_request.await_args_list]


async def _charges(services) -> list[float]:
    for _ in range(50):
        await asyncio.sleep(0)
    return [call.args[1] for call in services.operational_store.increment_user_cost.await_args_list]


async def _run_online(target: str) -> tuple[dict[str, Any], dict[str, Any], list[float]]:
    app, services, _upstream, _embedder = _setup()
    owner = _owner()
    app.dependency_overrides[verify_api_key] = lambda: dict(owner)
    for module in (completions, embeddings, anthropic_messages, responses):
        app.include_router(module.router)
    headers = {"anthropic-version": "2023-06-01"} if target == "messages" else {}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            _PATHS[target], json=copy.deepcopy(_BODIES[target]), headers=headers
        )
    assert response.status_code == 200, response.text
    rows = await _settled_rows(services)
    assert len(rows) == 1, rows
    return response.json(), rows[0], await _charges(services)


async def _run_batch(target: str) -> tuple[dict[str, Any], dict[str, Any], list[float], Any]:
    app, services, _upstream, _embedder = _setup()
    result = await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=copy.deepcopy(_BODIES[target]),
        batch_id="batch-parity",
        target=target,
        item_id=3,
        attempt=1,
        protocol_profile="2023-06-01" if target == "messages" else None,
    )
    assert result.error is None, result
    rows = await _settled_rows(services)
    assert len(rows) == 1, rows
    return result.response, rows[0], await _charges(services), result


@pytest.mark.parametrize("target", ["chat", "embeddings", "messages", "responses"])
async def test_an_item_is_served_logged_and_charged_as_the_same_online_request(target):
    online_body, online_row, online_charges = await _run_online(target)
    batch_body, batch_row, batch_charges, result = await _run_batch(target)

    # Charged once each, the same amount; the attempt saw that charge.
    assert len(online_charges) == len(batch_charges) == 1
    assert online_charges == pytest.approx(batch_charges)
    assert result.cost_status == "billed"
    assert result.cost_usd == pytest.approx(batch_charges[0])

    # The answer the caller gets.
    assert _without_minted(batch_body) == _without_minted(online_body)

    # The log rows, field by field.
    for field in ("model_id", "provider", "status_code", "usage", "prompt", "error", "pricing"):
        assert batch_row.get(field) == online_row.get(field), field
    assert _without_minted(batch_row.get("response")) == _without_minted(online_row.get("response"))
    online_params = {k: v for k, v in (online_row.get("params") or {}).items() if k != "request_id"}
    batch_params = {k: v for k, v in (batch_row.get("params") or {}).items() if k != "request_id"}
    if target == "messages":
        # The Messages log records the parameters as sent, stream included.
        assert batch_params == {**online_params, "stream": False}
    else:
        assert batch_params == online_params

    online_meta = {k: v for k, v in online_row["metadata"].items() if k not in _PROVENANCE}
    batch_meta = {k: v for k, v in batch_row["metadata"].items() if k not in _PROVENANCE}
    for ledger in ("ledger_started_at",):
        online_meta.pop(ledger, None)
        batch_meta.pop(ledger, None)
    assert batch_meta == online_meta
    assert batch_row["metadata"]["batch_job_id"] == "batch-parity"
    assert (batch_row["metadata"]["batch_item_id"], batch_row["metadata"]["batch_attempt"]) == (
        3,
        1,
    )
    assert "batch_job_id" not in online_row["metadata"]

    # The request as recorded: a batch item always runs with stream off.
    online_payload = online_row.get("request_payload")
    batch_payload = batch_row.get("request_payload")
    if target == "embeddings":
        assert batch_payload == online_payload
    else:
        # Chat and Messages record the body; Responses records the chat body it
        # translated, which carries the stream flag over.
        assert batch_payload == {**online_payload, "stream": False}
