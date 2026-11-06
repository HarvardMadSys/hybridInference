"""X-FI-* served-identity response headers (issue #1504).

The gateway resolves the served adapter, endpoint and provider before it
dispatches, but that identity never reached the caller: ``owned_by`` is only a
provider label, and the small-budget Anthropic reroute can serve a different
model than the one the client named. Four response headers carry the identity
that dispatch already knows:

``X-FI-Requested-Model``  the model string the client sent
``X-FI-Served-Model``     the model that actually served (post-alias/reroute)
``X-FI-Served-Endpoint``  the sanitized logical endpoint id
``X-FI-Served-Provider``  the provider label of the serving adapter

An endpoint id is emitted to trusted diagnostic callers only when registration
retained publishability metadata and it is exactly ``{served_model}:{location}``.
A safe-looking suffix is not proof that its origin is public. The api_logs
columns keep the full id either way.

Every served-* field is optional: auto-selection deliberately conceals which
model serves an auto request (the response names the auto model, and so do
error messages), and that concealment extends to these headers — callers
pass only the requested model there.
"""

from __future__ import annotations

import re
from typing import Any

from serving.utils.synthetic_probe import (
    is_trusted_probe_headers,
)

REQUESTED_MODEL_HEADER = "X-FI-Requested-Model"
SERVED_MODEL_HEADER = "X-FI-Served-Model"
SERVED_ENDPOINT_HEADER = "X-FI-Served-Endpoint"
SERVED_PROVIDER_HEADER = "X-FI-Served-Provider"
SERVED_IDENTITY_OPT_IN_HEADER = "X-FI-Served-Identity"
SERVED_IDENTITY_OPT_IN_VALUE = "include"
SERVED_IDENTITY_DIAGNOSTIC_ENTITLEMENT = "served_identity"
SERVED_IDENTITY_HEADER_NAMES = frozenset(
    (REQUESTED_MODEL_HEADER, SERVED_MODEL_HEADER, SERVED_ENDPOINT_HEADER, SERVED_PROVIDER_HEADER)
)

# Location grammar is only a final format check. The separately retained
# registry publishability bit is the authority for whether a location may be
# disclosed at all.
_SAFE_LOCATION_RE = re.compile(r"^(?:local(?:-\d+)?|[a-z][a-z0-9]*-api)$")


def should_include_served_identity_headers(headers: Any, user_ctx: Any) -> bool:
    """Authorize identity headers for a trusted probe or opted-in diagnostic call.

    The caller entitlement is derived only from the authenticated server-side
    context's explicit diagnostic entitlements, excluding agent grants. A
    request header can express per-call intent, but cannot grant that
    entitlement. Synthetic probes retain their existing marker and policy.
    """
    if is_trusted_probe_headers(headers, user_ctx):
        return True
    if not user_ctx or not user_ctx.get("authenticated"):
        return False
    if user_ctx.get("agent_grant_id") or user_ctx.get("agent_job_id"):
        return False
    entitlements = user_ctx.get("diagnostic_entitlements")
    if not isinstance(entitlements, (set, frozenset, tuple, list)):
        return False
    if SERVED_IDENTITY_DIAGNOSTIC_ENTITLEMENT not in entitlements:
        return False
    return (
        headers.get(SERVED_IDENTITY_OPT_IN_HEADER, "").strip().lower()
        == SERVED_IDENTITY_OPT_IN_VALUE
    )


def header_endpoint_id(
    endpoint_id: str | None,
    served_model: str | None,
    *,
    publishable: bool = False,
) -> str | None:
    """Sanitized endpoint identity for a trusted diagnostic response header.

    The api_logs column may carry a LAN or cluster-DNS stamp the registry
    derived from base_url, or an admin-supplied route_id kept verbatim; a
    response header must not. Trust here is anchored in ``served_model`` —
    the canonical model name the response's own ``X-FI-Served-Model`` header
    carries, resolved through the registry — not in any lexical property of
    ``endpoint_id``: the identifier is republished only when it is exactly
    ``{served_model}:{location}`` and the location is one of the registry's
    sanitized shapes. Anything else returns ``None`` so the endpoint header
    is omitted entirely.

    A location that merely looks registry-minted grants nothing: an endpoint
    id like ``private.internal:local-12003`` ends in a legitimate location,
    but the prefix is untrusted input, so the whole identifier degrades.
    A request that names an alias whose canonical id differs from the
    endpoint's prefix degrades the same way — the header set stays
    internally consistent with the model the caller was told about.
    """
    if not publishable or not endpoint_id or not served_model:
        return None
    prefix, sep, location = endpoint_id.rpartition(":")
    if sep and prefix == served_model and _SAFE_LOCATION_RE.fullmatch(location):
        return endpoint_id
    return None


def _latin1_header_value(value: str | None) -> str | None:
    """Return an HTTP-header-safe Latin-1 value, or omit it."""
    if not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return value


def served_identity_headers(
    *,
    requested_model: str | None = None,
    served_model: str | None = None,
    endpoint_id: str | None = None,
    provider: str | None = None,
    endpoint_publishable: bool = False,
) -> dict[str, str]:
    """Build the X-FI-* header set; a field is omitted when its value is None.

    Auto-selection's concealment of the served model is expressed by passing
    only ``requested_model`` — the served endpoint and provider name the same
    model the concealment hides, so they are withheld with it.
    """
    headers: dict[str, str] = {}
    for header, value in (
        (REQUESTED_MODEL_HEADER, requested_model),
        (SERVED_MODEL_HEADER, served_model),
        (SERVED_PROVIDER_HEADER, provider),
    ):
        safe_value = _latin1_header_value(value)
        if safe_value is not None:
            headers[header] = safe_value
    endpoint = header_endpoint_id(endpoint_id, served_model, publishable=endpoint_publishable)
    safe_endpoint = _latin1_header_value(endpoint)
    if safe_endpoint is not None:
        headers[SERVED_ENDPOINT_HEADER] = safe_endpoint
    return headers


def copy_served_identity_headers(headers: Any) -> dict[str, str]:
    """Copy only the opt-in identity headers from a delegated response."""
    return {
        name: value
        for name in SERVED_IDENTITY_HEADER_NAMES
        if (value := headers.get(name)) is not None
    }
