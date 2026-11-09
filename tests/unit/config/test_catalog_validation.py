"""Admin's model catalog check and registration agree, in both directions.

``documents.validate`` decides what Admin saves and what a stored catalog is
loaded as; ``register_from_models_document`` decides what the gateway serves.
Where they disagreed, an upgrade served no models (a carried-over catalog the
check refused) or a saved catalog failed at the next restart (one registration
refused). Both now run one per-entry checker, so these tests hold them to the
same answers, and hold every message to naming a place, never a value.
"""

from __future__ import annotations

import copy
import json

import pytest

from routing.routers import FixedRouter
from serving.config.documents import DocumentError, validate
from serving.servers.registry import RegistryConfigError, register_from_models_document


def _model(model_id: str = "chat", **fields) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "provider": "vllm",
        "route": [{"kind": "vllm", "weight": 1.0, "base_url": "http://localhost:9/v1"}],
        **fields,
    }


def _route(**fields) -> dict:
    return {"kind": "vllm", "weight": 1.0, "base_url": "http://localhost:9/v1", **fields}


def _register(document: dict) -> FixedRouter:
    router = FixedRouter()
    register_from_models_document(router, copy.deepcopy(document), {})
    return router


# --- what registration accepts, Admin accepts ---------------------------------

_ACCEPTED = {
    # A YAML catalog's anchor block survives the move to JSON as a top-level key.
    "anchor block": {"x-local": {"kind": "vllm", "weight": 1.0}, "models": [_model()]},
    # Registration serves a route without a kind with the model's provider.
    "route without kind": {
        "models": [_model(route=[{"weight": 1.0, "base_url": "http://localhost:9/v1"}])]
    },
    # An embedding model produces no output tokens.
    "embedding output length 0": {
        "models": [_model("embed", type="embedding", context_length=8192, max_output_length=0)]
    },
    # Registration warns; the name resolves to the model that loads last.
    "alias claimed twice": {
        "models": [_model("one", aliases=["shared"]), _model("two", aliases=["shared"])]
    },
    "alias repeated by its model": {"models": [_model(aliases=["same", "same"])]},
    "no name": {"models": [{key: value for key, value in _model().items() if key != "name"}]},
    "shorthand model": {"models": [{"id": "short", "provider": "zai", "base_url": "https://z"}]},
    "empty models": {"models": None},
    "auto section": {"models": [_model()], "auto": {"enabled": False}},
    "reasoning_effort_map on a route": {
        "models": [
            _model(
                route=[_route(reasoning_effort_map={"low": "high", "high": "xhigh", "max": "max"})]
            )
        ]
    },
}


@pytest.mark.parametrize("document", _ACCEPTED.values(), ids=_ACCEPTED.keys())
def test_what_registration_accepts_admin_accepts(document):
    validate("MODEL_CATALOG", json.dumps(document))
    _register(document)


def test_a_route_without_a_kind_is_served_by_its_models_provider():
    router = _register(_ACCEPTED["route without kind"])

    assert router.routes["chat"].adapters[0][0].config.provider == "vllm"


def test_a_model_without_a_name_is_shown_by_its_id():
    router = _register(_ACCEPTED["no name"])

    assert router.routes["chat"].adapters[0][0].config.name == "chat"


# --- what registration refuses, Admin refuses, with the same reason ------------

