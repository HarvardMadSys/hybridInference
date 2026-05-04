# Alembic Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt Alembic for the Postgres backend's schema management, replacing the current idempotent `CREATE TABLE IF NOT EXISTS` init pattern with versioned, deploy-time migrations + a startup version-check guard.

**Architecture:** One Alembic chain per Postgres DB (covers both operational + log schemas). Sync SQLAlchemy engine for migrations only (runtime stays asyncpg). Single baseline migration captures the current schema; existing DBs get `alembic stamp 0001_baseline` once at cut-over. Deploy scripts run `alembic upgrade head` before container restart; gateway boot fails fast on version mismatch.

**Tech Stack:** Python 3.12, Alembic ≥ 1.13, sync SQLAlchemy (substrate only), asyncpg (runtime), pytest + pytest-asyncio (auto mode), uv, ruff.

**Spec:** [docs/agents/specs/2026-05-03-alembic-migrations-design.md](../specs/2026-05-03-alembic-migrations-design.md)

**Process notes (from CLAUDE.md):**
- Pull `origin/dev` before starting.
- Single PR on feature branch `jason/claude/alembic-migrations`.
- Worktree: `/home/juncheng/hybridInference-worktrees/alembic-migrations`.
- Per CLAUDE.md: create issue → branch → implement → `make format` → PR → monitor CI every 2 min → cleanup after merge.

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `alembic.ini` | Repo-root Alembic config; points `script_location = apps/backend/serving/storage/migrations`; defers DB URL to `env.py`. |
| `apps/backend/serving/storage/migrations/env.py` | Alembic env hook; reads DB URL from `serving.config.settings.get_settings()`; creates sync SQLAlchemy engine. |
| `apps/backend/serving/storage/migrations/script.py.mako` | Standard Alembic template (copy from `alembic init` output). |
| `apps/backend/serving/storage/migrations/versions/0001_baseline.py` | Full current schema as `op.execute("CREATE TABLE IF NOT EXISTS ...")`. `downgrade` raises `NotImplementedError`. |
| `apps/backend/serving/storage/_expected_alembic_version.py` | `EXPECTED_ALEMBIC_VERSION = "0001_baseline"` — bumped per schema-change PR. |
| `tests/integration/storage/test_migrations.py` | DB-roundtrip tests via `dbtest` marker. |
| `tests/unit/servers/test_schema_version_check.py` | Tests for the `_verify_schema_version` boot guard. |

### Modified files

| File | Change |
|---|---|
| `pyproject.toml` | Add `alembic>=1.13` (pulls SQLAlchemy as substrate). |
| `apps/backend/serving/servers/bootstrap.py` | Add `_verify_schema_version(pool)`; call it early in `initialize()`. Skip when `settings.db_backend != "postgres"`. |
| `apps/backend/serving/storage/postgres_log.py` | Delete `CREATE TABLE` + `CREATE INDEX` blocks at lines 49-100 (Alembic owns now). |
| `apps/backend/serving/storage/postgres_operational.py` | Delete equivalent `CREATE TABLE`/`CREATE INDEX` init blocks. |
| `Makefile` | Add `migrate`, `migrate-down`, `migrate-new NAME=<name>`, `migrate-status` targets. Update `setup-dev` to call `migrate` after DB is reachable. |
| `scripts/deploy_production.sh` | Add `docker compose run --rm backend uv run alembic upgrade head` step before `docker compose up`. Abort deploy on failure. |
| `scripts/deploy_staging.sh` | Same as production. |
| `.github/workflows/ci.yml` | Add migration step to `Test` job before pytest. |

---

## Tasks

### Task 1: Worktree + issue setup

- [ ] **Step 1:** `git fetch origin && git checkout dev && git pull origin dev`
- [ ] **Step 2:** Create issue: `gh issue create --title "Adopt Alembic migrations for Postgres schema" --body "Spec: docs/agents/specs/2026-05-03-alembic-migrations-design.md\nPlan: docs/agents/plans/2026-05-03-alembic-migrations.md"`
- [ ] **Step 3:** `git worktree add /home/juncheng/hybridInference-worktrees/alembic-migrations -b jason/claude/alembic-migrations origin/dev`
- [ ] **Step 4:** `cd /home/juncheng/hybridInference-worktrees/alembic-migrations && make test 2>&1 | tail -3` to confirm clean baseline.

---

### Task 2: Add Alembic dependency

**Files:** Modify `pyproject.toml`.

