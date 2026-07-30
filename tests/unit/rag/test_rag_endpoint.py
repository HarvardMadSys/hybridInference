from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from serving.rag.chunker import Chunk
from serving.rag.config import load_rag_settings
from serving.rag.embedder import HashEmbedder
from serving.rag.store import VectorStore
from serving.servers.deps import get_current_user
from serving.servers.routers import rag as rag_module

# --------------------------------------------------------------------------- #
# Fakes for the gateway-as-a-user HTTP helpers (monkeypatched onto rag_module).
# --------------------------------------------------------------------------- #


def _fake_chat_json(capture, answer="Create a key from the dashboard."):
    async def _impl(settings, model, messages, on_behalf_of=None):
        capture["messages"] = messages
        capture["model"] = model
        capture["on_behalf_of"] = on_behalf_of
        return answer

    return _impl


def _fake_open_stream(pieces, capture=None):
    async def _impl(settings, model, messages, on_behalf_of=None):
        if capture is not None:
            capture["on_behalf_of"] = on_behalf_of

        async def _iter():
            yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            for piece in pieces:
                yield (
                    "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n"
                ).encode()
            yield b"data: [DONE]\n\n"

        async def _aclose():
            return None

        return _iter(), _aclose

    return _impl


def _fake_embed(vec):
    async def _impl(settings, model, text, on_behalf_of=None):
        return vec

    return _impl


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
            text="Available models include qwen3.6-35b for general coding tasks.",
            source="models.md",
            title="Available Models",
        ),
    ]
    for chunk in chunks:
        store.add(chunk, emb.embed_query(chunk.text))
    store.save(path)


def _build_gateway_index(path, dim):
    store = VectorStore(embed_model="bge-m3", embedder_mode="gateway", dim=dim)
    store.add(Chunk(id="a#0", text="alpha", source="a.md", title="A"), [1.0] + [0.0] * (dim - 1))
    store.save(path)


def _make_app(index_path, monkeypatch):
    monkeypatch.setenv("RAG_INDEX_PATH", str(index_path))
    monkeypatch.delenv("RAG_API_KEY", raising=False)
    rag_module._store = None
    rag_module._store_path = None
    rag_module._store_mtime = None
    app = FastAPI()
    app.include_router(rag_module.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "u1", "role": "user"}
    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    _build_index(tmp_path / "idx.json")
    return _make_app(tmp_path / "idx.json", monkeypatch)


