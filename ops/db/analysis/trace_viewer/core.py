"""Core data layer for the api_logs trace viewer.

Loads an exported ``api_logs`` trace (the JSONL produced by
``ops/db/export_logs.py``, optionally zstd-compressed) and turns it into
something a small web UI can query interactively:

* a light-weight in-memory index (one small dict per request) that powers
  fast filtering, aggregation and listing without holding every prompt and
  response body in memory, and
* lazy, on-demand reads of the full request body straight from disk (by byte
  offset) for the drill-down / detail view.

The parsing rules mirror ``ops/db/analysis/pretty_print_logs.py``: ``prompt``,
``response``, ``tools`` and ``metadata`` come out of the export as
JSON-encoded strings and are decoded here.

Nothing in this module touches a database or the network -- it only reads the
export file on disk.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # zstandard ships with the project (ops/db/export_logs.py uses it too)
    import zstandard as zstd
except ImportError:  # pragma: no cover - zstandard is a project dependency
    zstd = None  # type: ignore[assignment]

# Fields we pull into the light-weight index for every row. Everything else
# (prompt/response/tools bodies) is read lazily for the detail view only.
_NICE_BUCKETS = (
    60,
    300,
    900,
    3600,
    6 * 3600,
    12 * 3600,
    86400,
    7 * 86400,
    30 * 86400,
)


def _maybe_json(value: Any, default: Any = None) -> Any:
    """Decode a value that may be a JSON string, an object, or ``None``.

    Export columns backed by ``JSONB``/``TEXT`` (``prompt``, ``tools``,
    ``metadata``, ...) arrive as JSON-encoded strings. Some tooling may have
    already decoded them, so accept objects untouched and fall back to the raw
    string when it is not valid JSON.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _to_float(value: Any) -> float | None:
    """Best-effort float parse (cost columns export as decimal strings)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    """Best-effort int parse."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> float | None:
    """Parse a timestamp into epoch seconds (UTC), or ``None``.

    Handles ISO-8601 strings (the export format), bare epoch numbers (the
    qwen-trace format) and trailing ``Z`` designators.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _histogram(values: list[float], n_bins: int = 30) -> dict[str, Any]:
    """Build a linear histogram clipped at p99 with an overflow bucket."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"bins": [], "count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    p50 = _percentile(vals, 0.50)
    p95 = _percentile(vals, 0.95)
    p99 = _percentile(vals, 0.99)
    vmax = vals[-1]
    hi = p99 if p99 and p99 > 0 else vmax
    if hi <= 0:
        hi = 1.0
    width = hi / n_bins
    bins = [{"lo": i * width, "hi": (i + 1) * width, "count": 0} for i in range(n_bins)]
    overflow = 0
    for v in vals:
        if v >= hi:
            overflow += 1
            continue
        i = min(int(v / width), n_bins - 1)
        bins[i]["count"] += 1
    if overflow:
        bins.append({"lo": hi, "hi": vmax, "count": overflow, "overflow": True})
    return {
        "bins": bins,
        "count": len(vals),
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "max": vmax,
    }


def _pick_bucket(span_seconds: float, target_buckets: int = 160) -> int:
    """Choose a 'nice' time-bucket width for a given span."""
    if span_seconds <= 0:
        return _NICE_BUCKETS[0]
    ideal = span_seconds / target_buckets
    for b in _NICE_BUCKETS:
        if b >= ideal:
            return b
    return _NICE_BUCKETS[-1]


