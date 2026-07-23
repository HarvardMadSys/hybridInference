"""Strict closed-root bundle and deterministic lock tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serving.config.distribution import (
    DistributionConfigError,
    build_distribution_bundle_lock,
    load_distribution_bundle,
    render_distribution_bundle_lock,
    validate_distribution_bundle_lock,
    validate_distribution_bundle_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FREEINFERENCE_ROOT = REPO_ROOT / "distributions" / "freeinference"
BUNDLE = FREEINFERENCE_ROOT / "bundle.yaml"


def test_checked_in_bundle_is_strict_and_closed_root():
    bundle = load_distribution_bundle(BUNDLE)
    assert bundle.bundle_schema_version == 1
    assert Path(bundle.runtime_manifest).parent == FREEINFERENCE_ROOT
    assert Path(bundle.environment_contract).is_file()
    for resource in bundle.resources.values():
        Path(resource.path).resolve().relative_to(FREEINFERENCE_ROOT)


def test_bundle_lock_generation_is_byte_deterministic():
    first = render_distribution_bundle_lock(BUNDLE)
    second = render_distribution_bundle_lock(BUNDLE)
    assert first == second
    paths = [entry["path"] for entry in json.loads(first)["files"]]
    assert paths == sorted(paths, key=lambda value: value.encode())
    assert "bundle.lock.json" not in paths
    assert all(entry["classification"] for entry in json.loads(first)["files"])


def test_checked_in_bundle_lock_matches_inventory():
    validate_distribution_bundle_lock(BUNDLE)


def test_bundle_rejects_unknown_fields(tmp_path):
    (tmp_path / "runtime.yaml").write_text("schema_version: 1\ndistribution:\n  id: x\n")
    (tmp_path / "environment.yaml").write_text("environment_schema_version: 1\nvariables: []\n")
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        """\
bundle_schema_version: 1
runtime_manifest: runtime.yaml
environment_contract: environment.yaml
release: forbidden
"""
    )
    with pytest.raises(DistributionConfigError):
        load_distribution_bundle(bundle)


def test_bundle_rejects_parent_traversal(tmp_path):
    (tmp_path / "environment.yaml").write_text("environment_schema_version: 1\nvariables: []\n")
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        """\
bundle_schema_version: 1
runtime_manifest: ../runtime.yaml
environment_contract: environment.yaml
"""
    )
    with pytest.raises(DistributionConfigError, match="root-relative"):
        load_distribution_bundle(bundle)


def test_bundle_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-runtime.yaml"
    outside.write_text("schema_version: 1\ndistribution:\n  id: x\n")
    (tmp_path / "runtime.yaml").symlink_to(outside)
    (tmp_path / "environment.yaml").write_text("environment_schema_version: 1\nvariables: []\n")
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        """\
bundle_schema_version: 1
runtime_manifest: runtime.yaml
environment_contract: environment.yaml
"""
    )
    with pytest.raises(DistributionConfigError, match="escapes"):
        load_distribution_bundle(bundle)


def test_bundle_rejects_root_internal_resource_symlink(tmp_path):
    _write_contract(tmp_path / "environment.yaml")
    _, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    (tmp_path / "real.yaml").write_text("value: safe\n")
    (tmp_path / "alias.yaml").symlink_to(tmp_path / "real.yaml")
    bundle.write_text(
        bundle.read_text()
        + """\
resources:
  alias:
    path: alias.yaml
    classification: private
    consumer: backend
