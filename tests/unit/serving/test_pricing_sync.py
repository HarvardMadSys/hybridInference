"""Automatic tariffs are atomic, persistent and fixed for each request."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from serving import pricing
from serving.pricing_sources import sync
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.routers.completions_cost import PricingLookup
from serving.servers.routers.models import _catalog_pricing
from serving.servers.routers.routing_info import RoutingInfo
from serving.utils import context as req_ctx

NOW = dt.datetime(2026, 10, 19, 2, tzinfo=dt.timezone.utc)
SOURCE = {"provider": "deepseek", "model": "deepseek-flash"}


def _payload(*, revision="first", prompt="0.30", year=2026):
    return {
        "version": 1,
        "provider": "deepseek",
        "currency": "USD",
        "unit": "per_million_tokens",
        "revision": revision,
        "fetched_at": NOW.isoformat(),
        "price_url": sync.PRICE_URL,
        "calendar_urls": {str(year): sync.CALENDAR_URL.format(year=year)},
        "source_sha256": "a" * 64,
        "calendars": {str(year): [f"{year}-10-01"]},
        "calendar_years": [year],
        "models": {
            "deepseek-flash": {
                "model_version": "DeepSeek-V4.1-Flash",
                "default": {"prompt": "0.15", "completion": "0.60", "input_cache_reads": "0.003"},
                "weekdays": [1, 2, 3, 4, 5],
                "windows": [
                    {
                        "start": "01:00",
                        "end": "04:00",
                        "pricing": {
                            "prompt": prompt,
                            "completion": "1.20",
                            "input_cache_reads": "0.006",
                        },
                    }
                ],
            },
        },
    }


class MemoryStore:
    def __init__(self, payload=None):
        self.value = json.dumps(payload) if payload is not None else None

    async def get_setting(self, key):
        assert key == sync.SETTING_KEY
        return {"value": self.value} if self.value is not None else None

    async def compare_and_set_setting(self, key, expected, value, value_type, updated_by):
        assert (key, value_type, updated_by) == (sync.SETTING_KEY, "json", "pricing-sync:deepseek")
        if self.value != expected:
            return False
        self.value = value
        return True


def _service(store):
    return sync.PricingSyncService(store, {"deepseek-flash"}, clock=lambda: NOW)


@pytest.fixture(autouse=True)
def isolate_pricing_context(monkeypatch):
    monkeypatch.setattr(pricing, "_snapshot_provider", None)
    with req_ctx.push(pricing_time=NOW, pricing_snapshot=None):
        yield


def test_snapshot_is_deeply_immutable_and_year_bounded():
    snapshot = sync.decode_snapshot(json.dumps(_payload()))
    with pytest.raises(TypeError):
        snapshot.tariffs["deepseek-flash"].default["prompt"] = "0"
    with pytest.raises(TypeError):
        snapshot.tariffs["deepseek-flash"].windows[0].pricing["prompt"] = "0"
    with pytest.raises(pricing.PricingUnavailableError) as exc:
        snapshot.resolve("deepseek-flash", at=NOW.replace(year=2027))
    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    "mutation", ["zero", "unknown", "empty_calendar", "currency", "missing_cache", "missing_model"]
)
def test_stored_snapshot_cannot_bypass_download_validation(mutation):
    payload = _payload()
    if mutation == "zero":
        payload["models"]["deepseek-flash"]["default"]["prompt"] = "0"
    elif mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "empty_calendar":
        payload["calendars"]["2026"] = []
    elif mutation == "currency":
        payload["currency"] = "CNY"
    elif mutation == "missing_cache":
        del payload["models"]["deepseek-flash"]["default"]["input_cache_reads"]
    else:
        payload["models"] = {}
    with pytest.raises(ValueError):
        sync.decode_snapshot(json.dumps(payload))


@pytest.mark.parametrize(
    "mutation", ["weekend", "china_midnight", "equal_peak", "lower_cache_peak"]
)
def test_stored_snapshot_preserves_supported_official_calendar_semantics(mutation):
    payload = _payload()
    tariff = payload["models"]["deepseek-flash"]
    if mutation == "weekend":
        tariff["weekdays"] = [6, 7]
    elif mutation == "china_midnight":
        tariff["windows"][0].update(start="18:00", end="19:00")
    elif mutation == "equal_peak":
        tariff["windows"][0]["pricing"]["prompt"] = tariff["default"]["prompt"]
    else:
        tariff["windows"][0]["pricing"]["input_cache_reads"] = "0.001"
    with pytest.raises(ValueError):
        sync.decode_snapshot(json.dumps(payload))


def test_stored_snapshot_does_not_invent_a_permanent_two_times_price_ratio():
    snapshot = sync.decode_snapshot(json.dumps(_payload(prompt="0.45")))
    assert snapshot.resolve("deepseek-flash", at=NOW)["prompt"] == "0.45"


@pytest.mark.asyncio
async def test_restart_uses_persistent_valid_prices_even_when_sources_are_offline(monkeypatch):
    service = _service(MemoryStore(_payload()))
    download = AsyncMock(side_effect=OSError("offline"))
    monkeypatch.setattr(service, "_download", download)
    await service.initialize()
    assert service.snapshot().revision == "first"
    assert service.status()["ready"] is True
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_start_without_valid_cache_fails_explicitly(monkeypatch):
    service = _service(MemoryStore())
    monkeypatch.setattr(service, "_download", AsyncMock(side_effect=OSError("offline")))
    with pytest.raises(RuntimeError, match="no valid current-year snapshot"):
        await service.initialize()
    assert service.snapshot() is None
    assert service.status()["last_error"].startswith("OSError:")


@pytest.mark.asyncio
async def test_failed_fetch_retains_persistent_and_live_revision(monkeypatch):
    store = MemoryStore(_payload())
    service = _service(store)
    await service.initialize()
    previous = service.snapshot()
    stored = store.value
    monkeypatch.setattr(service, "_download", AsyncMock(side_effect=ValueError("changed HTML")))
    with pytest.raises(ValueError, match="changed HTML"):
        await service.sync_once()
    assert service.snapshot() is previous
    assert store.value == stored


@pytest.mark.asyncio
async def test_delayed_worker_cannot_overwrite_newer_success_and_readers_converge(monkeypatch):
    store = MemoryStore(_payload())
    slow, fast, reader = (_service(store) for _ in range(3))
    await asyncio.gather(slow.initialize(), fast.initialize(), reader.initialize())
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_download():
        started.set()
        await release.wait()
        return _payload(revision="old-download", prompt="0.40")

    monkeypatch.setattr(slow, "_download", delayed_download)
    monkeypatch.setattr(
        fast, "_download", AsyncMock(return_value=_payload(revision="new", prompt="0.50"))
    )
    pending = asyncio.create_task(slow.sync_once())
    await started.wait()
    assert await fast.sync_once() is True
    release.set()
    assert await pending is False
    await reader.refresh()
    assert {service.snapshot().revision for service in (slow, fast, reader)} == {"new"}


@pytest.mark.asyncio
async def test_refresh_in_flight_does_not_replace_a_newer_read(monkeypatch):
    store = MemoryStore(_payload())
    service = _service(store)
    await service.initialize()
    started, release = asyncio.Event(), asyncio.Event()
    original_get = store.get_setting

    async def slow_get(key):
        old = await original_get(key)
        started.set()
        await release.wait()
        return old

    monkeypatch.setattr(store, "get_setting", slow_get)
    pending = asyncio.create_task(service.refresh())
    await started.wait()
    store.value = json.dumps(_payload(revision="new"))
    monkeypatch.setattr(store, "get_setting", original_get)
    await service.refresh()
    release.set()
    await pending
    assert service.snapshot().revision == "new"


@pytest.mark.asyncio
async def test_request_keeps_snapshot_even_if_sync_happens_before_first_price_lookup(monkeypatch):
    first = sync.decode_snapshot(json.dumps(_payload()))
    newer = sync.decode_snapshot(json.dumps(_payload(revision="new", prompt="0.50")))
    current = first
    monkeypatch.setattr(pricing, "_snapshot_provider", lambda: current)
    config = SimpleNamespace(pricing_source=SOURCE, pricing={})
    seen = []

    async def app(scope, receive, send):
        nonlocal current
        captured_time = req_ctx.get()[req_ctx.PRICING_TIME]
        current = newer  # A refresh happens while the request is waiting for a route.
        seen.append((req_ctx.get()[req_ctx.PRICING_SNAPSHOT], captured_time))
        with req_ctx.push(pricing_time=NOW):
            seen.append(pricing.effective_pricing(config)["prompt"])
            await asyncio.sleep(0)
            seen.append(pricing.effective_pricing(config)["prompt"])

    middleware = RequestIdMiddleware(app)
    await middleware({"type": "http", "headers": [], "state": {}}, AsyncMock(), AsyncMock())
    await middleware({"type": "http", "headers": [], "state": {}}, AsyncMock(), AsyncMock())
    assert seen[0][0] is first
    assert seen[1:3] == ["0.30", "0.30"]
    assert seen[3][0] is newer
    assert seen[4:6] == ["0.50", "0.50"]
    assert seen[0][1].tzinfo is not None


def test_missing_source_quote_is_unknown_in_catalog_but_refused_before_dispatch():
    config = SimpleNamespace(pricing_source=SOURCE, pricing={"prompt": "0"})
    assert pricing.has_scheduled_pricing(config)
    assert _catalog_pricing(config) == {}
    with pytest.raises(pricing.PricingUnavailableError):
        pricing.require_route_pricing(
            SimpleNamespace(adapters=[(SimpleNamespace(config=config), 1)])
        )


@pytest.mark.parametrize("endpoint_id", ["endpoint", None])
def test_source_only_pricing_bypasses_both_cost_caches(endpoint_id):
    config = SimpleNamespace(
        pricing_source=SOURCE, pricing={}, provider="deepseek", endpoint_id="endpoint"
    )
    route = SimpleNamespace(adapters=[(SimpleNamespace(config=config), 1)])
    lookup = PricingLookup(router=SimpleNamespace(routes={"model": route}))
    routing = RoutingInfo(
        request_id="test", model="model", provider="deepseek", endpoint_id=endpoint_id
    )
    for amount in ("0.30", "0.50"):
        snapshot = sync.decode_snapshot(json.dumps(_payload(prompt=amount)))
        with req_ctx.push(pricing_snapshot=snapshot):
            assert lookup.raw_dict_for_routing(routing)["prompt"] == amount
            assert lookup.for_routing(routing).prompt_price == float(amount)


def test_calendar_coverage_is_distinct_from_next_year_previous_december_exclusions():
    payload = _payload()
    payload["calendar_years"] = [2026, 2027]
    payload["calendar_urls"]["2027"] = sync.CALENDAR_URL.format(year=2027)
    payload["calendars"]["2026"].append("2026-12-31")
    payload["calendars"]["2027"] = ["2027-01-01"]
    snapshot = sync.decode_snapshot(json.dumps(payload))
    assert snapshot.calendar_years == {2026, 2027}
    assert dt.date(2026, 12, 31) in snapshot.tariffs["deepseek-flash"].excluded_dates


@pytest.mark.asyncio
@pytest.mark.parametrize("next_year_mode", ["placeholder", "404"])
async def test_unpublished_next_year_is_optional_but_current_year_is_required(
    monkeypatch, next_year_mode
):
    service = _service(MemoryStore())
    tariff = _payload()["models"]["deepseek-flash"]
    monkeypatch.setattr(sync, "parse_deepseek_page", lambda html, model: copy.deepcopy(tariff))
    monkeypatch.setattr(sync, "parse_holiday_calendar", lambda raw, year: [f"{year}-10-01"])

    async def fetch(url, *, optional_calendar=False):
        if url == sync.PRICE_URL:
            return "fixture HTML"
        if optional_calendar:
            if next_year_mode == "404":
                raise sync.CalendarNotPublished("not published")
            return json.dumps({"year": 2027, "papers": [], "days": []})
        return json.dumps({"year": 2026, "papers": ["fixture"], "days": ["fixture"]})

    monkeypatch.setattr(sync, "fetch_public_text", fetch)
    payload = await service._download()
    assert set(payload["calendars"]) == {"2026"}
    service._clock = lambda: NOW.replace(year=2027)

    async def placeholder(url, **kwargs):
        return "HTML" if url == sync.PRICE_URL else json.dumps({"papers": [], "days": []})

    monkeypatch.setattr(sync, "fetch_public_text", placeholder)
    with pytest.raises(sync.CalendarNotPublished):
        await service._download()


@pytest.mark.asyncio
async def test_retired_unconfigured_model_does_not_stop_flash_sync(monkeypatch):
    service = _service(MemoryStore())
    tariff = _payload()["models"]["deepseek-flash"]

    def parse(html, model):
        if model == "deepseek-v4-pro":
            raise sync.MissingModelError("not present")
        return tariff

    monkeypatch.setattr(sync, "parse_deepseek_page", parse)
    monkeypatch.setattr(sync, "parse_holiday_calendar", lambda raw, year: [f"{year}-10-01"])
    monkeypatch.setattr(sync, "fetch_public_text", AsyncMock(return_value="{}"))
    payload = await service._download()
    assert set(sync.decode_snapshot(json.dumps(payload)).tariffs) == {"deepseek-flash"}


@pytest.mark.asyncio
async def test_stop_cancels_sleeping_refresh_task(monkeypatch):
    service = _service(MemoryStore(_payload()))
    await service.initialize()
    read = AsyncMock(wraps=service.refresh)
    monkeypatch.setattr(service, "refresh", read)
    service.start()
    await asyncio.sleep(0)
    task = service._task
    await service.stop()
    assert task.done()
    assert service._task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(302, b""), (200, b"x" * 9)])
async def test_public_fetch_refuses_redirects_and_oversized_bodies(monkeypatch, status, body):
    monkeypatch.setattr(sync, "MAX_RESPONSE_BYTES", 8)

    class Response:
        content_length = None

        def __init__(self):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def iter_chunked(self, size):
            yield body

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"].total == 10
            assert isinstance(kwargs["cookie_jar"], sync.aiohttp.DummyCookieJar)
            assert "headers" not in kwargs and "auth" not in kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            assert url == sync.PRICE_URL
            assert kwargs == {"allow_redirects": False}
            return Response()

    monkeypatch.setattr(sync.aiohttp, "ClientSession", Session)
    with pytest.raises(ValueError):
        await sync.fetch_public_text(sync.PRICE_URL)
