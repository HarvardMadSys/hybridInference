r"""Redirect a trailing slash on ``/v1`` and ``/anthropic`` the way the console does.

Behind the console, ``/v1/models/`` never reaches the gateway: the console's
``redirects()`` (``apps/frontend/next.config.js``) answers it with a ``308``
whose ``Location`` is the bare path ``/v1/models``. A deployment's entry may
instead send these two prefixes straight to the gateway (the ``entry_split``
paths of ``deploy/public-paths.json``), and then the gateway answers. Left to
Starlette, it answers ``307`` with an absolute ``Location`` built from the
request: the ``Host`` the entry sent, which may be an internal name, and the
scheme the gateway saw, which is ``http`` behind a TLS-terminating entry
(uvicorn runs with ``--no-proxy-headers``). A client following it leaves
HTTPS, or is sent to an address that resolves nowhere.

So on those prefixes this middleware answers as the console does: ``308``,
keeping the method and body as ``307`` does, to the same path without its
trailing slashes and with the query string, as a bare path the client resolves
against the URL it asked for. It decides before routing, like the console,
so a path that ends in a slash is redirected whether or not a route serves the
path without it.

It leaves alone anything a bare-path ``Location`` could carry somewhere else or
back to itself: a path beginning ``//`` or ``/\`` (a client reads that as
another host), one with a ``.`` or ``..`` segment, and one whose slash was sent
percent-encoded. Those, and every other prefix, reach the application as
before.

Registered outside the body limit, so a redirected request is never refused
for its size -- the console redirects before it reads a byte too -- and inside
CORS, so a browser can read the redirect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

#: The prefixes an entry may send straight to the gateway: the ``entry_split``
#: entries of ``deploy/public-paths.json``, which a test holds this to.
ENTRY_SPLIT_PREFIXES: tuple[str, ...] = ("/v1", "/anthropic")


def _under_split_prefix(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in ENTRY_SPLIT_PREFIXES)


def slashless_location(scope: Scope) -> bytes | None:
    """The ``Location`` to redirect this request to, or None to serve it as it is."""
    path: str = scope["path"]
    if len(path) < 2 or not path.endswith("/") or path[1] in "/\\":
        return None
    bare = path.rstrip("/")
    if not _under_split_prefix(bare):
        return None
    if any(segment in {".", ".."} for segment in bare.split("/")):
        return None
    raw: bytes = scope.get("raw_path") or path.encode("utf-8")
    if not raw.endswith(b"/"):
        return None
    location = raw.rstrip(b"/")
    if location[:1] != b"/" or location[1:2] in (b"/", b"\\"):
        return None
    query: bytes = scope.get("query_string") or b""
    return location + b"?" + query if query else location


class TrailingSlashRedirectMiddleware:
    """Answer a trailing slash on the entry-split prefixes with a bare-path 308."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Redirect a slash-terminated split-prefix path; pass anything else on."""
        location = slashless_location(scope) if scope["type"] == "http" else None
        if location is None:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 308,
                "headers": [(b"location", location), (b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
