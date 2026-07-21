"""Contract tests for canonical routing endpoint identity."""

from types import SimpleNamespace

import pytest

from routing.endpoints import endpoint_id_for_adapter


def test_endpoint_id_for_adapter_prefers_explicit_endpoint_id():
    adapter = SimpleNamespace(
        config=SimpleNamespace(
            endpoint_id="openai:api.example.com:443",
            provider="openai",
        )
    )

    assert endpoint_id_for_adapter(adapter) == "openai:api.example.com:443"


@pytest.mark.parametrize("endpoint_id", [None, ""])
def test_endpoint_id_for_adapter_falls_back_to_provider(endpoint_id):
    adapter = SimpleNamespace(config=SimpleNamespace(endpoint_id=endpoint_id, provider="openai"))

    assert endpoint_id_for_adapter(adapter) == "openai"


@pytest.mark.parametrize(
    ("endpoint_id", "provider", "expected"),
    [
        ("  endpoint-with-whitespace  ", "openai", "  endpoint-with-whitespace  "),
        (123, "openai", 123),
        (None, 456, 456),
    ],
)
def test_endpoint_id_for_adapter_preserves_raw_legacy_values(
    endpoint_id,
    provider,
    expected,
):
    adapter = SimpleNamespace(config=SimpleNamespace(endpoint_id=endpoint_id, provider=provider))

    assert endpoint_id_for_adapter(adapter) == expected
