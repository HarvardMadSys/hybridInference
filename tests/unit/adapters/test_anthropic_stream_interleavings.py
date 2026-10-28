"""Verify full output order and argument fidelity for parallel tool streams."""

import json
import random

import pytest

from serving.adapters.anthropic_translator import OpenAIToAnthropicStreamTranslator


def _frame(delta, finish=None):
    return (
        "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"
    ).encode()


@pytest.mark.parametrize("seed", range(24))
def test_parallel_tools_conserve_inputs_order_and_lifecycle(seed):
    rng = random.Random(seed)
    queues = []
    expected_tools = {}
    for index in range(3):
        name = f"tool_{index}"
        call_id = f"call_{index}"
        arguments = {"city": 'S"F', "n": index, "data": [1, 2]}
        expected_tools[call_id] = (name, arguments)
        encoded = json.dumps(arguments)
        fragments = ["", encoded[:5], encoded[5:17], encoded[17:], " \n"]
        parts = []
        for part, fragment in enumerate(fragments):
            tc = {"index": index, "function": {"arguments": fragment}}
            if part == 0:
                tc.update(id=call_id, type="function")
                tc["function"]["name"] = name
            parts.append({"tool_calls": [tc]})
        queues.append(parts)
    queues.append(
        [{"content": "A"}, {"reasoning_content": "B"}, {"content": "C"}, {"reasoning_content": "D"}]
    )
    expected_fragments = []
    deltas = [queues[0].pop(0)]
    while any(queues):
        available = [q for q in queues if q]
        delta = rng.choice(available).pop(0)
        deltas.append(delta)
        if "content" in delta:
            expected_fragments.append(("text", delta["content"]))
        if "reasoning_content" in delta:
            expected_fragments.append(("thinking", delta["reasoning_content"]))
    translator = OpenAIToAnthropicStreamTranslator(model="synthetic")
    expected_blocks = []
    for delta in deltas:
        for call in delta.get("tool_calls", []):
            if "id" in call:
                expected_blocks.append(("tool_use", call["id"]))
        for key, kind in [("content", "text"), ("reasoning_content", "thinking")]:
            if key in delta:
                if expected_blocks and expected_blocks[-1][0] == kind:
                    expected_blocks[-1] = (kind, expected_blocks[-1][1] + delta[key])
                else:
                    expected_blocks.append((kind, delta[key]))
    output = []
    for delta in deltas:
        output.extend(translator.feed(_frame(delta)))
    output.extend(translator.feed(_frame({}, "tool_calls")))
    output.extend(translator.finalize())
    blocks = {}
    open_blocks = set()
    observed_fragments = []
    stops = 0
    for wire in output:
        for line in wire.splitlines():
            if not line.startswith(b"data: "):
                continue
            event = json.loads(line[6:])
            kind = event["type"]
            index = event.get("index")
            if kind == "content_block_start":
                assert not open_blocks, (seed, "overlap", event)
                assert index not in blocks, (seed, "duplicate index", event)
                open_blocks.add(index)
                blocks[index] = {**event["content_block"], "args": "", "visible": ""}
            elif kind == "content_block_delta":
                assert index in open_blocks, (seed, "delta to closed block", event)
                delta = event["delta"]
                if delta["type"] == "input_json_delta":
                    blocks[index]["args"] += delta["partial_json"]
                elif delta["type"] == "text_delta":
                    blocks[index]["visible"] += delta["text"]
                    observed_fragments.extend(("text", ch) for ch in delta["text"])
                elif delta["type"] == "thinking_delta":
                    blocks[index]["visible"] += delta["thinking"]
                    observed_fragments.extend(("thinking", ch) for ch in delta["thinking"])
            elif kind == "content_block_stop":
                assert index in open_blocks, (seed, "stop of closed block", event)
                open_blocks.remove(index)
            elif kind == "message_stop":
                assert not open_blocks
                stops += 1
    assert stops == 1
    assert observed_fragments == expected_fragments, seed
    actual_tools = {}
    for block in blocks.values():
        if block["type"] == "tool_use":
            assert block["id"] and block["name"], (seed, "orphan", block)
            assert block["id"] not in actual_tools, (seed, "duplicate call", block)
            actual_tools[block["id"]] = (block["name"], json.loads(block["args"] or "{}"))
    assert actual_tools == expected_tools, seed
    actual_blocks = [
        (b["type"], b["id"] if b["type"] == "tool_use" else b["visible"]) for b in blocks.values()
    ]
    assert actual_blocks == expected_blocks, (seed, actual_blocks, expected_blocks)
