"""Tests for the POST /admin/usage-insights/analyze endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from serving.servers.deps import AppServices, verify_admin_access
from serving.servers.routers import admin as admin_router
from serving.servers.routers.admin import usage_insights


def _sample_rows():
    """Two api_logs-shaped rows with stored payloads (OpenAI + Anthropic shape)."""
    return [
        {
            "timestamp": datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
            "model_id": "glm-5.2",
            "provider": "zhipu",
            "metadata": {"user_agent": "claude-cli/1.2.0"},
            "request_payload": {
                "messages": [
                    {"role": "system", "content": "You are Claude Code."},
                    {"role": "user", "content": "Fix the bug in foo.py"},
                ]
            },
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
        },
    ]


class TestAdminUsageInsightsRoute:
    @pytest.fixture
    def admin_app(self, mock_db_logger):
        """Minimal FastAPI app with the admin router and a usable mock pool."""
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=_sample_rows())
        mock_conn.fetchrow = AsyncMock(return_value={"id": "user-123"})
        # log_admin_action falls back to the legacy pool path (DatabaseLogger has
        # no log_admin_action), so conn.execute must be awaitable.
        mock_conn.execute = AsyncMock()

        app = FastAPI(title="Usage Insights Test")
        services = AppServices(
            router=MagicMock(),
            db_logger=mock_db_logger,
            routing_manager=None,
        )
        app.state.services = services  # type: ignore[attr-defined]
        app.include_router(admin_router.router)
        return app

    @pytest.fixture
    def _patch_llm(self, monkeypatch):
        called = {}

        async def _fake(payload, content):
            called["model"] = payload.model
            called["content"] = content
            return "## Client tools\nClaude Code and Kilo Code dominate."

        monkeypatch.setattr(usage_insights, "_call_analysis_model", _fake)
        return called

    @pytest.mark.asyncio
    async def test_route_requires_admin_auth(self, admin_app):
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={"api_key": "sk-test"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_analyze_returns_markdown(self, admin_app, _patch_llm):
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/usage-insights/analyze",
                json={"api_key": "sk-test", "model": "glm-5.2"},
            )
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert "Claude Code" in body["analysis"]
        assert body["model"] == "glm-5.2"
        assert body["sampled_requests"] == 2
        assert body["scope"] == "all users"
        assert "generated_at" in body
        # The rendered content handed to the LLM includes harness identifiers.
        assert "Kilo Code agent" in _patch_llm["content"]

    @pytest.mark.asyncio
    async def test_analyze_scopes_to_user_email(self, admin_app, _patch_llm):
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/usage-insights/analyze",
                json={"api_key": "sk-test", "user_email": "heavy@user.com"},
            )
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["scope"] == "heavy@user.com"

    @pytest.mark.asyncio
    async def test_analyze_404_when_no_samples(self, admin_app, mock_db_logger, _patch_llm):
        mock_conn = mock_db_logger.pool.acquire.return_value.__aenter__.return_value
        mock_conn.fetch = AsyncMock(return_value=[])

        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={"api_key": "sk-test"})
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
                json={"api_key": "sk-test", "user_email": "nobody@x.com"},
            )
        admin_app.dependency_overrides.clear()

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_analyze_requires_api_key(self, admin_app, _patch_llm):
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/usage-insights/analyze", json={"api_key": ""})
        admin_app.dependency_overrides.clear()
        # Empty api_key violates the min_length=1 constraint → 422.
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_analyze_rejects_foreign_base_url(self, admin_app, _patch_llm):
        """base_url is constrained to freeinference.org to block SSRF/exfiltration."""
        admin_app.dependency_overrides[verify_admin_access] = lambda: "admin@test"
        transport = ASGITransport(app=admin_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/admin/usage-insights/analyze",
                json={"api_key": "sk-test", "base_url": "http://169.254.169.254/v1"},
            )
        admin_app.dependency_overrides.clear()
        assert resp.status_code == 422


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
                "system_opener": big,
                "user_messages": [big],
            }
            for _ in range(50)
        ]
        rendered, used = usage_insights._render_samples(samples)
        assert used < 50
        assert len(rendered) <= usage_insights._MAX_PROMPT_CHARS + 5000


class TestCallAnalysisModel:
    @pytest.mark.asyncio
    async def test_marks_synthetic_probe_and_builds_url(self, monkeypatch):
        """The outbound call targets <base>/chat/completions and is a synthetic probe.

        The synthetic-probe header keeps the gateway from logging (and later
        re-sampling) this feature's own analysis requests.
        """
        from serving.schemas_admin import UsageInsightsRequest

        captured: dict = {}

        class _FakeClient:
            async def json_post(self, url, *, json, headers, timeout):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return {"choices": [{"message": {"content": "  report  "}}]}

        monkeypatch.setattr(usage_insights.AsyncHTTPClient, "shared", lambda: _FakeClient())

        payload = UsageInsightsRequest(
            api_key="sk-x", model="glm-5.2", base_url="https://freeinference.org/v1"
        )
        text = await usage_insights._call_analysis_model(payload, "the content")

        assert text == "report"
        assert captured["url"] == "https://freeinference.org/v1/chat/completions"
        assert captured["headers"]["X-Probe"] == "synthetic"
        assert captured["headers"]["Authorization"] == "Bearer sk-x"
        assert captured["json"]["model"] == "glm-5.2"
