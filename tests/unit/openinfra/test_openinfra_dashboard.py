"""OpenInfra's pure helpers: tab order, fleet state, windows, settings, and the field lists."""

from __future__ import annotations

import importlib

import pytest

from serving.openinfra import config, dashboard, fields, store


def _engine(engine_id: str, name: str | None = None) -> dict:
    return {"id": engine_id, "name": name}


def test_engines_follow_the_configured_order_then_the_alphabet():
    engines = [
        _engine("e1", "Zeta"),
        _engine("e2", "alpha"),
        _engine("e3", "embed"),
        _engine("e4", None),
        _engine("e5", "beta"),
    ]
    ordered = dashboard.order_engines(engines, ("embed", "e4", "missing"))
    assert [e["id"] for e in ordered] == ["e3", "e4", "e2", "e5", "e1"]
    # Unnamed engines sort by id: "e4" before "embed".
    assert [e["id"] for e in dashboard.order_engines(engines)] == ["e2", "e5", "e4", "e3", "e1"]


@pytest.mark.parametrize(
    ("statuses", "queued", "util", "state"),
    [
        ([], 0, None, "offline"),
        (["offline", "offline"], 0, None, "offline"),
        (["online", "degraded"], 0, 10, "degraded"),
        (["online"], 1501, 10, "degraded"),
        (["online"], 201, 10, "busy"),
        (["online"], 0, 81, "busy"),
        (["online", "offline"], 200, 80, "healthy"),
    ],
)
def test_fleet_state(statuses, queued, util, state):
    gpus = [{"status": status} for status in statuses]
    assert dashboard.fleet_state(gpus, queued, util) == state


@pytest.mark.parametrize(
    ("hours", "clamped"),
    [(0, 0.25), (1, 1), (500, 168), (float("nan"), 2.0), (float("-inf"), 2.0)],
)
def test_clamp_hours(hours, clamped):
    assert dashboard.clamp_hours(hours) == clamped


@pytest.mark.parametrize(
    ("hours", "grain"),
    [
        (0.25, 5_000),
        (0.5, 10_000),
        (1, 20_000),
        (2, store.GRAIN_1M),
        (3, store.GRAIN_1M),
        (3.25, store.GRAIN_20M),
        (24, store.GRAIN_20M),
        (25, store.GRAIN_1H),
        (168, store.GRAIN_1H),
    ],
)
def test_history_grain(hours, grain):
    assert store.history_grain(hours) == grain


def test_rollup_floor_is_a_bucket_start_the_backfill_away():
    now = 1_800_000_000_000 + 7 * 60_000
    assert store.rollup_floor(store.GRAIN_20M, now) == 1_800_000_000_000 - 36 * 3_600_000
    assert store.rollup_floor(store.GRAIN_1H, now) == 1_800_000_000_000 - 30 * 24 * 3_600_000


def test_settings(monkeypatch):
    values = {
        config.INGEST_TOKEN: "  secret  ",
        config.ENGINE_ORDER: " b,a, ,b ",
        config.RETIRED_ENGINES: "",
    }
    monkeypatch.setattr(config, "config_value", lambda key, default=None: values.get(key, default))
    assert config.ingest_token() == "secret"
    assert config.engine_order() == ("b", "a")
    assert config.retired_engines() == ()


def test_the_field_lists_match_the_tables_the_migration_created():
    migration = importlib.import_module("migrations.versions.0026_openinfra")
    assert fields.CPU_FIELDS == migration._CPU
    assert fields.ENGINE_METRICS == migration._ENGINE
    assert fields.GPU_SAMPLE_FIELDS == migration._GPU_SAMPLE
    assert fields.GPU_ROLLUP_FIELDS == migration._GPU_ROLLUP
    assert fields.CPU_HISTORY == migration._CPU_ROLLUP
    rollup = {name for name, _ in fields.ENGINE_HISTORY} | set(fields.ENGINE_SUMS)
    assert rollup == set(migration._ENGINE_ROLLUP)
    assert len(rollup) == len(migration._ENGINE_ROLLUP)
    assert {source for _, source in fields.ENGINE_HISTORY} <= set(fields.ENGINE_METRICS)
    assert set(fields.GPU_SAMPLE_FIELDS) <= set(fields.GPU_NUMBERS)
