"""Time-aware upstream pricing resolution.

Static ``ModelConfig.pricing`` dictionaries remain the default.  A model may
optionally add a ``pricing_schedule`` that takes effect at an absolute UTC
timestamp and overrides selected pricing fields inside configured windows.
Weekdays and excluded dates can restrict the days on which windows apply.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from serving.adapters.base import ModelConfig


@dataclass(frozen=True, slots=True)
class _DailyPricingWindow:
    start_minute: int
    end_minute: int
    pricing: Mapping[str, str]

    def contains(self, minute: int) -> bool:
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


@dataclass(frozen=True, slots=True)
class PricingSchedule:
    """Validated pricing schedule with optional calendar restrictions.

    ``effective_at`` is an absolute UTC activation boundary. After activation,
    ``default`` applies outside the half-open daily windows ``[start, end)``.
    ``weekdays`` uses ISO weekday numbers (Monday=1, Sunday=7). ``excluded_dates``
    suppresses all windows on those UTC calendar dates, including the early
    portion of an overnight window. Outside eligible days, ``default`` applies.
    The implementation intentionally accepts only UTC schedules so the
    price selected by every gateway worker is independent of host timezone and
    daylight-saving rules.
    """

    effective_at: dt.datetime
    default: Mapping[str, str]
    windows: tuple[_DailyPricingWindow, ...]
    weekdays: frozenset[int] = frozenset(range(1, 8))
    excluded_dates: frozenset[dt.date] = frozenset()

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PricingSchedule:
        """Parse and validate a ``pricing_schedule`` mapping."""
        if not isinstance(raw, Mapping):
            raise ValueError("pricing_schedule must be a mapping")
        unknown = set(raw) - {
            "effective_at",
            "timezone",
            "default",
            "windows",
            "weekdays",
            "excluded_dates",
        }
        if unknown:
            raise ValueError(f"pricing_schedule has unknown keys: {sorted(unknown)}")

        timezone_name = str(raw.get("timezone", "UTC")).strip().upper()
        if timezone_name != "UTC":
            raise ValueError("pricing_schedule.timezone must be UTC")

        effective_at = _parse_effective_at(raw.get("effective_at"))
        default = _parse_pricing(raw.get("default"), "pricing_schedule.default")
        weekdays = _parse_weekdays(raw.get("weekdays", list(range(1, 8))))
        excluded_dates = _parse_excluded_dates(raw.get("excluded_dates", []))

        raw_windows = raw.get("windows", ())
        if isinstance(raw_windows, (str, bytes)) or not isinstance(raw_windows, Sequence):
            raise ValueError("pricing_schedule.windows must be a list")
        windows: list[_DailyPricingWindow] = []
        occupied_minutes: set[int] = set()
        for index, item in enumerate(raw_windows):
            context = f"pricing_schedule.windows[{index}]"
            if not isinstance(item, Mapping):
                raise ValueError(f"{context} must be a mapping")
            item_unknown = set(item) - {"start", "end", "pricing"}
            if item_unknown:
                raise ValueError(f"{context} has unknown keys: {sorted(item_unknown)}")
            start = _parse_time(item.get("start"), f"{context}.start")
            end = _parse_time(item.get("end"), f"{context}.end")
            if start == end:
                raise ValueError(f"{context} start and end must differ")
            window = _DailyPricingWindow(
                start_minute=start,
                end_minute=end,
                pricing=_parse_pricing(item.get("pricing"), f"{context}.pricing"),
            )
            minutes = range(start, end) if start < end else (*range(start, 24 * 60), *range(0, end))
            overlap = occupied_minutes.intersection(minutes)
            if overlap:
                raise ValueError("pricing_schedule.windows must not overlap")
            occupied_minutes.update(minutes)
            windows.append(window)

        return cls(
            effective_at=effective_at,
            default=default,
            windows=tuple(windows),
            weekdays=weekdays,
            excluded_dates=excluded_dates,
        )

    def resolve(
        self,
        base: dict[str, str],
        *,
        at: dt.datetime | None = None,
    ) -> dict[str, str]:
        """Return the effective pricing dictionary at ``at``."""
        instant = pricing_time(at)
        if instant < self.effective_at:
            return base

        minute = instant.hour * 60 + instant.minute
        override = self.default
        if instant.isoweekday() in self.weekdays and instant.date() not in self.excluded_dates:
            for window in self.windows:
                if window.contains(minute):
                    override = window.pricing
                    break
        return {**base, **override}


class PricingUnavailableError(HTTPException):
    """An opted-in source cannot safely price a request at its start time."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="Official model pricing is temporarily unavailable. Please retry later.",
        )


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    """One validated source revision, retained unchanged by in-flight requests."""

    revision: str
    fetched_at: dt.datetime
    calendar_years: frozenset[int]
    tariffs: Mapping[str, PricingSchedule]
    model_versions: Mapping[str, str]

    def resolve(self, model: str, *, at: dt.datetime) -> dict[str, str]:
        """Refuse unknown years instead of treating missing holidays as weekdays."""
        schedule = self.tariffs.get(model)
        if schedule is None or at.year not in self.calendar_years:
            raise PricingUnavailableError()
        return schedule.resolve({}, at=at)


