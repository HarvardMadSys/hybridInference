"""Direct contracts for the helpers that carry hybrid routing metadata.

``HybridRouter`` builds its attempt history and commits a stream from two sets of
pure helpers: the ``_routing`` records/merges in
:mod:`routing.hybrid_attempt_history` and the SSE frame tests in
:mod:`routing.hybrid_stream_frames`. The router-level tests cover the outcome of
a fallback; these pin the metadata shape and the exact framing the serving layer
and the request log read.
"""

from __future__ import annotations

import json

import pytest

from routing.decisions import RoutingTarget
from routing.hybrid_attempt_history import (
    attach_attempt_history,
    attempt_history_chunk,
    attempt_record,
    merge_attempt_history,
    tag_backend,
    tag_preference,
)
from routing.hybrid_stream_frames import is_client_visible, tag_backend_chunk


def _decode(frame: str) -> dict:
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    return json.loads(frame[len("data: ") :].strip())


# ---------------------------------------------------------------------------
# attempt_record
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_attempt_record_uses_the_routers_own_endpoint_and_provider() -> None:
    """The failing router's ``_routing`` block outranks the planned endpoint."""
    exc = RuntimeError("upstream went away")
    exc._routing = {"provider": "zai", "endpoint_id": "combo:zai-api"}  # type: ignore[attr-defined]

    assert attempt_record("cloud", "combo:local-12003", exc) == {
        "endpoint_id": "combo:zai-api",
        "error_type": "RuntimeError",
        "error": "upstream went away",
        "provider": "zai",
        "backend": "cloud",
    }


@pytest.mark.unit
def test_attempt_record_falls_back_to_the_planned_endpoint() -> None:
    """An error with no routing block reports the endpoint the plan named."""
    record = attempt_record("local", "combo:local-12003", ConnectionError("down"))

    assert record == {
        "endpoint_id": "combo:local-12003",
        "error_type": "ConnectionError",
        "error": "down",
        "backend": "local",
    }
    assert "provider" not in record


@pytest.mark.unit
def test_attempt_record_names_the_backend_when_it_has_no_endpoint() -> None:
    """An empty stream failed before any endpoint ran; the record still resolves."""
    record = attempt_record("local", None, RuntimeError("stream produced no output"))

    assert record["endpoint_id"] == "local"
    assert record["backend"] == "local"


@pytest.mark.unit
@pytest.mark.parametrize("routing", [None, "not-a-dict", {"provider": ""}])
def test_attempt_record_ignores_unusable_routing_metadata(routing: object) -> None:
    exc = ValueError("bad")
    exc._routing = routing  # type: ignore[attr-defined]

    record = attempt_record("cloud", "combo:zai-api", exc)

    assert record["endpoint_id"] == "combo:zai-api"
    assert "provider" not in record


# ---------------------------------------------------------------------------
# merge_attempt_history / attach_attempt_history
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_merge_attempt_history_is_a_no_op_without_attempts() -> None:
    response: dict = {"_routing": {"endpoint_id": "combo:zai-api"}}

    merge_attempt_history(response, [])

    assert response == {"_routing": {"endpoint_id": "combo:zai-api"}}


@pytest.mark.unit
def test_merge_attempt_history_creates_metadata_and_marks_the_fallback() -> None:
    response: dict = {"choices": []}

    merge_attempt_history(response, [{"endpoint_id": "combo:local"}])

    assert response["_routing"] == {
        "fallback": True,
        "failed_attempts": [{"endpoint_id": "combo:local"}],
    }


@pytest.mark.unit
def test_merge_attempt_history_keeps_the_answering_routers_own_attempts() -> None:
    """Its attempts come first: the domain that answered saw a subset of these."""
    response: dict = {
        "_routing": {
            "endpoint_id": "combo:zai-api",
            "failed_attempts": [{"endpoint_id": "combo:zai-api-2"}],
        }
    }

    merge_attempt_history(response, [{"endpoint_id": "combo:local"}] * 2)

    assert response["_routing"]["failed_attempts"] == [
        {"endpoint_id": "combo:zai-api-2"},
        {"endpoint_id": "combo:local"},
        {"endpoint_id": "combo:local"},
    ]
    assert response["_routing"]["fallback"] is True


@pytest.mark.unit
def test_merge_attempt_history_does_not_overwrite_an_existing_fallback_flag() -> None:
    response: dict = {"_routing": {"fallback": "reported-by-backend"}}

    merge_attempt_history(response, [{"endpoint_id": "combo:local"}])

    assert response["_routing"]["fallback"] == "reported-by-backend"


@pytest.mark.unit
def test_merge_attempt_history_replaces_an_unusable_attempt_list() -> None:
    response: dict = {"_routing": {"failed_attempts": "garbage"}}

    merge_attempt_history(response, [{"endpoint_id": "combo:local"}])

    assert response["_routing"]["failed_attempts"] == [{"endpoint_id": "combo:local"}]


