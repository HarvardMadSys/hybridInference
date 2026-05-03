"""Unit tests for the D1 migration script.

Tests type transforms, checkpoint management, and INSERT OR IGNORE
idempotency logic. No real database connections.
"""

from __future__ import annotations

import json

# Import internals from the migration script
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ops.cloudflare.d1_migrate import (
    _BOOLEAN_COLUMNS,
    _DECIMAL_COLUMNS,
    _JSONB_COLUMNS,
    _TABLES,
    _TIMESTAMP_COLUMNS,
    _build_insert_sql,
    _load_checkpoint,
    _save_checkpoint,
    _transform_row,
    _transform_value,
)

# ------------------------------------------------------------------
# Type transforms
# ------------------------------------------------------------------


class TestTransformValue:
    """Tests for individual value transforms."""

    def test_none_passthrough(self):
        for col in ["email", "created_at", "email_verified", "preferences"]:
            assert _transform_value(col, None) is None

    @pytest.mark.parametrize("col", sorted(_TIMESTAMP_COLUMNS))
    def test_timestamp_datetime_to_iso(self, col):
        dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _transform_value(col, dt)
        assert isinstance(result, str)
        assert "2026-03-15" in result

    @pytest.mark.parametrize("col", sorted(_TIMESTAMP_COLUMNS))
    def test_timestamp_string_passthrough(self, col):
        val = "2026-03-15T10:30:00+00:00"
        assert _transform_value(col, val) == val

    @pytest.mark.parametrize("col", sorted(_BOOLEAN_COLUMNS))
    def test_boolean_to_int(self, col):
        assert _transform_value(col, True) == 1
        assert _transform_value(col, False) == 0

    @pytest.mark.parametrize("col", sorted(_JSONB_COLUMNS))
    def test_jsonb_dict_to_string(self, col):
        val = {"key": "value"}
        result = _transform_value(col, val)
        assert isinstance(result, str)
        assert json.loads(result) == val

    @pytest.mark.parametrize("col", sorted(_JSONB_COLUMNS))
    def test_jsonb_string_passthrough(self, col):
        val = '{"key": "value"}'
        assert _transform_value(col, val) == val

    @pytest.mark.parametrize("col", sorted(_DECIMAL_COLUMNS))
    def test_decimal_to_float(self, col):
        result = _transform_value(col, Decimal("100.5050"))
        assert isinstance(result, float)
        assert result == pytest.approx(100.505)

    def test_text_passthrough(self):
        assert _transform_value("email", "alice@test.com") == "alice@test.com"
        assert _transform_value("user_name", "Alice") == "Alice"
        assert _transform_value("status", "active") == "active"


class TestTransformRow:
    """Tests for full row transforms."""

    def test_users_row(self):
        columns = ["id", "email", "email_verified", "created_at", "preferences"]
        row = {
            "id": "u1",
            "email": "a@b.com",
            "email_verified": True,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "preferences": {"theme": "dark"},
        }
        result = _transform_row(columns, row)
        assert result[0] == "u1"
        assert result[1] == "a@b.com"
        assert result[2] == 1  # bool → int
        assert "2026-01-01" in result[3]  # datetime → ISO string
        assert json.loads(result[4]) == {"theme": "dark"}  # dict → JSON string

    def test_missing_column_becomes_none(self):
        columns = ["id", "nonexistent_col"]
        row = {"id": "u1"}
        result = _transform_row(columns, row)
        assert result == ["u1", None]


# ------------------------------------------------------------------
# INSERT OR IGNORE SQL
# ------------------------------------------------------------------


class TestBuildInsertSQL:
    """Tests for SQL generation."""

    def test_users_insert(self):
        users_def = next(t for t in _TABLES if t["name"] == "users")
        sql = _build_insert_sql(users_def)
        assert sql.startswith("INSERT OR IGNORE INTO users")
        assert "VALUES" in sql
        assert sql.count("?") == len(users_def["columns"])

    def test_all_tables_have_valid_sql(self):
        for table_def in _TABLES:
            sql = _build_insert_sql(table_def)
            assert f"INSERT OR IGNORE INTO {table_def['name']}" in sql
            assert sql.count("?") == len(table_def["columns"])


# ------------------------------------------------------------------
# Table definitions
# ------------------------------------------------------------------


class TestTableDefs:
    """Verify table definitions are complete and correct."""

    def test_all_tables_defined(self):
        names = [t["name"] for t in _TABLES]
        assert "users" in names
        assert "api_keys" in names
        assert "auth_sessions" in names
        assert "email_verification_tokens" in names
        assert "password_reset_tokens" in names
        assert "admin_audit_log" in names
        assert "user_daily_cost" in names

    def test_each_table_has_pk(self):
        for t in _TABLES:
            assert t["pk"] in t["columns"], f"{t['name']} pk not in columns"

    def test_each_table_has_order_by(self):
        for t in _TABLES:
            assert t["order_by"], f"{t['name']} missing order_by"


# ------------------------------------------------------------------
# Checkpoint management
# ------------------------------------------------------------------


class TestCheckpoint:
    """Tests for checkpoint save/load."""

    def test_load_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "scripts.cloudflare.d1_migrate._CHECKPOINT_FILE", tmp_path / "missing.json"
        )
        assert _load_checkpoint() == {}

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        ckpt_file = tmp_path / "checkpoint.json"
        monkeypatch.setattr("scripts.cloudflare.d1_migrate._CHECKPOINT_FILE", ckpt_file)

        state = {"users": {"offset": 50, "migrated": 50}}
        _save_checkpoint(state)

        loaded = _load_checkpoint()
        assert loaded == state

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        ckpt_file = tmp_path / "nested" / "dir" / "checkpoint.json"
        monkeypatch.setattr("scripts.cloudflare.d1_migrate._CHECKPOINT_FILE", ckpt_file)

        _save_checkpoint({"test": True})
        assert ckpt_file.exists()
