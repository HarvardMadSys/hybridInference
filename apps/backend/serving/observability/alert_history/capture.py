"""Turn what the backend alert path does into history records.

**Episodes.** A history episode is one breach as this process observed it:
opened by the first breached evaluation, ended by the observation that ends
it — ``observed_healthy`` when the tracker saw the metric clear, ``no_samples``
when the stale sweep closed it for want of data. It is deliberately *not* the
notification tracker's firing state. When a recovery card fails to send, the
tracker re-arms the key so the card can be retried; the episode stays ended,
and the retries are attributed to it. A breach after that opens a new episode,
even though the tracker never left "firing". Different workers keep different
episodes; nothing here merges them.

Every attempt is bound to its episode synchronously, before the send awaits,
so a result that returns after a new breach opened cannot be attributed to the
new episode.

**What is kept.** Facts are copied from a rule's context only for keys in
:data:`SAFE_FACT_KEYS`; every other key is named in ``limitations`` and left
out. The message snapshot is the sink's own rendering of the redacted title and
context, so its shape matches the card that was posted. Account identifiers a
key embeds (``cost_overrun:<user>:<day>``) are replaced by ``[subject]`` and
recorded only as an erasure-fence digest in ``subject_digests``; without a
fence secret the digest is not kept either, and the record says so.

**Repeats.** A decision not to notify (cooldown, mute, snooze, in-flight) is
recorded at once the first time it happens in an episode, then counted in
non-overlapping 30-second buckets. A bucket not yet written when the process
dies is lost; the read API never claims otherwise.
"""

from __future__ import annotations

import collections
import datetime as dt
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from serving.observability.alert_history.queue import diag
from serving.observability.alert_history.records import (
    MAX_BODY,
    MAX_FACT_STRING,
    MAX_FACTS,
    MAX_RECORD_BYTES,
    MAX_TITLE,
    HistoryRecord,
    clean_text,
    finalize,
)
from serving.observability.alert_history.redaction import looks_sensitive, redact_text
from serving.observability.alert_types import alert_type_of
from serving.utils import context as req_ctx

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

BACKEND_SOURCE = "backend"
MESSAGE_FORMAT_VERSION = "backend-alert-card-1"
BUCKET_SEC = 30.0
MAX_BUCKETS = 1024
MAX_OPEN_EPISODES = 4096
MAX_CLOSED_EPISODES = 4096
_REQUEST_ID_MAX_LEN = 128
_SUBJECT_PLACEHOLDER = "[subject]"

#: Context keys whose values are kept as facts. Everything else is omitted and
#: named in ``limitations``: free text an upstream or a caller chose (error
#: detail, request paths, upstream messages), addresses, key prefixes, account
#: lists, request ids. Counts, rates, thresholds and labels of the gateway's
#: own objects stay.
SAFE_FACT_KEYS = frozenset(
    {
        "alert",
        "all_active_blocks",
        "availability",
        "block_seconds",
        "blocks_in_window",
        "budget",
        "capacity",
        "consecutive_auth_rejections",
        "consecutive_failures",
        "count",
        "db_kind",
        "distinct_ips",
        "endpoint_id",
        "evicted_count",
        "gate",
        "failure_rate",
        "failures_in_incident",
        "hour",
        "hourly_spend",
        "impact",
        "incident_duration_sec",
        "likely_cause",
        "max_age_sec",
        "max_idle_sec",
        "max_pending_count",
        "model",
        "note",
        "over_by",
        "p95_ms",
        "p99_ms",
        "peak_in_window",
        "provider",
        "quota",
        "quota_reset_at",
        "alert_muted_until",
        "rate",
        "rate_threshold",
        "reason",
        "remedy",
        "role",
        "samples",
        "size_cap_count",
        "spend",
        "status",
        "store",
        "task_name",
        "threshold",
        "threshold_ms",
        "top_endpoints",
        "top_error_types",
        "top_providers",
        "top_status_codes",
        "total",
        "trigger",
        "trip_cause",
        "ttl_count",
        "window_minutes",
        "window_sec",
    }
)

#: Alert types whose dedupe key names an account, and the segment it is in.
_SUBJECT_SEGMENT = {"cost_overrun": 1}
#: Alert types whose key suffix is a model or an endpoint.
_MODEL_KEY_TYPES = frozenset({"stream_failure_rate"})
_ENDPOINT_KEY_TYPES = frozenset({"circuit_open", "upstream_auth"})


