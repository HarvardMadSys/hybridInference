"""REQUEST_MAX_BODY_MB has a maximum, wherever its value comes from (review L8)."""

from __future__ import annotations

import pytest

from serving.config.app_config_registry import static_entry
from serving.servers.middleware.body_limit import (
    MAX_BODY_SETTING,
    MAXIMUM_MAX_BODY_MB,
    configured_max_body_bytes,
)


@pytest.mark.parametrize("raw", ["257", "100000", "9" * 30])
def test_a_value_past_the_maximum_is_held_to_it(monkeypatch, raw: str) -> None:
    # An environment value is not bounded by the registry, so the middleware
    # holds it: no setting reopens an unbounded body.
    monkeypatch.setenv(MAX_BODY_SETTING, raw)
    assert configured_max_body_bytes() == MAXIMUM_MAX_BODY_MB * 1024 * 1024


def test_the_maximum_itself_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv(MAX_BODY_SETTING, str(MAXIMUM_MAX_BODY_MB))
    assert configured_max_body_bytes() == MAXIMUM_MAX_BODY_MB * 1024 * 1024


def test_the_registry_refuses_what_the_middleware_would_hold() -> None:
    entry = static_entry(MAX_BODY_SETTING)
    assert entry is not None
    assert entry.maximum == MAXIMUM_MAX_BODY_MB
