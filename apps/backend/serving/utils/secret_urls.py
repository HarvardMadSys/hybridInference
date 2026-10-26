"""Keep credential-bearing URLs out of the log.

Some outbound URLs *are* the credential. A Slack incoming webhook's
``T…/B…/…`` path is the entire secret — there is no separate token, and whoever
holds the URL can post to the channel. A WeCom group-bot webhook carries its
credential in ``?key=…`` instead. ``httpx`` logs the full request URL at INFO
for every call it makes::

    httpx - INFO - HTTP Request: POST https://hooks.slack.com/services/<secret>
    "HTTP/1.1 404 Not Found"

so every alert post wrote that credential into the application log — 221 lines
in 24 hours of ``docker logs`` on the deployment this was found on, readable by
anyone who can read container logs, log shippers or a pasted support bundle.

Of the two available shapes — mute the client's logging, or redact it — these
helpers redact. ``posting_to`` marks one URL as secret for the duration of a
single request and a filter on the ``httpx``/``httpcore`` loggers rewrites that
URL, and only that URL, out of anything logged in that window; the request line
survives as ``scheme://host/#<fingerprint>``, so "did the post happen, and to
which sink" is still answerable from the log. Muting would have been one line
shorter and would have thrown away the answer.

The redaction is deliberately scoped to the sender that holds the secret rather
than applied to the logger globally: ``httpx``'s INFO line is the only record of
every *other* outbound call the gateway makes, and a blanket
``getLogger("httpx").setLevel(WARNING)`` would silently remove diagnostics far
from the one sink being fixed.

Used by the alert senders (``serving.observability.alerts`` and
``serving.admin.failed_request_alerter``) and by the batch completion webhook
sender (``serving.batch.webhooks``), whose caller-supplied URL can carry the
same kind of credential.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The URL being posted to in *this* context, or ``None`` outside a post. A
#: context variable rather than a module global so a concurrent request in
#: another task — which has its own copy of the context — is unaffected by the
#: redaction. It holds the already-derived :class:`UrlRedaction` so the filter
#: and the sender's own log lines do not re-parse the URL or re-hash it.
_SECRET_URL: ContextVar[UrlRedaction | None] = ContextVar("secret_url_in_flight", default=None)

#: ``httpx`` logs the request line; ``httpcore`` logs the request target at
#: DEBUG. Both are filtered because both can carry the path, and the filter is
#: a no-op on any record that does not contain the in-flight secret.
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")

_install_lock = threading.Lock()
_filters_installed = False


def url_fingerprint(url: str) -> str:
    """Return a short, stable id for ``url`` that does not reveal it.

    Truncated to 12 hex characters: enough to tell two webhooks apart in a log,
    and not a way back to a secret with a webhook token's entropy behind it.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class UrlRedaction:
    """Everything one URL's redaction needs, derived once.

    ``scrub`` used to re-parse the URL and re-hash it on every call, which for a
    failing sender meant once per log record *and* once more for the traceback.
    A sender now builds one of these inside :func:`posting_to` and reuses it for
    its own lines; the filter reuses the same instance through the context
    variable.

    ``display`` is the only form safe to log. ``variants`` maps every text that
    can betray the URL — the URL itself, httpx's normalized spelling, the bare
    request target and the bare query — to what replaces it.
    """

    url: str
    fingerprint: str
    display: str
    variants: tuple[tuple[str, str], ...]

    def scrub(self, text: str) -> str:
        """Replace every form of this URL that occurs in ``text``."""
        if not text:
            return text
        out = text
        for needle, replacement in self.variants:
            out = out.replace(needle, replacement)
        return out


