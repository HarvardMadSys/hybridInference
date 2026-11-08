"""Legacy route-catalog helpers retained for explicit previous-release tooling.

The gateway no longer calls this module. Migration 0012 preserves existing rows
as application settings; normal runtime configuration lives in app_config and
serving.config.documents. No application files are imported at startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

MODELS = "models"
ROUTING = "routing"
KINDS = (MODELS, ROUTING)

#: Boot import. A row from any other source is left untouched, same as one an
#: operator has already stored.
SOURCE_YAML_IMPORT = "yaml_import"

SELECT_SQL = "SELECT kind, body FROM route_catalog"

# Several workers boot at once. The lock makes the "missing?" check and the
# insert one decision, and ON CONFLICT keeps a row that arrived first.
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hybridinference:route-catalog'))"

INSERT_SQL = """
INSERT INTO route_catalog (kind, body, source, updated_by)
VALUES ($1, $2, $3, $4)
ON CONFLICT (kind) DO NOTHING
"""

# None: this process has not read the table. A dict, possibly empty: it has,
# and a missing kind still comes from the file.
_installed: dict[str, str] | None = None


def reset_installed() -> None:
    """Forget a catalog read earlier in this process.

    Database-free mode calls this so a test, or a process that lost the
    database, does not keep registering a document from a previous attempt.
    """
    global _installed
    _installed = None


def install(bodies: Mapping[str, str]) -> None:
    """Remember the documents this process should register.

    Args:
        bodies: Stored documents keyed by :data:`KINDS`. Kinds that are absent
            are not stored and continue to come from the file.
    """
    global _installed
    _installed = {kind: bodies[kind] for kind in KINDS if kind in bodies}


def installed_body(kind: str) -> str | None:
    """Return the stored document for *kind*, or None to read the file.

    A stored empty string is returned as ``""``. That is a real row, and the
    file is not a fallback for it.
    """
    if _installed is None:
        return None
    return _installed.get(kind)


async def import_missing(conn: Any, texts: Mapping[str, str]) -> dict[str, str]:
    """Store each missing kind whose text is non-empty, then return every row.

    An existing row is returned unchanged, including a blank one. A blank seed
    is not inserted, so a deployment with no routing file grows no routing row
    and keeps the "no routing config" path.

    Args:
        conn: An asyncpg connection. The caller owns its lifetime.
        texts: Explicit legacy documents supplied by the caller.

    Returns:
        The stored documents keyed by kind, after this import.
    """
    inserted: list[str] = []
    async with conn.transaction():
        await conn.execute(LOCK_SQL)
        current = {record["kind"]: record["body"] for record in await conn.fetch(SELECT_SQL)}
        for kind in KINDS:
            body = texts.get(kind)
            if kind in current or body is None or not str(body).strip():
                continue
            await conn.execute(INSERT_SQL, kind, body, SOURCE_YAML_IMPORT, "init")
            current[kind] = body
            inserted.append(kind)
    if inserted:
        logger.info("Imported %s into route_catalog", ", ".join(inserted))
    elif current:
        logger.info(
            "Route catalog loaded from the database (%s)",
            ", ".join(sorted(kind for kind in current if kind in KINDS)),
        )
    return {kind: body for kind, body in current.items() if kind in KINDS}
