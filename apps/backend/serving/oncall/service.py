"""Incident orchestration: Slack delivery and the analysis hand-off."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

from serving.oncall.analysis import (
    final_message_text,
    parse_analysis_output,
    validate_agent_events,
)
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
    """Analysis hand-off capability required by the orchestrator.

    Returns ``None`` when the receiving platform owns everything after the
    hand-off (the GitHub Actions workflow posts its own result), or an opaque
    job id when the relay must poll the result back itself (the cloud-agent
    backend) — the id parks the relay job in its ``await_result`` stage.
    """

    async def dispatch(self, event: AlertEvent, slack_thread_ts: str) -> str | None:
        """Trigger the analysis for one firing alert."""


class AnalysisPoller(Protocol):
    """Cloud-agent polling capability, present only on that backend."""

    async def get_job_state(self, job_id: str) -> str:
        """Return the platform job's current state string."""

    async def list_events(self, job_id: str) -> list[dict[str, Any]]:
        """Return the job's full normalized event log."""

    async def cancel_job(self, job_id: str) -> bool:
        """Request cancellation; best-effort."""


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
        agent_poller: AnalysisPoller | None = None,
        agent_poll_seconds: float = 10.0,
        agent_timeout_seconds: float = 1_500.0,
        agent_console_url: str = "",
    ) -> None:
        self.store = store
        self._slack = slack
        self._dispatcher = dispatcher
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._max_pending_jobs = max_pending_jobs
        self._agent_poller = agent_poller
        self._agent_poll_seconds = agent_poll_seconds
        self._agent_timeout_seconds = agent_timeout_seconds
        self._agent_console_url = agent_console_url.strip()
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
        """Advance one queued job a stage; return False when idle.

        ``dispatch`` hands the analysis off. On the GitHub backend the
        workflow owns everything after a successful dispatch — it runs Codex
        and replies (or posts its own failure notice) in the original Slack
        thread, and the relay only retries the hand-off itself. On the
        cloud-agent backend the dispatcher returns a platform job id, the
        relay job parks in ``await_result``, and each later claim of it is one
        poll of that platform job.

        Whichever stage runs, it runs inside the retry guard: a claimed job is
        already marked 'running', so a stage that raises its way out of here
        would never be claimed again.
        """
        job = await self.store.claim_next_job()
        if job is None:
            return False
        try:
            if job.stage == "await_result":
                await self._poll_agent_result(job)
            elif job.stage == "dispatch":
                handle = await self._dispatcher.dispatch(job.event, job.slack_thread_ts)
                if handle:
                    now = time.time()
                    await self.store.mark_awaiting(
                        job.id,
                        handle,
                        not_before=now + self._agent_poll_seconds,
                        deadline=now + self._agent_timeout_seconds,
                    )
                else:
                    await self.store.complete_job(job.id)
            else:
                raise RuntimeError(f"invalid oncall job stage: {job.stage}")
        except Exception as exc:
            # Both stages are inside this guard, and that is the whole point.
            # ``claim_next_job`` has already flipped the row to 'running' and
            # only 'queued' rows are ever claimed again, so anything that
            # escapes here strands the job until the next restart — silently,
            # while still counting against ``max_pending_jobs``. The poll,
            # settle and defer paths write to the store on every branch; one
            # unhandled sqlite3.Error there used to lose a finished,
            # paid-for analysis with nothing said in the thread.
            log.exception("oncall job %s failed during %s", job.id, job.stage)
            final = await self.store.retry_or_fail(job, str(exc), self._max_attempts)
            if final:
                reason = (
                    None
                    if job.stage == "dispatch"
                    else f"the relay could not settle the analysis job ({exc})"
                )
                await self._post_failure_notice(job, reason=reason)
            else:
                self._wake.set()
        return True

    async def _poll_agent_result(self, job: OnCallJob) -> None:
        """Poll one awaiting job's platform result and settle or reschedule.

        The deadline is the stage's whole error budget: a transient poll
        failure reschedules rather than counting attempts, because a control
        plane that is briefly unreachable says nothing about the analysis —
        while a poll loop that gave up after two blips would discard a
        finished, paid-for result.
        """
        poller = self._agent_poller
        if poller is None or not job.agent_job_id:
            # A cloud-agent job restarted into a relay now configured for the
            # GitHub backend (or a row corrupted past use). Unanswerable, and
            # polling again will not make it answerable.
            await self.store.fail_job(job.id, "await_result job without a poller or job id")
            await self._post_failure_notice(job, reason="the analysis job can no longer be polled")
            return
        try:
            state = await poller.get_job_state(job.agent_job_id)
        except Exception as exc:
            log.warning("oncall job %s poll failed: %s", job.id, exc)
            await self._defer_or_expire(job, why=f"unreachable control plane: {exc}")
            return
        if state == "succeeded":
            await self._publish_agent_analysis(job, poller)
        elif state in ("failed", "cancelled"):
            await self.store.fail_job(job.id, f"agent job ended {state}")
            await self._post_failure_notice(job, reason=f"the analysis job ended '{state}'")
        else:
            # queued / running / publishing / anything the platform grows
            # later: keep polling until the deadline says stop.
            await self._defer_or_expire(job, why=f"agent job still {state}")

    async def _defer_or_expire(self, job: OnCallJob, *, why: str) -> None:
        now = time.time()
        if job.deadline is not None and now >= job.deadline:
            if self._agent_poller is not None and job.agent_job_id:
                await self._agent_poller.cancel_job(job.agent_job_id)
            await self.store.fail_job(job.id, f"deadline exceeded ({why})")
            await self._post_failure_notice(
                job,
                reason=(
                    "the analysis did not finish within "
                    f"{int(self._agent_timeout_seconds)}s and was cancelled"
                ),
            )
            return
        await self.store.defer_poll(job.id, not_before=now + self._agent_poll_seconds)

    async def _publish_agent_analysis(self, job: OnCallJob, poller: AnalysisPoller) -> None:
        """Validate a succeeded platform job and post its analysis to the thread."""
        try:
            events = await poller.list_events(job.agent_job_id or "")
        except Exception as exc:
            log.warning("oncall job %s event fetch failed: %s", job.id, exc)
            await self._defer_or_expire(job, why=f"event fetch failed: {exc}")
            return
        try:
            validate_agent_events(events)
            analysis = parse_analysis_output(final_message_text(events))
        except ValueError as exc:
            # The platform says succeeded but the output is not one grounded,
            # schema-valid analysis. Publishing it anyway is how a model that
            # answered from its priors gets dressed up as an investigation —
            # the same refusal the workflow's posting step makes.
            await self.store.fail_job(job.id, f"unusable analysis: {exc}")
            await self._post_failure_notice(job, reason=f"the analysis result was unusable ({exc})")
            return
        text = format_analysis(analysis, None)
        reference = self._agent_job_reference(job.agent_job_id)
        if reference:
            text = f"{text}\n{reference}"
        try:
            await self._slack.post(text, thread_ts=job.slack_thread_ts)
        except Exception as exc:
            log.warning("oncall job %s result post failed: %s", job.id, exc)
            await self._defer_or_expire(job, why=f"Slack post failed: {exc}")
            return
        await self.store.complete_job(job.id)

    def _agent_job_reference(self, agent_job_id: str | None) -> str:
        if not agent_job_id:
            return ""
        if self._agent_console_url:
            with suppress(KeyError, IndexError, ValueError):
                return f"• *Agent job:* {self._agent_console_url.format(job_id=agent_job_id)}"
        return f"• *Agent job:* `{agent_job_id}`"

    async def _post_failure_notice(self, job: OnCallJob, *, reason: str | None = None) -> None:
        detail = (
            reason
            if reason is not None
            else (
                "Hand-off to the analysis backend failed after "
                f"{job.attempts} attempts. Check the oncall relay logs."
            )
        )
        text = f"*Codex on-call unavailable*\n{detail}"
        if reason is not None:
            text += "\nThe original alert above still stands."
        reference = self._agent_job_reference(job.agent_job_id)
        if reference:
            text += f"\n{reference}"
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
