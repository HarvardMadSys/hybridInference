"""Protect replay transformation and exclusion of future trace labels."""

import json

import pytest

from benchmark.nimbus.prepare_agentperf import prepare
from benchmark.nimbus.workload import load_workload


def test_open_loop_arrivals_keep_payloads_without_future_labels(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = [
        {
            "messages": [{"role": "user", "content": f"question {i}"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "simulated_tool_delay_ms_after": 250,
            "recorded_completion_tokens": 999,
            "target_output_tokens": 888,
        }
        for i in range(2)
    ]
    (source / "task.jsonl").write_text("\n".join(json.dumps(row) for row in original))
    output = tmp_path / "out.jsonl"
    manifest = prepare(
        source,
        output,
        rounds=2,
        copies=2,
        max_tokens=64,
        tasks=["task"],
        arrival_interval_s=0.5,
        open_loop=True,
    )
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["arrival_s"] for row in rows] == [0, 0.5, 1, 1.5]
    assert len({row["session_id"] for row in rows}) == 4
    assert manifest["open_loop"] is True
    for row in rows:
        assert row["messages"] == original[row["source_round_index"]]["messages"]
        assert row["tools"] == original[row["source_round_index"]]["tools"]
        assert row["max_tokens"] == 64
        assert "target_output_tokens" not in row
        assert "recorded_completion_tokens" not in row
    assert len(load_workload(output)) == 4
    with pytest.raises(FileExistsError):
        prepare(source, output, rounds=1, copies=1, max_tokens=64)


@pytest.mark.parametrize("interval", [-1, float("inf"), float("nan")])
def test_invalid_arrival_interval_is_rejected(tmp_path, interval):
    with pytest.raises(ValueError, match="arrival_interval"):
        prepare(
            tmp_path,
            tmp_path / "out",
            rounds=1,
            copies=1,
            max_tokens=1,
            arrival_interval_s=interval,
        )


def test_unknown_task_fails_before_writing(tmp_path):
    with pytest.raises(ValueError, match="unknown tasks"):
        prepare(tmp_path, tmp_path / "out", rounds=1, copies=1, max_tokens=1, tasks=["typo"])
    assert not (tmp_path / "out").exists()
