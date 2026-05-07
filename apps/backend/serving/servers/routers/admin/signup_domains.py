"""Admin signup-domain allowlist endpoints."""

from __future__ import annotations

import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from serving.auth.signup_policy import invalidate_allowlist_cache
from serving.schemas_admin import (
    AddSignupAllowedDomainRequest,
    ListSignupAllowedDomainsResponse,
    SignupAllowedDomain,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/admin")


# Domain label charset; matches RFC-1035 LDH plus the dot separator. Each
# label must start and end with an alphanumeric character (no leading or
# trailing hyphens), with optional alphanumeric/hyphen characters in between.
# The TLD must be at least two alpha-only characters.
_DOMAIN_RE = re.compile(r"^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def _normalize_signup_domain(raw: str) -> tuple[str, bool]:
    """Validate and normalize an admin-supplied signup domain entry.

    Strips whitespace, lowercases, peels a leading ``*.`` to flag wildcard
    intent, and enforces the LDH-plus-dot syntax. Raises ``HTTPException(400)``
    on any validation failure.

    Returns ``(domain_without_prefix, is_wildcard)``.
    """
    cleaned = (raw or "").strip().lower()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Domain is required")

    is_wildcard = False
    if cleaned.startswith("*."):
        is_wildcard = True
        cleaned = cleaned[2:]

    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail="Wildcard entry requires a suffix after '*.' (e.g. *.example.com).",
        )

    # Reject any remaining wildcard / sentinel chars or whitespace.
    for bad in ("*", "@", " ", "\t"):
        if bad in cleaned:
            raise HTTPException(
                status_code=400,
                detail="Domain must not contain '*', '@', or whitespace.",
            )

    if not _DOMAIN_RE.match(cleaned):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid domain format. Use e.g. 'example.com' or "
                "'*.example.com' (lowercase letters, digits, hyphens; "
                "TLD at least 2 letters)."
            ),
        )

    return cleaned, is_wildcard


def _signup_domain_to_schema(row: dict[str, Any]) -> SignupAllowedDomain:
    """Convert a store row to the response schema."""
    return SignupAllowedDomain(
        domain=row["domain"],
        is_wildcard=bool(row.get("is_wildcard")),
        created_at=row.get("created_at"),
        created_by=row.get("created_by"),
        created_by_email=row.get("created_by_email"),
    )


@router.get("/signup-domains", response_model=ListSignupAllowedDomainsResponse)
async def list_signup_allowed_domains_endpoint(
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> ListSignupAllowedDomainsResponse:
    """List all allowed signup domains.

    Empty list means all signups auto-approve. Otherwise only listed
    domains (exact match or ``*.suffix`` wildcard) auto-approve; everyone
    else lands in ``pending_approval``.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")
    rows = await op_store.list_signup_allowed_domains()
    return ListSignupAllowedDomainsResponse(domains=[_signup_domain_to_schema(r) for r in rows])


@router.post(
    "/signup-domains",
    response_model=SignupAllowedDomain,
    status_code=201,
)
async def add_signup_allowed_domain_endpoint(
    request: Request,
    payload: AddSignupAllowedDomainRequest,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> SignupAllowedDomain:
    r"""Add a domain (or ``*.subdomain`` wildcard) to the signup allowlist.

    Validation: strip + lowercase, ``*.`` prefix flips ``is_wildcard``,
    remainder must match ``^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$``
    (each label must start and end with an alphanumeric character).

    Returns 409 if the (domain, is_wildcard) composite key already exists.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    domain, is_wildcard = _normalize_signup_domain(payload.domain)

    # Resolve admin user id when JWT auth was used (admin_id is the email
    # in that case). Fall back to None for ADMIN_TOKEN where there's no
    # corresponding users row.
    created_by: str | None = None
    user_row = await op_store.get_user_by_email(admin_id) if "@" in admin_id else None
    if user_row:
        created_by = user_row["id"]

    # Translate dup-key violations to 409. We rely solely on asyncpg's typed
    # UniqueViolationError so unrelated DB errors (FK violations, syntax
    # errors that happen to mention the word "constraint", etc.) surface
    # as 500 instead of being silently masked as duplicates.
    try:
        row = await op_store.add_signup_allowed_domain(
            domain=domain,
            is_wildcard=is_wildcard,
            created_by=created_by,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Domain '{domain}' "
                f"({'wildcard' if is_wildcard else 'exact'}) is already on the allowlist."
            ),
        ) from exc

    invalidate_allowlist_cache()

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "signup_domain.add",
        None,
        {"domain": domain, "is_wildcard": is_wildcard},
    )

    return _signup_domain_to_schema(row)


@router.delete("/signup-domains/{domain}", status_code=204)
async def remove_signup_allowed_domain_endpoint(
    request: Request,
    domain: str,
    wildcard: bool = Query(False, description="True iff removing a *.suffix entry"),
    _admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> Response:
    """Remove a domain from the signup allowlist.

    The ``wildcard`` query param disambiguates the composite key: an
    entry added as ``example.com`` (exact) and ``*.example.com``
    (wildcard) coexist as two rows. Pass ``wildcard=true`` to delete
    the wildcard row, ``wildcard=false`` (default) for the exact row.

    Returns 204 on success, 404 if the row doesn't exist.

    Requires: Admin authentication (JWT or ADMIN_TOKEN)
    """
    if not op_store:
        raise HTTPException(500, "Database not configured")

    normalized = (domain or "").strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="Domain is required")

    removed = await op_store.remove_signup_allowed_domain(
        domain=normalized,
        is_wildcard=bool(wildcard),
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Domain '{normalized}' "
                f"({'wildcard' if wildcard else 'exact'}) is not on the allowlist."
            ),
        )

    invalidate_allowlist_cache()

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "signup_domain.remove",
        None,
        {"domain": normalized, "is_wildcard": bool(wildcard)},
    )

    return Response(status_code=204)
