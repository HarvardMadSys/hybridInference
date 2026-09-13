"""Admin read-only view of the adaptive outbound concurrency limiter.

``adapters/upstream_limiter`` learns a per-(provider, key) cap by AIMD: a 429
takes one off, a run of HTTP 200s puts one back. That learned number is the only
record of what a vendor's concurrency allowance actually is, and it lives
entirely in process memory — nothing persists it, and the ``upstream_limit_changed``
log line shows the transitions but never the standing value. Without a read-only
view of it, an operator answering "why is this provider queueing?" has to
reconstruct the current limit from the log history.

The gateway runs a single uvicorn worker (``deploy/docker/Dockerfile.backend``
sets no ``--workers``), so the limiter singleton this reads *is* the gateway's
whole outbound state, not one worker's slice of it.

Read-only on purpose: the limit is a measurement of the provider, and an admin
override would be a number the controller immediately argues with — a 429 would
take it straight back down, and a probe would walk it back up. Change the
``UPSTREAM_CONCURRENCY_*`` settings if the envelope itself is wrong.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from serving.adapters.upstream_limiter import get_upstream_limiter
from serving.schemas_admin import (
    UpstreamConcurrencyBucket,
    UpstreamConcurrencyConfig,
    UpstreamConcurrencyResponse,
)
from serving.servers.deps import verify_admin_access

router = APIRouter(prefix="/admin")


@router.get("/upstream-concurrency", response_model=UpstreamConcurrencyResponse)
async def get_upstream_concurrency(
    _admin_id: str = Depends(verify_admin_access),
) -> UpstreamConcurrencyResponse:
    """Return every outbound concurrency bucket's live state and the config.

    Buckets are created by traffic, so an idle gateway answers with an empty
    list; that is the honest answer rather than an error. Local inference
    servers are exempt from the limiter entirely and so never appear here.

    Only the key fingerprint identifies a bucket's credential — the limiter
    never stores the raw key, so there is nothing here to redact.
    """
    limiter = get_upstream_limiter()

    buckets = [
        UpstreamConcurrencyBucket(
            provider=provider,
            key_fingerprint=fingerprint,
            limit=int(state["limit"]),
            in_flight=int(state["in_flight"]),
            waiting=int(state["waiting"]),
            successes_since_probe=int(state["successes_since_probe"]),
            probing=bool(state["probing"]),
        )
        for (provider, fingerprint), state in limiter.snapshot().items()
    ]
    # Buckets come out in creation order, which reshuffles the table every time
    # a new key sees its first request. Sort so a polling UI keeps rows put.
    buckets.sort(key=lambda bucket: (bucket.provider, bucket.key_fingerprint))

    return UpstreamConcurrencyResponse(
        config=UpstreamConcurrencyConfig(
            enabled=limiter.enabled,
            initial_limit=limiter.initial_limit,
            max_limit=limiter.max_limit,
            probe_success_interval=limiter.probe_success_interval,
            acquire_timeout_sec=limiter.acquire_timeout,
        ),
        buckets=buckets,
    )
