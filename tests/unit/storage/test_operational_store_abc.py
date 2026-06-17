"""Verify the OperationalStore ABC declares role-quota methods."""

import inspect

from serving.storage.base import OperationalStore


def test_count_active_keys_for_role_signature():
    sig = inspect.signature(OperationalStore.count_active_keys_for_role)
    assert list(sig.parameters) == ["self", "role"]


def test_apply_role_quota_signature():
    sig = inspect.signature(OperationalStore.apply_role_quota)
    assert list(sig.parameters) == ["self", "role", "quota"]


def test_weight_override_signatures():
    assert list(inspect.signature(OperationalStore.list_weight_overrides_for_model).parameters) == [
        "self",
        "model_id",
    ]
    assert list(inspect.signature(OperationalStore.list_all_weight_overrides).parameters) == [
        "self"
    ]
    assert list(inspect.signature(OperationalStore.upsert_weight_override).parameters) == [
        "self",
        "model_id",
        "endpoint_id",
        "weight",
        "updated_by",
    ]
    assert list(inspect.signature(OperationalStore.delete_weight_override).parameters) == [
        "self",
        "model_id",
        "endpoint_id",
    ]


def test_provider_route_config_signatures():
    assert list(
        inspect.signature(OperationalStore.list_provider_route_configs_for_model).parameters
    ) == [
        "self",
        "model_id",
    ]
    assert list(inspect.signature(OperationalStore.list_all_provider_route_configs).parameters) == [
        "self"
    ]
    assert list(inspect.signature(OperationalStore.upsert_provider_route_config).parameters) == [
        "self",
        "model_id",
        "route_id",
        "provider",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "quota_limit",
        "updated_by",
    ]
    assert list(inspect.signature(OperationalStore.delete_provider_route_config).parameters) == [
        "self",
        "model_id",
        "route_id",
    ]


def test_provider_route_candidate_signatures():
    assert list(
        inspect.signature(OperationalStore.list_provider_route_candidates_for_model).parameters
    ) == [
        "self",
        "model_id",
    ]
    assert list(
        inspect.signature(OperationalStore.list_all_provider_route_candidates).parameters
    ) == ["self"]
    assert list(inspect.signature(OperationalStore.upsert_provider_route_candidate).parameters) == [
        "self",
        "model_id",
        "route_id",
        "route_type",
        "provider",
        "base_url",
        "api_key_id",
        "provider_model_id",
        "quota_limit",
        "concurrency_limit",
        "weight",
        "updated_by",
    ]
    assert list(inspect.signature(OperationalStore.delete_provider_route_candidate).parameters) == [
        "self",
        "model_id",
        "route_id",
    ]
