"""FastAPI web server for interactively exploring an exported api_logs trace.

Run it against a trace exported by ``ops/db/export_logs.py``::

    python ops/db/analysis/trace_viewer/server.py api_logs_export.jsonl.zst

then open the printed URL. The server reads the file once at start-up and
serves a small single-page UI (overview dashboard + request/session
drill-down) backed by a JSON API. It never talks to a database or the network.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ in (None, ""):
    # Running as a plain script (`python .../server.py`): make the repo root
    # importable so the absolute package import below resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ops.db.analysis.trace_viewer.core import TraceStore, _parse_ts

if TYPE_CHECKING:
    from collections.abc import Iterable

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _time_param(value: str | None) -> float | None:
    """Parse a start/end query param that may be ISO-8601 or epoch seconds."""
    if value in (None, ""):
        return None
    return _parse_ts(value)


def _spec(
    start: str | None,
    end: str | None,
    model_id: str | None,
    provider: str | None,
    served_model_id: str | None,
    user_id: str | None,
    session_id: str | None,
    agent: str | None,
    status_code: int | None,
    errors_only: bool,
    q: str | None,
) -> dict[str, object]:
    """Assemble a core filter spec from request query parameters."""
    return {
        "start": _time_param(start),
        "end": _time_param(end),
        "model_id": model_id,
        "provider": provider,
        "served_model_id": served_model_id,
        "user_id": user_id,
        "session_id": session_id,
        "agent": agent,
        "status_code": status_code,
        "errors_only": errors_only,
        "q": q,
    }


def create_app(store: TraceStore) -> FastAPI:
    """Build the FastAPI app bound to an already-loaded :class:`TraceStore`."""
    app = FastAPI(title="api_logs trace viewer", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/meta")
    def meta() -> JSONResponse:
        return JSONResponse(store.meta())

    @app.get("/api/summary")
    def summary(
        start: str | None = None,
        end: str | None = None,
        model_id: str | None = None,
        provider: str | None = None,
        served_model_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        agent: str | None = None,
        status_code: int | None = None,
        errors_only: bool = False,
        q: str | None = None,
    ) -> JSONResponse:
        spec = _spec(
            start,
            end,
            model_id,
            provider,
            served_model_id,
            user_id,
            session_id,
            agent,
            status_code,
            errors_only,
            q,
        )
        return JSONResponse(store.summary(spec))

    @app.get("/api/requests")
    def requests(
        start: str | None = None,
        end: str | None = None,
        model_id: str | None = None,
        provider: str | None = None,
        served_model_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        agent: str | None = None,
        status_code: int | None = None,
        errors_only: bool = False,
        q: str | None = None,
        sort: str = "ts",
        order: str = "desc",
        page: int = 1,
        page_size: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        spec = _spec(
            start,
            end,
            model_id,
            provider,
            served_model_id,
            user_id,
            session_id,
            agent,
            status_code,
            errors_only,
            q,
        )
        return JSONResponse(
            store.list_requests(spec, sort=sort, order=order, page=page, page_size=page_size)
        )

    @app.get("/api/requests/{row}")
    def request_detail(row: int) -> JSONResponse:
        rec = store.get_full_record(row)
        if rec is None:
            raise HTTPException(status_code=404, detail="request not found")
        return JSONResponse(rec)

    @app.get("/api/sessions")
    def sessions(
        user_id: str | None = None,
        model_id: str | None = None,
        min_requests: int = 1,
        sort: str = "start_ts",
        order: str = "desc",
        page: int = 1,
        page_size: int = Query(50, ge=1, le=500),
    ) -> JSONResponse:
        spec = {"user_id": user_id, "model_id": model_id}
        return JSONResponse(
            store.list_sessions(
                spec,
                sort=sort,
                order=order,
                page=page,
                page_size=page_size,
                min_requests=min_requests,
            )
        )

    @app.get("/api/sessions/{sid:path}")
    def session_detail(sid: str) -> JSONResponse:
        sess = store.get_session(sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse(sess)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _open_browser_later(url: str, delay: float = 1.0) -> None:
    """Open the default browser at ``url`` after a short delay, best-effort."""

    def _open() -> None:
        with contextlib.suppress(Exception):  # pragma: no cover - browser is a nicety
            webbrowser.open(url)

    threading.Timer(delay, _open).start()


def main(argv: Iterable[str] | None = None) -> int:
    """Parse CLI args, load the trace, and run the web server."""
    parser = argparse.ArgumentParser(
        description="Web viewer for an exported api_logs trace (.jsonl or .jsonl.zst)."
    )
    parser.add_argument("trace", help="Path to the exported trace (.jsonl or .jsonl.zst)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8677, help="Bind port (default: 8677)")
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    args = parser.parse_args(list(argv) if argv is not None else None)

    import uvicorn

    print(f"Loading trace from {args.trace} ...", flush=True)
    store = TraceStore(args.trace)
    meta = store.meta()
    print(
        f"Indexed {meta['total_rows']} requests across {meta['n_sessions']} sessions"
        f" ({meta['invalid_lines']} unparseable lines).",
        flush=True,
    )
    app = create_app(store)
    url = f"http://{args.host}:{args.port}/"
    print(f"Serving trace viewer at {url}  (Ctrl-C to stop)", flush=True)
    if not args.no_open:
        _open_browser_later(url)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