def test_status_reports_loaded_index(client):
    resp = client.get("/v1/rag/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["index_loaded"] is True
    assert data["num_chunks"] == 2
    assert data["embedder_mode"] == "hash"
    # The server filesystem path must not leak.
    assert "index_path" not in data


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
    assert TestClient(app).get("/v1/rag/status").status_code == 401


def test_chat_non_streaming_returns_answer_and_sources(client, monkeypatch):
    cap = {}
    monkeypatch.setattr(rag_module, "_gateway_chat_json", _fake_chat_json(cap))
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
    assert data["model"] == "qwen3.6-35b"
    assert any(s["source"] == "quickstart.md" for s in data["sources"])
    # Retrieved context is handed to the (upstream) chat call.
    assert "Documentation context" in cap["messages"][-1]["content"]
    # The gateway self-call acts on behalf of the JWT-verified end user (u1),
    # so its api_logs / cost / quota attribute to them, not the RAG account.
    assert cap["on_behalf_of"] == "u1"


def test_chat_streaming_emits_sources_then_tokens(client, monkeypatch):
    monkeypatch.setattr(
        rag_module, "_open_chat_stream", _fake_open_stream(["Create a key ", "from the dashboard."])
    )
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
    assert body.index('"type": "sources"') < body.index("from the dashboard.")


def test_chat_returns_503_when_index_missing(tmp_path, monkeypatch):
    client = _make_app(tmp_path / "missing.json", monkeypatch)
    resp = client.post("/v1/rag/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503


def test_chat_502_on_dimension_mismatch(tmp_path, monkeypatch):
    _build_gateway_index(tmp_path / "idx.json", dim=8)
    client = _make_app(tmp_path / "idx.json", monkeypatch)
    # Gateway embed returns a 4-dim vector against an 8-dim index.
    monkeypatch.setattr(rag_module, "_gateway_embed", _fake_embed([0.1, 0.2, 0.3, 0.4]))
    resp = client.post("/v1/rag/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 502


def test_chat_503_when_api_key_unset(tmp_path, monkeypatch):
    # Gateway index + no RAG_API_KEY and no monkeypatch -> _require_api_key fires.
    _build_gateway_index(tmp_path / "idx.json", dim=8)
    client = _make_app(tmp_path / "idx.json", monkeypatch)
    resp = client.post("/v1/rag/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503


def test_chat_passes_through_upstream_429(client, monkeypatch):
    async def _quota(settings, model, messages, on_behalf_of=None):
        raise rag_module._map_upstream_error(429, "generation")

    monkeypatch.setattr(rag_module, "_gateway_chat_json", _quota)
    resp = client.post(
        "/v1/rag/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert resp.status_code == 429


def test_history_split_does_not_duplicate_query(client, monkeypatch):
    cap = {}
    monkeypatch.setattr(rag_module, "_gateway_chat_json", _fake_chat_json(cap))
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
    seen = cap["messages"]
    history = seen[:-1]
    assert all("UNIQUEQUERY" not in m["content"] for m in history)
    assert "UNIQUEQUERY" in seen[-1]["content"]


def test_map_upstream_error_mapping():
    assert rag_module._map_upstream_error(429, "x").status_code == 429
    assert rag_module._map_upstream_error(401, "x").status_code == 502
    assert rag_module._map_upstream_error(403, "x").status_code == 502
    assert rag_module._map_upstream_error(500, "x").status_code == 502


# --------------------------------------------------------------------------- #
# HTTP-transport tests: exercise the real helper bodies (URL, auth header,
# status mapping, JSON parsing) via an httpx MockTransport.
# --------------------------------------------------------------------------- #


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rag_module.httpx, "AsyncClient", factory)


def _settings(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "test-key")
    monkeypatch.setenv("RAG_API_BASE_URL", "http://gw.test/v1")
    return load_rag_settings()


async def test_gateway_embed_transport_ok(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["ua"] = request.headers.get("user-agent")
        seen["obo"] = request.headers.get("x-on-behalf-of")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    _patch_transport(monkeypatch, handler)
    vec = await rag_module._gateway_embed(_settings(monkeypatch), "bge-m3", "hello")
    assert vec == [0.1, 0.2, 0.3]
    assert seen["url"].endswith("/v1/embeddings")
    assert seen["auth"] == "Bearer test-key"
    # RAG self-calls are tagged so they're identifiable in api_logs.
    assert seen["ua"] == "doc_assistant"
    assert seen["body"] == {"model": "bge-m3", "input": "hello"}
    # No on-behalf-of by default: the header is only sent when a user id is passed.
    assert seen["obo"] is None


async def test_gateway_embed_sends_on_behalf_of_header(monkeypatch):
    seen = {}

    def handler(request):
        seen["obo"] = request.headers.get("x-on-behalf-of")
        return httpx.Response(200, json={"data": [{"embedding": [0.5]}]})

    _patch_transport(monkeypatch, handler)
    await rag_module._gateway_embed(_settings(monkeypatch), "bge-m3", "hello", "user-42")
    # Threaded so /v1/embeddings attributes the row to the real end user.
    assert seen["obo"] == "user-42"


async def test_gateway_chat_json_transport_ok_and_payload(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi there"}}]})

    _patch_transport(monkeypatch, handler)
    out = await rag_module._gateway_chat_json(
        _settings(monkeypatch), "qwen3.6-35b", [{"role": "user", "content": "q"}]
    )
    assert out == "hi there"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["body"]["model"] == "qwen3.6-35b"
    assert seen["body"]["stream"] is False


async def test_gateway_chat_json_maps_429(monkeypatch):
    _patch_transport(monkeypatch, lambda request: httpx.Response(429, json={"error": "quota"}))
    with pytest.raises(HTTPException) as ei:
        await rag_module._gateway_chat_json(
            _settings(monkeypatch), "qwen3.6-35b", [{"role": "user", "content": "q"}]
        )
    assert ei.value.status_code == 429


async def test_gateway_embed_malformed_body_502(monkeypatch):
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json={"nope": True}))
    with pytest.raises(HTTPException) as ei:
        await rag_module._gateway_embed(_settings(monkeypatch), "bge-m3", "hi")
    assert ei.value.status_code == 502


async def test_open_chat_stream_preflight_error_raises(monkeypatch):
    _patch_transport(monkeypatch, lambda request: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(HTTPException) as ei:
        await rag_module._open_chat_stream(
            _settings(monkeypatch), "qwen3.6-35b", [{"role": "user", "content": "q"}]
        )
    # 401 from upstream => our RAG_API_KEY is bad => 502 to the client.
    assert ei.value.status_code == 502
