"""The startup check: which schemas the gateway starts on, and what it says otherwise."""

from __future__ import annotations

import logging

import pytest

from migrations.compat import (
    UPGRADE_COMMAND,
    SchemaNotReady,
    SchemaState,
    Verdict,
    describe,
    judge,
    require_schema,
)

CHAIN = ("0001_baseline", "0002_add_note", "0003_stop_reading_tier")


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        (SchemaState(revision=None, floor=None), Verdict.NOT_MIGRATED),
        (SchemaState(revision="0003_stop_reading_tier", floor=None), Verdict.CURRENT),
        (SchemaState(revision="0001_baseline", floor=None), Verdict.BEHIND),
        # A rolled-back deploy: the database is newer and says this code still runs.
        (SchemaState(revision="0004_add_index", floor=None), Verdict.NEWER_COMPATIBLE),
        (
            SchemaState(revision="0005_drop_tier", floor="0003_stop_reading_tier"),
            Verdict.NEWER_COMPATIBLE,
        ),
        # Newer, and the floor names a revision this code does not have.
        (SchemaState(revision="0007_drop_note", floor="0006_stop_reading_note"), Verdict.TOO_NEW),
    ],
)
def test_judge(state, verdict):
    assert judge(state, CHAIN) is verdict


@pytest.mark.parametrize("verdict", [Verdict.NOT_MIGRATED, Verdict.BEHIND])
def test_a_missing_migration_names_the_command(verdict):
    state = SchemaState(revision="0001_baseline" if verdict is Verdict.BEHIND else None, floor=None)
    assert UPGRADE_COMMAND in describe(verdict, state, CHAIN)


def test_too_new_names_the_floor():
    state = SchemaState(revision="0007_drop_note", floor="0006_stop_reading_note")
    assert "0006_stop_reading_note" in describe(Verdict.TOO_NEW, state, CHAIN)


class _Catalog:
    """Answers :func:`migrations.compat.read_state`'s queries from fixed values."""

    def __init__(self, revision: str | None, floor: str | None) -> None:
        self.revision, self.floor = revision, floor

    async def fetchval(self, sql: str, *args: object) -> object:
        if "to_regclass" in sql:
            return None if self.revision is None else args[0]
        return self.floor

    async def fetch(self, sql: str) -> list[dict[str, str]]:
        return [] if self.revision is None else [{"version_num": self.revision}]


async def test_require_schema_starts_on_the_current_revision():
    state = await require_schema(_Catalog("0003_stop_reading_tier", None), chain=CHAIN)
    assert state.revision == "0003_stop_reading_tier"


async def test_require_schema_warns_on_a_newer_compatible_schema(caplog):
    with caplog.at_level(logging.WARNING, logger="migrations.compat"):
        await require_schema(_Catalog("0004_add_index", None), chain=CHAIN)
    assert "newer than this code" in caplog.text


@pytest.mark.parametrize(
    ("revision", "floor"),
    [(None, None), ("0002_add_note", None), ("0007_drop_note", "0006_stop_reading_note")],
)
async def test_require_schema_refuses(revision, floor):
    with pytest.raises(SchemaNotReady):
        await require_schema(_Catalog(revision, floor), chain=CHAIN)
