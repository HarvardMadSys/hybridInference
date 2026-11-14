"""No public route creates an account while first-run setup is pending.

Setup ends once any account exists (``serving.setup_state`` records its marker
on sight of a user), so the first account must be the administrator the
operator creates with the setup code. An account a stranger registers first
completes setup instead: ``/auth/setup/admin`` then answers 409 and the
deployment has no administrator. Every public registration route therefore
answers 503 until setup is done.

Two halves keep that true as routes are added. The scan finds, in the
gateway's source, every statement that inserts a user row and every call to
the store methods holding one; a new one fails the scan until it is listed in
``ACCOUNT_INSERTS`` or ``ACCOUNT_CREATORS``, the latter with how it is gated.
Each public route listed there is then driven through the real application
while setup is pending.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from serving.servers.app import create_app
from serving.servers.deps import AppServices
from serving.servers.routers import phone_auth_routes
from serving.setup_state import init_setup_state

SERVING = Path(__file__).resolve().parents[4] / "apps" / "backend" / "serving"
STORAGE = SERVING / "storage"

NOT_SET_UP = "This deployment has not been set up yet."
PHONE = "+8613800138000"

# The store methods that insert a user row: nothing else in the gateway may.
ACCOUNT_INSERTS = {
    "serving.storage.postgres_operational.PostgresOperationalStore.create_user",
    "serving.storage.postgres_operational.PostgresOperationalStore.create_first_admin",
    "serving.storage.phone_auth.PostgresPhoneAuthMixin.create_phone_user",
}

# Every caller of those methods outside the store, with the public route it
# serves, or None for a path that is not public registration.
ACCOUNT_CREATORS: dict[str, str | None] = {
    "serving.servers.routers.auth_routes.signup": "/auth/signup",
    "serving.servers.routers.phone_auth_routes.phone_signup": "/auth/phone/signup",
    # First-run setup itself: POST /auth/setup/admin, which takes the setup
    # code, and whose store transaction creates the administrator only while
    # no account exists (tests/servers/test_setup_routes.py).
    "serving.setup_state.complete_setup": None,
}

# A request each public registration route would otherwise accept. The phone
# code request creates nothing, but it texts the code that the phone signup
# then turns into an account.
REGISTRATION_REQUESTS: dict[str, dict[str, Any]] = {
    "/auth/signup": {
        "email": "stranger@example.com",
        "password": "Str0ngPassword",
        "user_name": "Stranger",
        "accepted_tos": True,
    },
    "/auth/phone/code": {"phone": PHONE, "purpose": "signup"},
    "/auth/phone/signup": {
        "phone": PHONE,
        "code": "123456",
        "user_name": "Stranger",
        "accepted_tos": True,
    },
}

# ``INSERT INTO users`` in any case or spacing, schema-qualified or quoted, but
# not a longer table name such as ``users_archive``.
_INSERT_INTO_USERS = re.compile(r'\binsert\s+into\s+(?:\w+\.)?"?users"?(?!\w)', re.IGNORECASE)


class _Scan(ast.NodeVisitor):
    """Records the qualified name of each function that inserts a user row or calls *methods*."""

    def __init__(self, module: str, methods: frozenset[str]) -> None:
        self.module = module
        self.methods = methods
        self.scope: list[str] = []
        self.inserts: set[str] = set()
        self.calls: set[str] = set()

    def _where(self) -> str:
        return ".".join([self.module, *self.scope])

    def _enter(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Expr(self, node: ast.Expr) -> None:
        # A string statement is a docstring, never SQL that runs.
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _INSERT_INTO_USERS.search(node.value):
            self.inserts.add(self._where())

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in self.methods:
            self.calls.add(self._where())
        self.generic_visit(node)


def _scan(methods: frozenset[str] = frozenset()) -> tuple[set[str], set[str]]:
    """Return (user-row inserts anywhere, calls to *methods* outside the store)."""
    inserts: set[str] = set()
    calls: set[str] = set()
    for path in sorted(SERVING.rglob("*.py")):
        module = ".".join(path.relative_to(SERVING.parent).with_suffix("").parts)
        scan = _Scan(module.removesuffix(".__init__"), methods)
        scan.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        inserts |= scan.inserts
        if not path.is_relative_to(STORAGE):
            calls |= scan.calls
    return inserts, calls


def test_only_the_listed_store_methods_insert_users() -> None:
    inserts, _ = _scan()
    assert inserts == ACCOUNT_INSERTS, (
        "a new statement inserts into users. While first-run setup is pending no "
        "public route may create an account: gate its callers on "
        "refresh_setup_state(), then list it in ACCOUNT_INSERTS"
    )


def test_every_account_creator_is_listed_with_its_gate() -> None:
    methods = frozenset(name.rsplit(".", 1)[1] for name in ACCOUNT_INSERTS)
    _, calls = _scan(methods)
    assert calls == set(ACCOUNT_CREATORS), (
        "an unlisted function creates accounts. A public route must answer 503 "
        "while refresh_setup_state() reports setup pending, as POST /auth/signup "
        "does; list it in ACCOUNT_CREATORS and REGISTRATION_REQUESTS. An "
        "operator-only path needs admin authentication instead."
    )


def test_each_public_creator_is_driven_below() -> None:
    app = create_app()
    for creator, path in ACCOUNT_CREATORS.items():
        if path is not None:
            # Routes are named after their endpoint function.
            assert app.url_path_for(creator.rsplit(".", 1)[1]) == path
            assert path in REGISTRATION_REQUESTS


class _PendingSetupStore:
    """A database with no accounts yet: any use beyond reading the setup code is recorded."""

    def __init__(self) -> None:
        self.used: list[str] = []

    async def get_or_create_setup_code(self, **_: Any) -> str:
        return "ABCDEFGHJKLM"

    def __getattr__(self, name: str) -> Any:
        self.used.append(name)

        async def _unexpected(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError(f"store.{name} was used while setup is pending")

        return _unexpected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"), list(REGISTRATION_REQUESTS.items()), ids=list(REGISTRATION_REQUESTS)
)
async def test_registration_waits_for_first_run_setup(
    path: str, body: dict[str, Any], monkeypatch
) -> None:
    monkeypatch.setattr(phone_auth_routes, "is_sms_enabled", lambda: True)
    store = _PendingSetupStore()
    await init_setup_state(store)
    app = create_app()
    app.state.services = AppServices(router=MagicMock(), operational_store=store)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["message"] == NOT_SET_UP
    assert store.used == []
