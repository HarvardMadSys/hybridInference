"""Source versions for the database-backed configuration.

Every configuration source has one row in ``config_source_revisions``: an
``epoch`` and a ``revision``. The revision is advanced by a statement-level
``BEFORE`` trigger on every ``INSERT``, ``UPDATE``, ``DELETE`` and ``TRUNCATE``
of the source table, inside the writing transaction, so it covers every write
path — the admin API, the recovery CLI, the boot import, generated secrets,
reset and delete, backup restore and plain SQL alike — and a rolled-back write
leaves no version behind. A statement that changes nothing still advances it,
and an upsert (``INSERT ... ON CONFLICT DO UPDATE``) fires both the insert and
the update trigger, so a revision may skip; readers compare what they project
from the rows instead of trusting a bump.

The epoch names one continuous history of the source. It changes only through
:func:`reset_epoch`, the command an operator runs after restoring the whole
database, when an older revision coming back is expected rather than a fault.

The row also holds the source's ``contracts``: non-secret metadata naming each
managed configuration group, its schema version, the keys it owns and whether
an operator has activated it. Creating the schema never activates anything;
:func:`set_group_active` is the explicit deployment command, and it advances
the revision in the same transaction.

Nothing here stores a configuration value, a secret, or anything derived from
one. Application settings use ``app_config``; the ``routing`` source covers
runtime provider, key and model edits across the routing tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The source name of the ``app_config`` table.
APP_CONFIG_SOURCE = "app_config"

#: A fresh epoch, generated in the database: 16 hex characters.
_NEW_EPOCH_SQL = "substr(md5(random()::text || clock_timestamp()::text), 1, 16)"

# The base record: revision 0, no active group. Never overwrites an existing
# row, so a restart cannot reset the history or the contracts.
SEED_REVISION_SQL = f"""
INSERT INTO config_source_revisions (source, epoch, revision, contracts)
VALUES ($1, {_NEW_EPOCH_SQL}, 0, '{{}}'::jsonb)
ON CONFLICT (source) DO NOTHING
"""

SELECT_VERSION_SQL = (
    "SELECT epoch, revision, contracts FROM config_source_revisions WHERE source = $1"
)

LOCK_VERSION_SQL = f"{SELECT_VERSION_SQL} FOR UPDATE"

UPDATE_CONTRACTS_SQL = """
UPDATE config_source_revisions
SET contracts = $2::jsonb, revision = revision + 1, updated_at = NOW()
WHERE source = $1
RETURNING epoch, revision
"""

RESET_EPOCH_SQL = f"""
UPDATE config_source_revisions
SET epoch = {_NEW_EPOCH_SQL}, revision = revision + 1, updated_at = NOW()
WHERE source = $1
RETURNING epoch, revision
"""


@dataclass(frozen=True, order=True)
class SourceVersion:
    """Where a source's history stands: an epoch and a revision within it."""

    epoch: str
    revision: int

    @property
    def etag(self) -> str:
        """The version as an HTTP entity tag: ``"<epoch>:<revision>"``."""
        return f'"{self.epoch}:{self.revision}"'

    @classmethod
    def parse(cls, text: str) -> SourceVersion:
        """Parse an ``If-Match`` value: a quoted or bare ``<epoch>:<revision>``.

        Raises:
            ValueError: The text is not a version.
        """
        value = text.strip()
        if value.startswith("W/"):
            raise ValueError("a weak entity tag is not a version")
        value = value.strip('"')
        epoch, sep, revision = value.rpartition(":")
        if not sep or not epoch or not revision.isdigit():
            raise ValueError(f"not a configuration version: {text!r}")
        return cls(epoch, int(revision))

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form the admin API shows."""
        return {"epoch": self.epoch, "revision": self.revision}


@dataclass(frozen=True)
class GroupContract:
    """One managed group as the persisted contracts describe it."""

    name: str
    schema_version: int
    keys: tuple[str, ...]
    active: bool = False
    changed_at: str | None = None
    changed_by: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "schema_version": self.schema_version,
            "keys": list(self.keys),
            "active": self.active,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }


@dataclass(frozen=True)
class Contracts:
    """The persisted contracts of one source: its managed groups by name."""

    groups: Mapping[str, GroupContract] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Contracts:
        """Read the stored JSON; anything malformed is left out, never guessed at."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return cls()
        if not isinstance(raw, dict):
            return cls()
        groups: dict[str, GroupContract] = {}
        for name, body in (raw.get("groups") or {}).items():
            if not isinstance(name, str) or not isinstance(body, dict):
                continue
            version = body.get("schema_version")
            keys = body.get("keys")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                continue
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                continue
            groups[name] = GroupContract(
                name,
                version,
                tuple(keys),
                active=body.get("active") is True,
                changed_at=body.get("changed_at")
                if isinstance(body.get("changed_at"), str)
                else None,
                changed_by=body.get("changed_by")
                if isinstance(body.get("changed_by"), str)
                else None,
            )
        return cls(groups)

    def as_json(self) -> dict[str, Any]:
        """Return the stored form."""
        return {"groups": {name: group.as_json() for name, group in sorted(self.groups.items())}}


