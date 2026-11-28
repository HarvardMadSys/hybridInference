"""What uvicorn's access log is allowed to say about a request."""

from __future__ import annotations

import logging

import pytest

from serving.utils.logging import (
    _CallbackQueryFilter,
    _QuietPathFilter,
    attach_quiet_access_filter,
)


def _access_record(path: str, status: int = 302) -> logging.LogRecord:
    """A record shaped as uvicorn's access logger emits it."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:50000", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_a_single_sign_on_callback_is_logged_without_its_query():
    record = _access_record(
        "/auth/sso/example-univ/callback?state=one-time-state&code=one-time-code&iss=https%3A%2F%2Fidp"
    )

    assert _CallbackQueryFilter().filter(record) is True

    line = record.getMessage()
    assert line == '127.0.0.1:50000 - "GET /auth/sso/example-univ/callback HTTP/1.1" 302'
    assert "one-time" not in line


def test_uvicorn_writes_the_line_without_the_query():
    from uvicorn.logging import AccessFormatter

    record = _access_record(
        "/auth/sso/example-univ/callback?state=one-time-state&code=one-time-code"
    )
    _CallbackQueryFilter().filter(record)

    line = AccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    ).format(record)

    assert '"GET /auth/sso/example-univ/callback HTTP/1.1" 302' in line
    assert "one-time" not in line


def test_a_provider_error_on_the_callback_is_logged_without_its_query():
    record = _access_record(
        "/auth/sso/example-univ/callback?error=access_denied&state=one-time-state"
    )

    _CallbackQueryFilter().filter(record)

    assert record.getMessage().endswith('"GET /auth/sso/example-univ/callback HTTP/1.1" 302')


@pytest.mark.parametrize(
    "path",
    [
        "/admin/users?limit=20&offset=40",
        "/auth/sso/example-univ/start?return_to=%2Fdashboard",
        "/auth/sso/example-univ/callback",
        "/v1/auth/sso/example-univ/callback?code=not-this-route",
    ],
)
def test_every_other_request_line_is_left_as_it_is(path):
    record = _access_record(path, status=200)

    assert _CallbackQueryFilter().filter(record) is True

    assert record.args[2] == path


def test_a_record_of_another_shape_passes_through():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "started", None, None)

    assert _CallbackQueryFilter().filter(record) is True
    assert record.getMessage() == "started"


def test_the_filter_is_attached_once_with_the_access_log_filters():
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    try:
        attach_quiet_access_filter()
        attach_quiet_access_filter()

        attached = [f for f in access.filters if isinstance(f, _CallbackQueryFilter)]
        assert len(attached) == 1
    finally:
        access.filters[:] = before


@pytest.mark.parametrize(
    ("status", "logged"), [(200, False), (400, True), (401, True), (503, True)]
)
def test_an_openinfra_snapshot_is_logged_only_when_refused(monkeypatch, status, logged):
    # Reporters post every second from every GPU host.
    monkeypatch.setattr(logging.root, "level", logging.INFO)
    record = _access_record("/internal/openinfra/ingest", status=status)

    assert _QuietPathFilter().filter(record) is logged


def test_every_snapshot_is_logged_at_debug(monkeypatch):
    monkeypatch.setattr(logging.root, "level", logging.DEBUG)

    assert _QuietPathFilter().filter(_access_record("/internal/openinfra/ingest", 200)) is True