def url_redaction(url: str) -> UrlRedaction:
    """Derive the redaction of ``url``: its display form and what to scrub."""
    fingerprint = url_fingerprint(url)
    parts = urlsplit(url)
    # The host is kept because it is not the credential and it is what tells an
    # operator which service was called; everything after it is replaced by the
    # fingerprint, since for these URLs the path is the whole secret.
    scheme = parts.scheme or "?"
    host = parts.netloc or "?"
    display = f"{scheme}://{host}/#{fingerprint}"
    spellings = [url]
    try:
        # HTTPX percent-encodes Unicode and normalizes the request URL before
        # logging it. An extra query parameter must not prevent a key in that
        # same query from matching. Retain the original fingerprint for both.
        normalized = str(httpx.URL(url))
        if normalized != url:
            spellings.append(normalized)
    except (httpx.InvalidURL, ValueError):
        pass
    variants: list[tuple[str, str]] = [(spelling, display) for spelling in spellings]
    for spelling in spellings:
        target = urlsplit(spelling)
        if len(target.path) > 1:
            # A transport error may quote only the request target.
            variants.append((target.path, f"/#{fingerprint}"))
        if target.query:
            variants.append((f"?{target.query}", f"?#{fingerprint}"))
            variants.append((target.query, f"#{fingerprint}"))
    return UrlRedaction(url=url, fingerprint=fingerprint, display=display, variants=tuple(variants))


def scrub(text: str, url: str | UrlRedaction) -> str:
    """Replace every occurrence of ``url``, or of its secret parts, in ``text``.

    The path and query string are replaced separately as well as together,
    because a transport error may quote only the request target. That matters
    for WeCom, whose credential travels in ``?key=…`` rather than in the path.
    The replacement still names the sink by fingerprint, never by secret.

    Takes the already-derived :class:`UrlRedaction` where a sender has one, so a
    failure path does not re-derive the same hashes for every log line.
    """
    if not text or not url:
        return text
    redaction = url if isinstance(url, UrlRedaction) else url_redaction(url)
    return redaction.scrub(text)


def _scrub_arg(arg: object, redaction: UrlRedaction) -> object:
    """Scrub one ``%``-format argument, leaving non-text values untouched."""
    if isinstance(arg, str):
        return redaction.scrub(arg)
    if arg is None or isinstance(arg, (int, float, bool)):
        return arg
    # httpx passes an ``httpx.URL`` object, not a string, so the match has to be
    # made against its rendered form.
    try:
        text = str(arg)
    except Exception:
        # A filter that raises takes the caller's log call down with it, and
        # this one runs inside somebody else's library.
        return arg
    scrubbed = redaction.scrub(text)
    return scrubbed if scrubbed != text else arg


class _RedactSecretUrl(logging.Filter):
    """Rewrite the in-flight secret URL out of a record, never dropping it.

    Matching on the URL's text rather than on an argument position keeps this
    independent of how ``httpx`` words its request line: a future version that
    reorders or rephrases the message is still scrubbed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and keep the record (this filter never suppresses)."""
        redaction = _SECRET_URL.get()
        if redaction is None:
            return True
        if isinstance(record.msg, str):
            record.msg = redaction.scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(a, redaction) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub_arg(v, redaction) for k, v in record.args.items()}
        return True


def _install_filters() -> None:
    """Attach the redacting filter to the HTTP client loggers, once."""
    global _filters_installed
    if _filters_installed:
        return
    with _install_lock:
        if _filters_installed:
            return
        for name in _HTTP_CLIENT_LOGGERS:
            logging.getLogger(name).addFilter(_RedactSecretUrl())
        _filters_installed = True


@contextmanager
def posting_to(url: str) -> Iterator[UrlRedaction]:
    """Redact ``url`` out of HTTP client logs for the duration of one request.

    Yields the derived :class:`UrlRedaction`, whose ``display`` is the form safe
    to log and whose ``scrub`` redacts a rendered traceback. Deriving it here
    means the sender's own log lines and the filter share one parse and one
    hash.
    """
    _install_filters()
    redaction = url_redaction(url)
    token = _SECRET_URL.set(redaction)
    try:
        yield redaction
    finally:
        _SECRET_URL.reset(token)