"""
    )

    with pytest.raises(DistributionConfigError, match="must not use symlinks"):
        load_distribution_bundle(bundle)


def test_stale_lock_is_rejected(tmp_path):
    (tmp_path / "runtime.yaml").write_text("schema_version: 1\ndistribution:\n  id: x\n")
    (tmp_path / "environment.yaml").write_text("environment_schema_version: 1\nvariables: []\n")
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        """\
bundle_schema_version: 1
runtime_manifest: runtime.yaml
environment_contract: environment.yaml
"""
    )
    lock = tmp_path / "bundle.lock.json"
    lock.write_text(json.dumps(build_distribution_bundle_lock(bundle)))
    validate_distribution_bundle_lock(bundle, lock)
    (tmp_path / "runtime.yaml").write_text("schema_version: 1\ndistribution:\n  id: changed\n")
    with pytest.raises(DistributionConfigError, match="stale"):
        validate_distribution_bundle_lock(bundle, lock)


def _write_contract(path):
    path.write_text("environment_schema_version: 1\nvariables: []\n")


def _write_v2_bundle(tmp_path, runtime_environment: str, bundle_environment: str):
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        """\
schema_version: 2
distribution:
  id: test
resources:
  gateway: {}
environment_contract: """
        + runtime_environment
        + "\n"
    )
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        """\
bundle_schema_version: 1
runtime_manifest: runtime.yaml
environment_contract: """
        + bundle_environment
        + "\n"
    )
    return runtime, bundle


def test_bundle_rejects_runtime_resource_parent_traversal(tmp_path):
    _write_contract(tmp_path / "environment.yaml")
    outside = tmp_path.parent / "outside-models.yaml"
    outside.write_text("models: []\n")
    runtime, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    runtime.write_text(
        runtime.read_text().replace("gateway: {}", "gateway:\n    models: ../outside-models.yaml")
    )
    with pytest.raises(DistributionConfigError, match="canonical root-relative"):
        load_distribution_bundle(bundle)


def test_bundle_rejects_missing_nonempty_runtime_resource(tmp_path):
    _write_contract(tmp_path / "environment.yaml")
    runtime, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    runtime.write_text(
        runtime.read_text().replace("gateway: {}", "gateway:\n    models: models.yaml")
    )
    with pytest.raises(DistributionConfigError, match="missing or escapes"):
        load_distribution_bundle(bundle)


def test_bundle_rejects_runtime_resource_missing_from_bundle_inventory(tmp_path):
    _write_contract(tmp_path / "environment.yaml")
    runtime, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    (tmp_path / "models.yaml").write_text("models: []\n")
    runtime.write_text(
        runtime.read_text().replace("gateway: {}", "gateway:\n    models: models.yaml")
    )

    with pytest.raises(DistributionConfigError, match="not covered by a bundle backend resource"):
        build_distribution_bundle_lock(bundle)


def test_bundle_lock_covers_runtime_resource_through_backend_inventory(tmp_path):
    _write_contract(tmp_path / "environment.yaml")
    runtime, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/models.yaml").write_text("models: []\n")
    runtime.write_text(
        runtime.read_text().replace("gateway: {}", "gateway:\n    models: config/models.yaml")
    )
    bundle.write_text(
        bundle.read_text()
        + """\
resources:
  gateway_config:
    path: config
    classification: secret-reference
    consumer: backend
"""
    )

    lock = build_distribution_bundle_lock(bundle)

    assert "config/models.yaml" in {entry["path"] for entry in lock["files"]}


def test_bundle_and_runtime_environment_contract_must_match(tmp_path):
    _write_contract(tmp_path / "runtime-environment.yaml")
    _write_contract(tmp_path / "bundle-environment.yaml")
    _, bundle = _write_v2_bundle(
        tmp_path,
        "runtime-environment.yaml",
        "bundle-environment.yaml",
    )
    with pytest.raises(DistributionConfigError, match="same environment_contract"):
        load_distribution_bundle(bundle)


def _write_bundle_resource_fixture(
    tmp_path,
    *,
    contract_consumer: str = "backend",
    classification: str = "private",
    variable_classification: str = "config",
    reference: str = "${SERVICE_URL}",
    field: str = "endpoint",
):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/config.yaml").write_text(f"{field}: {reference}\n")
    (tmp_path / "environment.yaml").write_text(
        f"""\
environment_schema_version: 1
variables:
  - name: SERVICE_URL
    classification: {variable_classification}
    consumer: {contract_consumer}
    required: false
    default: false