- [ ] **Step 1:** `cd /home/juncheng/hybridInference-worktrees/alembic-migrations && uv add alembic`
- [ ] **Step 2:** Verify: `uv pip list | grep -E "^alembic|^SQLAlchemy"`. Expected: both present.
- [ ] **Step 3:** Commit:
  ```bash
  git add pyproject.toml uv.lock
  git commit -m "deps: add alembic for Postgres schema migrations"
  ```

---

### Task 3: Initialize Alembic structure

**Files:**
- Create: `alembic.ini`, `apps/backend/serving/storage/migrations/env.py`, `apps/backend/serving/storage/migrations/script.py.mako`, `apps/backend/serving/storage/migrations/versions/.gitkeep`

- [ ] **Step 1:** Run `cd /home/juncheng/hybridInference-worktrees/alembic-migrations && uv run alembic init apps/backend/serving/storage/migrations` to scaffold. Move `alembic.ini` from `apps/backend/serving/storage/migrations/` (where init may place it) to repo root if needed.

- [ ] **Step 2:** Edit `alembic.ini` so:
  - `script_location = apps/backend/serving/storage/migrations`
  - Comment out the default `sqlalchemy.url` line (URL is supplied by `env.py`).

- [ ] **Step 3:** Replace `apps/backend/serving/storage/migrations/env.py` with:

```python
"""Alembic environment hook.

Sync SQLAlchemy is used here as the migration substrate only;
runtime app code stays on asyncpg.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from serving.config.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
DB_URL = (
    f"postgresql://{settings.db_user}:{settings.db_password}"
    f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(DB_URL, poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4:** Verify: `cd /home/juncheng/hybridInference-worktrees/alembic-migrations && uv run alembic check 2>&1 | head`. Expected: no syntax errors (the absence of revisions is fine).

- [ ] **Step 5:** Commit:
  ```bash
  git add alembic.ini apps/backend/serving/storage/migrations/
  git commit -m "feat(storage): scaffold Alembic structure (env.py, alembic.ini, versions/)"
  ```

---

### Task 4: Create the baseline migration

**Files:** Create `apps/backend/serving/storage/migrations/versions/0001_baseline.py`.

- [ ] **Step 1:** Open `apps/backend/serving/storage/postgres_log.py` and `apps/backend/serving/storage/postgres_operational.py`. Find every `CREATE TABLE IF NOT EXISTS ...` and `CREATE INDEX ...` statement. Note line ranges.

- [ ] **Step 2:** Generate revision file: `cd /home/juncheng/hybridInference-worktrees/alembic-migrations && uv run alembic revision -m "baseline" --rev-id 0001_baseline`. This creates `apps/backend/serving/storage/migrations/versions/0001_baseline_baseline.py`.

- [ ] **Step 3:** Edit the generated file. Set `revision = "0001_baseline"`, `down_revision = None`, `branch_labels = None`, `depends_on = None`. Replace `upgrade()` body with `op.execute("...")` calls — one per `CREATE TABLE IF NOT EXISTS ...` and one per `CREATE INDEX IF NOT EXISTS ...` from the source files. Preserve `IF NOT EXISTS` for defense in depth. Replace `downgrade()` with `raise NotImplementedError("baseline migration is irreversible")`.

  Sketch:
  ```python
  """baseline

  Revision ID: 0001_baseline
  Revises:
  Create Date: 2026-05-03
  """
  from alembic import op

  revision = "0001_baseline"
  down_revision = None
  branch_labels = None
  depends_on = None


  def upgrade() -> None:
      op.execute("""
          CREATE TABLE IF NOT EXISTS api_logs (
              -- ... copy from postgres_log.py:49-... verbatim ...
          )
      """)
      op.execute("CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC)")
      # ... copy every CREATE TABLE / CREATE INDEX from postgres_log.py and postgres_operational.py ...


  def downgrade() -> None:
      raise NotImplementedError("baseline migration is irreversible")
  ```

- [ ] **Step 4:** Verify against fresh DB: spin up an empty Postgres locally (use the existing test-DB pattern), run `uv run alembic upgrade head`, then `\d` (or `psql ... -c "\d"`) and confirm every table and index exists.

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/backend/serving/storage/migrations/versions/0001_baseline_baseline.py
  git commit -m "feat(storage): baseline migration with current schema"
  ```

---

### Task 5: Add `_expected_alembic_version` constant

