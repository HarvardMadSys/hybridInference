"""Mirror new catalog settings for a previous image during the rollback window.

The new gateway never reads route_catalog. Older gateways already accept JSON
through their YAML parser and resolve references from app_config, so this keeps
them working after an administrator updates the new authoritative documents.
The original legacy rows are untouched until such an update occurs.

Revision ID: 0014_legacy_catalog_mirror
Revises: 0013_runtime_routing_revisions
"""

from __future__ import annotations

from alembic import op

revision = "0014_legacy_catalog_mirror"
down_revision = "0013_runtime_routing_revisions"
branch_labels = None
depends_on = None
compatibility = "expand"

STATEMENTS = (
    """CREATE OR REPLACE FUNCTION mirror_legacy_catalog() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE setting text; document text; actor text; kind_name text;
    BEGIN
        IF TG_OP='DELETE' THEN
            setting := OLD.key; document := '{}'; actor := OLD.updated_by;
        ELSE
            setting := NEW.key; actor := NEW.updated_by;
            document := CASE WHEN NEW.source='reset' OR btrim(NEW.value)=''
                             THEN '{}' ELSE NEW.value END;
        END IF;
        kind_name := CASE setting WHEN 'MODEL_CATALOG' THEN 'models'
                                  WHEN 'ROUTING_POLICY' THEN 'routing' ELSE NULL END;
        IF kind_name IS NOT NULL THEN
            INSERT INTO route_catalog(kind,body,source,updated_by)
            VALUES(kind_name,document,'settings_mirror',actor)
            ON CONFLICT(kind) DO UPDATE SET body=EXCLUDED.body, source=EXCLUDED.source,
                updated_by=EXCLUDED.updated_by, updated_at=NOW();
        END IF;
        RETURN NULL;
    END $$""",
    """DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='legacy_catalog_mirror'
                  AND tgrelid='app_config'::regclass) THEN
        CREATE TRIGGER legacy_catalog_mirror AFTER INSERT OR UPDATE OR DELETE
        ON app_config FOR EACH ROW EXECUTE FUNCTION mirror_legacy_catalog();
    END IF; END $$""",
)


def upgrade() -> None:
    """Keep prior images reading the catalog after new-document updates."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the compatibility mirror, which does not change the authority."""
