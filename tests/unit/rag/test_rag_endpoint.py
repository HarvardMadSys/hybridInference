from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from serving.rag.chunker import Chunk
from serving.rag.embedder import HashEmbedder
from serving.rag.store import VectorStore
from serving.servers.deps import get_current_user, get_embedding_adapters, get_router
from serving.servers.routers import rag as rag_module


class _FakeRouter:
    """Minimal stand-in for RouteExecutor covering both chat paths."""

    def __init__(self):
        self.seen_messages = None

    async def chat_completion(self, model, messages, **kwargs):
        self.seen_messages = messages
        return {
            "choices": [{"message": {"content": "Create a key from the dashboard."}}],
            "_routing": {"provider": "test"},
        }

    async def stream_chat_completion(self, model, messages, **kwargs):
        for piece in ["Create a key ", "from the dashboard."]:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n"
        yield "data: [DONE]\n\n"


def _build_index(path):
    store = VectorStore(embed_model="hash-256", embedder_mode="hash", dim=256)
    emb = HashEmbedder(dim=256, model="hash-256")
    chunks = [
        Chunk(
            id="quickstart.md#0",
            text="Get Your API Key: register and create your API key from the dashboard.",
            source="quickstart.md",
            title="Quick Start > Get Your API Key",
        ),
        Chunk(
            id="models.md#0",
            text="Available models include glm-5.1 for general coding tasks.",
            source="models.md",
            title="Available Models",
        ),
    ]
    for chunk in chunks:
        store.add(chunk, emb.embed_query(chunk.text))
    store.save(path)


@pytest.fixture
def client(tmp_path, monkeypatch):
    index_path = tmp_path / "docs_index.json"
    _build_index(index_path)
    monkeypatch.setenv("RAG_INDEX_PATH", str(index_path))
    # Reset the module-level index cache so each test loads fresh.
    rag_module._store = None
    rag_module._store_path = None
    rag_module._store_mtime = None

    app = FastAPI()
    app.include_router(rag_module.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1", "role": "user"}
    app.dependency_overrides[get_router] = lambda: _FakeRouter()
    app.dependency_overrides[get_embedding_adapters] = lambda: {}
    return TestClient(app)


def test_status_reports_loaded_index(client):
    resp = client.get("/v1/rag/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["index_loaded"] is True
    assert data["num_chunks"] == 2
    assert data["embedder_mode"] == "hash"


def test_chat_non_streaming_returns_answer_and_sources(client):
    resp = client.post(
        "/v1/rag/chat",
        json={
            "messages": [{"role": "user", "content": "How do I get an API key?"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "Create a key from the dashboard."
    assert "_routing" not in data
    assert data["sources"]
    # The API-key chunk should be retrieved as a source.
    assert any(s["source"] == "quickstart.md" for s in data["sources"])


def test_chat_streaming_emits_sources_then_tokens(client):
    resp = client.post(
        "/v1/rag/chat",
        json={
            "messages": [{"role": "user", "content": "How do I get an API key?"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert '"type": "sources"' in body
    assert "from the dashboard." in body
    assert "[DONE]" in body
    # Sources event must precede the answer tokens.
    assert body.index('"type": "sources"') < body.index("from the dashboard.")


def test_chat_returns_503_when_index_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_PATH", str(tmp_path / "missing.json"))
    rag_module._store = None
    rag_module._store_path = None
    rag_module._store_mtime = None

    app = FastAPI()
    app.include_router(rag_module.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1"}
    app.dependency_overrides[get_router] = lambda: _FakeRouter()
    app.dependency_overrides[get_embedding_adapters] = lambda: {}
    client = TestClient(app)

    resp = client.post(
        "/v1/rag/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 503


class _FakeEmbAdapter:
    """Stand-in embedding adapter for gateway-mode query embedding."""

    def __init__(self, vec=None, exc=None):
        self._vec = vec
        self._exc = exc

    async def embeddings(self, inp, **kwargs):
        if self._exc:
            raise self._exc
        return {"data": [{"embedding": self._vec}], "model": "bge-m3", "usage": {}}


def _build_gateway_index(path, dim):
    store = VectorStore(embed_model="bge-m3", embedder_mode="gateway", dim=dim)
    store.add(
        Chunk(id="a#0", text="alpha", source="a.md", title="A"),
        [1.0] + [0.0] * (dim - 1),
    )
    store.save(path)


def _make_app(index_path, adapters, monkeypatch, router=None):
    monkeypatch.setenv("RAG_INDEX_PATH", str(index_path))
    rag_module._store = None
    rag_module._store_path = None
    rag_module._store_mtime = None
    app = FastAPI()
    app.include_router(rag_module.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1"}
    app.dependency_overrides[get_router] = lambda: router or _FakeRouter()
    app.dependency_overrides[get_embedding_adapters] = lambda: adapters
    return TestClient(app)


def test_chat_502_on_dimension_mismatch(tmp_path, monkeypatch):
    _build_gateway_index(tmp_path / "idx.json", dim=8)
    # Adapter returns a 4-dim vector against an 8-dim index.
    adapters = {"bge-m3": _FakeEmbAdapter(vec=[0.1, 0.2, 0.3, 0.4])}
    client = _make_app(tmp_path / "idx.json", adapters, monkeypatch)
    resp = client.post("/v1/rag/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 502


def test_chat_503_on_embedding_upstream_error(tmp_path, monkeypatch):
    _build_gateway_index(tmp_path / "idx.json", dim=8)
    adapters = {"bge-m3": _FakeEmbAdapter(exc=RuntimeError("key pool exhausted"))}
    client = _make_app(tmp_path / "idx.json", adapters, monkeypatch)
    resp = client.post("/v1/rag/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503


def test_chat_503_on_missing_embedding_adapter(tmp_path, monkeypatch):
    _build_gateway_index(tmp_path / "idx.json", dim=8)
    client = _make_app(tmp_path / "idx.json", {}, monkeypatch)
    resp = client.post("/v1/rag/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503


def test_status_requires_auth(tmp_path, monkeypatch):
    _build_index(tmp_path / "idx.json")
    monkeypatch.setenv("RAG_INDEX_PATH", str(tmp_path / "idx.json"))
    rag_module._store = None
    rag_module._store_path = None
    rag_module._store_mtime = None
    app = FastAPI()
    app.include_router(rag_module.router)

    def _deny():
        raise HTTPException(status_code=401, detail="unauthorized")

    app.dependency_overrides[get_current_user] = _deny
    resp = TestClient(app).get("/v1/rag/status")
    assert resp.status_code == 401


def test_history_split_does_not_duplicate_query(tmp_path, monkeypatch):
    _build_index(tmp_path / "idx.json")  # hash index, dims match
    router = _FakeRouter()
    client = _make_app(tmp_path / "idx.json", {}, monkeypatch, router=router)
    # Conversation ends with an ASSISTANT turn — the query is the earlier user turn.
    resp = client.post(
        "/v1/rag/chat",
        json={
            "messages": [
                {"role": "user", "content": "UNIQUEQUERY about api keys"},
                {"role": "assistant", "content": "partial answer"},
            ],
            "stream": False,
        },
    )
    assert resp.status_code == 200
    seen = router.seen_messages
    # History turns (everything but the final grounded user turn) must not repeat
    # the query verbatim; it should appear only inside the grounded final turn.
    history = seen[:-1]
    assert all("UNIQUEQUERY" not in m["content"] for m in history)
    assert "UNIQUEQUERY" in seen[-1]["content"]
