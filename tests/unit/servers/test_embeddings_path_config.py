"""Configuration-error regressions for route-level embeddings paths."""

from types import SimpleNamespace

import pytest
import yaml

from routing.executor import RouteExecutor
from serving.servers import bootstrap
from serving.servers.registry import register_from_models_yaml

_PATH_ENV = "TEST_EMBEDDINGS_PATH_CONFIG"


def _model(path_value, *, model_id="embedding-test", optional=False):
    return {
        "id": model_id,
        "name": model_id,
        "provider": "openai_compat",
        "model_type": "embedding",
        "route": [
            {
                "kind": "openai_compat",
                "base_url": "https://gateway.example/api/v2",
                "embeddings_path": path_value,
                "optional": optional,
            }
        ],
    }


def _write_models(tmp_path, models):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump({"models": models}))
    return path


def _set_path_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(_PATH_ENV, raising=False)
    else:
        monkeypatch.setenv(_PATH_ENV, value)


@pytest.mark.parametrize("value", [None, "", " \t "])
@pytest.mark.parametrize("continue_on_missing_env", [False, True])
def test_required_path_env_cannot_fall_back_to_default(
    tmp_path, monkeypatch, value, continue_on_missing_env
):
    _set_path_env(monkeypatch, value)
    path = _write_models(tmp_path, [_model("${" + _PATH_ENV + "}")])

    with pytest.raises(ValueError) as caught:
        register_from_models_yaml(
            RouteExecutor(), path, {}, continue_on_missing_env=continue_on_missing_env
        )

    message = str(caught.value)
    assert "embeddings_path" in message
    assert "embedding-test" in message
    assert "route 1" in message
    assert _PATH_ENV in message


@pytest.mark.parametrize("value", [None, "", " \t "])
def test_optional_missing_path_env_skips_only_its_route(tmp_path, monkeypatch, caplog, value):
    _set_path_env(monkeypatch, value)
    model = _model("${" + _PATH_ENV + "}", optional=True)
    model["route"].append({"kind": "vllm", "base_url": "http://localhost:8000/v1"})
    path = _write_models(tmp_path, [model, _model("/embeddings", model_id="later-model")])
    adapters = {}

    register_from_models_yaml(RouteExecutor(), path, adapters)

    assert adapters["embedding-test"].config.provider == "vllm"
    assert adapters["embedding-test"].config.embeddings_path is None
    assert "later-model" in adapters
    assert "Skipping optional route" in caplog.text
    assert "embedding-test" in caplog.text
    assert _PATH_ENV in caplog.text


@pytest.mark.parametrize("explicit_routes", [True, False])
@pytest.mark.parametrize("path_value", ["/embeddings", None])
def test_model_level_path_is_rejected_including_shorthand(tmp_path, explicit_routes, path_value):
    model = _model("/embeddings")
    model["embeddings_path"] = path_value
    if not explicit_routes:
        model["base_url"] = model.pop("route")[0]["base_url"]
    path = _write_models(tmp_path, [model])

    with pytest.raises(ValueError, match="must be declared on a route") as caught:
        register_from_models_yaml(RouteExecutor(), path, {})
    assert "embedding-test" in str(caught.value)


@pytest.mark.parametrize("from_env", [False, True])
def test_path_padding_is_trimmed_before_env_expansion_and_url_join(tmp_path, monkeypatch, from_env):
    monkeypatch.setenv(_PATH_ENV, " \t/embeddings\n")
    raw_path = "  ${" + _PATH_ENV + "} " if from_env else " \t/embeddings\n"
    path = _write_models(tmp_path, [_model(raw_path)])
    adapters = {}

    register_from_models_yaml(RouteExecutor(), path, adapters)

    assert adapters["embedding-test"].config.embeddings_path == "/embeddings"
    assert (
        adapters["embedding-test"]._build_embeddings_url()
        == "https://gateway.example/api/v2/embeddings"
    )


@pytest.mark.parametrize("raw_path", [" \t ", "../embeddings", "/nested/../embeddings", 123])
@pytest.mark.parametrize("optional", [False, True])
def test_invalid_literal_path_is_not_an_optional_missing_value(tmp_path, raw_path, optional):
    path = _write_models(tmp_path, [_model(raw_path, optional=optional)])

    with pytest.raises(ValueError) as caught:
        register_from_models_yaml(RouteExecutor(), path, {})
    message = str(caught.value)
    assert "embeddings_path" in message
    assert "embedding-test" in message
    assert "route 1" in message


@pytest.mark.parametrize("missing_field", ["api_key", "base_url"])
def test_optional_missing_env_does_not_hide_invalid_path(tmp_path, monkeypatch, missing_field):
    monkeypatch.delenv("TEST_EMBEDDINGS_OTHER_MISSING_ENV", raising=False)
    model = _model(123, optional=True)
    model["route"][0][missing_field] = "${TEST_EMBEDDINGS_OTHER_MISSING_ENV}"
    path = _write_models(tmp_path, [model])

    with pytest.raises(ValueError, match="embeddings_path must be a string or null"):
        register_from_models_yaml(RouteExecutor(), path, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["type", "missing-env", "model-level"])
async def test_bootstrap_does_not_serve_partial_registry_on_invalid_path(
    tmp_path, monkeypatch, invalid_kind
):
    monkeypatch.delenv(_PATH_ENV, raising=False)
    invalid = _model(123, model_id="invalid-middle")
    if invalid_kind == "missing-env":
        invalid["route"][0]["embeddings_path"] = "${" + _PATH_ENV + "}"
    elif invalid_kind == "model-level":
        invalid = _model("/embeddings", model_id="invalid-middle")
        invalid["embeddings_path"] = "/embeddings"
    path = _write_models(
        tmp_path,
        [
            _model("/embeddings", model_id="valid-before"),
            invalid,
            _model("/embeddings", model_id="valid-after"),
        ],
    )
    monkeypatch.setattr(
        bootstrap, "resolve_config_path", lambda _: SimpleNamespace(path=path, source="env")
    )

    with pytest.raises(ValueError, match="invalid-middle"):
        await bootstrap._init_router_and_models(RouteExecutor())


@pytest.mark.asyncio
async def test_other_model_loading_errors_keep_existing_bootstrap_behavior(
    tmp_path, monkeypatch, caplog
):
    path = _write_models(tmp_path, [_model("/embeddings")])
    monkeypatch.setattr(
        bootstrap, "resolve_config_path", lambda _: SimpleNamespace(path=path, source="env")
    )

    def fail(*args, **kwargs):
        raise ValueError("unrelated config error")

    monkeypatch.setattr(bootstrap, "register_from_models_yaml", fail)

    assert await bootstrap._init_router_and_models(RouteExecutor()) == ({}, [])
    assert "Failed to load models.yaml: unrelated config error" in caplog.text
