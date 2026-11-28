"""Hourly watch on administrator accounts, reported through the alert channel.

Every hour one replica reads what administrators did since the last report and
sends up to two cards to the configured Slack or WeCom channel
(:func:`serving.observability.alerts.alert_slack`):

* **Administrator access from a new address** (alert type ``admin_new_address``).
  An administrator signed in, or failed to sign in, from an address the
  account had not used in the previous ``lookback_days`` (neither signed in
  from nor acted from), or an administrator's session acted from such an
  address, or the shared ``ADMIN_TOKEN`` acted from an address it had not
  acted from in that time. An address is an IPv4 address or an IPv6 /64
  (:func:`serving.utils.client_addresses.address_key`), so a host's
  rotating IPv6 privacy addresses are one address.
* **Dangerous administrator actions** (``admin_dangerous_action``): an audited
  action in :data:`DANGEROUS_ACTIONS`, an account given the admin or internal
  role, or a change to a setting in :data:`SENSITIVE_SETTINGS` or to a secret
  one, by an administrator or the admin token. Refused attempts count too.

**Each finding is reported once.** A run holds an advisory lock, so replicas
take turns, and starts where the last report ended: once a card is delivered,
an ``admin_activity.alerted`` audit row (actor ``system``) records how far it
covered. A card the channel did not take (it refused it, or alerts are
snoozed) is not recorded, so the next run reports it again with whatever
followed. A muted alert type is recorded as handled, without a card. Nothing
older than :data:`MAX_CATCH_UP`, the longest snooze the admin API accepts, is
reported. The first run ever covers the last hour, and records that it started
there (an ``admin_activity.started`` row), so every later run in any process
starts from a recorded point.

The monitor reads nothing while no alert channel is configured, and is off
when the alert rules set ``admin_activity.enabled: false``. It needs the
gateway to know client addresses (see the trusted-proxy settings): an event
whose address was not recorded is never new.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from serving.audit import AuditActor, AuditTarget, audit_columns
from serving.observability.alert_config import AdminActivity
from serving.observability.alerts import AlertSeverity, alert_on_transition, configured_sink
from serving.storage.audit_schema import insert_audit_row
from serving.utils.client_addresses import address_key
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import asyncpg

logger = get_logger(__name__)

#: The two alert types (registered in :mod:`serving.observability.alert_types`).
NEW_ADDRESS_ALERT = "admin_new_address"
DANGEROUS_ACTION_ALERT = "admin_dangerous_action"
ALERT_TITLES = {
    NEW_ADDRESS_ALERT: "Administrator access from a new address",
    DANGEROUS_ACTION_ALERT: "Dangerous administrator actions",
}

#: The minute past each hour the monitor runs, clear of the hourly rollups
#: (minutes 5, 10, 15, ...) and the user-tag detector (2, 12, 22, ...).
RUN_MINUTE = 7
#: Rows younger than this are left to the next run, so that a write still
#: committing when a run reads is not passed over.
SETTLE = timedelta(minutes=1)
#: What the very first run covers.
FIRST_WINDOW = timedelta(hours=1)
#: The furthest back a run reports: the longest snooze the admin API accepts.
MAX_CATCH_UP = timedelta(days=7)
#: Findings one card lists; the rest are counted.
MAX_LISTED = 20

#: The audit actions that record what a card covered, and where monitoring
#: started for an alert type that has had no card yet.
ALERTED_ACTION = "admin_activity.alerted"
STARTED_ACTION = "admin_activity.started"
#: How findings name the shared admin token, which belongs to no account.
TOKEN_LABEL = "ADMIN_TOKEN"
_TOKEN = "token"

#: Audited actions that are dangerous whatever their details: they grant or
#: remove access, expose users' data or the gateway's secrets, change where
#: traffic goes, or silence the monitoring and the record of it.
DANGEROUS_ACTIONS: dict[str, str] = {
    "hard_delete_user": "permanently deleted an account",
    "delete_user": "deleted an account",
    "two_factor.reset": "removed an account's authenticator",
    "unlink_sso_identity": "disconnected an account's single sign-on identity",
    "create_service_account": "created a service account",
    "reveal_key": "revealed an API key",
    "export_requests": "exported request logs",
    "batch.requests.read": "read a batch's prompts and answers",
    "config.backup": "downloaded a configuration backup",
    "config.restore": "restored a configuration backup",
    "add_provider_key": "added a provider key",
    "delete_provider_key": "deleted a provider key",
    "remove_provider_env_key": "removed a provider key set in the environment",
    "provider_definition.create": "added a provider definition",
    "provider_definition.update": "changed a provider definition",
    "provider_definition.delete": "deleted a provider definition",
    "alerts.mute": "muted an alert type",
    "alerts.snooze": "snoozed all alerts",
    "login_events.purge": "purged sign-in records",
    "auth_blocks.clear": "lifted an auth-failure block",
    "system.restart": "restarted the gateway",
}

#: Settings whose change is dangerous: where alerts go, who may sign in or sign
#: up and how, and which addresses the gateway believes. A change to any
#: secret setting is dangerous too. Compared upper-case.
SENSITIVE_SETTINGS: frozenset[str] = frozenset(
    {
        "ALERTS_ENABLED",
        "ALERT_RULES",
        "SLACK_WEBHOOK_URL",
        "SLACK_ALERTS_WEBHOOK_URL",
        "WECOM_ALERTS_WEBHOOK_URL",
        "SSO_PROVIDERS",
        "ADMIN_EMAILS",
        "ADMIN_TWO_FACTOR_REQUIRED",
        "USER_AUTH_ENABLED",
        "SIGNUP_ENABLED",
        "SIGNUP_REQUIRE_EMAIL_VERIFICATION",
        "TRUST_PROXY_HEADERS",
        "TRUSTED_PROXIES",
        "TRUST_CLOUDFLARE_HEADERS",
        "TRUSTED_CLOUDFLARE_NETWORKS",
        "TRUST_X_REAL_IP",
        "TRUSTED_DIRECT_CLIENT_NETWORKS",
        "CORS_ALLOWED_ORIGINS",
        "ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS",
    }
)

#: Roles whose grant is dangerous.
RAISED_ROLES = frozenset({"admin", "internal"})

#: Actions that change a setting, and how a finding words each.
_SETTING_VERBS = {"config.update": "changed", "config.reset": "reset", "settings.update": "changed"}

#: One run at a time across every replica: a session advisory lock on the run's
#: connection, released when it ends.
_LOCK_KEY = "hashtextextended('hybridinference:admin-activity-monitor', 0)"
_LOCK_SQL = f"SELECT pg_try_advisory_lock({_LOCK_KEY})"
_UNLOCK_SQL = f"SELECT pg_advisory_unlock({_LOCK_KEY})"

#: How far each alert type has been scanned with nothing to report, in this
#: process. A run with no findings writes no audit row, so this saves the next
#: run from reading that stretch again; another process, or this one after a
#: restart, starts from the last audit row instead and simply finds nothing.
_SCANNED_THROUGH: dict[str, datetime] = {}


# --- findings ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One line of a card, with the moment it happened."""

    at: datetime
    text: str


