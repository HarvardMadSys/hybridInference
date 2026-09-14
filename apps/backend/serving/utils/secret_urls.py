"""Keep credential-bearing URLs out of the log.

Some outbound URLs *are* the credential. A Slack incoming webhook's
``T…/B…/…`` path is the entire secret — there is no separate token, and whoever
holds the URL can post to the channel. ``httpx`` logs the full request URL at
INFO for every call it makes::

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

Used by the two Slack senders (``serving.observability.alerts`` and
``serving.admin.failed_request_alerter``) and deliberately nothing else.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The URL being posted to in *this* context, or "" outside a post. A context
#: variable rather than a module global so a concurrent request in another task
#: — which has its own copy of the context — is unaffected by the redaction.
_SECRET_URL: ContextVar[str] = ContextVar("secret_url_in_flight", default="")

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


def redact_url(url: str) -> str:
    """Return ``scheme://host/#<fingerprint>`` — names the sink, not the secret.

    The host is kept because it is not the credential and it is what tells an
    operator which service was called; everything after it is replaced by the
    fingerprint, since for these URLs the path is the whole secret.
    """
    parts = urlsplit(url)
    scheme = parts.scheme or "?"
    host = parts.netloc or "?"
    return f"{scheme}://{host}/#{url_fingerprint(url)}"


def scrub(text: str, url: str) -> str:
    """Replace every occurrence of ``url`` — or of its secret path — in ``text``."""
    if not text or not url:
        return text
    out = text.replace(url, redact_url(url))
    path = urlsplit(url).path
    if len(path) > 1:
        # Some transport errors are worded in terms of the request *target*
        # alone, which is the secret with the host taken off the front.
        out = out.replace(path, f"/#{url_fingerprint(url)}")
    return out


def _scrub_arg(arg: object, url: str) -> object:
    """Scrub one ``%``-format argument, leaving non-text values untouched."""
    if isinstance(arg, str):
        return scrub(arg, url)
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
    scrubbed = scrub(text, url)
    return scrubbed if scrubbed != text else arg


class _RedactSecretUrl(logging.Filter):
    """Rewrite the in-flight secret URL out of a record, never dropping it.

    Matching on the URL's text rather than on an argument position keeps this
    independent of how ``httpx`` words its request line: a future version that
    reorders or rephrases the message is still scrubbed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact in place and keep the record (this filter never suppresses)."""
        url = _SECRET_URL.get()
        if not url:
            return True
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg, url)
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(a, url) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub_arg(v, url) for k, v in record.args.items()}
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
def posting_to(url: str) -> Iterator[str]:
    """Redact ``url`` out of HTTP client logs for the duration of one request.

    Yields the redacted form so the caller can name the sink in its own log
    lines without ever formatting the secret itself.
    """
    _install_filters()
    token = _SECRET_URL.set(url)
    try:
        yield redact_url(url)
    finally:
        _SECRET_URL.reset(token)
