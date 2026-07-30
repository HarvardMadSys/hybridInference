"""Documentation RAG assistant.

A small, self-contained retrieval-augmented-generation pipeline that answers
questions about a deployment using its own user docs as the knowledge base.

Design notes
------------
* **Corpus**: the active distribution overlay's user docs, resolved by
  ``rag.config``; a checkout with no overlay has none and answers 503 until
  ``RAG_CORPUS_DIR`` names one.
* **Embeddings & chat route through the gateway itself** — the ingest CLI calls
  the OpenAI-compatible ``/v1/embeddings`` endpoint; the serving endpoint embeds
  the query with the in-process embedding adapter and generates the answer with
  the routing engine (``RouteExecutor.stream_chat_completion``).
* **Vector store** is a plain JSON file searched with pure-Python cosine
  similarity. The corpus is tiny (a handful of docs), so no ANN index / numpy
  dependency is warranted for this prototype.
* A deterministic ``HashEmbedder`` fallback lets the pipeline run end-to-end
  (ingest, retrieve, test) without the GPU embedding box — intended for local
  dev and CI only, not production retrieval quality.
"""

from __future__ import annotations

from serving.rag.config import RagSettings, load_rag_settings
from serving.rag.store import Record, VectorStore

__all__ = ["RagSettings", "Record", "VectorStore", "load_rag_settings"]