"""
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        """\
schema_version: 2
distribution:
  id: test
resources:
  gateway: {}
  rag: {}
environment_contract: environment.yaml
"""
    )
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text(
        f"""\
bundle_schema_version: 1
runtime_manifest: runtime.yaml
environment_contract: environment.yaml
resources:
  source:
    path: source
    classification: {classification}
    consumer: backend
"""
    )
    return bundle


def test_bundle_scans_declared_resource_environment_references(tmp_path):
    bundle = _write_bundle_resource_fixture(tmp_path)

    assert load_distribution_bundle(bundle).resources["source"].consumer == "backend"


def test_bundle_resource_reference_requires_matching_consumer(tmp_path):
    bundle = _write_bundle_resource_fixture(tmp_path, contract_consumer="frontend")

    with pytest.raises(DistributionConfigError, match="consumer backend"):
        load_distribution_bundle(bundle)


def test_bundle_rejects_secret_reference_in_public_resource(tmp_path):
    bundle = _write_bundle_resource_fixture(
        tmp_path,
        classification="public",
        variable_classification="secret",
    )

    with pytest.raises(DistributionConfigError, match="classified public"):
        load_distribution_bundle(bundle)


def test_bundle_secret_field_requires_secret_contract_classification(tmp_path):
    bundle = _write_bundle_resource_fixture(tmp_path, field="api_key")

    with pytest.raises(DistributionConfigError, match="must be classified secret"):
        load_distribution_bundle(bundle)


def test_bundle_secret_list_requires_secret_contract_classification(tmp_path):
    bundle = _write_bundle_resource_fixture(tmp_path)
    (tmp_path / "source/config.yaml").write_text("api_keys:\n  - ${SERVICE_URL}\n")

    with pytest.raises(DistributionConfigError, match="must be classified secret"):
        load_distribution_bundle(bundle)


@pytest.mark.parametrize("reference", ["${BROKEN_TOKEN", "$BROKEN_TOKEN"])
def test_bundle_rejects_malformed_environment_syntax(tmp_path, reference):
    bundle = _write_bundle_resource_fixture(tmp_path)
    (tmp_path / "source/config.yaml").write_text(f"endpoint: {reference}\n")

    with pytest.raises(DistributionConfigError, match="environment reference syntax"):
        load_distribution_bundle(bundle)


def test_bundle_reference_default_must_match_contract(tmp_path):
    bundle = _write_bundle_resource_fixture(
        tmp_path,
        reference="${SERVICE_URL:-https://service.invalid}",
    )

    with pytest.raises(DistributionConfigError, match="default not declared"):
        load_distribution_bundle(bundle)


def test_required_external_input_requires_matching_required_contract(tmp_path):
    (tmp_path / "environment.yaml").write_text(
        """\
environment_schema_version: 1
variables:
  - name: BACKEND_IMAGE
    classification: config
    consumer: deploy
    required: false
    default: true
"""
    )
    _, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    bundle.write_text(
        bundle.read_text()
        + """\
external_inputs:
  backend:
    kind: image-ref
    from_env: BACKEND_IMAGE
    consumer: deploy
    required: true
"""
    )

    with pytest.raises(DistributionConfigError, match="incompatible required/default"):
        load_distribution_bundle(bundle)


def test_default_bundle_lock_must_not_escape_through_symlink(tmp_path):
    _write_contract(tmp_path / "environment.yaml")
    _, bundle = _write_v2_bundle(tmp_path, "environment.yaml", "environment.yaml")
    outside_lock = tmp_path.parent / f"{tmp_path.name}-outside-lock.json"
    outside_lock.write_text(render_distribution_bundle_lock(bundle))
    (tmp_path / "bundle.lock.json").symlink_to(outside_lock)

    with pytest.raises(DistributionConfigError, match="escapes the validation root"):
        validate_distribution_bundle_manifest(bundle, root=tmp_path)
