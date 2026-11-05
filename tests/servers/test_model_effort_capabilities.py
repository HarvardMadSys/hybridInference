"""The model listings say which reasoning-effort values each model's routes deliver.

Published as ``reasoning.supported_efforts`` (OpenAI/OpenRouter shape) and
``capabilities.effort`` (Anthropic shape), read only from the registry's own
configuration (``serving.model_effort_capabilities``). These tests pin:

- the three outcomes -- a domain, an empty domain (the parameter is dropped),
  and unknown (published as nothing) -- including incomplete and malformed
  configuration, which is unknown rather than "no effort";
- the conservative aggregation across a model's routes, alias de-duplication,
  caller visibility and auto models, which the listings already had;
- that every value a listing advertises is one ``/v1/messages`` actually
  delivers to the upstream as ``reasoning_effort``.

The registry here is a **test configuration**. Its model names imitate the
kinds of route a deployment has (a DeepSeek-like reasoner behind an
OpenAI-compatible upstream, a plain chat model, a native Anthropic route), but
every domain in it is declared by this file, not taken from any provider. The
listings it produces are kept as golden files under
``tests/fixtures/model_effort_listing/``, which the cloud agent's tests consume
as the gateway's real answer; ``write_golden_listings`` regenerates them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters import OpenAICompatAdapter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.model_effort_capabilities import (
    adapter_effort_domain,
    anthropic_capabilities,
    model_effort_domain,
    openai_reasoning,
)
from serving.servers.auth import optional_verify_api_key, verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import AppServices
from serving.servers.routers import anthropic_messages, models
from tests.servers.conftest import ANTHROPIC_TEST_API_KEY

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "model_effort_listing"

#: Declared by this test, not by DeepSeek: a DeepSeek-like reasoner reached
#: through an OpenAI-compatible upstream, as the screenshot's model was.
DEEPSEEK_LIKE = "deepseek-like-reasoner"
DEEPSEEK_LIKE_DOMAIN = ["high", "max"]


class _Passive(BaseAdapter):
    """An adapter that does not forward ``reasoning_effort`` itself (native Anthropic, Vertex…)."""

    native_format = "anthropic"

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict:
        raise AssertionError("not called")  # pragma: no cover

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        raise AssertionError("not called")
        yield ""


def _cfg(model_id: str, *, params: list[str], efforts: Any = None, provider: str = "p") -> Any:
    cfg = ModelConfig(
        id=model_id,
        name=model_id,
        provider=provider,
        base_url=f"https://{provider}.upstream.test",
        api_key="upstream-test-key",
        chat_path="/chat/completions",
        max_output_length=4096,
        supported_params=["temperature", "max_tokens", "stream", *params],
    )
    # Set after construction so a malformed value reaches the listing as a
    # hand-edited YAML would, without the dataclass's type.
    cfg.reasoning_efforts = efforts if efforts is not None else []
    return cfg


def _compat(model_id: str, **kwargs: Any) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(_cfg(model_id, **kwargs))


def fixture_router(*, admin_only_reasoner: bool = False) -> FixedRouter:
    """The registry both listings and the delivery test read."""
    router = FixedRouter()
    effort = ["reasoning_effort"]
    router.register_route(
        DEEPSEEK_LIKE,
        [(_compat(DEEPSEEK_LIKE, params=effort, efforts=DEEPSEEK_LIKE_DOMAIN), 1.0)],
        admin_only=admin_only_reasoner,
    )
    # Listed once, under its canonical id, with the first route's adapters.
    router.register_route(
        "deepseek-like-alias",
        [(_compat(DEEPSEEK_LIKE, params=effort, efforts=DEEPSEEK_LIKE_DOMAIN), 1.0)],
        admin_only=admin_only_reasoner,
    )
    router.register_route("plain-chat", [(_compat("plain-chat", params=[]), 1.0)])
    # Takes the parameter but declares no domain: incomplete, so unknown.
    router.register_route(
        "undeclared-reasoner", [(_compat("undeclared-reasoner", params=effort), 1.0)]
    )
    # A hand-edited domain that is not a list of level names: unknown.
    router.register_route(
        "malformed-domain",
        [(_compat("malformed-domain", params=effort, efforts=["high", 3]), 1.0)],
    )
    router.register_route(
        "native-anthropic",
        [(_Passive(_cfg("native-anthropic", params=effort, efforts=["high"])), 1.0)],
    )
    # Two routes: only what both deliver.
    router.register_route(
        "mixed-routes",
        [
            (
                _compat(
                    "mixed-routes", params=effort, efforts=["low", "high", "max"], provider="a"
                ),
                0.5,
            ),
            (
                _compat(
                    "mixed-routes", params=effort, efforts=["high", "xhigh", "max"], provider="b"
                ),
                0.5,
            ),
        ],
    )
    # Values the Anthropic shape has no level for.
    router.register_route(
        "minimal-only",
        [(_compat("minimal-only", params=effort, efforts=["none", "minimal"]), 1.0)],
    )
    router.register_route(
        "staged-reasoner",
        [(_compat("staged-reasoner", params=effort, efforts=["high"]), 1.0)],
        published=False,
    )
    return router


def fixture_app(router: FixedRouter | None = None) -> FastAPI:
    app = FastAPI()
    app.state.services = AppServices(router=router or fixture_router(), db_logger=None)  # type: ignore[attr-defined]
    # An anonymous caller: the listing's own visibility rules still apply.
    app.dependency_overrides[optional_verify_api_key] = lambda: None
    app.include_router(models.router)
    return app


async def fetch_listings(app: FastAPI | None = None) -> dict[str, Any]:
    """Both listings, exactly as a client asking each way receives them."""
    transport = ASGITransport(app=app or fixture_app())
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        openai = await client.get("/v1/models", headers={"authorization": "Bearer caller"})
        anthropic = await client.get(
            "/v1/models",
            headers={"x-api-key": "caller", "anthropic-version": "2023-06-01"},
        )
    assert openai.status_code == anthropic.status_code == 200
    return {"openai": openai.json(), "anthropic": anthropic.json()}


def _normalized(listing: dict[str, Any]) -> dict[str, Any]:
    """The listing without its import-time timestamp, which differs per process."""
    for entry in listing.get("data", []):
        if "created" in entry:
            entry["created"] = 0
    return listing


async def write_golden_listings() -> None:  # pragma: no cover - run by hand
    """Regenerate the golden files: ``uv run python -c 'import asyncio, tests.servers.test_model_effort_capabilities as t; asyncio.run(t.write_golden_listings())'``."""
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for shape, listing in (await fetch_listings()).items():
        (GOLDEN / f"{shape}.json").write_text(
            json.dumps(_normalized(listing), indent=2, sort_keys=True) + "\n"
        )


def _entries(listing: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in listing["data"]}


# -- the rules ----------------------------------------------------------------


def test_an_adapter_that_forwards_with_a_domain_delivers_exactly_it():
    assert adapter_effort_domain(
        _compat("m", params=["reasoning_effort"], efforts=["low", "max"])
    ) == [
        "low",
        "max",
    ]


def test_an_adapter_without_the_parameter_delivers_nothing():
    assert adapter_effort_domain(_compat("m", params=[], efforts=["high"])) == []


@pytest.mark.parametrize(
    "efforts",
    [[], ["high", 3], ["high", "high"], [""], [" high"], "high", None],
    ids=["no-domain", "non-string", "duplicate", "empty", "padded", "not-a-list", "none"],
)
def test_incomplete_or_malformed_domains_are_unknown(efforts):
    adapter = _compat("m", params=["reasoning_effort"])
    adapter.config.reasoning_efforts = efforts
    assert adapter_effort_domain(adapter) is None


def test_an_adapter_that_does_not_forward_is_unknown_whatever_it_declares():
    adapter = _Passive(_cfg("m", params=["reasoning_effort"], efforts=["high"]))
    assert adapter_effort_domain(adapter) is None


def test_a_model_publishes_only_what_every_route_delivers():
    first = _compat("m", params=["reasoning_effort"], efforts=["low", "high", "max"])
    second = _compat("m", params=["reasoning_effort"], efforts=["max", "high"])
    dropped = _compat("m", params=[])
    passive = _Passive(_cfg("m", params=["reasoning_effort"], efforts=["high"]))
    assert model_effort_domain([first, second]) == ["high", "max"]
    assert model_effort_domain([first, dropped]) == []
    assert model_effort_domain([first, passive]) is None
    assert model_effort_domain([]) is None


def test_the_two_shapes():
    assert openai_reasoning(None) is None
    assert openai_reasoning([]) == {"supported_efforts": []}
    assert anthropic_capabilities(None) is None
    effort = anthropic_capabilities(["high", "max", "minimal"])["effort"]
    assert effort["supported"] is True
    assert {
        level for level, value in effort.items() if level != "supported" and value["supported"]
    } == {
        "high",
        "max",
    }
    assert anthropic_capabilities(["minimal"])["effort"]["supported"] is False


# -- the listings -------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_listing_publishes_the_reasoning_object():
    entries = _entries((await fetch_listings())["openai"])
    assert entries[DEEPSEEK_LIKE]["reasoning"] == {"supported_efforts": DEEPSEEK_LIKE_DOMAIN}
    assert entries["plain-chat"]["reasoning"] == {"supported_efforts": []}
    assert entries["mixed-routes"]["reasoning"] == {"supported_efforts": ["high", "max"]}
    assert entries["minimal-only"]["reasoning"] == {"supported_efforts": ["none", "minimal"]}
    for unknown in ("undeclared-reasoner", "malformed-domain", "native-anthropic"):
        assert entries[unknown]["reasoning"] is None
    # Aliases are still one entry; unpublished routes still absent.
    assert "deepseek-like-alias" not in entries
    assert "staged-reasoner" not in entries


@pytest.mark.asyncio
async def test_anthropic_listing_publishes_capabilities_effort():
    listing = (await fetch_listings())["anthropic"]
    entries = _entries(listing)
    assert set(entries[DEEPSEEK_LIKE]) == {
        "type",
        "id",
        "display_name",
        "created_at",
        "capabilities",
    }
    effort = entries[DEEPSEEK_LIKE]["capabilities"]["effort"]
    assert effort == {
        "supported": True,
        "low": {"supported": False},
        "medium": {"supported": False},
        "high": {"supported": True},
        "xhigh": {"supported": False},
        "max": {"supported": True},
    }
    assert entries["plain-chat"]["capabilities"]["effort"]["supported"] is False
    assert entries["minimal-only"]["capabilities"]["effort"]["supported"] is False
    for unknown in ("undeclared-reasoner", "malformed-domain", "native-anthropic"):
        assert "capabilities" not in entries[unknown]
    assert listing["has_more"] is False


@pytest.mark.asyncio
async def test_the_dedicated_anthropic_route_carries_the_same_capabilities():
    transport = ASGITransport(app=fixture_app())
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        dedicated = (await client.get("/anthropic/v1/models")).json()
    assert dedicated == (await fetch_listings())["anthropic"]


@pytest.mark.asyncio
async def test_a_model_hidden_from_the_caller_publishes_nothing():
    listings = await fetch_listings(fixture_app(fixture_router(admin_only_reasoner=True)))
    assert DEEPSEEK_LIKE not in _entries(listings["openai"])
    assert DEEPSEEK_LIKE not in _entries(listings["anthropic"])


@pytest.mark.asyncio
async def test_the_golden_listings_are_what_the_gateway_answers():
    listings = await fetch_listings()
    for shape, listing in listings.items():
        golden = json.loads((GOLDEN / f"{shape}.json").read_text())
        assert _normalized(listing) == golden, f"regenerate {shape}.json (write_golden_listings)"


# -- what is advertised is delivered -----------------------------------------


def _messages_app(router: FixedRouter, services_kwargs: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.state.services = AppServices(router=router, **services_kwargs)

    async def fake_verify(x_api_key: str | None = Header(None, alias="X-API-Key")):
        if x_api_key != ANTHROPIC_TEST_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, "user_id": "test-user", "role": "internal"}

    app.dependency_overrides[verify_api_key] = fake_verify
    app.dependency_overrides[optional_verify_api_key] = lambda: None
    app.dependency_overrides[enforce_user_concurrency] = lambda: None
    app.include_router(models.router)
    app.include_router(anthropic_messages.router)
    return app


@pytest.mark.asyncio
async def test_every_advertised_level_reaches_the_upstream(
    monkeypatch, mock_db_logger, mock_operational_store, mock_log_store
):
    """Claude Code's ``output_config.effort`` for each listed level arrives as that level."""
    from serving.http import AsyncHTTPClient

    sent: list[dict[str, Any]] = []

    async def fake_post(self, url, json=None, headers=None, timeout=None, retries=2):
        sent.append(json)
        return {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": json["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(AsyncHTTPClient, "json_post_with_retry", fake_post)
    router = fixture_router()
    app = _messages_app(
        router,
        {
            "db_logger": mock_db_logger,
            "operational_store": mock_operational_store,
            "log_store": mock_log_store,
            "routing_manager": None,
        },
    )
    listings = await fetch_listings(app)
    advertised = {
        entry["id"]: [
            level
            for level, value in entry["capabilities"]["effort"].items()
            if level != "supported" and value["supported"]
        ]
        for entry in listings["anthropic"]["data"]
        if "capabilities" in entry
    }
    assert advertised[DEEPSEEK_LIKE] == ["high", "max"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        for model, levels in advertised.items():
            for level in levels:
                sent.clear()
                response = await client.post(
                    "/v1/messages",
                    headers={"x-api-key": ANTHROPIC_TEST_API_KEY},
                    json={
                        "model": model,
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": "hi"}],
                        "output_config": {"effort": level},
                    },
                )
                assert response.status_code == 200, (model, level)
                assert sent and sent[-1]["reasoning_effort"] == level, (model, level)
