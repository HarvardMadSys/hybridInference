"""The admin user-tags API: tags, their limits and rules, and their members.

Against a mocked store: what each endpoint validates, what it asks the store
to write, and what it audits. The SQL itself is covered against PostgreSQL in
``tests/integration/storage/test_user_tags_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routing.routers import FixedRouter
from serving.adapters.base import BaseAdapter, ModelConfig
from serving.servers.deps import AppServices
from serving.servers.routers import admin as admin_router

AUTH = {"Authorization": "Bearer test-admin"}
NOW = datetime(2026, 11, 27, tzinfo=UTC)


class _Adapter(BaseAdapter):
    async def chat_completion(self, messages, **params):  # pragma: no cover
        return self.format_response(content="ok", model=self.config.id)

    async def stream_chat_completion(self, messages, **params):  # pragma: no cover
        if False:
            yield ""


def _config(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="test",
        base_url="http://test",
        context_length=8192,
        max_output_length=4096,
        supported_params=["temperature"],
        input_modalities=["text"],
        output_modalities=["text"],
        quantization="bf16",
    )


def _tag(name: str = "unattended", **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "",
        "quota_daily_cost": None,
        "quota_monthly_cost": None,
        "accounting_currency": None,
        "max_concurrent_requests": None,
        "allowed_models": None,
        "auto_enabled": name == "unattended",
        "auto_window_hours": 12,
        "auto_min_active_hours": 12,
        "system": name == "unattended",
        "created_at": NOW,
        "updated_at": NOW,
        "manual_members": 0,
        "auto_members": 2,
        "dismissed_members": 0,
        **fields,
    }


@pytest.fixture
async def client(monkeypatch):
    op_store = MagicMock()
    op_store.list_user_tags = AsyncMock(return_value=[_tag(), _tag("lab-a")])
    op_store.get_user_tag = AsyncMock(side_effect=lambda name: _tag(name))
    op_store.create_user_tag = AsyncMock(side_effect=lambda name, fields: _tag(name, **fields))
    op_store.update_user_tag = AsyncMock(side_effect=lambda name, fields: _tag(name, **fields))
    op_store.delete_user_tag = AsyncMock(return_value=True)
    op_store.list_user_tag_members = AsyncMock(return_value=(0, []))
    op_store.add_user_tag_members = AsyncMock(return_value=([], []))
    op_store.remove_user_tag_member = AsyncMock(return_value="auto")
    op_store.preview_user_tag_members = AsyncMock(return_value=[])
    op_store.detect_user_tag_members = AsyncMock(return_value=[])
    op_store.log_admin_action = AsyncMock()

    router = FixedRouter()
    router.register_route("model-a", [(_Adapter(_config("model-a")), 1.0)])
    router.register_route("model-b", [(_Adapter(_config("model-b")), 1.0)])

    app = FastAPI()
    app.state.services = AppServices(  # type: ignore[attr-defined]
        router=router,
        db_logger=MagicMock(),
        operational_store=op_store,
        log_store=MagicMock(),
        routing_manager=None,
    )
    app.include_router(admin_router.router)
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, op_store


def _audited(op_store, action: str) -> dict[str, Any]:
    calls = [c for c in op_store.log_admin_action.await_args_list if c.kwargs["action"] == action]
    assert len(calls) == 1, op_store.log_admin_action.await_args_list
    return calls[0].kwargs


async def test_lists_every_tag_in_the_ledger_currency(client):
    http, _store = client
    response = await http.get("/admin/user-tags", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["currency"] == "USD"
    assert [tag["name"] for tag in body["tags"]] == ["unattended", "lab-a"]
    assert body["tags"][0]["system"] is True
    assert body["tags"][0]["auto_members"] == 2


async def test_requires_an_administrator(client):
    http, _store = client
    assert (await http.get("/admin/user-tags")).status_code in (401, 403)


async def test_creates_a_tag_with_limits_and_canonical_models(client):
    http, store = client
    response = await http.post(
        "/admin/user-tags",
        headers=AUTH,
        json={
            "name": "lab-a",
            "description": " Lab A ",
            "quota_daily_cost": "5",
            "currency": "USD",
            "max_concurrent_requests": 2,
            "allowed_models": ["model-b", "model-a", "model-a"],
        },
    )
    assert response.status_code == 201, response.text
    name, fields = store.create_user_tag.await_args.args
    assert name == "lab-a"
    assert fields == {
        "description": "Lab A",
        "quota_daily_cost": Decimal("5"),
        "accounting_currency": "USD",
        "max_concurrent_requests": 2,
        "allowed_models": ["model-a", "model-b"],
    }
    assert _audited(store, "user_tag.create")["target"].id == "lab-a"


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"name": "Bad Name"}, 422),
        ({"name": "x", "allowed_models": ["nope"]}, 422),
        ({"name": "x", "allowed_models": []}, 422),
        ({"name": "x", "quota_daily_cost": 5}, 422),
        ({"name": "x", "quota_daily_cost": 5, "currency": "CNY"}, 409),
        ({"name": "x", "auto_window_hours": 6, "auto_min_active_hours": 7}, 422),
        ({"name": "x", "max_concurrent_requests": 0}, 422),
    ],
)
async def test_refuses_an_invalid_tag(client, payload, status):
    http, store = client
    response = await http.post("/admin/user-tags", headers=AUTH, json=payload)
    assert response.status_code == status, response.text
    store.create_user_tag.assert_not_awaited()


async def test_a_taken_name_is_a_conflict(client):
    http, store = client
    store.create_user_tag.side_effect = None
    store.create_user_tag.return_value = None
    response = await http.post("/admin/user-tags", headers=AUTH, json={"name": "lab-a"})
    assert response.status_code == 409


async def test_updates_only_what_is_sent_and_null_clears_a_limit(client):
    http, store = client
    store.get_user_tag.side_effect = lambda name: _tag(name, max_concurrent_requests=4)
    response = await http.patch(
        "/admin/user-tags/unattended",
        headers=AUTH,
        json={"max_concurrent_requests": None, "auto_window_hours": 24},
    )
    assert response.status_code == 200, response.text
    assert store.update_user_tag.await_args.args == (
        "unattended",
        {"max_concurrent_requests": None, "auto_window_hours": 24},
    )
    assert _audited(store, "user_tag.update")["details"] == {
        "max_concurrent_requests": None,
        "auto_window_hours": 24,
    }


async def test_an_update_checks_the_rule_against_the_saved_bounds(client):
    http, store = client
    store.get_user_tag.side_effect = lambda name: _tag(name, auto_window_hours=12)
    response = await http.patch(
        "/admin/user-tags/unattended", headers=AUTH, json={"auto_min_active_hours": 13}
    )
    assert response.status_code == 422
    response = await http.patch(
        "/admin/user-tags/unattended", headers=AUTH, json={"description": None}
    )
    assert response.status_code == 422
    store.update_user_tag.assert_not_awaited()


async def test_an_unknown_tag_is_not_found(client):
    http, store = client
    store.get_user_tag.side_effect = None
    store.get_user_tag.return_value = None
    assert (await http.patch("/admin/user-tags/nope", headers=AUTH, json={})).status_code == 404
    assert (await http.delete("/admin/user-tags/nope", headers=AUTH)).status_code == 404
    assert (await http.get("/admin/user-tags/nope/members", headers=AUTH)).status_code == 404


async def test_the_built_in_tag_cannot_be_deleted(client):
    http, store = client
    response = await http.delete("/admin/user-tags/unattended", headers=AUTH)
    assert response.status_code == 409
    store.delete_user_tag.assert_not_awaited()
    response = await http.delete("/admin/user-tags/lab-a", headers=AUTH)
    assert response.status_code == 204
    store.delete_user_tag.assert_awaited_once_with("lab-a")
    assert _audited(store, "user_tag.delete")["target"].id == "lab-a"


async def test_adds_members_by_hand_and_reports_the_rest(client):
    http, store = client
    store.add_user_tag_members.return_value = (["u1"], ["ghost"])
    response = await http.post(
        "/admin/user-tags/lab-a/members",
        headers=AUTH,
        json={"user_ids": ["u1", "u2", "ghost", " "]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"added": ["u1"], "unchanged": ["u2"], "unknown": ["ghost"]}
    assert store.add_user_tag_members.await_args.args == ("lab-a", ["u1", "u2", "ghost"])
    assert _audited(store, "user_tag.add_members")["details"] == {"user_ids": ["u1"]}


async def test_taking_an_automatic_tag_away_dismisses_it(client):
    http, store = client
    response = await http.delete("/admin/user-tags/unattended/members/u1", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"user_id": "u1", "previous_source": "auto", "dismissed": True}
    audit = _audited(store, "user_tag.remove_member")
    assert audit["target"].user_id == "u1"
    assert audit["details"]["tag"] == "unattended"


async def test_taking_away_a_tag_the_account_lacks_is_not_found(client):
    http, store = client
    store.remove_user_tag_member.return_value = None
    response = await http.delete("/admin/user-tags/lab-a/members/u1", headers=AUTH)
    assert response.status_code == 404


async def test_lists_members_with_how_they_carry_the_tag(client):
    http, store = client
    store.list_user_tag_members.return_value = (
        1,
        [
            {
                "user_id": "u1",
                "source": "auto",
                "assigned_at": NOW,
                "assigned_by": None,
                "email": "bot@example.test",
                "login_name": None,
                "phone": None,
                "user_name": None,
                "status": "active",
            }
        ],
    )
    response = await http.get(
        "/admin/user-tags/unattended/members?source=auto&source=manual&limit=10", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["members"][0]["source"] == "auto"
    assert store.list_user_tag_members.await_args.kwargs == {
        "sources": ["auto", "manual"],
        "limit": 10,
        "offset": 0,
    }


async def test_previews_a_rule_with_bounds_not_yet_saved(client):
    http, store = client
    store.preview_user_tag_members.return_value = [
        {
            "user_id": f"u{i}",
            "active_hours": 6,
            "member_source": None,
            "email": None,
            "login_name": None,
            "phone": None,
            "user_name": None,
            "status": "active",
        }
        for i in range(3)
    ]
    response = await http.get(
        "/admin/user-tags/unattended/preview?window_hours=6&min_active_hours=6&limit=2",
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["window_hours"] == 6 and body["min_active_hours"] == 6
    assert len(body["matched"]) == 2 and body["truncated"] is True
    assert store.preview_user_tag_members.await_args.kwargs == {
        "window_hours": 6,
        "min_active_hours": 6,
        "limit": 3,
    }
    response = await http.get(
        "/admin/user-tags/unattended/preview?window_hours=6&min_active_hours=7", headers=AUTH
    )
    assert response.status_code == 422


async def test_runs_a_rule_now_only_when_it_is_on(client):
    http, store = client
    store.detect_user_tag_members.return_value = ["u1"]
    response = await http.post("/admin/user-tags/unattended/detect", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"added": ["u1"]}
    assert store.detect_user_tag_members.await_args.kwargs == {
        "window_hours": 12,
        "min_active_hours": 12,
    }
    assert _audited(store, "user_tag.detect")["details"] == {"added": ["u1"]}
    response = await http.post("/admin/user-tags/lab-a/detect", headers=AUTH)
    assert response.status_code == 409
