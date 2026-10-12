"""A model skipped for missing configuration says so in its 404, to an administrator."""

from __future__ import annotations

import textwrap

import pytest

from routing.executor import RouteExecutor
from serving.model_access import GRANT_ALLOWED_MODELS_KEY
from serving.servers.model_availability import (
    model_not_found_detail,
    record_skipped_models,
    reset,
)
from serving.servers.registry import ModelLoadReport, register_from_models_yaml

ADMIN = {"user_id": "u-admin", "role": "admin"}


@pytest.fixture(autouse=True)
def _forget_skipped_models():
    yield
    reset()


def test_an_unknown_model_keeps_the_plain_message() -> None:
    assert model_not_found_detail("nope", user_ctx=ADMIN) == "Model 'nope' not found"
    assert (
        model_not_found_detail("nope", user_ctx=ADMIN, noun="Embedding model")
        == "Embedding model 'nope' not found"
    )


def test_a_skipped_model_and_its_aliases_point_at_the_administrator() -> None:
    record_skipped_models(ModelLoadReport(skipped_models={"chat": ["chat-latest"]}))

    for name in ("chat", "chat-latest"):
        assert model_not_found_detail(name, user_ctx=ADMIN) == (
            f"Model '{name}' is unavailable because this deployment's configuration is "
            "incomplete. Contact the administrator."
        )
    # Resolved through another name, such as an Anthropic alias.
    assert "unavailable" in model_not_found_detail("claude-alias", "chat", user_ctx=ADMIN)
    assert model_not_found_detail("other", user_ctx=ADMIN) == "Model 'other' not found"

    reset()
    assert model_not_found_detail("chat", user_ctx=ADMIN) == "Model 'chat' not found"


@pytest.mark.parametrize(
    "user_ctx",
    [
        None,
        {"user_id": "anonymous", "authenticated": False},
        {"user_id": "u-free", "role": "free"},
        {"user_id": "u-internal", "role": "internal"},
        # A grant names the only models its holder may know of, whoever's role
        # it carries -- an empty list included.
        {"user_id": "u-admin", "role": "admin", GRANT_ALLOWED_MODELS_KEY: ["other-model"]},
        {"user_id": "u-admin", "role": "admin", GRANT_ALLOWED_MODELS_KEY: []},
    ],
)
def test_no_one_else_can_tell_a_skipped_model_from_an_unknown_one(user_ctx) -> None:
    record_skipped_models(ModelLoadReport(skipped_models={"chat": ["chat-latest"]}))

    for name in ("chat", "chat-latest", "nope"):
        assert model_not_found_detail(name, user_ctx=user_ctx) == f"Model '{name}' not found"
    assert model_not_found_detail("claude-alias", "chat", user_ctx=user_ctx) == (
        "Model 'claude-alias' not found"
    )


def test_the_registry_reports_what_it_skipped(tmp_path, monkeypatch) -> None:
    for key in ("SKIP_TEST_KEY", "SKIP_TEST_BASE", "SKIP_TEST_POOL_A", "SKIP_TEST_POOL_B"):
        monkeypatch.delenv(key, raising=False)
    models = tmp_path / "models.yaml"
    models.write_text(
        textwrap.dedent(
            """
            models:
              - id: needs-key
                name: needs-key
                aliases: [needs-key-latest]
                route:
                  - kind: openai_compat
                    base_url: https://api.example/v1
                    api_key: ${SKIP_TEST_KEY}
              - id: needs-base
                name: needs-base
                route:
                  - kind: openai_compat
                    base_url: ${SKIP_TEST_BASE}
              - id: needs-pool
                name: needs-pool
                route:
                  - kind: openai_compat
                    base_url: https://api.example/v1
                    api_keys:
                      - ${SKIP_TEST_POOL_A}
                      - ${SKIP_TEST_POOL_B}
              - id: fine
                name: fine
                route:
                  - kind: openai_compat
                    base_url: https://api.example/v1
            """
        )
    )
    report = ModelLoadReport()

    _count, infos = register_from_models_yaml(
        RouteExecutor(), models, continue_on_missing_env=True, report=report
    )

    assert [info.model_id for info in infos] == ["fine"]
    assert report.skipped_models == {
        "needs-key": ["needs-key-latest"],
        "needs-base": [],
        "needs-pool": [],
    }
    assert report.unset_env_vars == {
        "SKIP_TEST_KEY",
        "SKIP_TEST_BASE",
        "SKIP_TEST_POOL_A",
        "SKIP_TEST_POOL_B",
    }


@pytest.mark.parametrize(
    ("user_ctx", "explained"),
    [
        (ADMIN, True),
        ({"user_id": "u-free", "role": "free"}, False),
        ({**ADMIN, GRANT_ALLOWED_MODELS_KEY: ["other-model"]}, False),
    ],
)
async def test_the_messages_endpoint_applies_the_same_rule(user_ctx, explained) -> None:
    """``/v1/messages`` resolves its model in ``_resolve``, with the same caller rule."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from serving.servers.routers.anthropic_messages import _resolve

    record_skipped_models(ModelLoadReport(skipped_models={"needs-key": []}))

    with pytest.raises(HTTPException) as excinfo:
        await _resolve("needs-key", SimpleNamespace(routes={}), user_ctx)

    assert excinfo.value.status_code == 404
    assert ("configuration is incomplete" in excinfo.value.detail) is explained
