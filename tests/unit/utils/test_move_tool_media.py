"""``move_tool_media``: a tool message holds text only, so its media follow it."""

from __future__ import annotations

import time

from serving.schemas import CONTENT_BLOCK_MODALITY
from serving.utils.messages import MEDIA_PART_TYPES, move_tool_media

_IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}
_CALLS = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "view_image", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
    ],
}


def test_media_part_types_match_the_modality_table():
    """Kept in step with the table the modality gate reads (not importable here)."""
    assert {k for k, v in CONTENT_BLOCK_MODALITY.items() if v != "text"} == MEDIA_PART_TYPES


def test_a_list_without_tool_media_is_returned_as_is():
    messages = [
        _CALLS,
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "a"}]},
    ]
    assert move_tool_media(messages) is messages


def test_a_tool_image_follows_the_whole_run_of_tool_messages():
    messages = [
        {"role": "user", "content": "look"},
        _CALLS,
        {"role": "tool", "tool_call_id": "c1", "content": [_IMG]},
        {"role": "tool", "tool_call_id": "c2", "content": "ok"},
        {"role": "assistant", "content": "seen"},
    ]

    out = move_tool_media(messages)

    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user", "assistant"]
    assert out[2] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "[The tool returned an image; attached in the next user message.]",
    }
    assert out[4] == {
        "role": "user",
        "content": [{"type": "text", "text": "Image output of tool call c1:"}, _IMG],
    }
    assert messages[2]["content"] == [_IMG], "the caller's list is not mutated"


def test_tool_media_join_the_user_message_after_the_run_ahead_of_its_text():
    messages = [
        _CALLS,
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": [{"type": "input_text", "text": "shot"}, _IMG, _IMG],
        },
        {"role": "user", "content": "now fix it"},
    ]

    out = move_tool_media(messages)

    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert (
        out[1]["content"]
        == "shot\n[The tool returned 2 images; attached in the next user message.]"
    )
    assert out[2]["content"] == [
        {"type": "text", "text": "Image output of tool call c1:"},
        _IMG,
        _IMG,
        {"type": "text", "text": "now fix it"},
    ]


def test_a_trailing_tool_run_gets_a_user_message_of_its_own():
    audio = {"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}}
    out = move_tool_media([_CALLS, {"role": "tool", "tool_call_id": "c1", "content": [audio]}])

    assert (
        out[1]["content"] == "[The tool returned a media part; attached in the next user message.]"
    )
    assert out[2] == {
        "role": "user",
        "content": [{"type": "text", "text": "Output of tool call c1:"}, audio],
    }


def test_identical_parts_and_repeated_media_each_keep_their_place():
    text = {"type": "text", "text": "a"}
    copy_of_img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}
    content = [text, _IMG, dict(text), _IMG, copy_of_img, "plain"]

    out = move_tool_media([_CALLS, {"role": "tool", "tool_call_id": "c1", "content": content}])

    assert (
        out[1]["content"]
        == "a\na\nplain\n[The tool returned 3 images; attached in the next user message.]"
    )
    assert out[2]["content"] == [
        {"type": "text", "text": "Image output of tool call c1:"},
        _IMG,
        _IMG,
        copy_of_img,
    ]
    assert out[2]["content"][1] is _IMG and out[2]["content"][3] is copy_of_img


def test_a_part_with_an_unhashable_type_is_not_media_and_does_not_raise():
    odd = {"type": ["image_url"], "text": "odd"}

    out = move_tool_media([_CALLS, {"role": "tool", "tool_call_id": "c1", "content": [odd, _IMG]}])

    assert (
        out[1]["content"] == "odd\n[The tool returned an image; attached in the next user message.]"
    )


def test_tens_of_thousands_of_parts_are_split_in_linear_time():
    # Removing the media by value (``part not in media``) was quadratic: 20,000
    # text and 20,000 image parts held the event loop for about 27 s.
    n = 40_000
    images = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{i}"}} for i in range(n)
    ]
    content = [{"type": "text", "text": "x"} for _ in range(n)] + images
    messages = [_CALLS, {"role": "tool", "tool_call_id": "c1", "content": content}]

    started = time.perf_counter()
    out = move_tool_media(messages)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert out[1]["content"].endswith(
        f"[The tool returned {n} images; attached in the next user message.]"
    )
    assert out[2]["content"][1:] == images
