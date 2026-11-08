"""Mirror new catalog settings for a previous image during the rollback window.

The new gateway never reads route_catalog. Older gateways parse its body with
their YAML loader and resolve references from app_config, so this keeps them
working after an administrator updates the new authoritative documents. The
original legacy rows are untouched until such an update occurs.

Only what the previous release can read is mirrored:

- The document is re-encoded as canonical JSON (``jsonb``), never copied as
  typed: JSON indented with tabs, valid as a setting, is a YAML scanner error
  there, and its gateway would start with no models. Characters YAML 1.1 does
  not accept raw (C1 controls, DEL, the Unicode line separators) are escaped.
- A model catalog whose credentials sit anywhere but a model's or a route's
  ``api_key`` or a route's ``api_keys`` is not mirrored: the previous registry
  expands references only there, so a lifted ``${CATALOG_SECRET_...}`` in,
  say, ``extra_body`` would reach the provider as that literal text. Its
  gateway keeps the catalog it last read instead.
- A reset, a blank value or a removed row is never mirrored: the legacy row is
  what a rollback runs on, and a restore of a backup taken before the upgrade
  would otherwise empty it.

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
    r"""CREATE OR REPLACE FUNCTION mirror_legacy_catalog() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE kind_name text; document jsonb; mirrored text; special text;
    BEGIN
        IF TG_OP <> 'INSERT' AND TG_OP <> 'UPDATE' THEN
            RETURN NULL;
        END IF;
        kind_name := CASE NEW.key WHEN 'MODEL_CATALOG' THEN 'models'
                                  WHEN 'ROUTING_POLICY' THEN 'routing' ELSE NULL END;
        IF kind_name IS NULL OR NEW.source = 'reset' OR btrim(NEW.value) = '' THEN
            RETURN NULL;
        END IF;
        BEGIN
            document := NEW.value::jsonb;
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
        IF jsonb_typeof(document) <> 'object' THEN
            RETURN NULL;
        END IF;
        IF kind_name = 'models' AND EXISTS (
            WITH RECURSIVE walk(path, item, credential) AS (
                SELECT ARRAY[]::text[], document, false
                UNION ALL
                SELECT walk.path || child.name, child.item, walk.credential OR child.credential
                FROM walk
                CROSS JOIN LATERAL (
                    SELECT entry.key AS name, entry.value AS item,
                           replace(entry.key, '-', '_')
                               ~* '(^|_)(api_keys?|token|password|secret|authorization|headers)(_|$)'
                               AS credential
                    FROM jsonb_each(CASE WHEN jsonb_typeof(walk.item) = 'object'
                                         THEN walk.item ELSE '{}'::jsonb END) AS entry
                    UNION ALL
                    SELECT (element.position - 1)::text, element.value, false
                    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(walk.item) = 'array'
                                                   THEN walk.item ELSE '[]'::jsonb END)
                         WITH ORDINALITY AS element(value, position)
                ) AS child
            )
            SELECT 1 FROM walk
            WHERE walk.credential
              AND jsonb_typeof(walk.item) IN ('string', 'number', 'boolean')
              AND walk.item NOT IN ('""'::jsonb, '0'::jsonb, 'false'::jsonb)
              AND NOT (walk.path[1] = 'models' AND (
                      (cardinality(walk.path) = 3 AND walk.path[3] = 'api_key')
                   OR (cardinality(walk.path) = 5 AND walk.path[3] = 'route'
                       AND walk.path[5] = 'api_key')
                   OR (cardinality(walk.path) = 6 AND walk.path[3] = 'route'
                       AND walk.path[5] = 'api_keys')))
        ) THEN
            RETURN NULL;
        END IF;
        mirrored := document::text;
        IF mirrored ~ '[\x7f-\x9f\u2028\u2029\ufffe\uffff]' THEN
            IF current_setting('server_encoding') <> 'UTF8' THEN
                RETURN NULL;
            END IF;
            FOR special IN
                SELECT DISTINCT matched[1]
                FROM regexp_matches(mirrored, '([\x7f-\x9f\u2028\u2029\ufffe\uffff])', 'g') AS matched
            LOOP
                mirrored := replace(
                    mirrored, special, '\u' || lpad(to_hex(ascii(special)), 4, '0')
                );
            END LOOP;
        END IF;
        INSERT INTO route_catalog(kind, body, source, updated_by)
        VALUES (kind_name, mirrored, 'settings_mirror', NEW.updated_by)
        ON CONFLICT (kind) DO UPDATE SET body = EXCLUDED.body, source = EXCLUDED.source,
            updated_by = EXCLUDED.updated_by, updated_at = NOW();
        RETURN NULL;
    END $$""",
    """DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='legacy_catalog_mirror'
                  AND tgrelid='app_config'::regclass) THEN
        CREATE TRIGGER legacy_catalog_mirror AFTER INSERT OR UPDATE
        ON app_config FOR EACH ROW EXECUTE FUNCTION mirror_legacy_catalog();
    END IF; END $$""",
)


def upgrade() -> None:
    """Keep prior images reading the catalog after new-document updates."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep the compatibility mirror, which does not change the authority."""
