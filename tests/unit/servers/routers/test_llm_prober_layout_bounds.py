"""A saved llm-prober layout is bounded, element by element and as a whole.

The layout's lists were unbounded, and so was the write: one account stored
31 MiB, which every request with its keys then decoded. The store's bounded
write is exercised against Postgres in
``tests/servers/test_user_preferences_atomic.py``; here a stand-in with the same
contract checks what the route asks of it and how it answers a refusal.
"""

import json
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from serving.exceptions import PreferencesTooLargeError
from serving.schemas_auth import (
    LLM_PROBER_LAYOUT_MAX_MODELS,
    LLM_PROBER_LAYOUT_MAX_PROVIDERS,
    LLM_PROBER_LAYOUT_NAME_MAX_LENGTH,
    LLMProberLayoutState,
)
from serving.servers.routers import user_routes
from serving.storage.base import USER_PREFERENCES_MAX_BYTES

USER = {"user_id": "u1"}


class _BoundedStore:
    """Stores a value unless the account's preferences would come out over the bound."""

    def __init__(self, held_bytes: int = 0) -> None:
        self.held_bytes = held_bytes
        self.calls: list[dict[str, Any]] = []

    async def set_user_preference(
        self, user_id: str, key: str, value: Any, *, max_total_bytes: int | None = None
    ) -> bool:
        self.calls.append({"key": key, "value": value, "max_total_bytes": max_total_bytes})
        if max_total_bytes is not None and self.held_bytes > max_total_bytes:
            raise PreferencesTooLargeError(max_total_bytes)
        return True


def _names(count: int, length: int = 8) -> list[str]:
    return [f"m{i:0{length - 1}d}" for i in range(count)]


def test_a_layout_of_ordinary_size_is_accepted():
    layout = LLMProberLayoutState(
        direct_models=_names(LLM_PROBER_LAYOUT_MAX_MODELS),
        direct_providers={"glm-4.7": ["glm-4.7::zai::api-z-ai"] * LLM_PROBER_LAYOUT_MAX_PROVIDERS},
        e2e_models=["x" * LLM_PROBER_LAYOUT_NAME_MAX_LENGTH],
    )
    assert len(layout.direct_models) == LLM_PROBER_LAYOUT_MAX_MODELS


@pytest.mark.parametrize(
    "layout",
    [
        {"direct_models": _names(LLM_PROBER_LAYOUT_MAX_MODELS + 1)},
        {"e2e_models": _names(LLM_PROBER_LAYOUT_MAX_MODELS + 1)},
        {"e2e_models": ["x" * (LLM_PROBER_LAYOUT_NAME_MAX_LENGTH + 1)]},
        {"direct_providers": {m: [] for m in _names(LLM_PROBER_LAYOUT_MAX_MODELS + 1)}},
        {"direct_providers": {"m": _names(LLM_PROBER_LAYOUT_MAX_PROVIDERS + 1)}},
        {"direct_providers": {"m" * (LLM_PROBER_LAYOUT_NAME_MAX_LENGTH + 1): []}},
        {"direct_providers": {"m": ["p" * (LLM_PROBER_LAYOUT_NAME_MAX_LENGTH + 1)]}},
    ],
    ids=[
        "direct-models",
        "e2e-models",
        "long-model-name",
        "provider-map",
        "providers-per-model",
        "long-map-key",
        "long-provider-name",
    ],
)
def test_a_layout_over_an_element_bound_is_refused(layout):
    with pytest.raises(ValidationError):
        LLMProberLayoutState.model_validate(layout)


def test_the_measured_attack_is_refused_before_it_reaches_the_store():
    """The review's 31 MiB layout was three million short names."""
    with pytest.raises(ValidationError):
        LLMProberLayoutState.model_validate({"direct_models": _names(3_000)})


@pytest.mark.asyncio
async def test_the_route_bounds_the_whole_document_it_writes():
    store = _BoundedStore()
    body = LLMProberLayoutState(direct_models=["glm-4.7"])

    result = await user_routes.update_llm_prober_layout(
        body=body, current_user=USER, op_store=store
    )

    assert result.layout == body
    assert store.calls == [
        {
            "key": "llm_prober_layout",
            "value": body.model_dump(),
            "max_total_bytes": USER_PREFERENCES_MAX_BYTES,
        }
    ]


@pytest.mark.asyncio
async def test_a_layout_that_would_overfill_the_preferences_is_a_413():
    store = _BoundedStore(held_bytes=USER_PREFERENCES_MAX_BYTES + 1)

    with pytest.raises(HTTPException) as refused:
        await user_routes.update_llm_prober_layout(
            body=LLMProberLayoutState(direct_models=["glm-4.7"]), current_user=USER, op_store=store
        )

    assert refused.value.status_code == 413
    assert str(USER_PREFERENCES_MAX_BYTES) in refused.value.detail


def test_the_bound_leaves_room_for_a_deployment_of_dozens_of_models():
    """Every model of a 40-model deployment, in every section, with four routes each."""
    models = [f"model-name-{i:02d}-instruct" for i in range(40)]
    layout = LLMProberLayoutState(
        direct_models=models,
        direct_providers={m: [f"{m}::provider-{j}::host-{j}" for j in range(4)] for m in models},
        e2e_models=models,
    )

    assert len(json.dumps({"llm_prober_layout": layout.model_dump()})) < USER_PREFERENCES_MAX_BYTES
