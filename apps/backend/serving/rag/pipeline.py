"""Prompt assembly for the docs RAG assistant.

Retrieval (embedding the query and scanning the store) and generation (calling
the routing engine) happen in the serving endpoint, which owns the async
embedding adapter and router. This module holds the pure, easily-tested glue:
turning retrieved chunks into a grounded chat prompt and a sources payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.config.site_identity import get_site_identity

if TYPE_CHECKING:
    from serving.rag.store import Record

_SYSTEM_PROMPT_TEMPLATE = (
    "You are the {site_name} documentation assistant. Answer the user's "
    "question using ONLY the documentation context provided in the user "
    "message. Cite the sources you use with their bracketed numbers, e.g. [1]. "
    "If the context does not contain the answer, say so plainly and point the "
    "user to {docs_url} instead of guessing. Do not infer "
    "facts (such as which upstream provider backs a model) from configuration "
    "values or client-protocol settings; if the context does not state something "
    "directly, treat it as unknown. Keep answers concise and formatted in Markdown."
)


def system_prompt() -> str:
    """Render the docs-assistant system prompt for the active site identity."""
    identity = get_site_identity()
    return _SYSTEM_PROMPT_TEMPLATE.format(site_name=identity.name, docs_url=identity.docs_url)


# Cap history turns carried into each request so the augmented prompt stays
# bounded; only the most recent exchanges matter for a docs Q&A follow-up.
MAX_HISTORY_MESSAGES = 6


def format_context(results: list[tuple[Record, float]]) -> str:
    """Render retrieved chunks as a numbered context block."""
    blocks = []
    for number, (record, _score) in enumerate(results, start=1):
        blocks.append(f"[{number}] Source: {record.source} — {record.title}\n{record.text}")
    return "\n\n".join(blocks)


def build_messages(
    query: str,
    results: list[tuple[Record, float]],
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the chat messages: system prompt, trimmed history, grounded query."""
    context = format_context(results) or "(no documentation matched this question)"
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt()}]

    if history:
        for message in history[-MAX_HISTORY_MESSAGES:]:
            role = message.get("role")
            content = message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})

    user_content = f"Documentation context:\n\n{context}\n\n---\n\nQuestion: {query}"
    messages.append({"role": "user", "content": user_content})
    return messages


def sources_payload(results: list[tuple[Record, float]]) -> list[dict[str, Any]]:
    """Compact, client-facing description of the retrieved sources."""
    return [
        {
            "n": number,
            "id": record.id,
            "source": record.source,
            "title": record.title,
            "score": round(score, 4),
        }
        for number, (record, score) in enumerate(results, start=1)
    ]
