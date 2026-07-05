"""A tiny JSON-backed vector store with pure-Python cosine search.

The docs corpus is small (dozens of chunks), so a flat scan is more than fast
enough and keeps the prototype free of a numpy / FAISS dependency. The store
records which embedder built it (``embed_model`` + ``embedder_mode`` + ``dim``)
so the serving endpoint can embed queries with a matching embedder.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serving.rag.chunker import Chunk

INDEX_VERSION = 1


@dataclass
class Record:
    """One stored chunk plus its embedding."""

    id: str
    text: str
    source: str
    title: str
    embedding: list[float]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on degenerate input)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class VectorStore:
    """In-memory records with JSON persistence and cosine top-k search."""

    def __init__(self, embed_model: str, embedder_mode: str, dim: int) -> None:
        self.embed_model = embed_model
        self.embedder_mode = embedder_mode
        self.dim = dim
        self.records: list[Record] = []

    def add(self, chunk: Chunk, embedding: list[float]) -> None:
        """Append a chunk and its embedding to the store."""
        self.records.append(
            Record(
                id=chunk.id,
                text=chunk.text,
                source=chunk.source,
                title=chunk.title,
                embedding=embedding,
            )
        )

    def search(self, query_embedding: list[float], top_k: int) -> list[tuple[Record, float]]:
        """Return the ``top_k`` records most similar to ``query_embedding``."""
        scored = [
            (record, cosine_similarity(query_embedding, record.embedding))
            for record in self.records
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(0, top_k)]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the store (metadata + records) to a JSON-ready dict.

        Embeddings are rounded to 6 decimals: negligible for cosine similarity
        but roughly halves the on-disk index (which is committed as a prebuilt
        artifact so it ships in the image).
        """
        return {
            "version": INDEX_VERSION,
            "embed_model": self.embed_model,
            "embedder_mode": self.embedder_mode,
            "dim": self.dim,
            "records": [
                {
                    "id": record.id,
                    "text": record.text,
                    "source": record.source,
                    "title": record.title,
                    "embedding": [round(float(x), 6) for x in record.embedding],
                }
                for record in self.records
            ],
        }

    def save(self, path: str | Path) -> None:
        """Write the index to ``path`` as JSON via an atomic temp+rename.

        The rename means a concurrent reader never observes a half-written file
        while a re-ingest is in progress.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False)
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)  # don't leave a partial .tmp behind
            raise

    @classmethod
    def load(cls, path: str | Path) -> VectorStore:
        """Load an index previously written by :meth:`save`."""
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
        store = cls(
            embed_model=data.get("embed_model", "unknown"),
            embedder_mode=data.get("embedder_mode", "gateway"),
            dim=int(data.get("dim", 0)),
        )
        store.records = [
            Record(
                id=item["id"],
                text=item["text"],
                source=item["source"],
                title=item["title"],
                embedding=list(item["embedding"]),
            )
            for item in data.get("records", [])
        ]
        # Reject a corrupt/mismatched index up front rather than letting a wrong
        # dimension silently degrade cosine search at query time.
        for record in store.records:
            if len(record.embedding) != store.dim:
                raise ValueError(
                    f"index record {record.id!r} has embedding dim "
                    f"{len(record.embedding)} != declared dim {store.dim}"
                )
        return store
