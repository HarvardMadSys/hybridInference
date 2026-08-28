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
from itertools import zip_longest
from pathlib import Path

from serving.rag.chunker import Chunk, chunk_markdown
from serving.rag.config import EMBEDDER_MODES, RagSettings, load_rag_settings
from serving.rag.embedder import build_ingest_embedder, recorded_model_name
from serving.rag.store import VectorStore, index_document_problem


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
    Corpus-side faults raise through; ``main`` turns those into 2 as well, so
    that 1 always means a comparison actually happened.
    Never embeds and never calls out, so this runs anywhere the corpus and the
    index file are — no gateway, no API key, milliseconds. Rebuilding is the
    expensive half; deciding whether to rebuild must not be.

    Two questions, answered in order, because they have different answers:

    *Can this file be served at all?* An index a query could not be scored
    against is not "current" no matter what its chunks say — reporting 0 would
    leave the gateway holding it with nothing scheduled to replace it. The
    question is delegated to :func:`index_document_problem`, which
    :meth:`VectorStore.load` also asks, and a failure is 2.

    *Is it built from this corpus, by this embedder?* Chunk identity and the
    recorded ``embed_model``/``embedder_mode``. Vectors from different models
    are not comparable, so a model change is genuine drift and must rebuild.
    The expected model name comes from :func:`recorded_model_name` rather than
    from ``settings.embed_model`` directly — the hash embedder records its own
    name regardless of what was asked, and comparing the requested name would
    report drift on every offline build.
    """
    expected = [(chunk.id, chunk.text) for chunk in chunk_corpus(settings)]
    try:
        data = json.loads(settings.index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"index unreadable at {settings.index_path}: {exc}", file=sys.stderr)
        return 2

    # Shape faults are unreadable (2), not drifted (1). Answering them with
    # "drifted" sends a caller off to spend a rebuild on a file it could not
    # parse; answering them with "current" leaves the gateway serving one it
    # cannot score. The definition of servable lives with the loader that has
    # to honour it, so the two cannot drift apart again.
    problem = index_document_problem(data)
    if problem:
        print(f"index at {settings.index_path} cannot be served: {problem}", file=sys.stderr)
        return 2

    records = data["records"]

    if data.get("embedder_mode") != settings.embedder_mode:
        print(
            f"embedder mode drift: index={data.get('embedder_mode')!r} "
            f"settings={settings.embedder_mode!r}",
            file=sys.stderr,
        )
        return 1

    expected_model = recorded_model_name(
        mode=settings.embedder_mode, embed_model=settings.embed_model
    )
    if data.get("embed_model") != expected_model:
        # Vectors from different models are not comparable (see embedder.py),
        # so the index has to be rebuilt — this is drift, not a fault.
        print(
            f"embed model drift: index={data.get('embed_model')!r} "
            f"settings would record {expected_model!r}",
            file=sys.stderr,
        )
        return 1

    actual = [(record["id"], record["text"]) for record in records]
    if expected == actual:
        print(f"index current: {len(expected)} chunks")
        return 0

    exp, act = dict(expected), dict(actual)
    reported = False
    for label, ids in (
        ("missing from index", [k for k in exp if k not in act]),
        ("stale in index", [k for k in act if k not in exp]),
        ("text changed", [k for k in exp if k in act and exp[k] != act[k]]),
    ):
        if ids:
            shown = ", ".join(ids[:10])
            more = f" (+{len(ids) - 10} more)" if len(ids) > 10 else ""
            print(f"{label} ({len(ids)}): {shown}{more}", file=sys.stderr)
            reported = True

    if not reported:
        # The verdict is an ordered comparison; the explanation above is keyed
        # by id. They disagree when the same ids appear in a different order —
        # a corpus subdirectory moving, or two files sharing a basename, since
        # chunk ids are built from `path.name`. Saying nothing while exiting 1
        # would send someone hunting for content that did not change.
        first = next(
            index
            for index, (want, have) in enumerate(zip_longest(expected, actual, fillvalue=None))
            if want != have
        )
        print(
            f"same chunk ids in a different order: first difference at position {first} "
            f"(corpus has {expected[first][0] if first < len(expected) else '<end>'}, "
            f"index has {actual[first][0] if first < len(actual) else '<end>'})",
            file=sys.stderr,
        )
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
        f"Embedding {len(chunks)} chunks from {len(_iter_markdown(settings.corpus_dir))} files "
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
            "(0 current, 1 drifted, 2 not checked); never embeds, never calls out"
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
        try:
            return check_index(settings)
        except Exception as exc:
            # `check_index` raises through for corpus-side faults: a missing or
            # renamed RAG_CORPUS_DIR, a file that is not UTF-8. Letting those
            # reach the interpreter would exit 1, which callers read as "drifted,
            # go rebuild" — a confident, actionable and wrong instruction, and
            # the same conflation that made this workflow's CI warning lie.
            # Only a real comparison may return 1.
            print(f"index not checked: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    store = build_index(settings)
    store.save(settings.index_path)
    print(
        f"Wrote {len(store.records)} chunks (dim={store.dim}, "
        f"model={store.embed_model}) to {settings.index_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