@dataclass(frozen=True)
class BackendIdentity:
    """Who this process is, fixed when capture starts."""

    source_namespace: str
    deployment_id: str
    target_environment: str
    source_instance_id: str
    process_id: str
    started_at: dt.datetime
    release_id: str | None = None


@dataclass
class _Episode:
    episode_id: str
    key: str
    opened_at: float
    #: Decision reasons already recorded individually in this episode.
    first_reasons: set[str] = field(default_factory=set)


@dataclass
class _Bucket:
    episode_id: str | None
    key: str
    reason: str
    start: float
    count: int = 0
    first_at: float = 0.0
    last_at: float = 0.0


@dataclass(frozen=True)
class AttemptHandle:
    """An attempt in progress, bound to its episode before the send awaits."""

    attempt_id: str
    key: str
    episode_id: str | None
    channel: str
    resolution: bool
    origin: str
    audit_ref: str | None
    started: float


@dataclass(frozen=True)
class DeliveryOutcome:
    """The detailed result of one post. ``accepted`` is the old bool."""

    accepted: bool
    outcome: str
    error_code: str | None = None
    http_status: int | None = None


class _Redactor:
    """Replace the subjects a key names, then credential-shaped values.

    ``redacted`` turns true once any value was changed by the second step, so
    the record can say its text is not verbatim.
    """

    def __init__(self, subjects: list[str]) -> None:
        self._subjects = sorted({s for s in subjects if s}, key=len, reverse=True)
        self.redacted = False

    def __call__(self, text: str) -> str:
        text = clean_text(text)
        for subject in self._subjects:
            text = text.replace(subject, _SUBJECT_PLACEHOLDER)
        text, changed = redact_text(text)
        if changed:
            self.redacted = True
        return text


