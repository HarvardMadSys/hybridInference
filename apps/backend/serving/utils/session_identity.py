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
* ``session_id`` / ``conversation_id`` request headers -- what Codex CLI stamps
  on the ``/v1/responses`` requests it sends.
* ``metadata.session_id`` in the request body -- a client that declares the
  session where it declares everything else.
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

#: Headers a coding agent stamps its own run id on. Codex CLI sends both on
#: every Responses request; ``session_id`` is preferred because a conversation
#: can outlive the process that started it.
_AGENT_SESSION_HEADERS = ("session_id", "conversation_id")

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
# some future version still yields the session rather than nothing.
_CLAUDE_CODE_USER_ID_RE = re.compile(
    r"^user_[A-Za-z0-9.:-]+_account_[A-Za-z0-9.:-]*"
    r"_session_(?P<sid>[A-Za-z0-9][A-Za-z0-9.:-]*)"
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

    metadata = body.get("metadata") if isinstance(body, dict) else None
    if not isinstance(metadata, dict):
        return None

    declared = normalize_session_id(metadata.get("session_id"))
    if declared is not None:
        return SessionIdentity(declared, "metadata.session_id")

    derived = _claude_code_session_id(metadata)
    if derived is not None:
        return SessionIdentity(derived, "metadata.user_id")

    return None
