"""Regression tests for _coerce_user_row — JSONB preferences decoding.

asyncpg returns JSONB columns as raw JSON strings when no type codec is
registered. Without coercion, get_disabled_models_from_preferences receives
a string instead of a dict and always returns [] — making all model
checkboxes appear enabled in the admin UI regardless of the actual setting.
"""

from __future__ import annotations

import json

from serving.storage.postgres_operational import _coerce_user_row


def test_coerce_user_row_returns_none_for_none():
    assert _coerce_user_row(None) is None


def test_coerce_user_row_preferences_as_dict():
    prefs = {"disabled_models": ["gpt-4o"]}
    row = {"id": "u1", "email": "a@b.com", "preferences": prefs}
    result = _coerce_user_row(row)
    assert result["preferences"] == prefs


def test_coerce_user_row_preferences_as_json_string():
    """Regression: asyncpg may return JSONB as a raw JSON string."""
    prefs = {"disabled_models": ["gpt-4o", "claude-3-opus"]}
    row = {"id": "u1", "email": "a@b.com", "preferences": json.dumps(prefs)}
    result = _coerce_user_row(row)
    assert result["preferences"] == prefs


def test_coerce_user_row_preferences_missing_key_becomes_empty_dict():
    row = {"id": "u1", "email": "a@b.com"}
    result = _coerce_user_row(row)
    assert result["preferences"] == {}


def test_coerce_user_row_preferences_none_becomes_empty_dict():
    row = {"id": "u1", "email": "a@b.com", "preferences": None}
    result = _coerce_user_row(row)
    assert result["preferences"] == {}


def test_coerce_user_row_preferences_malformed_json_becomes_empty_dict():
    row = {"id": "u1", "email": "a@b.com", "preferences": "not-valid-json"}
    result = _coerce_user_row(row)
    assert result["preferences"] == {}


def test_coerce_user_row_preferences_json_array_becomes_empty_dict():
    row = {"id": "u1", "email": "a@b.com", "preferences": json.dumps(["a", "b"])}
    result = _coerce_user_row(row)
    assert result["preferences"] == {}


def test_coerce_user_row_does_not_mutate_original():
    prefs_str = json.dumps({"disabled_models": ["m1"]})
    row = {"id": "u1", "preferences": prefs_str}
    _coerce_user_row(row)
    # original dict must be untouched
    assert row["preferences"] == prefs_str
