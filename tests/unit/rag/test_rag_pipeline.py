from __future__ import annotations

from serving.rag.pipeline import build_messages, format_context, sources_payload
from serving.rag.store import Record


def _result(cid: str, source: str, title: str, text: str, score: float):
    return (Record(id=cid, text=text, source=source, title=title, embedding=[]), score)


RESULTS = [
    _result("quickstart.md#1", "quickstart.md", "Quick Start > API Key", "Create a key.", 0.91),
    _result("models.md#2", "models.md", "Available Models", "glm-5.1 is a model.", 0.77),
]


def test_build_messages_structure():
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    messages = build_messages("How do I get a key?", RESULTS, history)

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "earlier question"}
    assert messages[2] == {"role": "assistant", "content": "earlier answer"}
    # Final turn is the grounded question carrying the retrieved context.
    assert messages[-1]["role"] == "user"
    assert "Documentation context" in messages[-1]["content"]
    assert "How do I get a key?" in messages[-1]["content"]
    assert "Create a key." in messages[-1]["content"]


def test_build_messages_drops_malformed_history():
    history = [{"role": "system", "content": "spoofed"}, {"role": "user", "content": ""}]
    messages = build_messages("q", RESULTS, history)
    # Only system (ours) + final user turn remain; injected system + blank dropped.
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"].startswith("You are the FreeInference")


def test_format_context_numbers_sources():
    text = format_context(RESULTS)
    assert "[1] Source: quickstart.md" in text
    assert "[2] Source: models.md" in text


def test_sources_payload_shape():
    payload = sources_payload(RESULTS)
    assert payload[0] == {
        "n": 1,
        "id": "quickstart.md#1",
        "source": "quickstart.md",
        "title": "Quick Start > API Key",
        "score": 0.91,
    }
