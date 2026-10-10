"""Batch processing surface (``/v1/batches``).

Submit many chat requests in one call, then poll or fetch results later. The
create body is inline (hybrid: no Files API) and the response is an
OpenAI-shaped Batch object with per-item results attached under ``results``.

This router owns auth, ownership scoping and persistence. Execution is the
in-process worker's job (``serving.batch_scheduler``); items are drained through
the normal ``chat_completions`` path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from serving.batch_schemas import (
    BatchCreateRequest,
    BatchDeleteResponse,
    BatchItemResult,
    BatchListResponse,
    BatchObject,
    BatchRequestCounts,
)
from serving.config.settings import has_role
from serving.servers.auth import verify_api_key
from serving.servers.deps import get_batch_store

router = APIRouter()

_SUPPORTED_ENDPOINTS = frozenset({"/v1/chat/completions"})


def _store_or_404(store: Any) -> Any:
    if store is None:
        raise HTTPException(404, "Batch processing is unavailable (no database configured)")
    return store


def _owner(request: Request, user_ctx: dict) -> tuple[str, bool]:
    user_id = user_ctx.get("user_id") or "anonymous"
    is_admin = has_role(user_ctx.get("role") or "free", "admin")
    return user_id, is_admin


def _epoch(value: Any) -> int | None:
    return int(value.timestamp()) if isinstance(value, datetime) else None


def _to_object(row: dict[str, Any], results: list[BatchItemResult] | None = None) -> BatchObject:
    return BatchObject(
        id=row["id"],
        endpoint=row.get("endpoint") or "/v1/chat/completions",
        status=row.get("status") or "validating",
        created_at=_epoch(row.get("created_at")) or 0,
        in_progress_at=_epoch(row.get("started_at")),
        completed_at=_epoch(row.get("completed_at")),
        expires_at=_epoch(row.get("expires_at")),
        request_counts=BatchRequestCounts(
            total=row.get("request_count") or 0,
            completed=row.get("completed_count") or 0,
            failed=row.get("failed_count") or 0,
        ),
        metadata=row.get("metadata"),
        results=results,
    )


def _results_from_items(items: list[dict[str, Any]]) -> list[BatchItemResult]:
    return [
        BatchItemResult(
            custom_id=item["custom_id"],
            response=item.get("response"),
            error=item.get("error"),
        )
        for item in items
    ]


@router.post("/v1/batches", response_model=BatchObject)
async def create_batch(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store: Any = Depends(get_batch_store),
):
    """Create a batch from an inline array of requests."""
    store = _store_or_404(store)
    try:
        body = await request.json()
        payload = BatchCreateRequest.model_validate(body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "Invalid batch request body") from exc

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    model_ids: set[str] = set()
    for entry in payload.requests:
        if entry.custom_id in seen:
            raise HTTPException(400, f"Duplicate custom_id: {entry.custom_id}")
        seen.add(entry.custom_id)
        if entry.url not in _SUPPORTED_ENDPOINTS:
            raise HTTPException(400, f"Unsupported batch url: {entry.url}")
        if entry.body.get("stream"):
            raise HTTPException(400, "Batch requests must not set stream=true")
        model = entry.body.get("model")
        if not isinstance(model, str) or not model:
            raise HTTPException(400, f"Item {entry.custom_id} is missing 'model'")
        model_ids.add(model)
        items.append(
            {
                "custom_id": entry.custom_id,
                "endpoint": entry.url,
                "model_id": model,
                "request": entry.body,
            }
        )

    user_id, _is_admin = _owner(request, user_ctx)
    batch_id = f"batch_{uuid.uuid4().hex}"
    await store.create_batch(
        batch_id=batch_id,
        user_id=user_id,
        role=user_ctx.get("role") or "free",
        endpoint=items[0]["endpoint"],
        items=items,
        model_ids=sorted(model_ids),
        metadata=payload.metadata,
    )
    row = await store.get_batch(batch_id)
    return _to_object(row)


@router.get("/v1/batches", response_model=BatchListResponse)
async def list_batches(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user_ctx: dict = Depends(verify_api_key),
    store: Any = Depends(get_batch_store),
):
    """List the caller's batches, newest first."""
    store = _store_or_404(store)
    user_id, _is_admin = _owner(request, user_ctx)
    rows = await store.list_batches(user_id=user_id, limit=limit)
    data = [_to_object(r) for r in rows]
    return BatchListResponse(
        data=data,
        has_more=len(data) == limit,
        first_id=data[0].id if data else None,
        last_id=data[-1].id if data else None,
    )


@router.get("/v1/batches/{batch_id}", response_model=BatchObject)
async def get_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store: Any = Depends(get_batch_store),
):
    """Fetch a batch and its per-item results."""
    store = _store_or_404(store)
    user_id, is_admin = _owner(request, user_ctx)
    row = await store.get_batch(batch_id, user_id=None if is_admin else user_id)
    if row is None:
        raise HTTPException(404, f"Batch {batch_id} not found")
    items = await store.get_items(batch_id)
    return _to_object(row, results=_results_from_items(items))


@router.delete("/v1/batches/{batch_id}", response_model=BatchDeleteResponse)
async def delete_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store: Any = Depends(get_batch_store),
):
    """Purge a batch and all of its items so nothing further is processed."""
    store = _store_or_404(store)
    user_id, is_admin = _owner(request, user_ctx)
    deleted = await store.delete_batch(batch_id, user_id=None if is_admin else user_id)
    if not deleted:
        raise HTTPException(404, f"Batch {batch_id} not found")
    return BatchDeleteResponse(id=batch_id)


@router.post("/v1/batches/{batch_id}/cancel", response_model=BatchObject)
async def cancel_batch(
    batch_id: str,
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    store: Any = Depends(get_batch_store),
):
    """Ask the worker to stop launching new items for a batch."""
    store = _store_or_404(store)
    user_id, is_admin = _owner(request, user_ctx)
    row = await store.get_batch(batch_id, user_id=None if is_admin else user_id)
    if row is None:
        raise HTTPException(404, f"Batch {batch_id} not found")
    await store.request_cancel(batch_id)
    row = await store.get_batch(batch_id)
    return _to_object(row)