_REFUSED = {
    "include_usage_in_stream quoted": (
        {"models": [_model(route=[_route(include_usage_in_stream="false")])]},
        r"include_usage_in_stream must be a boolean, not a string \(model 'chat', route 1\)",
    ),
    "include_usage_in_stream null": (
        {"models": [_model(route=[_route(include_usage_in_stream=None)])]},
        "include_usage_in_stream must be a boolean, not null",
    ),
    "include_usage_in_stream on the model": (
        {"models": [_model(include_usage_in_stream=True)]},
        r"include_usage_in_stream must be declared on a route.*\(model 'chat'\)",
    ),
    "null_cache_details_means_miss on the model": (
        {"models": [_model(null_cache_details_means_miss=True)]},
        "null_cache_details_means_miss must be declared on a route",
    ),
    "deployment_type on the model": (
        {"models": [_model(deployment_type="self_hosted")]},
        "deployment_type must be declared on a route",
    ),
    "deployment_type unknown": (
        {"models": [_model(route=[_route(deployment_type="local")])]},
        r"deployment_type must be one of: auto, self_hosted, provider_api \(model 'chat', route 1\)",
    ),
    "embeddings_path whitespace": (
        {"models": [_model(route=[_route(embeddings_path=" \t ")])]},
        "embeddings_path must not be whitespace-only",
    ),
    "embeddings_path climbing": (
        {"models": [_model(route=[_route(embeddings_path="/v1/../embeddings")])]},
        "embeddings_path must not contain '..' path segments",
    ),
    "embeddings_path on the model": (
        {"models": [_model(embeddings_path="/embeddings")]},
        "embeddings_path must be declared on a route",
    ),
    "provider label not a slug": (
        {"models": [_model(route=[_route(provider="Not A Slug!")])]},
        r"provider must use lowercase letters.*\(model 'chat', route 1\)",
    ),
    "provider label of another provider": (
        {"models": [_model(route=[_route(provider="zai")])]},
        "provider is reserved by a built-in provider",
    ),
    "blank display name": (
        {"models": [_model(route=[_route(provider_display_name=" ")])]},
        "provider_display_name must be a non-empty string",
    ),
    "unknown kind": (
        {"models": [_model(route=[_route(kind="kimi_coding")])]},
        r"kind names no adapter kind this gateway has.*\(model 'chat', route 1\)",
    ),
    "shorthand with an unknown provider": (
        {"models": [{"id": "gpt", "provider": "openai", "base_url": "https://api.openai.com"}]},
        r"provider names no adapter kind this gateway has.*\(model 'gpt'\)",
    ),
    "no adapter at all": (
        {"models": [{"id": "nothing", "base_url": "https://api.example.com"}]},
        r"kind is required.*\(model 'nothing'\)",
    ),
    "malformed openrouter pin": (
        {"models": [_model(route=[_route(kind="openrouter[]")])]},
        "kind must be openrouter or openrouter",
    ),
    "unknown processor": (
        {"models": [_model(processor="not-a-processor")]},
        "processor must be one of",
    ),
    "both api_key and api_keys": (
        {"models": [_model(route=[_route(api_key="${A}", api_keys=["${B}"])])]},
        "api_key and api_keys are both set",
    ),
    "api_keys not a list": (
        {"models": [_model(route=[_route(api_keys="${A}")])]},
        "api_keys must be a list",
    ),
    "weight text": (
        {"models": [_model(route=[_route(weight="0.5")])]},
        "weight must be a number",
    ),
    "aliases text": (
        {"models": [_model(aliases="abc")]},
        "aliases must be a list of non-empty strings",
    ),
    "extra_body text": (
        {"models": [_model(extra_body="x")]},
        "extra_body must be an object",
    ),
    "route an object": (
        {"models": [_model(route={"kind": "vllm"})]},
        "route must be a list of objects",
    ),
    "pricing source and schedule": (
        {
            "models": [
                _model(
                    pricing_source={"provider": "deepseek", "model": "deepseek-flash"},
                    route=[
                        _route(
                            pricing_schedule={
                                "effective_at": "2026-11-01T00:00:00Z",
                                "default": {"prompt": "1"},
                            }
                        )
                    ],
                )
            ]
        },
        "pricing_source and pricing_schedule cannot be combined",
    ),
    "unknown router": (
        {"models": [_model(router="nimbus")]},
        "router names no routing strategy",
    ),
    "invalid router params": (
        {"models": [_model(router="fixed", router_params={"unexpected": 1})]},
        r"router_params is not valid for this router: /unexpected Extra inputs",
    ),
    "models an object": ({"models": {"id": "chat"}}, "models must be a list"),
    "id missing": ({"models": [{"name": "x"}]}, r"id must be a non-empty string \(models\[0\]\)"),
    "reasoning_effort_map on the model": (
        {"models": [_model(reasoning_effort_map={"low": "high"})]},
        r"reasoning_effort_map must be declared on a route.*\(model 'chat'\)",
    ),
    "reasoning_effort_map empty": (
        {"models": [_model(route=[_route(reasoning_effort_map={})])]},
        r"reasoning_effort_map must map 1 to 16 distinct public levels.*\(model 'chat', route 1\)",
    ),
    "reasoning_effort_map with a blank in a level": (
        {"models": [_model(route=[_route(reasoning_effort_map={"low": "very high"})])]},
        r"reasoning_effort_map must map 1 to 16 distinct public levels.*\(model 'chat', route 1\)",
    ),
    "reasoning_effort_map a list of levels": (
        {"models": [_model(route=[_route(reasoning_effort_map=["low", "high"])])]},
        "reasoning_effort_map must map 1 to 16 distinct public levels",
    ),
    "reasoning_effort_map on a native adapter": (
        {
            "models": [
                _model(
                    route=[
                        _route(
                            kind="anthropic",
                            base_url="https://api.anthropic.com",
                            reasoning_effort_map={"low": "low"},
                        )
                    ]
                )
            ]
        },
        r"reasoning_effort_map needs an adapter that sends reasoning_effort.*\(model 'chat', route 1\)",
    ),
}


