# Anthropic Messages API Compatibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept Anthropic Messages API on the northbound surface for any registered model. Native Claude models pass upstream with zero translation via a new `AnthropicAdapter`. Non-Claude models translate Anthropic↔OpenAI in-process and reuse existing OpenAI adapter code paths. The OpenAI northbound (`/v1/chat/completions`) is unchanged.

**Architecture:** Add a default `messages()` / `stream_messages()` method on `BaseAdapter` that translates Anthropic→OpenAI via a new pure-function module, calls the adapter's existing `chat_completion()` / `stream_chat_completion()`, and translates the response back. New `AnthropicAdapter` (`kind: anthropic`) overrides those methods to identity-passthrough to `api.anthropic.com`. New router file `anthropic_messages.py` mounts `/v1/messages` and `/anthropic/v1/messages` aliases, plus `/anthropic/v1/models`. Existing `anthropic_proxy.py` (claude_sub-only) is deleted.

**Tech Stack:** Python 3.12, FastAPI, aiohttp, pytest, `aioresponses` for upstream mocking, `httpx`-style FastAPI `TestClient`. No new third-party dependencies.

**Spec:** `docs/agents/specs/2026-05-02-anthropic-compatibility-design.md`

**Worktree setup before starting:** Per project conventions, create a git worktree on a feature branch before any code change:

```bash
git fetch origin dev
git worktree add -b jason/claude/anthropic-compat ../hybridInference-anthropic-compat origin/dev
cd ../hybridInference-anthropic-compat
uv sync
```

---

## File Structure

### New files

| Path | Purpose |
|---|---|
| `serving/adapters/anthropic_translator.py` | Pure functions + streaming class for Anthropic↔OpenAI translation. ~350 LOC. Plus the SSE-usage extraction helper moved out of `anthropic_proxy.py`. |
| `serving/adapters/anthropic.py` | `AnthropicAdapter` (`kind: anthropic`). Overrides `messages` / `stream_messages` for identity passthrough. Implements `chat_completion` / `stream_chat_completion` for OpenAI-format northbound to Anthropic upstream. ~250 LOC. |
| `serving/adapters/anthropic_aliases.py` | Static `ANTHROPIC_MODEL_ALIASES` dict. ~30 LOC. |
| `serving/servers/routers/anthropic_messages.py` | Replaces `anthropic_proxy.py`. Handles `/v1/messages` and `/anthropic/v1/messages` (same handler, two decorators). ~250 LOC. |
| `test/unit/adapters/test_anthropic_translator.py` | Unit tests for translator. |
| `test/unit/adapters/test_anthropic_adapter.py` | Unit tests for `AnthropicAdapter`. |
| `test/servers/test_anthropic_messages_router.py` | Router end-to-end tests with FastAPI `TestClient`. |

### Modified files

| Path | Change |
|---|---|
| `serving/adapters/base.py` | Add `native_format: str = "openai"` class attr. Add default `messages()` and `stream_messages()` methods on `BaseAdapter`. |
| `serving/adapters/__init__.py` | Export `AnthropicAdapter`. |
| `serving/servers/registry.py` | Add `if kind == "anthropic": return AnthropicAdapter(model_cfg)` branch. Update docstring. |
| `serving/servers/app.py` | Replace `anthropic_proxy.router` include with `anthropic_messages.router`. Add a path-keyed exception handler for `HTTPException` on Anthropic surfaces. |
| `serving/servers/routers/models.py` | Add `_is_anthropic_client(request)` helper. Branch `/v1/models` on it. Add `/anthropic/v1/models` route. |
| `config/models.yaml` | Retarget `claude-sonnet-4.6`, `claude-opus-4.6`, `claude-opus-4.7` from `kind: openai_compat` (cli-proxy) → `kind: anthropic` (direct). |
| `setupAnthropic.sh` | Drop `/anthropic` suffix expectation; document both supported paths. |

### Deleted files

| Path | Reason |
|---|---|
| `serving/servers/routers/anthropic_proxy.py` | claude_sub-specific identity proxy; replaced by `anthropic_messages.py`. claude_sub backend is out of scope for this spec (separate cleanup spec). |
| `test/unit/servers/test_anthropic_proxy.py` | Tests for deleted router. New tests live in `test/servers/test_anthropic_messages_router.py`. |
| `test/servers/test_anthropic_proxy.py` | Same as above. |

---

## Task 1: Translator — Request Side, Basic + Images + Tools

**Files:**
- Create: `serving/adapters/anthropic_translator.py`
- Create: `test/unit/adapters/test_anthropic_translator.py`

- [ ] **Step 1: Write failing tests for `anthropic_request_to_openai` covering text, multi-turn, system string, images, tools, tool_use, tool_result.**

```python
# test/unit/adapters/test_anthropic_translator.py
"""Unit tests for serving.adapters.anthropic_translator.

Covers Anthropic Messages format -> OpenAI Chat Completions format translation.
"""

from __future__ import annotations

from serving.adapters.anthropic_translator import anthropic_request_to_openai


def test_request_text_only_single_turn():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    messages, params = anthropic_request_to_openai(body)
    assert messages == [{"role": "user", "content": "Hello"}]
    assert params["max_tokens"] == 100


def test_request_system_string_prepended():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "system": "Be concise.",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0] == {"role": "system", "content": "Be concise."}
    assert messages[1] == {"role": "user", "content": "Hi"}


def test_request_multi_turn_preserved():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert len(messages) == 3
    assert messages[2]["content"] == "How are you?"


def test_request_image_data_url_block():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo=",
                    },
                },
            ],
        }],
    }
    messages, _ = anthropic_request_to_openai(body)
    parts = messages[0]["content"]
    assert parts[0] == {"type": "text", "text": "What is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_request_image_url_block():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "url", "url": "https://example.com/x.png"}},
            ],
        }],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0]["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/x.png"},
    }


def test_request_tools_translated():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "Get the weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]


def test_request_tool_use_and_tool_result_round_trip():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "get_weather",
                        "input": {"city": "SF"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "72F sunny",
                    }
                ],
            },
        ],
    }
    messages, _ = anthropic_request_to_openai(body)
    # Assistant tool_use becomes assistant message with tool_calls.
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "checking"
    assert assistant["tool_calls"][0] == {
        "id": "toolu_01",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
    }
    # tool_result becomes a tool-role message.
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "toolu_01"
    assert tool_msg["content"] == "72F sunny"
```

- [ ] **Step 2: Run tests; verify they fail because the module doesn't exist.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: All tests fail with `ModuleNotFoundError: No module named 'serving.adapters.anthropic_translator'`.

- [ ] **Step 3: Create `serving/adapters/anthropic_translator.py` with minimal request-side translator.**

```python
"""Anthropic Messages <-> OpenAI Chat Completions translator (reverse direction).

Forward direction (OpenAI -> Anthropic) lives in serving/adapters/claude_format.py.
This module handles the reverse direction needed when Anthropic-format requests
arrive on the northbound surface and must be dispatched to OpenAI-style backends.

Pure functions plus one stateful streaming translator. No I/O, no logging.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request translation: Anthropic -> OpenAI
# ---------------------------------------------------------------------------


def anthropic_request_to_openai(body: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Translate an Anthropic Messages API request to OpenAI Chat Completions.

    Returns:
        (openai_messages, openai_params) where openai_params holds non-message
        fields like max_tokens, temperature, tools, etc.
    """
    messages: list[dict[str, Any]] = []

    # System prompt -> system message prepended.
    system = body.get("system")
    system_text = _flatten_system(system)
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []):
        messages.extend(_translate_message(msg))

    params = _translate_params(body)
    return messages, params


def _flatten_system(system: Any) -> str | None:
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n\n".join(p for p in parts if p) or None
    return None


def _translate_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a single Anthropic message. May produce multiple OpenAI messages
    (tool_result blocks become separate role:"tool" messages)."""
    role = msg.get("role")
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": ""}]

    # Bucket blocks: text/image -> content parts, tool_use -> tool_calls,
    # tool_result -> separate role:"tool" messages.
    text_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    text_only_buffer: list[str] = []

    for block in content:
        btype = block.get("type")

        if btype == "text":
            text_parts.append({"type": "text", "text": block.get("text", "")})
            text_only_buffer.append(block.get("text", ""))

        elif btype == "image":
            url = _image_block_to_url(block.get("source", {}))
            if url is not None:
                text_parts.append({"type": "image_url", "image_url": {"url": url}})

        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

        elif btype == "tool_result":
            tool_results.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": _flatten_tool_result_content(block.get("content")),
            })

        # Unknown blocks ignored.

    out: list[dict[str, Any]] = []

    # Build the primary translated message (text + image parts + tool_calls).
    has_image = any(p.get("type") == "image_url" for p in text_parts)
    if text_parts or tool_calls:
        primary: dict[str, Any] = {"role": role}
        if has_image:
            primary["content"] = text_parts
        else:
            primary["content"] = "".join(text_only_buffer) if text_only_buffer else None
        if tool_calls:
            primary["tool_calls"] = tool_calls
        out.append(primary)

    # tool_result blocks become standalone tool-role messages, appended after.
    out.extend(tool_results)
    return out


def _image_block_to_url(source: dict[str, Any]) -> str | None:
    src_type = source.get("type")
    if src_type == "url":
        return source.get("url")
    if src_type == "base64":
        media_type = source.get("media_type", "application/octet-stream")
        data = source.get("data", "")
        return f"data:{media_type};base64,{data}"
    return None


def _flatten_tool_result_content(content: Any) -> str:
    """Anthropic tool_result content can be string or list of blocks; OpenAI tool
    messages take a string. Concat text blocks; ignore non-text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _translate_params(body: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if "max_tokens" in body:
        params["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        params["temperature"] = body["temperature"]
    if "top_p" in body:
        params["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        params["stop"] = body["stop_sequences"]
    if body.get("stream"):
        params["stream"] = True
    if body.get("tools"):
        params["tools"] = [_translate_tool(t) for t in body["tools"]]
    return params


def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }
```

- [ ] **Step 4: Run tests; verify all 7 pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic_translator.py test/unit/adapters/test_anthropic_translator.py
git commit -m "feat(adapters): Anthropic->OpenAI request translator (basic, images, tools)"
```

---

## Task 2: Translator — Request Side, Edge Cases

**Files:**
- Modify: `serving/adapters/anthropic_translator.py`
- Modify: `test/unit/adapters/test_anthropic_translator.py`

- [ ] **Step 1: Add failing tests for cache_control drop, thinking drop, system array, tool_choice mapping, metadata.user_id, stop_sequences.**

```python
# Append to test/unit/adapters/test_anthropic_translator.py

import logging


def test_request_cache_control_blocks_dropped(caplog):
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Hi", "cache_control": {"type": "ephemeral"}},
            ],
        }],
    }
    with caplog.at_level(logging.WARNING, logger="serving.adapters.anthropic_translator"):
        messages, _ = anthropic_request_to_openai(body)
    # cache_control silently stripped from the text block.
    assert messages[0]["content"] == "Hi"


def test_request_thinking_field_dropped():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "thinking": {"type": "enabled", "budget_tokens": 5000},
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert "thinking" not in params


