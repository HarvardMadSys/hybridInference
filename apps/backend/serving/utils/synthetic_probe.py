"""Trust policy for the ``X-Probe: synthetic`` request header.

The header lets deployment-owned monitors mark their traffic so it does not
pollute request logs, dashboards, RouteWise's online-learning observations, or
the log level of the request line — and so probes can read the ``X-Provider``
response header to tell which backend answered.

It is still just a request header, which anyone can send. Every consumer must
therefore split two questions the original implementation conflated:

* *was the marker sent?* — :func:`probe_header_present`; and
* *is this caller allowed to mean it?* — :func:`is_trusted_probe_caller`.

A trusted probe caller is an **authenticated** internal/admin key that is not
an agent-sandbox credential:

* ``authenticated`` — a real key was presented and resolved. Role alone is
  never enough: with auth disabled, ``verify_api_key`` hands every anonymous
  caller ``role="admin"``, so an auth-disabled deployment has *no* caller
  whose marker is honoured. That is deliberate (fail-closed): "auth is off"
  means the API is open, not that an anonymous caller holds a verifiable
  monitor identity. A deployment that wants probes without auth needs an
  explicit mechanism (a probe secret, a source allowlist) — not this header.
* internal/admin — the deployment's own monitors run with these roles; a
  free/pro key must not be able to opt itself out of anything.
* not a grant — a grant context carries its *owner's* role while the requests
  are authored by sandboxed agent code, exactly the caller that must not
  self-mark.

Cost and quota are deliberately **not** part of what the marker controls, for
trusted and untrusted callers alike: billing is unconditional on every
surface (see the cost increments in ``embeddings.py`` / ``completions.py`` /
``completions_stream.py``). The marker affects noise, never money.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.config.settings import has_role

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import Request

PROBE_HEADER = "x-probe"
PROBE_HEADER_VALUE = "synthetic"


def probe_header_present(request: Request) -> bool:
    """Return whether the request carries the synthetic-probe marker at all."""
    return request.headers.get(PROBE_HEADER, "").lower() == PROBE_HEADER_VALUE


def is_trusted_probe_caller(user_ctx: Mapping[str, Any] | None) -> bool:
    """Return whether *user_ctx* may have its probe marker honoured.

    Accepts the full ``verify_api_key`` context as well as the slimmer
    ``{user_id, role, authenticated}`` shape the rejection log carries.
    ``None`` — no resolved caller — is never trusted.
    """
    if not user_ctx:
        return False
    if user_ctx.get("agent_grant_id") or user_ctx.get("agent_job_id"):
        return False
    if not has_role(user_ctx.get("role") or "free", "internal"):
        return False
    return bool(user_ctx.get("authenticated"))


def is_trusted_probe(request: Request, user_ctx: Mapping[str, Any] | None) -> bool:
    """Marker present *and* the caller is allowed to mean it."""
    return probe_header_present(request) and is_trusted_probe_caller(user_ctx)
