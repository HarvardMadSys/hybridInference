from __future__ import annotations

from serving.rag.chunker import chunk_markdown

SAMPLE = """# Quick Start

Intro paragraph.

## Step 1: Get Your API Key

Do the thing.

### Kilo Code

```bash
# this heading-looking comment must not split a section
echo hi
```

## Step 2: Configure

Another section body.
"""


def test_chunks_carry_heading_breadcrumb():
    chunks = chunk_markdown(SAMPLE, source="quickstart.md", max_chars=1000, overlap=50)
    assert chunks, "expected at least one chunk"
    titles = {c.title for c in chunks}
    assert any("Step 1: Get Your API Key" in t for t in titles)
    assert any("Step 1: Get Your API Key > Kilo Code" in t for t in titles)
    # Every chunk records its source file.
    assert {c.source for c in chunks} == {"quickstart.md"}
    # Ids are unique.
    assert len({c.id for c in chunks}) == len(chunks)


def test_code_fence_heading_does_not_split():
    chunks = chunk_markdown(SAMPLE, source="quickstart.md")
    # The `# this heading-looking comment` line lives inside a code fence, so it
    # must stay in the Kilo Code section rather than starting a new one.
    kilo = [c for c in chunks if c.title.endswith("Kilo Code")]
    assert kilo
    assert "echo hi" in kilo[0].text


def test_long_section_is_split_with_bounded_size():
    long_body = "# Big\n\n" + "\n\n".join(f"paragraph number {i} " * 20 for i in range(30))
    chunks = chunk_markdown(long_body, source="big.md", max_chars=400, overlap=40)
    assert len(chunks) > 1
    # Allow modest slack for the breadcrumb prefix that is prepended per chunk.
    assert all(len(c.text) <= 400 + 80 for c in chunks)
