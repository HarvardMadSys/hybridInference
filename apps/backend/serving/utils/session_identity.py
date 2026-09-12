"""Which session a request declares it belongs to.

``api_logs.session_id`` -- and the ``(session_id, timestamp DESC)`` index over
it -- exists so one conversation's requests can be pulled out of the firehose,
and RouteWise's prefix-cache cost adjustment scopes its warm entries on the
same value. Both were built around ``X-Session-ID``, the gateway's own header,
and both were empty for exactly the traffic they are most useful on, because no
coding agent sent that header. Some now send one of their own -- Claude Code
sends ``x-claude-code-session-id`` -- but never the gateway's. A Claude Code
session is hundreds of requests, and every one of them logged
``session_id = NULL``.

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
* ``x-claude-code-session-id`` -- Claude Code's own header, a bare UUID it
  sends on every request. Purpose-built for this and needing no inference, so
  it is the strongest source after the gateway's own header.
* ``metadata.user_id`` in the request body -- Claude Code packs the device, the
  account and the run into this one Anthropic field. Two shapes: current
  clients (>= 2.1.78) send a JSON object whose ``session_id`` member is the
  run, and older ones sent ``user_<hash>_account_<uuid>_session_<uuid>``, where
  the trailing ``_session_`` segment was the run. Both are read; the source
  says which.

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

import json
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
    "x-claude-code-session-id",
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

#: Bound on Claude Code's ``metadata.user_id`` *container* before it is decoded
#: -- distinct from :data:`MAX_SESSION_ID_CHARS`, which bounds the id read out
#: of it. Claude Code caps its own serialized metadata at 512 bytes and a real
#: value measures ~190, so this is generous for every shape it sends. It exists
#: because the field is client-controlled and ``json.loads`` recurses per level
#: of nesting: without a bound, a deeply nested value raises ``RecursionError``,
#: which is not a ``ValueError`` and would escape as a 500. At this length the
#: reachable depth is far under the interpreter's limit; the decode below also
#: catches ``RecursionError`` directly, so neither guard stands alone.
MAX_USER_ID_CHARS = 1024

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


def _claude_code_session_id(metadata: Mapping[str, Any]) -> SessionIdentity | None:
    """Return the session Claude Code packs into ``metadata.user_id``.

    Two formats, because Claude Code changed shape in 2.1.78 (2026-03-17) and
    both are still in the wild. The current one is a JSON object::

        {"device_id": "...", "account_uuid": "...", "session_id": "..."}

    read by name, which is robust to the key order (an operator's
    ``CLAUDE_CODE_EXTRA_METADATA`` keys are serialized ahead of the canonical
    ones) and to optional members. The retired one packed the same three ids
    into one underscore-delimited string, and is still parsed for older clients.

    Which format answered is reported as the source, because they are not
    equally strong claims and an operator looking at a grouping needs to know
    which parse produced it without re-deriving it.

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

    # Current Claude Code (>= 2.1.78) sends a JSON object, so the value starts
    # with ``{`` and the composite regex below can never match it. Try the JSON
    # shape first: it is the one live clients send, and it names its session
    # rather than positioning it.
    candidate = user_id.strip()
    if candidate.startswith("{"):
        if len(candidate) > MAX_USER_ID_CHARS:
            return None
        try:
            parsed = json.loads(candidate)
        except (ValueError, RecursionError):
            return None
        if not isinstance(parsed, dict):
            return None
        # The Claude markers are required, not just a ``session_id`` key. This
        # field is Anthropic's *user* identifier, and another client is free to
        # use a JSON object as its user identity; one that happened to carry a
        # ``session_id`` member -- ``{"session_id": "tenant-plan"}`` -- would
        # otherwise collapse every request behind that value into one invented
        # session. The legacy branch below insists on its whole composite shape
        # for exactly this reason, and this branch has to hold the same line.
        # Presence, not content: ``account_uuid`` is the empty string when the
        # client authenticated with an API key rather than an account.
        if "device_id" not in parsed or "account_uuid" not in parsed:
            return None
        # ``session_id`` only. ``parent_session_id``, present on a subagent
        # run, names the session that spawned this one -- reading it would
        # merge every subagent into its parent, the same trap as OpenCode's
        # ``x-parent-session-id``.
        declared = normalize_session_id(parsed.get("session_id"))
        if declared is None:
            return None
        return SessionIdentity(declared, "metadata.user_id.session_id")

    match = _CLAUDE_CODE_USER_ID_RE.match(user_id)
    if match is None:
        return None
    derived = normalize_session_id(match.group("sid"))
    if derived is None:
        return None
    return SessionIdentity(derived, "metadata.user_id")


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
            return derived

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
