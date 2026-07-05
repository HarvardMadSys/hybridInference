"""Split markdown docs into retrieval chunks.

Strategy: break each document at markdown headings so a chunk stays within one
logical section, then sub-split long sections on paragraph boundaries with a
small character overlap. Every chunk is prefixed with its heading breadcrumb
(e.g. ``Quick Start > Step 2: Configure Your Agent``) so the embedded text and
the context handed to the model both carry the section topic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    """One retrievable unit of text plus its provenance."""

    id: str
    text: str
    source: str  # file name, e.g. "quickstart.md"
    title: str  # heading breadcrumb, e.g. "Quick Start > Step 1: Get Your API Key"


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Return ``(breadcrumb, body)`` sections split on markdown headings.

    Headings inside fenced code blocks are ignored so a ``# comment`` line in a
    shell example doesn't start a new section.
    """
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    body_lines: list[str] = []
    current_breadcrumb = ""
    in_code = False

    def breadcrumb() -> str:
        return " > ".join(title for _, title in stack)

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            body_lines.append(line)
            continue

        match = None if in_code else _HEADING_RE.match(line)
        if match:
            if body_lines:
                sections.append((current_breadcrumb, "\n".join(body_lines)))
                body_lines = []
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_breadcrumb = breadcrumb()
        else:
            body_lines.append(line)

    if body_lines:
        sections.append((current_breadcrumb, "\n".join(body_lines)))
    return sections


def _split_text(body: str, max_chars: int, overlap: int) -> list[str]:
    """Pack paragraphs into pieces of at most ``max_chars`` with tail overlap."""
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]
    pieces: list[str] = []
    current = ""

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                pieces.append(current.strip())
                current = ""
            step = max(1, max_chars - overlap)
            for start in range(0, len(para), step):
                pieces.append(para[start : start + max_chars].strip())
            continue

        if current and len(current) + len(para) + 2 > max_chars:
            pieces.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{para}" if tail else para
        else:
            current = f"{current}\n\n{para}" if current else para

    if current.strip():
        pieces.append(current.strip())
    return pieces


def chunk_markdown(
    text: str,
    source: str,
    *,
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[Chunk]:
    """Chunk one markdown document into :class:`Chunk` objects."""
    chunks: list[Chunk] = []
    index = 0
    for breadcrumb, body in _split_by_headings(text):
        for piece in _split_text(body, max_chars, overlap):
            chunk_text = f"{breadcrumb}\n\n{piece}" if breadcrumb else piece
            chunks.append(
                Chunk(
                    id=f"{source}#{index}",
                    text=chunk_text.strip(),
                    source=source,
                    title=breadcrumb or source,
                )
            )
            index += 1
    return chunks
