"""Fetch, persist and publish public provider tariffs without rebuilding routes."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import aiohttp

from serving.pricing import PricingSchedule, PricingSnapshot, freeze_schedule
from serving.pricing_sources.deepseek import (
    MissingModelError,
    parse_deepseek_page,
    parse_holiday_calendar,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

SETTING_KEY = "provider_pricing:deepseek"
PRICE_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
PRICE_URLS = {
    "USD": PRICE_URL,
    "CNY": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
}
CALENDAR_URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
SUPPORTED_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
SYNC_INTERVAL = 300.0
REFRESH_INTERVAL = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CalendarNotPublished(ValueError):
    """The optional next-year calendar is not yet published."""


def configured_source_models(router: Any, embeddings: Mapping[str, Any] | None) -> set[str]:
    """Find opted-in sources on registered chat and embedding endpoints."""
    models: set[str] = set()

    def collect(adapter: Any) -> None:
        source = getattr(getattr(adapter, "config", None), "pricing_source", None)
        if isinstance(source, Mapping) and source.get("provider") == "deepseek":
            models.add(source["model"])
        for child in getattr(adapter, "_adapters", ()):
            collect(child)

    for route in router.routes.values():
        for entry in route.adapters:
            collect(entry[0])
    for adapter in (embeddings or {}).values():
        collect(adapter)
    return models


def decode_snapshot(raw: str, *, currency: str = "USD") -> PricingSnapshot:
    """Validate persisted data just as strictly as a newly downloaded tariff."""
    if currency not in PRICE_URLS:
        raise ValueError("unsupported provider pricing currency")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported provider pricing snapshot")
    if set(payload) != {
        "version",
        "provider",
        "revision",
        "fetched_at",
        "price_url",
        "calendar_urls",
        "source_sha256",
        "models",
        "calendars",
        "calendar_years",
        "currency",
        "unit",
    }:
        raise ValueError("unexpected provider pricing snapshot fields")
    if payload.get("provider") != "deepseek":
        raise ValueError("unexpected provider pricing source")
    if payload.get("currency") != currency or payload.get("unit") != "per_million_tokens":
        raise ValueError("unexpected provider pricing currency or unit")
    if payload.get("price_url") != PRICE_URLS[currency]:
        raise ValueError("unexpected official pricing URL")
    digest = payload.get("source_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("invalid official pricing content digest")
    revision = payload.get("revision")
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise ValueError("invalid provider pricing revision")
    fetched_at = dt.datetime.fromisoformat(
        str(payload.get("fetched_at", "")).replace("Z", "+00:00")
    )
    if fetched_at.tzinfo is None:
        raise ValueError("provider pricing fetched_at must include timezone")
    calendars = payload.get("calendars")
    if not isinstance(calendars, dict) or not 1 <= len(calendars) <= 3:
        raise ValueError("provider pricing requires validated calendar years")
    coverage = payload.get("calendar_years")
    if (
        not isinstance(coverage, list)
        or not 1 <= len(coverage) <= 2
        or any(type(year) is not int or not 2000 <= year < 2200 for year in coverage)
        or len(set(coverage)) != len(coverage)
    ):
        raise ValueError("provider pricing requires explicit annual calendar coverage")
    years = set(coverage)
    excluded: list[str] = []
    for year_text, dates in calendars.items():
        year = int(year_text)
        if (
            str(year) != year_text
            or not isinstance(dates, list)
            or not dates
            or year not in years | {min(years) - 1}
        ):
            raise ValueError("provider pricing calendar is not published")
        for date in dates:
            parsed = dt.date.fromisoformat(date)
            if parsed.isoformat() != date or parsed.year != year:
                raise ValueError("provider pricing calendar date does not match year")
            excluded.append(date)
    if len(years) == 2 and max(years) - min(years) != 1:
        raise ValueError("provider pricing calendar years must be consecutive")
    if not years <= {int(year) for year in calendars}:
        raise ValueError("annual calendar coverage requires holidays in each covered year")
    if payload.get("calendar_urls") != {
        str(year): CALENDAR_URL.format(year=year) for year in years
    }:
        raise ValueError("unexpected holiday calendar URLs")
    models = payload.get("models")
    if not isinstance(models, dict) or not models or not set(models) <= SUPPORTED_MODELS:
        raise ValueError("provider pricing snapshot must identify supported models")
    tariffs: dict[str, PricingSchedule] = {}
    versions: dict[str, str] = {}
    for model, tariff in models.items():
        if not isinstance(tariff, dict) or set(tariff) != {
            "model_version",
            "default",
            "windows",
            "weekdays",
        }:
            raise ValueError("provider tariff must be an object")
        model_version = tariff.get("model_version")
        if not isinstance(model_version, str) or not model_version:
            raise ValueError("provider tariff must identify its model version")
        schedule = PricingSchedule.from_raw(
            {
                "effective_at": "1970-01-01T00:00:00Z",
                "timezone": "UTC",
                "default": tariff.get("default"),
                "windows": tariff.get("windows"),
                "weekdays": tariff.get("weekdays"),
                "excluded_dates": excluded,
            }
        )
        required = {"prompt", "completion", "input_cache_reads"}
        if set(schedule.default) != required or not schedule.windows:
            raise ValueError("provider tariff has incomplete token prices")
        if any(set(window.pricing) != required for window in schedule.windows):
            raise ValueError("provider tariff has incomplete window prices")
        if schedule.weekdays != frozenset(range(1, 6)):
            raise ValueError("official pricing calendar must use Monday through Friday")
        for window in schedule.windows:
            if not 0 <= window.start_minute < window.end_minute <= 16 * 60:
                raise ValueError("official pricing windows must share their UTC and China dates")
            if any(
                Decimal(window.pricing[key]) <= Decimal(schedule.default[key]) for key in required
            ):
                raise ValueError("official peak prices must exceed off-peak prices")
        if any(
            Decimal(value) <= 0
            for prices in (schedule.default, *(window.pricing for window in schedule.windows))
            for value in prices.values()
        ):
            raise ValueError("official provider token prices must be positive")
        tariffs[model] = freeze_schedule(schedule)
        versions[model] = model_version
    return PricingSnapshot(
        revision=revision,
        fetched_at=fetched_at.astimezone(dt.timezone.utc),
        calendar_years=frozenset(years),
        tariffs=MappingProxyType(tariffs),
        model_versions=MappingProxyType(versions),
        currency=currency,
    )


async def fetch_public_text(url: str, *, optional_calendar: bool = False) -> str:
    """Fetch only the fixed public sources; never inherit credentials or redirects."""
    if url not in PRICE_URLS.values() and not any(
        url == CALENDAR_URL.format(year=year) for year in range(2000, 2200)
    ):
        raise ValueError("unsupported public pricing URL")
    timeout = aiohttp.ClientTimeout(total=10)
    async with (
        aiohttp.ClientSession(
            timeout=timeout, trust_env=False, cookie_jar=aiohttp.DummyCookieJar()
        ) as session,
        session.get(url, allow_redirects=False) as response,
    ):
        if response.status == 404 and optional_calendar:
            raise CalendarNotPublished("next-year holiday calendar is not published")
        if response.status != 200:
            raise ValueError(f"public pricing source returned HTTP {response.status}")
        if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
            raise ValueError("public pricing response exceeds size limit")
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("public pricing response exceeds size limit")
        return body.decode("utf-8")


class PricingSyncService:
    """One immutable tariff view, backed by an atomic shared setting.

    Every worker may fetch, but only a compare-and-set against the exact value
    read before the fetch can publish. A slow download never overwrites a newer
    worker's successful sync. Readers converge within the refresh interval.
    """

    def __init__(
        self,
        store: Any,
        models: set[str],
        *,
        clock: Callable[[], dt.datetime] | None = None,
        currency: str = "USD",
    ) -> None:
        if not models or not models <= SUPPORTED_MODELS:
            raise ValueError("unsupported automatic pricing models")
        if currency not in PRICE_URLS:
            raise ValueError("unsupported automatic pricing currency")
        self._currency = currency
        self._setting_key = SETTING_KEY if currency == "USD" else f"{SETTING_KEY}:{currency}"
        self._price_url = PRICE_URLS[currency]
        self._store = store
        self._models = frozenset(models)
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._snapshot: PricingSnapshot | None = None
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._last_attempt = float("-inf")
        self._last_error: str | None = None
        self._last_attempt_at: dt.datetime | None = None

    def snapshot(self) -> PricingSnapshot | None:
        """Return the current immutable revision without reading the database."""
        return self._snapshot

    def _usable(self) -> bool:
        return bool(
            self._snapshot
            and self._snapshot.currency == self._currency
            and self._clock().year in self._snapshot.calendar_years
            and self._models <= self._snapshot.tariffs.keys()
        )

    def status(self) -> dict[str, Any]:
        """Small operator-readable status; no response bodies or credentials."""
        snapshot = self._snapshot
        return {
            "provider": "deepseek",
            "currency": self._currency,
            "revision": snapshot.revision if snapshot else None,
            "fetched_at": snapshot.fetched_at.isoformat() if snapshot else None,
            "calendar_years": sorted(snapshot.calendar_years) if snapshot else [],
            "ready": self._usable(),
            "last_attempt_at": self._last_attempt_at.isoformat() if self._last_attempt_at else None,
            "last_error": self._last_error,
        }

    async def initialize(self) -> None:
        """Warm from persistent data, fetching only when no usable revision exists."""
        if self._store is None:
            raise RuntimeError("Automatic model pricing requires a shared operational database")
        try:
            await self.refresh()
        except Exception:
            logger.warning("Stored provider pricing could not be loaded", exc_info=True)
        if not self._usable():
            try:
                await self.sync_once()
            except Exception as exc:
                raise RuntimeError(
                    "Automatic DeepSeek pricing has no valid current-year snapshot; "
                    "check public pricing/calendar access and the operational database"
                ) from exc
        if not self._usable():
            raise RuntimeError("Automatic DeepSeek pricing has no valid current-year snapshot")
        logger.info("Official pricing initialized: %s", self.status())

    async def refresh(self) -> None:
        """Load the shared revision while fencing overlapping or outdated reads."""
        self._generation += 1
        generation = self._generation
        row = await self._store.get_setting(self._setting_key)
        if row is None:
            return  # Never discard the last valid revision after an accidental deletion.
        snapshot = decode_snapshot(row["value"], currency=self._currency)
        if generation != self._generation:
            return
        previous = self._snapshot
        self._snapshot = snapshot
        if previous is None or previous.revision != snapshot.revision:
            logger.info("Official pricing revision loaded: %s", self.status())

    async def _download(self) -> dict[str, Any]:
        now = self._clock().astimezone(dt.timezone.utc)
        html = await fetch_public_text(self._price_url)
        exclusions: dict[str, set[str]] = {}
        coverage: list[int] = []
        for year in (now.year, now.year + 1):
            optional = year != now.year
            try:
                text = await fetch_public_text(
                    CALENDAR_URL.format(year=year), optional_calendar=optional
                )
                raw = json.loads(text)
                if isinstance(raw, dict) and raw.get("papers") == [] and raw.get("days") == []:
                    raise CalendarNotPublished("holiday calendar is an unpublished placeholder")
                dates = parse_holiday_calendar(raw, year)
                coverage.append(year)
                for day in dates:
                    text_date = day.isoformat() if isinstance(day, dt.date) else day
                    calendar_year = str(dt.date.fromisoformat(text_date).year)
                    exclusions.setdefault(calendar_year, set()).add(text_date)
            except CalendarNotPublished:
                if not optional:
                    raise
        models = {}
        for model in sorted(SUPPORTED_MODELS):
            try:
                models[model] = parse_deepseek_page(html, model, currency=self._currency)
            except MissingModelError:
                if model in self._models:
                    raise
        return {
            "version": 1,
            "provider": "deepseek",
            "currency": self._currency,
            "unit": "per_million_tokens",
            "revision": str(uuid.uuid4()),
            "fetched_at": now.isoformat(),
            "price_url": self._price_url,
            "calendar_urls": {str(year): CALENDAR_URL.format(year=year) for year in coverage},
            "source_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "models": models,
            "calendars": {year: sorted(dates) for year, dates in exclusions.items()},
            "calendar_years": coverage,
        }

    async def sync_once(self) -> bool:
        """Download and CAS-publish; a lost race reloads the winning revision."""
        self._last_attempt = time.monotonic()
        self._last_attempt_at = self._clock()
        try:
            row = await self._store.get_setting(self._setting_key)
            expected = row["value"] if row is not None else None
            payload = await asyncio.wait_for(self._download(), timeout=30)
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            decode_snapshot(raw, currency=self._currency)
            changed = await self._store.compare_and_set_setting(
                self._setting_key, expected, raw, "json", "pricing-sync:deepseek"
            )
            await self.refresh()
            self._last_error = None
            logger.info(
                "Provider pricing sync completed provider=deepseek published=%s revision=%s",
                changed,
                self._snapshot.revision if self._snapshot else None,
            )
            return changed
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            logger.warning(
                "Provider pricing sync failed; retaining last valid revision status=%s",
                self.status(),
                exc_info=True,
            )
            raise

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                logger.warning(
                    "Provider pricing refresh failed; retaining snapshot status=%s",
                    self.status(),
                    exc_info=True,
                )
            snapshot = self._snapshot
            due = (
                not self._usable()
                or self._last_error is not None
                or (
                    snapshot is not None
                    and (self._clock() - snapshot.fetched_at).total_seconds() >= SYNC_INTERVAL
                )
            )
            if (
                due
                and time.monotonic() - self._last_attempt >= SYNC_INTERVAL
                and (self._sync_task is None or self._sync_task.done())
            ):
                self._sync_task = asyncio.create_task(self._sync_background())
            await asyncio.sleep(REFRESH_INTERVAL)

    async def _sync_background(self) -> None:
        # sync_once records failures; they must not stop the refresh task.
        with contextlib.suppress(Exception):
            await self.sync_once()

    def start(self) -> None:
        """Start the background refresh loop once."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel and await refresh and download tasks during shutdown."""
        task, self._task = self._task, None
        sync_task, self._sync_task = self._sync_task, None
        tasks = [pending for pending in (task, sync_task) if pending is not None]
        for pending in tasks:
            pending.cancel()
        for pending in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await pending
