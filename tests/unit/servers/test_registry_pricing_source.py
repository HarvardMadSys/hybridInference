"""Automatic pricing bindings survive registry and runtime config cloning."""

from __future__ import annotations

import pytest
import yaml

from routing.executor import RouteExecutor
from serving.adapters.base import ModelConfig
from serving.servers.registry import register_from_models_yaml
from serving.servers.routers.admin.provider_routes import _config_to_dict

SOURCE = {"provider": "deepseek", "model": "deepseek-flash"}


@pytest.mark.parametrize(
    "source",
    [
        {},
        "deepseek",
        {"provider": "unknown", "model": "deepseek-flash"},
        {"provider": "deepseek", "model": "guessed-model-alias"},
        {**SOURCE, "url": "https://untrusted.example/prices"},
    ],
)
def test_model_config_rejects_unsupported_price_sources(source):
    with pytest.raises(ValueError, match="pricing_source"):
        ModelConfig(
            id="m", name="m", provider="local", base_url="http://test", pricing_source=source
        )


def test_source_and_manual_schedule_are_mutually_exclusive():
    with pytest.raises(ValueError, match="cannot be combined"):
        ModelConfig(
            id="m",
            name="m",
            provider="local",
            base_url="http://test",
            pricing_source=SOURCE,
            pricing_schedule={
                "effective_at": "2026-01-01T00:00:00Z",
                "default": {"prompt": "1"},
                "windows": [],
            },
        )


def test_registry_inherits_source_and_route_can_disable_it(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "models": [
                    {
                        "id": "price-source-test",
                        "name": "Price source test",
                        "pricing_source": SOURCE,
                        "pricing_currency": "CNY",
                        "route": [
                            {
                                "kind": "openai_compat",
                                "base_url": "http://localhost:8101",
                                "weight": 1,
                            },
                            {
                                "kind": "openai_compat",
                                "base_url": "http://localhost:8102",
                                "weight": 1,
                                "pricing_source": None,
                                "pricing_currency": "USD",
                                "pricing": {"prompt": "2", "completion": "3"},
                            },
                        ],
                    }
                ]
            }
        )
    )
    router = RouteExecutor()
    register_from_models_yaml(router, path)
    configs = [adapter.config for adapter, _ in router.routes["price-source-test"].adapters]
    automatic = next(config for config in configs if config.base_url.endswith("8101"))
    manual = next(config for config in configs if config.base_url.endswith("8102"))
    assert automatic.pricing_source == SOURCE
    assert automatic.pricing_currency == "CNY"
    assert manual.pricing_source is None
    assert manual.pricing_currency == "USD"
    assert manual.pricing == {"prompt": "2", "completion": "3"}
    restored = ModelConfig(**_config_to_dict(automatic))
    assert restored.pricing_source == SOURCE
    assert restored.pricing_currency == "CNY"
    assert restored.pricing_source is not automatic.pricing_source
