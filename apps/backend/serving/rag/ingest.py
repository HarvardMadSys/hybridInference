"""Build the RAG index from the docs corpus.

Reads every markdown file under the corpus directory, chunks it, embeds the
chunks (through the gateway by default, or the offline hash embedder), and
writes a JSON vector index.

Examples
--------
Build with the offline hash embedder (no external services)::

    RAG_EMBEDDER=hash python -m serving.rag.ingest

Build against a running gateway (real ``bge-m3`` embeddings)::

    RAG_EMBEDDER=gateway RAG_GATEWAY_BASE_URL=http://localhost:8000/v1 \
        RAG_GATEWAY_API_KEY=hyi-xxx python -m serving.rag.ingest
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from serving.rag.chunker import chunk_markdown
from serving.rag.config import EMBEDDER_MODES, RagSettings, load_rag_settings
from serving.rag.embedder import build_ingest_embedder
from serving.rag.store import VectorStore


def _iter_markdown(corpus_dir: Path) -> list[Path]:
    return sorted(corpus_dir.rglob("*.md"))


def build_index(settings: RagSettings) -> VectorStore:
    """Chunk + embed the corpus and return a populated :class:`VectorStore`."""
    files = _iter_markdown(settings.corpus_dir)
    if not files:
        raise FileNotFoundError(f"No markdown files found under {settings.corpus_dir}")

    chunks = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        chunks.extend(
            chunk_markdown(
                text,
                source=path.name,
                max_chars=settings.chunk_max_chars,
                overlap=settings.chunk_overlap_chars,
            )
        )

    embedder = build_ingest_embedder(
        mode=settings.embedder_mode,
        embed_model=settings.embed_model,
        gateway_base_url=settings.gateway_base_url,
        gateway_api_key=settings.gateway_api_key,
    )
    print(
        f"Embedding {len(chunks)} chunks from {len(files)} files "
        f"via '{embedder.mode}' (model={embedder.model})...",
        file=sys.stderr,
    )
    vectors = embedder.embed_texts([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError(f"Embedder returned {len(vectors)} vectors for {len(chunks)} chunks")
    dim = len(vectors[0]) if vectors else 0
    if dim == 0:
        raise ValueError("Embedder returned empty vectors (dim=0); refusing to write index")

    store = VectorStore(embed_model=embedder.model, embedder_mode=embedder.mode, dim=dim)
    for chunk, vector in zip(chunks, vectors, strict=True):
        store.add(chunk, vector)
    return store


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the index and write it to disk."""
    defaults = load_rag_settings()
    parser = argparse.ArgumentParser(description="Build the docs RAG index.")
    parser.add_argument("--corpus", type=Path, default=defaults.corpus_dir)
    parser.add_argument("--out", type=Path, default=defaults.index_path)
    parser.add_argument("--embedder", choices=EMBEDDER_MODES, default=defaults.embedder_mode)
    parser.add_argument("--embed-model", default=defaults.embed_model)
    args = parser.parse_args(argv)

    settings = RagSettings(
        corpus_dir=args.corpus,
        index_path=args.out,
        embedder_mode=args.embedder,
        embed_model=args.embed_model,
        chat_model=defaults.chat_model,
        top_k=defaults.top_k,
        chunk_max_chars=defaults.chunk_max_chars,
        chunk_overlap_chars=defaults.chunk_overlap_chars,
        max_tokens=defaults.max_tokens,
        temperature=defaults.temperature,
        api_base_url=defaults.api_base_url,
        api_key=defaults.api_key,
        gateway_base_url=defaults.gateway_base_url,
        gateway_api_key=defaults.gateway_api_key,
    )

    store = build_index(settings)
    store.save(settings.index_path)
    print(
        f"Wrote {len(store.records)} chunks (dim={store.dim}, "
        f"model={store.embed_model}) to {settings.index_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
