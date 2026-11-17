"""The release's calls into a gateway process it hands traffic away from.

``GET /internal/handoff`` reports the process's handoff state and everything it
still has under way (:func:`serving.servers.handoff.status`); ``POST`` to
``/drain``, ``/resume`` and ``/seal`` move it (:mod:`serving.servers.handoff`).
A release calls them on one process, by its own address, not through the
deployment's public entry: the path is in no public path list, so neither the
console nor an entry split forwards it.

They answer only to ``Authorization: Bearer <HANDOFF_TOKEN>``, a process
environment variable of at least :data:`MIN_TOKEN_LENGTH` characters. Without
one the routes do not exist (404): a deployment that does not release this way
has nothing to protect. It is a credential of its own, not the administrator
token, because the release is a machine and the administrator token is
read-only for changes wherever administrators need a second factor.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from serving.servers import handoff
from serving.servers.deps import get_services
from serving.utils.logging import get_logger

router = APIRouter(prefix=handoff.HANDOFF_PATH, include_in_schema=False)
logger = get_logger(__name__)

#: The variable holding the release's credential.
TOKEN_ENV = "HANDOFF_TOKEN"
#: A shorter value is not accepted: the routes stay absent.
MIN_TOKEN_LENGTH = 32

_warned_short = False


def _token() -> str | None:
    global _warned_short
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        if not _warned_short:
            _warned_short = True
            logger.error(
                "%s is shorter than %d characters; the handoff routes stay disabled",
                TOKEN_ENV,
                MIN_TOKEN_LENGTH,
            )
        return None
    return token


async def require_release(authorization: str | None = Header(None)) -> None:
    """Admit the release's credential; 404 while none is configured, 401 for anything else."""
    token = _token()
    if token is None:
        raise HTTPException(status_code=404, detail="Not Found")
    given = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not given or not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Invalid handoff credential")


def _refuse_if_unsupported() -> None:
    reason = handoff.refusal_to_drain()
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)


@router.get("", dependencies=[Depends(require_release)])
async def handoff_status(services: Any = Depends(get_services)) -> dict[str, Any]:
    """The state, what is left, and whether a seal would succeed now."""
    return handoff.status(services)


@router.post("/drain", dependencies=[Depends(require_release)])
async def handoff_drain(services: Any = Depends(get_services)) -> dict[str, Any]:
    """Stop taking work meant for the deployment; keep serving what arrives."""
    _refuse_if_unsupported()
    changed = handoff.drain(services, by="release")
    return {"changed": changed, **handoff.status(services)}


@router.post("/resume", dependencies=[Depends(require_release)])
async def handoff_resume(services: Any = Depends(get_services)) -> dict[str, Any]:
    """Serve normally again (the release is abandoned)."""
    changed = handoff.resume(services, by="release")
    return {"changed": changed, **handoff.status(services)}


@router.post("/seal", dependencies=[Depends(require_release)], response_model=None)
async def handoff_seal(services: Any = Depends(get_services)) -> Any:
    """Seal when nothing is left (200); otherwise 409 with what is left, still draining."""
    _refuse_if_unsupported()
    found = handoff.seal(services, by="release")
    body = handoff.status(services)
    if found:
        return JSONResponse(status_code=409, content={**body, "blocking": found})
    return body
