"""Versioned schema migrations for the gateway's PostgreSQL database.

The schema is created and changed here and nowhere else: the gateway checks the
database's version when it starts (:func:`migrations.compat.require_schema`) and
issues no DDL of its own, except the performance indexes
:mod:`serving.storage.perf_metrics_index` builds in the background. A deploy
applies the migrations as a step of its own, with the image it is about to run,
before it restarts the gateway::

    python -m migrations upgrade

``0001_baseline`` brings any existing database to the schema the gateway used to
build at startup, by running that code, frozen in :mod:`migrations.baseline`.
Every later change is a new revision in ``versions/``. The design is
``docs/agents/specs/2026-11-02-versioned-schema-migrations-design.zh.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config

#: Where Alembic records the revision a database is at. Not the default
#: ``alembic_version``: a database may carry another tool's table by that name.
VERSION_TABLE = "hybridinference_alembic_version"

#: The single row recording the oldest code revision this schema still supports.
COMPAT_TABLE = "hybridinference_schema_compat"

#: The script directory: ``env.py``, ``script.py.mako`` and ``versions/``.
MIGRATIONS_DIR = Path(__file__).resolve().parent

#: SQLSTATE of a ``lock_timeout`` that expired.
LOCK_NOT_AVAILABLE = "55P03"


class MigrationInProgress(RuntimeError):
    """Another migration run holds the database's migration lock."""


def sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE somewhere in *exc*'s chain of causes, if a driver reported one."""
    seen: BaseException | None = exc
    while seen is not None:
        state = getattr(seen, "sqlstate", None)
        if isinstance(state, str):
            return state
        seen = seen.__cause__ or getattr(seen, "orig", None)
    return None


def alembic_config(url: str | None = None) -> Config:
    """An Alembic configuration for this directory, in any layout.

    The repository's ``[tool.alembic]`` table serves the ``alembic`` command
    there; this one works the same from the repository and from the image,
    where the package sits next to ``serving``.

    Args:
        url: A SQLAlchemy URL to migrate. ``None`` reads the gateway's own
            ``DB_*`` settings.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        # An attribute, not a main option: the option goes through
        # ConfigParser interpolation, which a ``%`` in a password breaks.
        config.attributes["url"] = url
    return config