_snapshot_provider: Callable[[], PricingSnapshot | None] | None = None


def bind_pricing_snapshot_provider(provider: Callable[[], PricingSnapshot | None] | None) -> None:
    """Attach the boot-owned resolver, or detach it during shutdown."""
    global _snapshot_provider
    _snapshot_provider = provider


def get_pricing_snapshot() -> PricingSnapshot | None:
    """Read the current immutable revision, outside any request's frozen view."""
    return _snapshot_provider() if _snapshot_provider is not None else None


def freeze_schedule(schedule: PricingSchedule) -> PricingSchedule:
    """Copy mutable parser output before exposing it to request contexts."""
    return PricingSchedule(
        effective_at=schedule.effective_at,
        default=MappingProxyType(dict(schedule.default)),
        windows=tuple(
            _DailyPricingWindow(
                window.start_minute, window.end_minute, MappingProxyType(dict(window.pricing))
            )
            for window in schedule.windows
        ),
        weekdays=schedule.weekdays,
        excluded_dates=schedule.excluded_dates,
    )


def require_source_pricing(config: Any) -> None:
    """Check a source-backed endpoint before upstream work can create usage."""
    if isinstance(getattr(config, "pricing_source", None), Mapping):
        effective_pricing(config)


def require_embedding_pricing(adapter: Any) -> None:
    """Validate the primary and ordered fallback embedding endpoints."""
    require_source_pricing(getattr(adapter, "config", None))
    for child in getattr(adapter, "_adapters", ()):
        require_embedding_pricing(child)


def require_route_pricing(route: Any) -> None:
    """Check every eligible endpoint, including fallback endpoints, before dispatch."""
    for entry in getattr(route, "adapters", ()):
        require_source_pricing(entry[0].config)


def pricing_time(at: dt.datetime | None = None) -> dt.datetime:
    """Return a timezone-aware UTC timestamp for pricing resolution.

    HTTP requests are stamped once by ``RequestIdMiddleware``. Reusing that
    value keeps route selection, eventual stream logging, and quota charging on
    the same side of a pricing boundary even when a long request crosses it.
    """
    instant = at
    if instant is None:
        contextual = req_ctx.get().get(req_ctx.PRICING_TIME)
        instant = contextual if isinstance(contextual, dt.datetime) else None
    if instant is None:
        return dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("pricing time must be timezone-aware")
    return instant.astimezone(dt.timezone.utc)


