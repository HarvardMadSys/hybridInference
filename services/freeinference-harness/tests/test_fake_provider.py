"""Direct HTTP tests for the deterministic fake provider."""

from __future__ import annotations

import json

import httpx
from freeinference_harness.agent_scripts import DS4_MALFORMED_ARGUMENTS, marker


def _chat_payload(script_id: str, *, stream: bool, assistant_turns: int = 0) -> dict:
    """Builds a chat request selecting a script at a given turn index."""
    messages = [{"role": "user", "content": f"{marker(script_id)} run"}]
    for index in range(assistant_turns):
        messages.append({"role": "assistant", "content": f"turn {index}"})
        messages.append({"role": "user", "content": "continue"})
    return {"model": "agent-loop-fake", "messages": messages, "stream": stream}


def _collect_sse(response: httpx.Response) -> tuple[list[dict], bool]:
    """Parses SSE data events and whether [DONE] terminated the stream."""
    events: list[dict] = []
    done = False
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        body = line[6:].strip()
        if body == "[DONE]":
            done = True
            continue
        events.append(json.loads(body))
    return events, done


def test_models_and_health_endpoints(fake_base_url):
    """The fake serves model listing and health probes."""
    models = httpx.get(f"{fake_base_url}/v1/models").json()
    assert models["data"][0]["id"] == "agent-loop-fake"
    assert httpx.get(f"{fake_base_url}/health").json() == {"status": "ok"}


def test_no_marker_serves_default_text(fake_base_url):
    """Marker-less traffic (e.g. health probes) gets a harmless 200."""
    response = httpx.post(
        f"{fake_base_url}/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "no script marker" in content


def test_unknown_script_is_a_loud_400(fake_base_url):
    """Unknown script ids fail loudly instead of serving garbage."""
    response = httpx.post(
        f"{fake_base_url}/v1/chat/completions",
        json=_chat_payload("does-not-exist", stream=False),
    )
    assert response.status_code == 400
    assert "Unknown agent script" in response.json()["error"]["message"]


def test_streaming_tool_call_fragments_are_replayed_exactly(fake_base_url):
    """fragmented_args streams one delta per scripted fragment."""
    with httpx.Client() as client:
        response = client.post(
            f"{fake_base_url}/v1/chat/completions",
            json=_chat_payload("fragmented_args", stream=True),
        )
    events, done = _collect_sse(response)
    assert done

    fragments = []
    for event in events:
        for choice in event.get("choices") or []:
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                fragments.append((tc.get("function") or {}).get("arguments") or "")
    assert fragments == ['{"comm', 'and": "up', 'time"}']
    assert "".join(fragments) == '{"command": "uptime"}'


def test_ds4_bytes_are_replayed_exactly(fake_base_url):
    """The ds4 regression bytes survive splicing verbatim."""
    with httpx.Client() as client:
        response = client.post(
            f"{fake_base_url}/v1/chat/completions",
            json=_chat_payload("ds4_malformed_args", stream=True),
        )
    events, done = _collect_sse(response)
    assert done
    spliced = ""
    for event in events:
        for choice in event.get("choices") or []:
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                spliced += (tc.get("function") or {}).get("arguments") or ""
    assert spliced == DS4_MALFORMED_ARGUMENTS


def test_turn_index_follows_assistant_count(fake_base_url):
    """One assistant message in history selects the second scripted turn."""
    response = httpx.post(
        f"{fake_base_url}/v1/chat/completions",
        json=_chat_payload("basic_tool_roundtrip", stream=False, assistant_turns=1),
    )
    message = response.json()["choices"][0]["message"]
    assert "PWD_OK" in message["content"]


def test_first_attempt_429_then_success(fake_base_url):
    """rate_limited_then_ok serves 429 once, then the scripted text."""
    payload = _chat_payload("rate_limited_then_ok", stream=False)
    first = httpx.post(f"{fake_base_url}/v1/chat/completions", json=payload)
    assert first.status_code == 429
    assert first.headers.get("Retry-After") == "0"
    second = httpx.post(f"{fake_base_url}/v1/chat/completions", json=payload)
    assert second.status_code == 200
    assert "AFTER_429_OK" in second.json()["choices"][0]["message"]["content"]


def test_reset_rearms_first_attempt_errors(fake_base_url):
    """The reset endpoint clears serve counters."""
    payload = _chat_payload("rate_limited_then_ok", stream=False)
    assert httpx.post(f"{fake_base_url}/v1/chat/completions", json=payload).status_code == 429
    assert httpx.post(f"{fake_base_url}/v1/chat/completions", json=payload).status_code == 200
    assert httpx.post(f"{fake_base_url}/__fake__/reset").status_code == 200
    assert httpx.post(f"{fake_base_url}/v1/chat/completions", json=payload).status_code == 429


def test_midstream_disconnect_has_partial_content_and_no_done(fake_base_url):
    """midstream_disconnect emits partial deltas and never [DONE]."""
    with httpx.Client() as client:
        response = client.post(
            f"{fake_base_url}/v1/chat/completions",
            json=_chat_payload("midstream_disconnect", stream=True),
        )
    events, done = _collect_sse(response)
    assert not done
    content = ""
    finish_reasons = []
    for event in events:
        for choice in event.get("choices") or []:
            content += (choice.get("delta") or {}).get("content") or ""
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])
    assert content == "PARTIAL_STREAM_"
    assert finish_reasons == []


def test_non_stream_tool_call_shape(fake_base_url):
    """Non-streaming tool calls join fragments into one arguments string."""
    response = httpx.post(
        f"{fake_base_url}/v1/chat/completions",
        json=_chat_payload("basic_tool_roundtrip", stream=False),
    )
    body = response.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "pwd"}
    assert body["usage"]["total_tokens"] > 0
