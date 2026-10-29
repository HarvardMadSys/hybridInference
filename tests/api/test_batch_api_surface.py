"""The gateway's batch API surface, as the real application registers it.

There is one batch API, ``/v1/batches``. Each of the twelve operations the
retired Anthropic Message Batches APIs offered (``/v1/messages/batches`` and
``/anthropic/v1/messages/batches``) answers ``410 Gone`` with the way to
``/v1/batches``: the same static answer for any caller, key or id, without
reading the body or touching a batch. Behind the batch availability gate, as
every batch route is (``404`` while batches are off or have no store). None of
them is in the OpenAPI document, other paths and methods under those prefixes
are not retired operations, and the online Messages routes are unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from serving.batch import config as batch_config_module
from serving.servers.app import create_app
from serving.servers.deps import AppServices
from serving.servers.routers.retired_message_batches import MIGRATION_DOC, RETIRED_OPERATIONS

_RETIRED = [
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


class _UntouchableStore:
    """A batch store any access to which fails the test: a tombstone must not touch it."""

    def __getattr__(self, name):
        raise AssertionError(f"the tombstone touched the batch store ({name})")


@pytest.fixture
def settings(monkeypatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(
        batch_config_module, "config_value", lambda key, default=None: values.get(key, default)
    )
    return values


@pytest.fixture
def app():
    application = create_app()
    application.state.services = AppServices(router=MagicMock(), batch_store=_UntouchableStore())
    return application


async def _call(app, method: str, path: str, **headers: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, headers=headers, content=b"{not json")


def test_the_tombstones_are_exactly_the_twelve_former_operations():
    assert len(RETIRED_OPERATIONS) == 12
    assert {(m, p.replace("{batch_id}", "msgbatch_x")) for m, p in RETIRED_OPERATIONS} == set(
        _RETIRED
    )


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
@pytest.mark.parametrize(("method", "path"), _RETIRED)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-key"},
        {"Authorization": "Bearer not-a-key", "anthropic-version": "2023-06-01"},
        {"X-On-Behalf-Of": "someone-else"},
    ],
    ids=["anonymous", "bad-key", "anthropic-client", "on-behalf-of"],
)
async def test_every_retired_operation_is_410_with_migration_guidance(
    app, settings, method, path, headers
):
    response = await _call(app, method, path, **headers)

    assert response.status_code == 410
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    message = body["error"]["message"]
    for needed in ("/v1/batches", '"url": "/v1/messages"', "anthropic-version", MIGRATION_DOC):
        assert needed in message
    assert "msgbatch_" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _RETIRED)
async def test_a_retired_operation_is_404_while_batches_are_off(app, settings, method, path):
    settings["BATCH_ENABLED"] = "0"

    response = await _call(app, method, path)

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"


@pytest.mark.asyncio
async def test_a_retired_operation_is_404_without_a_batch_store(app, settings):
    app.state.services = AppServices(router=MagicMock(), batch_store=None)

    response = await _call(app, "POST", "/v1/messages/batches")

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        # Not operations the retired APIs had.
        ("GET", "/v1/messages/batches/msgbatch_x/other", 404),
        ("GET", "/anthropic/v1/messages/batches/msgbatch_x/results/extra", 404),
        ("GET", "/v1/messages/batchez", 404),
        ("PUT", "/v1/messages/batches/msgbatch_x", 405),
        ("PATCH", "/v1/messages/batches", 405),
    ],
)
async def test_other_paths_and_methods_are_not_tombstones(app, settings, method, path, status):
    response = await _call(app, method, path)

    assert response.status_code == status
