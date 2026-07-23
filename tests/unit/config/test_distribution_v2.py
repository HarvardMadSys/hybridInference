"""Strict runtime-v2 selector, required-mode, and environment-contract tests."""

from __future__ import annotations

import json
import os
import traceback
from typing import TYPE_CHECKING

import pytest

from serving.config import distribution
from serving.config.distribution import (
    DistributionConfigError,
    DistributionStartupError,
    canonical_config_hash,
    get_distribution_config,
    get_distribution_config_comparison_state,
    load_distribution_config,
    preflight_distribution_config,
    resolve_config_path,
    validate_distribution_runtime_manifest,
)
from serving.config.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path

_ENV_VARS = (
    "DISTRIBUTION_CONFIG_PATH",
    "DISTRIBUTION_CONFIG_MODE",
    "DISTRIBUTION_MODELS_MODE",
    "DISTRIBUTION_ROUTING_MODE",
    "DISTRIBUTION_ALERTS_MODE",
    "DISTRIBUTION_CONFIG_REQUIRED",
    "DISTRIBUTION_EXPECTED_ID",
    "DISTRIBUTION_TARGET",
    "MODELS_CONFIG_PATH",
    "MODELS_CONFIG",
    "ROUTING_CONFIG_PATH",
    "ROUTING_CONFIG",
    "ALERTS_CONFIG_PATH",
)


@pytest.fixture(autouse=True)
def _clean_distribution_env(monkeypatch):
    targets = {name.casefold() for name in _ENV_VARS}
    for key in list(os.environ):
        if key.casefold() in targets:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    distribution._logged_once.clear()
    yield
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    distribution._logged_once.clear()


def _write_contract(
    root: Path,
    variables: str = "variables: []\n",
) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    contract = config_dir / "environment-contract.yaml"
    contract.write_text("environment_schema_version: 1\n" + variables)
    return contract


def _write_v2(
    root: Path,
    *,
    gateway: str = "",
    rag: str = "",
    extra: str = "",
) -> Path:
    manifest = root / "distribution.v2.yaml"
    manifest.write_text(
        """\
schema_version: 2
distribution:
  id: testdist
  display_name: Test Distribution
site:
  base_url: https://test.example
  docs_url: https://docs.test.example
  status_url: https://status.test.example
  support_email: support@test.example
features:
  auth.public_signup: true
  rag.chat: false
  admin.routing.routewise: true
resources:
  gateway:
"""
        + (gateway or "    {}\n")
        + """\
  rag:
"""
        + (rag or "    {}\n")
        + """\
environment_contract: config/environment-contract.yaml
"""
        + extra
    )
    return manifest


def _configure_required(monkeypatch, manifest: Path, **modes: str) -> None:
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_REQUIRED", "1")
    monkeypatch.setenv("DISTRIBUTION_MODELS_MODE", modes.get("models", "legacy"))
    monkeypatch.setenv("DISTRIBUTION_ROUTING_MODE", modes.get("routing", "legacy"))
    monkeypatch.setenv("DISTRIBUTION_ALERTS_MODE", modes.get("alerts", "legacy"))
    get_settings.cache_clear()
    get_distribution_config.cache_clear()


def test_v1_still_ignores_unknown_fields(tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        "schema_version: 1\n"
        "distribution:\n  id: legacy\n  future_identity: accepted\n"
        "future_top_level: accepted\n"
    )
    assert load_distribution_config(manifest).distribution.id == "legacy"


@pytest.mark.parametrize(
    "extra",
    [
        "future_top_level: rejected\n",
        "deployment:\n  target: production\n",
        "frontend:\n  path: frontend/\n",
    ],
)
def test_v2_rejects_unknown_or_non_runtime_top_level_fields(tmp_path, extra):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path, extra=extra)
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


def test_v2_rejects_unknown_nested_fields(tmp_path):
    _write_contract(tmp_path)
    manifest = _write_v2(
        tmp_path,
        gateway="    {}\n    deployment: forbidden\n",
    )
    with pytest.raises(DistributionConfigError):
        load_distribution_config(manifest)


