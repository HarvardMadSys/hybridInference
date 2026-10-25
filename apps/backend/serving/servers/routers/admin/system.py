"""Admin endpoints for the backend process itself."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from serving.schemas_config import RestartResponse
from serving.servers import restart
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_admin_audit_actor, get_operational_store

if TYPE_CHECKING:
    from serving.audit import AuditActor

router = APIRouter(prefix="/admin")


@router.post("/system/restart", status_code=202, response_model=RestartResponse)
async def restart_backend(
    background_tasks: BackgroundTasks,
    actor: AuditActor = Depends(get_admin_audit_actor),
    op_store=Depends(get_operational_store),
) -> RestartResponse:
    """Exit the process so its supervisor starts it again.

    The 202 is sent first; the shutdown begins once the response is out. It
    names this process (``instance``): the restart is over once ``/health``
    answers with a different one. Answers 409 when nothing would start the
    process again.
    """
    if not restart.restart_supported():
        raise HTTPException(
            status_code=409,
            detail=(
                "This backend cannot restart itself: its deployment does not declare a "
                "supervisor that starts it again (SUPERVISOR_RESTARTS_BACKEND), or several "
                "worker processes serve it. Restart it the way it was started."
            ),
        )
    await log_admin_action(
        op_store, action="system.restart", details={"restarting": True}, actor=actor
    )
    background_tasks.add_task(restart.request_restart)
    return RestartResponse(restarting=True, instance=restart.INSTANCE_ID)
