"""Standalone FastAPI application for the Codex alert triage relay."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, Header, HTTPException, Request, status

from serving.triage.config import TriageSettings
from serving.triage.models import AlertEvent, SubmitAlertResponse
from serving.triage.runner import CodexRunner
from serving.triage.security import protect_process_secrets
from serving.triage.service import TriageOverloadedError, TriageService
from serving.triage.slack import SlackClient, SlackDeliveryError
from serving.triage.store import TriageStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def build_service(settings: TriageSettings) -> TriageService:
    """Construct production dependencies without starting background work."""
    store = TriageStore(settings.state_dir.expanduser().resolve() / "triage.sqlite3")
    slack = SlackClient(
        settings.slack_bot_token.get_secret_value().strip(),
        settings.slack_channel_id.strip(),
    )
    runner = CodexRunner(settings)
    return TriageService(
        store,
        slack,
        runner,
        poll_seconds=settings.worker_poll_seconds,
        max_attempts=settings.max_attempts,
        max_pending_jobs=settings.max_pending_jobs,
    )


def create_app(
    settings: TriageSettings | None = None,
    *,
    service: TriageService | None = None,
) -> FastAPI:
    """Create the relay app with optional test dependencies."""
    resolved_settings = settings or TriageSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        triage_service = service
        if triage_service is None and resolved_settings.configured:
            protect_process_secrets()
            triage_service = build_service(resolved_settings)
        application.state.triage_service = triage_service
        if triage_service is not None:
            await triage_service.start()
        try:
            yield
        finally:
            if triage_service is not None:
                await triage_service.stop()

    application = FastAPI(
        title="HybridInference DeepSeek-backed Codex Alert Triage",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, object]:
        triage_service: TriageService | None = request.app.state.triage_service
        if triage_service is None:
            return {"status": "unconfigured", "ready": False}
        return {
            "status": "ok" if triage_service.running else "stopped",
            "ready": triage_service.running,
            "jobs": await triage_service.store.job_counts(),
        }

    @application.post(
        "/v1/alerts",
        response_model=SubmitAlertResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_alert(
        event: AlertEvent,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> SubmitAlertResponse:
        expected = resolved_settings.relay_token.get_secret_value().strip()
        if not expected:
            raise HTTPException(status_code=503, detail="triage relay is not configured")
        scheme, _, provided = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid relay token")
        triage_service: TriageService | None = request.app.state.triage_service
        if triage_service is None or not triage_service.running:
            raise HTTPException(status_code=503, detail="triage worker is unavailable")
        try:
            return await triage_service.submit(event)
        except SlackDeliveryError as exc:
            raise HTTPException(status_code=502, detail="initial Slack delivery failed") from exc
        except TriageOverloadedError as exc:
            raise HTTPException(status_code=503, detail="triage queue is full") from exc

    return application


app = create_app()
