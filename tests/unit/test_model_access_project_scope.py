"""Project allowlist scope in the shared model-access check."""

from __future__ import annotations

from types import SimpleNamespace

from serving.model_access import (
    ModelAccessDenial,
    is_model_outside_project_scope,
    model_access_denial,
)


def _route(canonical: str = "model-a", required_role: str = "free") -> SimpleNamespace:
    return SimpleNamespace(
        published=True,
        required_role=required_role,
        admin_only=False,
        adapters=[(SimpleNamespace(config=SimpleNamespace(id=canonical)), 1.0)],
    )


def test_empty_allowlist_means_every_model() -> None:
    ctx = {"role": "free", "project_allowed_models": []}
    assert not is_model_outside_project_scope("model-a", ctx)


def test_a_global_key_has_no_project_scope() -> None:
    assert not is_model_outside_project_scope("model-a", {"role": "free"})


def test_a_listed_model_passes_and_an_unlisted_one_does_not() -> None:
    ctx = {"project_allowed_models": ["model-a"]}
    assert not is_model_outside_project_scope("model-a", ctx)
    assert is_model_outside_project_scope("model-b", ctx)


async def test_the_shared_check_reports_the_project_denial() -> None:
    denial = await model_access_denial(
        _route(canonical="model-c"),
        "model-b",
        {"role": "free", "project_allowed_models": ["model-a"]},
    )
    assert denial is ModelAccessDenial.OUTSIDE_PROJECT
