"""A stream the client closes early is billed for what the upstream already did.

``StreamSession._finalize_cancelled`` used to log the 499/504 row and charge
nothing, so a client could take the useful part of an answer, disconnect before
``[DONE]``, and never move its daily cost quota. It now bills the way the
success path and ``/v1/messages`` do: the usage the upstream reported, or else an
estimate of the prompt plus the output produced so far, marked
``usage_estimated``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_cost import CostTracker, PricingLookup
from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.completions_stream import StreamSession
from serving.servers.routers.routing_info import RoutingInfo
from serving.storage.utils import calculate_cost
from serving.utils.tokens import (
    INLINE_ESTIMATE_MAX_CHARS,
    estimate_prompt_tokens_bounded,
    estimate_texts_tokens_bounded,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PRICING = {"prompt": "2", "completion": "8"}
MESSAGES = [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "Explain the halting problem in one paragraph."},
]
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


def _routing_chunk() -> str:
    routing = {
        "provider": "openai",
        "base_url": "https://api.openai.example/v1",
        "endpoint_id": "m:openai-api",
        "pricing": PRICING,
    }
    return f"data: {json.dumps({'choices': [], '_routing': routing})}\n\n"


def _delta_chunk(delta: dict[str, Any]) -> str:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _usage_chunk(usage: dict[str, int]) -> str:
    payload = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1}
    payload.update({"model": "m", "choices": [], "usage": usage})
    return f"data: {json.dumps(payload)}\n\n"


class _Harness:
    """A session over a real CostTracker, whose store records every increment."""

    def __init__(self, *, timed_out: bool = False, probe: bool = False) -> None:
        self.op_store = MagicMock()
        self.op_store.increment_user_cost = AsyncMock()
        self.tracker = CostTracker(
            op_store=self.op_store, pricing=PricingLookup(router=MagicMock(routes={}))
        )
        self.logger = MagicMock(spec=CompletionsLogger)
        self.session = StreamSession(
            routing=RoutingInfo(request_id="rid", model="m"),
            model="m",
            messages=MESSAGES,
            params={"stream": True, "tools": TOOLS},
            request_id="rid",
            start_time=time.time(),
            request_headers={},
            metadata={"api_key_id": 41},
            user_id="user-1",
            is_synthetic_probe=probe,
            suppress_synthetic_logging=probe,
            log_store=MagicMock(),
            active_router=MagicMock(),
            cost_tracker=self.tracker,
            completions_logger=self.logger,
            pricing_lookup=self.tracker._pricing,
            get_adapter_config_for_provider=lambda _p, _b: None,
            timeout_fired_probe=lambda: timed_out,
        )

    async def run_then_cancel(self, chunks: list[str]) -> None:
        """Feed *chunks*, then cancel the response task while the stream waits for more."""
        arrived = asyncio.Event()

        async def _upstream() -> AsyncIterator[str]:
            for chunk in chunks:
                yield chunk
            arrived.set()
            await asyncio.sleep(30)

        async def _respond() -> None:
            async for _frame in self.session.stream(_upstream()):
                pass

        response_task = asyncio.ensure_future(_respond())
        await arrived.wait()
        # Let the session take in everything the reader queued.
        for _ in range(20):
            await asyncio.sleep(0)
        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_task

    async def run_then_close(self, chunks: list[str], last_text: str) -> None:
        """Feed *chunks*, then close the stream once *last_text* reached the client."""

        async def _upstream() -> AsyncIterator[str]:
            for chunk in chunks:
                yield chunk
            await asyncio.sleep(30)

        gen = self.session.stream(_upstream())
        while last_text not in await gen.__anext__():
            pass
        await gen.aclose()

    async def charged(self) -> list[tuple[str, float, dict[str, Any]]]:
        for task in list(self.tracker._background_tasks):
            await task
        return [
            (c.args[0], c.args[1], c.kwargs)
            for c in self.op_store.increment_user_cost.call_args_list
        ]

    def row(self) -> dict[str, Any]:
        self.logger.schedule_log.assert_called_once()
        return self.logger.schedule_log.call_args.args[1]


@pytest.mark.asyncio
async def test_disconnect_after_reported_usage_bills_the_reported_usage():
    harness = _Harness()
    await harness.run_then_cancel(
        [
            _routing_chunk(),
            _delta_chunk({"content": "The halting problem"}),
            _usage_chunk({"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230}),
        ]
    )

    expected = calculate_cost({"prompt_tokens": 1200, "completion_tokens": 30}, PRICING)
    charged = await harness.charged()
    assert [(user, cost) for user, cost, _ in charged] == [("user-1", pytest.approx(expected))]
    assert charged[0][2]["api_key_id"] == 41
    row = harness.row()
    assert row["status_code"] == 499
    assert "usage_estimated" not in row["metadata"]
    assert row["usage"]["prompt_tokens"] == 1200
    assert row["usage"]["completion_tokens"] == 30
    # The row's own cost is computed from the same usage and price: it is the charge.
    assert row["pricing"] == PRICING
    assert calculate_cost(row["usage"], row["pricing"]) == pytest.approx(expected)
    assert row["upstream_cost_usd"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_disconnect_without_usage_bills_the_bounded_estimate():
    harness = _Harness()
    await harness.run_then_cancel(
        [
            _routing_chunk(),
            _delta_chunk({"reasoning_content": "Turing showed"}),
            _delta_chunk({"content": "No program can decide, for every program"}),
            _delta_chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": "{"}}
                    ]
                }
            ),
        ]
    )

    prompt = estimate_prompt_tokens_bounded(MESSAGES, extra_texts=(json.dumps(TOOLS),))
    output = estimate_texts_tokens_bounded(
        ["No program can decide, for every program", "Turing showed", "lookup", "{"],
        max_chars=INLINE_ESTIMATE_MAX_CHARS,
    )
    assert prompt > 0 and output > 0
    expected = calculate_cost({"prompt_tokens": prompt, "completion_tokens": output}, PRICING)
    charged = await harness.charged()
    assert [(user, cost) for user, cost, _ in charged] == [("user-1", pytest.approx(expected))]

    row = harness.row()
    assert row["status_code"] == 499
    assert row["metadata"]["usage_estimated"] is True
    assert row["metadata"]["terminal_state"] == "client_disconnect"
    assert row["usage"] == {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
    }
    assert calculate_cost(row["usage"], row["pricing"]) == pytest.approx(expected)
    # Still no routing observation for client behavior.
    harness.logger.record_routing_observation.assert_not_called()


@pytest.mark.asyncio
async def test_generator_close_is_billed_like_a_cancellation():
    harness = _Harness()
    await harness.run_then_close(
        [_routing_chunk(), _delta_chunk({"content": "partial answer"})], "partial answer"
    )

    assert len(await harness.charged()) == 1
    assert harness.row()["metadata"]["usage_estimated"] is True


@pytest.mark.asyncio
async def test_gateway_timeout_is_billed_like_a_disconnect():
    harness = _Harness(timed_out=True)
    await harness.run_then_cancel([_routing_chunk(), _delta_chunk({"content": "slow"})])

    assert len(await harness.charged()) == 1
    row = harness.row()
    assert row["status_code"] == 504
    assert row["metadata"]["terminal_state"] == "request_timeout"
    assert row["metadata"]["usage_estimated"] is True
    assert row["pricing"] == PRICING


@pytest.mark.asyncio
async def test_abort_before_any_attempt_began_charges_nothing():
    """No routing chunk: no endpoint was chosen, so nothing was asked of an upstream."""
    harness = _Harness()
    await harness.run_then_cancel([])

    assert await harness.charged() == []
    row = harness.row()
    assert row["status_code"] == 499
    assert row["pricing"] is None
    assert "usage_estimated" not in row["metadata"]


@pytest.mark.asyncio
async def test_probe_disconnect_is_billed_without_a_row():
    """The probe marker suppresses the log row, never the charge."""
    harness = _Harness(probe=True)
    await harness.run_then_cancel([_routing_chunk(), _delta_chunk({"content": "x"})])

    assert len(await harness.charged()) == 1
    harness.logger.schedule_log.assert_not_called()


@pytest.mark.asyncio
async def test_mid_stream_upstream_failure_stays_unbilled():
    """An upstream failure is not the client's choice: unbilled, as on /v1/messages."""
    harness = _Harness()

    async def _failing() -> AsyncIterator[str]:
        yield _routing_chunk()
        yield _delta_chunk({"content": "partial"})
        raise RuntimeError("upstream reset")

    frames = [frame async for frame in harness.session.stream(_failing())]

    assert any('"error"' in frame for frame in frames)
    assert await harness.charged() == []
    assert harness.row()["status_code"] == 500


@pytest.mark.asyncio
async def test_a_billing_failure_still_writes_the_row():
    harness = _Harness()
    harness.tracker.charge = MagicMock(side_effect=RuntimeError("pricing broke"))
    await harness.run_then_cancel([_routing_chunk(), _delta_chunk({"content": "x"})])

    row = harness.row()
    assert row["status_code"] == 499
    assert row["pricing"] is None
