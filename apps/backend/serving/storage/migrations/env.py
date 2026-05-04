"""Alembic environment hook.

Sync SQLAlchemy is used here as the migration substrate only;
runtime application code stays on asyncpg.
"""

from __future__ import annotations

from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import create_engine, pool

from serving.config.settings import get_settings

# Alembic Config object — provides access to values in the .ini file.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Build the URL from settings rather than alembic.ini so secrets stay out
# of the committed config. ``psycopg2`` is the sync DB-API used by Alembic;
# runtime app code keeps using asyncpg.
settings = get_settings()
DB_URL = (
    f"postgresql+psycopg2://{quote_plus(settings.db_user)}"
    f":{quote_plus(settings.db_password)}"
    f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)

# No ORM models — migrations are handwritten using ``op.execute(...)``.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DB connection)."""
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (apply against a live DB)."""
    engine = create_engine(DB_URL, poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
