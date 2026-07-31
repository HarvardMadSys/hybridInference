"""Gateway-owned recovery for terminal sessions left behind by settled jobs."""

from __future__ import annotations

from serving.agent_jobs.workspace_broker_client import (
    WorkspaceBrokerError,
    workspace_broker_from_env,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

_PENDING_SETTLED_RESUMES: set[str] = set()


def schedule_settled_terminal_resume(job_id: str) -> None:
    """Queue an idempotent authoritative resume for a terminally settled job."""
    _PENDING_SETTLED_RESUMES.add(job_id)


async def flush_settled_terminal_resumes() -> None:
    """Retry queued authoritative resumes until the broker confirms each one."""
    broker = workspace_broker_from_env()
    if broker is None:
        _PENDING_SETTLED_RESUMES.clear()
        return
    for job_id in sorted(_PENDING_SETTLED_RESUMES):
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
        else:
            _PENDING_SETTLED_RESUMES.discard(job_id)
