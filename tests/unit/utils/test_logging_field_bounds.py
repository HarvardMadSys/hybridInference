"""Every structured field a log line writes is bounded, whoever logs it.

A request's path and headers are the client's to choose, up to the server's
16 KiB header limit, and reach lines written for every such request -- an
invalid key's ``auth_failure``, a refused body -- not only the request log.
"""

from __future__ import annotations

import json
import logging

from serving.utils.logging import (
    MAX_LOGGED_FIELD_CHARS,
    MAX_LOGGED_FIELD_ITEMS,
    JsonFormatter,
    PlainFormatter,
)


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


def _entries(count: int) -> list[dict[str, object]]:
    # What the spend ledger logs for each cost a write left unconfirmed.
    return [{"user_id": f"user-{n}", "day": "2026-11-16", "cost_usd": "0.25"} for n in range(count)]


def test_a_long_list_keeps_its_first_items_and_counts_the_rest() -> None:
    entries = _entries(MAX_LOGGED_FIELD_ITEMS + 1200)
    payload = json.loads(JsonFormatter().format(_record(entries=entries)))
    assert payload["entries"][:MAX_LOGGED_FIELD_ITEMS] == entries[:MAX_LOGGED_FIELD_ITEMS]
    assert payload["entries"][MAX_LOGGED_FIELD_ITEMS:] == ["… and 1200 more"]
    line = PlainFormatter(fmt="%(levelname)s %(message)s").format(_record(entries=entries))
    assert "and 1200 more" in line and "user-0" in line and f"user-{len(entries) - 1}" not in line
    assert len(line) < 40_000


def test_a_list_within_the_bound_is_written_whole() -> None:
    entries = _entries(MAX_LOGGED_FIELD_ITEMS)
    payload = json.loads(JsonFormatter().format(_record(entries=entries, reservations=("a", "b"))))
    assert payload["entries"] == entries
    assert payload["reservations"] == ["a", "b"]
