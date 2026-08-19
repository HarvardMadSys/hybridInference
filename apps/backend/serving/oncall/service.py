"""Incident orchestration: Slack delivery and confirmed GitHub hand-off."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

from serving.oncall.models import (
    AlertEvent,
    OnCallAnalysis,
    SubmitAlertResponse,
    sanitize_for_agent,
)

if TYPE_CHECKING:
    from serving.oncall.store import OnCallJob, OnCallStore

log = logging.getLogger(__name__)


class OnCallOverloadedError(RuntimeError):
    """Raised when accepting another analysis would exceed the queue limit."""


class SlackPoster(Protocol):
    """Slack capability required by the orchestrator."""

    async def post(self, text: str, *, thread_ts: str | None = None) -> str:
        """Post text and return the resulting Slack timestamp."""


class AnalysisDispatcher(Protocol):
    """GitHub Actions hand-off capability required by the orchestrator."""

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> None:
        """Trigger the analysis workflow for one firing alert."""

    async def latest_run_id(self) -> int | None:
        """Return the newest run id before dispatch, or None when unknown."""

    async def confirm_run_started(
        self, *, before_run_id: int | None, dispatched_at: float
    ) -> tuple[bool, str | None]:
        """Return whether a workflow run started after the dispatch, with its URL."""


class OnCallService:
    """Deduplicate incidents and hand durable analysis jobs to GitHub Actions."""

    def __init__(
        self,
        store: OnCallStore,
        slack: SlackPoster,
        dispatcher: AnalysisDispatcher,
        *,
        poll_seconds: float = 1.0,
        max_attempts: int = 2,
        max_pending_jobs: int = 100,
        confirm_poll_seconds: float = 10.0,
        confirm_timeout_seconds: float = 120.0,
    ) -> None:
        self.store = store
        self._slack = slack
        self._dispatcher = dispatcher
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._max_pending_jobs = max_pending_jobs
        self._confirm_poll_seconds = confirm_poll_seconds
        self._confirm_timeout_seconds = confirm_timeout_seconds
        self._submit_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Return whether the background worker is alive."""
        return self._worker_task is not None and not self._worker_task.done()

    async def start(self) -> None:
        """Initialize persistence and start the queue worker."""
        await self.store.initialize()
        if self.running:
            return
        self._stop.clear()
        self._worker_task = asyncio.create_task(self._worker(), name="codex-oncall-worker")

    async def stop(self) -> None:
        """Stop the worker without accepting another job."""
        self._stop.set()
        self._wake.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            await task

    async def submit(self, event: AlertEvent) -> SubmitAlertResponse:
        """Deliver the original alert and queue firing incidents for analysis."""
        async with self._submit_lock:
            incident = await self.store.get_incident(event.fingerprint)
            duplicate_firing = bool(
                incident is not None
                and incident.status == "firing"
                and (
                    incident.alert_id == event.alert_id
                    or time.time() - incident.created_at < event.dedupe_window_seconds
                )
            )
            duplicate_resolved = bool(
                incident is not None
                and incident.status == "resolved"
                and (
                    incident.alert_id == event.alert_id
                    or time.time() - incident.updated_at < event.dedupe_window_seconds
                )
            )
            if event.status == "firing" and duplicate_firing:
                assert incident is not None
                return SubmitAlertResponse(
                    accepted=True,
                    duplicate=True,
                    fingerprint=event.fingerprint,
                    slack_thread_ts=incident.slack_thread_ts,
                )
            if event.status == "resolved" and duplicate_resolved:
                assert incident is not None
                return SubmitAlertResponse(
                    accepted=True,
                    duplicate=True,
                    fingerprint=event.fingerprint,
                    slack_thread_ts=incident.slack_thread_ts,
                )

            if event.status == "resolved":
                if incident is not None and incident.status == "firing":
                    await self._slack.post(event.slack_text, thread_ts=incident.slack_thread_ts)
                    await self.store.mark_resolved(event.fingerprint, event)
                    return SubmitAlertResponse(
                        accepted=True,
                        duplicate=False,
                        fingerprint=event.fingerprint,
                        slack_thread_ts=incident.slack_thread_ts,
                    )
                timestamp = await self._slack.post(event.slack_text)
                await self.store.create_resolved(event, timestamp)
                return SubmitAlertResponse(
                    accepted=True,
                    duplicate=False,
                    fingerprint=event.fingerprint,
                    slack_thread_ts=timestamp,
                )

            counts = await self.store.job_counts()
            if counts["queued"] + counts["running"] >= self._max_pending_jobs:
                raise OnCallOverloadedError("oncall queue is full")
            timestamp = await self._slack.post(event.slack_text)
            await self.store.create_firing(event, timestamp)
            self._wake.set()
            return SubmitAlertResponse(
                accepted=True,
                duplicate=False,
                fingerprint=event.fingerprint,
                slack_thread_ts=timestamp,
            )

    async def process_one(self) -> bool:
        """Hand one unit of work; return False when idle.

        Two kinds of work share the loop: dispatching a queued hand-off, and
        polling a parked hand-off to confirm its workflow run actually
        started. A dispatch is not considered delivered on HTTP 204 alone —
        GitHub can accept the event while no workflow listens for it any
        more, which would drop the analysis silently. The job parks in the
        confirm stage until a run appears (not_before schedules the polls,
        deadline bounds them), then the workflow owns the outcome: it runs
        the Codex analysis and replies (or posts its own failure notice) in
        the original Slack thread.
        """
        job = await self.store.claim_next_job()
        if job is not None:
            return await self._dispatch_job(job)
        job = await self.store.claim_next_confirm(time.time())
        if job is None:
            return False
        return await self._poll_confirm(job)

    async def _dispatch_job(self, job: OnCallJob) -> bool:
        """Dispatch one hand-off and park it for delivery confirmation."""
        try:
            if job.stage != "dispatch":
                raise RuntimeError(f"invalid oncall job stage: {job.stage}")
            before_run_id = await self._snapshot_latest_run()
            await self._dispatcher.dispatch(job.event, job.slack_thread_ts)
            now = time.time()
            await self.store.start_confirm(
                job.id,
                not_before=now,
                deadline=now + self._confirm_timeout_seconds,
                before_run_id=before_run_id,
                dispatched_at=now,
            )
        except Exception as exc:
            log.exception("oncall job %s failed during %s", job.id, job.stage)
            if await self._persist_failure(job, str(exc)):
                await self._post_failure_notice(job)
        return True

    async def _snapshot_latest_run(self) -> int | None:
        """Snapshot the newest run id before dispatch, tolerating probe failure."""
        try:
            return await self._dispatcher.latest_run_id()
        except Exception:
            log.exception("failed to snapshot latest Actions run id; confirming by creation time")
            return None

    async def _poll_confirm(self, job: OnCallJob) -> bool:
        """Poll one parked hand-off: complete it, defer it, or fail it."""
        try:
            found, run_url = await self._dispatcher.confirm_run_started(
                before_run_id=job.before_run_id,
                dispatched_at=job.dispatched_at if job.dispatched_at is not None else 0.0,
            )
        except Exception as exc:
            # A transient GitHub failure (network, 5xx, 403) is not an
            # analysis failure: reschedule the poll instead of giving up.
            # Only a deadline that passes without confirmation is final.
            log.warning("oncall job %s confirmation poll failed: %s", job.id, exc)
            if time.time() >= (job.deadline if job.deadline is not None else 0.0):
                return await self._fail_confirm(job, f"last confirmation check failed: {exc}")
            await self._defer_confirm(job, str(exc))
            return True

        if found:
            log.info("oncall job %s confirmed started: %s", job.id, run_url)
            try:
                await self.store.complete_job(job.id)
            except Exception:
                log.exception(
                    "completing oncall job %s failed; it will be requeued on restart",
                    job.id,
                )
            return True

        if time.time() >= (job.deadline if job.deadline is not None else 0.0):
            return await self._fail_confirm(job, None)
        await self._defer_confirm(job, None)
        return True

    async def _defer_confirm(self, job: OnCallJob, error: str | None) -> None:
        """Reschedule the next confirmation poll for a parked job."""
        try:
            await self.store.defer_confirm(
                job.id,
                not_before=time.time() + self._confirm_poll_seconds,
                error=error,
            )
        except Exception:
            log.exception(
                "deferring confirmation poll for oncall job %s failed; "
                "it will be requeued on restart",
                job.id,
            )

    async def _fail_confirm(self, job: OnCallJob, error: str | None) -> bool:
        """Turn a confirmation timeout into a retry or a final failure notice."""
        reason = (
            f"GitHub accepted the dispatch (HTTP 204) but no `Codex On-Call` "
            f"workflow run appeared within {self._confirm_timeout_seconds:.0f}s. "
            f"The original alert above still stands."
        )
        if error:
            reason += f" {error}."
        if await self._persist_failure(job, reason):
            await self._post_failure_notice(job, reason=reason)
        return True

    async def _persist_failure(self, job: OnCallJob, error: str) -> bool:
        """Requeue or finalize a failed job; return True when it is final.

        Every write here is guarded separately: an exception escaping this
        method would leave the row stuck in ``running`` — claimed but never
        claimable again — and the analysis would be lost silently. That is
        the same bug class this confirmation flow is meant to close, so the
        failure path must not reintroduce it. If even the requeue write
        fails, the startup requeue in the store is the last resort.
        """
        try:
            final = await self.store.retry_or_fail(job, error, self._max_attempts)
        except Exception:
            log.exception(
                "persisting failure for oncall job %s failed; it will be requeued on restart",
                job.id,
            )
            return False
        if not final:
            self._wake.set()
        return final

    async def _post_failure_notice(self, job: OnCallJob, *, reason: str | None = None) -> None:
        text = "*Codex on-call unavailable*\n" + (
            reason
            or f"Hand-off to the GitHub Actions analysis workflow failed after "
            f"{job.attempts} attempts. Check the oncall relay logs."
        )
        try:
            await self._slack.post(text, thread_ts=job.slack_thread_ts)
        except Exception:
            log.exception("failed to post final oncall failure notice for job %s", job.id)

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_one()
            except Exception:
                log.exception("oncall worker loop failed; retrying")
                processed = False
            if processed:
                continue
            self._wake.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)


