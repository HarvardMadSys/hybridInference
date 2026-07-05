from __future__ import annotations

import math

from serving.rag.embedder import HashEmbedder, l2_normalize


def test_hash_embedder_is_deterministic_and_normalized():
    emb = HashEmbedder(dim=64)
    a = emb.embed_query("how do I get an api key")
    b = emb.embed_query("how do I get an api key")
    assert a == b
    assert len(a) == 64
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-6)


def test_hash_embedder_similar_text_scores_higher():
    from serving.rag.store import cosine_similarity

    emb = HashEmbedder(dim=256)
    query = emb.embed_query("set up cursor with an openai compatible base url")
    related = emb.embed_query("configure cursor to use the openai compatible base url")
    unrelated = emb.embed_query("billing quota daily reset postgres migration")
    assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)


def test_l2_normalize_handles_zero_vector():
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]
