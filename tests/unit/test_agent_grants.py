"""Tests for inference grants: the capability boundary between the two repos.

The assertions that matter most are the ones about what a *caller cannot do*:
widen its own model scope, get a working grant for an MCP server the deployment
never configured, mint capability for a suspended account, or acquire two
capabilities for one attempt by retrying.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serving import grants
from serving.config.settings import get_settings
from serving.servers.deps import (
    get_log_store,
    get_model_visibility_resolver,
    get_operational_store,
    get_router,
)
from serving.servers.middleware.error import install_error_handlers
from serving.servers.routers import agent_grants
from serving.servers.routers.internal_auth import ENV_DISPATCH_TOKEN

DISPATCH = "dispatch-secret-value"
AUTH = {"Authorization": f"Bearer {DISPATCH}"}

# What this fake deployment's registry knows about, so "unknown" is meaningful.


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_ttl_is_clamped_to_the_ceiling_not_refused() -> None:
    """A too-long request is satisfiable, just not on the caller's terms."""
    assert grants.clamp_ttl(grants.MAX_GRANT_TTL_S + 10_000) == grants.MAX_GRANT_TTL_S
    assert grants.clamp_ttl(30) == 30
    assert grants.clamp_ttl(None) == grants.DEFAULT_GRANT_TTL_S
    assert grants.clamp_ttl(0) == grants.DEFAULT_GRANT_TTL_S
    assert grants.clamp_ttl(-5) == grants.DEFAULT_GRANT_TTL_S


def test_models_are_clamped_and_never_widened() -> None:
    visible = ["a", "b", "c"]
    assert grants.clamp_models(["b", "z"], visible=visible) == ["b"]
    assert grants.clamp_models(None, visible=visible) == visible
    assert grants.clamp_models([], visible=visible) == []


def test_clamped_models_keep_registry_order() -> None:
    """Stable output, so a grant does not change shape between mints."""
    assert grants.clamp_models(["c", "a"], visible=["a", "b", "c"]) == ["a", "c"]


def test_a_grant_token_round_trips_and_rejects_tampering() -> None:
    token = grants.mint_grant_token("agr_01")
    assert grants.looks_like_grant_token(token)
    assert grants.parse_grant_token(token) == "agr_01"

    prefix, payload, signature = token.split(".")
    other = grants.mint_grant_token("agr_02")
    with pytest.raises(grants.InvalidGrantToken):
        # Someone else's payload under this signature.
        grants.parse_grant_token(f"{prefix}.{other.split('.')[1]}.{signature}")
    for bad in ("", "nope", f"{prefix}.only-two", f"ajt.{payload}.{signature}"):
        with pytest.raises(grants.InvalidGrantToken):
            grants.parse_grant_token(bad)


def test_liveness_covers_revoked_and_expired() -> None:
    now = datetime.now(UTC)
    live = {"revoked_at": None, "expires_at": now + timedelta(seconds=60)}
    assert grants.is_live(live, now=now) is True
    assert grants.is_live({**live, "revoked_at": now}, now=now) is False
    assert grants.is_live({**live, "expires_at": now - timedelta(seconds=1)}, now=now) is False
    assert grants.is_live({"revoked_at": None, "expires_at": None}, now=now) is False


