"""The shared execution context (``serving.surfaces.context``, issue #1690).

The headers an execution function reads must answer exactly as the request's
own Starlette ``Headers`` did -- case-insensitively, first value of a repeated
header -- or the session, the admin route pin, the probe marker and the
Messages beta header silently stop being read. The admission wrappers only
ever move the interactive-load count; they never add or drop one.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from serving.batch.attempts import batch_log_attribution
from serving.batch.interactive_load import InteractiveLoad
from serving.observability import rejection_log
from serving.servers.surface_http import client_origin
from serving.surfaces.context import (
    ORIGIN_HEADERS,
    UNTRACKED,
    BatchAttribution,
    ClientOrigin,
    InteractiveAdmission,
    admission_from,
    origin_headers,
    read_only,
)
from serving.utils.request_ip import ClientIpInfo
from serving.utils.session_identity import session_identity
from serving.utils.synthetic_probe import is_trusted_probe, is_trusted_probe_headers

_RAW = [
    (b"X-Session-ID", b"raw-mixed-case"),
    (b"x-session-id", b"s-1"),
    (b"x-session-id", b"s-2"),
    (b"anthropic-beta", b"b-1"),
    (b"anthropic-beta", b"b-2"),
    (b"x-route-pin", b"pin"),
    (b"x-probe", b"Synthetic"),
    (b"x-reasoning-passthrough", b"false"),
    (b"session-id", b"agent"),
    (b"authorization", b"Bearer secret"),
    (b"x-api-key", b"secret"),
    (b"cookie", b"c=1"),
]

_LOOKUPS = [
    "X-Session-ID",
    "x-session-id",
    "X-SESSION-ID",
    "Anthropic-Beta",
    "ANTHROPIC-BETA",
    "X-Route-Pin",
    "x-route-pin",
    "X-Probe",
    "X-Reasoning-Passthrough",
    "Session-Id",
]


@pytest.mark.parametrize("name", _LOOKUPS)
def test_origin_headers_answer_every_lookup_as_the_request_headers_do(name):
    full, kept = Headers(raw=list(_RAW)), origin_headers(list(_RAW))

    assert kept.get(name) == full.get(name)
    assert kept.getlist(name) == full.getlist(name)
    assert (name in kept) == (name in full)


def test_origin_headers_keep_nothing_outside_the_list():
    kept = origin_headers(list(_RAW))

    for secret in ("authorization", "x-api-key", "cookie", "Authorization"):
        assert secret not in kept
    assert {name.lower() for name in kept} <= ORIGIN_HEADERS


def test_a_session_is_resolved_from_origin_headers_as_from_the_request():
    for raw in itertools.permutations(_RAW[:3] + _RAW[8:9]):
        full, kept = Headers(raw=list(raw)), origin_headers(list(raw))
        assert session_identity(kept, None) == session_identity(full, None)


def test_without_drops_every_spelling_of_the_header_and_nothing_else():
    raw = [
        (b"x-reasoning-passthrough", b"false"),
        (b"X-Reasoning-Passthrough", b"0"),
        (b"X-REASONING-PASSTHROUGH", b"no"),
        (b"x-session-id", b"s-1"),
    ]
    origin = _origin(raw)

    stripped = origin.without("X-Reasoning-Passthrough")

    assert stripped.headers.raw == [(b"x-session-id", b"s-1")]
    assert origin.headers.raw == raw  # the original is untouched
    assert (stripped.route_path, stripped.user_agent) == (origin.route_path, origin.user_agent)


def test_a_batch_origin_has_its_client_and_no_headers():
    ip = ClientIpInfo(
        client_ip="unknown",
        peer_ip="unknown",
        source="batch",
        trusted_proxy_headers=False,
        resolved=False,
    )

    origin = ClientOrigin.for_batch(path="/v1/messages", ip=ip, user_agent="ua/1")

    assert origin.ip is ip
    assert origin.headers.raw == []
    assert (origin.route_path, origin.user_agent, origin.referer) == ("/v1/messages", "ua/1", None)
    assert origin.authorization_header is False


def _request(raw: list[tuple[bytes, bytes]], path: str = "/v1/chat/completions") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw,
            "client": ("203.0.113.9", 4000),
            "server": ("gw", 80),
            "scheme": "http",
            "root_path": "",
            "state": {},
        }
    )


def _origin(raw: list[tuple[bytes, bytes]]) -> ClientOrigin:
    return client_origin(_request(raw))


def test_a_client_origin_reads_the_request_at_entry_and_its_ip_on_first_use(monkeypatch):
    calls: list[object] = []
    import serving.servers.surface_http as surface_http

    real = surface_http.get_client_ip_info
    monkeypatch.setattr(
        surface_http, "get_client_ip_info", lambda request: calls.append(request) or real(request)
    )
    request = _request(
        [(b"user-agent", b"ua"), (b"referer", b"https://r"), (b"x-session-id", b"s")]
    )

    origin = client_origin(request, authorization="Bearer k")

    assert calls == []
    assert (origin.user_agent, origin.referer, origin.route_path) == (
        "ua",
        "https://r",
        "/v1/chat/completions",
    )
    assert origin.authorization_header is True
    assert origin.ip.client_ip
    assert calls == [request]


@pytest.mark.parametrize("name", [b"x-probe", b"X-Probe", b"X-PROBE"])
@pytest.mark.parametrize("value", [b"synthetic", b"SYNTHETIC", b"other"])
def test_the_probe_marker_reads_the_same_from_origin_headers(name, value):
    trusted = {"role": "internal", "authenticated": True}
    raw = [(name.lower(), value)]  # as an ASGI server delivers it

    assert is_trusted_probe_headers(origin_headers(raw), trusted) == is_trusted_probe(
        _request(raw), trusted
    )


def test_interactive_admission_moves_the_count_and_never_adds_one():
    load = InteractiveLoad()
    with load.track("auto-x") as tracker:
        admission = admission_from(InteractiveAdmission(tracker))
        assert load.in_flight("auto-x") == 1
        admission.retarget("concrete")
        admission.retarget("concrete")
        assert (load.in_flight("auto-x"), load.in_flight("concrete")) == (0, 1)
    assert load.in_flight("concrete") == 0


@pytest.mark.parametrize("slot", [None, object(), "x"])
def test_anything_but_an_interactive_admission_is_untracked(slot):
    admission = admission_from(slot)

    assert admission is UNTRACKED
    admission.retarget("model")  # no tracker, nothing to move


def test_batch_attribution_logs_what_the_merged_caller_context_logged():
    for item_id, attempt in [(7, 2), (None, None), (0, 1)]:
        merged = {"user_id": "u", "batch_job_id": "b", "batch_item_id": item_id}
        merged["batch_attempt"] = attempt
        assert BatchAttribution("b", item_id, attempt).log_metadata() == batch_log_attribution(
            merged
        )


def test_the_caller_context_is_a_read_only_snapshot():
    user = {"user_id": "u", "role": "free"}
    view = read_only(user)
    user["role"] = "admin"

    assert view["role"] == "free"
    with pytest.raises(TypeError):
        view["role"] = "admin"  # type: ignore[index]


async def test_a_rejection_logged_by_source_is_the_row_logged_by_request():
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    settings = MagicMock()
    settings.get_bool = AsyncMock(return_value=True)
    user = {"user_id": "u", "role": "internal", "authenticated": True}
    request = _request([(b"x-probe", b"synthetic")], path="/v1/messages")
    common = {
        "status_code": 404,
        "error_code": "model_not_found",
        "reason": "Model 'm' not found",
        "user": user,
        "model_id": "m",
        "prompt": [{"role": "user", "content": "hi"}],
    }

    await rejection_log.log_rejection(
        request=request, log_store=log_store, runtime_settings=settings, **common
    )
    source = rejection_log.RejectionSource(
        route_path="/v1/messages",
        client_ip=lambda: rejection_log.get_client_ip(request),
        probe_marker=True,
        log_store=log_store,
        runtime_settings=settings,
    )
    await rejection_log.log_rejection_from(source=source, **common)

    by_request, by_source = (call.kwargs for call in log_store.log_request.await_args_list)
    by_request["metadata"].pop("ledger_started_at", None)
    by_source["metadata"].pop("ledger_started_at", None)
    assert by_source == by_request
    assert by_source["metadata"]["synthetic_probe"] is True
