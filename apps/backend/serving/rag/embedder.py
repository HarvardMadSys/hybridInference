"""Embedders for the docs RAG pipeline.

Two implementations, selected by :attr:`RagSettings.embedder_mode`:

* ``GatewayHTTPEmbedder`` — routes embeddings through the OpenAI-compatible
  gateway (the production / requested path). Used by the ingest CLI.
* ``HashEmbedder`` — a deterministic, dependency-free hashing embedder for
  local dev and CI when the GPU embedding box isn't reachable. Retrieval
  quality is weak; it exists only so the pipeline is runnable end-to-end.

The serving endpoint embeds the *query* with whichever mode built the index:
gateway mode reuses the in-process embedding adapter (async), hash mode uses
``HashEmbedder`` directly. Keeping the query embedder consistent with the index
embedder is essential — vectors from different models aren't comparable.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return [tok for tok in _TOKEN_RE.findall(text.lower()) if len(tok) > 1]


def l2_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit length (unchanged if it's all zeros)."""
    norm = math.sqrt(sum(value * value for value in vec))
    if norm == 0.0:
        return vec
    return [value / norm for value in vec]


class Embedder(Protocol):
    """Synchronous embedder interface used by the ingest CLI."""

    mode: str
    model: str
    dim: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        ...


class HashEmbedder:
    """Deterministic hashing bag-of-words embedder (dev/CI fallback).

    Each token is hashed into a fixed-width vector (signed hashing trick) and
    the result is L2-normalized. Deterministic across processes because it uses
    ``hashlib`` rather than Python's salted ``hash()``.
    """

    mode = "hash"

    def __init__(self, dim: int = 256, model: str = "hash-256") -> None:
        self.dim = dim
        self.model = model

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            digest = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
            idx = digest % self.dim
            sign = 1.0 if (digest // self.dim) & 1 else -1.0
            vec[idx] += sign
        return l2_normalize(vec)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed each text into a normalized hashed vector."""
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query into a normalized hashed vector."""
        return self._embed_one(text)


class GatewayHTTPEmbedder:
    """Embed via the OpenAI-compatible gateway ``/v1/embeddings`` endpoint.

    Uses the ``openai`` client (already a project dependency) pointed at the
    gateway. The vector dimension is discovered from the first response.
    """

    mode = "gateway"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        batch_size: int = 64,
    ) -> None:
        from openai import OpenAI

        # The gateway rejects a blank key when auth is enabled; a placeholder is
        # fine when it's disabled (local dev), and a real key is supplied via env.
        self._client = OpenAI(base_url=base_url, api_key=api_key or "sk-rag-ingest")
        self.model = model
        self._batch_size = batch_size
        self.dim = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in batches through the gateway embeddings endpoint."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            response = self._client.embeddings.create(model=self.model, input=batch)
            for item in response.data:
                vectors.append(list(item.embedding))
        if vectors:
            self.dim = len(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query through the gateway embeddings endpoint."""
        return self.embed_texts([text])[0]


def build_ingest_embedder(
    *,
    mode: str,
    embed_model: str,
    gateway_base_url: str,
    gateway_api_key: str,
) -> Embedder:
    """Construct the embedder the ingest CLI should use."""
    if mode == "hash":
        return HashEmbedder()
    return GatewayHTTPEmbedder(gateway_base_url, gateway_api_key, embed_model)