def test_no_module_surface_mentions_a_budget() -> None:
    """The adjudicated design: grants scope what, quota governs how much."""
    assert not [name for name in dir(grants) if "budget" in name.lower()]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStore:
    """Grant storage with the real idempotency and liveness semantics."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self._by_attempt: dict[tuple[str, str], str] = {}

    async def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self.users.get(user_id)

    async def upsert_agent_grant(
        self,
        *,
        grant_id: str,
        user_id: str,
        external_job_id: str,
        external_attempt_id: str,
        allowed_models: list[str],
        expires_at: datetime,
    ) -> dict[str, Any]:
        key = (external_job_id, external_attempt_id)
        if key in self._by_attempt:
            return self.grants[self._by_attempt[key]]
        row = {
            "grant_id": grant_id,
            "user_id": user_id,
            "external_job_id": external_job_id,
            "external_attempt_id": external_attempt_id,
            "allowed_models": list(allowed_models),
            "created_at": datetime.now(UTC),
            "expires_at": expires_at,
            "revoked_at": None,
        }
        self.grants[grant_id] = row
        self._by_attempt[key] = grant_id
        return row

    async def get_agent_grant(self, grant_id: str) -> dict[str, Any] | None:
        return self.grants.get(grant_id)

    async def renew_agent_grant(
        self, grant_id: str, *, expires_at: datetime
    ) -> dict[str, Any] | None:
        row = self.grants.get(grant_id)
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= datetime.now(UTC):
            return None
        row["expires_at"] = expires_at
        return row

    async def revoke_agent_grant(self, grant_id: str) -> bool:
        row = self.grants.get(grant_id)
        if row is None or row["revoked_at"] is not None:
            return False
        row["revoked_at"] = datetime.now(UTC)
        return True


@pytest.fixture
def store() -> FakeStore:
    fake = FakeStore()
    fake.users["user_1"] = {"id": "user_1", "role": "pro", "status": "active"}
    return fake


@pytest.fixture(autouse=True)
def _dispatch_token(monkeypatch) -> None:
    monkeypatch.setenv(ENV_DISPATCH_TOKEN, DISPATCH)


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch) -> None:
    """Grant tokens derive from API_KEY_SECRET, which tests do not otherwise set.

    ``get_settings`` is ``lru_cache``d, so the cache is cleared on both sides:
    once so this value is seen, and once after so a cached test settings object
    does not leak into whatever runs next.
    """
    monkeypatch.setenv("API_KEY_SECRET", "test-api-key-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(store: FakeStore) -> TestClient:
    app = FastAPI()
    app.include_router(agent_grants.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: store
    # The clamp source: this "registry" resolves three chat models for pro.
    app.dependency_overrides[get_router] = lambda: object()
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    return app, TestClient(app)


@pytest.fixture(autouse=True)
def _visible_models(monkeypatch) -> None:
    async def _visible(_router, **_kwargs):
        return ["glm-5.1", "qwen3.6-35b", "kimi-k2"]

    monkeypatch.setattr(agent_grants, "agent_visible_models", _visible)


def _mint(client: TestClient, **overrides: Any):
    body = {
        "user_id": "user_1",
        "external_job_id": "job_1",
        "external_attempt_id": "1",
    }
    body.update(overrides)
    return client.post("/internal/agent-grants", json=body, headers=AUTH)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_a_deployment_without_the_token_offers_no_internal_api(client, monkeypatch) -> None:
    """Unconfigured reads as "not offered", not as "guess again"."""
    _, http = client
    monkeypatch.delenv(ENV_DISPATCH_TOKEN, raising=False)
    response = http.post("/internal/agent-grants", json={}, headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "internal_api_not_configured"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "x"}])
def test_a_bad_dispatch_token_is_refused(client, headers: dict[str, str]) -> None:
    _, http = client
    response = http.post("/internal/agent-grants", json={}, headers=headers)
    assert response.status_code == 401


def test_no_route_here_answers_without_the_token(client) -> None:
    """Every route, not just the one this file happened to test first.

    Mint was the only path checked, so `/renew`, `/usage` and `/revoke` could
    each have lost their guard silently. That mattered more once the whole
    prefix became reachable from the public origin: these are enumerated from
    the router, so a route added later is covered without anyone remembering.
    """
    _, http = client
    for route in agent_grants.router.routes:
        path = route.path.replace("{grant_id}", "agr_x")
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            response = http.request(method, path, json={})
            assert response.status_code == 401, f"{method} {path} answered without a token"


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def test_mint_returns_the_effective_scope_and_a_token(client) -> None:
    _, http = client
    response = _mint(http)
    assert response.status_code == 200
    body = response.json()
    assert body["allowed_models"] == ["glm-5.1", "qwen3.6-35b", "kimi-k2"]
    assert grants.parse_grant_token(body["token"]) == body["grant_id"]


def test_a_request_for_more_models_than_the_role_allows_is_clamped_not_refused(client) -> None:
    """The caller learns what it got; it does not get an error it cannot fix."""
    _, http = client
    body = _mint(http, allowed_models=["glm-5.1", "gpt-5-secret"]).json()
    assert body["allowed_models"] == ["glm-5.1"]


def test_a_longer_ttl_than_the_ceiling_is_clamped_not_refused(client) -> None:
    _, http = client
    body = _mint(http, ttl_seconds=999_999).json()
    granted = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
    assert granted <= timedelta(seconds=grants.MAX_GRANT_TTL_S + 5)


def test_a_mint_request_cannot_ask_for_mcp_at_all(client) -> None:
    """**The boundary, as the wire sees it.**

    Two cases lived here — an unknown server refused the mint, a known subset
    was granted verbatim — and both were about a decision this gateway no
    longer makes. MCP moved to the cloud agent with its registry, credentials
    and proxy; the job and its requested servers are there, so validating a
    server list here meant deciding with a copy of somebody else's state.

    Those properties did not vanish: the cloud agent validates a job's
    requested servers against its own registry, and its relay refuses a server
    the job did not ask for. This asserts the *gateway* has stopped answering
    the question — a request carrying `allowed_mcp` is accepted (pydantic
    ignores the unknown field) and the grant that comes back says nothing about
    MCP, so nothing downstream can mistake silence for permission.
    """
    _, http = client
    body = _mint(http, allowed_mcp=["deepwiki", "not-configured"]).json()

    assert "allowed_mcp" not in body
    assert not [key for key in body if "mcp" in key.lower()]


@pytest.mark.parametrize("status_value", ["suspended", "deleted", "pending_approval"])
def test_minting_for_an_inactive_account_is_refused(client, store, status_value: str) -> None:
    _, http = client
    store.users["user_1"]["status"] = status_value
    response = _mint(http)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "subject_unavailable"


def test_minting_for_an_unknown_user_is_refused_the_same_way(client, store) -> None:
    """Same answer as suspended: whether an id exists is not the caller's business."""
    _, http = client
    store.users.clear()
    assert _mint(http).json()["error"]["type"] == "subject_unavailable"