def test_request_system_array_concatenated():
    body = {
        "model": "glm-4.7",
        "max_tokens": 100,
        "system": [
            {"type": "text", "text": "You are helpful."},
            {"type": "text", "text": "Be concise."},
        ],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    messages, _ = anthropic_request_to_openai(body)
    assert messages[0] == {"role": "system", "content": "You are helpful.\n\nBe concise."}


def test_request_tool_choice_auto():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "auto"


def test_request_tool_choice_any_becomes_required():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "f", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "any"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == "required"


def test_request_tool_choice_named_tool():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "get_weather", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "tool", "name": "get_weather"},
    }
    _, params = anthropic_request_to_openai(body)
    assert params["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }


def test_request_metadata_user_id_to_user_field():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
        "metadata": {"user_id": "u-abc"},
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["user"] == "u-abc"


def test_request_stop_sequences_renamed():
    body = {
        "model": "glm-4.7", "max_tokens": 100,
        "stop_sequences": ["END", "STOP"],
        "messages": [{"role": "user", "content": "Hi"}],
    }
    _, params = anthropic_request_to_openai(body)
    assert params["stop"] == ["END", "STOP"]
```

- [ ] **Step 2: Run tests; verify the new ones fail.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: failing tests for tool_choice, metadata.user_id (the rest may pass since no behavior contradicts them yet).

- [ ] **Step 3: Extend `_translate_params` and add `_translate_tool_choice` to `serving/adapters/anthropic_translator.py`.**

Replace `_translate_params` and append `_translate_tool_choice`:

```python
def _translate_params(body: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if "max_tokens" in body:
        params["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        params["temperature"] = body["temperature"]
    if "top_p" in body:
        params["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        params["stop"] = body["stop_sequences"]
    if body.get("stream"):
        params["stream"] = True
    if body.get("tools"):
        params["tools"] = [_translate_tool(t) for t in body["tools"]]
    if "tool_choice" in body:
        tc = _translate_tool_choice(body["tool_choice"])
        if tc is not None:
            params["tool_choice"] = tc
    metadata = body.get("metadata") or {}
    user_id = metadata.get("user_id")
    if user_id:
        params["user"] = user_id
    # thinking and other Anthropic-only top-level fields are silently dropped.
    return params


def _translate_tool_choice(tc: Any) -> Any:
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return None
```

`cache_control` already gets stripped because `_translate_message` only copies known fields from each block.

- [ ] **Step 4: Run tests; verify all pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic_translator.py test/unit/adapters/test_anthropic_translator.py
git commit -m "feat(adapters): translator handles cache_control/thinking/system-array/tool_choice"
```

---

## Task 3: Translator — Response Side

**Files:**
- Modify: `serving/adapters/anthropic_translator.py`
- Modify: `test/unit/adapters/test_anthropic_translator.py`

- [ ] **Step 1: Add failing tests for `openai_response_to_anthropic` covering text-only, tool_calls, mixed, finish_reason mapping, usage, cached_tokens.**

```python
# Append to test/unit/adapters/test_anthropic_translator.py

from serving.adapters.anthropic_translator import openai_response_to_anthropic


def test_response_text_only():
    resp = {
        "id": "chatcmpl-1",
        "model": "glm-4.7",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello there"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    out = openai_response_to_anthropic(resp, model="glm-4.7")
    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "glm-4.7"
    assert out["id"].startswith("msg_")
    assert out["content"] == [{"type": "text", "text": "Hello there"}]
    assert out["stop_reason"] == "end_turn"
    assert out["stop_sequence"] is None
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_response_tool_calls_only():
    resp = {
        "id": "chatcmpl-2",
        "model": "glm-4.7",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }
    out = openai_response_to_anthropic(resp, model="glm-4.7")
    assert out["content"] == [{
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {"city": "SF"},
    }]
    assert out["stop_reason"] == "tool_use"


def test_response_mixed_text_and_tool_calls():
    resp = {
        "id": "chatcmpl-3",
        "model": "glm-4.7",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    out = openai_response_to_anthropic(resp, model="glm-4.7")
    types = [b["type"] for b in out["content"]]
    assert types == ["text", "tool_use"]


def test_response_finish_reason_map():
    cases = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
        "function_call": "end_turn",  # legacy fallback
    }
    for fr, expected in cases.items():
        resp = {
            "id": "x", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": fr}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        assert openai_response_to_anthropic(resp, model="m")["stop_reason"] == expected


def test_response_cached_tokens_mapped():
    resp = {
        "id": "x", "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["usage"]["cache_read_input_tokens"] == 80


def test_response_id_preserves_msg_prefix_if_present():
    resp = {
        "id": "msg_already",
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "y"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    out = openai_response_to_anthropic(resp, model="m")
    assert out["id"] == "msg_already"
```

- [ ] **Step 2: Run tests; verify failures (function not defined).**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 6 new tests fail with `ImportError`.

- [ ] **Step 3: Implement `openai_response_to_anthropic` in `serving/adapters/anthropic_translator.py`.**

Append to the module:

```python
# ---------------------------------------------------------------------------
# Response translation: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def openai_response_to_anthropic(resp: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Translate an OpenAI ChatCompletion response to Anthropic Messages format."""
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []

    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": tool_input,
        })

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _FINISH_REASON_MAP.get(finish, "end_turn")

    raw_id = resp.get("id") or ""
    msg_id = raw_id if raw_id.startswith("msg_") else f"msg_{raw_id}" if raw_id else "msg_"

    usage_in = resp.get("usage") or {}
    anthropic_usage: dict[str, int] = {
        "input_tokens": int(usage_in.get("prompt_tokens", 0)),
        "output_tokens": int(usage_in.get("completion_tokens", 0)),
    }
    cached = (usage_in.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached:
        anthropic_usage["cache_read_input_tokens"] = int(cached)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage,
    }
```

- [ ] **Step 4: Run tests; verify all pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 20 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic_translator.py test/unit/adapters/test_anthropic_translator.py
git commit -m "feat(adapters): OpenAI->Anthropic response translator"
```

---

## Task 4: Translator — Streaming

**Files:**
- Modify: `serving/adapters/anthropic_translator.py`
- Modify: `test/unit/adapters/test_anthropic_translator.py`

- [ ] **Step 1: Add failing tests for `OpenAIToAnthropicStreamTranslator` covering text-only, tool_use, fragmented JSON, multi-tool, finish_reason buffering, usage in final chunk.**

```python
# Append to test/unit/adapters/test_anthropic_translator.py

import json as _json

from serving.adapters.anthropic_translator import OpenAIToAnthropicStreamTranslator


def _events(byte_iter):
    """Parse our emitted SSE bytes into a list of (event_name, parsed_json)."""
    text = b"".join(byte_iter).decode("utf-8")
    out = []
    cur_event = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            cur_event = line[len("event: "):].strip()
        elif line.startswith("data: "):
            payload = line[len("data: "):].strip()
            if payload and payload != "[DONE]":
                out.append((cur_event, _json.loads(payload)))
            cur_event = None
    return out


def _openai_chunk(delta_obj, finish_reason=None, usage=None):
    """Build one OpenAI SSE chunk byte-string."""
    obj = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "model": "m",
        "choices": [{"index": 0, "delta": delta_obj, "finish_reason": finish_reason}],
    }
    if usage is not None:
        obj["usage"] = usage
    return ("data: " + _json.dumps(obj) + "\n\n").encode("utf-8")


def test_stream_text_only():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    chunks = []
    for c in (
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"content": "Hi"}),
        _openai_chunk({"content": " there"}),
        _openai_chunk({}, finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}),
    ):
        chunks.append(b"".join(t.feed(c)))
    chunks.append(b"".join(t.finalize()))
    events = _events(chunks)
    names = [e[0] for e in events]
    assert names[0] == "message_start"
    assert "content_block_start" in names
    assert any(e[0] == "content_block_delta" and e[1]["delta"]["text"] == "Hi" for e in events)
    assert any(e[0] == "content_block_delta" and e[1]["delta"]["text"] == " there" for e in events)
    assert names[-1] == "message_stop"
    msg_delta = next(e for e in events if e[0] == "message_delta")
    assert msg_delta[1]["delta"]["stop_reason"] == "end_turn"
    assert msg_delta[1]["usage"]["output_tokens"] == 2