def effective_pricing(
    config: ModelConfig | Any,
    *,
    at: dt.datetime | None = None,
) -> dict[str, str] | None:
    """Resolve a model config's static or scheduled pricing.

    For configs without a schedule the original dictionary is returned, which
    preserves existing identity/caching behavior for static pricing consumers.
    """
    source = getattr(config, "pricing_source", None)
    if isinstance(source, Mapping):
        context = req_ctx.get()
        snapshot = (
            context[req_ctx.PRICING_SNAPSHOT]
            if req_ctx.PRICING_SNAPSHOT in context
            else get_pricing_snapshot()
        )
        if not isinstance(snapshot, PricingSnapshot) or source.get("provider") != "deepseek":
            raise PricingUnavailableError()
        return snapshot.resolve(str(source.get("model", "")), at=pricing_time(at))
    base = getattr(config, "pricing", None)
    if not isinstance(base, dict):
        return None
    schedule = getattr(config, "pricing_schedule", None)
    if not isinstance(schedule, (PricingSchedule, Mapping)):
        return base
    if isinstance(schedule, Mapping):
        schedule = PricingSchedule.from_raw(schedule)
    return schedule.resolve(base, at=at)


def has_scheduled_pricing(config: ModelConfig | Any) -> bool:
    """Return whether *config* has a dynamic pricing schedule."""
    return isinstance(
        getattr(config, "pricing_schedule", None), (PricingSchedule, Mapping)
    ) or isinstance(getattr(config, "pricing_source", None), Mapping)


def _parse_effective_at(raw: Any) -> dt.datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("pricing_schedule.effective_at must be an ISO-8601 timestamp")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("pricing_schedule.effective_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("pricing_schedule.effective_at must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _parse_time(raw: Any, context: str) -> int:
    if not isinstance(raw, str):
        raise ValueError(f"{context} must use HH:MM")
    try:
        parsed = dt.time.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{context} must use HH:MM") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise ValueError(f"{context} must use UTC HH:MM with minute precision")
    return parsed.hour * 60 + parsed.minute


def _parse_weekdays(raw: Any) -> frozenset[int]:
    if (
        isinstance(raw, (str, bytes))
        or not isinstance(raw, Sequence)
        or not raw
        or any(type(day) is not int or not 1 <= day <= 7 for day in raw)
        or len(set(raw)) != len(raw)
    ):
        raise ValueError(
            "pricing_schedule.weekdays must be a non-empty list of unique integers 1-7"
        )
    return frozenset(raw)


def _parse_excluded_dates(raw: Any) -> frozenset[dt.date]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("pricing_schedule.excluded_dates must be a list of YYYY-MM-DD dates")
    dates: set[dt.date] = set()
    for item in raw:
        # YAML may decode an unquoted date as datetime.date. Never accept a
        # timestamp: exclusions refer to whole UTC dates, not instants.
        if type(item) is dt.date:
            parsed = item
        elif isinstance(item, str):
            try:
                parsed = dt.date.fromisoformat(item)
            except ValueError as exc:
                raise ValueError("pricing_schedule.excluded_dates must use YYYY-MM-DD") from exc
            if parsed.isoformat() != item:
                raise ValueError("pricing_schedule.excluded_dates must use YYYY-MM-DD")
        else:
            raise ValueError("pricing_schedule.excluded_dates must use YYYY-MM-DD")
        if parsed in dates:
            raise ValueError("pricing_schedule.excluded_dates must not contain duplicate dates")
        dates.add(parsed)
    return frozenset(dates)


def _parse_pricing(raw: Any, context: str) -> dict[str, str]:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"{context} must be a non-empty pricing mapping")
    parsed: dict[str, str] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text:
            raise ValueError(f"{context} keys must not be blank")
        try:
            numeric = float(value_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{context}.{key_text} must be numeric") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"{context}.{key_text} must be a non-negative finite number")
        parsed[key_text] = value_text
    return parsed


__all__ = [
    "PricingSchedule",
    "effective_pricing",
    "has_scheduled_pricing",
    "pricing_time",
]
