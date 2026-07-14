"""Incident orchestration for Slack delivery and asynchronous Codex analysis."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

from serving.triage.models import (
    AlertEvent,
    SubmitAlertResponse,
    TriageAnalysis,
    sanitize_for_agent,
)

if TYPE_CHECKING:
    from serving.triage.runner import CodexRun
    from serving.triage.store import TriageJob, TriageStore

log = logging.getLogger(__name__)


class TriageOverloadedError(RuntimeError):
    """Raised when accepting another analysis would exceed the queue limit."""


class SlackPoster(Protocol):
    """Slack capability required by the orchestrator."""

    async def post(self, text: str, *, thread_ts: str | None = None) -> str:
        """Post text and return the resulting Slack timestamp."""


class AnalysisRunner(Protocol):
    """Codex capability required by the orchestrator."""

    async def run(self, event: AlertEvent) -> CodexRun:
        """Analyze one firing alert."""


class TriageService:
    """Deduplicate incidents and process durable analysis jobs."""

    def __init__(
        self,
        store: TriageStore,
        slack: SlackPoster,
        runner: AnalysisRunner,
        *,
        poll_seconds: float = 1.0,
        max_attempts: int = 2,
        max_pending_jobs: int = 100,
    ) -> None:
        self.store = store
        self._slack = slack
        self._runner = runner
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._max_pending_jobs = max_pending_jobs
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
        self._worker_task = asyncio.create_task(self._worker(), name="codex-triage-worker")

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
                raise TriageOverloadedError("triage queue is full")
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
        """Process one ready stage; return False when the queue is empty."""
        job = await self.store.claim_next_job()
        if job is None:
            return False
        try:
            if job.stage == "analysis":
                result = await self._runner.run(job.event)
                await self.store.save_analysis(
                    job.id,
                    job.fingerprint,
                    job.event.alert_id,
                    result.analysis,
                    result.thread_id,
                )
                self._wake.set()
            elif job.stage == "posting" and job.result is not None:
                await self._slack.post(
                    format_analysis(job.result, job.codex_thread_id),
                    thread_ts=job.slack_thread_ts,
                )
                await self.store.complete_job(job.id)
            else:
                raise RuntimeError(f"invalid triage job stage: {job.stage}")
        except Exception as exc:
            log.exception("triage job %s failed during %s", job.id, job.stage)
            final = await self.store.retry_or_fail(job, str(exc), self._max_attempts)
            if final and job.stage == "analysis":
                await self._post_failure_notice(job)
            elif not final:
                self._wake.set()
        return True

    async def _post_failure_notice(self, job: TriageJob) -> None:
        try:
            await self._slack.post(
                "*Codex triage (DeepSeek) unavailable*\n"
                f"Analysis failed after {job.attempts} attempts. Check the triage service logs.",
                thread_ts=job.slack_thread_ts,
            )
        except Exception:
            log.exception("failed to post final triage failure notice for job %s", job.id)

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_one()
            except Exception:
                log.exception("triage worker loop failed; retrying")
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


def format_analysis(analysis: TriageAnalysis, thread_id: str | None) -> str:
    """Render a bounded, mention-safe Codex result for a Slack thread."""
    evidence = "\n".join(f"• {_escape_slack(item)}" for item in analysis.evidence) or "• None"
    actions = "\n".join(
        f"{index}. {_escape_slack(item)}"
        for index, item in enumerate(analysis.recommended_actions, start=1)
    )
    lines = [
        "*Codex triage (DeepSeek)*",
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
