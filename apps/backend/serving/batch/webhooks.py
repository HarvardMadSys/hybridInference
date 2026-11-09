"""Batch completion webhooks: target checks, signing and delivery.

A user may register one HTTPS endpoint for their batches. When a batch is
sealed -- its results already readable -- an event is written to the outbox in
the same transaction (``serving.storage.batch_store``), and this module delivers
it: at least once, signed, retried with backoff up to :data:`MAX_ATTEMPTS`
times, then given up. Events carry the batch id and type only, never a prompt
or a result; the receiver fetches what it needs with its API key.

**Signing** follows the Standard Webhooks scheme OpenAI's webhooks use:
``webhook-id`` (the event id, stable across retries, for deduplication),
``webhook-timestamp`` (Unix seconds) and ``webhook-signature`` ``v1,<base64
HMAC-SHA256(secret, "<id>.<timestamp>.<body>")>``, where the secret is the
base64 part of the ``whsec_...`` value shown once when the webhook is set.

**Targets** are checked against server-side request forgery three times: the
URL when it is saved (https only, no IP literal or name resolving to a private,
loopback, link-local, multicast, reserved or cloud-metadata address), every
redirect hop again before it is followed (at most :data:`MAX_REDIRECTS`), and
every socket at connect time (:func:`guarded_socket`), which is the check that
holds when a name resolves differently later. ``BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS``
lifts the private-address rule (never the metadata one) for isolated tests.

**Delivery** runs up to :data:`CONCURRENCY` events at once per process, at most
one per user, so a receiver that answers slowly (or not at all, until every
hop has timed out) holds up only its own owner's events. A receiver whose
deliveries keep failing slowly is left alone for a while
(:class:`_SlowReceivers`) before it is tried again.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from serving.utils.logging import get_logger
from serving.utils.metadata_addresses import METADATA_HOSTNAMES, is_metadata_address

logger = get_logger(__name__)

#: Delivery attempts before an event is given up.
MAX_ATTEMPTS = 8
#: Delay before each retry, by the number of attempts made so far.
RETRY_DELAYS_SECONDS = (30, 120, 600, 1800, 3600, 3 * 3600, 6 * 3600)
MAX_REDIRECTS = 3
REQUEST_TIMEOUT_SECONDS = 10.0
#: An event's delivery lease: longer than the slowest delivery (every hop
#: timing out), so it cannot lapse while the event is being sent.
LEASE_SECONDS = int(REQUEST_TIMEOUT_SECONDS * (MAX_REDIRECTS + 1)) + 30
USER_AGENT = "hybridinference-batch-webhooks/1.0"
#: Deliveries one process makes at once; one user's events go one at a time.
CONCURRENCY = 8
#: A failed delivery that took this long is a receiver timing out ...
SLOW_FAILURE_SECONDS = REQUEST_TIMEOUT_SECONDS * 0.9
#: ... and after this many of those in a row its owner's events wait, first
#: this long, then twice as long each time, up to the cap.
SLOW_STREAK = 3
SLOW_HOLD_SECONDS = 600.0
SLOW_HOLD_CAP_SECONDS = 3600.0
#: How often a delivery run looks for newly due events while others are sent.
POLL_SECONDS = 5.0


class WebhookTargetRefused(ValueError):
    """A webhook URL the gateway will not send to."""


def new_secret() -> str:
    """A new signing secret, ``whsec_<base64 of 32 random bytes>``."""
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _secret_key(secret: str) -> bytes:
    raw = secret.removeprefix("whsec_")
    return base64.b64decode(raw)


def sign(secret: str, event_id: str, timestamp: int, body: bytes) -> str:
    """The ``webhook-signature`` header value for one delivery."""
    message = f"{event_id}.{timestamp}.".encode() + body
    digest = hmac.new(_secret_key(secret), message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify(secret: str, headers: dict[str, str], body: bytes, *, tolerance: int = 300) -> bool:
    """Check a delivery's signature, as a receiver would (used by the tests and docs)."""
    try:
        timestamp = int(headers["webhook-timestamp"])
    except (KeyError, ValueError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = sign(secret, headers.get("webhook-id", ""), timestamp, body)
    offered = headers.get("webhook-signature", "").split()
    return any(hmac.compare_digest(expected, candidate) for candidate in offered)


def _refused_address(address: str, *, allow_private: bool) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if is_metadata_address(ip):
        return True
    if allow_private:
        return ip.is_multicast or ip.is_unspecified
    return not ip.is_global or ip.is_multicast


def check_url(url: str, *, allow_private: bool) -> str:
    """Refuse a webhook URL by its form; return it normalized.

    Raises:
        WebhookTargetRefused: Malformed (including a bad port), not https
            (http only with private targets allowed), credentials in it, no
            host, a metadata host, or an IP literal in a refused range.
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise WebhookTargetRefused("The URL must be a string of at most 2048 characters.")
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        # Reading the port validates it (a non-number or one out of range raises).
        _port = parts.port
    except ValueError:
        raise WebhookTargetRefused("The webhook URL is malformed.") from None
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise WebhookTargetRefused("The webhook URL must use https.")
    if parts.username or parts.password:
        raise WebhookTargetRefused("The webhook URL must not contain credentials.")
    host = (hostname or "").rstrip(".").lower()
    if not host:
        raise WebhookTargetRefused("The webhook URL has no host.")
    local_name = host == "localhost" or host.endswith(".localhost")
    if host in METADATA_HOSTNAMES or (local_name and not (allow_private and host == "localhost")):
        raise WebhookTargetRefused("The webhook host is not allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # A name: it must encode as IDNA (no empty or over-long label), or the
        # lookup would fail with a UnicodeError before any network I/O.
        try:
            host.encode("idna")
        except UnicodeError:
            raise WebhookTargetRefused("The webhook URL is malformed.") from None
    else:
        if _refused_address(host, allow_private=allow_private):
            raise WebhookTargetRefused("The webhook address is not allowed.")
    return parts.geturl()


async def check_resolves_public(url: str, *, allow_private: bool) -> None:
    """Refuse a URL whose host resolves to any refused address (at save time).

    Raises:
        WebhookTargetRefused: A resolved address is refused, or the name does
            not resolve.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise WebhookTargetRefused("The webhook URL is malformed.") from None
    loop = asyncio.get_running_loop()
    try:
        host.encode("idna")
    except UnicodeError:
        raise WebhookTargetRefused("The webhook URL is malformed.") from None
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebhookTargetRefused("The webhook host does not resolve.") from exc
    for info in infos:
        if _refused_address(info[4][0], allow_private=allow_private):
            raise WebhookTargetRefused(
                "The webhook host resolves to an address that is not allowed."
            )


def url_problem(url: str, *, allow_private: bool) -> tuple[str | None, str | None]:
    """:func:`check_url` as a value: ``(normalized url, None)`` or ``(None, problem)``."""
    try:
        return check_url(url, allow_private=allow_private), None
    except WebhookTargetRefused as refused:
        return None, refused.args[0] if refused.args else "The webhook URL is not allowed."


async def resolution_problem(url: str, *, allow_private: bool) -> str | None:
    """:func:`check_resolves_public` as a value: None, or why the host is refused."""
    try:
        await check_resolves_public(url, allow_private=allow_private)
    except WebhookTargetRefused as refused:
        return refused.args[0] if refused.args else "The webhook host is not allowed."
    return None


def guarded_socket(*, allow_private: bool) -> Any:
    """An aiohttp ``socket_factory`` refusing every connection to a refused address."""

    def factory(addr_info: tuple[Any, ...]) -> socket.socket:
        family, type_, proto, _, sockaddr = addr_info
        if _refused_address(str(sockaddr[0]), allow_private=allow_private):
            raise OSError(errno.EACCES, f"webhook connection to {sockaddr[0]} refused")
        return socket.socket(family=family, type=type_, proto=proto)

    return factory


async def deliver(
    *,
    url: str,
    secret: str,
    event_id: str,
    payload: dict[str, Any],
    allow_private: bool,
    session_factory: Any = None,
) -> tuple[bool, int | None, str | None]:
    """POST one event; return ``(delivered, status, error)``.

    Redirects are followed by hand, each hop checked like the original URL, and
    the socket guard checks every connection. Any 2xx is a delivery.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        "webhook-id": event_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(secret, event_id, timestamp, body),
    }
    try:
        target = check_url(url, allow_private=allow_private)
    except WebhookTargetRefused as exc:
        return False, None, str(exc)
    connector = aiohttp.TCPConnector(
        socket_factory=guarded_socket(allow_private=allow_private), force_close=True
    )
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    make_session = session_factory or aiohttp.ClientSession
    try:
        async with make_session(connector=connector, timeout=timeout, trust_env=False) as session:
            for _hop in range(MAX_REDIRECTS + 1):
                async with session.post(
                    target, data=body, headers=headers, allow_redirects=False
                ) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        return True, status, None
                    if status in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        try:
                            target = check_url(
                                urljoin(target, resp.headers["location"]),
                                allow_private=allow_private,
                            )
                        except WebhookTargetRefused as exc:
                            return False, status, f"redirect refused: {exc}"
                        continue
                    return False, status, f"HTTP {status}"
            return False, None, "too many redirects"
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        return False, None, f"{type(exc).__name__}: {exc}"[:300]


def next_attempt_at(attempts: int) -> datetime | None:
    """When to retry after *attempts* deliveries failed; None to give up."""
    if attempts >= MAX_ATTEMPTS:
        return None
    delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
    return datetime.now(UTC) + timedelta(seconds=delay)


class _SlowReceivers:
    """The users whose webhook receiver keeps failing slowly, held back for a while.

    Kept per process. A delivery that fails after about the whole request
    timeout counts against its receiver; :data:`SLOW_STREAK` of them in a row
    hold the owner's events back for :data:`SLOW_HOLD_SECONDS`, doubling with
    each further slow failure up to :data:`SLOW_HOLD_CAP_SECONDS`. Their events
    stay due and are sent once the hold ends; any delivery that is not a slow
    failure clears the record.
    """

    def __init__(self) -> None:
        self._streak: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def held(self, now: float) -> list[str]:
        for user_id in [u for u, until in self._until.items() if until <= now]:
            del self._until[user_id]
        return sorted(self._until)

    def record(self, user_id: str, *, slow_failure: bool, now: float) -> None:
        if not slow_failure:
            self._streak.pop(user_id, None)
            self._until.pop(user_id, None)
            return
        streak = self._streak.get(user_id, 0) + 1
        self._streak[user_id] = streak
        if streak >= SLOW_STREAK:
            hold = min(SLOW_HOLD_CAP_SECONDS, SLOW_HOLD_SECONDS * 2 ** (streak - SLOW_STREAK))
            self._until[user_id] = now + hold
            logger.info(
                "batch webhook receiver of user %s failed slowly %d times in a row; "
                "its events wait %.0fs",
                user_id,
                streak,
                hold,
                extra={"event": "batch_webhook_receiver_held", "user_id": user_id},
            )


_SLOW_RECEIVERS = _SlowReceivers()


async def deliver_due_events(
    *,
    webhook_store: Any,
    worker_id: str,
    allow_private: bool,
    decrypt: Any,
    limit: int = 200,
    concurrency: int = CONCURRENCY,
    poll_seconds: float = POLL_SECONDS,
    slow: _SlowReceivers | None = None,
) -> int:
    """Deliver due events, up to *concurrency* at once; return how many were tried.

    Each event is leased just before it is sent, never left waiting in a local
    queue: its lease (:data:`LEASE_SECONDS`) outlasts the longest a delivery
    can take, so no other worker sends it meanwhile. At most one event per
    user is in flight, and the events of a receiver that keeps failing slowly
    wait (:class:`_SlowReceivers`), so one slow endpoint never holds up anyone
    else's. Recording the outcome is fenced on this worker's lease and
    attempt. Returns once nothing more is due and nothing is in flight, or
    once *limit* events have been tried and sent.
    """
    receivers = slow if slow is not None else _SLOW_RECEIVERS
    in_flight: dict[asyncio.Task[None], str] = {}
    tried = 0
    look = True
    claiming = True
    try:
        while True:
            while claiming and look and len(in_flight) < concurrency and tried < limit:
                busy = set(in_flight.values()) | set(receivers.held(time.monotonic()))
                try:
                    events = await webhook_store.claim_due_events(
                        worker_id=worker_id,
                        limit=1,
                        lease_seconds=LEASE_SECONDS,
                        exclude_users=sorted(busy),
                    )
                except Exception:
                    if not in_flight:
                        raise
                    # Let what is being sent finish; the next run looks again.
                    logger.warning("batch webhook events could not be claimed", exc_info=True)
                    claiming = False
                    break
                if not events:
                    look = False
                    break
                event = events[0]
                tried += 1
                task = asyncio.create_task(
                    _send_event(
                        event,
                        webhook_store=webhook_store,
                        worker_id=worker_id,
                        allow_private=allow_private,
                        decrypt=decrypt,
                        receivers=receivers,
                    ),
                    name=f"batch_webhook:{event['id']}",
                )
                in_flight[task] = str(event["user_id"])
            if not in_flight:
                return tried
            done, _ = await asyncio.wait(
                in_flight, timeout=poll_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                user_id = in_flight.pop(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.warning(
                        "batch webhook delivery for user %s failed",
                        user_id,
                        exc_info=task.exception(),
                        extra={"event": "batch_webhook_delivery_error"},
                    )
            # A user's turn came free, or time passed: look again.
            look = True
    finally:
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)


async def _send_event(
    event: dict[str, Any],
    *,
    webhook_store: Any,
    worker_id: str,
    allow_private: bool,
    decrypt: Any,
    receivers: _SlowReceivers,
) -> None:
    """Send one leased event and record how it went."""
    attempt = int(event["attempts"])
    payload = event["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not event.get("enabled"):
        await webhook_store.mark_failed(
            event_id=event["id"],
            worker_id=worker_id,
            attempt=attempt,
            error="webhook disabled",
            status_code=None,
            next_attempt_at=None,
        )
        return
    try:
        secret = decrypt(event["secret_encrypted"])
    except Exception:
        await webhook_store.mark_failed(
            event_id=event["id"],
            worker_id=worker_id,
            attempt=attempt,
            error="signing secret unreadable",
            status_code=None,
            next_attempt_at=None,
        )
        return
    started = time.monotonic()
    delivered, status, error = await deliver(
        url=event["url"],
        secret=secret,
        event_id=event["id"],
        payload=payload,
        allow_private=allow_private,
    )
    finished = time.monotonic()
    receivers.record(
        str(event["user_id"]),
        slow_failure=not delivered and finished - started >= SLOW_FAILURE_SECONDS,
        now=finished,
    )
    if delivered:
        await webhook_store.mark_delivered(
            event_id=event["id"],
            worker_id=worker_id,
            attempt=attempt,
            status_code=int(status or 200),
        )
    else:
        await webhook_store.mark_failed(
            event_id=event["id"],
            worker_id=worker_id,
            attempt=attempt,
            error=error or "delivery failed",
            status_code=status,
            next_attempt_at=next_attempt_at(attempt),
        )
