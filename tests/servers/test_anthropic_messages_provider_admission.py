"""Regression tests: /v1/messages honors circuit health and provider disable.

This surface forwards an Anthropic-native body, which FixedRouter has no method
for, so it picks its own adapter instead of dispatching through the router. Both
of the router's admission rules therefore have to be applied at the pick site:
an open circuit must be skipped while a healthy sibling exists, and an
admin-disabled provider must not serve traffic here.
"""

from __future__ import annotations

import pytest

from routing.endpoints import endpoint_id_for_adapter
from serving.adapters import OpenAICompatAdapter
from serving.adapters.base import ModelConfig
from serving.servers.routers import anthropic_messages

DUAL_MODEL = "dual-provider-model"
SINGLE_MODEL = "glm-4.7"
PRIMARY_URL = "https://example-primary.test"
BACKUP_URL = "https://example-backup.test"
THIRD_URL = "https://example-third.test"

_PRICING = {
    "prompt": "0.6",
    "completion": "2.2",
    "image": "0",
    "request": "0",
    "input_cache_reads": "0",
    "input_cache_writes": "0",
}


def _auth():
    from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


def _adapter(provider: str, base_url: str) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        ModelConfig(
            id=DUAL_MODEL,
            name=DUAL_MODEL,
            provider=provider,
            base_url=base_url,
            api_key=f"{provider}-test",
            chat_path="/chat/completions",
            max_output_length=1024,
            supported_params=["temperature", "max_tokens", "stream"],
            pricing=_PRICING,
        )
    )


def _tiered_adapter(provider: str, base_url: str, keys_min_roles: dict[str, str]):
    """Build an adapter whose key pool reserves each key for a minimum role."""
    adapter = OpenAICompatAdapter(
        ModelConfig(
            id=DUAL_MODEL,
            name=DUAL_MODEL,
            provider=provider,
            base_url=base_url,
            api_keys=list(keys_min_roles),
            chat_path="/chat/completions",
            max_output_length=1024,
            pricing=_PRICING,
        )
    )
    for key, role in keys_min_roles.items():
        adapter._key_pool.set_key_min_role(key, role)
    return adapter


def _register_dual_route(router):
    """Register one model served by a preferred provider and a backup."""
    primary = _adapter("zai", PRIMARY_URL)
    backup = _adapter("ollama", BACKUP_URL)
    router.register_route(DUAL_MODEL, [(primary, 0.9), (backup, 0.1)])
    return primary, backup


def _open_circuit(router, adapter) -> None:
    """Drive an adapter's circuit to OPEN through the real failure path."""
    registry = router.endpoint_health_registry
    endpoint_id = endpoint_id_for_adapter(adapter)
    for _ in range(20):
        registry.record_failure(endpoint_id, reason="test_outage")
        if not registry.allow_request(endpoint_id):
            return
    raise AssertionError(f"circuit for {endpoint_id} never opened")


class _StaticDisabledResolver:
    """Minimal admin disabled-provider resolver."""

    def __init__(self, disabled: set[str]) -> None:
        self._disabled = set(disabled)

    def is_disabled(self, provider: str) -> bool:
        return provider in self._disabled


def _capture_upstream(monkeypatch) -> list[str]:
    """Patch the HTTP client and return the list of URLs it is asked to POST."""
    from serving.http import AsyncHTTPClient

    urls: list[str] = []
    upstream_resp = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": DUAL_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        urls.append(url)
        return upstream_resp

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    return urls


