"""The retired Anthropic Message Batches routes, answered with ``410 Gone``.

``/v1/messages/batches`` and ``/anthropic/v1/messages/batches`` with their
operations are no longer served: Messages batches are created and managed
through ``/v1/batches`` (``serving.servers.routers.batches``). Each of the
twelve method and path combinations those APIs offered answers here with a
static ``410`` in Anthropic's error shape that says where to go instead, so a
client still calling them learns why rather than meeting a bare ``404``.

Nothing here reads the body, authenticates, looks up a batch or creates work:
the answer is the same for every caller and every id, so it reveals nothing.
The batch availability gate still applies first -- without a batch store or
with ``BATCH_ENABLED`` off these paths are ``404``, as every batch route is.
Other paths under these prefixes, and other methods, are not retired
operations and keep the ordinary ``404``/``405``. None of these routes is in
the OpenAPI document. See
``docs/agents/specs/2026-10-28-unified-batch-api-amendment.zh.md``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from serving.batch.config import batch_config
from serving.servers.deps import get_batch_store

MIGRATION_DOC = "https://doc.hybridinference.org/batches.html#moving-from-message-batches"

RETIRED_MESSAGE = (
    "The Message Batches API has been removed from this gateway. Create Messages "
    'batches with POST /v1/batches: each request has "url": "/v1/messages" (or the '
    'batch has "endpoint": "/v1/messages" for a JSONL file), and the anthropic-version '
    "header names the version. Batches created here before (msgbatch_... ids) are "
    "listed, read, cancelled, deleted and downloaded through /v1/batches and "
    f"/v1/batches/{{id}}. See {MIGRATION_DOC}"
)

#: The operations the retired APIs offered, under each of their two prefixes.
RETIRED_OPERATIONS: tuple[tuple[str, str], ...] = tuple(
    (method, f"{prefix}/v1/messages/batches{suffix}")
    for prefix in ("", "/anthropic")
    for method, suffix in (
        ("POST", ""),
        ("GET", ""),
        ("GET", "/{batch_id}"),
        ("POST", "/{batch_id}/cancel"),
        ("DELETE", "/{batch_id}"),
        ("GET", "/{batch_id}/results"),
    )
)

router = APIRouter()


def _available(store: Any = Depends(get_batch_store)) -> None:
    """Answer 404 where batch inference is not served at all, as every batch route does."""
    if store is None or not batch_config().enabled:
        raise HTTPException(404, "Batch inference is not available on this gateway")


async def retired_message_batches() -> JSONResponse:
    """The same static ``410`` for every retired operation, whoever asks about whatever id."""
    return JSONResponse(
        status_code=410,
        content={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": RETIRED_MESSAGE},
        },
    )


for _method, _path in RETIRED_OPERATIONS:
    router.add_api_route(
        _path,
        retired_message_batches,
        methods=[_method],
        include_in_schema=False,
        dependencies=[Depends(_available)],
        response_model=None,
    )
