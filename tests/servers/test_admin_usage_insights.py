"""Tests for the admin Usage Insights endpoints.

Covers POST /admin/usage-insights/analyze plus the GET/PUT
/admin/usage-insights/settings provider configuration. The analysis provider
(API key + model) is stored server-side in site_settings and read via the
operational store, so the tests wire a mock store alongside the mock db logger.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import usage_insights


def _sample_rows():
    """Two historical api_logs rows: the turns still live inside request_payload.

    This is the pre-de-duplication shape — ``prompt`` was written too, but rows
    from before that column was populated, and any row whose payload copy is the
    only content, must keep rendering. ``prompt`` is NULL here so these cases
    exercise the request_payload fallback specifically.
    """
    return [
        {
            "timestamp": datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
            "model_id": "glm-5.1",
            "provider": "zhipu",
            "metadata": {"user_agent": "claude-cli/1.2.0"},
            "request_payload": {
                "messages": [
                    {"role": "system", "content": "You are Claude Code."},
                    {"role": "user", "content": "Fix the bug in foo.py"},
                ]
            },
            "prompt": None,
        },
        {
            "timestamp": datetime(2026, 6, 25, 12, 5, tzinfo=timezone.utc),
            "model_id": "minimax-m3",
            "provider": "minimax",
            "metadata": {"user_agent": "kilo-code/0.9"},
            "request_payload": {
                "system": [{"type": "text", "text": "Kilo Code agent"}],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "refactor"}]}],
            },
            "prompt": None,
        },
    ]


def _sample_rows_new_shape():
    """The same two requests as stored today: no ``messages`` in request_payload.

    The storage layer strips ``messages``/``tools`` before insert because they are
    already written to the dedicated ``prompt``/``tools`` columns, so the payload
    keeps only the call parameters (plus ``system``, which is not stripped).
    ``prompt`` is TEXT holding ``json.dumps(messages)``, which is what asyncpg
    hands back.
    """
    return [
        {
            "timestamp": datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
            "model_id": "glm-5.1",
            "provider": "zhipu",
            "metadata": {"user_agent": "claude-cli/1.2.0"},
            "request_payload": {"model": "glm-5.1", "stream": True, "temperature": 0.2},
            "prompt": json.dumps(
                [
                    {"role": "system", "content": "You are Claude Code."},
                    {"role": "user", "content": "Fix the bug in foo.py"},
                ]
            ),
        },
        {
            "timestamp": datetime(2026, 6, 25, 12, 5, tzinfo=timezone.utc),
            "model_id": "minimax-m3",
            "provider": "minimax",
            "metadata": {"user_agent": "kilo-code/0.9"},
            # Anthropic surface: ``system`` survives in the payload, turns do not.
            "request_payload": {
                "system": [{"type": "text", "text": "Kilo Code agent"}],
                "max_tokens": 4096,
            },
            "prompt": json.dumps(
                [{"role": "user", "content": [{"type": "text", "text": "refactor"}]}]
            ),
        },
    ]


def _make_op_store(api_key: str | None = "sk-configured-key", model: str | None = "glm-5.1"):
    """A mock operational store that serves the usage-insights settings."""

    async def _get_setting(key):
        if key == usage_insights._SETTING_API_KEY and api_key is not None:
            return {"value": api_key, "value_type": "str"}
        if key == usage_insights._SETTING_MODEL and model is not None:
            return {"value": model, "value_type": "str"}
        return None

    store = MagicMock()
    store.get_setting = AsyncMock(side_effect=_get_setting)
    store.set_setting = AsyncMock()
    store.delete_setting = AsyncMock()
    store.log_admin_action = AsyncMock()
    return store


class TestAdminUsageInsightsAnalyze:
    @pytest.fixture
    def admin_app(self, mock_db_logger):
        """Minimal FastAPI app with the admin router, mock pool, and configured store."""
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=_sample_rows())
        mock_conn.fetchrow = AsyncMock(return_value={"id": "user-123"})
        # log_admin_action falls back to the legacy pool path for the db logger
        # (DatabaseLogger has no log_admin_action), so conn.execute must be awaitable.
        mock_conn.execute = AsyncMock()

        app = FastAPI(title="Usage Insights Test")
        services = AppServices(
            router=MagicMock(),
            db_logger=mock_db_logger,
            routing_manager=None,
        )
        services.operational_store = _make_op_store()
        app.state.services = services  # type: ignore[attr-defined]
        app.include_router(admin_router.router)
        return app

    @pytest.fixture
    def _patch_llm(self, monkeypatch):
        called = {}

        async def _fake(api_key, model, content):
            called["api_key"] = api_key
            called["model"] = model
            called["content"] = content
            return "## Client tools\nClaude Code and Kilo Code dominate."

        monkeypatch.setattr(usage_insights, "_call_analysis_model", _fake)
        return called

    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self, admin_app):
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_analyze_returns_markdown(self, admin_app, _patch_llm):
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={})
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert "Claude Code" in body["analysis"]
        # Model comes from the server-side setting, not the request body.
        assert body["model"] == "glm-5.1"
        assert body["sampled_requests"] == 2
        assert body["scope"] == "all users"
        assert "generated_at" in body
        # The stored key is the one handed to the analysis model.
        assert _patch_llm["api_key"] == "sk-configured-key"
        # The rendered content handed to the LLM includes harness identifiers.
        assert "Kilo Code agent" in _patch_llm["content"]
        # Historical rows keep their turns inside request_payload — still rendered.
        assert "Fix the bug in foo.py" in _patch_llm["content"]
        assert "<none captured>" not in _patch_llm["content"]

    @pytest.mark.asyncio
    async def test_analyze_renders_rows_whose_payload_has_no_messages(
        self, admin_app, mock_db_logger, _patch_llm
    ):
        """Rows stored today keep the turns only in ``prompt`` — the report must use it.

        Reading ``request_payload["messages"]`` alone would still return 200 here,
        with every sampled request rendered as "<none captured>": a silent, plausible
        looking report built from nothing. Assert the real content instead.
        """
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=_sample_rows_new_shape())

        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={})
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["sampled_requests"] == 2
        content = _patch_llm["content"]
        assert "<none captured>" not in content
        # OpenAI shape: both the system turn and the user turn come from `prompt`.
        assert "You are Claude Code." in content
        assert "Fix the bug in foo.py" in content
        # Anthropic shape: `system` still comes from the payload, the turn from `prompt`.
        assert "Kilo Code agent" in content
        assert "refactor" in content

    @pytest.mark.asyncio
    async def test_analyze_scopes_to_user_email(self, admin_app, _patch_llm):
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/usage-insights/analyze",
                json={"user_email": "heavy@user.com"},
            )
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["scope"] == "heavy@user.com"

    @pytest.mark.asyncio
    async def test_analyze_scopes_to_user_id(self, admin_app, _patch_llm):
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/usage-insights/analyze",
                json={"user_id": "user-999", "limit": 10},
            )
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["scope"] == "user-999"

    @pytest.mark.asyncio
    async def test_analyze_400_when_not_configured(self, mock_db_logger, _patch_llm):
        """No stored API key → 400 directing the admin to Settings."""
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=_sample_rows())

        app = FastAPI()
        services = AppServices(router=MagicMock(), db_logger=mock_db_logger, routing_manager=None)
        services.operational_store = _make_op_store(api_key=None)
        app.state.services = services  # type: ignore[attr-defined]
        app.include_router(admin_router.router)
        app.dependency_overrides[verify_admin_access] = lambda: "admin@test"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={})

        assert resp.status_code == 400
        # Error-body shape varies (detail vs error.message) by handler; the text
        # must point the admin at Settings either way.
        assert "Settings" in resp.text

    @pytest.mark.asyncio
    async def test_analyze_404_when_no_samples(self, admin_app, mock_db_logger, _patch_llm):
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=[])

        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={})
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_analyze_404_when_user_email_unknown(self, admin_app, mock_db_logger, _patch_llm):
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow = AsyncMock(return_value=None)

        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/usage-insights/analyze",
                json={"user_email": "nobody@x.com"},
            )
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 404


class TestAdminUsageInsightsSettings:
    @pytest.fixture
    def app_with_store(self, mock_db_logger):
        def _build(store):
            app = FastAPI()
            services = AppServices(
                router=MagicMock(), db_logger=mock_db_logger, routing_manager=None
            )
            services.operational_store = store
            app.state.services = services  # type: ignore[attr-defined]
            app.include_router(admin_router.router)
            return app

        return _build

    @pytest.mark.asyncio
    async def test_get_requires_admin(self, app_with_store):
        app = app_with_store(_make_op_store())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/usage-insights/settings")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_get_returns_masked_hint(self, app_with_store):
        app = app_with_store(_make_op_store(api_key="sk-abcd1234", model="minimax-m3"))
        app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/usage-insights/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["api_key_hint"] == "…1234"  # only a tail, never the full key
        assert body["model"] == "minimax-m3"

    @pytest.mark.asyncio
    async def test_get_unconfigured_uses_default_model(self, app_with_store):
        app = app_with_store(_make_op_store(api_key=None, model=None))
        app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/usage-insights/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is False
        assert body["api_key_hint"] is None
        assert body["model"] == usage_insights._DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_put_sets_key_and_model(self, app_with_store):
        store = _make_op_store(api_key=None, model=None)
        app = app_with_store(store)
        app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/admin/usage-insights/settings",
                json={"api_key": "  sk-new-key  ", "model": "minimax-m3"},
            )
        assert resp.status_code == 200
        # Key is trimmed and stored under the API-key setting; model is stored too.
        store.set_setting.assert_any_call(
            usage_insights._SETTING_API_KEY, "sk-new-key", "str", "admin@test"
        )
        store.set_setting.assert_any_call(
            usage_insights._SETTING_MODEL, "minimax-m3", "str", "admin@test"
        )

    @pytest.mark.asyncio
    async def test_put_rejects_whitespace_model(self, app_with_store):
        """A whitespace-only model passes min_length=1 but must not be stored as ''."""
        store = _make_op_store()
        app = app_with_store(store)
        app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                "/admin/usage-insights/settings",
                json={"api_key": "sk-keep", "model": "   "},
            )
        assert resp.status_code == 400
        # The model is rejected before any write, so the api_key is not half-applied.
        store.set_setting.assert_not_called()
        store.delete_setting.assert_not_called()

    @pytest.mark.asyncio
    async def test_put_empty_key_clears(self, app_with_store):
        store = _make_op_store()
        app = app_with_store(store)
        app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put("/admin/usage-insights/settings", json={"api_key": ""})
        assert resp.status_code == 200
        store.delete_setting.assert_called_once_with(usage_insights._SETTING_API_KEY)

    @pytest.mark.asyncio
    async def test_put_requires_admin(self, app_with_store):
        app = app_with_store(_make_op_store())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put("/admin/usage-insights/settings", json={"model": "glm-5.1"})
        assert resp.status_code == 401


class TestFetchSamples:
    @pytest.mark.asyncio
    async def test_randomly_samples_from_recent_pool(self):
        """The query draws a random sample from a bounded recent window, not the latest N."""
        captured: dict = {}

        async def _fetch(query, *params):
            captured["query"] = query
            captured["params"] = params
            return _sample_rows()

        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        payload = usage_insights.UsageInsightsRequest(user_id="user-1", limit=100)

        samples = await usage_insights._fetch_samples(conn, "user-1", payload)

        # Randomized draw, bounded to the recent candidate pool then the limit.
        assert "random()" in captured["query"]
        assert captured["params"] == ("user-1", usage_insights._SAMPLE_POOL, 100)
        assert len(samples) == 2
        # The turns live in their own column now, so the query must fetch it.
        assert "prompt" in captured["query"]

    @pytest.mark.asyncio
    async def test_extracts_turns_from_the_prompt_column(self):
        """Rows whose payload no longer carries ``messages`` still yield content."""

        async def _fetch(query, *params):
            return _sample_rows_new_shape()

        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        payload = usage_insights.UsageInsightsRequest(limit=10)

        samples = await usage_insights._fetch_samples(conn, None, payload)

        by_model = {s["model_id"]: s for s in samples}
        openai_row = by_model["glm-5.1"]
        assert openai_row["system_opener"] == "You are Claude Code."
        assert openai_row["user_messages"] == ["Fix the bug in foo.py"]
        anthropic_row = by_model["minimax-m3"]
        assert anthropic_row["system_opener"] == "Kilo Code agent"
        assert anthropic_row["user_messages"] == ["refactor"]

    @pytest.mark.asyncio
    async def test_still_extracts_turns_from_historical_payloads(self):
        """Old rows keep their copy inside request_payload; the fallback reads it."""

        async def _fetch(query, *params):
            return _sample_rows()

        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        payload = usage_insights.UsageInsightsRequest(limit=10)

        samples = await usage_insights._fetch_samples(conn, None, payload)

        by_model = {s["model_id"]: s for s in samples}
        assert by_model["glm-5.1"]["system_opener"] == "You are Claude Code."
        assert by_model["glm-5.1"]["user_messages"] == ["Fix the bug in foo.py"]
        assert by_model["minimax-m3"]["user_messages"] == ["refactor"]

    @pytest.mark.asyncio
    async def test_samples_rendered_newest_first(self):
        """A randomly-ordered draw is re-sorted so the report reads chronologically."""

        async def _fetch(query, *params):
            # _sample_rows() is oldest-first (12:00 then 12:05); return it as-is so
            # the re-sort has to actually reorder it (random() yields no order).
            return _sample_rows()

        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        payload = usage_insights.UsageInsightsRequest(limit=50)

        samples = await usage_insights._fetch_samples(conn, None, payload)

        timestamps = [s["timestamp"] for s in samples]
        # Newest-first, and genuinely reordered from the oldest-first input.
        assert timestamps == sorted(timestamps, reverse=True)
        assert samples[0]["model_id"] == "minimax-m3"  # the 12:05 row, now first


class TestRenderSamples:
    def test_render_budget_truncates(self):
        """_render_samples stops adding requests once the char budget is hit."""
        big = "x" * 5000
        samples = [
            {
                "timestamp": "2026-06-25T12:00:00+00:00",
                "model_id": "m",
                "provider": "p",
                "user_agent": "ua",
                "referer": None,
                "system_opener": big,
                "user_messages": [big],
            }
            for _ in range(50)
        ]
        rendered, used = usage_insights._render_samples(samples)
        assert used < 50
        assert len(rendered) <= usage_insights._MAX_PROMPT_CHARS + 5000


class TestCallAnalysisModel:
    @pytest.fixture(autouse=True)
    def _deployment_knows_its_own_address(self, monkeypatch):
        """The feature posts to this gateway, so it has to be told which one.

        There used to be a localhost:8080 fallback here. It is right for the
        Compose stack and wrong for the systemd unit in this repository, which
        listens on 8000 — so these cases name the address rather than inherit
        a guess, and the guess is gone.
        """
        monkeypatch.setenv("SITE_PUBLIC_BASE_URL", "http://localhost:8080")

    @pytest.mark.asyncio
    async def test_without_a_configured_address_it_says_so(self):
        """A 503 naming the missing setting beats posting prompts at a guess."""
        import os

        from fastapi import HTTPException

        from serving.servers.routers.admin.usage_insights import _analysis_base_url

        previous = os.environ.pop("SITE_PUBLIC_BASE_URL", None)
        try:
            with pytest.raises(HTTPException) as caught:
                _analysis_base_url()
        finally:
            if previous is not None:
                os.environ["SITE_PUBLIC_BASE_URL"] = previous
        assert caught.value.status_code == 503
        assert "SITE_PUBLIC_BASE_URL" in caught.value.detail

    @pytest.mark.asyncio
    async def test_marks_synthetic_probe_and_builds_url(self, monkeypatch):
        """The outbound call targets <base>/chat/completions and is a synthetic probe.

        The synthetic-probe header keeps the gateway from logging (and later
        re-sampling) this feature's own analysis requests.
        """
        captured: dict = {}

        class _FakeClient:
            async def json_post(self, url, *, json, headers, timeout):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return {"choices": [{"message": {"content": "  report  "}}]}

        monkeypatch.setattr(usage_insights.AsyncHTTPClient, "shared", lambda: _FakeClient())

        text = await usage_insights._call_analysis_model("sk-x", "glm-5.1", "the content")

        assert text == "report"
        # The target is this deployment's own gateway; unconfigured, that is the
        # local one. Sampled user prompts must never leave for a hardcoded host.
        assert captured["url"] == "http://localhost:8080/v1/chat/completions"
        assert captured["headers"]["X-Probe"] == "synthetic"
        assert captured["headers"]["Authorization"] == "Bearer sk-x"
        assert captured["json"]["model"] == "glm-5.1"
        # Output is bounded so a slow report can't exceed the edge proxy timeout.
        assert captured["json"]["max_tokens"] == usage_insights._MAX_OUTPUT_TOKENS

    @pytest.mark.asyncio
    async def test_timeout_returns_clean_504(self, monkeypatch):
        """An upstream timeout becomes a JSON 504 (not an edge "Network error")."""

        class _SlowClient:
            async def json_post(self, url, *, json, headers, timeout):
                raise TimeoutError

        monkeypatch.setattr(usage_insights.AsyncHTTPClient, "shared", lambda: _SlowClient())
        with pytest.raises(HTTPException) as exc:
            await usage_insights._call_analysis_model("k", "m", "c")
        assert exc.value.status_code == 504
        assert "too long" in exc.value.detail

    @pytest.mark.asyncio
    async def test_extracts_list_content_and_reasoning_fallback(self, monkeypatch):
        """Content as a block list is joined; empty content falls back to reasoning."""

        class _ListClient:
            async def json_post(self, url, *, json, headers, timeout):
                return {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]}

        monkeypatch.setattr(usage_insights.AsyncHTTPClient, "shared", lambda: _ListClient())
        assert await usage_insights._call_analysis_model("k", "m", "c") == "hi"

        class _ReasoningClient:
            async def json_post(self, url, *, json, headers, timeout):
                return {"choices": [{"message": {"content": "", "reasoning_content": "thought"}}]}

        monkeypatch.setattr(usage_insights.AsyncHTTPClient, "shared", lambda: _ReasoningClient())
        assert await usage_insights._call_analysis_model("k", "m", "c") == "thought"