def _body(model: str = DUAL_MODEL) -> dict:
    return {
        "model": model,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.mark.asyncio
async def test_open_circuit_is_not_selected_while_a_healthy_adapter_exists(
    anthropic_test_client, anthropic_compat_router, monkeypatch
):
    """The preferred adapter's circuit is open: dispatch must go to the backup."""
    primary, _backup = _register_dual_route(anthropic_compat_router)
    _open_circuit(anthropic_compat_router, primary)
    urls = _capture_upstream(monkeypatch)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 200
    assert urls and urls[0].startswith(BACKUP_URL)


@pytest.mark.asyncio
async def test_resolve_skips_the_open_circuit(anthropic_test_client, anthropic_compat_router):
    """Same rule at the resolution seam, independent of the HTTP surface."""
    primary, backup = _register_dual_route(anthropic_compat_router)
    _open_circuit(anthropic_compat_router, primary)

    _canonical, _route, adapter = await anthropic_messages._resolve(
        DUAL_MODEL, anthropic_compat_router, None
    )

    assert adapter is backup


@pytest.mark.asyncio
async def test_admin_disabled_provider_does_not_serve_v1_messages(
    anthropic_test_client, anthropic_compat_router, monkeypatch
):
    """An admin-disabled provider is skipped in favor of an enabled sibling."""
    _primary, _backup = _register_dual_route(anthropic_compat_router)
    anthropic_compat_router.disabled_provider_resolver = _StaticDisabledResolver({"zai"})
    urls = _capture_upstream(monkeypatch)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 200
    assert urls and urls[0].startswith(BACKUP_URL)


@pytest.mark.asyncio
async def test_disabling_every_provider_takes_the_model_off_this_surface(
    anthropic_test_client, anthropic_compat_router, monkeypatch
):
    """With no enabled provider left the request is refused, not dispatched."""
    _register_dual_route(anthropic_compat_router)
    anthropic_compat_router.disabled_provider_resolver = _StaticDisabledResolver({"zai", "ollama"})
    urls = _capture_upstream(monkeypatch)

    r = await anthropic_test_client.post("/v1/messages", json=_body(), headers=_auth())

    assert r.status_code == 503
    assert r.json()["error"]["type"] == "overloaded_error"
    assert urls == []


@pytest.mark.asyncio
async def test_full_outage_on_a_single_provider_model_returns_503(
    anthropic_test_client, anthropic_compat_router, monkeypatch
):
    """The only provider's circuit is open: 503 overloaded, no upstream call."""
    adapter, _weight = anthropic_compat_router.routes[SINGLE_MODEL].adapters[0]
    _open_circuit(anthropic_compat_router, adapter)
    urls = _capture_upstream(monkeypatch)

    r = await anthropic_test_client.post("/v1/messages", json=_body(SINGLE_MODEL), headers=_auth())

    assert r.status_code == 503
    assert r.json()["error"]["type"] == "overloaded_error"
    assert urls == []


@pytest.mark.asyncio
async def test_streaming_dispatch_also_skips_the_open_circuit(
    anthropic_test_client, anthropic_compat_router, monkeypatch
):
    """Adapter selection happens before the stream, so streaming inherits the rule."""
    primary, backup = _register_dual_route(anthropic_compat_router)
    _open_circuit(anthropic_compat_router, primary)

    seen: list[str] = []

    async def fake_stream(self, body, request_id=None, usage_sink=None, extra_headers=None):
        seen.append(self.config.base_url)
        yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    monkeypatch.setattr(OpenAICompatAdapter, "stream_messages", fake_stream)

    body = _body()
    body["stream"] = True
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())

    assert r.status_code == 200
    assert seen == [backup.config.base_url]


@pytest.mark.asyncio
async def test_key_tier_preference_never_resurrects_an_inadmissible_adapter(
    anthropic_test_client, anthropic_compat_router
):
    """Admission runs first: the key-tier preference only reorders survivors.

    An adapter with an open circuit holds a key this caller could spend, and the
    next adapter's keys are reserved above them. Neither may win over the third,
    which is both admitted and serviceable.
    """
    open_circuit = _tiered_adapter("zai", PRIMARY_URL, {"free-key": "free"})
    reserved = _tiered_adapter("ollama", BACKUP_URL, {"pro-only-key": "pro"})
    serviceable = _tiered_adapter("featherless", THIRD_URL, {"shared-key": "free"})
    anthropic_compat_router.register_route(
        DUAL_MODEL, [(open_circuit, 0.8), (reserved, 0.1), (serviceable, 0.1)]
    )
    _open_circuit(anthropic_compat_router, open_circuit)

    _canonical, _route, adapter = await anthropic_messages._resolve(
        DUAL_MODEL, anthropic_compat_router, {"role": "free"}
    )

    assert adapter is serviceable


@pytest.mark.asyncio
async def test_count_tokens_still_answers_during_a_full_outage(
    anthropic_test_client, anthropic_compat_router
):
    """count_tokens is computed locally, so an outage must not blind the client."""
    adapter, _weight = anthropic_compat_router.routes[SINGLE_MODEL].adapters[0]
    _open_circuit(anthropic_compat_router, adapter)

    r = await anthropic_test_client.post(
        "/v1/messages/count_tokens",
        json={"model": SINGLE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )

    assert r.status_code == 200
    assert r.json()["input_tokens"] > 0