**Files:** Create `apps/backend/serving/storage/_expected_alembic_version.py`.

- [ ] **Step 1:** Write file:
  ```python
  """Pinned to the head migration revision; bumped per schema-change PR.

  apps/backend/serving/servers/bootstrap.py reads this to verify the DB matches the
  code's expected schema version on startup.
  """
  EXPECTED_ALEMBIC_VERSION = "0001_baseline"
  ```

- [ ] **Step 2:** Commit:
  ```bash
  git add apps/backend/serving/storage/_expected_alembic_version.py
  git commit -m "feat(storage): add EXPECTED_ALEMBIC_VERSION pin"
  ```

---

### Task 6: Add `_verify_schema_version` boot guard

**Files:**
- Modify: `apps/backend/serving/servers/bootstrap.py`
- Test: `tests/unit/servers/test_schema_version_check.py`

- [ ] **Step 1:** Write failing test:

  ```python
  """Tests for _verify_schema_version boot guard."""
  from unittest.mock import AsyncMock, MagicMock
  import asyncpg
  import pytest

  from serving.servers.bootstrap import _verify_schema_version


  @pytest.mark.asyncio
  async def test_verify_schema_version_passes_when_matched(monkeypatch):
      monkeypatch.setattr(
          "serving.storage._expected_alembic_version.EXPECTED_ALEMBIC_VERSION",
          "0001_baseline",
      )
      conn = MagicMock()
      conn.fetchval = AsyncMock(return_value="0001_baseline")
      pool = MagicMock()
      pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
      pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
      settings = MagicMock(db_backend="postgres")

      # Should not raise.
      await _verify_schema_version(pool, settings)


  @pytest.mark.asyncio
  async def test_verify_schema_version_raises_on_mismatch(monkeypatch):
      monkeypatch.setattr(
          "serving.storage._expected_alembic_version.EXPECTED_ALEMBIC_VERSION",
          "0002_add_foo",
      )
      conn = MagicMock()
      conn.fetchval = AsyncMock(return_value="0001_baseline")
      pool = MagicMock()
      pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
      pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
      settings = MagicMock(db_backend="postgres")

      with pytest.raises(RuntimeError, match="Schema version mismatch"):
          await _verify_schema_version(pool, settings)


  @pytest.mark.asyncio
  async def test_verify_schema_version_raises_on_missing_table():
      conn = MagicMock()
      conn.fetchval = AsyncMock(side_effect=asyncpg.UndefinedTableError("rel does not exist"))
      pool = MagicMock()
      pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
      pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
      settings = MagicMock(db_backend="postgres")

      with pytest.raises(RuntimeError, match="alembic_version table missing"):
          await _verify_schema_version(pool, settings)


  @pytest.mark.asyncio
  async def test_verify_schema_version_skips_when_not_postgres():
      pool = MagicMock()
      settings = MagicMock(db_backend="d1")
      # Should be a no-op (no acquire call).
      await _verify_schema_version(pool, settings)
      pool.acquire.assert_not_called()
  ```

- [ ] **Step 2:** Run: `uv run pytest tests/unit/servers/test_schema_version_check.py -v`. Expected: ImportError.

- [ ] **Step 3:** Add to `apps/backend/serving/servers/bootstrap.py`:

  ```python
  import asyncpg

  async def _verify_schema_version(pool, settings) -> None:
      """Fail fast on boot if the DB schema version doesn't match the code.

      Skips when the backend is not Postgres (D1 backend stays on its
      static-SQL pattern).
      """
      if settings.db_backend != "postgres":
          return
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

  Then call `_verify_schema_version(pool, settings)` early in `initialize()` after the connection pool is created but before any other DB-touching work.

- [ ] **Step 4:** Run: `uv run pytest tests/unit/servers/test_schema_version_check.py -v`. Expected: 4 passed.

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/backend/serving/servers/bootstrap.py tests/unit/servers/test_schema_version_check.py
  git commit -m "feat(storage): boot guard verifying alembic schema version"
  ```

---

### Task 7: Delete old init code

**Files:**
- Modify: `apps/backend/serving/storage/postgres_log.py` (delete `CREATE TABLE`/`CREATE INDEX` blocks at lines 49-100 — exact range may have shifted; grep first).
- Modify: `apps/backend/serving/storage/postgres_operational.py` (delete equivalent init blocks).

- [ ] **Step 1:** Grep first: `grep -n "CREATE TABLE\|CREATE INDEX" apps/backend/serving/storage/postgres_log.py apps/backend/serving/storage/postgres_operational.py`. Note line ranges.

