# Postgres Schema Migrations with Alembic — Design

**Date:** 2026-05-03
**Status:** Draft → ready for plan
**Author:** Architecture review follow-up (issue #3 of 6)

## Problem

There is no schema migration framework for the Postgres backend. Schema definitions live in two places:

- Embedded in Python: `CREATE TABLE IF NOT EXISTS` blocks scattered across [apps/backend/serving/storage/postgres_log.py:49-100](../../../apps/backend/serving/storage/postgres_log.py#L49) and `apps/backend/serving/storage/postgres_operational.py`.
- A static SQL file for the D1 backend: [apps/backend/serving/storage/d1_schema.sql](../../../apps/backend/serving/storage/d1_schema.sql).

Schema is applied at app startup via the idempotent `IF NOT EXISTS` pattern. Schema changes are added by appending new `ALTER TABLE` lines, also idempotent.

Problems:

- No version tracking — operators can't tell what's been applied to a given DB.
- No rollback — failed deploys must be recovered from backups.
- No drift detection — code and DB can silently diverge across environments.
- Deployment is `git reset --hard` + restart with no schema-migration gate; an incompatible code/schema combo crashes the gateway on boot.

## Goals

1. Adopt Alembic for the Postgres backend (operational + log schemas live in one DB; one migration chain).
2. Establish a versioned, deploy-time migration step that runs *before* gateway restart.
3. Hard-fail gateway boot when the DB is at an unexpected schema version.
4. Provide ergonomic local-dev and CI workflows (`make migrate`, `make migrate-new`, etc.).
5. Cut over the existing schema cleanly via a baseline-and-stamp procedure with no production downtime beyond a normal deploy.

## Non-goals

- D1 backend migrations — D1 stays on `d1_schema.sql` and `wrangler d1 execute` for now; if D1 becomes a primary, that's a separate brainstorm.
- ORM adoption — no SQLAlchemy `Table` / `Model` definitions; Alembic uses `op.execute("...")` and `op.create_table(...)` directly.
- Autogenerate — without ORM models, Alembic can't autogenerate; engineers handwrite each migration.
- Async migrations — Alembic uses a sync SQLAlchemy engine *only for migrations*; runtime app code stays asyncpg-only.
- Online DDL / zero-downtime ALTER for huge tables — out of scope; the team handles those case-by-case using concurrent indexes and batched updates.

## Architecture

### Tooling

- **Alembic ≥ 1.13** — pulled in via `uv add alembic`. Brings sync SQLAlchemy as a substrate (used only by migrations).
- Migrations are handwritten Python files under `apps/backend/serving/storage/migrations/versions/` using `op.execute("ALTER TABLE …")` for raw SQL or `op.create_table(...)` / `op.add_column(...)` for portable ops.

### File layout

```
apps/backend/serving/storage/
  migrations/
    env.py                           # Alembic env hook (loads DB URL from settings)
    script.py.mako                   # Template for new migration files
    versions/
      0001_baseline.py               # Full current schema, stamped (not run) on existing DBs
      0002_<future>.py               # Each future schema change
      ...
  _expected_alembic_version.py       # EXPECTED_ALEMBIC_VERSION = "0001_baseline"
  postgres_log.py                    # CREATE TABLE blocks DELETED
  postgres_operational.py            # CREATE TABLE blocks DELETED
alembic.ini                          # repo-root Alembic config (points at apps/backend/serving/storage/migrations)
```

### `env.py` shape

Reads DB connection from settings. Supports online (apply against live DB) and offline (`alembic upgrade head --sql`) modes.

```python
from alembic import context
from sqlalchemy import create_engine
from serving.config.settings import get_settings

settings = get_settings()
DB_URL = (
    f"postgresql://{settings.db_user}:{settings.db_password}"
    f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)


def run_migrations_online():
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline():
    context.configure(url=DB_URL, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

### Baseline migration (`0001_baseline.py`)

`upgrade()` body is mechanically extracted from current init code:
- All `CREATE TABLE IF NOT EXISTS` blocks in [apps/backend/serving/storage/postgres_log.py:49-100](../../../apps/backend/serving/storage/postgres_log.py#L49).
- All `CREATE INDEX` lines from `postgres_log.py:85-93`.
- All `CREATE TABLE` and `CREATE INDEX` in `postgres_operational.py`.

Statements use `CREATE TABLE IF NOT EXISTS` so a misordered run on an already-populated DB is a no-op rather than a hard error (defense in depth).

`downgrade()`: `raise NotImplementedError("baseline migration is irreversible")`.

### Cut-over sequence (one-time, ops procedure)

1. Cut-over PR (Alembic setup + `0001_baseline.py` + deploy-script changes + `_expected_alembic_version.py` + init-code deletion) lands and deploys.
2. **Before the first deploy** that includes the gateway version-check, the operator runs on each environment (staging then prod):
   ```bash
   uv run alembic stamp 0001_baseline
   ```
3. Verify: `uv run alembic current` shows `0001_baseline (head)`.
4. After this, every schema change PR adds a new migration; deploys run `alembic upgrade head` and the gateway boots with version verified.

### Deploy script changes

Both `scripts/deploy_production.sh` and `scripts/deploy_staging.sh` get a migration step *before* container restart:

```bash
echo ">> Running database migrations..."
docker compose -f infrastructure/docker/docker-compose.yml run --rm backend \
    uv run alembic upgrade head
if [ $? -ne 0 ]; then
    echo "Migration failed; aborting deploy"
    exit 1
fi

echo ">> Restarting services..."
docker compose up -d --build
```

GitHub Actions workflows (`.github/workflows/deploy.yml`, `deploy-staging.yml`) call these scripts via SSH; no workflow YAML change beyond confirming the SSH-runner has access to the same image.

### Startup version check

`apps/backend/serving/storage/_expected_alembic_version.py`:

```python
EXPECTED_ALEMBIC_VERSION = "0001_baseline"
```

Each schema-change PR updates this constant to the new revision id. The diff makes the version bump visible to reviewers.

In `apps/backend/serving/servers/bootstrap.py`, early in `initialize()` (after settings load, before anything else hits the DB):

```python
async def _verify_schema_version(pool: asyncpg.Pool) -> None:
    if settings.db_backend != "postgres":
        return  # D1 is out of scope; skip.
    from serving.storage._expected_alembic_version import EXPECTED_ALEMBIC_VERSION
    async with pool.acquire() as conn:
        try:
            current = await conn.fetchval("SELECT version_num FROM alembic_version")
        except asyncpg.UndefinedTableError:
            raise RuntimeError(
                "alembic_version table missing — run 'uv run alembic stamp <version>' "
                "or 'uv run alembic upgrade head' before starting"
            )
    if current != EXPECTED_ALEMBIC_VERSION:
        raise RuntimeError(
            f"Schema version mismatch: code expects {EXPECTED_ALEMBIC_VERSION!r}, "
            f"DB at {current!r}. Run 'uv run alembic upgrade head' to fix."
        )
```

### Dev workflow — Makefile targets

```makefile
migrate:
	uv run alembic upgrade head

migrate-down:
	uv run alembic downgrade -1

migrate-new:
	@if [ -z "$(NAME)" ]; then echo "Usage: make migrate-new NAME='add_foo_to_bar'"; exit 1; fi
	uv run alembic revision -m "$(NAME)"

migrate-status:
	uv run alembic current
	uv run alembic history --verbose | head -20
```

Update `make setup-dev` to run `make migrate` after creating the venv and DB:

```makefile
setup-dev: ...
	@if [ -n "$$DB_NAME" ]; then make migrate; fi
```

### CI integration

In `.github/workflows/ci.yml`'s `Test` job, before `pytest`:

```yaml
- name: Apply migrations to test database
  run: uv run alembic upgrade head
  env:
    DB_HOST: localhost
    DB_PORT: 5433  # existing test postgres port
    DB_USER: ${{ env.DB_USER }}
    DB_PASSWORD: ${{ env.DB_PASSWORD }}
    DB_NAME: ${{ env.DB_NAME }}
```

Failed migration → CI fails before any test runs.

### Schema-change PR workflow (post-rollout)

For developers adding schema:

1. `make migrate-new NAME='add_last_login_at_to_users'` → generates `0002_add_last_login_at_to_users.py`.
2. Edit the file's `upgrade()` and `downgrade()` bodies.
3. `make migrate` against local dev DB to apply.
4. `make migrate-down` to verify downgrade (or set `raise NotImplementedError(...)` and document why).
5. Update `apps/backend/serving/storage/_expected_alembic_version.py` to point at the new revision id.
6. Commit migration file + version pin update + any code that uses the new schema.
7. Reviewer checks: migration looks reasonable, downgrade works (or is justifiably absent), version pin updated.

### Rollback policy

- **Best-effort `downgrade()`** — most migrations get one. Genuinely-irreversible ones use `raise NotImplementedError("irreversible: column drops data")`. Reviewers can push back if a `NotImplementedError` looks lazy.
- **Forward-only is the day-to-day reality** — even with `downgrade()` written, prod rollbacks usually involve restoring from backup; downgrade exists primarily for dev-loop ergonomics and as a last-resort emergency tool.

## Testing strategy

1. **Per-migration roundtrip in CI** — a pytest test (using existing `dbtest` marker) that:
   - Drops the test DB, recreates empty.
   - `alembic upgrade head` to apply all migrations.
   - Asserts a key invariant per migration (table exists, column has expected type, etc.).
   - For migrations with `downgrade()`: `alembic downgrade -1`, then `upgrade head` again to confirm reversibility.

2. **Schema-matches-code via existing tests** — the existing test suite already exercises real DB queries with the `dbtest` marker; if a migration is missing or wrong, those tests fail. No new fixture needed.

3. **One-shot baseline sanity check** — before merging the cut-over PR, the operator runs:
   ```bash
   pg_dump --schema-only $PROD_DB_URL > /tmp/prod_schema.sql
   # spin up empty postgres locally
   uv run alembic upgrade head
   pg_dump --schema-only $LOCAL_DB_URL > /tmp/built_schema.sql
   diff /tmp/prod_schema.sql /tmp/built_schema.sql
   ```
   The diff should be empty modulo Alembic's `alembic_version` table. Document the procedure in the PR description; not recurring CI.

4. **Pre-deploy `--sql` review** — before any non-trivial migration ships, the engineer runs `uv run alembic upgrade head --sql` locally and pastes the generated SQL into the PR description.

## Risk + mitigations

| Risk | Mitigation |
|---|---|
| Cut-over PR's baseline misses a column or index | Baseline sanity check (one-shot `pg_dump` diff) before merge. |
| Operator forgets the `alembic stamp` step | Startup version check fails fast and the deploy script's migration step also fails fast — if both fail, the gateway can't boot, so the failure is visible immediately rather than silent. |
| Migration with bad SQL in PR | CI runs migrations against a fresh DB before tests; bad SQL fails CI. `--sql` preview in PR description catches issues at review. |
| Long migration on big table blocks deploy | Acknowledged trade-off; engineers schedule low-traffic windows or use concurrent-index patterns case-by-case. |
| Two `api_logs` tables (Postgres vs. D1 schemas) | Documentation explicit: only Postgres is Alembic-managed. D1 stays static-SQL. |
| Backend = D1 (no Postgres) | Startup version check skips when `settings.db_backend != "postgres"`. |
| Migration step changes break `make setup-dev` for newcomers | Update `make setup-dev` to call `make migrate` automatically after DB is reachable. |

## Open questions (resolved during brainstorming)

| Question | Resolution |
|---|---|
| Where does `alembic_version` live (log vs. operational)? | Same Postgres DB hosts both schemas → one `alembic_version` table → one migration chain. |
| Async migrations? | No — sync SQLAlchemy substrate for migrations only; runtime stays asyncpg. |
| Long migrations on big tables? | Engineer-scheduled; out of framework scope. |
| D1 drift? | Out of scope; D1 stays on `d1_schema.sql`. |

## Out-of-scope follow-ups (separate brainstorms)

- D1 migration framework (if D1 ever becomes primary) — likely a homegrown numbered-SQL-files runner.
- Online DDL / zero-downtime patterns for huge tables.
- Cross-environment schema diff CI check (compare prod-snapshot dump vs. CI-built schema).
- Decompose [apps/backend/serving/servers/routers/completions.py](../../../apps/backend/serving/servers/routers/completions.py) (issue #2 — spec exists).
- Make fire-and-forget side effects observable (issue #4).
- Decompose admin page (issue #5).
- Routing config expressiveness (issue #6).
