"""First-run setup endpoints (``/auth/setup/*``).

Public, because no account exists yet: what authorizes creating the first
administrator is the one-time setup code the backend prints in its startup
log (see :mod:`serving.setup_state`). The administrator has a login name and
no email address; success signs them in exactly as ``POST /auth/login`` does.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from serving.schemas_auth import LoginResponse, SetupAdminRequest, SetupStatusResponse
from serving.servers.deps import database_enabled, get_operational_store
from serving.servers.routers.auth_routes import start_user_session
from serving.setup_state import complete_setup, refresh_setup_state, verify_setup_code
from serving.utils import password as password_utils
from serving.utils.jwt import generate_ulid
from serving.utils.logging import get_logger
from serving.utils.login_rate_limit import record_failed_setup_attempt
from serving.utils.request_ip import get_client_ip

router = APIRouter(prefix="/auth/setup", tags=["setup"])
logger = get_logger(__name__)

_SETUP_COMPLETE_DETAIL = "This deployment has already been set up."


@router.get("/status", response_model=SetupStatusResponse)
async def setup_status() -> SetupStatusResponse:
    """Report whether the console should show the first-run setup page."""
    return SetupStatusResponse(
        setup_required=await refresh_setup_state(),
        database_enabled=database_enabled(),
    )


@router.post("/admin", response_model=LoginResponse)
async def create_setup_admin(
    request: Request,
    response: Response,
    body: SetupAdminRequest,
    op_store=Depends(get_operational_store),
) -> LoginResponse:
    """Create the deployment's first administrator and sign them in.

    The account is an active, verified ``admin`` with ``body.login_name`` and
    no email address; its display name is ``display_name`` or the login name.
    Creation, the setup marker and the ``setup.admin_created`` audit row are
    one transaction that re-checks setup is still pending, so of two
    concurrent requests with the right code exactly one succeeds.

    Errors: 409 setup already complete, 403 wrong setup code, 422 invalid
    input (by field), 429 a wrong code from an IP that already sent ten in
    15 minutes, 503 no database.
    """
    if op_store is None:
        raise HTTPException(
            status_code=503, detail="First-run setup needs a database, and none is available."
        )

    if not await refresh_setup_state():
        raise HTTPException(status_code=409, detail=_SETUP_COMPLETE_DETAIL)

    client_ip = get_client_ip(request)
    # The code is checked before the limiter, and only failures count: behind
    # the console's proxy every browser can share one client address, so a
    # limiter that counted every attempt would let a stranger sending junk
    # codes keep the operator's correct one out indefinitely. The code's 60
    # random bits, not the limiter, are what make guessing it hopeless.
    if not verify_setup_code(body.setup_code):
        if not await record_failed_setup_attempt(client_ip):
            raise HTTPException(
                status_code=429,
                detail="Too many wrong setup codes. Please try again later.",
                headers={"Retry-After": "900"},
            )
        logger.warning("First-run setup attempt with a wrong setup code from %s", client_ip)
        raise HTTPException(
            status_code=403,
            detail="That setup code is not valid. Copy the code from the backend's startup log.",
        )

    user_id = generate_ulid()
    created = await complete_setup(
        op_store,
        user_id=user_id,
        login_name=body.login_name,
        password_hash=password_utils.hash_password(body.password),
        user_name=body.display_name or body.login_name,
        admin_ip=client_ip,
    )
    if not created:
        raise HTTPException(status_code=409, detail=_SETUP_COMPLETE_DETAIL)
    logger.info("First-run setup created administrator %s (%s)", user_id, body.login_name)

    await op_store.update_user_last_login(user_id)
    user_row = await op_store.get_user_by_id(user_id)
    if user_row is None:  # pragma: no cover - the row was committed just above
        raise HTTPException(status_code=500, detail="The administrator account was not found.")
    return await start_user_session(op_store, response, user_row, user_role="admin")