def test_re_minting_the_same_attempt_returns_the_first_grant(client) -> None:
    """A retry after a timeout must not create a second capability."""
    _, http = client
    first = _mint(http).json()
    second = _mint(http, ttl_seconds=30).json()
    assert second["grant_id"] == first["grant_id"]
    # The retry cannot re-time the existing grant either. A lifetime is not a
    # privilege, so a differing TTL is still the same request — it is refused
    # by being ignored rather than by a conflict.
    assert second["expires_at"] == first["expires_at"]


def test_a_retry_that_asks_for_a_different_scope_is_refused(client) -> None:
    """**Idempotent means "the same request", not "the same key".**

    This case used to return 200 with the *first* grant's scope. A caller
    asking for a narrower capability was handed a wider one and told it
    succeeded — the privilege decision going to whichever call happened to
    arrive first. The key is the control plane's own id pair, so a mismatch
    means its state and ours disagree, and answering at all resolves that
    disagreement by guessing.
    """
    _, http = client
    first = _mint(http).json()
    conflict = _mint(http, allowed_models=["kimi-k2"])

    assert conflict.status_code == 409
    assert conflict.json()["error"]["type"] == "grant_conflict"
    # And the existing grant is untouched by the attempt.
    assert _mint(http).json()["allowed_models"] == first["allowed_models"]


def test_a_model_the_owner_disabled_is_not_granted(client, store, monkeypatch) -> None:
    """The mint resolves visibility with the owner's own denylist, or without it.

    `agent_visible_models` already refuses a disabled model — but only if it is
    told which ones. The mint passed `role` and `user_id` and nothing else, so
    the resolver's denylist check read an absent key, passed, and a grant was
    issued for a model the owner had explicitly turned off in their dashboard.

    The fake here filters the way the real resolver does, so this asserts the
    outcome rather than the call signature: drop the wiring and a disabled
    model comes back inside `allowed_models`.
    """
    seen: dict[str, Any] = {}

    async def _visible(_router, **kwargs):
        seen.update(kwargs)
        denied = set((kwargs.get("user_ctx") or {}).get("disabled_models") or [])
        return [m for m in ["glm-5.1", "qwen3.6-35b", "kimi-k2"] if m not in denied]

    monkeypatch.setattr(agent_grants, "agent_visible_models", _visible)
    store.users["user_1"]["preferences"] = {"disabled_models": ["glm-5.1"]}

    _, http = client
    granted = _mint(http).json()["allowed_models"]

    assert seen["user_ctx"]["disabled_models"] == ["glm-5.1"]
    assert "glm-5.1" not in granted
    assert "kimi-k2" in granted, "the denylist narrowed more than the one model"


def test_a_different_attempt_gets_its_own_grant(client) -> None:
    _, http = client
    first = _mint(http).json()
    second = _mint(http, external_attempt_id="2").json()
    assert second["grant_id"] != first["grant_id"]


def test_no_mint_response_carries_a_budget(client) -> None:
    _, http = client
    assert not [k for k in _mint(http).json() if "budget" in k.lower()]


# ---------------------------------------------------------------------------
# Renew and revoke
# ---------------------------------------------------------------------------


