"""Copy legacy catalog documents into the shared configuration store.

An expand revision: previous-release catalog rows remain unchanged. New code
reads only application settings and never imports files at startup.

Revision ID: 0012_application_documents
Revises: 0011_trim_oversized_preferences
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

import sqlalchemy as sa
import yaml
from alembic import op

revision = "0012_application_documents"
down_revision = "0011_trim_oversized_preferences"
branch_labels = None
depends_on = None
compatibility = "expand"


def _separate_secrets(value, namespace):
    # Frozen with this revision: migration history must not import service code.
    secret_field = re.compile(
        r"(^|_)(api_keys?|token|password|secret|authorization|headers)(_|$)", re.I
    )
    reference = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
    secrets = {}

    def visit(item, path, credential=False):
        if isinstance(item, dict):
            return {
                key: visit(
                    val,
                    f"{path}/{key}",
                    credential or bool(secret_field.search(key.replace("-", "_"))),
                )
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [visit(val, f"{path}/{index}", credential) for index, val in enumerate(item)]
        if credential and isinstance(item, str) and item and not reference.fullmatch(item):
            suffix = hashlib.sha256(f"{namespace}:{path}".encode()).hexdigest()[:20].upper()
            name = f"CATALOG_SECRET_{suffix}"
            secrets[name] = item
            return "${" + name + "}"
        return item

    return visit(value, ""), secrets


def _json_scalar(item):
    # Pricing schedules accept unquoted YAML dates in excluded_dates.
    if isinstance(item, date):
        return item.isoformat()
    raise TypeError("Unsupported legacy application document value")


def upgrade() -> None:
    """Preserve stored catalogs and lift embedded credentials into secret settings."""
    conn = op.get_bind()
    insert = sa.text(
        "INSERT INTO app_config (key, value, secret, source, updated_by) "
        "VALUES (:key, :value, :secret, 'admin', 'migration:0012') "
        "ON CONFLICT (key) DO NOTHING"
    )
    for row in conn.execute(sa.text("SELECT kind, body FROM route_catalog")).mappings():
        if row["kind"] not in ("models", "routing"):
            continue
        key = {"models": "MODEL_CATALOG", "routing": "ROUTING_POLICY"}[row["kind"]]
        if conn.execute(sa.text("SELECT 1 FROM app_config WHERE key=:key"), {"key": key}).scalar():
            continue
        value = yaml.safe_load(row["body"]) or {}
        if not isinstance(value, dict):
            raise ValueError("Stored route catalog must contain a mapping")
        document, secrets = _separate_secrets(value, row["kind"])
        for name, secret in secrets.items():
            conn.execute(insert, {"key": name, "value": secret, "secret": True})
        conn.execute(
            insert,
            {
                "key": key,
                "value": json.dumps(document, allow_nan=False, default=_json_scalar),
                "secret": False,
            },
        )


def downgrade() -> None:
    """Keep settings and legacy rows, both of which previous code tolerates."""
