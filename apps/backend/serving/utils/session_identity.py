"""Which session a request declares it belongs to.

``api_logs.session_id`` -- and the ``(session_id, timestamp DESC)`` index over
it -- exists so one conversation's requests can be pulled out of the firehose,
and RouteWise's prefix-cache cost adjustment scopes its warm entries on the
same value. Both were built around ``X-Session-ID``, the gateway's own header,
and both are empty for exactly the traffic they are most useful on: **no coding
agent sends that header**. A Claude Code session is hundreds of requests, and
every one of them logs ``session_id = NULL``.

Those requests are not anonymous, though. Each agent already carries a session
identifier of its own, in its own idiom, and this module reads whichever idiom
the client used:

* ``X-Session-ID`` -- the gateway's own contract. Wins whenever it is present.
* ``session-id`` / ``thread-id`` request headers -- what Codex CLI stamps on the
  ``/v1/responses`` requests it sends. The underscore spellings are accepted
  beside them: header names may contain underscores, but it is unusual enough
  that intermediaries drop such headers by default (nginx does), so a client
  can sensibly send either.
* ``x-session-affinity`` / ``x-opencode-session`` -- OpenCode and its Kilo Code
  fork. Both already send ``X-Session-Id`` alongside the affinity header on any
  provider they do not recognise as their own, so the canonical source usually
  wins first; these are read because one arriving without the other means the
  session is still knowable.
* ``metadata.session_id`` / ``client_metadata.session_id`` in the request body
  -- a client that declares the session where it declares everything else.
  Codex uses ``client_metadata``.
* ``metadata.user_id`` in the request body -- Claude Code packs three ids into
  that one string (``user_<hash>_account_<uuid>_session_<uuid>``), so the
  trailing ``_session_`` segment is the run.

The source is reported alongside the value and recorded as
``metadata.session_id_source``, because these are not equally strong claims: a
value read out of Claude Code's composite user id was *inferred* from a format
nobody promised us, and an operator looking at a suspicious grouping needs to
know that without re-deriving it.

Trust: every source here is client-declared, ``X-Session-ID`` included. Nothing
is authorized, billed, or rate-limited by a session id -- it labels log rows and
scopes a prefix-cache warm entry that is *already* keyed on the caller's own
affinity key, so one caller's declaration cannot reach another's. What a
declaration can still do is land an unbounded string in an indexed column, so
:func:`normalize_session_id` bounds the length and rejects control characters,
uniformly across sources. The Claude Code parse is stricter still -- it accepts
only an id-shaped trailing segment -- because we inferred that one rather than
being told it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The gateway's own header. Documented, and the only source a client can use
#: on a surface that carries no request body.
CANONICAL_SESSION_HEADER = "X-Session-ID"

#: Headers a coding agent stamps its own run id on, most specific first. Codex
#: CLI sends ``session-id`` and ``thread-id`` on every Responses request; the
#: session names come before the thread/conversation ones because a thread can
#: outlive the run that opened it, and both spellings of each are accepted
#: because header names with underscores, while legal, are dropped by default by
#: some intermediaries (nginx among them) and clients differ over which to send.
_AGENT_SESSION_HEADERS = (
    "x-opencode-session",
    "x-session-affinity",
    "session-id",
    "session_id",
    "thread-id",
    "conversation_id",
)

#: Body objects a client declares its session in, most specific first. Codex
#: puts it in ``client_metadata``; the Anthropic and OpenAI surfaces both define
#: a ``metadata`` map that a client can use for the same purpose.
_BODY_SESSION_OBJECTS = ("metadata", "client_metadata")

#: Longer than any session id a real client mints (a UUID is 36 chars), short
#: enough that a declaration cannot bloat an indexed column. A value over the
#: bound is rejected rather than truncated: truncation would silently merge two
#: sessions that share a prefix, which is worse than recording neither.
MAX_SESSION_ID_CHARS = 128

# Control characters break log rendering and JSON round-tripping, and no client
# means to send them; a value carrying one is malformed rather than long.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Claude Code's composite ``metadata.user_id``:
# ``user_<hash>_account_<uuid>_session_<uuid>``. The *whole* shape has to match,
# not just the ``_session_`` marker: another client's ordinary user id that
# merely contains that substring (``customer_session_internal``) would otherwise
# have its tail read as a session, silently collapsing every such caller into
# one invented group. None of the segment classes admits ``_``, so a segment
# cannot swallow the separator that ends it, and the account segment is allowed
# to be empty (``_account__session_``). The session itself runs to the next
# ``_`` boundary rather than to the end of the string, so a segment appended in
# some future version still yields the session rather than nothing -- but the
# trailing lookahead insists on *a* boundary, because without one the pattern
# also matches a prefix of something else entirely and reports a truncation of
# it: ``user_customer_account_tenant_session_admin@example.com`` would be
# grouped under ``admin``.
_CLAUDE_CODE_USER_ID_RE = re.compile(
    r"^user_[A-Za-z0-9.:-]+_account_[A-Za-z0-9.:-]*"
    r"_session_(?P<sid>[A-Za-z0-9][A-Za-z0-9.:-]*)(?=_|$)"
)


class SessionIdentity(NamedTuple):
    """A declared session id and where the gateway read it from."""

    session_id: str
    source: str


def normalize_session_id(value: Any) -> str | None:
    """Return *value* as a usable session id, or None if it is not one.

    Applied to every source, canonical header included, so one declaration
    cannot be held to a looser standard than another. Surrounding whitespace is
    trimmed; an empty, over-long (see :data:`MAX_SESSION_ID_CHARS`), non-string
    or control-character-bearing value is rejected outright.
    """
    if not isinstance(value, str):
        return None
    session_id = value.strip()
    if not session_id or len(session_id) > MAX_SESSION_ID_CHARS:
        return None
    if _CONTROL_CHARS_RE.search(session_id):
        return None
    return session_id


def _claude_code_session_id(metadata: Mapping[str, Any]) -> str | None:
    """Return the session packed into Claude Code's ``metadata.user_id``.

    Claude Code sends one string carrying the user, the account and the run
    (``user_<hash>_account_<uuid>_session_<uuid>``). Only the value of the
    ``_session_`` segment is read; the rest identifies the *caller*, which the
    gateway already knows from the API key it authenticated. The full composite
    shape is required, so any other client's plain identifier -- including one
    that happens to contain ``_session_`` -- yields None rather than a guess.
    """
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str):
        return None
    match = _CLAUDE_CODE_USER_ID_RE.match(user_id)
    if match is None:
        return None
    return normalize_session_id(match.group("sid"))


def session_identity(
    headers: Mapping[str, str],
    body: Any = None,
) -> SessionIdentity | None:
    """Resolve the session a request declares, or None when it declares none.

    Sources are tried in descending order of how explicit the claim is: the
    gateway's own ``X-Session-ID`` header, then an agent's session header, then
    a session declared in the request body, then the session inferred from
    Claude Code's composite user id. The first usable value wins, so a client
    that sends the documented header is never overridden by something derived.

    A caller that goes on to dispatch ``body`` upstream must pair this with
    :func:`consume_session_fields`; the body declarations are gateway-only.

    Args:
        headers: The request headers. Starlette's ``Headers`` looks names up
            case-insensitively, which is what the header sources rely on.
        body: The decoded JSON request body, when the surface has one. Anything
            that is not a mapping (an embeddings model, a missing body) simply
            skips the body sources.

    Returns:
        The session id and the source it was read from, or None.
    """
    canonical = normalize_session_id(headers.get(CANONICAL_SESSION_HEADER))
    if canonical is not None:
        return SessionIdentity(canonical, CANONICAL_SESSION_HEADER.lower())

    for header in _AGENT_SESSION_HEADERS:
        declared = normalize_session_id(headers.get(header))
        if declared is not None:
            return SessionIdentity(declared, header)

    if not isinstance(body, dict):
        return None

    for field in _BODY_SESSION_OBJECTS:
        container = body.get(field)
        if not isinstance(container, dict):
            continue
        declared = normalize_session_id(container.get("session_id"))
        if declared is not None:
            return SessionIdentity(declared, f"{field}.session_id")

    # Claude Code declares nothing; its run is read out of the composite id it
    # sends as ``metadata.user_id``, so this is the last thing tried.
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        derived = _claude_code_session_id(metadata)
        if derived is not None:
            return SessionIdentity(derived, "metadata.user_id")

    return None


def consume_session_fields(body: Any) -> None:
    """Strip the gateway-only session declarations from a body bound upstream.

    ``metadata.session_id`` and ``client_metadata.session_id`` are declarations
    to *this* gateway, not fields any provider knows: Anthropic's Messages
    metadata admits ``user_id`` alone, and a surface that forwards the client's
    body verbatim would turn a labelled request into an upstream 400. A caller
    that dispatches the body it was handed must therefore consume the
    declaration once it has resolved it -- after whatever copy it logs, so the
    stored payload still shows what the client sent.

    Removing the key is not always enough. When the declaration *was* the whole
    object, the empty container left behind is still a top-level field, and
    ``client_metadata`` is not one Anthropic's Messages API defines -- so
    ``{"client_metadata": {}}`` fails the request exactly as the key would have.
    An emptied container is therefore removed with it.

    A container that still carries something else the client sent is left as it
    is: that part is not this gateway's to consume, and it stands or falls
    upstream just as it did before any of this existed.

    Mutates ``body`` in place. Anything that is not a mapping, and any container
    that does not carry the key, is left untouched.
    """
    if not isinstance(body, dict):
        return
    for field in _BODY_SESSION_OBJECTS:
        container = body.get(field)
        if not isinstance(container, dict) or "session_id" not in container:
            continue
        container.pop("session_id")
        if not container:
            body.pop(field, None)