@pytest.mark.unit
def test_attach_attempt_history_adds_the_merged_records_to_the_error() -> None:
    exc = RuntimeError("every backend failed")
    exc._routing = {"endpoint_id": "combo:local"}  # type: ignore[attr-defined]

    attach_attempt_history(exc, [{"endpoint_id": "combo:local"}])

    assert exc._routing == {  # type: ignore[attr-defined]
        "endpoint_id": "combo:local",
        "failed_attempts": [{"endpoint_id": "combo:local"}],
    }


@pytest.mark.unit
def test_attach_attempt_history_deduplicates_by_endpoint() -> None:
    """The raised error already names its own attempt; the caller sees each once."""
    exc = RuntimeError("every backend failed")
    exc._routing = {  # type: ignore[attr-defined]
        "failed_attempts": [
            {"endpoint_id": "combo:local", "error_type": "ConnectionError"},
            {"endpoint_id": "combo:zai-api", "error_type": "TimeoutError"},
        ]
    }

    attach_attempt_history(
        exc,
        [
            {"endpoint_id": "combo:local", "error_type": "ConnectionError"},
            {"endpoint_id": "combo:local-2", "error_type": "ConnectionError"},
        ],
    )

    assert [attempt["endpoint_id"] for attempt in exc._routing["failed_attempts"]] == [  # type: ignore[attr-defined]
        "combo:local",
        "combo:zai-api",
        "combo:local-2",
    ]


@pytest.mark.unit
def test_attach_attempt_history_only_annotates_an_error_that_carries_routing() -> None:
    """A surfaced error without ``_routing`` is left exactly as it was.

    The router annotates the error it is about to raise, and only the errors a
    dispatch produced carry that block. Inventing one here would report metadata
    for an exception no backend ever raised, so the record is skipped instead.
    """
    exc = RuntimeError("not from a router")

    attach_attempt_history(exc, [{"endpoint_id": "combo:local"}])

    assert not hasattr(exc, "_routing")


@pytest.mark.unit
def test_attach_attempt_history_is_a_no_op_without_attempts() -> None:
    exc = RuntimeError("no plan")
    exc._routing = {"endpoint_id": "combo:local"}  # type: ignore[attr-defined]

    attach_attempt_history(exc, [])

    assert exc._routing == {"endpoint_id": "combo:local"}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# tag_backend / tag_preference
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tag_backend_records_the_domain_that_answered() -> None:
    response: dict = {"_routing": {"endpoint_id": "combo:zai-api"}}

    tag_backend(response, "cloud")

    assert response["_routing"]["backend"] == "cloud"


@pytest.mark.unit
def test_tag_backend_keeps_a_domain_the_backend_already_reported() -> None:
    response: dict = {"_routing": {"backend": "reported-by-backend"}}

    tag_backend(response, "cloud")

    assert response["_routing"]["backend"] == "reported-by-backend"


@pytest.mark.unit
def test_tag_backend_ignores_a_response_without_metadata() -> None:
    response: dict = {"choices": []}

    tag_backend(response, "cloud")

    assert response == {"choices": []}


@pytest.mark.unit
def test_tag_preference_reports_an_honored_target() -> None:
    response: dict = {"_routing": {"endpoint_id": "combo:zai-api"}}
    target = RoutingTarget(endpoint_id="combo:zai-api")

    tag_preference(response, target, "combo:zai-api")

    assert response["_routing"] == {
        "endpoint_id": "combo:zai-api",
        "preferred_endpoint_id": "combo:zai-api",
        "preferred_target": "endpoint:combo:zai-api",
        "preference_in_range": True,
    }


@pytest.mark.unit
def test_tag_preference_reports_an_unreachable_target() -> None:
    """The metadata must say the policy's target was not in range, not omit it."""
    response: dict = {"_routing": {}}
    target = RoutingTarget(provider="zai")

    tag_preference(response, target, None)

    assert response["_routing"] == {
        "preferred_target": "provider:zai",
        "preference_in_range": False,
    }
    assert "preferred_endpoint_id" not in response["_routing"]


@pytest.mark.unit
def test_tag_preference_keeps_metadata_the_backend_already_reported() -> None:
    response: dict = {
        "_routing": {
            "preferred_endpoint_id": "reported-by-backend",
            "preference_in_range": "reported-by-backend",
        }
    }

    tag_preference(response, RoutingTarget(endpoint_id="combo:zai-api"), "combo:zai-api")

    assert response["_routing"]["preferred_endpoint_id"] == "reported-by-backend"
    assert response["_routing"]["preference_in_range"] == "reported-by-backend"
    assert response["_routing"]["preferred_target"] == "endpoint:combo:zai-api"


