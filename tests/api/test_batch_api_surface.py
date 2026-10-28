"""The gateway's batch API surface, as the real application registers it.

There is one batch API, ``/v1/batches``. The Anthropic Message Batches routes
(``/v1/messages/batches`` and ``/anthropic/v1/messages/batches``, with every
child operation) were removed rather than redirected: they are absent from the
OpenAPI document and answer 404. The online Messages routes they sat beside
are unchanged.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from serving.servers.app import create_app

_REMOVED = [
    (method, f"{prefix}/v1/messages/batches{suffix}")
    for prefix in ("", "/anthropic")
    for method, suffix in (
        ("POST", ""),
        ("GET", ""),
        ("GET", "/msgbatch_x"),
        ("POST", "/msgbatch_x/cancel"),
        ("DELETE", "/msgbatch_x"),
        ("GET", "/msgbatch_x/results"),
    )
]


@pytest.fixture(scope="module")
def app():
    return create_app()


def test_the_openapi_document_has_one_batch_api_and_no_message_batches(app):
    paths = app.openapi()["paths"]

    assert not [path for path in paths if "messages/batches" in path]
    assert {"post", "get"} <= set(paths["/v1/batches"])
    for child in ("/{batch_id}", "/{batch_id}/cancel", "/{batch_id}/results"):
        assert f"/v1/batches{child}" in paths
    # Online Messages is still served under both prefixes.
    assert "post" in paths["/v1/messages"]
    assert "post" in paths["/anthropic/v1/messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _REMOVED)
async def test_every_removed_message_batches_route_is_404(app, method, path):
    headers = {"Authorization": "Bearer any", "anthropic-version": "2023-06-01"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, headers=headers, json={"requests": []})

    assert response.status_code == 404
    # The Anthropic-shaped 404 every unknown path under these prefixes gets.
    assert response.json()["error"]["type"] == "not_found_error"