def test_renew_extends_a_live_grant_without_reissuing_a_token(client) -> None:
    _, http = client
    minted = _mint(http, ttl_seconds=30).json()
    renewed = http.post(f"/internal/agent-grants/{minted['grant_id']}/renew", headers=AUTH).json()
    assert datetime.fromisoformat(renewed["expires_at"]) > datetime.fromisoformat(
        minted["expires_at"]
    )
    assert "token" not in renewed


def test_renew_refuses_a_revoked_grant(client) -> None:
    _, http = client
    grant_id = _mint(http).json()["grant_id"]
    http.post(f"/internal/agent-grants/{grant_id}/revoke", headers=AUTH)
    response = http.post(f"/internal/agent-grants/{grant_id}/renew", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "grant_not_live"


def test_renew_refuses_an_expired_grant(client, store) -> None:
    """A lapsed grant is not resurrected — that is what the TTL is for."""
    _, http = client
    grant_id = _mint(http).json()["grant_id"]
    store.grants[grant_id]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    assert http.post(f"/internal/agent-grants/{grant_id}/renew", headers=AUTH).status_code == 404


def test_renew_refuses_once_the_user_is_no_longer_active(client, store) -> None:
    """Renewal is the recurring moment a mid-job suspension can take effect."""
    _, http = client
    grant_id = _mint(http).json()["grant_id"]
    store.users["user_1"]["status"] = "suspended"
    response = http.post(f"/internal/agent-grants/{grant_id}/renew", headers=AUTH)
    assert response.status_code == 403


def test_renew_consults_no_attempt_or_lease_state(client, store) -> None:
    """After the split this gateway holds none, so it must not look for any.

    The fake store exposes no attempt or lease method, so a renewal that tried
    to check a fence would raise AttributeError instead of quietly returning a
    wrong answer. Renewal succeeding here *is* the assertion.
    """
    _, http = client
    grant_id = _mint(http).json()["grant_id"]
    attempt_methods = [
        name
        for name in dir(store)
        if not name.startswith("_") and ("attempt" in name or "lease" in name)
    ]
    assert attempt_methods == [], f"the fake would answer a fence lookup via {attempt_methods}"
    assert http.post(f"/internal/agent-grants/{grant_id}/renew", headers=AUTH).status_code == 200


def test_renewing_repeatedly_cannot_push_expiry_past_one_step(client) -> None:
    """Renewal buys another bounded step *from now*, never a cumulative one.

    Extending from the previous expiry would let a caller walk a grant
    arbitrarily far into the future by renewing in a tight loop — which is the
    long-lived credential the short TTL exists to prevent.
    """
    _, http = client
    grant_id = _mint(http, ttl_seconds=grants.MAX_GRANT_TTL_S).json()["grant_id"]
    ceiling = datetime.now(UTC) + timedelta(seconds=grants.MAX_GRANT_TTL_S + 5)

    for _ in range(5):
        renewed = http.post(f"/internal/agent-grants/{grant_id}/renew", headers=AUTH).json()
        assert datetime.fromisoformat(renewed["expires_at"]) <= ceiling


def test_revoke_is_idempotent(client) -> None:
    _, http = client
    grant_id = _mint(http).json()["grant_id"]
    first = http.post(f"/internal/agent-grants/{grant_id}/revoke", headers=AUTH).json()
    second = http.post(f"/internal/agent-grants/{grant_id}/revoke", headers=AUTH).json()
    assert first == {"grant_id": grant_id, "revoked": True, "already_revoked": False}
    assert second["already_revoked"] is True


def test_revoking_an_unknown_grant_is_a_404(client) -> None:
    _, http = client
    assert http.post("/internal/agent-grants/agr_nope/revoke", headers=AUTH).status_code == 404


# ── The column that stays in the database ──────────────────────────────
#
# `allowed_mcp` is gone from the DDL and from every statement, and the physical
# column is deliberately **not** dropped. A rolling deploy runs both versions at
# once: the old one still selects and inserts that column, so dropping it would
# break the instances that have not restarted yet, and would make a rollback
# impossible. `DROP COLUMN` is a separate, human-approved migration for after
# the rollback window closes; see MIGRATION notes.
#
# These two say why the orphan is harmless, in the two ways it could stop being.


def test_a_fresh_database_gets_no_mcp_column() -> None:
    """The canonical DDL is what a new deployment gets."""
    assert "allowed_mcp" not in grants.CREATE_TABLE_SQL


def test_every_grant_statement_names_its_columns() -> None:
    """**Why an existing `allowed_mcp` column costs nothing.**

    An explicit column list on the way in means the orphan takes its
    `DEFAULT '[]'`, and an explicit list on the way out means it is never read.
    A `SELECT *` would return it and put it back into a grant response — the
    field would reappear on the wire from a table nobody edited, which is
    exactly the sort of resurrection nobody goes looking for.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "apps/backend/serving/storage/postgres_operational.py"
    ).read_text()
    grant_sql = [
        line
        for line in source.splitlines()
        if "agent_grants" in line or "grant_id, user_id, external_job_id" in line
    ]
    assert grant_sql, "no grant SQL found; this check would pass vacuously"
    assert not [line for line in grant_sql if "SELECT *" in line or "select *" in line]


# ---------------------------------------------------------------------------
# Usage — informational, never a limit
# ---------------------------------------------------------------------------


class FakeLedger:
    """The two ledger reads the usage endpoint makes."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, float]] = {}

    async def get_agent_job_cost(self, job_id: str) -> float:
        return self.rows.get(job_id, {}).get("cost", 0.0)

    async def get_agent_job_usage(self, job_id: str) -> dict[str, float]:
        row = self.rows.get(job_id, {})
        return {
            "tokens_in": row.get("tokens_in", 0),
            "tokens_out": row.get("tokens_out", 0),
            "calls": row.get("calls", 0),
        }


@pytest.fixture
def ledger() -> FakeLedger:
    fake = FakeLedger()
    fake.rows["job_1"] = {"cost": 1.25, "tokens_in": 900, "tokens_out": 300, "calls": 7}
    return fake


@pytest.fixture
def client_with_ledger(store: FakeStore, ledger: FakeLedger) -> TestClient:
    app = FastAPI()
    app.include_router(agent_grants.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_log_store] = lambda: ledger
    app.dependency_overrides[get_router] = lambda: object()
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    return TestClient(app)


def test_usage_reports_the_grant_job_s_ledger_totals(client_with_ledger) -> None:
    grant_id = _mint(client_with_ledger).json()["grant_id"]
    body = client_with_ledger.get(f"/internal/agent-grants/{grant_id}/usage", headers=AUTH).json()
    assert body["spent_usd"] == 1.25
    assert body["request_count"] == 7
    assert body["tokens_in"] == 900
    assert body["external_job_id"] == "job_1"


def test_usage_is_not_a_limit(client_with_ledger, ledger) -> None:
    """Spend far past any plausible cap still reports, and still mints.

    Nothing consults this endpoint to decide whether a call may proceed. If a
    future change made usage gate anything, this is the test that notices.
    """
    ledger.rows["job_1"]["cost"] = 10_000.0
    assert (
        client_with_ledger.get(
            f"/internal/agent-grants/{_mint(client_with_ledger).json()['grant_id']}/usage",
            headers=AUTH,
        ).json()["spent_usd"]
        == 10_000.0
    )
    assert _mint(client_with_ledger, external_attempt_id="9").status_code == 200


def test_usage_carries_no_budget_or_remaining_field(client_with_ledger) -> None:
    """A `remaining` field would imply a ceiling this endpoint does not have."""
    grant_id = _mint(client_with_ledger).json()["grant_id"]
    body = client_with_ledger.get(f"/internal/agent-grants/{grant_id}/usage", headers=AUTH).json()
    assert not [k for k in body if "budget" in k.lower() or "remaining" in k.lower()]


def test_usage_for_an_unknown_grant_is_a_404(client_with_ledger) -> None:
    assert (
        client_with_ledger.get("/internal/agent-grants/agr_nope/usage", headers=AUTH).status_code
        == 404
    )


def test_usage_without_a_ledger_says_so_rather_than_reporting_zero(store: FakeStore) -> None:
    """Zero reads as "spent nothing", which is not what "cannot tell" means."""
    app = FastAPI()
    app.include_router(agent_grants.router)
    install_error_handlers(app)
    app.dependency_overrides[get_operational_store] = lambda: store
    app.dependency_overrides[get_log_store] = lambda: None
    app.dependency_overrides[get_router] = lambda: object()
    app.dependency_overrides[get_model_visibility_resolver] = lambda: None
    http = TestClient(app)

    grant_id = _mint(http).json()["grant_id"]
    response = http.get(f"/internal/agent-grants/{grant_id}/usage", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "ledger_unavailable"
