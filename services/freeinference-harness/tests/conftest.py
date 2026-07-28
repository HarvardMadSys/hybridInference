"""Test fixtures for the standalone harness (run from this directory)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from freeinference_harness.fake_provider import FakeProviderServer  # noqa: E402


@pytest.fixture(scope="session")
def fake_server():
    """Starts one fake provider on an ephemeral port for the whole session."""
    server = FakeProviderServer(port=0).start()
    yield server
    server.stop()


@pytest.fixture()
def fake_base_url(fake_server):
    """Returns the fake's base URL with serve counters reset per test."""
    fake_server.reset()
    return fake_server.base_url
