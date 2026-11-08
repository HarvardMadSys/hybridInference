"""The internal API the cloud agent calls is actually routed to the backend.

The Cloudflare tunnel routes every public path to the frontend container, so a
backend path reaches FastAPI only if the console forwards it: through a route
handler under `apps/frontend/src/app`, or a rewrite in `next.config.js`. Every route
the control plane calls was missing from that list, and the failure is silent
in the worst way: the caller gets a 404 whose body is an HTML page, which reads
as "the gateway is down" rather than "this path is not forwarded". Verified
against both live deployments before this was fixed — `/internal/verify-admin`
answered the gateway's JSON 401 while `/internal/model-catalog`, on the same
host, answered the frontend's HTML.

A route existing and a route being reachable are different facts, and every
test in this repository asserted the first one.

The route list comes from the routers themselves, so a route added later is
checked without anyone adding it here.
"""

from __future__ import annotations

import re
from pathlib import Path

from serving.servers.routers.agent_grants import router as grants_router
from serving.servers.routers.internal_lookups import router as lookups_router

FRONTEND = Path(__file__).resolve().parents[2] / "apps" / "frontend"
NEXT_CONFIG = FRONTEND / "next.config.js"
APP_DIR = FRONTEND / "src" / "app"

#: `source: '/internal/...'` in the rewrites block.
_SOURCE = re.compile(r"source:\s*'([^']+)'")

#: Matches any single path segment: FastAPI's `{user_id}` and Next's `:userId`.
_PARAM = "\x00param"
#: Matches every remaining segment: Next's `:path*`.
_REST = "\x00rest"


def _normalize(path: str) -> list[str]:
    """Reduce a path template to comparable segments.

    Parameter *names* differ between the two systems by convention — FastAPI
    uses snake_case, Next.js camelCase — and comparing them would fail on a
    difference that changes nothing about which requests match.
    """
    segments = []
    for segment in path.strip("/").split("/"):
        if segment.startswith(":") and segment.endswith("*"):
            segments.append(_REST)
        elif segment.startswith(":") or (segment.startswith("{") and segment.endswith("}")):
            segments.append(_PARAM)
        else:
            segments.append(segment)
    return segments


def _covers(source: list[str], route: list[str]) -> bool:
    """Whether a rewrite source forwards a route's path."""
    for index, expected in enumerate(source):
        if expected == _REST:
            # `:path*` matches zero or more remaining segments, so a source
            # ending in one covers the prefix itself as well.
            return True
        if index >= len(route):
            return False
        if expected != _PARAM and expected != route[index]:
            return False
    return len(source) == len(route)


def _rewrite_source_templates() -> list[str]:
    """The `source` of every rewrite; `redirects()` and `headers()` come first and are skipped."""
    text = NEXT_CONFIG.read_text()
    return _SOURCE.findall(text[text.index("async rewrites()") :])


def _route_file_templates() -> list[str]:
    """Each route handler under the console's app directory, as a path template."""
    templates = []
    for route in APP_DIR.rglob("route.ts"):
        segments = []
        for part in route.parent.relative_to(APP_DIR).parts:
            if part.startswith("(") and part.endswith(")"):
                continue  # A route group is not part of the URL.
            if part.startswith("[[...") or part.startswith("[..."):
                segments.append(":path*")
            elif part.startswith("[") and part.endswith("]"):
                segments.append(f":{part[1:-1]}")
            else:
                segments.append(part)
        templates.append("/" + "/".join(segments))
    return templates


def _published_templates() -> list[str]:
    return _rewrite_source_templates() + _route_file_templates()


def _rewrite_sources() -> list[list[str]]:
    return [_normalize(template) for template in _published_templates()]


def _internal_paths() -> list[str]:
    paths = set()
    for router in (grants_router, lookups_router):
        for route in router.routes:
            # Already absolute: FastAPI applies the router's prefix when the
            # route is registered, so prepending it again doubles it — and the
            # doubled path still matched the grants wildcard, which is how that
            # mistake survived its first run here.
            paths.add(getattr(route, "path_format", route.path))
    return sorted(paths)


def test_every_internal_route_is_forwarded_by_the_frontend() -> None:
    """A route the control plane calls does not stop at the Next.js 404 page."""
    sources = _rewrite_sources()
    unreachable = [
        path for path in _internal_paths() if not any(_covers(s, _normalize(path)) for s in sources)
    ]
    assert not unreachable, (
        "these reach the frontend and get its HTML 404 instead of the backend; "
        f"add a route handler under apps/frontend/src/app: {unreachable}"
    )


def test_the_shared_internal_prefix_is_not_forwarded_wholesale() -> None:
    """Neither a rewrite nor a route handler forwards `/internal` blindly.

    `/internal` also carries `verify-admin`, which authenticates a browser
    session by cookie. A `/internal/:path*` rewrite would forward whatever is
    added under this prefix next, without anyone deciding it should be
    reachable from outside — the opposite mistake to the one above, and the
    easy way to "fix" a failure of the first test.
    """
    blanket = [
        source for source in _published_templates() if _normalize(source)[:2] == ["internal", _REST]
    ]
    assert not blanket, f"these forward the whole /internal prefix: {blanket}"


def test_every_internal_route_carries_the_dispatch_token() -> None:
    """Being reachable is only safe while every route is still guarded.

    The routers declare the dependency once rather than route by route, which
    is what makes forwarding the whole grants prefix safe. This asserts the
    property that forwarding relies on, so removing the router-level dependency
    fails here rather than quietly publishing an open endpoint.
    """
    for router in (grants_router, lookups_router):
        names = [dependency.dependency.__name__ for dependency in router.dependencies]
        assert "require_dispatch_token" in names, (
            f"{router.prefix} no longer guards every route at the router level, "
            "but its paths are forwarded from the public origin"
        )
