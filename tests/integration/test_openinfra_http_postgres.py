"""OpenInfra end to end over HTTP: a reporter's snapshot in, the page's documents out."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.config.distribution import get_distribution_config
from serving.config.settings import get_settings
from serving.openinfra import config
from serving.servers.deps import get_db_logger
from serving.servers.routers import openinfra
from tests.fixtures import postgres
from tests.fixtures.application_documents import seed_site
from tests.integration.test_openinfra_store_postgres import TABLES

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

TOKEN = "test-ingest-token-0123456789abcdef"
MANIFEST = """\
schema_version: 1
distribution:
  id: example-site
  display_name: Example Site
  release: 2026.11.1
features:
  routers: [fixed]
  openinfra: true
"""


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path):
    manifest = tmp_path / "distribution.yaml"
    manifest.write_text(MANIFEST)
    seed_site(monkeypatch, manifest)
    monkeypatch.setenv("DISTRIBUTION_CONFIG_MODE", "active")
    monkeypatch.setattr(
        config,
        "config_value",
        lambda key, default=None: {config.INGEST_TOKEN: TOKEN}.get(key, default),
    )
    get_settings.cache_clear()
    get_distribution_config.cache_clear()
    openinfra._DASHBOARDS.clear()
    openinfra._HISTORIES.clear()
    pool = await postgres.create_pool(min_size=1, max_size=2)
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(TABLES)}")
    app = FastAPI()
    app.include_router(openinfra.router)
    app.dependency_overrides[get_db_logger] = lambda: SimpleNamespace(pool=pool)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"TRUNCATE {', '.join(TABLES)}")
        await pool.close()
        get_settings.cache_clear()
        get_distribution_config.cache_clear()


async def test_a_reported_snapshot_is_on_the_page(client):
    snapshot = {
        "node": {
            "id": "node-a",
            "hostname": "node-a",
            "region": "lab",
            "cpu": {"cpu_util_pct": 12.5, "mem_used_mb": 2048, "mem_total_mb": 8192},
        },
        "gpus": [
            {
                "index": 0,
                "name": "Test GPU",
                "utilization_pct": 77,
                "memory_used_mb": 30,
                "memory_total_mb": 80,
                "temperature_c": 60,
                "power_w": 400,
            },
        ],
        "sglang": [
            {
                "server": {
                    "id": "node-a-chat",
                    "node_id": "node-a",
                    "name": "chat-model",
                    "engine": "vllm",
                    "gpu_indexes": "0",
                },
                "metrics": {
                    "num_running_reqs": 4,
                    "num_queue_reqs": 2,
                    "gen_throughput": 512,
                    "req_per_min": 30,
                    "prefill_tps": 100,
                },
                "requests": [
                    {"ts": 1_700_000_000_000, "input_len": 900, "output_len": 120, "ttft_s": 0.4}
                ],
            }
        ],
    }
    stored = await client.post(
        "/internal/openinfra/ingest", json=snapshot, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert stored.status_code == 200
    assert stored.json() == {"ok": True, "gpus": 1, "sglang": 1}

    page = await client.get("/public-openinfra")
    assert page.status_code == 200
    doc = page.json()
    assert doc["fleet"] == "healthy"
    assert doc["kpis"]["gpus_online"] == 1
    assert doc["kpis"]["running_reqs"] == 4
    assert [(e["id"], e["engine"], e["hostname"]) for e in doc["sglang"]] == [
        ("node-a-chat", "vllm", "node-a")
    ]
    assert doc["gpus"][0]["name"] == "Test GPU"
    assert doc["requests"][0]["input_len"] == 900
    assert doc["served"]["servers"][0]["id"] == "node-a-chat"
    assert doc["served"]["hour"]["reqs"] == 3.0  # 30/min over one 6 s bucket

    history = await client.get("/public-openinfra/history?hours=2")
    assert history.status_code == 200
    rows = history.json()
    assert rows["grain_ms"] == 60_000
    assert [r["utilization_pct"] for r in rows["gpu"]] == [77.0]
    assert [r["running"] for r in rows["sglang"]] == [4.0]
    assert [r["cpu_util_pct"] for r in rows["cpu"]] == [12.5]