def test_stream_tool_call_fragmented_json():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        }]}),
        _openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"ci'}}]}),
        _openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ty":"SF"}'}}]}),
        _openai_chunk({}, finish_reason="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    starts = [e for e in events if e[0] == "content_block_start"]
    assert any(s[1]["content_block"]["type"] == "tool_use" for s in starts)
    deltas = [e for e in events if e[0] == "content_block_delta"]
    json_pieces = [e[1]["delta"]["partial_json"] for e in deltas if e[1]["delta"]["type"] == "input_json_delta"]
    assert "".join(json_pieces) == '{"city":"SF"}'
    msg_delta = next(e for e in events if e[0] == "message_delta")
    assert msg_delta[1]["delta"]["stop_reason"] == "tool_use"


def test_stream_multi_tool_calls():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    parts = [
        _openai_chunk({"role": "assistant"}),
        _openai_chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]}),
        _openai_chunk({"tool_calls": [{"index": 1, "id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]}),
        _openai_chunk({}, finish_reason="tool_calls"),
    ]
    out = b""
    for c in parts:
        out += b"".join(t.feed(c))
    out += b"".join(t.finalize())
    events = _events([out])
    starts = [e for e in events if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "tool_use"]
    assert [s[1]["content_block"]["name"] for s in starts] == ["a", "b"]
    indices = [e[1]["index"] for e in events if e[0] == "content_block_stop"]
    assert sorted(set(indices)) == [0, 1]


def test_stream_empty_response():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    out += b"".join(t.feed(_openai_chunk({}, finish_reason="stop", usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1})))
    out += b"".join(t.finalize())
    events = _events([out])
    names = [e[0] for e in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"


def test_stream_done_sentinel_ignored():
    t = OpenAIToAnthropicStreamTranslator(model="m")
    out = b""
    out += b"".join(t.feed(_openai_chunk({"role": "assistant"})))
    out += b"".join(t.feed(_openai_chunk({"content": "x"})))
    out += b"".join(t.feed(b"data: [DONE]\n\n"))
    out += b"".join(t.finalize())
    events = _events([out])
    assert any(e[0] == "message_stop" for e in events)
```

- [ ] **Step 2: Run tests; verify failure (class not defined).**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 5 new failures with `ImportError: cannot import OpenAIToAnthropicStreamTranslator`.

- [ ] **Step 3: Implement the streaming translator.**

Append to `serving/adapters/anthropic_translator.py`:

```python
# ---------------------------------------------------------------------------
# Streaming translation: OpenAI SSE -> Anthropic SSE
# ---------------------------------------------------------------------------


class OpenAIToAnthropicStreamTranslator:
    """Stateful translator: feed OpenAI SSE chunks, emit Anthropic SSE bytes.

    OpenAI emits per-chunk JSON deltas. Anthropic emits a richer event sequence:
    message_start -> content_block_start -> content_block_delta...
    -> content_block_stop -> ... -> message_delta -> message_stop.

    Caller pattern:
        t = OpenAIToAnthropicStreamTranslator(model="m")
        async for chunk in upstream_openai_stream:
            for ant in t.feed(chunk):
                yield ant
        for ant in t.finalize():
            yield ant
    """

    def __init__(self, *, model: str) -> None:
        self.model = model
        self._started = False
        self._closed = False
        self._current_text_index: int | None = None
        self._tool_blocks: dict[int, dict[str, Any]] = {}  # openai-tool-index -> {anthropic_index, name, id}
        self._next_index = 0
        self._finish_reason: str | None = None
        self._usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._buffer = b""

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        self._buffer += chunk
        while b"\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split(b"\n\n", 1)
            yield from self._handle_frame(frame)

    def finalize(self) -> Iterator[bytes]:
        if self._closed:
            return
        if not self._started:
            yield from self._emit_message_start()
        # Close any open content block.
        if self._current_text_index is not None:
            yield self._sse("content_block_stop", {"type": "content_block_stop", "index": self._current_text_index})
            self._current_text_index = None
        for tb in list(self._tool_blocks.values()):
            yield self._sse("content_block_stop", {"type": "content_block_stop", "index": tb["anthropic_index"]})
        self._tool_blocks.clear()
        # Emit message_delta with stop_reason + final usage.
        stop_reason = _FINISH_REASON_MAP.get(self._finish_reason or "stop", "end_turn")
        yield self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self._usage["output_tokens"]},
        })
        yield self._sse("message_stop", {"type": "message_stop"})
        self._closed = True

    # -- internals --

    def _handle_frame(self, frame: bytes) -> Iterator[bytes]:
        for line in frame.split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            payload = line[len(b"data: "):].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            yield from self._handle_chunk(obj)

    def _handle_chunk(self, obj: dict[str, Any]) -> Iterator[bytes]:
        usage = obj.get("usage")
        if usage:
            self._usage["input_tokens"] = int(usage.get("prompt_tokens", self._usage["input_tokens"]))
            self._usage["output_tokens"] = int(usage.get("completion_tokens", self._usage["output_tokens"]))

        choices = obj.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        if not self._started:
            yield from self._emit_message_start()

        text = delta.get("content")
        if text:
            yield from self._emit_text(text)

        for tc in delta.get("tool_calls") or []:
            yield from self._emit_tool_call_delta(tc)

        fr = choice.get("finish_reason")
        if fr:
            self._finish_reason = fr

    def _emit_message_start(self) -> Iterator[bytes]:
        self._started = True
        yield self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_stream",
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": dict(self._usage),
            },
        })

    def _emit_text(self, text: str) -> Iterator[bytes]:
        if self._current_text_index is None:
            # Close any open tool blocks first? No - text and tool deltas can interleave;
            # OpenAI rarely interleaves them in practice. Open new text block.
            self._current_text_index = self._next_index
            self._next_index += 1
            yield self._sse("content_block_start", {
                "type": "content_block_start",
                "index": self._current_text_index,
                "content_block": {"type": "text", "text": ""},
            })
        yield self._sse("content_block_delta", {
            "type": "content_block_delta",
            "index": self._current_text_index,
            "delta": {"type": "text_delta", "text": text},
        })

    def _emit_tool_call_delta(self, tc: dict[str, Any]) -> Iterator[bytes]:
        idx = tc.get("index", 0)
        fn = tc.get("function") or {}
        # Close text block if open before opening a tool block (matches Anthropic ordering convention).
        if idx not in self._tool_blocks and self._current_text_index is not None:
            yield self._sse("content_block_stop", {"type": "content_block_stop", "index": self._current_text_index})
            self._current_text_index = None

        if idx not in self._tool_blocks:
            anthropic_index = self._next_index
            self._next_index += 1
            self._tool_blocks[idx] = {
                "anthropic_index": anthropic_index,
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
            }
            yield self._sse("content_block_start", {
                "type": "content_block_start",
                "index": anthropic_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": {},
                },
            })

        anthropic_index = self._tool_blocks[idx]["anthropic_index"]
        partial = fn.get("arguments")
        if partial:
            yield self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": anthropic_index,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            })

    def _sse(self, event: str, payload: dict[str, Any]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
```

- [ ] **Step 4: Run tests; verify all pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 25 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic_translator.py test/unit/adapters/test_anthropic_translator.py
git commit -m "feat(adapters): streaming OpenAI->Anthropic SSE translator"
```

---

## Task 5: SSE Usage Helper Migration

**Files:**
- Modify: `serving/adapters/anthropic_translator.py`
- Modify: `test/unit/adapters/test_anthropic_translator.py`

The existing `_extract_usage_from_sse` helper lives in `serving/servers/routers/anthropic_proxy.py:128`. Move it into the translator module so both the new `AnthropicAdapter.stream_messages` and the new router can reuse it before the old file is deleted in Task 16.

- [ ] **Step 1: Add failing test.**

```python
# Append to test/unit/adapters/test_anthropic_translator.py

from serving.adapters.anthropic_translator import extract_anthropic_usage_from_sse


def test_extract_usage_from_anthropic_sse():
    chunk = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"x","usage":{"input_tokens":42,'
        b'"cache_creation_input_tokens":3,"cache_read_input_tokens":7}}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":99}}\n\n'
    )
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    extract_anthropic_usage_from_sse(chunk, usage)
    assert usage == {"input_tokens": 42, "output_tokens": 99,
                     "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7}


def test_extract_usage_silently_ignores_garbage():
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    extract_anthropic_usage_from_sse(b"garbage\n\n", usage)
    assert usage == {"input_tokens": 0, "output_tokens": 0,
                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
```

- [ ] **Step 2: Run tests; verify failures.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 2 new failures with `ImportError`.

- [ ] **Step 3: Add helper to `serving/adapters/anthropic_translator.py`.**

Append:

```python
# ---------------------------------------------------------------------------
# Anthropic-native SSE usage extraction (moved from anthropic_proxy.py)
# ---------------------------------------------------------------------------


def extract_anthropic_usage_from_sse(raw: bytes, usage: dict[str, int]) -> None:
    """Best-effort parse of Anthropic SSE frames to extract usage counters.

    Mutates ``usage`` in-place. Never raises -- failures are silently ignored
    so the forwarded stream is never affected.

    Anthropic sends cumulative values, not per-event deltas:
      message_start.message.usage.input_tokens     -- total input tokens
      message_delta.usage.output_tokens             -- total output tokens so far
    """
    try:
        text = raw.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            event_type = obj.get("type", "")
            if event_type == "message_start":
                msg_usage = (obj.get("message") or {}).get("usage") or {}
                if "input_tokens" in msg_usage:
                    usage["input_tokens"] = int(msg_usage["input_tokens"])
                if "cache_creation_input_tokens" in msg_usage:
                    usage["cache_creation_input_tokens"] = int(msg_usage["cache_creation_input_tokens"])
                if "cache_read_input_tokens" in msg_usage:
                    usage["cache_read_input_tokens"] = int(msg_usage["cache_read_input_tokens"])
            elif event_type == "message_delta":
                delta_usage = obj.get("usage") or {}
                if "output_tokens" in delta_usage:
                    usage["output_tokens"] = int(delta_usage["output_tokens"])
    except Exception:
        pass
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_translator.py -v`
Expected: 27 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic_translator.py test/unit/adapters/test_anthropic_translator.py
git commit -m "feat(adapters): move SSE usage extractor into translator module"
```

---

## Task 6: BaseAdapter — `native_format` + Default `messages` / `stream_messages`

**Files:**
- Modify: `serving/adapters/base.py`
- Create: `test/unit/adapters/test_base_anthropic.py`

- [ ] **Step 1: Write failing tests using a minimal mock subclass.**

```python
# test/unit/adapters/test_base_anthropic.py
"""Unit tests for BaseAdapter default messages() / stream_messages() impls."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from serving.adapters.base import BaseAdapter, ModelConfig


class _FakeOpenAIAdapter(BaseAdapter):
    """Captures translated OpenAI requests; returns canned responses."""

    last_messages: list[dict[str, Any]] | None = None
    last_params: dict[str, Any] | None = None

    async def chat_completion(self, messages, **params):
        self.last_messages = messages
        self.last_params = params
        return {
            "id": "chatcmpl-fake",
            "model": "fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def stream_chat_completion(self, messages, **params) -> AsyncGenerator[str, None]:
        self.last_messages = messages
        self.last_params = params
        # Three OpenAI SSE chunks.
        yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
        yield 'data: {"id":"x","object":"chat.completion.chunk","model":"fake","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'


def _cfg() -> ModelConfig:
    return ModelConfig(id="fake", name="Fake", provider="fake", base_url="http://x")


def test_native_format_default():
    a = _FakeOpenAIAdapter(_cfg())
    assert a.native_format == "openai"


@pytest.mark.asyncio
async def test_default_messages_translates_through_chat_completion():
    a = _FakeOpenAIAdapter(_cfg())
    body = {"model": "fake", "max_tokens": 10, "messages": [{"role": "user", "content": "Hello"}]}
    out = await a.messages(body, request_id="req_1")
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "Hi"
    # Verify translation reached the inner chat_completion.
    assert a.last_messages == [{"role": "user", "content": "Hello"}]
    assert a.last_params["max_tokens"] == 10


@pytest.mark.asyncio
async def test_default_stream_messages_translates_sse():
    a = _FakeOpenAIAdapter(_cfg())
    body = {"model": "fake", "max_tokens": 10, "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
    out = b""
    async for chunk in a.stream_messages(body, request_id="req_1"):
        out += chunk
    assert b"event: message_start" in out
    assert b"event: message_stop" in out
    assert b'"text":"Hi"' in out
```

- [ ] **Step 2: Run tests; verify failures (`messages` / `stream_messages` undefined).**

Run: `uv run pytest test/unit/adapters/test_base_anthropic.py -v`
Expected: failures with `AttributeError`.

- [ ] **Step 3: Modify `serving/adapters/base.py`. Add `native_format` class attr and the two default methods on `BaseAdapter`.**

After the existing `__init__` and abstract method declarations, add:

```python
    # Format the adapter speaks natively. Anthropic-native adapters override
    # messages()/stream_messages() to identity-passthrough; OpenAI-native ones
    # rely on the default impls below which translate Anthropic <-> OpenAI.
    native_format: str = "openai"

    async def messages(
        self,
        body: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Anthropic Messages API non-streaming. Returns Anthropic-format dict.

        Default impl translates Anthropic -> OpenAI, calls self.chat_completion,
        translates OpenAI -> Anthropic.
        """
        from serving.adapters.anthropic_translator import (
            anthropic_request_to_openai,
            openai_response_to_anthropic,
        )

        oai_messages, oai_params = anthropic_request_to_openai(body)
        oai_resp = await self.chat_completion(oai_messages, **oai_params)
        return openai_response_to_anthropic(oai_resp, model=body.get("model", ""))

    async def stream_messages(
        self,
        body: dict[str, Any],
        *,
        request_id: str,
    ) -> AsyncGenerator[bytes, None]:
        """Anthropic Messages API streaming. Yields raw Anthropic SSE bytes."""
        from serving.adapters.anthropic_translator import (
            OpenAIToAnthropicStreamTranslator,
            anthropic_request_to_openai,
        )

        oai_messages, oai_params = anthropic_request_to_openai(body)
        oai_params["stream"] = True
        translator = OpenAIToAnthropicStreamTranslator(model=body.get("model", ""))
        async for openai_chunk in self.stream_chat_completion(oai_messages, **oai_params):
            data = openai_chunk.encode("utf-8") if isinstance(openai_chunk, str) else openai_chunk
            for ant in translator.feed(data):
                yield ant
        for ant in translator.finalize():
            yield ant
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/unit/adapters/test_base_anthropic.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/base.py test/unit/adapters/test_base_anthropic.py
git commit -m "feat(adapters): BaseAdapter.native_format + default messages/stream_messages"
```

---

## Task 7: AnthropicAdapter — OpenAI-Format Northbound (chat_completion / stream_chat_completion)

**Files:**
- Create: `serving/adapters/anthropic.py`
- Create: `test/unit/adapters/test_anthropic_adapter.py`

This task covers the path: **OpenAI-format client request → freeinference → Anthropic upstream**. Reuses the forward translator already in `serving/adapters/claude_format.py`, which is exactly what the Vertex `ClaudeAdapter` uses. Effectively a sibling adapter with Anthropic-direct auth/path.

- [ ] **Step 1: Write failing tests using `aioresponses` to mock the upstream.**

```python
# test/unit/adapters/test_anthropic_adapter.py
"""Unit tests for AnthropicAdapter."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses

from serving.adapters.anthropic import AnthropicAdapter
from serving.adapters.base import ModelConfig


def _cfg(**overrides) -> ModelConfig:
    base = dict(
        id="claude-opus-4.7",
        name="Claude Opus 4.7",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        provider_model_id="claude-opus-4-7",
        max_output_length=1024,
        supports_tools=True,
        supported_params=["temperature", "max_tokens", "top_p", "stream", "tools", "tool_choice"],
    )
    base.update(overrides)
    return ModelConfig(**base)


@pytest.mark.asyncio
async def test_chat_completion_translates_to_anthropic_upstream():
    adapter = AnthropicAdapter(_cfg())
    upstream_resp = {
        "id": "msg_01abc",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hello there"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    with aioresponses() as m:
        m.post("https://api.anthropic.com/v1/messages", payload=upstream_resp)
        out = await adapter.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=128,
            temperature=0.5,
        )
        # OpenAI-format response shape.
        assert out["choices"][0]["message"]["content"] == "Hello there"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["usage"]["prompt_tokens"] == 10
        assert out["usage"]["completion_tokens"] == 3

        # Inspect the request that was sent upstream.
        req = m.requests[("POST", _yarl("https://api.anthropic.com/v1/messages"))][0]
        sent = json.loads(req.kwargs["data"])
        assert sent["model"] == "claude-opus-4-7"
        assert sent["max_tokens"] == 128
        assert sent["messages"][0]["role"] == "user"
        # Anthropic upstream auth header.
        assert req.kwargs["headers"]["x-api-key"] == "sk-ant-test"
        assert req.kwargs["headers"]["anthropic-version"]


@pytest.mark.asyncio
async def test_stream_chat_completion_translates_anthropic_sse_to_openai_chunks():
    adapter = AnthropicAdapter(_cfg())
    upstream_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"msg_x","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n'
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    with aioresponses() as m:
        m.post("https://api.anthropic.com/v1/messages", body=upstream_sse,
               content_type="text/event-stream")
        chunks = []
        async for c in adapter.stream_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=64,
            stream=True,
        ):
            chunks.append(c)
        joined = "".join(c if isinstance(c, str) else c.decode("utf-8") for c in chunks)
        assert '"content":"Hi"' in joined or '"content": "Hi"' in joined
        assert "[DONE]" in joined


def _yarl(u: str):
    """aioresponses keys requests by yarl.URL; helper to produce one."""
    from yarl import URL
    return URL(u)
```

- [ ] **Step 2: Run tests; verify failures (module not found).**

Run: `uv run pytest test/unit/adapters/test_anthropic_adapter.py -v`
Expected: `ModuleNotFoundError: No module named 'serving.adapters.anthropic'`.

- [ ] **Step 3: Create `serving/adapters/anthropic.py` with the OpenAI-northbound paths.**

Anthropic-northbound (`messages` / `stream_messages` overrides) lands in **Task 8**. For now stub them as `NotImplementedError` so subclasses still work.

```python
"""Direct-Anthropic adapter (kind: anthropic).

Talks api.anthropic.com using x-api-key auth (no Vertex, no OAuth subscription).
Provides:
  - messages() / stream_messages()      Anthropic-format identity passthrough
  - chat_completion() / stream_chat_completion()
                                        OpenAI-format northbound -> translates
                                        OpenAI -> Anthropic upstream using the
                                        existing claude_format helpers.

Sibling of the Vertex ClaudeAdapter (serving/adapters/claude.py); same forward
translation, different upstream auth/path.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import aiohttp

from serving.http import AsyncHTTPClient
from serving.stream import done_sentinel, make_final_usage_chunk
from serving.utils.logging import get_logger

from .base import BaseAdapter
from .claude_format import (
    ToolCallAccumulator,
    build_final_usage,
    convert_messages,
    convert_tool_choice,
    convert_tools,
    extract_system,
    handle_stream_event,
    map_stop_reason,
    parse_response_content,
    parse_usage,
)

logger = get_logger(__name__)


class AnthropicAdapter(BaseAdapter):
    """Direct Anthropic Messages API adapter."""

    native_format = "anthropic"

    ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_BASE = "https://api.anthropic.com"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return (self.config.base_url or self.DEFAULT_BASE).rstrip("/")

    def _upstream_url(self) -> str:
        return f"{self._base_url()}/v1/messages"

    def _upstream_model(self) -> str:
        return self.config.provider_model_id or self.config.id

    def _upstream_headers(self, *, streaming: bool) -> dict[str, str]:
        headers = {
            "x-api-key": self.config.api_key or "",
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if streaming:
            headers["accept"] = "text/event-stream"
            headers["accept-encoding"] = "identity"
        else:
            headers["accept"] = "application/json"
        return headers

    # ------------------------------------------------------------------
    # OpenAI-format northbound -> Anthropic upstream
    # ------------------------------------------------------------------

    async def chat_completion(self, messages: list[dict[str, Any]], **params: Any) -> dict[str, Any]:
        validated = self.validate_params(params)

        payload: dict[str, Any] = {
            "model": self._upstream_model(),
            "messages": convert_messages(messages),
            "max_tokens": validated.get("max_tokens", self.config.max_output_length),
        }
        sys_text = params.get("system") or extract_system(messages)
        if sys_text:
            payload["system"] = sys_text

        for k in ("temperature", "top_p"):
            if k in validated:
                payload[k] = validated[k]
        if "top_k" in validated and "top_k" in self.config.supported_params:
            payload["top_k"] = validated["top_k"]
        if "stop" in validated:
            payload["stop_sequences"] = validated["stop"]

        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = convert_tools(params["tools"])
            if params.get("tool_choice") is not None:
                convert_tool_choice(params["tool_choice"], payload)

        http = AsyncHTTPClient.shared()
        upstream = await http.json_post_with_retry(
            self._upstream_url(),
            json=payload,
            headers=self._upstream_headers(streaming=False),
            timeout=None,
            retries=2,
        )

        # Translate Anthropic response -> OpenAI Chat Completion shape.
        text, tool_calls = parse_response_content(upstream.get("content", []))
        usage = parse_usage(upstream.get("usage", {}))
        stop_reason = map_stop_reason(upstream.get("stop_reason", "end_turn"))
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {
            "id": upstream.get("id", f"chatcmpl-{int(time.time()*1000)}"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "message": message, "finish_reason": stop_reason}],
            "usage": usage.to_dict(),
        }

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncGenerator[str, None]:
        validated = self.validate_params(params)
        payload: dict[str, Any] = {
            "model": self._upstream_model(),
            "messages": convert_messages(messages),
            "max_tokens": validated.get("max_tokens", self.config.max_output_length),
            "stream": True,
        }
        sys_text = params.get("system") or extract_system(messages)
        if sys_text:
            payload["system"] = sys_text
        for k in ("temperature", "top_p"):
            if k in validated:
                payload[k] = validated[k]
        if "stop" in validated:
            payload["stop_sequences"] = validated["stop"]
        if params.get("tools") and self.config.supports_tools:
            payload["tools"] = convert_tools(params["tools"])
            if params.get("tool_choice") is not None:
                convert_tool_choice(params["tool_choice"], payload)

        http = AsyncHTTPClient.shared()
        session = await http._ensure_session()
        timeout = aiohttp.ClientTimeout(total=None)
        accum = ToolCallAccumulator()
        usage_state: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        chat_id = f"chatcmpl-{int(time.time()*1000)}"
        created = int(time.time())

        async with session.post(
            self._upstream_url(),
            json=payload,
            headers=self._upstream_headers(streaming=True),
            timeout=timeout,
        ) as resp:
            buf = b""
            async for raw in resp.content.iter_any():
                buf += raw
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    for line in frame.split(b"\n"):
                        if not line.startswith(b"data: "):
                            continue
                        payload_bytes = line[len(b"data: "):].strip()
                        if not payload_bytes:
                            continue
                        try:
                            evt = json.loads(payload_bytes)
                        except json.JSONDecodeError:
                            continue
                        for chunk_str in handle_stream_event(
                            evt, accum, chat_id=chat_id, model=self.config.id, created=created
                        ):
                            yield chunk_str
                        # Track usage cumulatively from message_start / message_delta.
                        et = evt.get("type")
                        if et == "message_start":
                            mu = (evt.get("message") or {}).get("usage") or {}
                            usage_state["input_tokens"] = int(mu.get("input_tokens", 0))
                        elif et == "message_delta":
                            du = evt.get("usage") or {}
                            if "output_tokens" in du:
                                usage_state["output_tokens"] = int(du["output_tokens"])

        yield make_final_usage_chunk(
            chat_id=chat_id,
            model=self.config.id,
            created=created,
            usage=build_final_usage(usage_state),
        )
        yield done_sentinel()

    # ------------------------------------------------------------------
    # Anthropic-format northbound (Task 8 implements; placeholder here)
    # ------------------------------------------------------------------

    async def messages(self, body: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        raise NotImplementedError("Anthropic-format passthrough lands in Task 8")

    async def stream_messages(
        self, body: dict[str, Any], *, request_id: str
    ) -> AsyncIterator[bytes]:
        raise NotImplementedError("Anthropic-format passthrough lands in Task 8")
        yield b""  # pragma: no cover - keeps async-generator type
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_adapter.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic.py test/unit/adapters/test_anthropic_adapter.py
git commit -m "feat(adapters): AnthropicAdapter chat_completion + stream_chat_completion"
```

---

## Task 8: AnthropicAdapter — Anthropic-Format Identity Passthrough

**Files:**
- Modify: `serving/adapters/anthropic.py`
- Modify: `test/unit/adapters/test_anthropic_adapter.py`

- [ ] **Step 1: Add failing tests for `messages()` and `stream_messages()` identity passthrough.**

```python
# Append to test/unit/adapters/test_anthropic_adapter.py

import asyncio


@pytest.mark.asyncio
async def test_messages_identity_passthrough():
    adapter = AnthropicAdapter(_cfg())
    body_in = {
        "model": "claude-opus-4.7",
        "max_tokens": 200,
        "messages": [{"role": "user", "content": "Hi"}],
        "system": "Be helpful.",
    }
    upstream_resp = {
        "id": "msg_passthrough",
        "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 2},
    }
    with aioresponses() as m:
        m.post("https://api.anthropic.com/v1/messages", payload=upstream_resp)
        out = await adapter.messages(body_in, request_id="req_pass")
        assert out == upstream_resp

        req = m.requests[("POST", _yarl("https://api.anthropic.com/v1/messages"))][0]
        sent = json.loads(req.kwargs["data"])
        # Model rewritten to provider_model_id; rest verbatim.
        assert sent["model"] == "claude-opus-4-7"
        assert sent["messages"] == [{"role": "user", "content": "Hi"}]
        assert sent["system"] == "Be helpful."
        assert sent["max_tokens"] == 200
        assert req.kwargs["headers"]["x-api-key"] == "sk-ant-test"


@pytest.mark.asyncio
async def test_stream_messages_identity_passthrough_records_usage():
    adapter = AnthropicAdapter(_cfg())
    upstream_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"msg_y","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    body = {
        "model": "claude-opus-4.7", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    with aioresponses() as m:
        m.post("https://api.anthropic.com/v1/messages", body=upstream_sse,
               content_type="text/event-stream")
        out = b""
        async for c in adapter.stream_messages(body, request_id="req_s"):
            out += c
        # Bytes are streamed verbatim from upstream.
        assert out == upstream_sse
        # Adapter exposes accumulated usage post-stream for DB logging.
        assert adapter.last_stream_usage == {
            "input_tokens": 7, "output_tokens": 4,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
```

- [ ] **Step 2: Run tests; verify failures.**

Run: `uv run pytest test/unit/adapters/test_anthropic_adapter.py -v`
Expected: failures from `NotImplementedError` and missing `last_stream_usage`.

- [ ] **Step 3: Replace the `NotImplementedError` stubs with full implementations and add a `last_stream_usage` attribute.**

In `serving/adapters/anthropic.py`, replace the placeholder `messages` / `stream_messages` and add an `__init__` extension:

```python
    def __init__(self, config):
        super().__init__(config)
        # Populated after stream_messages() finishes; consumed by the router for
        # DB logging.
        self.last_stream_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    async def messages(self, body: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        from serving.http import AsyncHTTPClient
        forward = dict(body)
        forward["model"] = self._upstream_model()
        http = AsyncHTTPClient.shared()
        return await http.json_post_with_retry(
            self._upstream_url(),
            json=forward,
            headers=self._upstream_headers(streaming=False),
            timeout=None,
            retries=2,
        )

    async def stream_messages(
        self, body: dict[str, Any], *, request_id: str
    ) -> AsyncIterator[bytes]:
        from serving.adapters.anthropic_translator import extract_anthropic_usage_from_sse

        forward = dict(body)
        forward["model"] = self._upstream_model()
        forward["stream"] = True

        http = AsyncHTTPClient.shared()
        session = await http._ensure_session()
        timeout = aiohttp.ClientTimeout(total=None)

        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

        async with session.post(
            self._upstream_url(),
            json=forward,
            headers=self._upstream_headers(streaming=True),
            timeout=timeout,
        ) as resp:
            async for chunk in resp.content.iter_any():
                extract_anthropic_usage_from_sse(chunk, usage)
                yield chunk

        self.last_stream_usage = usage
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_adapter.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic.py test/unit/adapters/test_anthropic_adapter.py
git commit -m "feat(adapters): AnthropicAdapter Anthropic-format identity passthrough"
```

---

## Task 9: Adapter Registry — Register `kind: anthropic`

**Files:**
- Modify: `serving/adapters/__init__.py`
- Modify: `serving/servers/registry.py`

- [ ] **Step 1: Write a smoke test that the registry returns an `AnthropicAdapter` for `kind: anthropic`.**

Append to `test/unit/adapters/test_anthropic_adapter.py`:

```python
def test_registry_returns_anthropic_adapter_for_kind_anthropic():
    from serving.servers.registry import build_adapter_for_route

    cfg = {
        "id": "claude-opus-4.7",
        "name": "Claude Opus 4.7",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-test",
        "provider_model_id": "claude-opus-4-7",
    }
    adapter = build_adapter_for_route("anthropic", cfg)
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.native_format == "anthropic"
```

(Adjust `build_adapter_for_route` import name to whatever the existing registry exposes — see `serving/servers/registry.py:99-155`.)

- [ ] **Step 2: Run test; verify failure (`Unknown adapter kind: anthropic`).**

Run: `uv run pytest test/unit/adapters/test_anthropic_adapter.py::test_registry_returns_anthropic_adapter_for_kind_anthropic -v`
Expected: `ValueError: Unknown adapter kind: anthropic`.

- [ ] **Step 3: Register `AnthropicAdapter` in `serving/adapters/__init__.py`.**

```python
# serving/adapters/__init__.py
from .anthropic import AnthropicAdapter  # noqa: F401
from .base import BaseAdapter, ModelConfig, UsageInfo
from .claude import ClaudeAdapter
from .claude_sub import ClaudeSubscriptionAdapter
from .codex_sub import CodexSubscriptionAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter

__all__ = [
    "AnthropicAdapter",
    "BaseAdapter",
    "ClaudeAdapter",
    "ClaudeSubscriptionAdapter",
    "CodexSubscriptionAdapter",
    "GeminiAdapter",
    "ModelConfig",
    "OpenAICompatAdapter",
    "UsageInfo",
]
```

- [ ] **Step 4: Add the `kind: anthropic` branch in `serving/servers/registry.py` (the function around line 99-155 that maps kinds → adapters).**

After the `claude_sub` branch, add:

```python
    if kind == "anthropic":
        return AnthropicAdapter(model_cfg)
```

Update the docstring kind list to include `"anthropic"`. Add the import at the top of the module:

```python
from serving.adapters import AnthropicAdapter  # alongside existing adapter imports
```

- [ ] **Step 5: Run test; verify pass + run wider adapter suite to ensure no regression.**

Run:
```
uv run pytest test/unit/adapters/ -v
```
Expected: All adapter unit tests green.

- [ ] **Step 6: Commit.**

```bash
git add serving/adapters/__init__.py serving/servers/registry.py test/unit/adapters/test_anthropic_adapter.py
git commit -m "feat(registry): register kind: anthropic -> AnthropicAdapter"
```

---

## Task 10: Anthropic Model Aliases

**Files:**
- Create: `serving/adapters/anthropic_aliases.py`
- Create: `test/unit/adapters/test_anthropic_aliases.py`

- [ ] **Step 1: Write failing tests.**

```python
# test/unit/adapters/test_anthropic_aliases.py
"""Unit tests for the Anthropic public-display-id alias map."""

from __future__ import annotations

from serving.adapters.anthropic_aliases import ANTHROPIC_MODEL_ALIASES, resolve_anthropic_alias


def test_known_alias_resolves():
    assert resolve_anthropic_alias("claude-3-5-sonnet-latest") == "claude-sonnet-4.6"
    assert resolve_anthropic_alias("claude-3-5-sonnet-20241022") == "claude-sonnet-4.6"


def test_unknown_returns_input_unchanged():
    assert resolve_anthropic_alias("nonexistent-model") == "nonexistent-model"


def test_already_canonical_passes_through():
    assert resolve_anthropic_alias("claude-opus-4.7") == "claude-opus-4.7"


def test_alias_map_has_required_entries():
    required_aliases = {
        "claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022",
        "claude-3-opus-latest", "claude-3-opus-20240229",
    }
    assert required_aliases.issubset(ANTHROPIC_MODEL_ALIASES.keys())
```

- [ ] **Step 2: Run tests; verify failure.**

Run: `uv run pytest test/unit/adapters/test_anthropic_aliases.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create the alias module.**

```python
# serving/adapters/anthropic_aliases.py
"""Alias map: Anthropic's public display IDs -> our registry IDs.

Why: clients calling the Anthropic Messages API typically pass model names like
``claude-3-5-sonnet-latest`` or dated aliases (``claude-3-5-sonnet-20241022``).
We register models under our own canonical IDs (e.g. ``claude-sonnet-4.6``).
This table maps the former to the latter so existing Anthropic SDK code works
without reconfiguration.
"""

from __future__ import annotations

ANTHROPIC_MODEL_ALIASES: dict[str, str] = {
    # Sonnet family
    "claude-3-5-sonnet-latest": "claude-sonnet-4.6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4.6",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4.6",
    "claude-sonnet-4-5": "claude-sonnet-4.6",
    "claude-sonnet-4-6": "claude-sonnet-4.6",

    # Opus family
    "claude-3-opus-latest": "claude-opus-4.7",
    "claude-3-opus-20240229": "claude-opus-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-7": "claude-opus-4.7",
}


def resolve_anthropic_alias(model_id: str) -> str:
    """Return the registry ID for ``model_id``, or ``model_id`` if unknown.

    Unknown IDs pass through; the router then attempts a direct registry lookup
    and returns 404 if that also fails.
    """
    return ANTHROPIC_MODEL_ALIASES.get(model_id, model_id)
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/unit/adapters/test_anthropic_aliases.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit.**

```bash
git add serving/adapters/anthropic_aliases.py test/unit/adapters/test_anthropic_aliases.py
git commit -m "feat(adapters): Anthropic public-id alias map"
```

---

## Task 11: `/v1/models` Anthropic-Format Detection + `/anthropic/v1/models`

**Files:**
- Modify: `serving/servers/routers/models.py`
- Create / extend: `test/unit/servers/test_models_router_anthropic.py`

- [ ] **Step 1: Inspect the existing `models.py` router to find the OpenAI-format formatter and reuse the same registry source.**

Run: `uv run python -c "import serving.servers.routers.models as m; help(m)" | head -60`

(Read the source; confirm where the OpenAI-format model list is built.)

- [ ] **Step 2: Write failing tests.**

```python
# test/unit/servers/test_models_router_anthropic.py
"""Tests for /v1/models Anthropic-format detection and /anthropic/v1/models."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_v1_models_returns_openai_format_by_default(test_app):
    r = TestClient(test_app).get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    # OpenAI shape sample
    assert body["data"][0].get("object") == "model"


def test_v1_models_with_anthropic_version_header_returns_anthropic_format(test_app):
    r = TestClient(test_app).get("/v1/models", headers={"anthropic-version": "2023-06-01"})
    assert r.status_code == 200
    body = r.json()
    assert "data" in body
    item = body["data"][0]
    assert item.get("type") == "model"
    assert "display_name" in item
    assert "created_at" in item


def test_v1_models_with_claude_cli_user_agent_returns_anthropic_format(test_app):
    r = TestClient(test_app).get("/v1/models", headers={"user-agent": "claude-cli/2.0"})
    assert r.status_code == 200
    assert r.json()["data"][0]["type"] == "model"


def test_anthropic_v1_models_always_anthropic_format(test_app):
    r = TestClient(test_app).get("/anthropic/v1/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["type"] == "model"
```

`test_app` should be a pytest fixture that builds the FastAPI app with at least one registered model. If the suite already has such a fixture (look in `test/conftest.py`), reuse it; otherwise add a minimal one in this test file's `conftest.py`.

- [ ] **Step 3: Run tests; verify failures.**

Run: `uv run pytest test/unit/servers/test_models_router_anthropic.py -v`
Expected: failures (path or format mismatch).

- [ ] **Step 4: Modify `serving/servers/routers/models.py`.**

Add helpers and a new route:

```python
# At top of serving/servers/routers/models.py, alongside existing imports:
from fastapi import Request


def _is_anthropic_client(request: Request) -> bool:
    if request.headers.get("anthropic-version"):
        return True
    ua = (request.headers.get("user-agent") or "").lower()
    return ua.startswith(("anthropic-", "claude-cli", "claude-sdk"))


def _format_anthropic_model_list(routes: dict) -> dict:
    """Build the Anthropic GET /v1/models response shape from our registry."""
    data = []
    for model_id, route in routes.items():
        # Filter out role-gated entries the same way the OpenAI formatter does.
        # (Mirror existing behavior; if the OpenAI path applies a filter, apply
        # the same one here.)
        data.append({
            "type": "model",
            "id": model_id,
            "display_name": getattr(route, "name", None) or model_id,
            "created_at": "1970-01-01T00:00:00Z",
        })
    first_id = data[0]["id"] if data else None
    last_id = data[-1]["id"] if data else None
    return {"data": data, "has_more": False, "first_id": first_id, "last_id": last_id}
```

Modify the existing `GET /v1/models` handler signature to accept `request: Request` and branch:

```python
@router.get("/v1/models")
async def list_models(request: Request, ..., router_exec=Depends(get_router), ...):
    if _is_anthropic_client(request):
        return JSONResponse(_format_anthropic_model_list(router_exec.routes))
    # ... existing OpenAI-format body unchanged ...
```

Add a new route:

```python
@router.get("/anthropic/v1/models")
async def list_models_anthropic(router_exec=Depends(get_router)):
    return JSONResponse(_format_anthropic_model_list(router_exec.routes))
```

Match whatever role/eligibility filtering the existing handler does; do not loosen it.

- [ ] **Step 5: Run tests; verify pass.**

Run: `uv run pytest test/unit/servers/test_models_router_anthropic.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit.**

```bash
git add serving/servers/routers/models.py test/unit/servers/test_models_router_anthropic.py
git commit -m "feat(routers): Anthropic-format /v1/models detection + /anthropic/v1/models"
```

---

## Task 12: New Router — Non-Streaming Dispatch

**Files:**
- Create: `serving/servers/routers/anthropic_messages.py`
- Create: `test/servers/test_anthropic_messages_router.py`
- Modify: `test/servers/conftest.py` — add `anthropic_test_app` / `anthropic_test_client` fixtures

The existing `test_client` fixture in `test/servers/conftest.py` is async (`httpx.AsyncClient`) and uses `mock_router` with only a `MagicMock` adapter. We need a separate fixture that registers **real** `AnthropicAdapter` (`kind: anthropic`) and `OpenAICompatAdapter` (`kind: zai`) instances so dispatch actually exercises the new code paths.

Constants used in tests below:
- `NATIVE_MODEL = "claude-opus-4.7"` — registered as `kind: anthropic`, base URL `https://api.anthropic.com`.
- `OPENAI_MODEL = "glm-4.7"` — registered as `kind: zai`, base URL `https://example-zai.test`.
- `ZAI_UPSTREAM_URL = "https://example-zai.test/chat/completions"` — concrete upstream URL the zai adapter will hit (Zhipu adapter uses `chat_path: /chat/completions`).

- [ ] **Step 1: Add fixtures to `test/servers/conftest.py`.**

Append to the file:

```python
# Anthropic compat router fixtures ------------------------------------------

ANTHROPIC_TEST_API_KEY = "hyi-anthropic-compat-test"


@pytest_asyncio.fixture
async def anthropic_compat_router():
    """RouteExecutor with one anthropic-kind and one zai-kind real adapter."""
    from routing.executor import RouteExecutor
    from serving.adapters import AnthropicAdapter, OpenAICompatAdapter
    from serving.adapters.base import ModelConfig

    re = RouteExecutor()

    anthropic_cfg = ModelConfig(
        id="claude-opus-4.7", name="Claude Opus 4.7",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        provider_model_id="claude-opus-4-7",
        max_output_length=1024, supports_tools=True,
        supported_params=["temperature", "max_tokens", "top_p", "stream", "tools", "tool_choice"],
        pricing={"prompt": "5.0", "completion": "25.0", "image": "0", "request": "0",
                 "input_cache_reads": "0.5", "input_cache_writes": "6.25"},
    )
    zai_cfg = ModelConfig(
        id="glm-4.7", name="GLM-4.7",
        provider="zai",
        base_url="https://example-zai.test",
        api_key="zai-test",
        chat_path="/chat/completions",
        max_output_length=1024, supports_tools=True,
        supported_params=["temperature", "max_tokens", "stop", "stream", "tools", "tool_choice"],
        pricing={"prompt": "0.6", "completion": "2.2", "image": "0", "request": "0",
                 "input_cache_reads": "0.11", "input_cache_writes": "0"},
    )
    re.register_route("claude-opus-4.7", [(AnthropicAdapter(anthropic_cfg), 1.0)])
    re.register_route("glm-4.7", [(OpenAICompatAdapter(zai_cfg), 1.0)])
    return re


@pytest_asyncio.fixture
async def anthropic_app_services(anthropic_compat_router, mock_db_logger, mock_rate_limiter):
    from serving.servers.deps import AppServices
    return AppServices(
        router=anthropic_compat_router,
        db_logger=mock_db_logger,
        rate_limiter=mock_rate_limiter,
        routing_manager=None,
    )


@pytest_asyncio.fixture
async def anthropic_test_app(anthropic_app_services, monkeypatch):
    """Test FastAPI app with the anthropic_messages router mounted and a stubbed
    verify_api_key dep that accepts ANTHROPIC_TEST_API_KEY.
    """
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Header, HTTPException

    @asynccontextmanager
    async def lifespan(app):
        app.state.services = anthropic_app_services
        yield

    app = FastAPI(title="Anthropic Compat Test App", lifespan=lifespan)
    app.state.services = anthropic_app_services

    # Stub auth: accept the test API key via x-api-key OR Authorization: Bearer.
    from serving.servers.auth import verify_api_key

    async def fake_verify(authorization: str | None = Header(None),
                          x_api_key: str | None = Header(None, alias="X-API-Key")):
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[len("Bearer "):]
        elif x_api_key:
            token = x_api_key
        if token != ANTHROPIC_TEST_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"authenticated": True, "user_id": "test-user", "role": "internal"}

    app.dependency_overrides[verify_api_key] = fake_verify

    # Stub concurrency dep to a no-op.
    from serving.servers.concurrency import enforce_user_concurrency
    app.dependency_overrides[enforce_user_concurrency] = lambda: None

    # Wire the router-under-test plus the exception handler we'll add in Task 15.
    from serving.servers.routers import anthropic_messages
    app.include_router(anthropic_messages.router)
    return app


@pytest_asyncio.fixture
async def anthropic_test_client(anthropic_test_app):
    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=anthropic_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

- [ ] **Step 2: Write failing tests.**

```python
# test/servers/test_anthropic_messages_router.py
"""End-to-end tests for the Anthropic Messages router."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses
from yarl import URL

from test.servers.conftest import ANTHROPIC_TEST_API_KEY


NATIVE_MODEL = "claude-opus-4.7"
OPENAI_MODEL = "glm-4.7"
ANTHROPIC_UPSTREAM = "https://api.anthropic.com/v1/messages"
ZAI_UPSTREAM = "https://example-zai.test/chat/completions"


def _auth():
    return {"x-api-key": ANTHROPIC_TEST_API_KEY}


@pytest.mark.asyncio
async def test_v1_messages_native_identity_passthrough(anthropic_test_client):
    upstream_resp = {
        "id": "msg_native", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "Hi"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    body = {"model": NATIVE_MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    with aioresponses() as m:
        m.post(ANTHROPIC_UPSTREAM, payload=upstream_resp)
        r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
        assert r.status_code == 200
        assert r.json() == upstream_resp


@pytest.mark.asyncio
async def test_anthropic_v1_messages_alias_reaches_same_handler(anthropic_test_client):
    upstream_resp = {
        "id": "msg_alias", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "via alias"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body = {"model": NATIVE_MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    with aioresponses() as m:
        m.post(ANTHROPIC_UPSTREAM, payload=upstream_resp)
        r = await anthropic_test_client.post("/anthropic/v1/messages", json=body, headers=_auth())
        assert r.status_code == 200
        assert r.json()["content"][0]["text"] == "via alias"


@pytest.mark.asyncio
async def test_openai_backend_translated(anthropic_test_client):
    """Anthropic-format request to glm-4.7 (zai) -> translation."""
    openai_resp = {
        "id": "chatcmpl-1", "object": "chat.completion",
        "model": OPENAI_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Translated reply"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    }
    body = {"model": OPENAI_MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    with aioresponses() as m:
        m.post(ZAI_UPSTREAM, payload=openai_resp)
        r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
        assert r.status_code == 200
        out = r.json()
        assert out["type"] == "message"
        assert out["content"][0]["text"] == "Translated reply"
        assert out["usage"]["input_tokens"] == 9


@pytest.mark.asyncio
async def test_alias_model_id_resolves(anthropic_test_client):
    upstream_resp = {
        "id": "msg_a", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body = {"model": "claude-3-opus-latest", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    with aioresponses() as m:
        m.post(ANTHROPIC_UPSTREAM, payload=upstream_resp)
        r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_unknown_model_returns_anthropic_format_404(anthropic_test_client):
    body = {"model": "no-such-model", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
    assert r.status_code == 404
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "not_found_error"
```

- [ ] **Step 2: Run tests; verify failures.**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v`
Expected: failures (route 404 or wiring missing).

- [ ] **Step 3: Create `serving/servers/routers/anthropic_messages.py` (non-streaming half only; streaming lands in Task 13).**

```python
"""Anthropic Messages API northbound router.

Serves both /v1/messages and /anthropic/v1/messages via two decorators on the
same handler. Replaces the old anthropic_proxy.py (claude_sub-only).

Field translation lives in adapter.messages() / adapter.stream_messages();
this router only owns:
  - auth, rate limiting, concurrency
  - model resolution (with Anthropic alias map)
  - field sanitization for OpenAI-backed dispatch
  - error formatting (Anthropic shape)
  - DB logging + metrics
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from serving.adapters.anthropic_aliases import resolve_anthropic_alias
from serving.config.settings import has_role
from serving.observability.metrics import (
    API_MODEL_REQUESTS,
    normalize_model_label,
    normalize_provider_label,
)
from serving.servers.auth import verify_api_key
from serving.servers.concurrency import enforce_user_concurrency
from serving.servers.deps import get_db_logger, get_rate_limiter, get_router
from serving.utils.logging import get_logger
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from routing.executor import RouteExecutor
    from serving.storage.database import DatabaseLogger

logger = get_logger(__name__)
router = APIRouter()


# ------------------------------------------------------------------
# Anthropic-format error
# ------------------------------------------------------------------


_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
}


def _anthropic_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error",
                 "error": {"type": _ERROR_TYPE_BY_STATUS.get(status, "api_error"),
                           "message": message}},
    )


# ------------------------------------------------------------------
# Model resolution
# ------------------------------------------------------------------


def _resolve(model_id: str, router_exec, user_ctx: dict | None):
    """Return (canonical_model_id, route, adapter)."""
    canonical = resolve_anthropic_alias(model_id)
    route = router_exec.routes.get(canonical)
    if route is None:
        raise HTTPException(404, f"Model '{model_id}' not found")
    required = route.required_role or ("admin" if route.admin_only else "free")
    user_role = (user_ctx or {}).get("role", "free")
    if not has_role(user_role, required):
        raise HTTPException(404, f"Model '{model_id}' not found")
    if not route.adapters:
        raise HTTPException(404, f"Model '{model_id}' has no adapters")
    adapter, _ = route.adapters[0]
    return canonical, route, adapter


# ------------------------------------------------------------------
# Field sanitization for OpenAI-backed dispatch
# ------------------------------------------------------------------


def _sanitize_for_openai_backend(body: dict[str, Any]) -> list[str]:
    """Strip Anthropic-only block fields the OpenAI translator can't represent.

    Returns a list of dropped-field names for warning logging.
    """
    dropped: set[str] = set()
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    block.pop("cache_control")
                    dropped.add("cache_control")
    if "thinking" in body:
        body.pop("thinking")
        dropped.add("thinking")
    return sorted(dropped)


# ------------------------------------------------------------------
# DB logging
# ------------------------------------------------------------------


def _schedule_db_log(
    db_logger,
    *,
    request_id: str,
    model_id: str,
    provider: str,
    usage: dict[str, int],
    latency_ms: int,
    status_code: int,
    pricing: dict[str, str],
    metadata: dict[str, Any],
) -> None:
    async def _log() -> None:
        try:
            await db_logger.log_request(
                request_id=request_id,
                model_id=model_id,
                provider=provider,
                prompt=[],
                response=None,
                usage={
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
                latency_ms=latency_ms,
                status_code=status_code,
                params={"surface": "anthropic_messages"},
                metadata=metadata,
                pricing=pricing,
            )
        except Exception:
            logger.debug(f"Background DB log failed for {request_id}", exc_info=True)

    asyncio.create_task(_log())  # noqa: RUF006


# ------------------------------------------------------------------
# Token-pre-check helper for the rate limiter
# ------------------------------------------------------------------


def _flatten_anthropic_for_token_estimate(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return an OpenAI-shaped messages list for tiktoken pre-estimation.
    Approximation only -- final usage is recorded from the response."""
    out = []
    sys = body.get("system")
    if isinstance(sys, str) and sys:
        out.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        out.append({"role": "system", "content": "\n\n".join(
            b.get("text", "") for b in sys if isinstance(b, dict) and b.get("type") == "text")})
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
        elif isinstance(content, list):
            text = "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
            out.append({"role": msg["role"], "content": text})
    return out


# ------------------------------------------------------------------
# Route handler (non-streaming half; streaming added in Task 13)
# ------------------------------------------------------------------


@router.post("/v1/messages", response_model=None)
@router.post("/anthropic/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user_ctx: dict = Depends(verify_api_key),
    router_exec=Depends(get_router),
    rate_limiter=Depends(get_rate_limiter),
    db_logger=Depends(get_db_logger),
    _conc=Depends(enforce_user_concurrency),
):
    request_id = f"amsg_{int(time.time()*1_000_000)}"
    start = time.time()

    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Invalid JSON in request body")

    model_id = body.get("model")
    if not model_id:
        return _anthropic_error(400, "Missing required field: model")
    if "messages" not in body:
        return _anthropic_error(400, "Missing required field: messages")
    if "max_tokens" not in body:
        return _anthropic_error(400, "Missing required field: max_tokens")

    try:
        canonical, route, adapter = _resolve(model_id, router_exec, user_ctx)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))

    body["model"] = canonical

    if rate_limiter:
        oai_msgs = _flatten_anthropic_for_token_estimate(body)
        ok, meta = await rate_limiter.acquire_tokens(
            model_id=canonical,
            messages=oai_msgs,
            max_tokens=body.get("max_tokens"),
            priority=1 if user_ctx.get("authenticated") else 0,
            timeout=30.0,
        )
        if not ok:
            return _anthropic_error(429, meta.get("error", "Rate limit exceeded"))

    if adapter.native_format == "openai":
        dropped = _sanitize_for_openai_backend(body)
        if dropped:
            logger.warning(f"[{request_id}] Dropped Anthropic-only fields for OpenAI backend: {dropped}")

    metadata = {
        "user_agent": request.headers.get("user-agent"),
        "ip": get_client_ip(request),
        "authenticated": bool(user_ctx.get("authenticated")),
        "user_id": user_ctx.get("user_id"),
        "surface": "anthropic_messages",
        "alias_input": model_id if model_id != canonical else None,
    }

    is_streaming = bool(body.get("stream"))
    if is_streaming:
        # Streaming dispatch lands in Task 13.
        return _anthropic_error(501, "Streaming on /v1/messages not yet implemented")

    # --- Non-streaming dispatch ---
    try:
        resp = await adapter.messages(body, request_id=request_id)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))
    except Exception as exc:
        logger.exception(f"[{request_id}] Adapter messages() failed")
        return _anthropic_error(502, f"Upstream error: {exc}")

    usage = (resp.get("usage") or {}) if isinstance(resp, dict) else {}
    usage_for_log = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    latency_ms = int((time.time() - start) * 1000)
    provider = adapter.config.provider
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(canonical),
        provider=normalize_provider_label(provider),
        status_code="200",
    ).inc()
    if db_logger:
        _schedule_db_log(
            db_logger,
            request_id=request_id,
            model_id=canonical,
            provider=provider,
            usage=usage_for_log,
            latency_ms=latency_ms,
            status_code=200,
            pricing=route.pricing if hasattr(route, "pricing") else {},
            metadata=metadata,
        )
    return JSONResponse(content=resp)
```

Wire the router in `serving/servers/app.py` (alongside the still-existing `anthropic_proxy.router`; that file is removed in Task 16):

```python
from serving.servers.routers import anthropic_messages
# ... existing router includes ...
app.include_router(anthropic_messages.router)
```

- [ ] **Step 4: Run tests; verify non-streaming tests pass.**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v`
Expected: all 5 non-streaming tests pass.

- [ ] **Step 5: Commit.**

```bash
git add serving/servers/routers/anthropic_messages.py serving/servers/app.py \
        test/servers/test_anthropic_messages_router.py test/servers/conftest.py
git commit -m "feat(routers): /v1/messages router (non-streaming dispatch)"
```

---

## Task 13: New Router — Streaming Dispatch

**Files:**
- Modify: `serving/servers/routers/anthropic_messages.py`
- Modify: `test/servers/test_anthropic_messages_router.py`

- [ ] **Step 1: Add failing streaming tests.**

```python
# Append to test/servers/test_anthropic_messages_router.py


@pytest.mark.asyncio
async def test_v1_messages_native_streaming_passthrough(anthropic_test_client):
    upstream_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"msg_s","model":"claude-opus-4-7",'
        b'"role":"assistant","content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    body = {"model": NATIVE_MODEL, "max_tokens": 50, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    with aioresponses() as m:
        m.post(ANTHROPIC_UPSTREAM, body=upstream_sse, content_type="text/event-stream")
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=body, headers=_auth()
        ) as r:
            assert r.status_code == 200
            collected = b""
            async for chunk in r.aiter_bytes():
                collected += chunk
        assert collected == upstream_sse


@pytest.mark.asyncio
async def test_v1_messages_translated_streaming(anthropic_test_client):
    """Anthropic-format streaming request to glm-4.7 -> translated SSE."""
    openai_sse = (
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","model":"glm-4.7",'
        b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
    )
    body = {"model": OPENAI_MODEL, "max_tokens": 50, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    with aioresponses() as m:
        m.post(ZAI_UPSTREAM, body=openai_sse, content_type="text/event-stream")
        async with anthropic_test_client.stream(
            "POST", "/v1/messages", json=body, headers=_auth()
        ) as r:
            assert r.status_code == 200
            collected = b""
            async for chunk in r.aiter_bytes():
                collected += chunk
        assert b"event: message_start" in collected
        assert b"event: message_stop" in collected
        assert b'"text":"Hi"' in collected
```

- [ ] **Step 2: Run tests; verify they fail (501 returned).**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v -k streaming`
Expected: 2 failures with 501.

- [ ] **Step 3: Replace the streaming-501 stub in `serving/servers/routers/anthropic_messages.py`.**

Locate the `if is_streaming:` block in the handler and replace it with the real streaming dispatch:

```python
    if is_streaming:
        sse_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }

        async def _gen():
            usage = {"input_tokens": 0, "output_tokens": 0}
            stream_failed = False
            try:
                async for chunk in adapter.stream_messages(body, request_id=request_id):
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    yield chunk
            except Exception as exc:
                stream_failed = True
                logger.exception(f"[{request_id}] Streaming dispatch failed")
                err = {"type": "error",
                       "error": {"type": "api_error",
                                 "message": f"Stream interrupted: {exc}"}}
                import json as _j
                yield f"event: error\ndata: {_j.dumps(err)}\n\n".encode("utf-8")
            finally:
                # Pull usage from the adapter post-stream.
                if hasattr(adapter, "last_stream_usage"):
                    usage = adapter.last_stream_usage
                latency_ms = int((time.time() - start) * 1000)
                status_code = 502 if stream_failed else 200
                API_MODEL_REQUESTS.labels(
                    model=normalize_model_label(canonical),
                    provider=normalize_provider_label(adapter.config.provider),
                    status_code=str(status_code),
                ).inc()
                if db_logger:
                    _schedule_db_log(
                        db_logger,
                        request_id=request_id,
                        model_id=canonical,
                        provider=adapter.config.provider,
                        usage=usage,
                        latency_ms=latency_ms,
                        status_code=status_code,
                        pricing=route.pricing if hasattr(route, "pricing") else {},
                        metadata=metadata,
                    )

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=sse_headers)
```

For OpenAI-backend translation, the `BaseAdapter.stream_messages` default impl already covers it (Task 6); but it does not populate `last_stream_usage`. Augment the default impl to do so. Modify `serving/adapters/base.py` so the default `stream_messages` exposes the translator's `.usage` after `finalize`:

```python
        translator = OpenAIToAnthropicStreamTranslator(model=body.get("model", ""))
        async for openai_chunk in self.stream_chat_completion(oai_messages, **oai_params):
            data = openai_chunk.encode("utf-8") if isinstance(openai_chunk, str) else openai_chunk
            for ant in translator.feed(data):
                yield ant
        for ant in translator.finalize():
            yield ant
        # Expose usage for the router's DB-logging step.
        self.last_stream_usage = {
            "input_tokens": translator.usage.get("input_tokens", 0),
            "output_tokens": translator.usage.get("output_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
```

(Initialize `last_stream_usage = {...}` on `BaseAdapter` similar to `AnthropicAdapter`.)

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v`
Expected: all router tests green.

- [ ] **Step 5: Commit.**

```bash
git add serving/servers/routers/anthropic_messages.py serving/adapters/base.py \
        test/servers/test_anthropic_messages_router.py
git commit -m "feat(routers): /v1/messages streaming dispatch"
```

---

## Task 14: Router — Field Sanitization Test Coverage

The sanitizer was implemented in Task 12 but lacks dedicated test coverage. Add it now.

**Files:**
- Modify: `test/servers/test_anthropic_messages_router.py`

- [ ] **Step 1: Add failing tests.**

```python
# Append to test/servers/test_anthropic_messages_router.py


@pytest.mark.asyncio
async def test_cache_control_dropped_for_openai_backend(anthropic_test_client, caplog):
    openai_resp = {
        "id": "x", "object": "chat.completion", "model": OPENAI_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    body = {
        "model": OPENAI_MODEL, "max_tokens": 50,
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": "hi",
                                   "cache_control": {"type": "ephemeral"}}]}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }
    with aioresponses() as m:
        m.post(ZAI_UPSTREAM, payload=openai_resp)
        with caplog.at_level("WARNING"):
            r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
        assert r.status_code == 200
    assert any("cache_control" in rec.message and "thinking" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_cache_control_preserved_for_native_backend(anthropic_test_client):
    """Native (Anthropic kind) backend gets cache_control passed through unchanged."""
    upstream_resp = {
        "id": "msg_z", "type": "message", "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body = {
        "model": NATIVE_MODEL, "max_tokens": 50,
        "messages": [{"role": "user",
                      "content": [{"type": "text", "text": "hi",
                                   "cache_control": {"type": "ephemeral"}}]}],
    }
    with aioresponses() as m:
        m.post(ANTHROPIC_UPSTREAM, payload=upstream_resp)
        r = await anthropic_test_client.post("/v1/messages", json=body, headers=_auth())
        assert r.status_code == 200
        sent_req = m.requests[("POST", URL(ANTHROPIC_UPSTREAM))][0]
        sent = json.loads(sent_req.kwargs["data"])
    # cache_control must reach upstream verbatim.
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
```

- [ ] **Step 2: Run tests; verify pass (sanitizer was implemented in Task 12).**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v -k cache_control`
Expected: 2 passed.

- [ ] **Step 3: Commit.**

```bash
git add test/servers/test_anthropic_messages_router.py
git commit -m "test(routers): cache_control + thinking sanitization coverage"
```

---

## Task 15: App-Level Anthropic-Format Exception Handler

When `verify_api_key` (or another dependency) raises `HTTPException`, FastAPI returns `{"detail": "..."}`. Anthropic clients need `{"type":"error", "error":{"type":"...", "message":"..."}}`.

**Files:**
- Modify: `serving/servers/app.py`
- Modify: `test/servers/test_anthropic_messages_router.py`

- [ ] **Step 1: Add failing tests.**

```python
# Append to test/servers/test_anthropic_messages_router.py


@pytest.mark.asyncio
async def test_missing_auth_returns_anthropic_format(anthropic_test_client):
    r = await anthropic_test_client.post("/v1/messages", json={
        "model": NATIVE_MODEL, "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    err = r.json()
    assert err["type"] == "error"
    assert err["error"]["type"] == "authentication_error"


@pytest.mark.asyncio
async def test_invalid_auth_returns_anthropic_format(anthropic_test_client):
    r = await anthropic_test_client.post(
        "/v1/messages",
        headers={"x-api-key": "hyi-not-a-real-key"},
        json={"model": NATIVE_MODEL, "max_tokens": 10,
              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"
```

Note: the third regression test (default-format on `/v1/chat/completions`) is dropped from this task — the `anthropic_test_app` fixture only mounts the anthropic_messages router, so `/v1/chat/completions` would 404 anyway. The `_ANTHROPIC_PATHS` filter in the exception handler will be exercised once the full app is run end-to-end (Task 19 manual smoke).

- [ ] **Step 2: Run tests; verify failures.**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v -k auth`
Expected: failures (FastAPI default shape returned).

- [ ] **Step 3: Define a reusable handler function in `serving/servers/routers/anthropic_messages.py` and register it on both the prod app (`app.py`) and the test fixture.**

Append to `serving/servers/routers/anthropic_messages.py`:

```python
_ANTHROPIC_PATHS = ("/v1/messages", "/anthropic/")


async def anthropic_aware_http_exception_handler(request, exc):
    """FastAPI exception handler that emits Anthropic-format errors for
    requests against the Anthropic surfaces, and the default JSON shape
    everywhere else."""
    from fastapi.responses import JSONResponse
    path = request.url.path
    if any(path.startswith(p) for p in _ANTHROPIC_PATHS):
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": "error",
                     "error": {"type": _ERROR_TYPE_BY_STATUS.get(exc.status_code, "api_error"),
                               "message": str(exc.detail)}},
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
```

Register in `serving/servers/app.py` after `app = FastAPI(...)` and the router includes:

```python
from fastapi import HTTPException
from serving.servers.routers.anthropic_messages import anthropic_aware_http_exception_handler

app.add_exception_handler(HTTPException, anthropic_aware_http_exception_handler)
```

Register in the test fixture `anthropic_test_app` (in `test/servers/conftest.py`). After `app.include_router(anthropic_messages.router)`, add:

```python
    from fastapi import HTTPException
    from serving.servers.routers.anthropic_messages import (
        anthropic_aware_http_exception_handler,
    )
    app.add_exception_handler(HTTPException, anthropic_aware_http_exception_handler)
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `uv run pytest test/servers/test_anthropic_messages_router.py -v`
Expected: all router tests green.

- [ ] **Step 5: Commit.**

```bash
git add serving/servers/app.py test/servers/test_anthropic_messages_router.py
git commit -m "feat(app): Anthropic-format error envelope for /v1/messages and /anthropic paths"
```

---

## Task 16: Delete Legacy `anthropic_proxy.py`

**Files:**
- Delete: `serving/servers/routers/anthropic_proxy.py`
- Delete: `test/unit/servers/test_anthropic_proxy.py`
- Delete: `test/servers/test_anthropic_proxy.py`
- Modify: `serving/servers/app.py`

- [ ] **Step 1: Remove the include of `anthropic_proxy.router` from `app.py`.**

```python
# Remove this line from serving/servers/app.py:
#   app.include_router(anthropic_proxy.router)
# And remove the import.
```

- [ ] **Step 2: Delete the files.**

```bash
git rm serving/servers/routers/anthropic_proxy.py
git rm test/unit/servers/test_anthropic_proxy.py
git rm test/servers/test_anthropic_proxy.py
```

- [ ] **Step 3: Find any leftover imports and fix.**

```bash
grep -rn "anthropic_proxy\|_extract_usage_from_sse\b" serving test --include="*.py"
```

If the SSE helper is referenced by code other than the router we just deleted, it now lives at `serving.adapters.anthropic_translator.extract_anthropic_usage_from_sse` (renamed during Task 5). Update those callsites.

- [ ] **Step 4: Run the full test suite to confirm no regressions.**

Run: `uv run pytest test/ -x`
Expected: green.

- [ ] **Step 5: Commit.**

```bash
git add serving/servers/app.py
git commit -m "chore: remove legacy claude_sub-only anthropic_proxy router"
```

---

## Task 17: Retarget Claude Models to `kind: anthropic`

**Files:**
- Modify: `config/models.yaml`

- [ ] **Step 1: Inspect current `claude-sonnet-4.6`, `claude-opus-4.6`, `claude-opus-4.7` entries.**

Read `config/models.yaml` lines 522-605. Each currently uses `provider: openai_compat` with a single route `kind: openai_compat` to a local cli-proxy.

- [ ] **Step 2: Replace each block. Example for `claude-opus-4.7`.**

```yaml
  - id: claude-opus-4.7
    name: Claude Opus 4.7
    provider: anthropic
    required_role: internal
    provider_model_id: "claude-opus-4-7"
    context_length: 200000
    max_output_length: 128000
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop, stream, tools, tool_choice]
    input_modalities: [text, image]
    output_modalities: [text]
    quantization: "none"
    pricing:
      prompt: "5.00"
      completion: "25.00"
      image: "0"
      request: "0"
      input_cache_reads: "0.50"
      input_cache_writes: "6.25"
    route:
      - kind: anthropic
        weight: 1.0
        base_url: https://api.anthropic.com
        api_key: ${ANTHROPIC_API_KEY}
        provider_model_id: "claude-opus-4-7"
```

Repeat structure for `claude-opus-4.6` and `claude-sonnet-4.6`. Keep pricing, role gating, `provider_model_id`, and limits identical to the old entries.

- [ ] **Step 3: Verify the YAML loads cleanly (boot the server in dry-run if available, else import and parse).**

```bash
uv run python -c "
from serving.servers.registry import register_from_models_yaml
from routing.executor import RouteExecutor
import os
os.environ.setdefault('ANTHROPIC_API_KEY', 'sk-ant-placeholder')
re = RouteExecutor()
n, infos = register_from_models_yaml(re, 'config/models.yaml')
print('registered', n, 'models')
print([i.id for i in infos if 'claude' in i.id])
"
```
Expected: prints registered count > 0 and includes the three claude IDs.

- [ ] **Step 4: Commit.**

```bash
git add config/models.yaml
git commit -m "config(models): retarget Claude models to direct kind: anthropic"
```

---

## Task 18: Update `setupAnthropic.sh`

**Files:**
- Modify: `setupAnthropic.sh`

- [ ] **Step 1: Replace the file content.**

```bash
#!/usr/bin/env bash
# Configure environment + Claude Code CLI to point at freeinference's
# Anthropic Messages compat surface.
#
# Both endpoints are supported and reach the same handler:
#   - https://staging.freeinference.org/v1/messages           (recommended)
#   - https://staging.freeinference.org/anthropic/v1/messages (legacy alias)

export ANTHROPIC_AUTH_TOKEN="hyi-your-api-key"
export ANTHROPIC_API_KEY="hyi-your-api-key"
export ANTHROPIC_BASE_URL="https://staging.freeinference.org"
export CLAUDE_MODEL="claude-opus-4.7"

mkdir -p ~/.claude
cat > ~/.claude/settings.json <<EOF
{
  "env": {
    "ANTHROPIC_BASE_URL": "${ANTHROPIC_BASE_URL}",
    "ANTHROPIC_AUTH_TOKEN": "${ANTHROPIC_AUTH_TOKEN}",
    "ANTHROPIC_MODEL": "${CLAUDE_MODEL}"
  },
  "hasCompletedOnboarding": true
}
EOF

# Sanity check
curl -s "${ANTHROPIC_BASE_URL}/v1/models" \
  -H "Authorization: Bearer ${ANTHROPIC_API_KEY}" \
  -H "anthropic-version: 2023-06-01" | head -c 500
echo
```

- [ ] **Step 2: Commit.**

```bash
git add setupAnthropic.sh
git commit -m "chore(setup): point Anthropic CLI script at /v1/messages root path"
```

---

## Task 19: Lint, Format, Typecheck, Manual Smoke Test

**Files:** none (verification only).

- [ ] **Step 1: Run `ruff format` and `ruff check`.**

```bash
uv run ruff format .
uv run ruff format --check .
uv run ruff check .
```
Expected: clean.

- [ ] **Step 2: Run typecheck.**

```bash
make typecheck
```
Expected: no new errors introduced (compare against baseline if pre-existing errors are present in the codebase).

- [ ] **Step 3: Run the full test suite.**

```bash
uv run pytest test/ -x
```
Expected: green.

- [ ] **Step 4: Manual smoke test against staging once deployed.**

```bash
# Native model passthrough
curl https://staging.freeinference.org/v1/messages \
  -H "Authorization: Bearer ${HYI_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4.7","max_tokens":64,"messages":[{"role":"user","content":"Say hi in 3 words."}]}'

# Translated path: Anthropic-format -> OpenAI-style backend
curl https://staging.freeinference.org/v1/messages \
  -H "Authorization: Bearer ${HYI_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"glm-4.7","max_tokens":64,"messages":[{"role":"user","content":"Say hi in 3 words."}]}'

# Streaming on translated path
curl -N https://staging.freeinference.org/v1/messages \
  -H "Authorization: Bearer ${HYI_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"glm-4.7","max_tokens":64,"stream":true,"messages":[{"role":"user","content":"Count to 3."}]}'

# Claude Code CLI smoke (non-interactive)
ANTHROPIC_BASE_URL=https://staging.freeinference.org \
ANTHROPIC_API_KEY=${HYI_KEY} \
  claude --model claude-opus-4.7 -p "Say hi in 3 words"
```

Expected: each command returns a sensible response. The translated streaming response shows Anthropic-format SSE events (`event: message_start`, `event: content_block_delta`, …, `event: message_stop`).

- [ ] **Step 5: Open a PR to `dev` once smoke tests pass.**

Per project conventions (CLAUDE.md):

```bash
git push -u origin jason/claude/anthropic-compat
gh pr create --base dev --title "feat: Anthropic Messages API compatibility across all backends" \
  --body "$(cat <<'EOF'
## Summary
- New `AnthropicAdapter` (`kind: anthropic`) talks `api.anthropic.com` directly.
- `BaseAdapter.messages()` / `stream_messages()` defaults translate Anthropic ↔ OpenAI for all OpenAI-style backends.
- New router `anthropic_messages.py` mounted at `/v1/messages` and `/anthropic/v1/messages`; replaces legacy `anthropic_proxy.py`.
- `/v1/models` now returns Anthropic-format on requests with `anthropic-version` header or Anthropic-family User-Agent.
- Claude models retargeted from cli-proxy to direct `kind: anthropic` routes.

## Test plan
- [ ] Unit tests pass (`uv run pytest test/`)
- [ ] `ruff format --check .` clean
- [ ] Manual: Claude Code CLI works against staging on `claude-opus-4.7` and `glm-4.7`
- [ ] Manual: `anthropic` Python SDK works against staging on both native and translated models
- [ ] Manual: `/v1/chat/completions` regression-free
EOF
)"
```

Then per CLAUDE.md: "please check comments and fix CI errors 8min after creating PR".

---

## Self-Review Checklist (run before PR)

| Check | Status |
|---|---|
| Spec section 1 (Goal) — covered by Tasks 6, 7, 8, 12, 13 | ✓ |
| Spec section 5 (Routing) — covered by Tasks 11, 12, 13 | ✓ |
| Spec section 6 (BaseAdapter) — covered by Task 6 | ✓ |
| Spec section 7 (AnthropicAdapter) — covered by Tasks 7, 8, 9 | ✓ |
| Spec section 8 (Translator) — covered by Tasks 1-5 | ✓ |
| Spec section 9 (Router) — covered by Tasks 12, 13, 14, 15 | ✓ |
| Spec section 10 (Best-effort field drops) — covered by Tasks 2 (translator) + 14 (router test) | ✓ |
| Spec section 11 (Testing) — distributed throughout each task | ✓ |
| Spec section 12 (Rollout) — covered by Tasks 17, 18, 19 | ✓ |
| Spec section 14 (Success criteria) — verified in Task 19 | ✓ |
| No "TBD"/"TODO"/placeholders in plan body — verified | ✓ |
| Function/class names consistent across tasks — verified (`AnthropicAdapter`, `OpenAIToAnthropicStreamTranslator`, `extract_anthropic_usage_from_sse`, `resolve_anthropic_alias`, `_anthropic_error`) | ✓ |