@dataclass(frozen=True)
class SourceSnapshot:
    """A source's rows and its version, read in one database snapshot."""

    source: str
    #: ``None`` only when the version row is missing (a database restored from
    #: before this table existed); the rows are still a consistent read.
    version: SourceVersion | None
    contracts: Contracts
    #: Raw rows, as the store returns them.
    rows: Mapping[str, Any]


class VersionConflict(Exception):
    """A conditional write named a version that is no longer current."""

    def __init__(self, expected: SourceVersion, current: SourceVersion | None) -> None:
        super().__init__(f"expected {expected.etag}, current {current.etag if current else None}")
        self.expected = expected
        self.current = current


def _version(record: Any) -> SourceVersion | None:
    if record is None:
        return None
    return SourceVersion(record["epoch"], int(record["revision"]))


async def read_version(
    conn: Any, source: str = APP_CONFIG_SOURCE
) -> tuple[SourceVersion | None, Contracts]:
    """Read a source's version and contracts on *conn*, in its current transaction."""
    record = await conn.fetchrow(SELECT_VERSION_SQL, source)
    if record is None:
        return None, Contracts()
    return _version(record), Contracts.parse(record["contracts"])


async def lock_version(
    conn: Any, source: str = APP_CONFIG_SOURCE
) -> tuple[SourceVersion, Contracts]:
    """Lock the version row for the rest of the transaction, creating it when missing.

    Every write to the source advances this row from its trigger, so holding
    the lock also holds off a concurrent plain-SQL writer until commit.
    """
    record = await conn.fetchrow(LOCK_VERSION_SQL, source)
    if record is None:
        await conn.execute(SEED_REVISION_SQL, source)
        record = await conn.fetchrow(LOCK_VERSION_SQL, source)
    version = _version(record)
    assert version is not None
    return version, Contracts.parse(record["contracts"])


def check_expected(expected: SourceVersion | None, current: SourceVersion) -> None:
    """Refuse a conditional write whose version is not the current one.

    Raises:
        VersionConflict: *expected* is set and differs from *current*.
    """
    if expected is not None and expected != current:
        raise VersionConflict(expected, current)


async def set_group_active(
    conn: Any,
    contract: GroupContract,
    *,
    active: bool,
    expected: SourceVersion | None,
    changed_by: str,
    source: str = APP_CONFIG_SOURCE,
) -> tuple[SourceVersion, Contracts]:
    """Activate or deactivate one managed group, advancing the revision.

    Runs in the caller's transaction, which must hold the configuration write
    lock. The stored contract takes *contract*'s schema version and keys: the
    command is how a group's protocol is upgraded as well.

    Raises:
        VersionConflict: *expected* is not the current version.
    """
    import datetime as dt

    current, contracts = await lock_version(conn, source)
    check_expected(expected, current)
    groups = dict(contracts.groups)
    groups[contract.name] = GroupContract(
        contract.name,
        contract.schema_version,
        tuple(contract.keys),
        active=active,
        changed_at=dt.datetime.now(dt.UTC).isoformat(),
        changed_by=changed_by,
    )
    updated = Contracts(groups)
    record = await conn.fetchrow(UPDATE_CONTRACTS_SQL, source, json.dumps(updated.as_json()))
    version = _version(record)
    assert version is not None
    return version, updated


async def reset_epoch(
    conn: Any, *, expected: SourceVersion | None, source: str = APP_CONFIG_SOURCE
) -> SourceVersion:
    """Start a new epoch after a whole-database restore; return the new version.

    Every instance registration is removed in the same transaction: a restored
    copy cannot prove which processes are serving now, so each running process
    registers again on its next heartbeat and reports against the new epoch.

    Raises:
        VersionConflict: *expected* is not the current version.
    """
    from serving.config.instances import DELETE_ALL_INSTANCES_SQL

    current, _ = await lock_version(conn, source)
    check_expected(expected, current)
    record = await conn.fetchrow(RESET_EPOCH_SQL, source)
    await conn.execute(DELETE_ALL_INSTANCES_SQL)
    version = _version(record)
    assert version is not None
    return version
