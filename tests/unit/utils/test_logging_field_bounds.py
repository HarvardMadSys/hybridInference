"""Every structured field a log line writes is bounded, whoever logs it.

A request's path and headers are the client's to choose, up to the server's
16 KiB header limit, and reach lines written for every such request -- an
invalid key's ``auth_failure``, a refused body -- not only the request log.
"""

from __future__ import annotations

import json
import logging

from serving.utils.logging import MAX_LOGGED_FIELD_CHARS, JsonFormatter, PlainFormatter


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "serving.servers.auth", logging.WARNING, __file__, 1, "auth_failure", None, None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_a_long_field_is_cut_in_a_plain_line() -> None:
    path = "/v1/" + "é" * 16_000
    line = PlainFormatter(fmt="%(levelname)s %(message)s").format(_record(path=path, reason="r"))
    # Each kept character is escaped as six (é), so the cut is what bounds it.
    assert len(line) < 7 * MAX_LOGGED_FIELD_CHARS
    assert 'reason="r"' in line


def test_a_long_field_is_cut_in_a_json_line() -> None:
    path = "/v1/" + "a" * 16_000
    payload = json.loads(JsonFormatter().format(_record(path=path)))
    assert payload["path"] == path[:MAX_LOGGED_FIELD_CHARS] + "…"


def test_short_fields_are_written_as_they_are() -> None:
    payload = json.loads(JsonFormatter().format(_record(path="/v1/x", status_code=404)))
    assert (payload["path"], payload["status_code"]) == ("/v1/x", 404)
