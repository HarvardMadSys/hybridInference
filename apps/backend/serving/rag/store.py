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


_REQUIRED_RECORD_FIELDS = ("id", "text", "source", "title", "embedding")


def _bad_component(embedding: list[Any]) -> str | None:
    """Name the first embedding component a cosine scan could not use."""
    for position, value in enumerate(embedding):
        # bool is an int in Python, and `[True, False, ...]` scores a confident
        # 1.0 against anything — a corrupt index that looks like a perfect hit
        # is worse than one that raises.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"non-numeric value at position {position}: {value!r}"
        if not math.isfinite(value):
            # NaN and Infinity survive json.loads, survive the scan, and come
            # out the other side as nan scores: no exception, silently wrong
            # ranking. This is the one shape that must never reach a query.
            return f"non-finite value at position {position}: {value!r}"
    return None


def index_document_problem(data: Any) -> str | None:
    """Say why ``data`` cannot be served as an index, or ``None`` if it can.

    One definition with two callers: :meth:`VectorStore.load` refuses to build a
    store from a document this rejects, and the ingest freshness check refuses
    to call such a document current. Keeping the definition here is the point —
    the two drifted apart twice already. First the check validated only the
    fields it compared, and passed indexes the loader could not read; then both
    checked the shape of an embedding but not its contents, and passed indexes
    that loaded fine and scored every query ``nan``.
    """
    if not isinstance(data, dict):
        return "expected a JSON object"

    dim = data.get("dim")
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        return f"declared dim is {dim!r}"

    records = data.get("records")
    if not isinstance(records, list):
        return "expected an object with a list of 'records'"

    for position, record in enumerate(records):
        if not isinstance(record, dict):
            return f"record at position {position} is not an object"
        missing = [field for field in _REQUIRED_RECORD_FIELDS if field not in record]
        if missing:
            return f"record at position {position} is missing {', '.join(missing)}"
        name = record["id"]
        embedding = record["embedding"]
        if not isinstance(embedding, list):
            return f"record {name!r} has a non-list embedding"
        if len(embedding) != dim:
            return f"record {name!r} has embedding dim {len(embedding)} != declared dim {dim}"
        problem = _bad_component(embedding)
        if problem:
            return f"record {name!r} has a {problem}"
    return None


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
        # Reject a corrupt index up front rather than letting it degrade cosine
        # search at query time — and reject it by the same definition the
        # freshness check uses, so an index called "current" is always one this
        # can load and score.
        problem = index_document_problem(data)
        if problem:
            raise ValueError(f"{path}: {problem}")
        store = cls(
            embed_model=data.get("embed_model", "unknown"),
            embedder_mode=data.get("embedder_mode", "gateway"),
            dim=int(data["dim"]),
        )
        store.records = [
            Record(
                id=item["id"],
                text=item["text"],
                source=item["source"],
                title=item["title"],
                embedding=list(item["embedding"]),
            )
            for item in data["records"]
        ]
        return store
