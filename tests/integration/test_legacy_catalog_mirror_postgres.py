"""The rollback mirror: route_catalog only ever holds what the previous release can run.

``PREVIOUS_GATEWAY_SOURCE`` (a checkout of the previous release) additionally
registers each mirrored catalog with that release's own registry, in a process
of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import yaml

from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

LEGACY = (
    "models:\n  - id: legacy\n    name: Legacy\n    provider: openai_compat\n"
    "    base_url: http://127.0.0.1:9/v1\n    api_key: legacy-key\n"
)


async def legacy_body(conn, kind="models"):
    return await conn.fetchval("SELECT body FROM route_catalog WHERE kind=$1", kind)


async def save(conn, key, value, *, source="admin"):
    await conn.execute(
        "INSERT INTO app_config(key,value,secret,source) VALUES ($1,$2,false,$3) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, source=EXCLUDED.source",
        key,
        value,
        source,
    )


async def upgraded_with_legacy_catalog(database):
    """A database the previous release used, upgraded: its catalog copied and lifted."""
    await mig.upgrade(database, "0011_trim_oversized_preferences")
    async with mig.connected(database) as conn:
        await conn.execute(
            "INSERT INTO route_catalog(kind,body,source) VALUES ('models',$1,'yaml_import')",
            LEGACY,
        )
    await mig.upgrade(database)
    async with mig.connected(database) as conn:
        catalog = json.loads(
            await conn.fetchval("SELECT value FROM app_config WHERE key='MODEL_CATALOG'")
        )
    # The lifted credential's setting, which the previous release resolves too.
    return catalog["models"][0]["api_key"][2:-1]


def previous_registry(body, settings):
    """Register *body* with the previous release's registry; ``None`` without its source."""
    root = os.environ.get("PREVIOUS_GATEWAY_SOURCE")
    if not root:
        return None
    code = (
        "import json, sys\n"
        "from routing.routers import FixedRouter\n"
        "from serving.servers.registry import register_from_models_text\n"
        "router = FixedRouter()\n"
        "register_from_models_text(router, sys.stdin.read())\n"
        "print(json.dumps({model: [(adapter.config.api_key, adapter.config.extra_body)\n"
        "    for adapter, _ in route.adapters] for model, route in router.routes.items()}))\n"
    )
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    environment.update(settings, PYTHONPATH=os.path.join(root, "apps/backend"), DB_ENABLED="false")
    done = subprocess.run(
        [sys.executable, "-c", code],
        input=body,
        capture_output=True,
        text=True,
        env=environment,
        cwd=root,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


async def test_a_document_saved_as_typed_is_mirrored_as_canonical_yaml_readable_json():
    async with mig.scratch_database() as database:
        secret = await upgraded_with_legacy_catalog(database)
        document = {
            "models": [
                {
                    "id": "saved",
                    # Characters YAML 1.1 refuses raw, or folds, beside ordinary ones.
                    "name": "Caf\u00e9 \u2615 \u0085\u2028\u007f\u0090",
                    "provider": "openai_compat",
                    "base_url": "http://127.0.0.1:9/v1",
                    "api_key": "${" + secret + "}",
                    # Dumped as 1e-07, which YAML 1.1 reads as a string.
                    "weight": 1e-07,
                }
            ]
        }
        async with mig.connected(database) as conn:
            # Valid JSON, and a setting Admin accepts: tabs are a YAML scanner error.
            await save(conn, "MODEL_CATALOG", json.dumps(document, indent="\t"))
            await save(conn, "ROUTING_POLICY", json.dumps({"default_router": "fixed"}, indent="\t"))
            body = await legacy_body(conn)
            routing = await legacy_body(conn, "routing")
        assert "\t" not in body
        assert yaml.safe_load(body) == document
        assert yaml.safe_load(routing) == {"default_router": "fixed"}
        registered = previous_registry(body, {secret: "legacy-key"})
        if registered is not None:
            assert registered == {"saved": [["legacy-key", {}]]}


async def test_a_credential_the_previous_registry_cannot_expand_keeps_its_catalog():
    async with mig.scratch_database() as database:
        secret = await upgraded_with_legacy_catalog(database)
        nested = {
            "models": [
                {
                    "id": "nested",
                    "base_url": "http://127.0.0.1:9/v1",
                    "api_key": "${" + secret + "}",
                    # Lifted from extra_body by the upgrade: the previous registry
                    # would send this reference upstream as literal text.
                    "extra_body": {"custom_token": "${CATALOG_SECRET_NESTED}"},
                }
            ]
        }
        async with mig.connected(database) as conn:
            await save(conn, "MODEL_CATALOG", json.dumps(nested))
            assert await legacy_body(conn) == LEGACY
            # Neither is a value that is not JSON at all.
            await save(conn, "MODEL_CATALOG", "models: []")
            assert await legacy_body(conn) == LEGACY
        registered = previous_registry(LEGACY, {})
        if registered is not None:
            assert registered == {"legacy": [["legacy-key", {}]]}


async def test_a_reset_or_a_removal_never_empties_the_rollback_catalog():
    async with mig.scratch_database() as database:
        secret = await upgraded_with_legacy_catalog(database)
        document = {"models": [{"id": "kept", "api_key": "${" + secret + "}"}]}
        async with mig.connected(database) as conn:
            await save(conn, "MODEL_CATALOG", json.dumps(document))
            assert yaml.safe_load(await legacy_body(conn)) == document
            await conn.execute(
                "UPDATE app_config SET source='reset', value='' WHERE key='MODEL_CATALOG'"
            )
            assert yaml.safe_load(await legacy_body(conn)) == document
            await conn.execute("DELETE FROM app_config WHERE key='MODEL_CATALOG'")
            assert yaml.safe_load(await legacy_body(conn)) == document
