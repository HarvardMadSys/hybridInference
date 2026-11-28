"""The ingest body's parsing: lenient about values, strict about shape."""

from __future__ import annotations

import pytest

from serving.openinfra import wire
from serving.openinfra.wire import IngestError, parse_snapshot


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        (3, 3.0),
        (2.5, 2.5),
        ("12", 12.0),
        (" 1e3 ", 1000.0),
        ("", None),
        ("abc", None),
        (True, None),
        (None, None),
        (float("nan"), None),
        (float("inf"), None),
        ("Infinity", None),
        ([1], None),
    ],
)
def test_number(raw, parsed):
    assert wire.number(raw) == parsed


def test_a_full_snapshot():
    snapshot = parse_snapshot(
        {
            "node": {
                "id": " node-a ",
                "hostname": "",
                "region": "lab",
                "metadata": None,
                "cpu": {"cpu_util_pct": "7.5", "load1": "high"},
                "unknown": 1,
            },
            "gpus": [{"index": "1", "name": "Test GPU", "processes": [{"pid": 1}], "power_w": 99}],
            "sglang": {
                "server": {"id": "chat-a", "engine": "vllm", "gpu_indexes": "0,1"},
                "metrics": {"num_running_reqs": "3", "model_name": "org/model", "extra": 1},
                "requests": [{"ts": 1_700_000_000, "input_len": 2.5, "output_len": 3.49}] * 25,
            },
        }
    )
    node = snapshot.node
    assert node is not None
    assert (node.id, node.hostname, node.region, node.cluster) == ("node-a", None, "lab", None)
    assert node.metadata is None
    assert node.cpu is not None
    assert node.cpu["cpu_util_pct"] == 7.5
    assert node.cpu["load1"] is None
    gpu = snapshot.gpus[0]
    assert (gpu.index, gpu.name, gpu.processes) == (1, "Test GPU", [{"pid": 1}])
    assert gpu.numbers["power_w"] == 99.0
    assert gpu.numbers["utilization_pct"] is None
    engine = snapshot.engines[0]
    assert engine.server.engine == "vllm"
    assert engine.metrics is not None
    assert engine.metrics["num_running_reqs"] == 3.0
    assert engine.metrics["model_name"] == "org/model"
    assert "extra" not in engine.metrics
    assert len(engine.requests) == wire.MAX_REQUESTS
    event = engine.requests[0]
    # Seconds become milliseconds; token counts round half up.
    assert event["ts"] == 1_700_000_000_000
    assert (event["input_len"], event["output_len"]) == (3, 3)


def test_absent_free_form_fields_are_told_apart_from_null():
    snapshot = parse_snapshot({"node": {"id": "a"}, "gpus": [{"index": 0}]})
    assert snapshot.node is not None
    assert snapshot.node.metadata is ...
    assert snapshot.node.cpu is None
    assert snapshot.gpus[0].processes is ...


def test_an_empty_metrics_object_counts_as_sent():
    # As in the Worker's JavaScript, where {} is truthy.
    snapshot = parse_snapshot({"sglang": [{"server": {"id": "e"}, "metrics": {}}]})
    assert snapshot.engines[0].metrics is not None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (None, "body must be an object"),
        ([], "body must be an object"),
        ({}, "body must include node, gpus, or sglang"),
        ({"node": None, "gpus": [], "sglang": []}, "body must include node, gpus, or sglang"),
        ({"node": "a"}, "node must be an object"),
        ({"node": {}}, "node.id is required"),
        ({"node": {"id": "  "}}, "node.id is required"),
        ({"node": {"id": "a", "cpu": [1]}}, "node.cpu must be an object"),
        ({"gpus": [{"index": 0}]}, "node is required when ingesting gpus"),
        ({"node": {"id": "a"}, "gpus": {"index": 0}}, "gpus must be an array"),
        ({"node": {"id": "a"}, "gpus": [{}]}, "gpu.index is required"),
        ({"node": {"id": "a"}, "gpus": [{"index": -1}]}, "gpu.index must be"),
        ({"node": {"id": "a"}, "gpus": [{"index": 0.5}]}, "gpu.index must be"),
        ({"node": {"id": "a"}, "gpus": [{"index": 0}] * 65}, "gpus exceeds 64"),
        ({"sglang": [{"server": {"id": "e"}}] * 17}, "sglang exceeds 16"),
        ({"sglang": [{}]}, "sglang.server must be an object"),
        ({"sglang": [{"server": {}}]}, "sglang.server.id is required"),
        ({"sglang": [{"server": {"id": "e"}, "metrics": [1]}]}, "sglang.metrics must be"),
        ({"sglang": [{"server": {"id": "e"}, "requests": [1]}]}, "sglang.requests[] must be"),
    ],
)
def test_a_body_of_the_wrong_shape_is_refused(body, message):
    with pytest.raises(IngestError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_snapshot(body)


def test_requests_that_are_not_a_list_are_ignored():
    snapshot = parse_snapshot({"sglang": {"server": {"id": "e"}, "requests": {"ts": 1}}})
    assert snapshot.engines[0].requests == ()
