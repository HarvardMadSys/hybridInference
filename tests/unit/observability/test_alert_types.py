"""The alert-type catalog, and the guard that keeps it complete.

A mute matches the type prefix of a dedupe key, so an alert whose type is not
catalogued can never be muted from the dashboard. Nothing fails at runtime when
that happens -- the alert simply keeps sending -- which is why the scan below
exists: every call site that sends an alert must use a key whose type is
registered, and every registered type must still have a call site.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from serving.observability.alert_types import ALERT_TYPES, alert_type_of, get_alert_type

_BACKEND = Path(__file__).resolve().parents[3] / "apps" / "backend"

#: Sender -> the keyword naming its dedupe key.
_SENDERS = {"alert_on_transition": "key", "alert_slack": "dedupe_key"}

#: The sink itself only forwards keys its callers chose.
_EXCLUDED = {_BACKEND / "serving" / "observability" / "alerts.py"}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _key_prefix(node: ast.expr, module: ast.Module, cls: ast.ClassDef | None) -> str | None:
    """The literal text a key expression starts with, when it can be read statically.

    Covers the four shapes the producers use: a string, an f-string with a
    literal head, ``self.name`` read off the rule class, and a module-level
    helper returning one of those. Anything else is unreadable, and the test
    says where rather than guessing.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str) and ":" in head.value:
            return head.value
        return None
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and cls is not None
    ):
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == node.attr for target in stmt.targets
            ):
                return _key_prefix(stmt.value, module, None)
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        for stmt in module.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == node.func.id:
                returns = [
                    n for n in ast.walk(stmt) if isinstance(n, ast.Return) and n.value is not None
                ]
                if len(returns) == 1 and returns[0].value is not None:
                    return _key_prefix(returns[0].value, module, None)
        return None
    return None


class _CallSites(ast.NodeVisitor):
    def __init__(self, path: Path, module: ast.Module) -> None:
        self.path = path
        self.module = module
        self.classes: list[ast.ClassDef] = []
        self.found: list[tuple[str, str | None]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node)
        self.generic_visit(node)
        self.classes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        keyword = _SENDERS.get(_call_name(node) or "")
        if keyword is not None:
            where = f"{self.path.relative_to(_BACKEND)}:{node.lineno}"
            value = next((kw.value for kw in node.keywords if kw.arg == keyword), None)
            cls = self.classes[-1] if self.classes else None
            prefix = _key_prefix(value, self.module, cls) if value is not None else None
            self.found.append((where, alert_type_of(prefix) if prefix is not None else None))
        self.generic_visit(node)


def _alert_call_sites() -> list[tuple[str, str | None]]:
    sites: list[tuple[str, str | None]] = []
    for path in sorted(_BACKEND.rglob("*.py")):
        if path in _EXCLUDED:
            continue
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _CallSites(path, module)
        visitor.visit(module)
        sites.extend(visitor.found)
    return sites


def test_every_alert_call_site_sends_under_a_catalogued_type() -> None:
    sites = _alert_call_sites()
    # A scan that finds nothing would pass vacuously, e.g. after a move.
    assert len(sites) >= len(ALERT_TYPES)

    unreadable = [where for where, alert_type in sites if alert_type is None]
    assert not unreadable, (
        "cannot tell which alert type these call sites send under; give the key a "
        f"literal '<type>:' head (or teach this test the new shape): {unreadable}"
    )
    unknown = sorted(
        {(alert_type, where) for where, alert_type in sites if get_alert_type(alert_type) is None}
    )
    assert not unknown, (
        "these alerts cannot be muted until their type is added to ALERT_TYPES in "
        f"serving/observability/alert_types.py: {unknown}"
    )


def test_every_catalogued_type_still_has_a_call_site() -> None:
    """A type whose alert was removed would list a mute that silences nothing."""
    sent = {alert_type for _, alert_type in _alert_call_sites()}
    stale = [alert_type.id for alert_type in ALERT_TYPES if alert_type.id not in sent]
    assert not stale


def test_type_ids_are_unique_and_match_their_key_patterns() -> None:
    ids = [alert_type.id for alert_type in ALERT_TYPES]
    assert len(ids) == len(set(ids))
    for alert_type in ALERT_TYPES:
        assert ":" not in alert_type.id
        assert alert_type_of(alert_type.key_pattern) == alert_type.id
        assert alert_type.label and alert_type.description and alert_type.group


@pytest.mark.parametrize(
    ("key", "alert_type"),
    [
        ("auth_ip_blocked", "auth_ip_blocked"),
        ("circuit_open:zhipu", "circuit_open"),
        ("cost_overrun:user-1:2026-09-27", "cost_overrun"),
        ("db_disconnect:postgres_log", "db_disconnect"),
        ("upstream_auth:glm-4.6:local-12003", "upstream_auth"),
    ],
)
def test_a_key_belongs_to_the_type_before_its_first_colon(key: str, alert_type: str) -> None:
    assert alert_type_of(key) == alert_type


def test_an_unknown_type_is_not_in_the_catalog() -> None:
    assert get_alert_type("auth_ip_blocked") is not None
    assert get_alert_type("not_an_alert") is None
    # The severity-and-title key alert_slack falls back to is never a type.
    assert get_alert_type(alert_type_of("error:Something broke")) is None
