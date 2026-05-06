"""Verify the OperationalStore ABC declares role-quota methods."""

import inspect

from serving.storage.base import OperationalStore


def test_count_active_keys_for_role_signature():
    sig = inspect.signature(OperationalStore.count_active_keys_for_role)
    assert list(sig.parameters) == ["self", "role"]


def test_apply_role_quota_signature():
    sig = inspect.signature(OperationalStore.apply_role_quota)
    assert list(sig.parameters) == ["self", "role", "quota"]