@pytest.mark.unit
def test_tag_preference_without_a_target_still_reports_the_resolved_endpoint() -> None:
    response: dict = {"_routing": {}}

    tag_preference(response, None, "combo:local-12003")

    assert response["_routing"] == {"preferred_endpoint_id": "combo:local-12003"}


# ---------------------------------------------------------------------------
# attempt_history_chunk
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_attempt_history_chunk_keeps_the_exact_sse_framing() -> None:
    attempts = [{"endpoint_id": "combo:local", "error_type": "ConnectionError"}]

    frame = attempt_history_chunk(attempts, target=None, resolved_endpoint=None)

    payload = _decode(frame)
    assert payload == {
        "choices": [],
        "_routing": {"fallback": True, "failed_attempts": attempts},
    }
    # One JSON object per frame: the serving layer splits on the blank line.
    assert frame.count("\n\n") == 1
    assert frame.count("\n") == 2


@pytest.mark.unit
def test_attempt_history_chunk_explains_the_preference() -> None:
    attempts = [{"endpoint_id": "combo:local"}]
    target = RoutingTarget(endpoint_id="combo:zai-api")

    payload = _decode(
        attempt_history_chunk(attempts, target=target, resolved_endpoint="combo:zai-api")
    )

    assert payload["_routing"] == {
        "fallback": True,
        "failed_attempts": attempts,
        "preferred_endpoint_id": "combo:zai-api",
        "preferred_target": "endpoint:combo:zai-api",
        "preference_in_range": True,
    }


@pytest.mark.unit
def test_attempt_history_chunk_reports_a_target_out_of_range() -> None:
    target = RoutingTarget(provider="zai")

    payload = _decode(attempt_history_chunk([], target=target, resolved_endpoint=None))

    assert payload["_routing"] == {
        "fallback": True,
        "failed_attempts": [],
        "preferred_target": "provider:zai",
        "preference_in_range": False,
    }
    assert "preferred_endpoint_id" not in payload["_routing"]


# ---------------------------------------------------------------------------
# is_client_visible
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_synthetic_routing_frame_is_not_client_visible() -> None:
    """Counting it as output would refuse a fallback the failure still allows."""
    assert not is_client_visible('data: {"choices": [], "_routing": {"backend": "local"}}\n\n')


@pytest.mark.unit
def test_the_attempt_history_frame_is_not_client_visible() -> None:
    frame = attempt_history_chunk(
        [{"endpoint_id": "combo:local"}], target=None, resolved_endpoint=None
    )

    assert not is_client_visible(frame)


@pytest.mark.unit
def test_a_content_chunk_is_client_visible() -> None:
    assert is_client_visible('data: {"choices": [{"delta": {"content": "hi"}}]}\n\n')


@pytest.mark.unit
def test_a_usage_only_frame_is_client_visible() -> None:
    """A stream can end on usage alone; that is still output the client received."""
    assert is_client_visible('data: {"choices": [], "usage": {"total_tokens": 3}}\n\n')


@pytest.mark.unit
@pytest.mark.parametrize(
    "chunk",
    [
        "data: [DONE]\n\n",
        "data: not-json\n\n",
        "data: [1, 2, 3]\n\n",
        'data: {"choices": []}\n\n',
        "keep-alive",
        b'data: {"choices": []}\n\n',
        {"choices": []},
    ],
)
def test_anything_unrecognised_counts_as_committed(chunk: object) -> None:
    """Wrong in this direction only forgoes a fallback; the reverse splices answers."""
    assert is_client_visible(chunk) is True


# ---------------------------------------------------------------------------
# tag_backend_chunk
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tag_backend_chunk_tags_the_routing_frame() -> None:
    chunk = 'data: {"choices": [], "_routing": {"endpoint_id": "combo:local"}}\n\n'

    tagged = tag_backend_chunk(chunk, "local")

    assert tagged == (
        'data: {"choices": [], "_routing": {"endpoint_id": "combo:local", "backend": "local"}}\n\n'
    )


@pytest.mark.unit
def test_tag_backend_chunk_keeps_a_backend_the_frame_already_names() -> None:
    chunk = 'data: {"choices": [], "_routing": {"backend": "reported-by-backend"}}\n\n'

    assert tag_backend_chunk(chunk, "local") == chunk


@pytest.mark.unit
@pytest.mark.parametrize(
    "chunk",
    [
        'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n',
        "data: [DONE]\n\n",
        "data: not-json\n\n",
        "data: [1, 2, 3]\n\n",
        "keep-alive",
        "",
        b'data: {"_routing": {}}\n\n',
        {"_routing": {}},
    ],
)
def test_tag_backend_chunk_forwards_untouched_frames_byte_for_byte(chunk: object) -> None:
    """Anything that is not a routing frame must not be re-serialized."""
    tagged = tag_backend_chunk(chunk, "local")

    assert tagged is chunk
