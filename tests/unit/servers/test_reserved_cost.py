"""What a request reserves against its caller's limits while it runs (M19).

The prompt (its body's size, within the model's context window) and the output
it allows (its ``max_tokens``, within the model's output limit), at the dearest
of the model's routes, bounded by ``quota.estimate_request_cost``.
"""

from __future__ import annotations

import json
import math
import types
from typing import Any

import pytest

from serving import quota
from serving.servers.concurrency import _reserved_cost


def _config(
    prompt: str,
    completion: str,
    *,
    context: int = 100_000,
    max_output: int = 8_000,
    model_type: str = "chat",
) -> Any:
    return types.SimpleNamespace(
        id="m",
        pricing={"prompt": prompt, "completion": completion},
        context_length=context,
        max_output_length=max_output,
        model_type=model_type,
    )


def _router(*configs: Any) -> Any:
    adapters = [(types.SimpleNamespace(config=config), 1.0) for config in configs]
    return types.SimpleNamespace(routes={"m": types.SimpleNamespace(adapters=adapters)})


def _request(payload: dict[str, Any], *, pad_to: int = 0) -> Any:
    body = json.dumps(payload).encode()
    body += b" " * max(0, pad_to - len(body))
    return types.SimpleNamespace(_body=body, _json=payload)


def _cost(prompt_tokens: int, output_tokens: int, prompt: float, completion: float) -> float:
    return (prompt_tokens * prompt + output_tokens * completion) / 1_000_000


def test_the_prompt_and_the_output_asked_for_at_the_dearest_route():
    request = _request({"model": "m", "max_tokens": 1_000}, pad_to=40_000)
    router = _router(_config("1", "2"), _config("3", "4"))

    reserved = _reserved_cost(request, "m", router=router, embedding_adapters={})

    assert reserved == pytest.approx(_cost(10_000, 1_000, 3, 4))


def test_without_max_tokens_the_model_output_limit_is_reserved():
    request = _request({"model": "m"}, pad_to=4_000)

    reserved = _reserved_cost(
        request, "m", router=_router(_config("1", "2")), embedding_adapters={}
    )

    assert reserved == pytest.approx(_cost(1_000, 8_000, 1, 2))


def test_the_estimate_stays_within_what_the_model_can_take():
    request = _request({"model": "m", "max_tokens": 10**9}, pad_to=8_000_000)

    reserved = _reserved_cost(
        request, "m", router=_router(_config("1", "2", context=50_000)), embedding_adapters={}
    )

    assert reserved == pytest.approx(_cost(50_000, 8_000, 1, 2))


def test_an_embedding_reserves_its_input_only():
    embedding = types.SimpleNamespace(config=_config("4", "9", model_type="embedding"))
    request = _request({"model": "e", "input": "x"}, pad_to=400_000)

    reserved = _reserved_cost(
        request, "e", router=types.SimpleNamespace(routes={}), embedding_adapters={"e": embedding}
    )

    assert reserved == pytest.approx(_cost(100_000, 0, 4, 0))


@pytest.mark.parametrize(
    ("model", "incoming"),
    [
        ("unknown", _request({"model": "unknown"}, pad_to=40_000)),
        ("m", types.SimpleNamespace()),  # the body was not read
        (None, _request({}, pad_to=40_000)),
    ],
    ids=["unknown-model", "body-not-read", "no-model"],
)
def test_without_a_price_or_a_body_the_flat_minimum_is_reserved(model, incoming):
    reserved = _reserved_cost(
        incoming, model, router=_router(_config("9", "9")), embedding_adapters={}
    )

    assert reserved == quota.ESTIMATED_REQUEST_COST_USD


def test_the_estimate_is_bounded_both_ways():
    small = quota.estimate_request_cost(
        prompt_tokens=10, output_tokens=10, prompt_price=0.1, completion_price=0.1
    )
    large = quota.estimate_request_cost(
        prompt_tokens=10**7, output_tokens=10**6, prompt_price=75, completion_price=150
    )
    nan = quota.estimate_request_cost(
        prompt_tokens=10, output_tokens=10, prompt_price=math.nan, completion_price=1
    )

    assert small == quota.ESTIMATED_REQUEST_COST_USD
    assert large == nan == quota.MAX_RESERVED_REQUEST_COST_USD
