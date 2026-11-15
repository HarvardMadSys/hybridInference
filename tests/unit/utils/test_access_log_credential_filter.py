"""uvicorn's access log does not keep an export download link usable.

The link is a URL whose ``token`` downloads an export with no other
authentication for a minute; uvicorn logs the URL whole.
"""

from __future__ import annotations

import logging

from serving.utils.logging import _CredentialQueryFilter, attach_quiet_access_filter

LINK = "/admin/export/requests/download?token=eyJhbGciOi.payload.signature"


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:5000", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_a_download_link_token_is_redacted_and_the_line_kept():
    record = _access_record(LINK)

    assert _CredentialQueryFilter().filter(record) is True
    message = record.getMessage()
    assert "eyJhbGciOi" not in message
    assert '"GET /admin/export/requests/download?token=<redacted> HTTP/1.1" 200' in message


def test_other_parameters_of_the_link_survive():
    record = _access_record("/admin/export/requests/download?a=1&token=secret&b=2")

    _CredentialQueryFilter().filter(record)

    assert "?a=1&token=<redacted>&b=2" in record.getMessage()


def test_a_token_on_any_other_path_is_left_as_logged():
    record = _access_record("/auth/verify-email?token=abc")

    _CredentialQueryFilter().filter(record)

    assert "token=abc" in record.getMessage()


def test_the_filter_is_attached_once_to_uvicorn_access():
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    try:
        attach_quiet_access_filter()
        attach_quiet_access_filter()
        attached = [f for f in access.filters if isinstance(f, _CredentialQueryFilter)]
        assert len(attached) == 1
    finally:
        access.filters[:] = before
