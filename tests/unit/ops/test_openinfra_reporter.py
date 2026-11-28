"""The OpenInfra reporter: where it posts, what it needs, and that the gateway takes what it sends."""

from __future__ import annotations

import argparse
import json
import sys

import pytest

from ops.openinfra import reporter
from serving.openinfra.store import SnapshotWrite
from serving.openinfra.wire import parse_snapshot


def test_the_snapshot_goes_to_the_gateways_ingest_route():
    assert reporter.ingest_url("https://gateway.example.org/") == (
        "https://gateway.example.org/internal/openinfra/ingest"
    )
    assert reporter.ingest_url("http://10.0.0.5:8080") == (
        "http://10.0.0.5:8080/internal/openinfra/ingest"
    )


def test_post_sends_json_with_the_bearer_token(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true, "gpus": 1, "sglang": 0}'

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["headers"] = dict(request.header_items())
        sent["body"] = json.loads(request.data)
        sent["timeout"] = timeout
        return Response()

    monkeypatch.setattr(reporter.urllib.request, "urlopen", fake_urlopen)
    answer = reporter.post("https://gateway.example.org", "s3cret", {"node": {"id": "a"}})
    assert answer == {"ok": True, "gpus": 1, "sglang": 0}
    assert sent["url"] == "https://gateway.example.org/internal/openinfra/ingest"
    assert sent["headers"]["Authorization"] == "Bearer s3cret"
    assert sent["headers"]["Content-type"] == "application/json"
    assert sent["body"] == {"node": {"id": "a"}}


