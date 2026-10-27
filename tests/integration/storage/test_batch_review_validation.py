"""File batch validation against PostgreSQL.

Malformed JSON Unicode must fail file validation, not strand its work lease;
and an embeddings file is admitted through the application's own embedding
access gates and visibility resolver, as /v1/embeddings applies them.
"""

import hashlib
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from routing.routers import FixedRouter
from serving.batch import validation
from serving.batch.config import BatchConfig
from serving.storage.batch_file_store import BatchFileStore
from serving.storage.batch_store import BatchStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


async def test_unpaired_surrogate_custom_id_fails_batch_validation(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = f"review-unicode-{uuid.uuid4().hex}"
    await store.initialize()
    try:
        raw = (
            json.dumps(
                {
                    "custom_id": "\ud800",
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": "review-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                }
            )
            + "\n"
        ).encode("ascii")
        file_id = await files.create_upload(
            user_id=owner,
            filename="invalid.jsonl",
            purpose="batch",
            max_total_bytes=1000,
            max_uploads_per_hour=10,
        )
        await files.append_chunk(
            file_id=file_id, user_id=owner, seq=0, data=raw, max_total_bytes=1000
        )
        await files.finish_upload(
            file_id=file_id,
            user_id=owner,
            sha256=hashlib.sha256(raw).hexdigest(),
            line_count=1,
            retention_seconds=3600,
            check_owner=False,
        )
        batch = await store.create_batch(
            batch_id=f"batch_{uuid.uuid4().hex}",
            user_id=owner,
            key_hash=None,
            endpoint="/v1/chat/completions",
            model_ids=[],
            items=[],
            metadata=None,
            completion_window_seconds=3600,
            api_surface="openai_file",
            phase="validating",
            input_file_id=file_id,
        )
        await store.claim_batch_work(
            phase="validating", worker_id="review-validator", lease_seconds=60
        )
        monkeypatch.setattr(
            validation, "authenticate_stored_key", AsyncMock(return_value={"user_id": owner})
        )
        result = await validation.validate_file_batch(
            store=store,
            file_store=files,
            batch=batch,
            worker_id="review-validator",
            services=SimpleNamespace(),
            cfg=BatchConfig(),
            lease_seconds=60,
        )
        assert result == "failed"
        row = await store.get_batch(batch["id"])
        assert row["validation_error"]["data"][0]["line"] == 1
        assert await store.list_results(batch["id"]) == []
    finally:
        await store.delete_user_batches(owner)
        await pool.close()


# -- embeddings: validation reads the application's gates and overrides ------------


class _Visibility:
    """A runtime visibility override for one canonical model."""

    def __init__(self, model_id: str, role: str) -> None:
        self._model_id = model_id
        self._role = role
        self.asked: list[str] = []

    async def get_effective_required_role(self, model_id: str, default_role: str) -> str:
        self.asked.append(model_id)
        return self._role if model_id == self._model_id else default_role


def _embedding_services(
    model: str, alias: str, *, gate_role: str | None, visibility: Any
) -> SimpleNamespace:
    """Services as the registry leaves them; the adapter is never dispatched here.

    The embedding model is registered the way ``register_from_models_yaml``
    registers one: one adapter under the canonical id and its alias, and the
    access gate on the router under the same names. ``gate_role=None`` leaves
    the gate out, the inconsistency every embeddings surface must refuse.
    """
    adapter = SimpleNamespace(config=SimpleNamespace(id=model))
    router = FixedRouter()
    if gate_role is not None:
        router.register_embedding_route(
            model, [(adapter, 1.0)], aliases=[alias], required_role=gate_role
        )
    return SimpleNamespace(
        router=router,
        embedding_adapters={model: adapter, alias: adapter},
        model_visibility_resolver=visibility,
        operational_store=None,
    )


async def _validate_embeddings_file(
    monkeypatch, store, files, *, owner: str, role: str, models: list[str], services
) -> tuple[str, str]:
    """Upload an embeddings file naming *models*, lease its batch, validate it."""
    raw = "".join(
        json.dumps(
            {
                "custom_id": f"req-{index}",
                "method": "POST",
                "url": "/v1/embeddings",
                "body": {"model": model, "input": "hello"},
            }
        )
        + "\n"
        for index, model in enumerate(models)
    ).encode()
    file_id = await files.create_upload(
        user_id=owner,
        filename="embeddings.jsonl",
        purpose="batch",
        max_total_bytes=10_000,
        max_uploads_per_hour=10,
    )
    await files.append_chunk(
        file_id=file_id, user_id=owner, seq=0, data=raw, max_total_bytes=10_000
    )
    await files.finish_upload(
        file_id=file_id,
        user_id=owner,
        sha256=hashlib.sha256(raw).hexdigest(),
        line_count=len(models),
        retention_seconds=3600,
        check_owner=False,
    )
    batch = await store.create_batch(
        batch_id=f"batch_{uuid.uuid4().hex}",
        user_id=owner,
        key_hash=None,
        endpoint="/v1/embeddings",
        model_ids=[],
        items=[],
        metadata=None,
        completion_window_seconds=3600,
        api_surface="openai_file",
        phase="validating",
        input_file_id=file_id,
    )
    # Lease this batch by id: claim_batch_work takes the oldest validating
    # batch in the database, which may be another test file's.
    worker_id = f"review-validator-{uuid.uuid4().hex}"
    async with store.pool.acquire() as conn:
        await conn.execute(
            "UPDATE batch_jobs SET work_owner = $2, work_expires_at = now() + interval '1 min' "
            "WHERE id = $1",
            batch["id"],
            worker_id,
        )
    monkeypatch.setattr(
        validation,
        "authenticate_stored_key",
        AsyncMock(return_value={"user_id": owner, "role": role, "disabled_models": []}),
    )
    result = await validation.validate_file_batch(
        store=store,
        file_store=files,
        batch=batch,
        worker_id=worker_id,
        services=services,
        cfg=BatchConfig(),
        lease_seconds=60,
    )
    return result, batch["id"]


async def test_an_embeddings_file_is_admitted_through_the_apps_gate_and_override(monkeypatch):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = f"review-embed-ok-{uuid.uuid4().hex}"
    model = f"review-embed-{uuid.uuid4().hex}"
    alias = f"{model}-alias"
    # The gate requires pro; only the runtime override opens it to this free
    # owner, so admission proves validation passes the app's resolver along.
    visibility = _Visibility(model, "free")
    services = _embedding_services(model, alias, gate_role="pro", visibility=visibility)
    await store.initialize()
    try:
        result, batch_id = await _validate_embeddings_file(
            monkeypatch,
            store,
            files,
            owner=owner,
            role="free",
            models=[alias, model],
            services=services,
        )

        assert result == "published"
        row = await store.get_batch(batch_id)
        assert row["validation_error"] is None
        assert (row["status"], row["request_count"]) == ("in_progress", 2)
        # The alias line is staged, and the batch keyed, under the canonical id.
        assert row["model_ids"] == [model]
        items = await store.list_results(batch_id)
        assert [(item["custom_id"], item["model_id"]) for item in items] == [
            ("req-0", model),
            ("req-1", model),
        ]
        # The override is read by canonical id, whichever name the line used.
        assert set(visibility.asked) == {model}
    finally:
        await store.delete_user_batches(owner)
        await pool.close()


@pytest.mark.parametrize(
    ("gate_role", "override"),
    [
        pytest.param("free", "admin", id="visibility-override"),
        pytest.param("admin", None, id="role"),
        pytest.param(None, None, id="missing-gate"),
    ],
)
async def test_an_embeddings_file_the_owner_may_not_use_fails_with_nothing_staged(
    monkeypatch, gate_role, override
):
    pool = await postgres.create_pool(min_size=1, max_size=6)
    store, files = BatchStore(pool), BatchFileStore(pool)
    owner = f"review-embed-denied-{uuid.uuid4().hex}"
    model = f"review-embed-{uuid.uuid4().hex}"
    alias = f"{model}-alias"
    visibility = _Visibility(model, override) if override is not None else None
    services = _embedding_services(model, alias, gate_role=gate_role, visibility=visibility)
    await store.initialize()
    try:
        result, batch_id = await _validate_embeddings_file(
            monkeypatch,
            store,
            files,
            owner=owner,
            role="pro",
            models=[alias],
            services=services,
        )

        assert result == "failed"
        row = await store.get_batch(batch_id)
        (error,) = row["validation_error"]["data"]
        # Refused exactly as an unknown model would be.
        assert (error["code"], error["line"], error["param"]) == ("model_not_found", 1, "model")
        assert error["message"] == f"Line 1: Embedding model '{alias}' not found"
        assert (row["request_count"], row["model_ids"]) == (0, [])
        assert await store.list_results(batch_id) == []
    finally:
        await store.delete_user_batches(owner)
        await pool.close()
