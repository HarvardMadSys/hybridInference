"""An embeddings batch item runs on the model it was admitted for (issue #1690, T9).

Admission records an embeddings item under its adapter's ``config.id``; the
worker then pins the stored body's ``model`` to that id, and the execution
looks the adapter up by it in ``embedding_adapters``. That only works while
every adapter the registry stores is reachable under its own ``config.id`` --
whatever name (canonical or alias) the caller used, whatever the route's
``provider_model_id``, and for a fallback chain as well. And because the alias
is never resolved again, an alias remapped after admission does not move the
item to another model, and a model removed after admission fails as an
unknown model without reaching any upstream.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from routing.routers import FixedRouter
from serving.batch.dispatch import dispatch_item
from serving.batch.scheduler import BatchScheduler
from serving.servers.registry import register_from_models_yaml
from tests.unit.serving.test_batch_targets_dispatch import (
    EMBED,
    EMBED_ALIAS,
    _admit_embedding,
    _Embedder,
    _increments,
    _logged,
    _owner,
    _setup,
)

_MODELS = """
models:
  - id: emb-canon
    name: Embedding Canon
    provider: openai_compat
    model_type: embedding
    required_role: free
    aliases: [emb-alias-one, emb-alias-two]
    route:
      - kind: openai_compat
        base_url: https://primary.example/v1
        api_key: test-only-not-a-key
        provider_model_id: vendor/embedding-large
      - kind: openai_compat
        base_url: https://fallback.example/v1
        api_key: test-only-not-a-key
  - id: emb-single
    name: Embedding Single
    provider: openai_compat
    model_type: embedding
    required_role: free
    aliases: [emb-single-alias]
    route:
      - kind: openai_compat
        base_url: https://single.example/v1
        api_key: test-only-not-a-key
        provider_model_id: vendor/other
"""


def test_every_registered_embedding_adapter_is_reachable_under_its_config_id(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(_MODELS)
    router, adapters = FixedRouter(), {}

    register_from_models_yaml(router, path, adapters)

    assert set(adapters) == {
        "emb-canon",
        "emb-alias-one",
        "emb-alias-two",
        "emb-single",
        "emb-single-alias",
    }
    for name, adapter in adapters.items():
        pinned = adapter.config.id
        # What admission stores and the worker pins, for whichever name was used.
        assert pinned in ("emb-canon", "emb-single"), name
        assert adapters[pinned] is adapter, name
        assert pinned in router.embedding_routes, name
    # The fallback chain is pinned by its primary's id, not the vendor model.
    assert type(adapters["emb-canon"]).__name__ == "FallbackEmbeddingAdapter"
    assert adapters["emb-canon"].config.provider_model_id == "vendor/embedding-large"


def _pinned(item_body: dict, canonical: str) -> dict:
    """The body the worker dispatches: the stored one, pinned to the admitted id."""
    entry = SimpleNamespace(model_id=canonical)
    return BatchScheduler._dispatch_body(None, {"request": item_body}, entry, "embeddings")


async def _run(services, app, body: dict):
    return await dispatch_item(
        app=app,
        services=services,
        user_ctx=_owner(),
        body=body,
        batch_id="batch-pin",
        target="embeddings",
    )


async def test_an_item_named_by_alias_is_admitted_pinned_and_run_on_its_model():
    app, services, _upstream, embedder = _setup()
    item = {"model": EMBED_ALIAS, "input": "hello"}

    canonical, problem = await _admit_embedding(services, model=EMBED_ALIAS)
    assert (canonical, problem) == (EMBED, None)
    body = _pinned(item, canonical)
    assert body == {"model": EMBED, "input": "hello"}
    assert item["model"] == EMBED_ALIAS  # the stored request is left as sent

    result = await _run(services, app, body)

    assert result.error is None, result
    assert result.cost_status == "billed"
    assert len(embedder.calls) == 1
    assert (await _logged(services))["model_id"] == EMBED


async def test_an_alias_remapped_after_admission_does_not_move_the_item():
    app, services, _upstream, original = _setup()
    canonical, _problem = await _admit_embedding(services, model=EMBED_ALIAS)
    # An admin points the alias at another model before the item runs.
    other = _Embedder()
    services.embedding_adapters[EMBED_ALIAS] = other

    result = await _run(services, app, _pinned({"model": EMBED_ALIAS, "input": "x"}, canonical))

    assert result.error is None, result
    assert len(original.calls) == 1
    assert other.calls == []


@pytest.mark.parametrize("used", [EMBED, EMBED_ALIAS])
async def test_a_model_removed_after_admission_fails_unknown_without_upstream(used):
    app, services, _upstream, embedder = _setup()
    canonical, _problem = await _admit_embedding(services, model=used)
    # The model is gone; an alias of it is still registered.
    del services.embedding_adapters[EMBED]
    del services.router.embedding_routes[EMBED]

    result = await _run(services, app, _pinned({"model": used, "input": "x"}, canonical))

    assert (result.status_code, result.retryable) == (404, False), result
    assert result.error["code"] == "model_not_found"
    assert result.cost_status == "not_billed"
    assert embedder.calls == []
    assert await _increments(services) == []