def _escape_slack(text: str) -> str:
    sanitized = sanitize_for_agent(text)
    assert isinstance(sanitized, str)
    return sanitized.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_analysis(analysis: OnCallAnalysis, thread_id: str | None) -> str:
    """Render a bounded, mention-safe Codex result for a Slack thread.

    Used by the ``codex-oncall`` GitHub Actions workflow (via
    ``serving.oncall.gha``) to post the structured analysis back into the
    original alert thread.
    """
    evidence = "\n".join(f"• {_escape_slack(item)}" for item in analysis.evidence) or "• None"
    actions = "\n".join(
        f"{index}. {_escape_slack(item)}"
        for index, item in enumerate(analysis.recommended_actions, start=1)
    )
    lines = [
        "*Codex on-call*",
        f"• *Classification:* `{analysis.classification}`",
        f"• *Confidence:* {analysis.confidence:.0%}",
        f"• *Summary:* {_escape_slack(analysis.summary)}",
        f"• *Impact:* {_escape_slack(analysis.impact)}",
        f"• *Likely cause:* {_escape_slack(analysis.likely_cause)}",
        "",
        "*Evidence*",
        evidence,
        "",
        "*Recommended actions*",
        actions,
        "",
        f"• *Issue:* `{analysis.issue_recommendation}`",
        f"• *Draft PR:* `{analysis.draft_pr_recommendation}`",
    ]
    if thread_id:
        lines.append(f"• *Codex thread:* `{_escape_slack(thread_id)}`")
    return "\n".join(lines)[:40_000]
