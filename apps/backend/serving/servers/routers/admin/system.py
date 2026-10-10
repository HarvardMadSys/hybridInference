"""Admin endpoints for the backend process itself."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from serving.schemas_config import RestartResponse
from serving.servers import restart
from serving.servers.auth import log_admin_action
from serving.servers.deps import get_operational_store, verify_admin_access

router = APIRouter(prefix="/admin")


@router.post("/system/restart", status_code=202, response_model=RestartResponse)
async def restart_backend(
    background_tasks: BackgroundTasks,
    admin_id: str = Depends(verify_admin_access),
    op_store=Depends(get_operational_store),
) -> RestartResponse:
    """Exit the process so its supervisor starts it again.

    The 202 is sent first; the shutdown begins once the response is out.
    Answers 409 when nothing would start the process again.
    """
    if not restart.restart_supported():
        raise HTTPException(
            status_code=409,
            detail=(
                "This backend cannot restart itself: it is not running under Docker or "
                "systemd. Restart it the way it was started."
            ),
        )
    await log_admin_action(op_store, admin_id, "system.restart", None, {"restarting": True})
    background_tasks.add_task(restart.request_restart)
    return RestartResponse(restarting=True)
