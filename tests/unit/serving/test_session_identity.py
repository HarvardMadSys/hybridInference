"""Unit tests for :mod:`serving.utils.session_identity`."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.datastructures import Headers

from serving.utils.session_identity import (
    MAX_SESSION_ID_CHARS,
    SessionIdentity,
    normalize_session_id,
    session_identity,
)

# What Claude Code puts in ``metadata.user_id``: one string carrying the user,
# the account and the run.
CLAUDE_CODE_USER_ID = (
    "user_9f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f"
    "_account_2f1a0b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
    "_session_7c6b5a49-3827-1605-f4e3-d2c1b0a99887"
)
CLAUDE_CODE_SESSION = "7c6b5a49-3827-1605-f4e3-d2c1b0a99887"


def _headers(**pairs: str) -> Headers:
    return Headers(pairs)


def test_no_declaration_returns_none() -> None:
    assert session_identity(_headers()) is None
    assert session_identity(_headers(), {"messages": []}) is None


def test_canonical_header() -> None:
    assert session_identity(_headers(**{"X-Session-ID": "sess_1"})) == SessionIdentity(
        "sess_1", "x-session-id"
    )


def test_canonical_header_is_case_insensitive() -> None:
    # Starlette normalizes header names; the lookup must not depend on casing.
    assert session_identity(_headers(**{"x-session-id": "sess_1"})) == SessionIdentity(
        "sess_1", "x-session-id"
    )


def test_canonical_header_wins_over_every_other_source() -> None:
    # A client using the gateway's own contract is never overridden by a value
    # derived from something it did not mean as a session id.
    identity = session_identity(
        _headers(**{"X-Session-ID": "canonical", "session_id": "codex"}),
        {"metadata": {"session_id": "body", "user_id": CLAUDE_CODE_USER_ID}},
    )
    assert identity == SessionIdentity("canonical", "x-session-id")


def test_agent_session_header() -> None:
    # Codex CLI stamps its run id on every Responses request.
    assert session_identity(_headers(session_id="codex-run")) == SessionIdentity(
        "codex-run", "session_id"
    )


def test_conversation_header_used_when_session_header_absent() -> None:
    assert session_identity(_headers(conversation_id="conv-1")) == SessionIdentity(
        "conv-1", "conversation_id"
    )


def test_session_header_preferred_over_conversation_header() -> None:
    identity = session_identity(_headers(session_id="run-1", conversation_id="conv-1"))
    assert identity == SessionIdentity("run-1", "session_id")


def test_body_metadata_session_id() -> None:
    identity = session_identity(_headers(), {"metadata": {"session_id": "s-9"}})
    assert identity == SessionIdentity("s-9", "metadata.session_id")


def test_headers_win_over_body() -> None:
    identity = session_identity(
        _headers(session_id="from-header"), {"metadata": {"session_id": "from-body"}}
    )
    assert identity == SessionIdentity("from-header", "session_id")


def test_claude_code_composite_user_id() -> None:
    identity = session_identity(_headers(), {"metadata": {"user_id": CLAUDE_CODE_USER_ID}})
    assert identity == SessionIdentity(CLAUDE_CODE_SESSION, "metadata.user_id")


def test_claude_code_session_read_up_to_the_next_segment() -> None:
    # The id ends at the next ``_`` boundary rather than at the end of the
    # string, so a segment appended in some future version still yields the
    # session rather than nothing.
    identity = session_identity(
        _headers(), {"metadata": {"user_id": f"{CLAUDE_CODE_USER_ID}_env_ide"}}
    )
    assert identity == SessionIdentity(CLAUDE_CODE_SESSION, "metadata.user_id")


def test_declared_body_session_wins_over_the_composite_user_id() -> None:
    identity = session_identity(
        _headers(),
        {"metadata": {"session_id": "declared", "user_id": CLAUDE_CODE_USER_ID}},
    )
    assert identity == SessionIdentity("declared", "metadata.session_id")


@pytest.mark.parametrize(
    "user_id",
    [
        "alice",  # a plain identifier: no session packed into it
        "user_9f1c_account_2f1a",  # the composite id without the run segment
        "sessionless-client",  # "session" without the segment separators
        "user_1_session_",  # the marker with nothing after it
        "user_1_session_-abc",  # an id that does not start with a name character
        "user_1_session_ 7c6b",  # a space is not part of an id
    ],
)
def test_user_id_without_a_session_segment_is_not_guessed(user_id: str) -> None:
    assert session_identity(_headers(), {"metadata": {"user_id": user_id}}) is None


def test_overlong_claude_code_segment_rejected() -> None:
    # The derived value is held to the same bound as a declared one.
    long_segment = "a" * (MAX_SESSION_ID_CHARS + 1)
    assert (
        session_identity(_headers(), {"metadata": {"user_id": f"u_session_{long_segment}"}}) is None
    )


@pytest.mark.parametrize(
    "body",
    [None, "not-json-object", ["metadata"], 7, {"metadata": "u1"}, {"metadata": None}, {}],
)
def test_body_without_a_metadata_object_is_skipped(body: Any) -> None:
    assert session_identity(_headers(), body) is None
    # ...and does not stop a header from being read.
    assert session_identity(_headers(session_id="run-1"), body) == SessionIdentity(
        "run-1", "session_id"
    )


def test_whitespace_is_trimmed() -> None:
    assert normalize_session_id("  sess_1\t") == "sess_1"


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        {"session": "x"},
        ["x"],
        "",
        "   ",
        "sess\n1",  # embedded control characters break log rendering
        "sess\x001",
        "sess\x7f1",
        "a" * (MAX_SESSION_ID_CHARS + 1),
    ],
)
def test_unusable_values_are_rejected(value: Any) -> None:
    assert normalize_session_id(value) is None


def test_value_at_the_length_bound_is_kept() -> None:
    at_bound = "a" * MAX_SESSION_ID_CHARS
    assert normalize_session_id(at_bound) == at_bound


def test_unusable_header_does_not_shadow_a_usable_source() -> None:
    # A rejected canonical header must fall through rather than resolve to
    # nothing: the request still declares a session, just not there.
    identity = session_identity(
        _headers(**{"X-Session-ID": "   ", "session_id": "run-1"}),
    )
    assert identity == SessionIdentity("run-1", "session_id")
