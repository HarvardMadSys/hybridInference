from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from utils.server_metrics import API_REQUEST_LATENCY, API_REQUESTS


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record per-request counters and latency histograms.

    Labels are intentionally coarse to avoid high cardinality: ``route`` uses
    the request URL path, and we distinguish streaming responses by type.
    """

    async def dispatch(self, request: Request, call_next: Callable):  # type: ignore[override]
        route = request.url.path
        method = request.method
        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            # Record as 500 and re-raise
            elapsed = time.perf_counter() - started
            API_REQUEST_LATENCY.labels(route=route, method=method, stream="unknown").observe(
                elapsed
            )
            API_REQUESTS.labels(
                route=route, method=method, status_code="500", stream="unknown"
            ).inc()
            raise
        else:
            stream_label = "yes" if isinstance(response, StreamingResponse) else "no"
            elapsed = time.perf_counter() - started
            API_REQUEST_LATENCY.labels(route=route, method=method, stream=stream_label).observe(
                elapsed
            )
            API_REQUESTS.labels(
                route=route,
                method=method,
                status_code=str(getattr(response, "status_code", 0)),
                stream=stream_label,
            ).inc()
            return response
