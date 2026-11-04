"""The versioned schema migrations against a real PostgreSQL server."""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config

from migrations import MIGRATIONS_DIR, baseline
from migrations.compat import SchemaState, Verdict, judge, read_state
from tests.fixtures import migrations as mig, schema_snapshot

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

#: These tests are about the baseline; later revisions have tests of their own.
BASELINE = "0001_baseline"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _describe(database: str) -> dict:
    async with mig.connected(database) as conn:
        return await schema_snapshot.describe(conn)


async def test_an_empty_database_migrates_to_the_recorded_baseline(scratch):
    await mig.upgrade(scratch, BASELINE)

    assert schema_snapshot.differences(schema_snapshot.load(), await _describe(scratch)) == []
    async with mig.connected(scratch) as conn:
        assert await read_state(conn) == SchemaState(revision="0001_baseline", floor=None)


async def test_a_database_startup_built_is_recorded_without_change(scratch):
    """Today's deployments: built by startup, never migrated. Nothing to stamp by hand."""
    async with mig.connected(scratch) as conn:
        await baseline.run(conn)
    before = await _describe(scratch)

    await mig.upgrade(scratch, BASELINE)

    assert schema_snapshot.differences(before, await _describe(scratch)) == []
    async with mig.connected(scratch) as conn:
        assert (await read_state(conn)).revision == "0001_baseline"


async def test_upgrading_again_changes_nothing(scratch):
    await mig.upgrade(scratch, BASELINE)
    before = await _describe(scratch)

    await mig.upgrade(scratch, BASELINE)

    assert await _describe(scratch) == before


def _script_directory(tmp_path: Path, revisions: list[tuple[str, str | None, str]]) -> Config:
    """A script directory with this package's ``env.py`` and the given small revisions."""
    root = tmp_path / "scripts"
    (root / "versions").mkdir(parents=True)
    shutil.copy(MIGRATIONS_DIR / "env.py", root / "env.py")
    shutil.copy(MIGRATIONS_DIR / "script.py.mako", root / "script.py.mako")
    for revision, down, body in revisions:
        (root / "versions" / f"{revision}.py").write_text(
            f'"""{revision}."""\n'
            "from alembic import op\n"
            "from migrations.compat import raise_compat_floor\n\n"
            f"revision = {revision!r}\ndown_revision = {down!r}\n"
            "branch_labels = None\ndepends_on = None\n\n\n"
            f"def upgrade():\n    {body}\n\n\ndef downgrade():\n    pass\n"
        )
    config = Config()
    config.set_main_option("script_location", str(root))
    return config


_FLOOR_CHAIN = [
    ("0001_base", None, 'op.execute("CREATE TABLE t (id INT PRIMARY KEY, legacy TEXT)")'),
    ("0002_stop_reading_legacy", "0001_base", "pass"),
    (
        "0003_drop_legacy",
        "0002_stop_reading_legacy",
        'op.execute("ALTER TABLE t DROP COLUMN legacy")\n'
        '    raise_compat_floor("0002_stop_reading_legacy", by=revision)',
    ),
    (
        "0004_name_an_older_floor",
        "0003_drop_legacy",
        'raise_compat_floor("0001_base", by=revision)',
    ),
]


async def test_a_contract_revision_raises_the_floor_that_older_code_reads(scratch, tmp_path):
    config = _script_directory(tmp_path, _FLOOR_CHAIN)
    config.attributes["url"] = mig.url(scratch)

    await asyncio.to_thread(command.upgrade, config, "head")

    async with mig.connected(scratch) as conn:
        state = await read_state(conn)
    # The later, lower floor does not lower it.
    assert state == SchemaState(
        revision="0004_name_an_older_floor", floor="0002_stop_reading_legacy"
    )
    # Code from before the marker would read the dropped column: it must not start.
    assert judge(state, ("0001_base",)) is Verdict.TOO_NEW
    # The release that stopped reading it is still supported: a rollback to it starts.
    assert judge(state, ("0001_base", "0002_stop_reading_legacy")) is Verdict.NEWER_COMPATIBLE


async def test_a_floor_must_name_an_earlier_revision(scratch, tmp_path):
    config = _script_directory(
        tmp_path,
        [
            ("0001_base", None, "pass"),
            ("0002_own_floor", "0001_base", 'raise_compat_floor("0002_own_floor", by=revision)'),
        ],
    )
    config.attributes["url"] = mig.url(scratch)

    with pytest.raises(ValueError, match="must be a revision before"):
        await asyncio.to_thread(command.upgrade, config, "head")

    async with mig.connected(scratch) as conn:
        assert await read_state(conn) == SchemaState(revision="0001_base", floor=None)