class BackendCapture:
    """Build backend history records and hand them to ``offer``."""

    def __init__(
        self,
        identity: BackendIdentity,
        offer: Callable[[HistoryRecord], bool],
        *,
        fence_secret: str | None,
        clock: Callable[[], float] = time.time,
        bucket_sec: float = BUCKET_SEC,
        max_buckets: int = MAX_BUCKETS,
    ) -> None:
        self.identity = identity
        self._offer = offer
        self._fence_secret = fence_secret or None
        self._clock = clock
        self._bucket_sec = bucket_sec
        self._max_buckets = max_buckets
        self._seq = 0
        self._open: dict[str, _Episode] = {}
        self._closed: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._buckets: collections.OrderedDict[tuple[str | None, str, str], _Bucket] = (
            collections.OrderedDict()
        )
        #: Decisions with no episode already recorded individually (bounded).
        self._first_unattached: collections.OrderedDict[tuple[str, str], None] = (
            collections.OrderedDict()
        )
        self.records_built = 0
        self.records_refused = 0
        self.evicted_episodes = 0
        #: Short hash of the alert rule configuration in force, if known.
        self.rule_version: str | None = None

    # -- identity and redaction ---------------------------------------------

    def _next_id(self) -> tuple[int, str]:
        self._seq += 1
        return self._seq, f"{self.identity.source_instance_id}:{self._seq}"

    def _subjects(self, key: str) -> list[str]:
        segment = _SUBJECT_SEGMENT.get(alert_type_of(key))
        if segment is None:
            return []
        parts = key.split(":")
        return [parts[segment]] if len(parts) > segment and parts[segment] else []

    def _digest(self, subject: str) -> str | None:
        if not self._fence_secret:
            return None
        from serving.storage.log_schema import fence_account_digest

        return fence_account_digest(subject, self._fence_secret)

    def _alert_fields(self, key: str) -> tuple[dict[str, Any], _Redactor]:
        subjects = self._subjects(key)
        redact = _Redactor(subjects)
        alert_type = alert_type_of(key)
        limitations: list[str] = []
        digests: list[str] = []
        for subject in subjects:
            digest = self._digest(subject)
            if digest is None:
                limitations.append("subject_unfenced")
            else:
                digests.append(digest)
        suffix = key.split(":", 1)[1] if ":" in key else None
        fields_: dict[str, Any] = {
            "alert_type": redact(alert_type)[:100],
            "alert_key": redact(key)[:300],
            "rule_id": redact(alert_type)[:100],
            "rule_version": self.rule_version,
            "subject_digests": tuple(digests),
            "limitations": tuple(limitations),
        }
        if suffix and alert_type in _MODEL_KEY_TYPES:
            fields_["model_id"] = redact(suffix)[:200]
        elif suffix and alert_type in _ENDPOINT_KEY_TYPES:
            fields_["endpoint_id"] = redact(suffix)[:200]
        if redact.redacted:
            fields_["limitations"] = (*limitations, "redacted:alert_key")
            redact.redacted = False
        return fields_, redact

    def _request_ref(self) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[str, ...]]:
        """The ambient request, if its owner is known: (refs, digests, limitations)."""
        ctx = req_ctx.get()
        raw = ctx.get("request_id")
        if not isinstance(raw, str):
            return (), (), ()
        request_id = " ".join(raw.split())[:_REQUEST_ID_MAX_LEN]
        if not request_id:
            return (), (), ()
        if looks_sensitive(request_id):
            # A client-chosen id that looks like a credential is not kept.
            return (), (), ("request_ref_omitted:unsafe_value",)
        user_id = ctx.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return (), (), ("request_ref_omitted:owner_unknown",)
        digest = self._digest(user_id)
        if digest is None:
            return (), (), ("request_ref_omitted:unfenced",)
        return ((clean_text(request_id), digest),), (digest,), ()

    def _facts(
        self, context: Mapping[str, Any] | None, redact: _Redactor
    ) -> tuple[dict[str, Any], list[str]]:
        facts: dict[str, Any] = {}
        limitations: list[str] = []
        for raw_key, value in (context or {}).items():
            key = str(raw_key)
            if key not in SAFE_FACT_KEYS:
                limitations.append(f"omitted:{redact_text(clean_text(key))[0][:64]}")
                continue
            if len(facts) >= MAX_FACTS:
                limitations.append("facts_truncated")
                break
            facts[key] = self._fact_value(key, value, redact, limitations)
        return facts, limitations

    @staticmethod
    def _fact_value(key: str, value: Any, redact: _Redactor, limitations: list[str]) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value if abs(value) < 2**53 else str(value)
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        already = redact.redacted
        redact.redacted = False
        text = redact(str(value))
        if redact.redacted:
            limitations.append(f"redacted:{key}")
        redact.redacted = redact.redacted or already
        if len(text) > MAX_FACT_STRING:
            limitations.append(f"truncated:{key}")
            text = text[:MAX_FACT_STRING]
        return text

    # -- record plumbing ----------------------------------------------------

    def _emit(self, kind: str, occurred: float, **values: Any) -> bool:
        seq, origin_record_id = self._next_id()
        ident = self.identity
        limitations = tuple(dict.fromkeys(values.pop("limitations", ())))
        record = HistoryRecord(
            source=BACKEND_SOURCE,
            source_namespace=ident.source_namespace,
            origin_record_id=origin_record_id,
            kind=kind,
            occurred_at=dt.datetime.fromtimestamp(occurred, dt.UTC),
            deployment_id=ident.deployment_id,
            target_environment=ident.target_environment,
            source_instance_id=ident.source_instance_id,
            observation_scope="process",
            process_id=ident.process_id,
            producer_seq=seq,
            limitations=limitations,
            **values,
        )
        record = finalize(record)
        if record.size > MAX_RECORD_BYTES:
            # Only a snapshot can get this large; keep the fact without it.
            from dataclasses import replace

            record = finalize(
                replace(record, message=None, limitations=(*limitations, "message_omitted:size"))
            )
        self.records_built += 1
        accepted = self._offer(record)
        if not accepted:
            self.records_refused += 1
        return accepted

    # -- episodes -----------------------------------------------------------

    def open_episode(self, key: str) -> str | None:
        """The episode currently open on ``key``, if any."""
        episode = self._open.get(key)
        return episode.episode_id if episode is not None else None

    def closed_episode(self, key: str) -> str | None:
        """The most recently ended episode on ``key``, for its recovery retries."""
        return self._closed.get(key)

    def on_breach(
        self,
        key: str,
        *,
        severity: str,
        title: str,
        kind: str,
        context: Mapping[str, Any] | None,
        now: float | None = None,
    ) -> str:
        """Note a breached evaluation; open an episode when none is open."""
        episode = self._open.get(key)
        if episode is not None:
            return episode.episode_id
        moment = self._clock() if now is None else now
        episode = _Episode(episode_id=f"ep_{uuid.uuid4().hex}", key=key, opened_at=moment)
        if len(self._open) >= MAX_OPEN_EPISODES:
            evicted_key = next(iter(self._open))
            self._open.pop(evicted_key)
            self.evicted_episodes += 1
        self._open[key] = episode
        self._closed.pop(key, None)
        alert, redact = self._alert_fields(key)
        facts, fact_limits = self._facts(context, redact)
        facts["title"] = redact(title)[:MAX_TITLE]
        facts["tracker_kind"] = kind
        refs, ref_digests, ref_limits = self._request_ref()
        self._emit(
            "condition_triggered",
            moment,
            episode_id=episode.episode_id,
            severity=severity,
            facts=facts,
            request_refs=refs,
            **{
                **alert,
                "subject_digests": (*alert["subject_digests"], *ref_digests),
                "limitations": (*alert["limitations"], *fact_limits, *ref_limits),
            },
        )
        return episode.episode_id

    def on_end(self, key: str, *, basis: str, now: float | None = None) -> str | None:
        """End the open episode on ``key``; otherwise return the last ended one."""
        episode = self._open.pop(key, None)
        if episode is None:
            return self._closed.get(key)
        moment = self._clock() if now is None else now
        self._flush_episode(episode.episode_id)
        self._closed[key] = episode.episode_id
        self._closed.move_to_end(key)
        while len(self._closed) > MAX_CLOSED_EPISODES:
            self._closed.popitem(last=False)
        alert, _ = self._alert_fields(key)
        self._emit(
            "condition_ended",
            moment,
            episode_id=episode.episode_id,
            end_basis=basis,
            facts={"episode_duration_sec": round(max(0.0, moment - episode.opened_at), 3)},
            **alert,
        )
        return episode.episode_id

    def forget(self, key: str, *, episode_id: str | None) -> None:
        """The notification side is done with ``episode_id`` on ``key``.

        Only that episode's mapping goes: a late confirmation for an old
        episode must not drop a newer ended episode's retry association.
        """
        if episode_id is not None and self._closed.get(key) == episode_id:
            self._closed.pop(key, None)

    # -- decisions ----------------------------------------------------------

    def decision(
        self,
        key: str,
        *,
        episode_id: str | None,
        reason: str,
        resolution: bool = False,
        now: float | None = None,
    ) -> None:
        """Record a decision not to send, first individually then in buckets."""
        moment = self._clock() if now is None else now
        if resolution:
            self._emit_decision(key, episode_id, reason, moment, resolution=True)
            return
        if episode_id is not None:
            episode = self._open.get(key)
            if episode is not None and episode.episode_id == episode_id:
                if reason not in episode.first_reasons:
                    episode.first_reasons.add(reason)
                    self._emit_decision(key, episode_id, reason, moment, resolution=False)
                    return
            else:
                # A decision for an ended episode is rare (a late repeat);
                # record it on its own rather than bucket it.
                self._emit_decision(key, episode_id, reason, moment, resolution=False)
                return
        else:
            marker = (key, reason)
            if marker not in self._first_unattached:
                self._first_unattached[marker] = None
                while len(self._first_unattached) > self._max_buckets:
                    self._first_unattached.popitem(last=False)
                self._emit_decision(key, None, reason, moment, resolution=False)
                return
        self._count(key, episode_id, reason, moment)

    def _emit_decision(
        self, key: str, episode_id: str | None, reason: str, moment: float, *, resolution: bool
    ) -> None:
        alert, _ = self._alert_fields(key)
        self._emit(
            "notification_decision",
            moment,
            episode_id=episode_id,
            decision_reason=reason,
            facts={"notification": "resolution" if resolution else "firing"},
            association_incomplete=episode_id is None,
            **alert,
        )

    def _count(self, key: str, episode_id: str | None, reason: str, moment: float) -> None:
        start = math.floor(moment / self._bucket_sec) * self._bucket_sec
        ident = (episode_id, key, reason)
        bucket = self._buckets.get(ident)
        if bucket is not None and bucket.start != start:
            self._buckets.pop(ident)
            self._emit_bucket(bucket)
            bucket = None
        if bucket is None:
            if len(self._buckets) >= self._max_buckets:
                self.flush_buckets(moment)
            if len(self._buckets) >= self._max_buckets:
                # Still full of live buckets: write the oldest early rather
                # than drop its count. Its window then closes short of 30s,
                # which the record's own bucket bounds say.
                _, oldest = self._buckets.popitem(last=False)
                self._emit_bucket(oldest, end=moment)
            bucket = _Bucket(
                episode_id=episode_id, key=key, reason=reason, start=start, first_at=moment
            )
            self._buckets[ident] = bucket
        bucket.count += 1
        bucket.last_at = moment

    def _emit_bucket(self, bucket: _Bucket, *, end: float | None = None) -> None:
        if bucket.count <= 0:
            return
        alert, _ = self._alert_fields(bucket.key)
        bucket_end = bucket.start + self._bucket_sec if end is None else end
        self._emit(
            "suppression_summary",
            bucket.last_at,
            episode_id=bucket.episode_id,
            decision_reason=bucket.reason,
            count=bucket.count,
            bucket_start=dt.datetime.fromtimestamp(bucket.start, dt.UTC),
            bucket_end=dt.datetime.fromtimestamp(bucket_end, dt.UTC),
            facts={
                "first_at": _iso(bucket.first_at),
                "last_at": _iso(bucket.last_at),
            },
            association_incomplete=bucket.episode_id is None,
            **alert,
        )

    def flush_buckets(self, now: float | None = None, *, force: bool = False) -> None:
        """Write every bucket whose window has passed (all of them when ``force``)."""
        moment = self._clock() if now is None else now
        for ident, bucket in list(self._buckets.items()):
            if force or bucket.start + self._bucket_sec <= moment:
                self._buckets.pop(ident, None)
                self._emit_bucket(bucket)

    def _flush_episode(self, episode_id: str) -> None:
        for ident, bucket in list(self._buckets.items()):
            if bucket.episode_id == episode_id:
                self._buckets.pop(ident, None)
                self._emit_bucket(bucket)

    def seal(self) -> None:
        """Shutdown: write every open bucket now."""
        self.flush_buckets(force=True)

    def retry_terminated(
        self, key: str, *, episode_id: str | None, reason: str, attempts: int | None = None
    ) -> None:
        """A recovery card is no longer being retried."""
        alert, _ = self._alert_fields(key)
        facts: dict[str, Any] = {}
        if attempts is not None:
            facts["attempts"] = attempts
        self._emit(
            "resolution_retry_terminated",
            self._clock(),
            episode_id=episode_id,
            decision_reason=reason,
            facts=facts,
            association_incomplete=episode_id is None,
            **alert,
        )

    # -- delivery -----------------------------------------------------------

    def attempt_begin(
        self,
        key: str,
        *,
        episode_id: str | None,
        resolution: bool,
        sink: Any,
        severity: Any,
        title: str,
        context: Mapping[str, Any],
        status: str,
        sent_message: str,
        origin: str = "live",
        audit_ref: str | None = None,
        prefix: str = "",
    ) -> AttemptHandle:
        """Record that a real send is starting, with its redacted snapshot."""
        moment = self._clock()
        channel = _channel(sink)
        attempt_id = f"att_{uuid.uuid4().hex}"
        if origin == "test":
            alert: dict[str, Any] = {"alert_type": "test_notification", "alert_key": key}
            redact = _Redactor([])
        else:
            alert, redact = self._alert_fields(key)
        facts, fact_limits = self._facts(context, redact)
        message, message_limits = self._snapshot(
            sink, severity, title, context, status, redact, sent_message, prefix
        )
        refs: tuple[tuple[str, str], ...] = ()
        ref_digests: tuple[str, ...] = ()
        ref_limits: tuple[str, ...] = ()
        if not resolution and origin == "live":
            refs, ref_digests, ref_limits = self._request_ref()
        facts["notification"] = "resolution" if resolution else "firing"
        self._emit(
            "delivery_attempt",
            moment,
            origin=origin,
            episode_id=episode_id,
            attempt_id=attempt_id,
            channel=channel,
            severity=_severity(severity),
            facts=facts,
            message=message,
            request_refs=refs,
            audit_ref=audit_ref,
            association_incomplete=episode_id is None and origin == "live",
            **{
                **alert,
                "subject_digests": (*alert.get("subject_digests", ()), *ref_digests),
                "limitations": (
                    *alert.get("limitations", ()),
                    *fact_limits,
                    *message_limits,
                    *ref_limits,
                ),
            },
        )
        return AttemptHandle(
            attempt_id=attempt_id,
            key=key,
            episode_id=episode_id,
            channel=channel,
            resolution=resolution,
            origin=origin,
            audit_ref=audit_ref,
            started=time.monotonic(),
        )

    def attempt_end(self, handle: AttemptHandle, outcome: DeliveryOutcome) -> None:
        """Record the result of an attempt started by :meth:`attempt_begin`."""
        duration_ms = int(max(0.0, time.monotonic() - handle.started) * 1000)
        if handle.origin == "test":
            alert: dict[str, Any] = {"alert_type": "test_notification", "alert_key": handle.key}
        else:
            alert, _ = self._alert_fields(handle.key)
        facts: dict[str, Any] = {"notification": "resolution" if handle.resolution else "firing"}
        if outcome.http_status is not None:
            facts["http_status"] = outcome.http_status
        self._emit(
            "delivery_result",
            self._clock(),
            origin=handle.origin,
            episode_id=handle.episode_id,
            attempt_id=handle.attempt_id,
            channel=handle.channel,
            outcome=outcome.outcome,
            error_code=outcome.error_code,
            duration_ms=duration_ms,
            facts=facts,
            audit_ref=handle.audit_ref,
            association_incomplete=handle.episode_id is None and handle.origin == "live",
            **alert,
        )

    def render_failed(
        self, key: str, *, episode_id: str | None, resolution: bool, sink: Any
    ) -> None:
        """Rendering raised: nothing left the process."""
        alert, _ = self._alert_fields(key)
        self._emit(
            "delivery_result",
            self._clock(),
            episode_id=episode_id,
            attempt_id=f"att_{uuid.uuid4().hex}",
            channel=_channel(sink),
            outcome="not_sent",
            error_code="render_failed",
            duration_ms=0,
            facts={"notification": "resolution" if resolution else "firing"},
            association_incomplete=episode_id is None,
            **alert,
        )

    def _snapshot(
        self,
        sink: Any,
        severity: Any,
        title: str,
        context: Mapping[str, Any],
        status: str,
        redact: _Redactor,
        sent_message: str,
        prefix: str,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        omitted = [str(k) for k in context if str(k) not in SAFE_FACT_KEYS]
        redact.redacted = False
        shown = {
            k: ("[omitted]" if str(k) not in SAFE_FACT_KEYS else redact(str(v)))
            for k, v in context.items()
        }
        safe_title = redact(title)
        try:
            body = prefix + sink.render(severity, safe_title, shown, status)
        except Exception:
            diag.debug("alert history snapshot render failed", exc_info=True)
            return None, ["message_omitted:render_failed"]
        # The rendered card adds the server block (host address, base URL);
        # it goes through the same value redaction as everything else.
        body = redact(body)
        truncated = False
        channel = _channel(sink)
        if channel == "wecom" and len(sent_message.encode("utf-8")) > 4096:
            # The WeCom sender cuts the card to 4096 bytes before posting.
            truncated = True
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY]
            truncated = True
        return (
            {
                "title": safe_title[:MAX_TITLE],
                "body": body,
                "format": "slack_mrkdwn"
                if channel == "slack"
                else "wecom_markdown"
                if channel == "wecom"
                else "plain",
                "format_version": MESSAGE_FORMAT_VERSION,
                "redacted": True,
                "truncated": truncated,
                "omitted_fields": [redact(name)[:64] for name in omitted[:32]],
            },
            ["redacted:message"] if redact.redacted else [],
        )

    # -- gaps and status ----------------------------------------------------

    def gap_record(
        self,
        counts: collections.Counter[str],
        first_at: float | None,
        last_at: float | None,
    ) -> HistoryRecord | None:
        """A ``history_gap`` record for refusals; queued by the writer itself."""
        total = sum(counts.values())
        if total <= 0:
            return None
        seq, origin_record_id = self._next_id()
        ident = self.identity
        reasons = sorted(counts)
        return finalize(
            HistoryRecord(
                source=BACKEND_SOURCE,
                source_namespace=ident.source_namespace,
                origin_record_id=origin_record_id,
                kind="history_gap",
                occurred_at=dt.datetime.fromtimestamp(last_at or self._clock(), dt.UTC),
                deployment_id=ident.deployment_id,
                target_environment=ident.target_environment,
                source_instance_id=ident.source_instance_id,
                observation_scope="process",
                process_id=ident.process_id,
                producer_seq=seq,
                count=total,
                error_code=reasons[0] if len(reasons) == 1 else "multiple",
                facts={
                    **{f"lost_{reason}": counts[reason] for reason in reasons},
                    "first_lost_at": _iso(first_at) if first_at else None,
                    "last_lost_at": _iso(last_at) if last_at else None,
                },
            )
        )


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _channel(sink: Any) -> str:
    name = str(getattr(sink, "name", "") or "")
    return name if name in ("slack", "wecom") else "other"


def _severity(severity: Any) -> str | None:
    value = getattr(severity, "value", severity)
    return value if value in ("critical", "error", "warn", "info") else None
