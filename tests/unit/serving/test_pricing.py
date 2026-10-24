"""Tests for static and scheduled model pricing."""

from __future__ import annotations

import datetime as dt

import pytest

from serving.adapters.base import ModelConfig
from serving.pricing import PricingSchedule, effective_pricing
from serving.utils import context as req_ctx

UTC = dt.UTC
ACTIVATION = dt.datetime(2026, 8, 16, 16, 0, tzinfo=UTC)


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
    ("timestamp", "peak"),
    [
        ("2026-09-28T01:00:00Z", True),  # Monday
        ("2026-09-29T06:00:00Z", True),
        ("2026-09-30T03:59:59Z", True),
        ("2026-10-01T02:00:00Z", False),  # National Day
        ("2026-10-07T09:59:59Z", False),  # Last holiday
        ("2026-10-08T01:00:00Z", True),
        ("2026-10-09T09:59:59Z", True),  # Friday
        ("2026-10-10T02:00:00Z", False),  # Make-up working Saturday
        ("2026-10-11T06:00:00Z", False),  # Sunday
        ("2026-10-12T09:00:00+08:00", True),
        ("2026-10-12T12:00:00+08:00", False),
    ],
)
def test_calendar_restricts_all_prices(timestamp: str, peak: bool) -> None:
    raw = _schedule()
    raw["weekdays"] = [1, 2, 3, 4, 5]
    raw["excluded_dates"] = [f"2026-10-{day:02d}" for day in range(1, 8)]
    config = _config(pricing_schedule=raw)
    expected = raw["windows"][0]["pricing"] if peak else raw["default"]

    assert effective_pricing(
        config, at=dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    ) == {
        **config.pricing,
        **expected,
    }


@pytest.mark.unit
def test_overnight_windows_use_the_current_utc_calendar_date() -> None:
    raw = _schedule()
    raw["weekdays"] = [1, 2, 3, 4, 5]
    raw["excluded_dates"] = [dt.date(2026, 8, 18)]  # YAML date value
    raw["windows"] = [{"start": "22:00", "end": "02:00", "pricing": {"prompt": "0.44"}}]
    schedule = PricingSchedule.from_raw(raw)

    assert schedule.resolve({}, at=_at(17, 23))["prompt"] == "0.44"
    assert schedule.resolve({}, at=_at(18, 1))["prompt"] == "0.22"
    assert schedule.resolve({}, at=_at(18, 23))["prompt"] == "0.22"
    assert schedule.resolve({}, at=_at(19, 1))["prompt"] == "0.44"
    assert schedule.resolve({}, at=_at(22, 1))["prompt"] == "0.22"  # Saturday


@pytest.mark.unit
@pytest.mark.parametrize("invalid", [None, [], "1,2", [0], [8], [True], [1.0], ["1"], [1, 1]])
def test_schedule_rejects_invalid_weekdays(invalid) -> None:
    raw = _schedule()
    raw["weekdays"] = invalid
    with pytest.raises(ValueError, match="weekdays"):
        PricingSchedule.from_raw(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "2026-10-01",
        ["2026-02-30"],
        ["20261001"],
        ["2026-10-01T00:00:00Z"],
        [True],
        [dt.datetime(2026, 10, 1)],
        ["2026-10-01", dt.date(2026, 10, 1)],
    ],
)
def test_schedule_rejects_invalid_excluded_dates(invalid) -> None:
    raw = _schedule()
    raw["excluded_dates"] = invalid
    with pytest.raises(ValueError, match="excluded_dates"):
        PricingSchedule.from_raw(raw)
