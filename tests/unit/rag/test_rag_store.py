from __future__ import annotations

from serving.rag.chunker import Chunk
from serving.rag.store import VectorStore, cosine_similarity


def _chunk(cid: str, text: str) -> Chunk:
    return Chunk(id=cid, text=text, source="doc.md", title="Doc")


def test_search_ranks_by_cosine_similarity():
    store = VectorStore(embed_model="test", embedder_mode="hash", dim=3)
    store.add(_chunk("a", "a"), [1.0, 0.0, 0.0])
    store.add(_chunk("b", "b"), [0.0, 1.0, 0.0])
    store.add(_chunk("c", "c"), [0.9, 0.1, 0.0])

    results = store.search([1.0, 0.0, 0.0], top_k=2)
    assert [r.id for r, _ in results] == ["a", "c"]
    assert results[0][1] > results[1][1]


def test_save_and_load_round_trip(tmp_path):
    store = VectorStore(embed_model="bge-m3", embedder_mode="gateway", dim=2)
    store.add(_chunk("a", "hello"), [0.1, 0.2])
    path = tmp_path / "index.json"
    store.save(path)

    loaded = VectorStore.load(path)
    assert loaded.embed_model == "bge-m3"
    assert loaded.embedder_mode == "gateway"
    assert loaded.dim == 2
    assert len(loaded.records) == 1
    assert loaded.records[0].text == "hello"
    assert loaded.records[0].embedding == [0.1, 0.2]


def test_cosine_similarity_degenerate_inputs():
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0
