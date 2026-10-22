"""Request-scope tests for online traffic classification metadata."""

from __future__ import annotations

from serving.utils import context as req_ctx


def test_traffic_metadata_is_cleared_between_request_scopes():
    try:
        req_ctx.set(
            {
                req_ctx.TRAFFIC_CLASSIFICATION: "likely_human",
                req_ctx.TRAFFIC_AUTOMATION_SCORE: 0.1,
                req_ctx.TRAFFIC_CONFIDENCE: 0.8,
                req_ctx.TRAFFIC_REASONS: ["client_tool"],
            }
        )

        req_ctx.reset_request_scope(request_id="next-request")
        current = req_ctx.get()

        assert current == {"request_id": "next-request"}
    finally:
        req_ctx.set({})