def test_v2_maps_final_site_features_and_gateway_paths(tmp_path):
    _write_contract(tmp_path)
    (tmp_path / "config/models.yaml").write_text("models: []\n")
    manifest = _write_v2(tmp_path, gateway="    models: config/models.yaml\n")
    config = load_distribution_config(manifest)
    assert config.schema_version == 2
    assert config.distribution.release == ""
    assert config.site.public_base_url == "https://test.example"
    assert config.site.docs_url == "https://docs.test.example"
    assert config.features.public_signup is True
    assert config.features.rag is False
    assert config.features.routers == ["fixed", "routewise"]
    assert config._capability_expectations == {
        "auth.public_signup": True,
        "rag.chat": False,
        "admin.routing.routewise": True,
    }
    assert config.paths.models == str((tmp_path / "config/models.yaml").resolve())
    assert config.resources.gateway.models == config.paths.models


def test_v2_non_required_omitted_selectors_default_to_legacy(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    assert preflight_distribution_config() is not None
    assert resolve_config_path("models").source == "default"


def test_required_v2_needs_all_three_explicit_selectors(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_REQUIRED", "1")
    monkeypatch.setenv("DISTRIBUTION_MODELS_MODE", "legacy")
    with pytest.raises(DistributionStartupError, match="explicit selectors"):
        preflight_distribution_config()


def test_required_v2_rejects_global_mode_even_with_selectors(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    _configure_required(monkeypatch, manifest)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "dark")
    get_settings.cache_clear()
    with pytest.raises(DistributionStartupError, match="rejects DISTRIBUTION_CONFIG_MODE"):
        preflight_distribution_config()


def test_required_v2_allows_explicit_legacy_tuple_and_ignores_deploy_env(
    tmp_path,
    monkeypatch,
):
    _write_contract(
        tmp_path,
        """\
variables:
  - name: DEPLOY_ONLY_TOKEN
    classification: secret
    consumer: deploy
    required: true
    default: false
""",
    )
    manifest = _write_v2(tmp_path)
    _configure_required(monkeypatch, manifest)
    assert preflight_distribution_config() is not None


def test_v2_shadow_or_active_candidate_must_be_closed_root(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    outside = tmp_path.parent / "outside-models.yaml"
    outside.write_text("models: []\n")
    manifest = _write_v2(tmp_path, gateway="    models: ../outside-models.yaml\n")
    _configure_required(monkeypatch, manifest, models="shadow")
    with pytest.raises(DistributionStartupError, match="closed-root"):
        preflight_distribution_config()


def test_required_active_resource_rejects_legacy_env_override(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text("models: []\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="active")
    monkeypatch.setenv("MODELS_CONFIG_PATH", "config/models.yaml")
    get_settings.cache_clear()
    with pytest.raises(DistributionStartupError, match="cannot be overridden"):
        resolve_config_path("models")


def test_required_broken_manifest_does_not_fail_open(tmp_path, monkeypatch):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text("schema_version: 99\ndistribution:\n  id: bad\n")
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_REQUIRED", "1")
    with pytest.raises(DistributionStartupError, match="failed static validation"):
        get_distribution_config()


def test_manifest_schema_secret_is_absent_from_fail_open_logs(tmp_path, monkeypatch, caplog):
    canary = "sk-startup-log-do-not-leak"
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        "schema_version: 2\n"
        "distribution:\n  id: testdist\n"
        f"accidental_secret: {canary}\n"
        "environment_contract: environment.yaml\n"
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    with caplog.at_level("ERROR"):
        assert get_distribution_config() is None

    assert canary not in caplog.text


def test_manifest_schema_secret_is_absent_from_required_traceback(tmp_path, monkeypatch):
    canary = "sk-startup-traceback-do-not-leak"
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(
        "schema_version: 2\n"
        "distribution:\n  id: testdist\n"
        f"accidental_secret: {canary}\n"
        "environment_contract: environment.yaml\n"
    )
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_CONFIG_REQUIRED", "1")
    get_settings.cache_clear()
    get_distribution_config.cache_clear()

    with pytest.raises(DistributionStartupError) as exc_info:
        get_distribution_config()

    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    assert canary not in rendered


def test_expected_id_is_an_external_assertion(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_EXPECTED_ID", "other")
    with pytest.raises(DistributionStartupError, match="identity mismatch"):
        preflight_distribution_config()


def test_target_is_validated_as_an_audit_label(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_PATH", str(manifest))
    monkeypatch.setenv("DISTRIBUTION_TARGET", "prodution")
    with pytest.raises(DistributionStartupError, match="DISTRIBUTION_TARGET"):
        preflight_distribution_config()


def test_selected_backend_required_no_default_env_must_exist(tmp_path, monkeypatch):
    _write_contract(
        tmp_path,
        """\
variables:
  - name: PROVIDER_TOKEN
    classification: secret
    consumer: backend
    required: true
    default: false
    resources: [models]
""",
    )
    (tmp_path / "models.yaml").write_text(
        "models:\n"
        "  - id: test\n"
        "    route:\n"
        "      - kind: openai_compat\n"
        "        api_key: ${PROVIDER_TOKEN}\n"
    )
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="shadow")
    with pytest.raises(DistributionStartupError, match="PROVIDER_TOKEN"):
        preflight_distribution_config()


def test_selected_reference_must_be_declared_without_logging_values(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text(
        "models:\n  - id: test\n    api_key: ${UNDECLARED_TOKEN}\n"
    )
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="shadow")
    with pytest.raises(DistributionStartupError, match="UNDECLARED_TOKEN"):
        preflight_distribution_config()


def test_semantic_validator_control_environment_names_are_reserved(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text("models: []\nvalidator_override_attempt: ${PYTHONPATH}\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="shadow")
    with pytest.raises(DistributionStartupError, match="reserved validator environment"):
        preflight_distribution_config()


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("models", "models:\n  - invalid scalar\n"),
        (
            "routing",
            "local_deployment:\n  - endpoint: not-a-url\n    models: [test]\n",
        ),
        ("alerts", "rules:\n  failed_request_rate:\n    window_sec: nope\n"),
    ],
)
def test_shadow_candidates_run_their_real_semantic_parser(
    tmp_path,
    monkeypatch,
    kind,
    content,
):
    _write_contract(tmp_path)
    candidate = tmp_path / f"{kind}.yaml"
    candidate.write_text(content)
    manifest = _write_v2(tmp_path, gateway=f"    {kind}: {kind}.yaml\n")
    _configure_required(monkeypatch, manifest, **{kind: "shadow"})
    with pytest.raises(DistributionStartupError, match=f"{kind} candidate failed semantic"):
        preflight_distribution_config()


def test_legacy_selector_still_validates_nonempty_gateway_candidate(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text("models:\n  - invalid scalar\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="legacy")

    with pytest.raises(DistributionStartupError, match="models candidate failed semantic"):
        preflight_distribution_config()


def test_legacy_selector_still_rejects_gateway_path_escape(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    outside = tmp_path.parent / "outside-models.yaml"
    outside.write_text("models: []\n")
    manifest = _write_v2(tmp_path, gateway="    models: ../outside-models.yaml\n")
    _configure_required(monkeypatch, manifest, models="legacy")

    with pytest.raises(DistributionStartupError, match="closed-root"):
        preflight_distribution_config()


def test_strict_runtime_rejects_wrong_type_gateway_reference(tmp_path):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    manifest.write_text(
        manifest.read_text().replace("gateway:\n    {}", "gateway:\n    models: [models.yaml]")
    )

    with pytest.raises(DistributionConfigError, match=r"resources\.gateway\.models"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


@pytest.mark.parametrize("reference", ["${BROKEN_TOKEN", "$BROKEN_TOKEN"])
def test_strict_runtime_rejects_malformed_environment_syntax(tmp_path, reference):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text(f"models: []\napi_key: {reference}\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")

    with pytest.raises(DistributionConfigError, match="environment reference syntax"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_runtime_secret_list_requires_secret_environment_classification(tmp_path):
    _write_contract(
        tmp_path,
        """\
variables:
  - name: PROVIDER_TOKEN
    classification: config
    consumer: backend
    required: false
    default: false
    resources: [models]
""",
    )
    (tmp_path / "models.yaml").write_text("models: []\napi_keys:\n  - ${PROVIDER_TOKEN}\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")

    with pytest.raises(DistributionConfigError, match="must be classified secret"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def _write_valid_rag_resources(root: Path) -> str:
    corpus = root / "rag-corpus"
    corpus.mkdir()
    (corpus / "guide.md").write_text("# Guide\n\nPortable inference.")
    (root / "rag-settings.yaml").write_text(
        """\
schema_version: 1
embedder_mode: hash
embed_model: bge-m3
chat_model: test-chat
"""
    )
    (root / "rag-index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "embed_model": "bge-m3",
                "embedder_mode": "hash",
                "dim": 2,
                "records": [
                    {
                        "id": "guide.md#0",
                        "text": "Portable inference.",
                        "source": "guide.md",
                        "title": "Guide",
                        "embedding": [0.5, -0.5],
                    }
                ],
            }
        )
    )
    (root / "rag-metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "corpus_digest": "a" * 64,
                "chunk_text_digest": "b" * 64,
                "embedding_model": "bge-m3",
                "embedding_dimension": 2,
                "chunker_version": "v1",
            }
        )
    )
    return """\
    settings: rag-settings.yaml
    corpus: rag-corpus
    index: rag-index.json
    metadata: rag-metadata.json
"""


def test_strict_runtime_validates_complete_rag_set_with_real_index_parser(tmp_path):
    _write_contract(tmp_path)
    rag = _write_valid_rag_resources(tmp_path)
    manifest = _write_v2(tmp_path, rag=rag)

    assert validate_distribution_runtime_manifest(manifest, root=tmp_path).schema_version == 2


@pytest.mark.parametrize(
    ("resource", "replacement", "message"),
    [
        ("settings", "rag-corpus", "rag.settings must be a file"),
        ("corpus", "rag-index.json", "rag.corpus must be a directory"),
        ("index", "rag-corpus", "rag.index must be a file"),
        ("metadata", "rag-corpus", "rag.metadata must be a file"),
    ],
)
def test_strict_runtime_enforces_rag_resource_types(
    tmp_path,
    resource,
    replacement,
    message,
):
    _write_contract(tmp_path)
    rag = _write_valid_rag_resources(tmp_path)
    rag = rag.replace(
        next(line for line in rag.splitlines() if line.strip().startswith(f"{resource}:")),
        f"    {resource}: {replacement}",
    )
    manifest = _write_v2(tmp_path, rag=rag)

    with pytest.raises(DistributionConfigError, match=message):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_strict_runtime_rejects_corrupt_or_mismatched_rag_artifacts(tmp_path):
    _write_contract(tmp_path)
    rag = _write_valid_rag_resources(tmp_path)
    (tmp_path / "rag-index.json").write_text(
        (tmp_path / "rag-index.json")
        .read_text()
        .replace(
            '"embedding": [0.5, -0.5]',
            '"embedding": [0.5]',
        )
    )
    manifest = _write_v2(tmp_path, rag=rag)

    with pytest.raises(DistributionConfigError, match="RAG index failed semantic"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_strict_runtime_rejects_unknown_rag_settings_field(tmp_path):
    _write_contract(tmp_path)
    rag = _write_valid_rag_resources(tmp_path)
    settings = tmp_path / "rag-settings.yaml"
    settings.write_text(settings.read_text() + "future_setting: forbidden\n")
    manifest = _write_v2(tmp_path, rag=rag)

    with pytest.raises(DistributionConfigError, match="RAG settings failed strict"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_strict_runtime_cross_checks_rag_metadata_and_index(tmp_path):
    _write_contract(tmp_path)
    rag = _write_valid_rag_resources(tmp_path)
    metadata = tmp_path / "rag-metadata.json"
    metadata.write_text(
        metadata.read_text().replace('"embedding_dimension": 2', '"embedding_dimension": 3')
    )
    manifest = _write_v2(tmp_path, rag=rag)

    with pytest.raises(DistributionConfigError, match="metadata does not match"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_rag_environment_reference_must_be_declared(tmp_path):
    _write_contract(tmp_path)
    rag = _write_valid_rag_resources(tmp_path)
    settings = tmp_path / "rag-settings.yaml"
    settings.write_text(settings.read_text() + "api_key: ${RAG_API_KEY}\n")
    manifest = _write_v2(tmp_path, rag=rag)

    with pytest.raises(DistributionConfigError, match="RAG_API_KEY"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_rag_secret_field_requires_secret_environment_classification(tmp_path):
    _write_contract(
        tmp_path,
        """\
variables:
  - name: RAG_API_KEY
    classification: config
    consumer: backend
    required: false
    default: false
    resources: [rag]
""",
    )
    rag = _write_valid_rag_resources(tmp_path)
    settings = tmp_path / "rag-settings.yaml"
    settings.write_text(settings.read_text() + "api_key: ${RAG_API_KEY}\n")
    manifest = _write_v2(tmp_path, rag=rag)

    with pytest.raises(DistributionConfigError, match="must be classified secret"):
        validate_distribution_runtime_manifest(manifest, root=tmp_path)


def test_canonical_hash_ignores_yaml_mapping_order(tmp_path):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("beta: 2\nalpha:\n  nested: true\n")
    second.write_text("alpha:\n  nested: true\nbeta: 2\n")
    assert canonical_config_hash(first) == canonical_config_hash(second)


def test_preflight_emits_value_safe_canonical_mismatch_signal(tmp_path, monkeypatch, caplog):
    _write_contract(tmp_path)
    candidate = tmp_path / "models.yaml"
    candidate.write_text("models: []\n")
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("models:\n  - id: secret-marker-must-not-be-logged\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="shadow")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(legacy))
    get_settings.cache_clear()

    with caplog.at_level("INFO"):
        preflight_distribution_config()

    assert "distribution_config_state kind=models selector=shadow" in caplog.text
    assert "distribution_config_mismatch=1" in caplog.text
    assert canonical_config_hash(candidate) in caplog.text
    assert "secret-marker-must-not-be-logged" not in caplog.text


def test_missing_candidate_is_a_machine_readable_mismatch(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    manifest = _write_v2(tmp_path)
    _configure_required(monkeypatch, manifest)

    preflight_distribution_config()

    state = get_distribution_config_comparison_state()
    assert state["models"] == {
        "resource": "models",
        "selector": "legacy",
        "source": "default",
        "status": "candidate_missing",
        "mismatch": 1,
    }


def test_invalid_effective_config_is_a_machine_readable_mismatch(tmp_path, monkeypatch):
    _write_contract(tmp_path)
    (tmp_path / "models.yaml").write_text("models: []\n")
    invalid_effective = tmp_path / "invalid-effective.yaml"
    invalid_effective.write_text("- not\n- a\n- mapping\n")
    manifest = _write_v2(tmp_path, gateway="    models: models.yaml\n")
    _configure_required(monkeypatch, manifest, models="shadow")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(invalid_effective))
    get_settings.cache_clear()

    preflight_distribution_config()

    assert get_distribution_config_comparison_state()["models"]["status"] == "effective_invalid"
    assert get_distribution_config_comparison_state()["models"]["mismatch"] == 1