@dataclass(slots=True)
class _Use:
    """One account's (or the token's) use of one new address in the window."""

    first_at: datetime
    address: str
    sign_ins: int = 0
    failures: int = 0
    actions: list[str] = field(default_factory=list)
    client: str | None = None


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _moment(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def new_address_findings(
    sign_ins: Iterable[Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
    known: Mapping[str, set[str]],
) -> list[Finding]:
    """The uses of a new address in the window, one finding per account and address.

    *sign_ins* rows have ``who``, ``label``, ``ip``, ``at``, ``outcome`` and
    ``user_agent``; *actions* rows have ``who``, ``label``, ``ip``, ``at`` and
    ``action``. ``who`` is ``user:<id>`` for an account and ``token`` for the
    admin token. *known* maps each ``who`` to the address keys it used in the
    lookback. An event whose address has no key (it was not recorded) is
    never new.
    """
    events = [(row["at"], True, row) for row in sign_ins]
    events += [(row["at"], False, row) for row in actions]
    events.sort(key=lambda event: event[0])
    uses: dict[tuple[str, str], _Use] = {}
    labels: dict[str, str] = {}
    for at, is_sign_in, row in events:
        key = address_key(row["ip"])
        who = row["who"]
        if key is None or key in known.get(who, ()):
            continue
        labels[who] = row["label"]
        use = uses.get((who, key))
        if use is None:
            use = uses[(who, key)] = _Use(first_at=at, address=str(row["ip"]).strip())
        if not is_sign_in:
            use.actions.append(row["action"])
        elif row["outcome"] == "success":
            use.sign_ins += 1
        else:
            use.failures += 1
        if is_sign_in and use.client is None and row.get("user_agent"):
            use.client = row["user_agent"]
    findings = [
        Finding(use.first_at, _describe_use(labels[who], who, key, use))
        for (who, key), use in uses.items()
    ]
    findings.sort(key=lambda finding: (finding.at, finding.text))
    return findings


def _describe_use(label: str, who: str, key: str, use: _Use) -> str:
    parts = []
    if use.sign_ins:
        parts.append(_plural(use.sign_ins, "sign-in"))
    if use.failures:
        parts.append(_plural(use.failures, "failed sign-in attempt"))
    if use.actions:
        names = sorted(set(use.actions))
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        parts.append(f"{_plural(len(use.actions), 'action')} ({shown})")
    where = use.address if use.address == key else f"{use.address} ({key})"
    text = f"{label} from {where}: {', '.join(parts)}; first at {_moment(use.first_at)}"
    if who != _TOKEN and not use.sign_ins and not use.failures:
        text += "; no sign-in from this address"
    if use.client:
        text += f"; {_clip(use.client)}"
    return text


def dangerous_description(action: str, details: Mapping[str, Any] | None) -> str | None:
    """What an audited action did, when it is dangerous; None when it is not."""
    if action in DANGEROUS_ACTIONS:
        return DANGEROUS_ACTIONS[action]
    details = details or {}
    if action == "update_user":
        values = details.get("values")
        role = values.get("role") if isinstance(values, dict) else None
        return f"gave an account the {role} role" if role in RAISED_ROLES else None
    if action in _SETTING_VERBS:
        key = str(details.get("key") or "").upper()
        secret = details.get("changed") is True or details.get("marked_secret") is True
        if key in SENSITIVE_SETTINGS or (secret and key):
            return f"{_SETTING_VERBS[action]} the setting {key}"
    return None


def dangerous_findings(rows: Iterable[Mapping[str, Any]]) -> list[Finding]:
    """One finding per dangerous action among audit *rows*, oldest first.

    Each row has ``at``, ``action``, ``actor_label``, ``ip``, ``target_label``
    (may be empty), ``success`` and ``details`` (a mapping or None).
    """
    findings = []
    for row in rows:
        description = dangerous_description(row["action"], row["details"])
        if description is None:
            continue
        target = f" ({row['target_label']})" if row.get("target_label") else ""
        refused = ", refused," if row["success"] is False else ""
        where = row["ip"] or "an unrecorded address"
        findings.append(
            Finding(
                row["at"],
                f"{row['actor_label']} {description}{target}{refused} from {where} "
                f"at {_moment(row['at'])}",
            )
        )
    findings.sort(key=lambda finding: (finding.at, finding.text))
    return findings


def card(
    findings: list[Finding], *, since: datetime, until: datetime, lookback_days: int | None = None
) -> dict[str, str]:
    """The alert context for *findings*: the window, then one line each."""
    context = {"window": f"{_moment(since)} to {_moment(until)}"}
    if lookback_days is not None:
        context["new"] = f"an address the account had not used in the {lookback_days} days before"
    for index, finding in enumerate(findings[:MAX_LISTED], 1):
        context[str(index)] = finding.text
    if len(findings) > MAX_LISTED:
        context["more"] = (
            f"{len(findings) - MAX_LISTED} more: see the admin audit log and sign-in records"
        )
    return context


# --- reading the database ----------------------------------------------------

#: How far the last card of an alert type covered, or where monitoring started.
_WATERMARK_SQL = """
    SELECT max((details->>'through')::timestamptz)
    FROM admin_audit_log
    WHERE action IN ('admin_activity.alerted', 'admin_activity.started')
      AND target_type = 'alert_type'
      AND target_id = $1
"""

#: Administrators' sign-ins in the window, successful or not.
_SIGN_INS_SQL = """
    SELECT e.user_id, e.ip, e.created_at AS at, e.outcome, e.user_agent,
           COALESCE(u.email, u.login_name, u.phone, u.id) AS label
    FROM login_events e
    JOIN users u ON u.id = e.user_id
    WHERE u.role = 'admin'
      AND e.created_at > $1 AND e.created_at <= $2
"""

#: Audited actions in the window by an administrator's session or the token.
_ACTIONS_SQL = """
    SELECT a.timestamp AS at, a.action, a.actor_type, a.actor_id, a.source_ip AS ip,
           a.success, a.details, a.target_type, a.target_id,
           COALESCE(actor.email, actor.login_name, actor.phone, a.actor_id) AS actor_label,
           COALESCE(target.email, target.login_name, target.phone, a.target_user_id)
               AS target_user_label
    FROM admin_audit_log a
    LEFT JOIN users actor ON actor.id = a.actor_id
    LEFT JOIN users target ON target.id = a.target_user_id
    WHERE a.timestamp > $1 AND a.timestamp <= $2
      AND a.actor_type IN ('admin', 'admin_token')
"""

#: The addresses each administrator, and the token, used in the lookback:
#: successful sign-ins and audited actions.
_KNOWN_SQL = """
    SELECT 'user:' || user_id AS who, ip
    FROM login_events
    WHERE outcome = 'success' AND user_id = ANY($1::text[])
      AND created_at > $2 AND created_at <= $3 AND ip IS NOT NULL
    GROUP BY 1, 2
    UNION
    SELECT CASE WHEN actor_type = 'admin_token' THEN 'token' ELSE 'user:' || actor_id END,
           source_ip
    FROM admin_audit_log
    WHERE timestamp > $2 AND timestamp <= $3 AND source_ip IS NOT NULL
      AND (actor_type = 'admin_token' OR (actor_type = 'admin' AND actor_id = ANY($1::text[])))
    GROUP BY 1, 2
"""


def _details(raw: Any) -> dict[str, Any] | None:
    """An audit row's details as a mapping; asyncpg hands JSONB back as text."""
    if raw is None or isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _who(actor_type: str, actor_id: str | None) -> str:
    return _TOKEN if actor_type == "admin_token" else f"user:{actor_id}"


async def _new_address(
    conn: asyncpg.Connection, since: datetime, until: datetime, lookback: timedelta
) -> list[Finding]:
    sign_ins = [
        {
            "who": f"user:{row['user_id']}",
            "label": row["label"],
            "ip": row["ip"],
            "at": row["at"],
            "outcome": row["outcome"],
            "user_agent": row["user_agent"],
        }
        for row in await conn.fetch(_SIGN_INS_SQL, since, until)
    ]
    actions = [
        {
            "who": _who(row["actor_type"], row["actor_id"]),
            "label": TOKEN_LABEL if row["actor_type"] == "admin_token" else row["actor_label"],
            "ip": row["ip"],
            "at": row["at"],
            "action": row["action"],
        }
        for row in await conn.fetch(_ACTIONS_SQL, since, until)
        if row["actor_type"] == "admin_token" or row["actor_id"]
    ]
    if not sign_ins and not actions:
        return []
    user_ids = sorted(
        {row["who"].removeprefix("user:") for row in (*sign_ins, *actions) if row["who"] != _TOKEN}
    )
    known: dict[str, set[str]] = {}
    for row in await conn.fetch(_KNOWN_SQL, user_ids, since - lookback, since):
        key = address_key(row["ip"])
        if key is not None:
            known.setdefault(row["who"], set()).add(key)
    return new_address_findings(sign_ins, actions, known)


async def _dangerous(
    conn: asyncpg.Connection, since: datetime, until: datetime, lookback: timedelta
) -> list[Finding]:
    rows = []
    for row in await conn.fetch(_ACTIONS_SQL, since, until):
        target = row["target_user_label"] or (
            f"{row['target_type']} {row['target_id']}"
            if row["target_type"] and row["target_id"]
            else ""
        )
        rows.append(
            {
                "at": row["at"],
                "action": row["action"],
                "actor_label": (
                    TOKEN_LABEL if row["actor_type"] == "admin_token" else row["actor_label"]
                ),
                "ip": row["ip"],
                "target_label": target,
                "success": row["success"],
                "details": _details(row["details"]),
            }
        )
    return dangerous_findings(rows)


_COLLECTORS = {NEW_ADDRESS_ALERT: _new_address, DANGEROUS_ACTION_ALERT: _dangerous}


async def _start(conn: asyncpg.Connection, alert_type: str, until: datetime) -> datetime:
    """Where a run of *alert_type* starts: after the last card, within the catch-up bound.

    The first run ever records where monitoring started, an hour back, so that
    every later run, in any process, starts from a recorded point: a first
    card that is not delivered is then reported by whichever run comes next.
    """
    reported = await conn.fetchval(_WATERMARK_SQL, alert_type)
    if reported is None:
        reported = until - FIRST_WINDOW
        await _write(conn, STARTED_ACTION, alert_type, {"through": reported.isoformat()})
    scanned = _SCANNED_THROUGH.get(alert_type)
    start = max(reported, scanned) if scanned is not None else reported
    return max(start, until - MAX_CATCH_UP)


async def _record(
    conn: asyncpg.Connection,
    alert_type: str,
    *,
    since: datetime,
    until: datetime,
    findings: int,
    delivered: bool,
) -> None:
    """Write the audit row that says a card of *alert_type* covered through *until*."""
    details: dict[str, Any] = {
        "since": since.isoformat(),
        "through": until.isoformat(),
        "findings": findings,
        "delivered": delivered,
    }
    if not delivered:
        details["reason"] = "muted"
    await _write(conn, ALERTED_ACTION, alert_type, details)


async def _write(
    conn: asyncpg.Connection, action: str, alert_type: str, details: dict[str, Any]
) -> None:
    """One of the monitor's own audit rows, by the system, about *alert_type*."""
    await insert_audit_row(
        conn,
        columns=audit_columns(
            admin_ip=None,
            target_user_id=None,
            actor=AuditActor.system(),
            target=AuditTarget("alert_type", alert_type),
        ),
        action=action,
        details={"alert_type": alert_type, **details},
        success=True,
    )


async def _type_muted(alert_type: str) -> bool:
    """Whether an administrator muted *alert_type*; fails open, like the sink's own check."""
    try:
        from serving.observability.alert_mutes import is_alert_type_muted

        return await is_alert_type_muted(alert_type)
    except Exception:
        logger.debug("admin_activity_monitor: mute check failed; sending", exc_info=True)
        return False


async def _send(alert_type: str, stamp: str, context: dict[str, str]) -> bool:
    """Post one card, then close its incident without a word.

    A card reports events, not a condition that later recovers, so it opens
    and closes in one go; nothing would ever observe it again otherwise. The
    keys are spelled out per type, so that the alert-type catalog's guard can
    read them.
    """
    title = ALERT_TITLES[alert_type]
    sent = False
    for breached in (True, False):
        if alert_type == NEW_ADDRESS_ALERT:
            result = await alert_on_transition(
                key=f"admin_new_address:{stamp}",
                breached=breached,
                severity=AlertSeverity.WARN,
                title=title,
                context=lambda: context,
                cooldown_sec=0,
                kind="state",
                announce_resolution=False,
            )
        else:
            result = await alert_on_transition(
                key=f"admin_dangerous_action:{stamp}",
                breached=breached,
                severity=AlertSeverity.WARN,
                title=title,
                context=lambda: context,
                cooldown_sec=0,
                kind="state",
                announce_resolution=False,
            )
        if breached:
            sent = result
    return sent


async def run_monitor(
    pool: Any, config: AdminActivity | None = None, *, now: datetime | None = None
) -> dict[str, int] | None:
    """Report what administrators did since the last report; return the findings per type.

    ``None`` when another replica's run holds the lock; ``{}`` when the
    monitor is off or no alert channel is configured. A type maps to 0 when
    it had nothing to report or its card was not delivered. Raises what the
    database raises; :func:`monitor_job` is the scheduler's wrapper that never
    does.
    """
    config = config or AdminActivity()
    if not config.enabled or configured_sink() is None:
        return {}
    lookback = timedelta(days=config.lookback_days)
    async with pool.acquire() as conn:
        if not await conn.fetchval(_LOCK_SQL):
            return None
        try:
            until = (now or await conn.fetchval("SELECT now()")) - SETTLE
            reported: dict[str, int] = {}
            for alert_type, collect in _COLLECTORS.items():
                since = await _start(conn, alert_type, until)
                if since >= until:
                    reported[alert_type] = 0
                    continue
                findings = await collect(conn, since, until, lookback)
                reported[alert_type] = await _report(
                    conn, alert_type, findings, since=since, until=until, config=config
                )
            return reported
        finally:
            # The connection goes back to the pool: the lock must not go with it.
            await conn.execute(_UNLOCK_SQL)


async def _report(
    conn: asyncpg.Connection,
    alert_type: str,
    findings: list[Finding],
    *,
    since: datetime,
    until: datetime,
    config: AdminActivity,
) -> int:
    """Send the card for *findings*, and record it once it is delivered or muted."""
    if not findings:
        _SCANNED_THROUGH[alert_type] = until
        return 0
    if await _type_muted(alert_type):
        await _record(
            conn, alert_type, since=since, until=until, findings=len(findings), delivered=False
        )
        _SCANNED_THROUGH[alert_type] = until
        return 0
    lookback_days = config.lookback_days if alert_type == NEW_ADDRESS_ALERT else None
    context = card(findings, since=since, until=until, lookback_days=lookback_days)
    if not await _send(alert_type, until.isoformat(), context):
        logger.warning(
            "admin_activity_monitor: %s card for %d finding(s) not delivered; "
            "the next run reports them again",
            alert_type,
            len(findings),
        )
        return 0
    await _record(
        conn, alert_type, since=since, until=until, findings=len(findings), delivered=True
    )
    _SCANNED_THROUGH[alert_type] = until
    return len(findings)


async def monitor_job(pool: Any, config: AdminActivity | None = None) -> None:
    """The scheduled run, for the scheduler: never raises."""
    started_at = time.monotonic()
    outcome = "error"
    reported: dict[str, int] | None = None
    try:
        reported = await run_monitor(pool, config)
        outcome = "skipped" if reported is None else "ok"
    except Exception:
        logger.exception("admin_activity_monitor failed")
    finally:
        logger.info(
            "admin_activity_monitor: outcome=%s reported=%s duration_ms=%d",
            outcome,
            reported or {},
            int((time.monotonic() - started_at) * 1000),
            extra={"event": "admin_activity_monitor", "outcome": outcome},
        )


def load_config() -> AdminActivity:
    """The ``admin_activity`` section of the alert rules in force; defaults when unreadable."""
    try:
        from serving.config.documents import get as get_document
        from serving.observability.alert_config import alert_config_from_data

        return alert_config_from_data(get_document("alerts")).admin_activity
    except Exception:
        logger.warning(
            "admin_activity_monitor: alert rules unreadable; using defaults", exc_info=True
        )
        return AdminActivity()


def register_monitor_job(scheduler: Any, pool: Any, config: AdminActivity | None = None) -> None:
    """Register the monitor on the running AsyncIOScheduler, hourly at :data:`RUN_MINUTE`."""
    from apscheduler.triggers.cron import CronTrigger

    config = config or load_config()
    if not config.enabled:
        logger.info("admin_activity_monitor: off (alert rules set admin_activity.enabled: false)")
        return
    scheduler.add_job(
        monitor_job,
        trigger=CronTrigger(minute=RUN_MINUTE, timezone=UTC),
        args=[pool, config],
        id="admin_activity_monitor",
        replace_existing=True,
        misfire_grace_time=1800,
        coalesce=True,
        max_instances=1,
    )
    logger.info(
        "admin_activity_monitor: registered (hourly at :%02d, lookback %d days)",
        RUN_MINUTE,
        config.lookback_days,
    )