class TraceStore:
    """In-memory index over an exported api_logs trace file.

    Call :meth:`close` when done to remove any temporary decompressed file.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Load and index the trace at ``path`` (``.jsonl`` or ``.jsonl.zst``)."""
        self.source_path = Path(path)
        if not self.source_path.is_file():
            raise FileNotFoundError(f"trace file not found: {self.source_path}")
        self._tmp_path: Path | None = None
        self._plain_path = self._ensure_plain(self.source_path)
        self.records: list[dict[str, Any]] = []
        self._offsets: list[int] = []
        self.invalid_lines = 0
        self._load()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._build_sessions()

    # -- loading ---------------------------------------------------------

    def _ensure_plain(self, path: Path) -> Path:
        """Return a plain-text JSONL path, decompressing ``.zst`` if needed."""
        if path.suffix != ".zst":
            return path
        if zstd is None:  # pragma: no cover - dependency always present
            raise RuntimeError("zstandard is required to read .zst exports")
        fd, tmp_name = tempfile.mkstemp(prefix="trace_viewer_", suffix=".jsonl")
        os.close(fd)
        self._tmp_path = Path(tmp_name)
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as src, open(self._tmp_path, "wb") as dst:
            dctx.copy_stream(src, dst)
        return self._tmp_path

    def _load(self) -> None:
        """Scan the plain file once, indexing each row by byte offset."""
        with open(self._plain_path, "rb") as f:
            while True:
                offset = f.tell()
                raw = f.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    self.invalid_lines += 1
                    continue
                if not isinstance(row, dict):
                    self.invalid_lines += 1
                    continue
                self.records.append(self._index_row(row, len(self.records), offset))
                self._offsets.append(offset)

    @staticmethod
    def _index_row(row: dict[str, Any], row_index: int, offset: int) -> dict[str, Any]:
        """Extract the light-weight, filter/aggregate-friendly fields."""
        meta = _maybe_json(row.get("metadata"), {}) or {}
        if not isinstance(meta, dict):
            meta = {}
        status = _to_int(row.get("status_code"))
        error = row.get("error")
        has_error = bool(error) or (status is not None and status >= 400)
        prompt_tokens = _to_int(row.get("prompt_tokens")) or 0
        completion_tokens = _to_int(row.get("completion_tokens")) or 0
        total_tokens = _to_int(row.get("total_tokens"))
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens
        return {
            "row": row_index,
            "offset": offset,
            "id": row.get("id"),
            "request_id": row.get("request_id"),
            "timestamp": row.get("timestamp"),
            "ts": _parse_ts(row.get("timestamp")),
            "model_id": row.get("model_id"),
            "provider": row.get("provider"),
            "served_model_id": row.get("served_model_id"),
            "served_endpoint_id": row.get("served_endpoint_id"),
            "status_code": status,
            "has_error": has_error,
            "error": (str(error)[:200] if error else None),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": _to_int(row.get("reasoning_tokens")) or 0,
            "total_tokens": total_tokens,
            "cache_read_tokens": _to_int(row.get("cache_read_tokens")) or 0,
            "cache_write_tokens": _to_int(row.get("cache_write_tokens")) or 0,
            "latency_ms": _to_int(row.get("latency_ms")),
            "ttft_ms": _to_int(row.get("ttft_ms")),
            "cost_usd": _to_float(row.get("cost_usd")) or 0.0,
            "upstream_cost_usd": _to_float(row.get("upstream_cost_usd")) or 0.0,
            "user_id": row.get("user_id"),
            "session_id": row.get("session_id"),
            "stream": row.get("stream"),
            "num_turns": _to_int(row.get("num_turns")),
            "num_user_turns": _to_int(row.get("num_user_turns")),
            "num_tool_calls": _to_int(row.get("num_tool_calls")) or 0,
            "agent": meta.get("agent") if isinstance(meta, dict) else None,
            "request_type": meta.get("request_type") if isinstance(meta, dict) else None,
        }

    def close(self) -> None:
        """Remove the temporary decompressed file, if any."""
        if self._tmp_path and self._tmp_path.exists():
            with contextlib.suppress(OSError):  # pragma: no cover
                self._tmp_path.unlink()
            self._tmp_path = None

    # -- detail ----------------------------------------------------------

    def get_full_record(self, row_index: int) -> dict[str, Any] | None:
        """Read one full row from disk and decode its nested JSON payloads."""
        if row_index < 0 or row_index >= len(self._offsets):
            return None
        with open(self._plain_path, "rb") as f:
            f.seek(self._offsets[row_index])
            raw = f.readline()
        try:
            row = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        light = self.records[row_index]
        row["_light"] = light
        row["_row"] = row_index
        row["prompt_parsed"] = _maybe_json(row.get("prompt"), [])
        row["response_parsed"] = _maybe_json(row.get("response"), {})
        row["tools_parsed"] = _maybe_json(row.get("tools"), [])
        row["metadata_parsed"] = _maybe_json(row.get("metadata"), {})
        row["request_payload_parsed"] = _maybe_json(row.get("request_payload"), {})
        return row

    # -- filtering -------------------------------------------------------

    def filter(self, spec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return light-weight records matching a filter spec.

        Recognised keys: ``start``/``end`` (epoch seconds), ``model_id``,
        ``provider``, ``served_model_id``, ``user_id``, ``session_id``,
        ``agent``, ``status_code`` (int), ``errors_only`` (bool) and ``q``
        (case-insensitive substring over id/request_id/user/model/error).
        """
        spec = spec or {}
        start = spec.get("start")
        end = spec.get("end")
        q = (spec.get("q") or "").strip().lower()
        exact = {
            k: spec[k]
            for k in ("model_id", "provider", "served_model_id", "user_id", "session_id", "agent")
            if spec.get(k) not in (None, "")
        }
        status = (
            _to_int(spec.get("status_code")) if spec.get("status_code") not in (None, "") else None
        )
        errors_only = bool(spec.get("errors_only"))

        out: list[dict[str, Any]] = []
        for r in self.records:
            ts = r["ts"]
            if start is not None and (ts is None or ts < start):
                continue
            if end is not None and (ts is None or ts > end):
                continue
            if errors_only and not r["has_error"]:
                continue
            if status is not None and r["status_code"] != status:
                continue
            if any(r.get(k) != v for k, v in exact.items()):
                continue
            if q:
                hay = " ".join(
                    str(r.get(k) or "")
                    for k in ("id", "request_id", "user_id", "model_id", "provider", "error")
                ).lower()
                if q not in hay:
                    continue
            out.append(r)
        return out

    # -- aggregation -----------------------------------------------------

    def summary(self, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        """Compute the overview dashboard payload over a filtered subset."""
        rows = self.filter(spec)
        n = len(rows)
        users = {r["user_id"] for r in rows if r["user_id"]}
        errors = sum(1 for r in rows if r["has_error"])
        cost = sum(r["cost_usd"] for r in rows)
        upstream_cost = sum(r["upstream_cost_usd"] for r in rows)
        prompt_tokens = sum(r["prompt_tokens"] for r in rows)
        completion_tokens = sum(r["completion_tokens"] for r in rows)
        latencies = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
        ttfts = sorted(r["ttft_ms"] for r in rows if r["ttft_ms"] is not None)
        timestamps = [r["ts"] for r in rows if r["ts"] is not None]

        return {
            "kpis": {
                "requests": n,
                "users": len(users),
                "errors": errors,
                "error_rate": (errors / n) if n else 0.0,
                "cost_usd": cost,
                "upstream_cost_usd": upstream_cost,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "latency_p50": _percentile(latencies, 0.50),
                "latency_p95": _percentile(latencies, 0.95),
                "latency_p99": _percentile(latencies, 0.99),
                "ttft_p50": _percentile(ttfts, 0.50),
                "ttft_p95": _percentile(ttfts, 0.95),
                "start_ts": min(timestamps) if timestamps else None,
                "end_ts": max(timestamps) if timestamps else None,
            },
            "timeseries": self._timeseries(rows),
            "by_model": self._breakdown(rows, "model_id"),
            "by_provider": self._breakdown(rows, "provider"),
            "by_served_model": self._breakdown(rows, "served_model_id"),
            "by_status": self._breakdown(rows, "status_code", limit=None, numeric=True),
            "top_users": self._breakdown(rows, "user_id"),
            "latency_hist": _histogram(latencies),
            "ttft_hist": _histogram(ttfts),
            "prompt_tokens_hist": _histogram([r["prompt_tokens"] for r in rows]),
            "completion_tokens_hist": _histogram([r["completion_tokens"] for r in rows]),
        }

    @staticmethod
    def _timeseries(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Bucket rows over time into counts, tokens, cost and error series."""
        ts_rows = [r for r in rows if r["ts"] is not None]
        if not ts_rows:
            return {"bucket_seconds": 0, "points": []}
        lo = min(r["ts"] for r in ts_rows)
        hi = max(r["ts"] for r in ts_rows)
        bucket = _pick_bucket(hi - lo)
        agg: dict[int, dict[str, float]] = {}
        for r in ts_rows:
            key = int(r["ts"] // bucket) * bucket
            slot = agg.setdefault(
                key,
                {"count": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "errors": 0},
            )
            slot["count"] += 1
            slot["prompt_tokens"] += r["prompt_tokens"]
            slot["completion_tokens"] += r["completion_tokens"]
            slot["cost"] += r["cost_usd"]
            if r["has_error"]:
                slot["errors"] += 1
        points = [
            {
                "ts": key,
                "iso": datetime.fromtimestamp(key, tz=timezone.utc).isoformat(),
                **vals,
            }
            for key, vals in sorted(agg.items())
        ]
        return {"bucket_seconds": bucket, "points": points}

    @staticmethod
    def _breakdown(
        rows: list[dict[str, Any]],
        key: str,
        limit: int | None = 20,
        numeric: bool = False,
    ) -> list[dict[str, Any]]:
        """Group rows by ``key`` with per-group count, cost and token totals."""
        agg: dict[Any, dict[str, float]] = {}
        for r in rows:
            k = r.get(key)
            if k is None:
                k = "(none)"
            slot = agg.setdefault(k, {"count": 0, "cost": 0.0, "tokens": 0, "errors": 0})
            slot["count"] += 1
            slot["cost"] += r["cost_usd"]
            slot["tokens"] += r["total_tokens"] or 0
            if r["has_error"]:
                slot["errors"] += 1
        items = [{"key": str(k), "raw": k, **v} for k, v in agg.items()]
        if numeric:
            items.sort(key=lambda x: (x["raw"] == "(none)", x["raw"]))
        else:
            items.sort(key=lambda x: x["count"], reverse=True)
        if limit is not None and len(items) > limit:
            head = items[:limit]
            rest = items[limit:]
            head.append(
                {
                    "key": f"(+{len(rest)} more)",
                    "raw": None,
                    "count": sum(x["count"] for x in rest),
                    "cost": sum(x["cost"] for x in rest),
                    "tokens": sum(x["tokens"] for x in rest),
                    "errors": sum(x["errors"] for x in rest),
                }
            )
            return head
        return items

    def list_requests(
        self,
        spec: dict[str, Any] | None = None,
        sort: str = "ts",
        order: str = "desc",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Return a filtered, sorted, paginated slice of light-weight records."""
        rows = self.filter(spec)
        reverse = order != "asc"
        default = -math.inf if reverse else math.inf

        def key_fn(r: dict[str, Any]) -> Any:
            v = r.get(sort)
            if v is None:
                return default
            if isinstance(v, (int, float)):
                return v
            return str(v)

        try:
            rows.sort(key=key_fn, reverse=reverse)
        except TypeError:
            rows.sort(key=lambda r: str(r.get(sort) or ""), reverse=reverse)
        total = len(rows)
        page = max(1, page)
        start = (page - 1) * page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "rows": rows[start : start + page_size],
        }

    # -- sessions --------------------------------------------------------

    def _build_sessions(self, gap_seconds: float = 1800.0) -> None:
        """Group requests into sessions.

        Uses the ``session_id`` column when present; otherwise infers sessions
        heuristically per user, starting a new one after an idle gap. Inferred
        sessions are marked ``source="inferred"`` and are approximate.
        """
        sessions: dict[str, dict[str, Any]] = {}
        # 1) explicit session_id grouping.
        no_sid: list[dict[str, Any]] = []
        for r in self.records:
            sid = r.get("session_id")
            if sid:
                key = f"sid:{sid}"
                sessions.setdefault(key, {"source": "session_id", "rows": []})["rows"].append(r)
            else:
                no_sid.append(r)
        # 2) infer sessions for the rest, per user, split on idle gaps.
        by_user: dict[Any, list[dict[str, Any]]] = {}
        for r in no_sid:
            by_user.setdefault(r.get("user_id") or "(anon)", []).append(r)
        for user, urows in by_user.items():
            urows.sort(key=lambda r: (r["ts"] if r["ts"] is not None else 0, r["row"]))
            seq = 0
            prev_ts: float | None = None
            cur: list[dict[str, Any]] = []
            for r in urows:
                ts = r["ts"]
                if cur and prev_ts is not None and ts is not None and (ts - prev_ts) > gap_seconds:
                    sessions[f"inf:{user}:{seq}"] = {"source": "inferred", "rows": cur}
                    seq += 1
                    cur = []
                cur.append(r)
                if ts is not None:
                    prev_ts = ts
            if cur:
                sessions[f"inf:{user}:{seq}"] = {"source": "inferred", "rows": cur}
        # 3) finalize summaries.
        for sid, sess in sessions.items():
            sess["sid"] = sid
            sess["summary"] = self._session_summary(sid, sess)
        self._sessions = sessions

    @staticmethod
    def _session_summary(sid: str, sess: dict[str, Any]) -> dict[str, Any]:
        """Compute the compact summary row for one session."""
        rows = sorted(sess["rows"], key=lambda r: (r["ts"] if r["ts"] is not None else 0, r["row"]))
        tss = [r["ts"] for r in rows if r["ts"] is not None]
        start = min(tss) if tss else None
        end = max(tss) if tss else None
        return {
            "sid": sid,
            "source": sess["source"],
            "user_id": rows[0].get("user_id") if rows else None,
            "n_requests": len(rows),
            "start_ts": start,
            "end_ts": end,
            "duration_s": (end - start) if (start is not None and end is not None) else None,
            "models": sorted({r["model_id"] for r in rows if r["model_id"]}),
            "providers": sorted({r["provider"] for r in rows if r["provider"]}),
            "total_tokens": sum(r["total_tokens"] or 0 for r in rows),
            "total_cost": sum(r["cost_usd"] for r in rows),
            "n_tool_calls": sum(r["num_tool_calls"] for r in rows),
            "errors": sum(1 for r in rows if r["has_error"]),
        }

    def list_sessions(
        self,
        spec: dict[str, Any] | None = None,
        sort: str = "start_ts",
        order: str = "desc",
        page: int = 1,
        page_size: int = 50,
        min_requests: int = 1,
    ) -> dict[str, Any]:
        """Return filtered, sorted, paginated session summaries."""
        spec = spec or {}
        user = spec.get("user_id")
        model = spec.get("model_id")
        summaries = []
        for sess in self._sessions.values():
            s = sess["summary"]
            if s["n_requests"] < min_requests:
                continue
            if user and s["user_id"] != user:
                continue
            if model and model not in s["models"]:
                continue
            summaries.append(s)
        reverse = order != "asc"
        default = -math.inf if reverse else math.inf
        summaries.sort(
            key=lambda s: s.get(sort) if s.get(sort) is not None else default,
            reverse=reverse,
        )
        total = len(summaries)
        page = max(1, page)
        start = (page - 1) * page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "rows": summaries[start : start + page_size],
        }

    def get_session(self, sid: str) -> dict[str, Any] | None:
        """Return one session's summary plus its ordered request timeline."""
        sess = self._sessions.get(sid)
        if not sess:
            return None
        rows = sorted(sess["rows"], key=lambda r: (r["ts"] if r["ts"] is not None else 0, r["row"]))
        start = sess["summary"]["start_ts"]
        cumulative = 0
        timeline = []
        for i, r in enumerate(rows):
            cumulative += r["total_tokens"] or 0
            timeline.append(
                {
                    "seq": i,
                    "row": r["row"],
                    "offset_s": (r["ts"] - start)
                    if (r["ts"] is not None and start is not None)
                    else None,
                    "timestamp": r["timestamp"],
                    "model_id": r["model_id"],
                    "provider": r["provider"],
                    "status_code": r["status_code"],
                    "has_error": r["has_error"],
                    "latency_ms": r["latency_ms"],
                    "ttft_ms": r["ttft_ms"],
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "total_tokens": r["total_tokens"],
                    "cumulative_tokens": cumulative,
                    "num_tool_calls": r["num_tool_calls"],
                    "cost_usd": r["cost_usd"],
                }
            )
        return {"summary": sess["summary"], "requests": timeline}

    # -- meta ------------------------------------------------------------

    def meta(self) -> dict[str, Any]:
        """Return file-level metadata for the UI header."""
        tss = [r["ts"] for r in self.records if r["ts"] is not None]
        present = set()
        for r in self.records[:200]:
            for k in ("served_model_id", "session_id", "cost_usd", "ttft_ms", "num_tool_calls"):
                if r.get(k) not in (None, "", 0):
                    present.add(k)
        return {
            "source_path": str(self.source_path),
            "total_rows": len(self.records),
            "invalid_lines": self.invalid_lines,
            "n_sessions": len(self._sessions),
            "start_ts": min(tss) if tss else None,
            "end_ts": max(tss) if tss else None,
            "columns_present": sorted(present),
        }
