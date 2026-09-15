"""Structural guards for the serving-to-classifier integration points."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _handler_body(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text())
    handler = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    return handler


def _call_lines(node: ast.AST, attribute: str) -> list[int]:
    return [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == attribute
        and call.lineno is not None
    ]


def test_anthropic_handler_parses_body_before_classifying_and_resolves_session_once():
    """The native Messages path must not inspect locals before JSON parsing."""
    handler = _handler_body(
        ROOT / "apps/backend/serving/servers/routers/anthropic_messages.py",
        "anthropic_messages",
    )
    json_lines = [line for line in _call_lines(handler, "json") if line > handler.lineno]
    classification_lines = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "classify_traffic"
        and node.lineno is not None
    ]
    session_lines = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "session_identity"
        and node.lineno is not None
    ]
    resolve_lines = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_resolve"
        and node.lineno is not None
    ]

    assert json_lines
    assert classification_lines
    assert min(json_lines) < min(classification_lines)
    assert resolve_lines
    assert min(resolve_lines) < min(classification_lines)
    assert len(session_lines) == 1


def test_completions_handler_hands_classification_to_typed_routing_options():
    """The OpenAI-compatible path passes the result beyond request metadata."""
    handler = _handler_body(
        ROOT / "apps/backend/serving/servers/routers/completions.py",
        "chat_completions",
    )
    classification_lines = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "classify_traffic"
        and node.lineno is not None
    ]
    options_lines = [
        node.lineno
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RoutingRequestOptions"
        and node.lineno is not None
    ]

    assert classification_lines
    assert options_lines
    assert min(classification_lines) < min(options_lines)

    source = (ROOT / "apps/backend/serving/servers/routers/completions.py").read_text()
    assert "traffic_classification.confidence > 0.0" in source
