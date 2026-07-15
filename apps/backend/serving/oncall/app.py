"""Standalone FastAPI application for the Codex on-call alert relay."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, Header, HTTPException, Request, status

from serving.oncall.config import OnCallSettings
from serving.oncall.dispatcher import GitHubDispatcher
from serving.oncall.models import AlertEvent, SubmitAlertResponse
from serving.oncall.security import protect_process_secrets
from serving.oncall.service import OnCallOverloadedError, OnCallService
from serving.oncall.slack import SlackClient, SlackDeliveryError
from serving.oncall.store import OnCallStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def build_service(settings: OnCallSettings) -> OnCallService:
    """Construct production dependencies without starting background work."""
    store = OnCallStore(settings.state_dir.expanduser().resolve() / "oncall.sqlite3")
    slack = SlackClient(
        settings.slack_bot_token.get_secret_value().strip(),
        settings.slack_channel_id.strip(),
    )
    dispatcher = GitHubDispatcher(settings)
    return OnCallService(
        store,
        slack,
        dispatcher,
        poll_seconds=settings.worker_poll_seconds,
        max_attempts=settings.max_attempts,
        max_pending_jobs=settings.max_pending_jobs,
    )


def create_app(
    settings: OnCallSettings | None = None,
    *,
    service: OnCallService | None = None,
) -> FastAPI:
    """Create the relay app with optional test dependencies."""
    resolved_settings = settings or OnCallSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        oncall_service = service
        if oncall_service is None and resolved_settings.configured:
            protect_process_secrets()
            oncall_service = build_service(resolved_settings)
        application.state.oncall_service = oncall_service
        if oncall_service is not None:
            await oncall_service.start()
        try:
            yield
        finally:
            if oncall_service is not None:
                await oncall_service.stop()

    application = FastAPI(
        title="HybridInference Codex On-Call",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, object]:
        oncall_service: OnCallService | None = request.app.state.oncall_service
        if oncall_service is None:
            return {"status": "unconfigured", "ready": False}
        return {
            "status": "ok" if oncall_service.running else "stopped",
            "ready": oncall_service.running,
            "jobs": await oncall_service.store.job_counts(),
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
            raise HTTPException(status_code=503, detail="oncall relay is not configured")
        scheme, _, provided = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid relay token")
        oncall_service: OnCallService | None = request.app.state.oncall_service
        if oncall_service is None or not oncall_service.running:
            raise HTTPException(status_code=503, detail="oncall worker is unavailable")
        try:
            return await oncall_service.submit(event)
        except SlackDeliveryError as exc:
            raise HTTPException(status_code=502, detail="initial Slack delivery failed") from exc
        except OnCallOverloadedError as exc:
            raise HTTPException(status_code=503, detail="oncall queue is full") from exc

    return application


app = create_app()
