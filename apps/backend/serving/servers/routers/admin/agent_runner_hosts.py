"""Admin endpoints for the agent runner host pool.

Runners pull work, so there is no gateway-side dial that points jobs at a
machine — the only moment the platform gets to decide "not you" is the claim.
These endpoints therefore do not move anything: they name the host whose
runners may claim, and the switch takes effect on the losing host's next poll.

A host joins the pool by polling, never by being registered here. That keeps
the list to machines that actually exist and are actually configured, which is
the difference between switching to a host and switching to a typo.

**A host name is a scheduling label, not a machine identity.** The runner
reports it, so anything holding the dispatcher credential can report any name:
two machines configured alike are one entry, and a runner that wants another
host's work only has to claim its name. That is acceptable because the
credential is already the boundary — it opens the claim door for every host —
and this switch decides *where our own machines run our own jobs*. Do not
build anything on it that needs to survive a hostile runner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request

from serving.schemas_admin import (
    AgentRunnerHost,
    ListAgentRunnerHostsResponse,
    SetActiveAgentRunnerHostRequest,
)
from serving.servers.auth import log_admin_action
from serving.servers.deps import (
    get_agent_job_store,
    get_operational_store,
    verify_admin_access,
)
from serving.utils.request_ip import get_client_ip

if TYPE_CHECKING:
    from serving.storage.agent_job_store import AgentJobStore

router = APIRouter(prefix="/admin")


def _require_store(store: AgentJobStore | None) -> AgentJobStore:
    """Return the store or 503 — agent jobs are an optional deployment part."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Agent jobs are not configured for this deployment.",
        )
    return store


def _to_item(row: dict) -> AgentRunnerHost:
    last_seen: datetime = row["last_seen_at"]
    now = datetime.now(UTC)
    # The column is TIMESTAMPTZ, so asyncpg hands back an aware datetime; a
    # naive one would only come from a hand-built row in a test.
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return AgentRunnerHost(
        host=row["host"],
        active=bool(row["is_active"]),
        last_worker_id=row.get("last_worker_id"),
        first_seen_at=row["first_seen_at"],
        last_seen_at=last_seen,
        seconds_since_seen=max(0.0, (now - last_seen).total_seconds()),
    )


async def _snapshot(store: AgentJobStore) -> ListAgentRunnerHostsResponse:
    """The pool as it stands — the response every endpoint here returns.

    ``active_host`` is read from the policy rather than derived from the list.
    They agree in every normal case; where they would not — a pinned host
    deleted out from under the policy — the honest answer is the name the claim
    gate is actually enforcing, not "unpinned" while the queue sits still.
    """
    hosts = [_to_item(row) for row in await store.list_runner_hosts()]
    return ListAgentRunnerHostsResponse(
        hosts=hosts,
        active_host=await store.active_runner_host(),
    )


@router.get("/agent/runner-hosts", response_model=ListAgentRunnerHostsResponse)
async def list_agent_runner_hosts(
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
) -> ListAgentRunnerHostsResponse:
    """List every machine that has polled for agent work, and the pinned one."""
    return await _snapshot(_require_store(store))


@router.put("/agent/runner-hosts/active", response_model=ListAgentRunnerHostsResponse)
async def set_active_agent_runner_host(
    request: Request,
    payload: SetActiveAgentRunnerHostRequest,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    op_store=Depends(get_operational_store),
) -> ListAgentRunnerHostsResponse:
    """Pin agent jobs to one host, or unpin so any runner may claim.

    Non-preemptive, and worth being precise about: the new host starts claiming
    at once, while jobs already running on the old one keep running to the end
    — they hold a lease and their runner reports through without claiming
    again. The two overlap. This is not a drain, which would mean waiting for
    the old host to empty before the new one starts.
    """
    job_store = _require_store(store)
    previous = await job_store.active_runner_host()

    if not await job_store.set_active_runner_host(host=payload.host):
        # Pinning to a machine that has never polled parks the queue on a host
        # that may not exist, and it presents as "every job hangs" with nothing
        # in the logs to say why.
        raise HTTPException(
            status_code=404,
            detail=(
                f"No runner on {payload.host!r} has ever polled. Start one there "
                "(ops/deploy/agent_runner.sh up) and it will appear here."
            ),
        )

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "agent.runner_host.set_active",
        None,
        {"old_host": previous, "new_host": payload.host},
    )
    return await _snapshot(job_store)


@router.delete("/agent/runner-hosts/{host}", response_model=ListAgentRunnerHostsResponse)
async def forget_agent_runner_host(
    request: Request,
    host: str,
    _admin_id: str = Depends(verify_admin_access),
    store: AgentJobStore | None = Depends(get_agent_job_store),
    op_store=Depends(get_operational_store),
) -> ListAgentRunnerHostsResponse:
    """Drop a decommissioned host from the list.

    Housekeeping only: a runner still polling on that machine re-adds itself
    within seconds. The active host cannot be dropped, because doing so would
    silently unpin — every other machine would start claiming, which is the
    opposite of what removing a host from a list looks like it does.
    """
    job_store = _require_store(store)
    if host == await job_store.active_runner_host():
        raise HTTPException(
            status_code=409,
            detail=(
                f"{host!r} is the active host. Switch to another host (or unpin) "
                "before removing it."
            ),
        )
    if not await job_store.forget_runner_host(host=host):
        raise HTTPException(status_code=404, detail=f"Unknown runner host: {host}")

    await log_admin_action(
        op_store,
        get_client_ip(request),
        "agent.runner_host.forget",
        None,
        {"host": host},
    )
    return await _snapshot(job_store)
