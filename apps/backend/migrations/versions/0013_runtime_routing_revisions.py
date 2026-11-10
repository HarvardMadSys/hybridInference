"""Track committed routing changes for synchronization between backend processes.

Only identifiers and counters are recorded, never route targets or credentials.
This is an expand revision; old releases ignore the new table and triggers.

Revision ID: 0013_runtime_routing_revisions
Revises: 0012_application_documents
"""

from __future__ import annotations

from alembic import op

revision = "0013_runtime_routing_revisions"
down_revision = "0012_application_documents"
branch_labels = None
depends_on = None
compatibility = "expand"

STATEMENTS = (
    """INSERT INTO config_source_revisions(source,epoch,revision,contracts)
    VALUES ('routing', substr(md5(random()::text || clock_timestamp()::text),1,16),0,'{}'::jsonb)
    ON CONFLICT(source) DO NOTHING""",
    """CREATE TABLE IF NOT EXISTS routing_config_revisions (
        model_id TEXT PRIMARY KEY, revision BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE OR REPLACE FUNCTION routing_config_changed() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE item jsonb; model text; setting text; next_revision bigint;
    BEGIN
        IF TG_OP = 'DELETE' THEN item := to_jsonb(OLD); ELSE item := to_jsonb(NEW); END IF;
        IF TG_TABLE_NAME = 'site_settings' THEN
            setting := item->>'key';
            IF setting LIKE 'model_router_strategy:%' OR setting LIKE 'model_required_role:%' THEN
                model := substring(setting FROM position(':' IN setting) + 1);
            ELSE RETURN NULL; END IF;
        ELSE model := COALESCE(item->>'model_id', '@providers'); END IF;
        INSERT INTO config_source_revisions AS r (source,epoch,revision,contracts)
        VALUES ('routing', substr(md5(random()::text || clock_timestamp()::text),1,16),1,'{}'::jsonb)
        ON CONFLICT(source) DO UPDATE SET revision=r.revision+1, updated_at=NOW()
        RETURNING revision INTO next_revision;
        INSERT INTO routing_config_revisions AS r (model_id, revision) VALUES (model, next_revision)
        ON CONFLICT (model_id) DO UPDATE SET revision = EXCLUDED.revision;
        RETURN NULL;
    END $$""",
    *(
        f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='runtime_routing_changed'
                       AND tgrelid='{table}'::regclass) THEN
            CREATE TRIGGER runtime_routing_changed AFTER INSERT OR UPDATE OR DELETE
            ON {table} FOR EACH ROW EXECUTE FUNCTION routing_config_changed();
        END IF; END $$"""
        for table in (
            "provider_route_candidates",
            "provider_route_configs",
            "provider_route_capabilities",
            "provider_definitions",
            "provider_api_keys",
            "disabled_provider_env_keys",
            "provider_env_key_min_roles",
            "site_settings",
        )
    ),
)


def upgrade() -> None:
    """Install transactional change counters."""
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Keep counters, which earlier releases ignore."""
