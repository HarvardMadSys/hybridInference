"""Gateway-owned recovery for terminal sessions left behind by settled jobs."""

from __future__ import annotations

from serving.agent_jobs.workspace_broker_client import (
    WorkspaceBrokerError,
    workspace_broker_from_env,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)


async def resume_settled_terminal(job_id: str) -> bool:
    """Authoritatively resume one settled workspace, reporting confirmation."""
    broker = workspace_broker_from_env()
    if broker is None:
        return True
    try:
        await broker.resume_settled_terminals(job_id)
    except WorkspaceBrokerError:
        logger.warning(
            "agent_terminal_settled_resume_failed",
            exc_info=True,
            extra={
                "event": "agent_terminal_settled_resume_failed",
                "job_id": job_id,
            },
        )
        return False
    return True
