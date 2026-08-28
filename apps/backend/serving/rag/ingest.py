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

Ask whether the committed index is still current, without embedding anything::

    python -m serving.rag.ingest --check

The check exists because rebuilding needs a gateway, a key and minutes, while
deciding *whether* to rebuild needs only the chunker and milliseconds. Fusing
the two is what lets an index go stale unobserved: nothing cheap enough to run
routinely can answer the question.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from serving.rag.chunker import Chunk, chunk_markdown
from serving.rag.config import EMBEDDER_MODES, RagSettings, load_rag_settings
from serving.rag.embedder import build_ingest_embedder
from serving.rag.store import VectorStore


def _iter_markdown(corpus_dir: Path) -> list[Path]:
    return sorted(corpus_dir.rglob("*.md"))


def chunk_corpus(settings: RagSettings) -> list[Chunk]:
    """Turn the corpus into chunks — deterministic, no embedder, no network.

    This is the half of :func:`build_index` that decides *what* is retrievable,
    and it is the whole of what :func:`check_index` needs. Both call it so a
    freshness check can never disagree with the builder about chunk identity.
    """
    files = _iter_markdown(settings.corpus_dir)
    if not files:
        # The corpus is distribution content. A checkout with no overlay — which
        # is every clone of the neutral upstream — resolves this default to a
        # path that was never going to exist, so `make rag-ingest` failed with
        # a bare "no markdown files" naming a directory the reader has no
        # reason to have heard of. Say what to supply instead.
        raise FileNotFoundError(
            f"No markdown files under {settings.corpus_dir}.\n"
            "The default corpus lives in a distribution overlay, and this "
            "checkout has none. Point RAG_CORPUS_DIR at your own documentation:\n"
            "    RAG_CORPUS_DIR=path/to/docs make rag-ingest"
        )

    chunks: list[Chunk] = []
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
    return chunks


def check_index(settings: RagSettings) -> int:
    """Report whether the committed index still matches the corpus.

    Returns 0 when current, 1 when drifted, 2 when the index cannot be read.
    Never embeds and never calls out, so this runs anywhere the corpus and the
    index file are — no gateway, no API key, milliseconds. Rebuilding is the
    expensive half; deciding whether to rebuild must not be.

    Scope is chunk identity plus ``embedder_mode``. It deliberately does not
    compare ``embed_model``: the builder records the *embedder's* model, and
    the hash embedder hardcodes its own (``hash-256``) regardless of what was
    requested, so that comparison would report drift on every offline build.
    A false positive here costs a full rebuild, and the mode check already
    catches the case that matters — an index built offline while the settings
    ask for real gateway vectors.
    """
    expected = [(chunk.id, chunk.text) for chunk in chunk_corpus(settings)]
    try:
        data = json.loads(settings.index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"index unreadable at {settings.index_path}: {exc}", file=sys.stderr)
        return 2

    if data.get("embedder_mode") != settings.embedder_mode:
        print(
            f"embedder mode drift: index={data.get('embedder_mode')!r} "
            f"settings={settings.embedder_mode!r}",
            file=sys.stderr,
        )
        return 1

    actual = [(record["id"], record["text"]) for record in data.get("records", [])]
    if expected == actual:
        print(f"index current: {len(expected)} chunks")
        return 0

    exp, act = dict(expected), dict(actual)
    for label, ids in (
        ("missing from index", [k for k in exp if k not in act]),
        ("stale in index", [k for k in act if k not in exp]),
        ("text changed", [k for k in exp if k in act and exp[k] != act[k]]),
    ):
        if ids:
            shown = ", ".join(ids[:10])
            more = f" (+{len(ids) - 10} more)" if len(ids) > 10 else ""
            print(f"{label} ({len(ids)}): {shown}{more}", file=sys.stderr)
    return 1


def build_index(settings: RagSettings) -> VectorStore:
    """Chunk + embed the corpus and return a populated :class:`VectorStore`."""
    chunks = chunk_corpus(settings)

    embedder = build_ingest_embedder(
        mode=settings.embedder_mode,
        embed_model=settings.embed_model,
        gateway_base_url=settings.gateway_base_url,
        gateway_api_key=settings.gateway_api_key,
    )
    print(
        f"Embedding {len(chunks)} chunks from {len({chunk.source for chunk in chunks})} files "
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
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "verify the committed index against the corpus and exit "
            "(0 current, 1 drifted, 2 unreadable); never embeds, never calls out"
        ),
    )
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

    if args.check:
        return check_index(settings)

    store = build_index(settings)
    store.save(settings.index_path)
    print(
        f"Wrote {len(store.records)} chunks (dim={store.dim}, "
        f"model={store.embed_model}) to {settings.index_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