- [ ] **Step 2:** Delete the blocks. Keep any function signatures intact (e.g., if `_init_schema()` exists, replace its body with a `pass` or remove the function and its caller).

- [ ] **Step 3:** Find any callers of the removed init functions (`grep -rn "_init_schema\|init_tables" apps/backend/serving/`). Remove the calls or replace with a comment noting Alembic owns schema now.

- [ ] **Step 4:** Run full test suite: `make test 2>&1 | tail -10`. Tests touching DB will use the test fixture which already creates schema (need to wire that to Alembic — see Task 9).

- [ ] **Step 5:** Commit:
  ```bash
  git add apps/backend/serving/storage/postgres_log.py apps/backend/serving/storage/postgres_operational.py
  git commit -m "refactor(storage): delete CREATE TABLE init blocks (Alembic owns schema)"
  ```

---

### Task 8: Per-migration roundtrip test

**Files:** Create `tests/integration/storage/test_migrations.py`.

- [ ] **Step 1:** Write test:

  ```python
  """Per-migration roundtrip — confirm baseline produces a usable schema and
  (where applicable) downgrade-then-upgrade is reversible."""
  import os
  import subprocess

  import pytest

  pytestmark = pytest.mark.dbtest


  def _alembic(*args: str) -> None:
      """Run alembic with the test DB env."""
      env = os.environ.copy()
      subprocess.run(
          ["uv", "run", "alembic", *args],
          env=env, check=True,
      )


  def test_baseline_upgrade_creates_all_tables(test_db_pool):
      """Apply head migration to a fresh DB and verify key tables exist."""
      _alembic("upgrade", "head")
      # Probe: every table named in the baseline migration should exist.
      expected_tables = {
          "users", "api_keys", "auth_sessions",
          "email_verification_tokens", "password_reset_tokens",
          "admin_audit_log", "user_daily_cost", "signup_allowed_domains",
          "api_logs", "alembic_version",
      }
      async def check():
          async with test_db_pool.acquire() as conn:
              rows = await conn.fetch(
                  "SELECT tablename FROM pg_tables WHERE schemaname='public'"
              )
          actual = {r["tablename"] for r in rows}
          missing = expected_tables - actual
          assert not missing, f"Missing tables: {missing}"
      import asyncio
      asyncio.run(check())
  ```

  (Adapt the `test_db_pool` fixture to whatever's already in `tests/conftest.py` or `tests/integration/conftest.py`.)

- [ ] **Step 2:** Run with the test DB: `cd /home/juncheng/hybridInference-worktrees/alembic-migrations && uv run pytest tests/integration/storage/test_migrations.py -v -m dbtest`. Expected: pass.

- [ ] **Step 3:** Commit:
  ```bash
  git add tests/integration/storage/test_migrations.py
  git commit -m "test(storage): per-migration roundtrip via dbtest marker"
  ```

---

### Task 9: Makefile targets + setup-dev integration

**Files:** Modify `Makefile`.

- [ ] **Step 1:** Add at the bottom of `Makefile`:
  ```makefile
  .PHONY: migrate migrate-down migrate-new migrate-status

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

- [ ] **Step 2:** Locate the `setup-dev` target. Add a `make migrate` step at the end (only run if DB env vars are set):
  ```makefile
  setup-dev: ...
  	# ... existing steps ...
  	@if [ -n "$$DB_NAME" ]; then $(MAKE) migrate; fi
  ```

- [ ] **Step 3:** Verify: `make migrate-status` prints `0001_baseline` (assuming local DB is migrated).

- [ ] **Step 4:** Commit:
  ```bash
  git add Makefile
  git commit -m "feat(storage): make targets for migrate/migrate-down/migrate-new/migrate-status"
  ```

---

### Task 10: Deploy script integration

**Files:** Modify `scripts/deploy_production.sh`, `scripts/deploy_staging.sh`.

- [ ] **Step 1:** In each deploy script, find where `docker compose ... up` is called. Insert *before* it:
  ```bash
  echo ">> Running database migrations..."
  docker compose -f infrastructure/docker/docker-compose.yml run --rm backend \
      uv run alembic upgrade head
  if [ $? -ne 0 ]; then
      echo "Migration failed; aborting deploy"
      exit 1
  fi
  ```

- [ ] **Step 2:** Test locally (if possible): run the modified script up to the migration step against a staging-like DB; verify the `alembic upgrade head` invocation completes.

- [ ] **Step 3:** Commit:
  ```bash
  git add scripts/deploy_production.sh scripts/deploy_staging.sh
  git commit -m "feat(deploy): run alembic upgrade head before container restart"
  ```

---

### Task 11: CI integration

**Files:** Modify `.github/workflows/ci.yml`.

- [ ] **Step 1:** Locate the `Test` job. Add a step *before* the `pytest` step:
  ```yaml
        - name: Apply migrations to test database
          run: uv run alembic upgrade head
          env:
            DB_HOST: localhost
            DB_PORT: 5433
            DB_USER: ${{ env.DB_USER }}
            DB_PASSWORD: ${{ env.DB_PASSWORD }}
            DB_NAME: ${{ env.DB_NAME }}
  ```

  (Match the env-var names already used in the Test job.)

- [ ] **Step 2:** Commit:
  ```bash
  git add .github/workflows/ci.yml
  git commit -m "ci: apply alembic migrations before pytest"
  ```

---

### Task 12: PR finalization

- [ ] **Step 1:** `make format && make test 2>&1 | tail -5`. Confirm green.

- [ ] **Step 2:** Push: `git push -u origin jason/claude/alembic-migrations`.

- [ ] **Step 3:** Open PR:
  ```bash
  gh pr create --base dev --title "Adopt Alembic for Postgres schema migrations" \
    --body "$(cat <<'EOF'
  ## Summary

  Replaces the `CREATE TABLE IF NOT EXISTS`-on-startup pattern with versioned Alembic migrations for the Postgres backend. D1 stays on its static-SQL pattern (out of scope).

  - Single migration chain covers operational + log schemas (one DB).
  - Baseline migration captures current schema.
  - Deploy scripts run \`alembic upgrade head\` before container restart; gateway boot fails fast on schema-version mismatch.
  - D1 backend untouched.

  Spec: [docs/agents/specs/2026-05-03-alembic-migrations-design.md](docs/agents/specs/2026-05-03-alembic-migrations-design.md)

  ## One-time cut-over (post-merge, before next deploy)

  Operator runs once on each environment:

  \`\`\`bash
  uv run alembic stamp 0001_baseline
  \`\`\`

  Verify with \`uv run alembic current\` (expects \`0001_baseline (head)\`).

  ## Test plan

  - [x] Unit tests for \`_verify_schema_version\` (4 tests)
  - [x] Integration test: \`alembic upgrade head\` builds expected tables on fresh DB
  - [x] CI applies migrations before pytest
  - [x] Full test suite passes
  - [ ] Manual: on staging, \`pg_dump --schema-only\` of prod ≈ \`pg_dump --schema-only\` of locally-built schema (modulo \`alembic_version\` table)

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

- [ ] **Step 4:** Monitor CI + comments every 2 min until merged (per CLAUDE.md).

- [ ] **Step 5:** After merge:
  - Operator runs `alembic stamp 0001_baseline` on staging, then prod.
  - Cleanup: `git worktree remove /home/juncheng/hybridInference-worktrees/alembic-migrations && git branch -D jason/claude/alembic-migrations`.

---

## Self-Review Checklist (post-implementation)

- [ ] `pyproject.toml` has `alembic>=1.13`.
- [ ] `alembic.ini` exists at repo root; `apps/backend/serving/storage/migrations/` has `env.py`, `script.py.mako`, `versions/0001_baseline_baseline.py`.
- [ ] `apps/backend/serving/storage/_expected_alembic_version.py` pins the head revision.
- [ ] `apps/backend/serving/servers/bootstrap.py` calls `_verify_schema_version(pool, settings)` early in `initialize()`.
- [ ] No `CREATE TABLE IF NOT EXISTS` blocks remain in `apps/backend/serving/storage/postgres_log.py` or `apps/backend/serving/storage/postgres_operational.py`.
- [ ] `make migrate` / `migrate-down` / `migrate-new` / `migrate-status` targets work locally.
- [ ] Both deploy scripts run `alembic upgrade head` before `docker compose up`.
- [ ] CI applies migrations before pytest.
- [ ] `make test` passes locally.
- [ ] Manual sanity check: `pg_dump --schema-only` of an Alembic-built local DB matches a snapshot of prod schema (modulo `alembic_version`).
- [ ] PR description documents the operator's one-time `alembic stamp` step.
