"""``0011_trim_oversized_preferences``: drop the prober layouts no bounded write can have left."""

from __future__ import annotations

import importlib
import json

import pytest

from migrations.compat import read_state
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

PREVIOUS = "0010_storage_indexes"
REVISION = "0011_trim_oversized_preferences"

#: A layout as large as only a write made before the bound can have stored.
_HUGE_LAYOUT = {"direct_models": [f"m{i:07d}" for i in range(20_000)], "e2e_models": []}
_SMALL_LAYOUT = {"direct_models": ["glm-4.6", "qwen3.6-35b"], "e2e_models": []}


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _users(database: str, **preferences: dict) -> None:
    async with mig.connected(database) as conn:
        for user_id, document in preferences.items():
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, preferences) "
                "VALUES ($1, $1 || '@example.test', 'x', $2::jsonb)",
                user_id,
                json.dumps(document),
            )


async def _preferences(database: str) -> dict[str, dict]:
    async with mig.connected(database) as conn:
        return {
            r["id"]: json.loads(r["preferences"])
            for r in await conn.fetch("SELECT id, preferences FROM users")
        }


async def test_only_an_oversized_layout_goes_and_every_other_entry_stays(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    await _users(
        scratch,
        huge={
            "llm_prober_layout": _HUGE_LAYOUT,
            "disabled_models": ["m-off"],
            "locale": {"value": "zh-CN"},
        },
        small={"llm_prober_layout": _SMALL_LAYOUT, "disabled_models": ["m-off"]},
        empty={},
    )

    await mig.upgrade(scratch, REVISION)

    assert await _preferences(scratch) == {
        "huge": {"disabled_models": ["m-off"], "locale": {"value": "zh-CN"}},
        "small": {"llm_prober_layout": _SMALL_LAYOUT, "disabled_models": ["m-off"]},
        "empty": {},
    }
    async with mig.connected(scratch) as conn:
        assert (await read_state(conn)).revision == REVISION


async def test_its_statements_can_run_again(scratch):
    await mig.upgrade(scratch, PREVIOUS)
    await _users(scratch, huge={"llm_prober_layout": _HUGE_LAYOUT})
    await mig.upgrade(scratch, REVISION)
    revision = importlib.import_module(f"migrations.versions.{REVISION}")

    async with mig.connected(scratch) as conn:
        for statement in revision.STATEMENTS:
            assert await conn.execute(statement) == "UPDATE 0"
    assert await _preferences(scratch) == {"huge": {}}
