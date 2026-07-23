"""Unit tests for the trace-viewer core data layer.

Exercises loading (plain + zstd), the JSON-string field decoding, filtering,
aggregation, request listing/sorting and session grouping against a small
synthetic export shaped like ``ops/db/export_logs.py`` output.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import zstandard as zstd

from ops.db.analysis.trace_viewer.core import TraceStore

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _row(i: int, **over: object) -> dict:
    """Build one export-shaped row (nested payloads as JSON strings)."""
    pt, ct = 100 + i, 20 + i
    is_err = over.pop("error_row", False)
    prompt = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": f"hello {i}"},
    ]
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": f"hi {i}"}}
        ],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }
    row = {
        "id": i + 1,
        "request_id": f"req_{i:04d}",
        "timestamp": (BASE + timedelta(minutes=i)).isoformat(),
        "model_id": "glm-4.6" if i % 2 == 0 else "kimi-k2",
        "provider": "zhipu" if i % 2 == 0 else "moonshot",
        "served_model_id": "glm-4.6-served",
        "status_code": 500 if is_err else 200,
        "error": "boom" if is_err else None,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "latency_ms": None if is_err else 1000 + i * 10,
        "ttft_ms": None if is_err else 100 + i,
        "cost_usd": f"{0.001 * (i + 1):.8f}",
        "upstream_cost_usd": f"{0.0008 * (i + 1):.8f}",
        "user_id": "alice" if i < 6 else "bob",
        "session_id": "sess-A" if i < 4 else None,
        "num_tool_calls": i % 3,
        "metadata": json.dumps({"agent": "claude-code", "request_type": "chat"}),
        "tools": json.dumps([{"type": "function", "function": {"name": "bash"}}]),
        "prompt": json.dumps(prompt),
        "response": json.dumps(response),
    }
    row.update(over)
    return row


@pytest.fixture
def export_path(tmp_path):
    """Write a 10-row synthetic export (+1 blank +1 malformed line)."""
    rows = [_row(i, error_row=(i == 9)) for i in range(10)]
    p = tmp_path / "export.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write("\n")
        f.write("{bad json\n")
    return p


def test_load_and_meta(export_path):
    store = TraceStore(export_path)
    try:
        meta = store.meta()
        assert meta["total_rows"] == 10
        assert meta["invalid_lines"] == 1  # malformed line; blank line ignored
        assert meta["start_ts"] is not None and meta["end_ts"] > meta["start_ts"]
        assert "session_id" in meta["columns_present"]
    finally:
        store.close()


def test_zstd_roundtrip_matches_plain(export_path, tmp_path):
    zpath = tmp_path / "export.jsonl.zst"
    cctx = zstd.ZstdCompressor()
    with export_path.open("rb") as src, zpath.open("wb") as dst, cctx.stream_writer(dst) as w:
        w.write(src.read())
    plain = TraceStore(export_path)
    comp = TraceStore(zpath)
    tmp = comp._tmp_path
    try:
        assert comp.meta()["total_rows"] == plain.meta()["total_rows"] == 10
        assert comp.summary()["kpis"]["cost_usd"] == pytest.approx(
            plain.summary()["kpis"]["cost_usd"]
        )
        # a temp file is created for the .zst path...
        assert tmp is not None and tmp.exists()
    finally:
        plain.close()
        comp.close()
    # ...and removed on close (the pointer is cleared too)
    assert not tmp.exists()
    assert comp._tmp_path is None


def test_filter_and_summary(export_path):
    store = TraceStore(export_path)
    try:
        s = store.summary()
        k = s["kpis"]
        assert k["requests"] == 10
        assert k["errors"] == 1
        assert k["error_rate"] == pytest.approx(0.1)
        assert k["cost_usd"] > 0
        assert k["latency_p50"] is not None

        # provider filter
        zhipu = store.summary({"provider": "zhipu"})
        assert zhipu["kpis"]["requests"] == 5
        assert all(b["raw"] in ("zhipu", None) for b in zhipu["by_provider"])

        # errors_only filter
        errs = store.summary({"errors_only": True})
        assert errs["kpis"]["requests"] == errs["kpis"]["errors"] == 1

        # status breakdown lists both codes
        codes = {b["raw"] for b in s["by_status"]}
        assert codes == {200, 500}

        # substring search
        one = store.filter({"q": "req_0003"})
        assert len(one) == 1 and one[0]["request_id"] == "req_0003"
    finally:
        store.close()


def test_histograms_and_timeseries(export_path):
    store = TraceStore(export_path)
    try:
        s = store.summary()
        assert s["latency_hist"]["count"] == 9  # error row has null latency
        assert s["latency_hist"]["p50"] is not None
        assert sum(b["count"] for b in s["latency_hist"]["bins"]) == 9
        ts = s["timeseries"]
        assert ts["bucket_seconds"] > 0
        assert sum(p["count"] for p in ts["points"]) == 10
    finally:
        store.close()


def test_list_requests_sort_and_page(export_path):
    store = TraceStore(export_path)
    try:
        desc = store.list_requests(sort="prompt_tokens", order="desc", page=1, page_size=3)
        assert desc["total"] == 10
        pts = [r["prompt_tokens"] for r in desc["rows"]]
        assert pts == sorted(pts, reverse=True)

        asc = store.list_requests(sort="prompt_tokens", order="asc", page=1, page_size=3)
        assert asc["rows"][0]["prompt_tokens"] <= asc["rows"][-1]["prompt_tokens"]

        page2 = store.list_requests(sort="id", order="asc", page=2, page_size=4)
        assert page2["page"] == 2 and len(page2["rows"]) == 4
        assert page2["rows"][0]["id"] == 5
    finally:
        store.close()


def test_get_full_record_decodes_payloads(export_path):
    store = TraceStore(export_path)
    try:
        rec = store.get_full_record(0)
        assert rec is not None
        assert isinstance(rec["prompt_parsed"], list)
        assert rec["prompt_parsed"][0]["role"] == "system"
        assert rec["response_parsed"]["choices"][0]["message"]["content"].startswith("hi")
        assert rec["tools_parsed"][0]["function"]["name"] == "bash"
        assert rec["metadata_parsed"]["agent"] == "claude-code"
        assert store.get_full_record(9999) is None
    finally:
        store.close()


def test_sessions_explicit_and_inferred(export_path):
    store = TraceStore(export_path)
    try:
        listing = store.list_sessions(page_size=100, min_requests=1)
        by_source = {}
        for s in listing["rows"]:
            by_source.setdefault(s["source"], []).append(s)
        assert "session_id" in by_source  # rows 0-3 share sess-A
        assert "inferred" in by_source  # rows without session_id

        explicit = next(s for s in listing["rows"] if s["sid"] == "sid:sess-A")
        assert explicit["n_requests"] == 4
        assert explicit["user_id"] == "alice"

        detail = store.get_session("sid:sess-A")
        assert detail is not None
        assert len(detail["requests"]) == 4
        cum = [r["cumulative_tokens"] for r in detail["requests"]]
        assert cum == sorted(cum)  # cumulative tokens are non-decreasing
        assert detail["requests"][0]["seq"] == 0

        assert store.get_session("nope") is None
    finally:
        store.close()


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        TraceStore(tmp_path / "does-not-exist.jsonl")
