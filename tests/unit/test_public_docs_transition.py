"""Tests for the temporary public documentation parity guard."""

from __future__ import annotations

from ops.ci.check_public_docs_parity import find_parity_errors


def test_transitional_public_documentation_trees_match() -> None:
    """Keep both Pages build roots identical until the legacy root is retired."""
    assert find_parity_errors() == []
