"""Unit tests for :mod:`serving.utils.session_identity`."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.datastructures import Headers

from serving.utils.session_identity import (
    MAX_SESSION_ID_CHARS,
    SessionIdentity,
    consume_session_fields,
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


@pytest.mark.parametrize(
    "header",
    # What Codex CLI actually sends is the hyphenated pair; the underscore
    # spellings are accepted beside them because header names carrying
    # underscores are legal but commonly dropped by intermediaries, so clients
    # differ over which to send.
    [
        "x-opencode-session",
        "x-session-affinity",
        "session-id",
        "session_id",
        "thread-id",
        "conversation_id",
    ],
)
def test_agent_session_headers(header: str) -> None:
    assert session_identity(_headers(**{header: "codex-run"})) == SessionIdentity(
        "codex-run", header
    )


def test_session_header_preferred_over_thread_header() -> None:
    # A thread outlives the run that opened it, so the run wins when both are
    # present -- which is the case on every Codex request.
    identity = session_identity(
        _headers(**{"session-id": "run-1", "thread-id": "thread-1"}),
    )
    assert identity == SessionIdentity("run-1", "session-id")


def test_hyphenated_header_is_not_matched_by_the_underscore_name() -> None:
    # Starlette matches header names case-insensitively but not across
    # separators, which is what made the underscore-only list miss Codex.
    assert session_identity(_headers(**{"session-id": "run-1"})) is not None
    assert session_identity(_headers(**{"Session-Id": "run-1"})) == SessionIdentity(
        "run-1", "session-id"
    )


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


def test_overlong_id_after_the_session_is_rejected_not_truncated() -> None:
    # The bound applies to what the pattern captured, and the boundary
    # lookahead means it cannot capture a prefix of a longer token.
    long_tail = "a" * (MAX_SESSION_ID_CHARS + 1)
    body = {"metadata": {"user_id": f"user_9f1c_account_2f1a_session_{long_tail}"}}
    assert session_identity(_headers(), body) is None


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
        "user_9f1c_account_2f1a_session_",  # the marker with nothing after it
        "user_9f1c_account_2f1a_session_-ab",  # not starting with a name character
        "user_9f1c_account_2f1a_session_ 7c",  # a space is not part of an id
        # Not Claude Code's composite shape, merely a plain user id that
        # contains the marker: reading "internal" out of this would collapse
        # every such caller into one invented session.
        "customer_session_internal",
        "session_abc",
        "user_9f1c_session_7c6b",  # the account segment is missing entirely
        # The composite prefix matches, but what follows the session is not a
        # segment boundary: reading "admin" here would group an unrelated
        # caller under a truncation of its own identifier.
        "user_customer_account_tenant_session_admin@example.com",
        "user_9f1c_account_2f1a_session_7c6b/extra",
    ],
)
def test_only_the_full_composite_shape_yields_a_session(user_id: str) -> None:
    assert session_identity(_headers(), {"metadata": {"user_id": user_id}}) is None


def test_empty_account_segment_still_yields_the_session() -> None:
    # Claude Code sends ``_account__session_`` when there is no account.
    identity = session_identity(
        _headers(), {"metadata": {"user_id": f"user_9f1c_account__session_{CLAUDE_CODE_SESSION}"}}
    )
    assert identity == SessionIdentity(CLAUDE_CODE_SESSION, "metadata.user_id")


def test_overlong_claude_code_segment_rejected() -> None:
    # The derived value is held to the same bound as a declared one.
    long_segment = "a" * (MAX_SESSION_ID_CHARS + 1)
    user_id = f"user_9f1c_account_2f1a_session_{long_segment}"
    assert session_identity(_headers(), {"metadata": {"user_id": user_id}}) is None


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


def test_client_metadata_session_id() -> None:
    # Where Codex puts the declaration in the body.
    identity = session_identity(_headers(), {"client_metadata": {"session_id": "codex-body"}})
    assert identity == SessionIdentity("codex-body", "client_metadata.session_id")


def test_metadata_preferred_over_client_metadata() -> None:
    identity = session_identity(
        _headers(),
        {"metadata": {"session_id": "from-metadata"}, "client_metadata": {"session_id": "other"}},
    )
    assert identity == SessionIdentity("from-metadata", "metadata.session_id")


def test_headers_win_over_client_metadata() -> None:
    identity = session_identity(
        _headers(**{"session-id": "from-header"}), {"client_metadata": {"session_id": "from-body"}}
    )
    assert identity == SessionIdentity("from-header", "session-id")


def test_claude_code_composite_still_read_when_client_metadata_declares_nothing() -> None:
    identity = session_identity(
        _headers(),
        {"client_metadata": {"origin": "cli"}, "metadata": {"user_id": CLAUDE_CODE_USER_ID}},
    )
    assert identity == SessionIdentity(CLAUDE_CODE_SESSION, "metadata.user_id")


def test_consume_session_fields_strips_both_containers() -> None:
    body: dict[str, Any] = {
        "metadata": {"user_id": "u1", "session_id": "s1"},
        "client_metadata": {"session_id": "s2", "origin": "cli"},
        "messages": [],
    }
    consume_session_fields(body)
    # The declarations are gone; everything the upstream does know is untouched,
    # including whatever else the client put beside the declaration.
    assert body["metadata"] == {"user_id": "u1"}
    assert body["client_metadata"] == {"origin": "cli"}
    assert body["messages"] == []


def test_consume_session_fields_removes_a_container_the_declaration_emptied() -> None:
    # An emptied ``client_metadata`` is still a top-level field Anthropic's
    # Messages API does not define, so leaving ``{}`` behind fails the request
    # just as the key would have.
    body: dict[str, Any] = {"client_metadata": {"session_id": "s2"}, "messages": []}
    consume_session_fields(body)
    assert body == {"messages": []}

    body = {"metadata": {"session_id": "s1"}, "messages": []}
    consume_session_fields(body)
    assert body == {"messages": []}


@pytest.mark.parametrize("body", [None, "text", ["metadata"], {}, {"metadata": "u1"}])
def test_consume_session_fields_tolerates_any_shape(body: Any) -> None:
    consume_session_fields(body)  # must not raise


# --- OpenCode and its Kilo Code fork -----------------------------------------
#
# Both build the same header block: every request to a provider they do not
# recognise as their own carries ``X-Session-Id`` and ``x-session-affinity``
# set to the same ``ses_...`` id, and a request to their own provider carries
# ``x-opencode-session`` instead. Kilo additionally forked before that block was
# restored to the newer runner (opencode#43188), so a build of either can send
# no session header at all -- which is what the cache-key fallback is for.

OPENCODE_SESSION = "ses_7f3a9c2e14b8d05a6e1f2c3b4d"


def test_opencode_sends_both_the_canonical_header_and_the_affinity_header() -> None:
    # The canonical header is one of the two, so it wins and the affinity
    # header never decides -- but both name the same session either way.
    identity = session_identity(
        _headers(
            **{
                "X-Session-Id": OPENCODE_SESSION,
                "x-session-affinity": OPENCODE_SESSION,
            }
        ),
    )
    assert identity == SessionIdentity(OPENCODE_SESSION, "x-session-id")


def test_affinity_header_alone_still_names_the_session() -> None:
    # An intermediary that drops one of the pair must not cost the session.
    identity = session_identity(_headers(**{"x-session-affinity": OPENCODE_SESSION}))
    assert identity == SessionIdentity(OPENCODE_SESSION, "x-session-affinity")


def test_opencode_own_provider_header() -> None:
    identity = session_identity(_headers(**{"x-opencode-session": OPENCODE_SESSION}))
    assert identity == SessionIdentity(OPENCODE_SESSION, "x-opencode-session")


def test_parent_session_header_is_not_read_as_the_session() -> None:
    # A subagent request carries its parent's id beside its own. Reading the
    # parent would merge every subagent run into the session that spawned it.
    identity = session_identity(
        _headers(
            **{
                "x-session-affinity": OPENCODE_SESSION,
                "x-parent-session-id": "ses_parent",
            }
        ),
    )
    assert identity == SessionIdentity(OPENCODE_SESSION, "x-session-affinity")


def test_prompt_cache_key_is_read_when_nothing_else_is_declared() -> None:
    body = {"messages": [], "prompt_cache_key": OPENCODE_SESSION}
    assert session_identity(_headers(), body) == SessionIdentity(
        OPENCODE_SESSION, "prompt_cache_key"
    )


def test_prompt_cache_key_loses_to_every_other_source() -> None:
    body = {
        "prompt_cache_key": "cache-key",
        "metadata": {"session_id": "declared", "user_id": CLAUDE_CODE_USER_ID},
    }
    # A body declaration outranks it...
    assert session_identity(_headers(), body) == SessionIdentity("declared", "metadata.session_id")
    # ...as does the composite user id, which is at least session-shaped.
    del body["metadata"]["session_id"]
    assert session_identity(_headers(), body) == SessionIdentity(
        CLAUDE_CODE_SESSION, "metadata.user_id"
    )
    # ...and so does any header.
    assert session_identity(_headers(**{"session-id": "hdr"}), body) == SessionIdentity(
        "hdr", "session-id"
    )


@pytest.mark.parametrize("value", [None, "", "   ", 42, "x" * (MAX_SESSION_ID_CHARS + 1)])
def test_unusable_prompt_cache_key_is_rejected(value: Any) -> None:
    assert session_identity(_headers(), {"prompt_cache_key": value}) is None


def test_prompt_cache_key_survives_consumption() -> None:
    # It is a real OpenAI parameter that raises the provider's cache hit rate,
    # not a declaration aimed at this gateway, so it must reach upstream intact
    # even though the gateway read a session out of it.
    body: dict[str, Any] = {
        "messages": [],
        "prompt_cache_key": OPENCODE_SESSION,
        "metadata": {"session_id": "declared"},
    }
    assert session_identity(_headers(), body) == SessionIdentity("declared", "metadata.session_id")
    consume_session_fields(body)
    assert body == {"messages": [], "prompt_cache_key": OPENCODE_SESSION}
