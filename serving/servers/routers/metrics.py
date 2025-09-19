from __future__ import annotations

"""Expose Prometheus metrics at /metrics.

If prometheus_client is unavailable or METRICS_ENABLED=0, returns a minimal
payload so the endpoint still exists without crashing.
"""

from fastapi import APIRouter, Response

from serving.observability.metrics import render_latest

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    payload = render_latest()
    return Response(content=payload, media_type="text/plain; version=0.0.4; charset=utf-8")
