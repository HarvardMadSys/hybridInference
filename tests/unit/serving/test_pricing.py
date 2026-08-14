"""Tests for static and scheduled model pricing."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from serving.adapters.base import ModelConfig
from serving.pricing import PricingSchedule, effective_pricing
from serving.utils import context as req_ctx

UTC = dt.timezone.utc
ACTIVATION = dt.datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
MODELS_YAML = (
    Path(__file__).resolve().parents[3]
    / "distributions"
    / "freeinference"
    / "config"
    / "models.yaml"
)


def _at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def _config(*, pricing_schedule=None) -> ModelConfig:
    return ModelConfig(
        id="scheduled-model",
        name="Scheduled model",
        provider="provider",
        base_url="https://provider.example/v1",
        pricing={
            "prompt": "0.14",
            "completion": "0.28",
            "input_cache_reads": "0.0028",
            "input_cache_writes": "0",
        },
        pricing_schedule=pricing_schedule,
    )


def _schedule() -> dict:
    return {
        "effective_at": "2026-08-16T16:00:00Z",
        "timezone": "UTC",
        "default": {
            "prompt": "0.22",
            "completion": "0.66",
            "input_cache_reads": "0.007",
        },
        "windows": [
            {
                "start": "01:00",
                "end": "04:00",
                "pricing": {
                    "prompt": "0.44",
                    "completion": "1.32",
                    "input_cache_reads": "0.014",
                },
            },
            {
                "start": "06:00",
                "end": "10:00",
                "pricing": {
                    "prompt": "0.44",
                    "completion": "1.32",
                    "input_cache_reads": "0.014",
                },
            },
        ],
    }


@pytest.mark.unit
def test_static_pricing_keeps_existing_dictionary_behavior() -> None:
    config = _config()

    resolved = effective_pricing(config, at=_at(17, 2))

    assert resolved is config.pricing
    assert resolved == {
        "prompt": "0.14",
        "completion": "0.28",
        "input_cache_reads": "0.0028",
        "input_cache_writes": "0",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("instant", "expected_prompt"),
    [
        (ACTIVATION - dt.timedelta(microseconds=1), "0.14"),
        (ACTIVATION, "0.22"),
        (_at(17, 0, 59), "0.22"),
        (_at(17, 1, 0), "0.44"),
        (_at(17, 3, 59), "0.44"),
        (_at(17, 4, 0), "0.22"),
        (_at(17, 5, 59), "0.22"),
        (_at(17, 6, 0), "0.44"),
        (_at(17, 9, 59), "0.44"),
        (_at(17, 10, 0), "0.22"),
    ],
)
def test_schedule_activation_and_daily_window_boundaries(
    instant: dt.datetime,
    expected_prompt: str,
) -> None:
    config = _config(pricing_schedule=_schedule())

    assert effective_pricing(config, at=instant)["prompt"] == expected_prompt


@pytest.mark.unit
def test_request_pricing_time_is_reused_by_implicit_resolution() -> None:
    config = _config(pricing_schedule=_schedule())

    with req_ctx.push(pricing_time=_at(17, 6)):
        first = effective_pricing(config)
        second = effective_pricing(config)

    assert first == second
    assert first is not None
    assert first["prompt"] == "0.44"
    assert first["completion"] == "1.32"
    assert first["input_cache_reads"] == "0.014"
    assert first["input_cache_writes"] == "0"


@pytest.mark.unit
def test_schedule_rejects_naive_activation_timestamp() -> None:
    raw = _schedule()
    raw["effective_at"] = "2026-08-16T16:00:00"

    with pytest.raises(ValueError, match="must include a timezone"):
        PricingSchedule.from_raw(raw)


@pytest.mark.unit
def test_schedule_rejects_overlapping_windows() -> None:
    raw = _schedule()
    raw["windows"][1]["start"] = "03:00"

    with pytest.raises(ValueError, match="must not overlap"):
        PricingSchedule.from_raw(raw)


@pytest.mark.unit
def test_schedule_supports_window_across_midnight() -> None:
    raw = _schedule()
    raw["windows"] = [
        {
            "start": "22:00",
            "end": "02:00",
            "pricing": {"prompt": "0.44"},
        }
    ]
    schedule = PricingSchedule.from_raw(raw)
    base = {"prompt": "0.14", "completion": "0.28"}

    assert schedule.resolve(base, at=_at(17, 23))["prompt"] == "0.44"
    assert schedule.resolve(base, at=_at(18, 1))["prompt"] == "0.44"
    assert schedule.resolve(base, at=_at(18, 2))["prompt"] == "0.22"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "current", "off_peak", "peak"),
    [
        (
            "deepseek-v4-flash",
            ("0.14", "0.0028", "0.28"),
            ("0.22", "0.007", "0.66"),
            ("0.44", "0.014", "1.32"),
        ),
        (
            "deepseek-v4-pro",
            ("0.435", "0.003625", "0.87"),
            ("0.66", "0.022", "1.98"),
            ("1.32", "0.044", "3.96"),
        ),
    ],
)
def test_shipped_deepseek_prices_match_official_schedule(
    model_id: str,
    current: tuple[str, str, str],
    off_peak: tuple[str, str, str],
    peak: tuple[str, str, str],
) -> None:
    document = yaml.safe_load(MODELS_YAML.read_text())
    raw = next(model for model in document["models"] if model["id"] == model_id)
    schedule = PricingSchedule.from_raw(raw["pricing_schedule"])

    def prices(instant: dt.datetime) -> tuple[str, str, str]:
        resolved = schedule.resolve(raw["pricing"], at=instant)
        return (
            resolved["prompt"],
            resolved["input_cache_reads"],
            resolved["completion"],
        )

    assert prices(ACTIVATION - dt.timedelta(microseconds=1)) == current
    assert prices(_at(17, 12)) == off_peak
    assert prices(_at(17, 2)) == peak
    assert prices(_at(17, 7)) == peak