def _parse(monkeypatch, *argv: str, **env: str) -> argparse.Namespace:
    for name in ("OPENINFRA_URL", "OPENINFRA_TOKEN", "INGEST_TOKEN", "OPENINFRA_NODE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["reporter.py", *argv])
    return reporter.parse_args()


def test_the_gateway_address_is_required(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _parse(monkeypatch)
    assert "OPENINFRA_URL" in capsys.readouterr().err


def test_the_address_and_token_come_from_the_environment(monkeypatch):
    args = _parse(monkeypatch, OPENINFRA_URL="https://gateway.example.org", INGEST_TOKEN="t")
    assert (args.dashboard, args.token) == ("https://gateway.example.org", "t")
    assert args.region is None
    assert args.cluster is None


def test_a_node_configuration_supplies_identity_and_engines(monkeypatch, tmp_path):
    path = tmp_path / "node.json"
    path.write_text(
        json.dumps(
            {
                "dashboard": "https://gateway.example.org",
                "node_id": "node-a",
                "region": "lab",
                "flush_interval": 10,
                "sglang": [{"url": "http://127.0.0.1:30000/metrics", "id": "node-a-chat"}],
            }
        )
    )
    args = _parse(monkeypatch, "--config", str(path))
    assert (args.dashboard, args.node_id, args.region, args.flush_interval) == (
        "https://gateway.example.org",
        "node-a",
        "lab",
        10.0,
    )
    assert reporter.sglang_targets(args) == [
        {"url": "http://127.0.0.1:30000/metrics", "id": "node-a-chat"}
    ]


def test_the_example_node_configuration_is_valid(monkeypatch):
    from pathlib import Path

    example = Path(reporter.__file__).with_name("node.example.json")
    args = _parse(monkeypatch, "--config", str(example), OPENINFRA_URL="https://g.example.org")
    targets = reporter.sglang_targets(args)
    assert targets
    assert all({"url", "id", "name", "engine", "gpu_indexes"} <= set(t) for t in targets)


SGLANG_METRICS = """\
sglang:num_running_reqs{{model_name="org/chat"}} 3
sglang:num_queue_reqs{{model_name="org/chat"}} 1
sglang:num_used_tokens{{model_name="org/chat"}} 5000
sglang:max_total_num_tokens{{model_name="org/chat"}} 100000
sglang:token_usage{{model_name="org/chat"}} 0.05
sglang:gen_throughput{{model_name="org/chat"}} 250
sglang:prompt_tokens_total{{model_name="org/chat"}} {prompt}
sglang:generation_tokens_total{{model_name="org/chat"}} {generated}
sglang:num_requests_total{{model_name="org/chat"}} {requests}
sglang:time_to_first_token_seconds_sum{{model_name="org/chat"}} {ttft_sum}
sglang:time_to_first_token_seconds_count{{model_name="org/chat"}} {requests}
"""

VLLM_METRICS = """\
vllm:num_requests_running{{model_name="org/embed"}} 2
vllm:num_requests_waiting{{model_name="org/embed"}} 0
vllm:kv_cache_usage_perc{{model_name="org/embed"}} 0.1
vllm:prompt_tokens_total{{model_name="org/embed"}} {prompt}
vllm:generation_tokens_total{{model_name="org/embed"}} {generated}
vllm:request_success_total{{model_name="org/embed",finished_reason="stop"}} {requests}
"""


def _scraped(target: dict, text: str) -> dict:
    return {"target": target, **reporter.parse_sglang_body(text)}


def test_the_gateway_accepts_what_a_flush_sends(monkeypatch):
    """The reporter's real flush, fed a scrape, produces a body the gateway stores."""
    monkeypatch.setattr(reporter, "container_for_port", lambda port: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        reporter, "post", lambda url, token, body: sent.append(body) or {"ok": True}
    )
    args = argparse.Namespace(
        node_id="node-a", dashboard="https://gateway.example.org", token="t", skip_gpu=False
    )
    node = {"id": "node-a", "hostname": "node-a", "region": "lab", "cluster": None}
    chat = {
        "url": "http://127.0.0.1:30000/metrics",
        "id": "node-a-chat",
        "name": "chat-model",
        "engine": "sglang",
        "endpoint": "http://node-a:30000",
        "gpu_indexes": [0],
    }
    embed = {
        "url": "http://127.0.0.1:8000/metrics",
        "id": "node-a-embed",
        "name": "embed-model",
        "engine": "vllm",
        "endpoint": "http://node-a:8000",
        "gpu_indexes": [1],
    }
    gpus = [
        {
            "index": 0,
            "name": "Test GPU",
            "utilization_pct": 70.0,
            "memory_used_mb": 1000.0,
            "memory_total_mb": 80000.0,
        },
        {"index": 1, "name": "Test GPU", "utilization_pct": 20.0},
    ]
    cpu = {"cpu_util_pct": 12.0, "load1": 0.5, "mem_used_mb": 1024, "mem_total_mb": 4096}

    def scrape(step: int) -> dict:
        return {
            "node-a-chat": _scraped(
                chat,
                SGLANG_METRICS.format(
                    prompt=1000 + 500 * step,
                    generated=2000 + 800 * step,
                    requests=10 + 2 * step,
                    ttft_sum=1.0 + 0.5 * step,
                ),
            ),
            "node-a-embed": _scraped(
                embed,
                VLLM_METRICS.format(prompt=100 + 50 * step, generated=0, requests=4 + step),
            ),
        }

    prev = reporter.flush(args, node, gpus, scrape(0), cpu, {}, 5.0, False)
    reporter.flush(args, node, gpus, scrape(1), cpu, prev, 5.0, True)

    assert len(sent) == 2
    body = sent[-1]
    snapshot = parse_snapshot(json.loads(json.dumps(body)))
    write = SnapshotWrite(snapshot, 1_800_000_000_000)
    assert snapshot.node is not None
    assert snapshot.node.id == "node-a"
    assert [g.index for g in snapshot.gpus] == [0, 1]
    assert [e.server.id for e in snapshot.engines] == ["node-a-chat", "node-a-embed"]
    assert [e.server.engine for e in snapshot.engines] == ["sglang", "vllm"]
    assert [row[0] for row in write.engines] == ["node-a-chat", "node-a-embed"]
    chat_metrics = snapshot.engines[0].metrics
    assert chat_metrics is not None
    assert chat_metrics["num_running_reqs"] == 3.0
    # The engine's busiest mapped GPU is reported as its SM utilization.
    assert chat_metrics["sm_util_pct"] == 70.0
    # Two requests in 5 s is 24 a minute.
    assert chat_metrics["req_per_min"] == pytest.approx(24.0)


def test_one_second_flushes_count_their_counters(monkeypatch):
    """A flush that comes a little early still gets rates from its counters."""
    monkeypatch.setattr(reporter, "container_for_port", lambda port: None)
    sent: list[dict] = []
    monkeypatch.setattr(
        reporter, "post", lambda url, token, body: sent.append(body) or {"ok": True}
    )
    args = argparse.Namespace(
        node_id="node-a", dashboard="https://gateway.example.org", token="t", skip_gpu=True
    )
    node = {"id": "node-a", "hostname": "node-a", "region": None, "cluster": None}
    chat = {
        "url": "http://127.0.0.1:30000/metrics",
        "id": "chat",
        "name": "chat",
        "engine": "sglang",
        "endpoint": "",
    }

    def scrape(requests: int) -> dict:
        text = SGLANG_METRICS.format(prompt=0, generated=0, requests=requests, ttft_sum=0)
        return {"chat": _scraped(chat, text)}

    prev = reporter.flush(args, node, [], scrape(10), None, {}, 1.0, False)
    reporter.flush(args, node, [], scrape(11), None, prev, 0.97, False, taken=1_700_000_000.5)

    # One request in 0.97 s.
    assert sent[-1]["sglang"][0]["metrics"]["req_per_min"] == pytest.approx(61.9, abs=0.1)
    assert sent[-1]["ts"] == 1_700_000_000_500
    assert "ts" not in sent[0]


@pytest.mark.parametrize(
    ("now", "interval", "slot"),
    [
        (100.2, 1.0, 100.5),
        (100.5, 1.0, 101.5),  # its own slot has begun: the next one
        (100.9, 1.0, 101.5),
        (101.0, 5.0, 102.5),
        (103.0, 5.0, 107.5),
        (100.1, 0.5, 100.25),
    ],
)
def test_snapshots_are_taken_mid_interval(now, interval, slot):
    assert reporter.next_slot(now, interval) == pytest.approx(slot)


def test_a_slow_snapshot_skips_the_slots_it_overran():
    # A snapshot taken at 100.5 that took 2.3 s: 101.5 and 102.5 have gone.
    assert reporter.next_slot(102.8, 1.0) == pytest.approx(103.5)


def test_the_flush_interval_must_be_positive(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _parse(monkeypatch, "--flush-interval", "0", OPENINFRA_URL="https://g.example.org")
    assert "positive" in capsys.readouterr().err


def test_flushes_are_one_second_apart_and_logged_once_a_minute_by_default(monkeypatch):
    args = _parse(monkeypatch, OPENINFRA_URL="https://g.example.org")
    assert (args.flush_interval, args.log_interval) == (1.0, 60.0)


def test_a_repeating_failure_is_reported_once_per_interval(monkeypatch, capsys):
    clock = iter([0.0, 1.0, 2.0, 61.0])
    monkeypatch.setattr(reporter.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(reporter, "_error_log", {})
    monkeypatch.setattr(reporter, "_error_interval", 60.0)
    for _ in range(4):
        reporter.log_error("flush", "flush error: refused")
    assert capsys.readouterr().err.splitlines() == [
        "flush error: refused",
        "flush error: refused (2 more since the last report)",
    ]


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        ("    0    1200    3400", (0, {"pcie_rx_bps": 1.2e9, "pcie_tx_bps": 3.4e9})),
        ("# gpu  rxpci  txpci", None),
        ("# Idx   MB/s   MB/s", None),
        ("    1      -      -", None),
        ("", None),
    ],
)
def test_a_dmon_line(line, parsed):
    assert reporter.parse_dmon_line(line) == parsed


def test_the_pcie_monitor_reports_recent_readings(monkeypatch):
    lines = ["# gpu rxpci txpci\n", "    0  100  200\n", "    1  300  400\n"]

    class Process:
        def __init__(self, *args, **kwargs):
            self.stdout = iter(lines)

        def wait(self):
            raise SystemExit  # ends the reader thread

    monkeypatch.setattr(reporter.subprocess, "Popen", Process)
    monitor = reporter.PcieMonitor()
    monitor.latest()  # starts the reader
    monitor._thread.join(timeout=5)
    assert monitor.latest() == {
        0: {"pcie_rx_bps": 1e8, "pcie_tx_bps": 2e8},
        1: {"pcie_rx_bps": 3e8, "pcie_tx_bps": 4e8},
    }
    # Readings go stale.
    monkeypatch.setattr(reporter.PcieMonitor, "MAX_AGE_S", -1.0)
    assert monitor.latest() == {}
