"""A saved embedding alias must not select a different model after replacement."""

from types import SimpleNamespace

from serving.batch.scheduler import BatchScheduler


def test_removed_embedding_model_does_not_fall_back_to_remapped_alias():
    scheduler = object.__new__(BatchScheduler)
    replacement = SimpleNamespace(config=SimpleNamespace(id="new-embedding-model"))
    scheduler._services = SimpleNamespace(
        embedding_adapters={
            "friendly-alias": replacement,
            "new-embedding-model": replacement,
        }
    )
    saved = {"request": {"model": "friendly-alias", "input": "hello"}}
    claimed = SimpleNamespace(model_id="removed-embedding-model")
    body = scheduler._dispatch_body(saved, claimed, "embeddings")
    assert body["model"] == "removed-embedding-model", (
        "A removed pinned model must fail as unavailable, never use its alias's new target"
    )
    assert saved["request"]["model"] == "friendly-alias"
