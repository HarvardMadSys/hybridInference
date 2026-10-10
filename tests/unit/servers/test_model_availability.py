"""A model skipped for missing configuration says so in its 404."""

from __future__ import annotations

import textwrap

from routing.executor import RouteExecutor
from serving.servers.model_availability import (
    model_not_found_detail,
    record_skipped_models,
    reset,
)
from serving.servers.registry import ModelLoadReport, register_from_models_yaml


def test_an_unknown_model_keeps_the_plain_message() -> None:
    assert model_not_found_detail("nope") == "Model 'nope' not found"
    assert (
        model_not_found_detail("nope", noun="Embedding model") == "Embedding model 'nope' not found"
    )


def test_a_skipped_model_and_its_aliases_point_at_the_administrator() -> None:
    record_skipped_models(ModelLoadReport(skipped_models={"chat": ["chat-latest"]}))

    for name in ("chat", "chat-latest"):
        assert model_not_found_detail(name) == (
            f"Model '{name}' is unavailable because this deployment's configuration is "
            "incomplete. Contact the administrator."
        )
    # Resolved through another name, such as an Anthropic alias.
    assert "unavailable" in model_not_found_detail("claude-alias", "chat")
    assert model_not_found_detail("other") == "Model 'other' not found"

    reset()
    assert model_not_found_detail("chat") == "Model 'chat' not found"


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