@pytest.mark.parametrize("document,reason", _REFUSED.values(), ids=_REFUSED.keys())
def test_what_registration_refuses_admin_refuses_with_the_same_reason(document, reason):
    with pytest.raises(DocumentError, match=reason) as saved:
        validate("MODEL_CATALOG", json.dumps(document))
    with pytest.raises(RegistryConfigError, match=reason) as registered:
        _register(document)
    assert str(saved.value) == str(registered.value)


def test_a_maximum_output_length_must_still_be_a_whole_number():
    for value in (-1, 1.5, True, "8192", 10_000_001):
        document = {"models": [_model(max_output_length=value)]}
        with pytest.raises(DocumentError, match="max_output_length must be a whole number"):
            validate("MODEL_CATALOG", json.dumps(document))
    with pytest.raises(DocumentError, match="context_length must be a whole number"):
        validate("MODEL_CATALOG", json.dumps({"models": [_model(context_length=0)]}))


# --- a refusal names the place, never the value --------------------------------

_SECRET = "sk-live-do-not-show-0042"


@pytest.mark.parametrize(
    "key,document",
    [
        ("MODEL_CATALOG", {"models": [_model(route=[_route(weight=_SECRET)])]}),
        ("MODEL_CATALOG", {"models": [_model(route=[_route(provider=f"{_SECRET}!")])]}),
        ("MODEL_CATALOG", {"models": [_model(route=[_route(kind=_SECRET)])]}),
        ("MODEL_CATALOG", {"models": [_model(route=[_route(include_usage_in_stream=_SECRET)])]}),
        (
            "MODEL_CATALOG",
            {"models": [_model(route=[_route(reasoning_effort_map={"low": f"{_SECRET}!"})])]},
        ),
        ("MODEL_CATALOG", {"models": [_model(router="routewise", router_params={"x": _SECRET})]}),
        ("MODEL_CATALOG", {"models": [_model(router=_SECRET)]}),
        ("MODEL_CATALOG", {"models": [_model(api_key=_SECRET)]}),
        ("ROUTING_POLICY", {"default_router": "fixed", "timeout": _SECRET}),
        ("ALERT_RULES", {"rules": {"client_error_burst": {"threshold_count": _SECRET}}}),
        ("SITE_CONFIGURATION", {"schema_version": 1, "distribution": {"id": "x"}, "z": _SECRET}),
        ("SITE_CONFIGURATION", {"schema_version": _SECRET, "distribution": {"id": "x"}}),
        ("SITE_BRANDING", {"organization": {"name": "x", "url": _SECRET}}),
    ],
)
def test_a_refusal_names_the_place_and_never_shows_the_value(key, document):
    with pytest.raises(DocumentError) as refused:
        validate(key, json.dumps(document))
    assert _SECRET not in str(refused.value)
    assert str(refused.value)


# --- the public site document holds no credentials -----------------------------


@pytest.mark.parametrize(
    "document,reason",
    [
        (
            {"schema_version": 1, "distribution": {"id": "x", "token": _SECRET}},
            "/distribution/token names a credential, and this document holds none",
        ),
        (
            {"schema_version": 1, "distribution": {"id": "x"}, "site": {"api_key": "${KEY}"}},
            "/site/api_key names a credential",
        ),
        (
            {"schema_version": 1, "distribution": {"id": "x"}, "secrets": {"a": "b"}},
            "/secrets Extra inputs are not permitted",
        ),
    ],
)
def test_the_site_configuration_refuses_credentials_and_unknown_keys(document, reason):
    with pytest.raises(DocumentError, match=reason) as refused:
        validate("SITE_CONFIGURATION", json.dumps(document))
    assert _SECRET not in str(refused.value)


def test_a_site_configuration_without_credentials_is_still_accepted():
    validate(
        "SITE_CONFIGURATION",
        json.dumps(
            {
                "schema_version": 1,
                "distribution": {"id": "x", "display_name": "X"},
                "site": {"support_email": "help@example.com"},
                "features": {"public_signup": False},
            }
        ),
    )
