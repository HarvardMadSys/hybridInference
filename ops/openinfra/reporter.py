#!/usr/bin/env python3
"""OpenInfra reporter: scrape this GPU host and post a snapshot to the gateway every second.

Runs on each GPU host, apart from the gateway. Each flush it reads the GPUs
(``nvidia-smi``, and DCGM's exporter on ``127.0.0.1:9400`` when it runs), the
host (``/proc``) and every SGLang or vLLM engine the node configuration lists
(``/v1/loads`` or ``/metrics``, and the engine's container logs for request
shapes), and posts one JSON snapshot to the gateway's
``POST /internal/openinfra/ingest`` with ``Authorization: Bearer <token>``.
Only outbound HTTPS is needed: the gateway never connects to the host.

Snapshots are taken in the middle of each flush interval of wall-clock time
and carry the time they were taken (``ts``), so network delay cannot put two
in the gateway's same second. A snapshot that takes longer than the interval
skips the next one rather than queueing it.

    OPENINFRA_URL=https://gateway.example.org OPENINFRA_TOKEN=... \
        python3 reporter.py --config node.json --once

``OPENINFRA_URL`` is any address that reaches the gateway: the public site, or
the backend's address on a private network. The token is the gateway's
``OPENINFRA_INGEST_TOKEN``. Standard library only, so it runs on the system
Python of a GPU host. See docs/developer/openinfra.md.
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

_LOG_SKIP = (
    "/health",
    "/v1/loads",
    "/get_server_info",
    "/server_info",
    "/metrics",
    "/flush_cache",
)
_LOG_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_PREFILL_CACHED = re.compile(r"#cached-token:\s*([\d.]+)")
_PREFILL_NEW = re.compile(r"#new-token:\s*([\d.]+)")
_PREFILL_TPUT = re.compile(r"input throughput \(token/s\):\s*([\d.]+)")
_PREFILL_PENDING = re.compile(r"#pending-token:\s*([\d.]+)")
_PREFILL_SEQ = re.compile(r"#new-seq:\s*([\d.]+)")
_QUEUE_REQ = re.compile(r"#queue-req:\s*([\d.]+)")
_RUNNING_REQ = re.compile(r"#running-req:\s*([\d.]+)")
_FULL_TOKEN = re.compile(r"#full token:\s*([\d.]+)")
_ACCEPT_LEN = re.compile(r"accept len:\s*([\d.]+)")
_ACCEPT_RATE = re.compile(r"accept rate:\s*([\d.]+)")
_DECODE_TPUT = re.compile(r"gen throughput \(token/s\):\s*([\d.]+)")
_TP_OTHER = re.compile(r"\bTP([1-9]\d*)\b")
_FINISH_KV = re.compile(
    r"[\"']?(prompt_tokens|completion_tokens|cached_tokens|queue_time|prefill_latency|"
    r"prefill_delay|e2e_latency|time_per_token|time_to_first_token|decode_throughput|"
    r"spec_accept_length|spec_accept_rate|inference_time)[\"']?\s*:\s*"
    r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)
_VLLM_FINISHED = re.compile(
    r"Finished request.*?prompt[^\d]{0,16}(\d+).*?(?:generation|output|generated)[^\d]{0,16}(\d+)",
    re.I,
)
_HTTP_STATUS = re.compile(r'"POST /[^"]*"\s+(\d{3})')
MAX_REQUEST_EVENTS = 20
REQUEST_FLUSH_S = 60.0
#: How often a flush is logged; failures always are.
LOG_INTERVAL_S = 60.0
_docker_probed = False
_docker_cmd: list[str] | None = None


def docker_cmd() -> list[str] | None:
    """The docker command this host lets the reporter run, probed once.

    ``docker`` or ``sudo -n docker``; None when neither works.
    """
    global _docker_probed, _docker_cmd
    if _docker_probed:
        return _docker_cmd
    _docker_probed = True
    for cmd in (["docker"], ["sudo", "-n", "docker"]):
        try:
            result = subprocess.run([*cmd, "ps", "-q"], capture_output=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            _docker_cmd = cmd
            break
    return _docker_cmd


def _parse_log_ts(stamp: str) -> float | None:
    try:
        naive = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    local_ts = naive.timestamp()
    # timezone.utc, not datetime.UTC: a GPU host's system Python may predate 3.11.
    utc_ts = naive.replace(tzinfo=datetime.timezone.utc).timestamp()  # noqa: UP017
    now = time.time()
    return utc_ts if abs(utc_ts - now) <= abs(local_ts - now) else local_ts


def container_for_port(port: int) -> str | None:
    """The name of the container that publishes *port*, or None."""
    cmd = docker_cmd()
    if not cmd:
        return None
    try:
        result = subprocess.run(
            [*cmd, "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    needle = f":{port}->"
    for line in result.stdout.splitlines():
        name, _, ports = line.partition("\t")
        if needle in ports:
            return name.strip()
    return None


def _re_f(rx: re.Pattern[str], line: str) -> float | None:
    match = rx.search(line)
    return float(match.group(1)) if match else None


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _finish_event(ts: float | None, line: str) -> dict[str, Any] | None:
    found = {k: float(v) for k, v in _FINISH_KV.findall(line)}
    if "prompt_tokens" not in found and "completion_tokens" not in found:
        return None
    prompt = found.get("prompt_tokens")
    cached = found.get("cached_tokens")
    uncached = None
    if prompt is not None and cached is not None:
        uncached = max(0.0, prompt - cached)
    prefill = found.get("prefill_latency")
    delay = found.get("prefill_delay")
    queue = found.get("queue_time")
    ttft = found.get("time_to_first_token")
    if ttft is None:
        parts = [x for x in (queue, delay, prefill) if x is not None]
        ttft = sum(parts) if parts else prefill
    return {
        "ts": int((ts or time.time()) * 1000),
        "input_len": int(prompt) if prompt is not None else None,
        "output_len": int(found["completion_tokens"]) if "completion_tokens" in found else None,
        "cache_len": int(cached) if cached is not None else None,
        "uncached_len": int(uncached) if uncached is not None else None,
        "ttft_s": _round(ttft),
        "queue_s": _round(queue),
        "prefill_s": _round(
            prefill if delay is None else prefill + delay if prefill is not None else delay
        ),
        "tpot_s": _round(found.get("time_per_token")),
        "e2e_s": _round(found.get("e2e_latency") or found.get("inference_time")),
        "decode_tps": _round(found.get("decode_throughput"), 1),
        "spec_accept_len": _round(found.get("spec_accept_length"), 2),
        "spec_accept_rate": _round(found.get("spec_accept_rate"), 4),
        "batch_size": 1,
    }


def _open_from_prefill(session: dict[str, Any] | None) -> dict[str, Any] | None:
    if not session:
        return None
    uncached = session["uncached"]
    cache = session["cache"]
    if uncached <= 0 and cache <= 0:
        return None
    if session["chunks"] == 1 and uncached == 1 and cache == 0:
        return None
    token_prefill = session["prefill_s"] or None
    wall = None
    if session["start_ts"] is not None and session["end_ts"] is not None:
        wall = max(0.0, session["end_ts"] - session["start_ts"])
    prefill_s = wall if wall and wall > 0 else token_prefill
    if prefill_s and token_prefill and prefill_s > token_prefill * 4:
        prefill_s = token_prefill
    if (not wall or wall == 0) and token_prefill and token_prefill > 1.5:
        prefill_s = None
    return {
        "start_ts": session["start_ts"],
        "end_ts": session["end_ts"],
        "input_len": int(cache + uncached),
        "cache_len": int(cache),
        "uncached_len": int(uncached),
        "prefill_s": prefill_s,
        "ttft_s": prefill_s,
        "batch_size": int(session["batch"] or 1),
        "out": 0.0,
        "tputs": [],
        "accepts": [],
        "rates": [],
        "last_ts": session["end_ts"],
        "chunks": session["chunks"],
    }


def _emit_inflight(req: dict[str, Any]) -> dict[str, Any]:
    output_len = round(req["out"]) if req["out"] >= 1 else None
    decode_tps = mean(req["tputs"])
    spec_len = mean(req["accepts"])
    spec_rate = mean(req["rates"])
    e2e = None
    if req["start_ts"] is not None and req.get("last_ts") is not None:
        e2e = max(0.0, req["last_ts"] - req["start_ts"])
    tpot = 1.0 / decode_tps if decode_tps and decode_tps > 0 else None
    prefill_s = req.get("prefill_s")
    if output_len and output_len > 1 and e2e is not None and prefill_s is not None:
        decode_s = max(0.0, e2e - prefill_s)
        if decode_s > 0:
            tpot = decode_s / (output_len - 1)
    return {
        "ts": int((req["start_ts"] or req["last_ts"] or time.time()) * 1000),
        "input_len": req["input_len"],
        "output_len": output_len,
        "cache_len": req["cache_len"],
        "uncached_len": req["uncached_len"],
        "ttft_s": _round(req.get("ttft_s")),
        "queue_s": None,
        "prefill_s": _round(prefill_s),
        "tpot_s": _round(tpot),
        "e2e_s": _round(e2e),
        "decode_tps": _round(decode_tps, 1),
        "spec_accept_len": _round(spec_len, 2),
        "spec_accept_rate": _round(spec_rate, 4),
        "batch_size": req["batch_size"],
        "chunks": req.get("chunks") or 1,
    }


def parse_engine_log_text(
    text: str,
    since_s: float,
    start: float | None = None,
    end: float | None = None,
) -> dict[str, Any]:
    """Summarize engine log lines from the last since_s seconds.

    With start and end (epoch seconds), count only lines stamped in
    [start, end) instead ("tiled" in the result). Reads that each start where
    the previous one ended then see every line once; a trailing window read
    every ~60 s overlaps or gaps its neighbours by a second or two. Lines
    without a timestamp cannot be placed and are counted either way, as before.
    """
    posts: list[float | None] = []
    http_errors: list[float | None] = []
    cached: list[tuple[float | None, float]] = []
    uncached: list[tuple[float | None, float]] = []
    prefill_s: list[tuple[float | None, float]] = []
    prefill_done: list[float | None] = []
    decode_tput: list[tuple[float | None, float]] = []
    finish_events: list[dict[str, Any]] = []
    reconstructed: list[dict[str, Any]] = []
    session: dict[str, Any] | None = None
    inflight: list[dict[str, Any]] = []
    prev_running = 0
    last_decode_ts: float | None = None
    newest = None

    def complete_prefill() -> None:
        nonlocal session, last_decode_ts, prev_running
        opened = _open_from_prefill(session)
        if opened:
            inflight.append(opened)
            if last_decode_ts is None and opened["end_ts"] is not None:
                last_decode_ts = opened["end_ts"]
            if prev_running < 1:
                prev_running = 1
        session = None

    def finish_completed(running: int) -> None:
        while inflight and len(inflight) > max(0, running):
            reconstructed.append(_emit_inflight(inflight.pop(0)))

    for line in text.splitlines():
        if _TP_OTHER.search(line):
            continue
        match = _LOG_TS.match(line.strip())
        ts = None
        if match:
            ts = _parse_log_ts(match.group(1))
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        if '"POST /' in line and not any(skip in line for skip in _LOG_SKIP):
            posts.append(ts)
            status_m = _HTTP_STATUS.search(line)
            if status_m and int(status_m.group(1)) >= 400:
                http_errors.append(ts)
        if "Finish:" in line or "meta_info" in line:
            ev = _finish_event(ts, line)
            if ev:
                finish_events.append(ev)
        vllm_done = _VLLM_FINISHED.search(line)
        if vllm_done:
            prompt_n = int(vllm_done.group(1))
            gen_n = int(vllm_done.group(2))
            finish_events.append(
                {
                    "ts": int((ts or time.time()) * 1000),
                    "input_len": prompt_n,
                    "output_len": gen_n,
                    "cache_len": None,
                    "uncached_len": prompt_n,
                    "ttft_s": None,
                    "queue_s": None,
                    "prefill_s": None,
                    "tpot_s": None,
                    "e2e_s": None,
                    "decode_tps": None,
                    "spec_accept_len": None,
                    "spec_accept_rate": None,
                    "batch_size": 1,
                }
            )
        if "Prefill batch" in line:
            cached_n = _re_f(_PREFILL_CACHED, line)
            new_n = _re_f(_PREFILL_NEW, line)
            tput_n = _re_f(_PREFILL_TPUT, line)
            pending_n = _re_f(_PREFILL_PENDING, line)
            seq_n = _re_f(_PREFILL_SEQ, line) or 1
            healthish = new_n == 1 and (cached_n or 0) == 0 and (tput_n is None or tput_n < 1)
            if healthish:
                continue
            if cached_n is not None:
                cached.append((ts, cached_n))
            if new_n is not None:
                uncached.append((ts, new_n))
            if new_n is not None and tput_n is not None and tput_n > 1:
                prefill_s.append((ts, new_n / tput_n))
            if pending_n == 0:
                prefill_done.append(ts)
            jumped = (
                session is not None
                and pending_n is not None
                and session.get("pending") is not None
                and pending_n > float(session["pending"]) + max(new_n or 0, 4096)
            )
            if session is not None and (session.get("pending") == 0 or jumped):
                complete_prefill()
            if session is None:
                session = {
                    "start_ts": ts,
                    "end_ts": ts,
                    "uncached": 0.0,
                    "cache": float(cached_n or 0),
                    "prefill_s": 0.0,
                    "chunks": 0,
                    "batch": seq_n,
                    "pending": pending_n,
                }
            session["end_ts"] = ts
            session["uncached"] += float(new_n or 0)
            session["batch"] = max(float(session["batch"]), seq_n)
            session["chunks"] += 1
            session["pending"] = pending_n
            if new_n is not None and tput_n is not None and tput_n > 1:
                session["prefill_s"] += new_n / tput_n
            if session.get("pending") == 0:
                complete_prefill()
            continue
        if "Decode batch" in line:
            tput_n = _re_f(_DECODE_TPUT, line)
            running_n = _re_f(_RUNNING_REQ, line)
            accept_n = _re_f(_ACCEPT_LEN, line)
            rate_n = _re_f(_ACCEPT_RATE, line)
            if tput_n is not None:
                decode_tput.append((ts, tput_n))
            running = int(running_n) if running_n is not None else prev_running
            if (
                inflight
                and last_decode_ts is not None
                and ts is not None
                and prev_running > 0
                and tput_n
                and tput_n > 0
            ):
                dt = ts - last_decode_ts
                if 0 < dt < 30:
                    per = (tput_n * dt) / prev_running
                    for req in inflight:
                        req["out"] += per
                        req["tputs"].append(tput_n)
                        if accept_n is not None:
                            req["accepts"].append(accept_n)
                        if rate_n is not None:
                            req["rates"].append(rate_n)
                        req["last_ts"] = ts
            if ts is not None:
                last_decode_ts = ts
            prev_running = running
            finish_completed(running)
    if session is not None:
        complete_prefill()
    now = time.time()
    cutoff = now - since_s - 2
    until: float | None = None
    if newest is not None and abs(newest - now) > 3600:
        # The log clock does not map onto ours, so start/end cannot be
        # applied either; fall back to the newest line.
        cutoff = newest - since_s
    elif start is not None and end is not None:
        cutoff, until = start, end

    def in_window(ts: float | None) -> bool:
        if until is not None and ts is not None and ts >= until:
            return False
        return cutoff is None or ts is None or ts >= cutoff

    cutoff_ms = int(cutoff * 1000)
    until_ms = int(until * 1000) if until is not None else None
    chosen = finish_events if finish_events else reconstructed
    in_win = [
        ev
        for ev in chosen
        if ev.get("ts") is None
        or (ev["ts"] >= cutoff_ms and (until_ms is None or ev["ts"] < until_ms))
    ]
    requests = in_win[-MAX_REQUEST_EVENTS:]
    chunked_avg = mean(
        [r.get("prefill_s") for r in in_win if (r.get("chunks") or 1) > 1 and r.get("prefill_s")]
    )
    for row in requests:
        row.pop("chunks", None)

    return {
        "posts": float(sum(1 for ts in posts if in_window(ts))),
        # A trailing window counts errors over the whole `docker logs` output,
        # as it always has; a [start, end) read keeps them to its own lines.
        "http_errors": float(
            sum(1 for ts in http_errors if in_window(ts)) if until is not None else len(http_errors)
        ),
        "tiled": until is not None,
        "cached_tokens": sum(val for ts, val in cached if in_window(ts)),
        "new_tokens": sum(val for ts, val in uncached if in_window(ts)),
        "prefill_s": sum(val for ts, val in prefill_s if in_window(ts)),
        "prefill_done": float(sum(1 for ts in prefill_done if in_window(ts))),
        "decode_tps": mean([val for ts, val in decode_tput if in_window(ts)]),
        "chunked_prefill_avg_s": chunked_avg,
        "req_prefill_avg_s": mean([r.get("prefill_s") for r in in_win]),
        "req_tpot_avg_s": mean([r.get("tpot_s") for r in in_win]),
        "req_e2e_avg_s": mean([r.get("e2e_s") for r in in_win]),
        "req_ttft_avg_s": mean([r.get("ttft_s") for r in in_win]),
        "req_prompt_avg": mean([r.get("input_len") for r in in_win]),
        "req_uncached_avg": mean([r.get("uncached_len") for r in in_win]),
        "req_gen_avg": mean([r.get("output_len") for r in in_win]),
        "req_spec_len": mean([r.get("spec_accept_len") for r in in_win]),
        "req_spec_rate": mean([r.get("spec_accept_rate") for r in in_win]),
        "requests": requests,
    }


_no_logs_noted: set[tuple[int, str]] = set()


def note_no_engine_logs(port: int, why: str) -> None:
    """Say once per port and reason that an engine's container logs cannot be read.

    Everything else is still reported, so without this the only sign is a
    page whose recent requests stay empty.
    """
    if (port, why) in _no_logs_noted:
        return
    _no_logs_noted.add((port, why))
    print(
        f"engine logs for port {port}: {why}; its request events (the page's recent "
        "requests) are not reported",
        file=sys.stderr,
        flush=True,
    )


def summarize_engine_logs(
    port: int | None, since_s: float, start: float | None = None, end: float | None = None
) -> dict[str, Any] | None:
    """Request shapes and rates from the engine's container logs over the last *since_s* seconds, or None."""
    if not port:
        return None
    cmd = docker_cmd()
    if not cmd:
        note_no_engine_logs(
            port,
            "this account cannot run docker (tried `docker` and `sudo -n docker`) -- "
            "add it to the docker group and restart the reporter",
        )
        return None
    name = container_for_port(port)
    if not name:
        note_no_engine_logs(port, "no running container publishes that port")
        return None
    seconds = max(90, int(since_s) + 20)
    try:
        result = subprocess.run(
            [*cmd, "logs", "--since", f"{seconds}s", name],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        note_no_engine_logs(port, f"`docker logs {name}` failed: {exc}")
        return None
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or [f"exit {result.returncode}"]
        note_no_engine_logs(port, f"`docker logs {name}` failed: {tail[0]}")
        return None
    text = (result.stdout or "") + (result.stderr or "")
    return parse_engine_log_text(text, since_s, start, end)


def read_log_window(
    port: int | None, hold: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, float, dict[str, Any] | None]:
    """Engine-log stats since the previous read, for a target with no request counter.

    Returns (stats, seconds covered, new hold), or (None, 0.0, None) when the
    logs cannot be read. The window runs from where the previous read (the
    hold) stopped to two seconds ago: lines stamped with the current second
    or the one before may still be arriving, so they are left for the next
    read. Rates divided by this span can then be held until the next read
    and still integrate to the requests actually logged. A first read, or
    one after a gap, covers the last REQUEST_FLUSH_S seconds.
    """
    now = time.time()
    end = float(math.floor(now) - 1)
    start = hold["end"] if hold is not None and hold["end"] < end else end - REQUEST_FLUSH_S
    stats = summarize_engine_logs(port, now - start, start, end)
    if stats is None:
        return None, 0.0, None
    window_s = end - start if stats["tiled"] else now - start
    return stats, window_s, {"end": end, "at": now, "values": {}}


GPU_QUERY = ",".join(
    [
        "index",
        "uuid",
        "name",
        "driver_version",
        "memory.total",
        "memory.used",
        "utilization.gpu",
        "utilization.memory",
        "temperature.gpu",
        "power.draw",
        "power.limit",
        "fan.speed",
        "clocks.sm",
        "clocks.mem",
        "ecc.errors.uncorrected.volatile.total",
        "pstate",
    ]
)


def num(value: str) -> float | None:
    """An ``nvidia-smi`` field as a number; None for an empty or ``N/A`` one."""
    value = value.strip()
    if value in {"", "[N/A]", "N/A"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def mean(values: list[float | None]) -> float | None:
    """The mean of the numbers in *values*, or None when there are none."""
    xs = [v for v in values if isinstance(v, (int, float))]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 4)


def mx(values: list[float | None]) -> float | None:
    """The largest number in *values*, or None when there are none."""
    xs = [v for v in values if isinstance(v, (int, float))]
    return round(max(xs), 4) if xs else None


def run_nvidia_smi() -> list[dict[str, Any]]:
    """Every GPU, as ``nvidia-smi`` reports it."""
    out = subprocess.check_output(
        ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
        text=True,
    )
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 16:
            continue
        gpus.append(
            {
                "index": int(float(parts[0])),
                "uuid": parts[1],
                "name": parts[2],
                "driver": parts[3],
                "memory_total_mb": num(parts[4]),
                "memory_used_mb": num(parts[5]),
                "utilization_pct": num(parts[6]),
                "memory_util_pct": num(parts[7]),
                "temperature_c": num(parts[8]),
                "power_w": num(parts[9]),
                "power_limit_w": num(parts[10]),
                "fan_speed_pct": num(parts[11]),
                "clocks_sm_mhz": num(parts[12]),
                "clocks_mem_mhz": num(parts[13]),
                "ecc_errors": num(parts[14]),
                "pstate": parts[15],
            }
        )
    return gpus


_prev_cpu: tuple[int, int] | None = None


def read_host() -> dict[str, Any]:
    """This host's CPU utilization, load, memory and process counts, from ``/proc``."""
    global _prev_cpu
    cpu_count = 0
    total = idle = 0
    with open("/proc/stat", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("cpu ") and line[3] == " ":
                parts = [int(x) for x in line.split()[1:8]]
                idle = parts[3] + parts[4]
                total = sum(parts)
            elif line.startswith("cpu") and len(line) > 3 and line[3].isdigit():
                cpu_count += 1
    util = None
    if _prev_cpu and total > _prev_cpu[0]:
        dt, di = total - _prev_cpu[0], idle - _prev_cpu[1]
        util = round(100.0 * (1.0 - di / dt), 2)
    _prev_cpu = (total, idle)

    load1 = load5 = load15 = None
    procs_running = procs_total = None
    with open("/proc/loadavg", encoding="utf-8") as fh:
        bits = fh.read().split()
        load1, load5, load15 = float(bits[0]), float(bits[1]), float(bits[2])
        run, tot = bits[3].split("/")
        procs_running, procs_total = int(run), int(tot)

    mem: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, rest = line.split(":", 1)
            mem[key] = int(rest.strip().split()[0])
    total_mb = mem.get("MemTotal", 0) // 1024
    avail_mb = mem.get("MemAvailable", 0) // 1024
    swap_total = mem.get("SwapTotal", 0) // 1024
    swap_free = mem.get("SwapFree", 0) // 1024
    return {
        "cpu_util_pct": util,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "mem_used_mb": total_mb - avail_mb,
        "mem_total_mb": total_mb,
        "mem_available_mb": avail_mb,
        "swap_used_mb": swap_total - swap_free,
        "procs_running": procs_running,
        "procs_total": procs_total,
        "cpu_count": cpu_count or 1,
    }


def fetch(url: str) -> str:
    """The body at *url*, within five seconds."""
    req = urllib.request.Request(url, headers={"User-Agent": "openinfra-reporter/1.0"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read().decode("utf-8", "replace")


def parse_prom_samples(body: str) -> list[tuple[str, dict[str, str], float]]:
    """Every sample in Prometheus text, as (name, labels, value)."""
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, rest = ([*line.split("{", 1), ""])[:2]
        if "{" in line:
            labels_raw, _, value_raw = rest.rpartition("}")
            labels: dict[str, str] = {}
            for part in labels_raw.split(","):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                labels[k.strip()] = v.strip().strip('"')
            try:
                samples.append((name, labels, float(value_raw.strip().split()[-1])))
            except ValueError:
                continue
        else:
            bits = line.split()
            if len(bits) < 2:
                continue
            try:
                samples.append((bits[0], {}, float(bits[-1])))
            except ValueError:
                continue
    return samples


def prom_info_label(
    samples: list[tuple[str, dict[str, str], float]], name: str, label: str
) -> float | None:
    """Read a numeric LABEL off an info-style metric.

    vLLM publishes engine configuration as labels on `vllm:cache_config_info`
    rather than as series -- the KV pool size, for instance, is
    kv_cache_size_tokens="2456957" with a sample value of 1.0. sglang exposes
    the same quantity as a real gauge, so nothing upstream needed this.
    Labels that are absent, empty or the literal "None" (which vLLM emits for
    an unset config value) yield None rather than 0.
    """
    for n, labels, _v in samples:
        if n != name:
            continue
        raw = labels.get(label)
        if raw in (None, "", "None"):
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def prom_gauge(samples: list[tuple[str, dict[str, str], float]], name: str) -> float | None:
    """The largest value among the series *name*, or None."""
    vals = [v for n, _l, v in samples if n == name]
    return max(vals) if vals else None


def prom_gauge_any(samples: list[tuple[str, dict[str, str], float]], *names: str) -> float | None:
    """The first of *names* that has a series, read as :func:`prom_gauge` reads it."""
    for name in names:
        val = prom_gauge(samples, name)
        if val is not None:
            return val
    return None


def prom_sum_any(samples: list[tuple[str, dict[str, str], float]], *names: str) -> float | None:
    """The sum over the series of the first of *names* that has any, or None."""
    for name in names:
        vals = [v for n, _l, v in samples if n == name]
        if vals:
            return sum(vals)
    return None


def prom_sum_label(
    samples: list[tuple[str, dict[str, str], float]], name: str, key: str, value: str
) -> float | None:
    """The sum over the series *name* whose label *key* is *value*, or None."""
    vals = [v for n, labels, v in samples if n == name and labels.get(key) == value]
    return sum(vals) if vals else None


def _sum_http_status(
    samples: list[tuple[str, dict[str, str], float]], buckets: tuple[str, ...]
) -> float | None:
    """Total http_requests_total across the given status buckets.

    The FastAPI instrumentator labels status as "2xx"/"4xx"/"5xx", not as a
    numeric code, so this sums whole buckets rather than filtering a range.
    Returns None when the metric is absent entirely -- an engine that does not
    export it must leave the column NULL rather than claim zero errors.
    """
    parts = [prom_sum_label(samples, "http_requests_total", "status", b) for b in buckets]
    present = [p for p in parts if p is not None]
    if not present:
        # Distinguish "no error buckets yet" from "no such metric". A server
        # that has served only 2xx has no 4xx/5xx series at all, and reporting
        # NULL there would blank a panel that should read zero.
        if any(n == "http_requests_total" for n, _l, _v in samples):
            return 0.0
        return None
    return sum(present)


_PROM_RANK_LABELS = {"tp_rank", "pp_rank", "moe_ep_rank", "dp_rank"}


def prom_grouped_sum(
    samples: list[tuple[str, dict[str, str], float]],
    name: str,
    extra: dict[str, str] | None = None,
) -> float | None:
    """The sum over the label sets of *name*, each the mean of its parallel ranks' replicas."""
    groups: dict[tuple[tuple[str, str], ...], list[float]] = defaultdict(list)
    for n, labels, v in samples:
        if n != name:
            continue
        if extra and any(labels.get(k) != val for k, val in extra.items()):
            continue
        key = tuple(
            sorted(
                (k, val) for k, val in labels.items() if k not in _PROM_RANK_LABELS and k != "le"
            )
        )
        groups[key].append(v)
    if not groups:
        return None
    return sum(sum(vals) / len(vals) for vals in groups.values())


def prom_hist_parts(
    samples: list[tuple[str, dict[str, str], float]],
    base: str,
    extra: dict[str, str] | None = None,
) -> tuple[float | None, float | None, float | None]:
    """A histogram's sum, count and mean, with rank replicas averaged."""
    total = prom_grouped_sum(samples, f"{base}_sum", extra)
    count = prom_grouped_sum(samples, f"{base}_count", extra)
    avg = None
    if total is not None and count is not None and count > 0:
        avg = round(total / count, 6)
    return total, count, avg


def parse_vllm_prom(body: str) -> dict[str, Any]:
    """Engine metrics from vLLM's ``/metrics`` text; empty without ``vllm:`` series."""
    samples = parse_prom_samples(body)
    if not any(n.startswith("vllm:") for n, _l, _v in samples):
        return {}
    model = None
    for _n, labels, _v in samples:
        if labels.get("model_name"):
            model = labels["model_name"]
            break
    hits = prom_sum_any(samples, "vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits")
    queries = prom_sum_any(samples, "vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries")
    token_usage = prom_gauge_any(samples, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")
    requests_total = prom_sum_any(
        samples, "vllm:request_success_total", "vllm:e2e_request_latency_seconds_count"
    )
    cached_tokens = prom_sum_any(samples, "vllm:prompt_tokens_cached_total")
    if cached_tokens is None:
        cached_tokens = prom_sum_label(
            samples, "vllm:prompt_tokens_by_source_total", "source", "local_cache_hit"
        )
    # Counters are summed over their series, as cached_tokens is: a
    # data-parallel server has one per engine, and the uncached rate takes the
    # cached counter from the prompt one.
    generated = prom_sum_any(samples, "vllm:generation_tokens_total")
    # The KV pool size is a LABEL on cache_config_info, not a series.
    max_total = prom_info_label(samples, "vllm:cache_config_info", "kv_cache_size_tokens")
    # token_usage is a 0-1 fraction; sglang reports the absolute figure too.
    used_tokens = (
        round(token_usage * max_total)
        if isinstance(token_usage, (int, float)) and isinstance(max_total, (int, float))
        else None
    )
    kv_bytes = prom_info_label(samples, "vllm:cache_config_info", "kv_cache_memory_bytes")
    kv_cache_gb = round(kv_bytes / 1024**3, 2) if kv_bytes else None

    out: dict[str, Any] = {
        "engine": "vllm",
        "model_name": model,
        "num_running_reqs": prom_gauge(samples, "vllm:num_requests_running"),
        "num_queue_reqs": prom_gauge(samples, "vllm:num_requests_waiting"),
        "token_usage": token_usage,
        "prompt_tokens_total": prom_sum_any(samples, "vllm:prompt_tokens_total"),
        "generation_tokens_total": generated,
        "requests_total": requests_total,
        "cached_tokens_total": cached_tokens,
        # vLLM counts prompt and generated tokens as each engine step runs
        # them, which is what the step counters hold (see snapshot_sglang()).
        "step_generated_total": generated,
        "step_computed_total": prom_sum_label(
            samples, "vllm:prompt_tokens_by_source_total", "source", "local_compute"
        ),
        "step_cached_total": cached_tokens,
        "gen_throughput": prom_gauge_any(
            samples,
            "vllm:avg_generation_throughput_toks_per_s",
            "vllm:generation_tokens_per_second",
        ),
        # No prefill_tps here: the only gauge for it, V0's
        # vllm:avg_prompt_throughput_toks_per_s (gone since v0.7, never in V1),
        # counted cached prompt tokens too, and prefill_tps is the uncached
        # rate. snapshot_sglang() differences the prompt counters instead.
        # No cache_hit_rate: hits over queries since the engine started is no
        # sample's rate. snapshot_sglang() takes it over each window, and
        # leaves a window with no prompts blank.
        "prefix_cache_hits": hits,
        "prefix_cache_queries": queries,
        # Fields below were sglang-only, leaving the matching columns NULL for
        # every vLLM replica. vLLM does expose the underlying quantities, just
        # under different names -- or, for the KV pool, as a label.
        "max_total_num_tokens": max_total,
        "num_used_tokens": used_tokens,
        "kv_cache_gb": kv_cache_gb,
        # A retraction in sglang is a preemption in vLLM: a running request
        # pushed back to the queue because the KV pool ran short. Counter here,
        # differenced into a per-window count in snapshot_sglang().
        "retracts_total": prom_sum_any(samples, "vllm:num_preemptions_total"),
        # Only present when speculative decoding is on; absent -> stays NULL.
        "spec_accepted_total": prom_sum_any(samples, "vllm:spec_decode_num_accepted_tokens_total"),
        "spec_draft_tokens_total": prom_sum_any(samples, "vllm:spec_decode_num_draft_tokens_total"),
        "spec_drafts_total": prom_sum_any(samples, "vllm:spec_decode_num_drafts_total"),
        # HTTP errors. Not a vllm: series at all -- it comes from the FastAPI
        # instrumentator on the API server, so it counts what the ENGINE never
        # sees: a malformed body, an unknown model, a rejected API key. That is
        # precisely the half a vllm:request_success_total{finished_reason} view
        # misses, which is why this is worth having and why the two disagree.
        # Summed over 4xx and 5xx because the label is bucketed, not numeric.
        "http_error_total": _sum_http_status(samples, ("4xx", "5xx")),
        # Cache SOURCE, which vLLM breaks out and the dashboard has columns for.
        # local_cache_hit is the on-GPU prefix cache; external_kv_transfer is a
        # KV connector pulling blocks from outside this engine, which is the
        # nearest thing vLLM has to sglang's host tier. It reads 0 here rather
        # than absent, so the column will be an honest zero, not a gap.
        "device_cached_tokens_total": prom_sum_label(
            samples, "vllm:prompt_tokens_by_source_total", "source", "local_cache_hit"
        ),
        "host_cached_tokens_total": prom_sum_label(
            samples, "vllm:prompt_tokens_by_source_total", "source", "external_kv_transfer"
        ),
        **_latency_fields(
            samples,
            ttft="vllm:time_to_first_token_seconds",
            queue="vllm:request_queue_time_seconds",
            prefill="vllm:request_prefill_time_seconds",
        ),
    }
    _put_hist(out, samples, "vllm:e2e_request_latency_seconds", "e2e", "e2e_latency_avg_s")
    _put_hist(out, samples, "vllm:inter_token_latency_seconds", "itl", "tpot_avg_s")
    if "itl_sum" not in out:
        _put_hist(out, samples, "vllm:request_time_per_output_token_seconds", "itl", "tpot_avg_s")
    _put_hist(out, samples, "vllm:request_prompt_tokens", "prompt_hist", "prompt_tokens_avg")
    _put_hist(out, samples, "vllm:request_generation_tokens", "gen_hist", "generation_tokens_avg")
    # Prompt tokens the engine actually had to compute, straight from vLLM
    # instead of inferred. snapshot_sglang() already windows any *_sum/_count
    # pair named here (the "uncached_hist" entry in its histogram table), and
    # its counter-subtraction fallback only runs when this is absent -- so
    # publishing the histogram silently retires the fragile path on any engine
    # that has it, and leaves the fallback in place for one that does not.
    _put_hist(
        out,
        samples,
        "vllm:request_prefill_kv_computed_tokens",
        "uncached_hist",
        "uncached_prompt_tokens_avg",
    )
    # Tokens per engine step. This is NOT sglang's num_active_tokens, which is
    # an instantaneous count of tokens in the running batch; it is the mean
    # width of a scheduler iteration over the window, which is the same
    # quantity in spirit (how much work a step carries) and differs whenever
    # the batch is changing size. Named to the existing column because that is
    # what the column means, with the difference recorded here rather than
    # silently.
    _put_hist(out, samples, "vllm:iteration_tokens_total", "iter_hist", "num_active_tokens")
    return {k: v for k, v in out.items() if v is not None}


def _put_hist(
    out: dict[str, Any],
    samples: list[tuple[str, dict[str, str], float]],
    base: str,
    prefix: str,
    avg_key: str,
    extra: dict[str, str] | None = None,
) -> None:
    total, count, avg = prom_hist_parts(samples, base, extra)
    if total is not None:
        out[f"{prefix}_sum"] = total
    if count is not None:
        out[f"{prefix}_count"] = count
    if avg is not None:
        out[avg_key] = avg


def _latency_fields(
    samples: list[tuple[str, dict[str, str], float]],
    ttft: str,
    queue: str,
    prefill: str,
    prefill_extra: dict[str, str] | None = None,
) -> dict[str, float]:
    out: dict[str, Any] = {}
    _put_hist(out, samples, ttft, "ttft", "ttft_avg_s")
    _put_hist(out, samples, queue, "queue", "queue_avg_s")
    _put_hist(out, samples, prefill, "prefill", "prefill_avg_s", prefill_extra)
    return out


def prom_status_sum(
    samples: list[tuple[str, dict[str, str], float]],
    name: str,
    prefixes: tuple[str, ...],
) -> float | None:
    """The responses counted by *name* whose status code starts with one of *prefixes*."""
    groups: dict[tuple[tuple[str, str], ...], list[float]] = defaultdict(list)
    for n, labels, v in samples:
        if n != name:
            continue
        code = str(labels.get("status_code", ""))
        if not any(code.startswith(p) for p in prefixes):
            continue
        key = tuple(sorted((k, val) for k, val in labels.items() if k not in _PROM_RANK_LABELS))
        groups[key].append(v)
    if not groups:
        # A status code gets its series on its first response, so a server
        # that has answered no 4xx/5xx since it started has no such series.
        # That is zero errors, not a missing metric, as _sum_http_status has
        # it for vLLM. None here left http_error_per_min to the once-a-minute
        # log read, NULL on the other ~11 flushes, from every restart until
        # the replica's first error.
        if any(n == name and "status_code" in labels for n, labels, _v in samples):
            return 0.0
        return None
    return sum(sum(vals) / len(vals) for vals in groups.values())


def utilization_pct(ratio: Any) -> float | None:
    """The percentage ``sglang_util_pct`` holds, from SGLang's ``utilization`` ratio.

    SGLang reports the larger of its running requests over the most it may run
    within its latency target and its KV fill over 90 %; -1 on the prefill
    side of a disaggregated pair; and 0 while it has no latency target, which
    nothing in 0.5.20 to 0.5.22 sets. Neither -1 nor 0 is a reading, so both
    are left out, as vLLM, which has no such figure, leaves it out.
    """
    if not isinstance(ratio, (int, float)) or ratio <= 0:
        return None
    return round(float(ratio) * 100.0, 2)


def _sglang_step_counters(samples: list[tuple[str, dict[str, str], float]]) -> dict[str, float]:
    """The token counters SGLang moves as each step runs, under the names vLLM's fill.

    SGLang's prompt, generation, cached and request counters move only when a
    request finishes, so a rate taken from them puts a request's whole prompt
    and generation into the second it ends, where vLLM's counters count them
    as each engine step runs. These count them as SGLang's steps run:
    ``prefill_effective_tokens_total`` (0.5.20 on) counts a retracted request's
    prompt once, as vLLM does, and ``realtime_tokens_total`` (older releases)
    counts it again on each re-prefill. A mode SGLang has not counted yet has
    no series: ``prefill_cache`` before the first cache hit, for instance.
    """
    realtime = "sglang:realtime_tokens_total"
    effective = "sglang:prefill_effective_tokens_total"
    out: dict[str, float | None] = {
        "step_generated_total": prom_grouped_sum(samples, realtime, {"mode": "decode"})
    }
    computed = prom_grouped_sum(samples, effective, {"mode": "input"})
    if computed is not None:
        device, host, storage = (
            prom_grouped_sum(samples, effective, {"mode": mode})
            for mode in ("device_hit", "host_hit", "storage_hit")
        )
        out["step_device_cached_total"] = device
        out["step_host_cached_total"] = host
        cached = sum(v for v in (device, host, storage) if v is not None)
    else:
        computed = prom_grouped_sum(samples, realtime, {"mode": "prefill_compute"})
        cached = prom_grouped_sum(samples, realtime, {"mode": "prefill_cache"})
        if cached is None and computed is not None:
            cached = 0.0
    out["step_computed_total"] = computed
    out["step_cached_total"] = cached if computed is not None else None
    return {k: v for k, v in out.items() if v is not None}


def parse_sglang_prom(body: str) -> dict[str, Any]:
    """Engine metrics from SGLang's ``/metrics`` text; empty without ``sglang:`` series."""
    samples = parse_prom_samples(body)
    if not any(n.startswith("sglang:") for n, _l, _v in samples):
        return {}
    model = None
    for _n, labels, _v in samples:
        if labels.get("model_name"):
            model = labels["model_name"]
            break
    cached = prom_grouped_sum(samples, "sglang:cached_tokens_total")
    requests_total = prom_grouped_sum(samples, "sglang:num_requests_total")
    prompt_tokens = prom_grouped_sum(samples, "sglang:prompt_tokens_total")
    gen_tokens = prom_grouped_sum(samples, "sglang:generation_tokens_total")
    # SGLang reports 0 for both while speculative decoding is off, where vLLM
    # has no series at all; a running speculator accepts at least one token a step.
    spec_length = prom_gauge(samples, "sglang:spec_accept_length")
    speculating = isinstance(spec_length, (int, float)) and spec_length > 0
    out: dict[str, Any] = {
        "engine": "sglang",
        "model_name": model,
        "num_running_reqs": prom_gauge(samples, "sglang:num_running_reqs"),
        "num_queue_reqs": prom_gauge(samples, "sglang:num_queue_reqs"),
        "num_used_tokens": prom_gauge(samples, "sglang:num_used_tokens"),
        "token_usage": prom_gauge(samples, "sglang:token_usage"),
        "gen_throughput": prom_gauge(samples, "sglang:gen_throughput"),
        "max_total_num_tokens": prom_gauge(samples, "sglang:max_total_num_tokens"),
        "sglang_util_pct": utilization_pct(prom_gauge(samples, "sglang:utilization")),
        "spec_accept_length": spec_length if speculating else None,
        "spec_accept_rate": prom_gauge(samples, "sglang:spec_accept_rate") if speculating else None,
        "hicache_host_used": prom_gauge(samples, "sglang:hicache_host_used_tokens"),
        "hicache_host_total": prom_gauge(samples, "sglang:hicache_host_total_tokens"),
        "retracts": prom_gauge(samples, "sglang:num_retracted_reqs"),
        **_latency_fields(
            samples,
            ttft="sglang:time_to_first_token_seconds",
            queue="sglang:queue_time_seconds",
            prefill="sglang:per_stage_req_latency_seconds",
            prefill_extra={"stage": "prefill_forward"},
        ),
    }
    _put_hist(out, samples, "sglang:e2e_request_latency_seconds", "e2e", "e2e_latency_avg_s")
    _put_hist(out, samples, "sglang:inter_token_latency_seconds", "itl", "tpot_avg_s")
    if "itl_sum" not in out:
        _put_hist(out, samples, "sglang:time_per_output_token_seconds", "itl", "tpot_avg_s")
    _put_hist(
        out,
        samples,
        "sglang:per_stage_req_latency_seconds",
        "chunked",
        "chunked_prefill_avg_s",
        {"stage": "chunked_prefill"},
    )
    _put_hist(out, samples, "sglang:prompt_tokens_histogram", "prompt_hist", "prompt_tokens_avg")
    _put_hist(
        out,
        samples,
        "sglang:uncached_prompt_tokens_histogram",
        "uncached_hist",
        "uncached_prompt_tokens_avg",
    )
    _put_hist(
        out, samples, "sglang:generation_tokens_histogram", "gen_hist", "generation_tokens_avg"
    )
    if cached is not None:
        out["cached_tokens_total"] = cached
    if requests_total is not None:
        out["requests_total"] = requests_total
    if prompt_tokens is not None:
        out["prompt_tokens_total"] = prompt_tokens
    if gen_tokens is not None:
        out["generation_tokens_total"] = gen_tokens
    device_cached = prom_grouped_sum(
        samples, "sglang:cached_tokens_total", {"cache_source": "device"}
    )
    host_cached = prom_grouped_sum(samples, "sglang:cached_tokens_total", {"cache_source": "host"})
    if device_cached is not None:
        out["device_cached_tokens_total"] = device_cached
    if host_cached is not None:
        out["host_cached_tokens_total"] = host_cached
    out.update(_sglang_step_counters(samples))
    loadback = prom_grouped_sum(samples, "sglang:load_back_tokens_total")
    evict = prom_grouped_sum(samples, "sglang:evicted_tokens_total")
    backup = prom_grouped_sum(samples, "sglang:hicache_backup_tokens_total")
    if loadback is not None:
        out["loadback_tokens_total"] = loadback
    if evict is not None:
        out["evict_tokens_total"] = evict
    if backup is not None:
        out["backup_tokens_total"] = backup
    http_err = prom_status_sum(samples, "sglang:http_responses_total", ("4", "5"))
    if http_err is not None:
        out["http_error_total"] = http_err
    retract_total = prom_grouped_sum(samples, "sglang:num_retracted_requests_total")
    if retract_total is not None:
        out["retracts_total"] = retract_total
    return {k: v for k, v in out.items() if v is not None}


def sibling_metrics_url(url: str) -> str | None:
    """The ``/metrics`` address beside an engine's ``/v1/loads``; None for a ``/metrics`` target."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/metrics"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}/metrics"


def on_demand_idle(target: dict[str, Any]) -> bool:
    """Whether an engine that did not answer was stopped on purpose, by the proxy that starts it.

    ``on_demand_url`` is that proxy's model listing (OpenAI's ``GET
    /v1/models``, which FreeInference's idle proxies answer without a key, each
    model with a ``status``), and ``on_demand_model``, else ``model_name``, the
    engine's id in it. The engine is idle while the listing has it and does not
    call it ``loaded``. A proxy that does not answer, or that calls the model
    loaded, says nothing of the kind, so the engine goes unreported and reads
    as offline, as an engine with no proxy does.
    """
    url = target.get("on_demand_url")
    model = target.get("on_demand_model") or target.get("model_name")
    if not url or not model:
        return False
    try:
        listing = json.loads(fetch(url))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    models = listing.get("data") if isinstance(listing, dict) else None
    for entry in models if isinstance(models, list) else ():
        if isinstance(entry, dict) and entry.get("id") == model:
            return entry.get("status") != "loaded"
    return False


_no_metrics_noted: set[str] = set()
#: Each repeating failure's last report and the reports held back since.
_error_log: dict[str, tuple[float, int]] = {}
#: Seconds between reports of one failure; main() sets it from --log-interval.
_error_interval = LOG_INTERVAL_S


def log_error(key: str, message: str) -> None:
    """Print *message* to stderr, at most once per log interval for each *key*.

    A failure that repeats on every flush would otherwise print every second;
    the next report says how many were held back.
    """
    now = time.monotonic()
    last, held = _error_log.get(key, (-math.inf, 0))
    if now - last < _error_interval:
        _error_log[key] = (last, held + 1)
        return
    more = f" ({held} more since the last report)" if held else ""
    print(f"{message}{more}", file=sys.stderr, flush=True)
    _error_log[key] = (now, 0)


def note_no_sibling_metrics(target: dict[str, Any], url: str, why: str) -> None:
    """Say once per target that its request rate has to come from the logs."""
    key = target.get("id") or target["url"]
    if key in _no_metrics_noted:
        return
    _no_metrics_noted.add(key)
    print(
        f"sglang {key}: no sglang: metrics at {url} ({why}); request rates fall back to "
        f"engine logs, read every {REQUEST_FLUSH_S:.0f}s -- start sglang with --enable-metrics",
        file=sys.stderr,
        flush=True,
    )


def parse_sglang_body(body: str) -> dict[str, Any]:
    """Engine metrics from one scrape.

    SGLang's ``/v1/loads`` JSON, or either engine's ``/metrics`` text.
    """
    text = body.strip()
    if not (text.startswith("{") or text.startswith("[")):
        vllm = parse_vllm_prom(text)
        if vllm:
            return {"metrics": vllm}
        sgl = parse_sglang_prom(text)
        if sgl:
            return {"metrics": sgl}
        return {"prometheus": body}
    data = json.loads(text)
    if isinstance(data, list):
        load = data[0] if data else {}
        model = None
    else:
        loads = data.get("loads") or []
        load = loads[0] if loads else data
        model = data.get("model")
    queues = load.get("queues") if isinstance(load.get("queues"), dict) else {}
    spec = load.get("speculative") if isinstance(load.get("speculative"), dict) else {}
    memory = load.get("memory") if isinstance(load.get("memory"), dict) else {}
    disagg = load.get("disaggregation") if isinstance(load.get("disaggregation"), dict) else {}
    spec_rate = spec.get("accept_rate")
    if spec_rate in (0, 0.0):
        spec_rate = None
    retracts = queues.get("retracted")
    if retracts is None:
        retracts = disagg.get("decode_retracted_queue_reqs")
    max_tokens = load.get("max_total_num_tokens", memory.get("token_capacity"))
    used = load.get("num_used_tokens", load.get("num_tokens"))
    usage = load.get("token_usage")
    if usage is None and used is not None and max_tokens:
        usage = round(used / max_tokens, 4)
    moments = load.get("decode_moments")
    sampled = moments[5] if isinstance(moments, list) and len(moments) >= 6 else None
    return {
        "metrics": {
            "engine": "sglang",
            "model_name": model or load.get("served_model_name"),
            "num_running_reqs": load.get("num_running_reqs", load.get("num_reqs")),
            "num_queue_reqs": load.get("num_waiting_reqs", queues.get("waiting")),
            "num_used_tokens": used,
            "num_active_tokens": load.get("num_active_tokens", load.get("num_total_tokens")),
            "num_waiting_uncached_tokens": load.get("num_waiting_uncached_tokens"),
            "max_total_num_tokens": max_tokens,
            "token_usage": usage,
            "prefill_tps": load.get("prefill_throughput", load.get("prefill_tps")),
            "gen_throughput": load.get("gen_throughput"),
            "cache_hit_rate": (
                None
                if load.get("cache_hit_rate", load.get("prefix_cache_hit_rate")) in (None, 0, 0.0)
                else load.get("cache_hit_rate", load.get("prefix_cache_hit_rate"))
            ),
            # Only the prompt tokens each prefill batch computed (sglang leaves
            # out steps of 2 s or more), so it is kept apart from
            # prompt_tokens_total, which the sibling /metrics fills with whole
            # prompts, cached tokens included.
            "uncached_tokens_total": load.get("total_prefill_uncached_tokens"),
            "generation_tokens_total": (
                sampled
                if sampled is not None
                else load.get("total_generated_tokens", load.get("generation_tokens"))
            ),
            # The tokens of the decode steps SGLang timed: each that followed
            # another decode step, which leaves out the first after a prefill.
            # A step count, unlike the sibling /metrics' generation counter,
            # which moves when a request ends; kept under a name of its own so
            # that a rate is never taken between the two.
            "loads_generated_total": sampled,
            "prefill_busy_us": load.get("total_prefill_busy_us"),
            "spec_accept_length": spec.get("accept_length"),
            "spec_accept_rate": spec_rate,
            "sglang_util_pct": utilization_pct(load.get("utilization")),
            "max_running_requests": load.get("max_running_requests"),
            "weight_gb": memory.get("weight_gb"),
            "kv_cache_gb": memory.get("kv_cache_gb"),
            "retracts": retracts,
        }
    }


#: The gateway's ingest route, appended to OPENINFRA_URL.
INGEST_PATH = "/internal/openinfra/ingest"
#: A User-Agent edges let through: some refuse library defaults such as
#: Python-urllib's. OPENINFRA_USER_AGENT overrides it.
USER_AGENT = os.environ.get("OPENINFRA_USER_AGENT") or "curl/8.5.0"


def ingest_url(dashboard: str) -> str:
    """The ingest endpoint under the gateway address *dashboard*."""
    return dashboard.rstrip("/") + INGEST_PATH


def post(dashboard: str, token: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Post one snapshot to the gateway; its answer (the rows it stored)."""
    req = urllib.request.Request(
        ingest_url(dashboard),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def default_sglang_targets() -> list[dict[str, str]]:
    """The engines to scrape without a configuration.

    ``SGLANG_METRICS_URL``, else a local SGLang.
    """
    env = os.environ.get("SGLANG_METRICS_URL")
    if env:
        return [
            {"url": url.strip(), "id": "", "name": "", "endpoint": ""}
            for url in env.split(",")
            if url.strip()
        ]
    return [{"url": "http://127.0.0.1:30000/metrics", "id": "", "name": "", "endpoint": ""}]


def parse_args() -> argparse.Namespace:
    """The command line, environment and node configuration, merged."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dashboard",
        default=os.environ.get("OPENINFRA_URL"),
        help="the gateway's address (OPENINFRA_URL); the snapshot goes to " + INGEST_PATH,
    )
    parser.add_argument(
        "--token", default=os.environ.get("OPENINFRA_TOKEN") or os.environ.get("INGEST_TOKEN")
    )
    parser.add_argument("--config", default=os.environ.get("OPENINFRA_NODE_CONFIG"))
    parser.add_argument(
        "--node-id", default=os.environ.get("OPENINFRA_NODE_ID") or os.uname().nodename
    )
    parser.add_argument(
        "--hostname", default=os.environ.get("OPENINFRA_HOSTNAME") or os.uname().nodename
    )
    parser.add_argument("--region", default=os.environ.get("OPENINFRA_REGION"))
    parser.add_argument("--cluster", default=os.environ.get("OPENINFRA_CLUSTER"))
    parser.add_argument("--sglang-url", action="append", default=[])
    parser.add_argument(
        "--flush-interval",
        type=float,
        default=float(os.environ.get("OPENINFRA_FLUSH_INTERVAL", "1")),
        help="seconds between snapshots (1); the gateway keeps one sample a second",
    )
    parser.add_argument(
        "--log-interval",
        type=float,
        default=float(os.environ.get("OPENINFRA_LOG_INTERVAL", str(LOG_INTERVAL_S))),
        help="seconds between logged flushes (60); 0 logs every one",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--skip-gpu", action="store_true")
    parser.add_argument("--skip-sglang", action="store_true")
    args = parser.parse_args()
    args.sglang_from_config: list[dict[str, Any]] = []
    # Distinguish "config omitted the key" from "config listed no engines".
    # An empty list is how a node says its last engine is gone.
    args.sglang_configured = False
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        for key in ("dashboard", "token", "node_id", "hostname", "region", "cluster"):
            if cfg.get(key):
                setattr(args, key, cfg[key])
        if cfg.get("flush_interval") is not None:
            args.flush_interval = float(cfg["flush_interval"])
        if cfg.get("log_interval") is not None:
            args.log_interval = float(cfg["log_interval"])
        if "sglang" in cfg:
            raw = cfg["sglang"] or []
            args.sglang_from_config = list(raw)
            args.sglang_configured = True
    if not args.dashboard:
        parser.error("set the gateway's address with --dashboard, OPENINFRA_URL or the config")
    if not args.flush_interval > 0:
        parser.error("the flush interval must be a positive number of seconds")
    return args


def sglang_targets(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The engines this host scrapes.

    The node configuration's, the ``--sglang-url`` flags', or the default.
    """
    if args.sglang_configured:
        return args.sglang_from_config
    if args.sglang_url:
        out = []
        for i, url in enumerate(args.sglang_url):
            host = urlparse(url).hostname or "sglang"
            out.append(
                {
                    "url": url,
                    "id": f"{args.node_id}:{host}:{urlparse(url).port or i}",
                    "name": f"sglang-{i}",
                    "endpoint": url.rsplit("/", 1)[0],
                }
            )
        return out
    return default_sglang_targets()


DCGM_FIELDS = {
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE": "tensor_active",
    "DCGM_FI_PROF_DRAM_ACTIVE": "dram_active",
    "DCGM_FI_PROF_PCIE_TX_BYTES": "pcie_tx_bps",
    "DCGM_FI_PROF_PCIE_RX_BYTES": "pcie_rx_bps",
    "DCGM_FI_PROF_NVLINK_TX_BYTES": "nvlink_tx_bps",
    "DCGM_FI_PROF_NVLINK_RX_BYTES": "nvlink_rx_bps",
}


def scrape_pcie_dmon() -> dict[int, dict[str, float]]:
    """Per-GPU PCIe throughput from nvidia-smi, for hosts with no DCGM.

    pcie_tx_bps / pcie_rx_bps normally come from DCGM_FI_PROF_PCIE_*_BYTES,
    which needs a running nv-hostengine. On the CS2680 Slurm nodes that is not
    available: nv-hostengine refuses to stay up as a non-root user, so those
    two columns were permanently NULL. `nvidia-smi dmon -s t` reports the same
    quantity unprivileged.

    dmon prints MB/s (decimal megabytes, per nvidia-smi's own documentation);
    DCGM reports bytes/s, so scale by 1e6 to keep one unit in the column.
    Returns {} on any failure -- this is strictly a fallback and must never
    take down a scrape.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "dmon", "-s", "t", "-c", "1"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    by_gpu: dict[int, dict[str, float]] = {}
    for line in out.splitlines():
        parsed = parse_dmon_line(line)
        if parsed:
            by_gpu[parsed[0]] = parsed[1]
    return by_gpu


def parse_dmon_line(line: str) -> tuple[int, dict[str, float]] | None:
    """One GPU's PCIe throughput from a line of ``nvidia-smi dmon -s t``, in bytes/s."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split()
    if len(parts) < 3:
        return None
    try:
        idx = int(parts[0])
        rx, tx = float(parts[1]), float(parts[2])
    except ValueError:
        # dmon prints "-" for a metric it cannot read on this card.
        return None
    return idx, {"pcie_rx_bps": rx * 1e6, "pcie_tx_bps": tx * 1e6}


class PcieMonitor:
    """``nvidia-smi dmon -s t`` running in the background: each GPU's latest PCIe reading.

    dmon samples over a second before it prints, so running it once per
    flush, as :func:`scrape_pcie_dmon` does, would take the whole of a
    one-second interval. One long-running dmon prints a line per GPU every
    second instead, read here as it comes. It starts on the first call to
    :meth:`latest`, and is started again 10 s after it exits; without
    ``nvidia-smi`` it gives up. It is stopped when the reporter exits, and
    should the reporter be killed instead, it ends at its next line, which
    no longer has anywhere to go.
    """

    #: A reading older than this is not reported.
    MAX_AGE_S = 5.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[int, tuple[float, dict[str, float]]] = {}
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None

    def latest(self) -> dict[int, dict[str, float]]:
        """The readings of the last few seconds, by GPU index; empty until dmon prints."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pcie-dmon", daemon=True)
            self._thread.start()
            atexit.register(self.stop)
        now = time.monotonic()
        with self._lock:
            return {
                idx: values
                for idx, (at, values) in self._latest.items()
                if now - at <= self.MAX_AGE_S
            }

    def stop(self) -> None:
        """End the running dmon, if any."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _run(self) -> None:
        while True:
            try:
                proc = subprocess.Popen(
                    ["nvidia-smi", "dmon", "-s", "t", "-d", "1"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError:
                return
            self._proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                parsed = parse_dmon_line(line)
                if parsed:
                    with self._lock:
                        self._latest[parsed[0]] = (time.monotonic(), parsed[1])
            proc.wait()
            time.sleep(10)


PCIE_MONITOR = PcieMonitor()


def scrape_dcgm(url: str = "http://127.0.0.1:9400/metrics") -> dict[int, dict[str, float]]:
    """Tensor, DRAM, PCIe and NVLink activity per GPU index, from DCGM's exporter."""
    body = fetch(url)
    by_gpu: dict[int, dict[str, float]] = defaultdict(dict)
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        key = DCGM_FIELDS.get(name.strip())
        if not key:
            continue
        labels, _, value_s = rest.rpartition("}")
        gpu_m = None
        for part in labels.split(","):
            if part.strip().startswith("gpu="):
                gpu_m = int(part.split("=", 1)[1].strip().strip('"'))
                break
        if gpu_m is None:
            continue
        try:
            val = float(value_s.strip().split()[0])
        except ValueError:
            continue
        by_gpu[gpu_m][key] = val
    return by_gpu


def scrape(args: argparse.Namespace, targets: list[dict[str, str]]) -> dict[str, Any]:
    """One reading of the host, its GPUs, DCGM and every engine, taken concurrently."""
    gpus: list[dict[str, Any]] = []
    sglang: dict[str, dict[str, Any]] = {}
    host: dict[str, Any] = {}
    dcgm: dict[int, dict[str, float]] = {}

    def gpu_job() -> None:
        nonlocal gpus
        if not args.skip_gpu:
            gpus = run_nvidia_smi()

    def host_job() -> None:
        nonlocal host
        host = read_host()

    def dcgm_job() -> None:
        nonlocal dcgm
        try:
            dcgm = scrape_dcgm()
        except (urllib.error.URLError, OSError, TimeoutError):
            dcgm = {}

    def sglang_job(target: dict[str, str]) -> None:
        try:
            body = fetch(target["url"])
            parsed = parse_sglang_body(body)
            metrics_url = sibling_metrics_url(target["url"])
            if metrics_url:
                try:
                    extra = parse_sglang_prom(fetch(metrics_url))
                    if extra:
                        keep = parsed.get("metrics") or {}
                        skip_if_present = {
                            "num_used_tokens",
                            "token_usage",
                            "cache_hit_rate",
                            "sglang_util_pct",
                        }
                        overlay = {
                            k: v
                            for k, v in extra.items()
                            if k not in skip_if_present or keep.get(k) is None
                        }
                        parsed["metrics"] = {**keep, **overlay}
                    else:
                        note_no_sibling_metrics(target, metrics_url, "no sglang: series")
                except urllib.error.HTTPError as exc:
                    note_no_sibling_metrics(target, metrics_url, f"HTTP {exc.code}")
                except (urllib.error.URLError, OSError, TimeoutError):
                    pass
            sglang[target["id"] or target["url"]] = {"target": target, **parsed}
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            name = target.get("id") or target.get("url")
            if on_demand_idle(target):
                sglang[target["id"] or target["url"]] = {"target": target, "idle": True}
                return
            log_error(f"sglang {name}", f"sglang scrape {name}: {exc}")

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(host_job), pool.submit(dcgm_job)]
        if not args.skip_gpu:
            futs.append(pool.submit(gpu_job))
        if not args.skip_sglang:
            futs.extend(pool.submit(sglang_job, target) for target in targets)
        for fut in futs:
            try:
                fut.result()
            except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
                log_error("scrape", f"scrape error: {exc}")
    if dcgm:
        for gpu in gpus:
            extra = dcgm.get(int(gpu["index"]))
            if extra:
                gpu.update(extra)
    # Only where DCGM did not supply them: DCGM is the better source when a
    # host engine is running, and a DCGM deployment must not be downgraded to
    # a once-per-flush nvidia-smi sample.
    if gpus and not any(g.get("pcie_tx_bps") is not None for g in gpus):
        pcie = scrape_pcie_dmon() if args.once else PCIE_MONITOR.latest()
        for gpu in gpus:
            extra = pcie.get(int(gpu["index"]))
            if extra:
                gpu.setdefault("pcie_rx_bps", extra["pcie_rx_bps"])
                gpu.setdefault("pcie_tx_bps", extra["pcie_tx_bps"])
    return {"gpus": gpus, "sglang": sglang, "cpu": host}


def _counter_rate(first: Any, last: Any, elapsed: float) -> float | None:
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return None
    if elapsed <= 0 or last < first:
        return None
    return round((last - first) / elapsed, 1)


def _counter_ratio(first_n: Any, last_n: Any, first_d: Any, last_d: Any) -> float | None:
    if not all(isinstance(v, (int, float)) for v in (first_n, last_n, first_d, last_d)):
        return None
    num = last_n - first_n
    den = last_d - first_d
    if den <= 0 or num < 0:
        return None
    return round(num / den, 4)


#: Each histogram's ``<prefix>_sum`` and ``_count``, and the average they give.
_HISTOGRAMS = (
    ("ttft", "ttft_avg_s"),
    ("queue", "queue_avg_s"),
    ("prefill", "prefill_avg_s"),
    ("chunked", "chunked_prefill_avg_s"),
    ("e2e", "e2e_latency_avg_s"),
    ("itl", "tpot_avg_s"),
    ("prompt_hist", "prompt_tokens_avg"),
    ("uncached_hist", "uncached_prompt_tokens_avg"),
    ("iter_hist", "num_active_tokens"),
    ("gen_hist", "generation_tokens_avg"),
)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _shared_rate(
    first: dict[str, Any], last: dict[str, Any], elapsed: float, *keys: str
) -> tuple[str | None, float | None]:
    """The first of *keys* the engine reports, and its rate over the window.

    A counter both scrapes hold comes first, so that no rate is taken between
    two different counters. The rate is None when the window gives none: the
    first scrape, or a counter that went back (a restart).
    """
    for key in keys:
        if _number(first.get(key)) and _number(last.get(key)):
            return key, _counter_rate(first[key], last[key], elapsed)
    for key in keys:
        if _number(last.get(key)):
            return key, None
    return None, None


def snapshot_sglang(
    last: dict[str, Any], prev: dict[str, Any] | None, elapsed: float
) -> dict[str, Any]:
    """An engine's metrics for one flush, with rates from its counters since the previous scrape.

    Every rate, ratio and average is the window's, the same on either engine:
    token rates from counters that move as each step runs (vLLM's own, or
    SGLang's ``realtime``/``prefill_effective`` ones, else SGLang's gauge),
    averages from each histogram's growth. What a window gives none of is left
    out, never filled with the average since the engine started, so an idle
    second is blank. ``measured`` in the result names the metrics the
    engine's own counters and histograms gave, which no log fallback replaces.
    """
    last_m = last.get("metrics") or {}
    first_m = (prev or {}).get("metrics") or last_m
    metrics = dict(last_m)
    measured: set[str] = set()
    # Half a second: a one-second flush may come a little early.
    can_rate = prev is not None and elapsed >= 0.5
    src_first = first_m if can_rate else last_m
    src_elapsed = elapsed if can_rate else 0.0
    for prefix, dest in _HISTOGRAMS:
        if not _number(last_m.get(f"{prefix}_count")):
            continue
        measured.add(dest)
        win = _counter_ratio(
            src_first.get(f"{prefix}_sum"),
            last_m.get(f"{prefix}_sum"),
            src_first.get(f"{prefix}_count"),
            last_m.get(f"{prefix}_count"),
        )
        metrics[dest] = round(win, 6) if win is not None else None
    if (
        "prefill_avg_s" not in measured
        and metrics.get("prefill_avg_s") is None
        and metrics.get("ttft_avg_s") is not None
        and metrics.get("queue_avg_s") is not None
    ):
        metrics["prefill_avg_s"] = round(
            max(0.0, float(metrics["ttft_avg_s"]) - float(metrics["queue_avg_s"])),
            6,
        )

    # Prompt tokens computed and read from the cache, as each step runs them
    # (_sglang_step_counters(); vLLM's prompt counters move that way too).
    # prefill_tps is the UNCACHED rate on every path, and the gateway sums it
    # as is; the hit rate is the cached share of the window's prompt tokens.
    # Only where the step counter gives the window a rate: one only the newer
    # scrape holds (after a missed sibling /metrics, or in a series' first
    # scrape) leaves the window to the counters both scrapes share.
    _, step_computed = _shared_rate(src_first, last_m, src_elapsed, "step_computed_total")
    if step_computed is not None:
        _, step_cached = _shared_rate(src_first, last_m, src_elapsed, "step_cached_total")
        metrics["prefill_tps"] = step_computed
        metrics["cached_prefill_tps"] = step_cached
        c0, c1 = src_first.get("step_cached_total"), last_m.get("step_cached_total")
        u0, u1 = src_first.get("step_computed_total"), last_m.get("step_computed_total")
        metrics["cache_hit_rate"] = (
            _counter_ratio(c0, c1, c0 + u0, c1 + u1)
            if all(_number(v) for v in (c0, c1, u0, u1))
            else None
        )
        measured.update(("prefill_tps", "cached_prefill_tps", "cache_hit_rate"))
    else:
        _legacy_prompt_rates(metrics, measured, src_first, last_m, src_elapsed, can_rate)

    # Generated tokens: a counter that moves every step (vLLM's, SGLang's
    # realtime one, or the steps /v1/loads timed). SGLang's generation counter
    # moves only when a request ends, so the window a request ended in took
    # its whole generation, which the windows before it, falling back to the
    # gauge, had already counted: the served totals counted it twice. The
    # gauge stands in only where no step counter gives the window a rate.
    inst_decode = last_m.get("gen_throughput")
    decode_key, decode = _shared_rate(
        src_first, last_m, src_elapsed, "step_generated_total", "loads_generated_total"
    )
    if decode_key is not None:
        metrics["gen_throughput"] = decode if decode is not None else inst_decode
        measured.add("gen_throughput")
    elif inst_decode is None:
        metrics["gen_throughput"] = _counter_rate(
            src_first.get("generation_tokens_total"),
            last_m.get("generation_tokens_total"),
            src_elapsed,
        )
    metrics.pop("cached_tokens_total", None)
    metrics.pop("prefix_cache_hits", None)
    metrics.pop("prefix_cache_queries", None)
    if _number(last_m.get("requests_total")):
        measured.add("req_per_min")
    first_req = src_first.get("requests_total")
    last_req = last_m.get("requests_total")
    if (
        can_rate
        and isinstance(first_req, (int, float))
        and isinstance(last_req, (int, float))
        and src_elapsed > 0
        and last_req >= first_req
    ):
        metrics["req_per_min"] = round((last_req - first_req) / src_elapsed * 60.0, 1)
    # SGLang before 0.5.20 counts cache tiers only as requests finish, so on
    # its realtime counters device and host add up to the cached rate over a
    # minute rather than in each second.
    for dest, keys in (
        ("device_cached_tps", ("step_device_cached_total", "device_cached_tokens_total")),
        ("host_cached_tps", ("step_host_cached_total", "host_cached_tokens_total")),
    ):
        metrics[dest] = _shared_rate(src_first, last_m, src_elapsed, *keys)[1]
    metrics["loadback_tps"] = _counter_rate(
        src_first.get("loadback_tokens_total"), last_m.get("loadback_tokens_total"), src_elapsed
    )
    metrics["evict_tps"] = _counter_rate(
        src_first.get("evict_tokens_total"), last_m.get("evict_tokens_total"), src_elapsed
    )
    metrics["backup_tps"] = _counter_rate(
        src_first.get("backup_tokens_total"), last_m.get("backup_tokens_total"), src_elapsed
    )
    _speculation(metrics, measured, src_first, last_m)
    if _number(last_m.get("retracts_total")):
        # Requests retracted (vLLM: preempted) in this window, from the
        # counter either engine keeps. SGLang's num_retracted_reqs gauge, the
        # fallback, counts those of its last report and holds that while idle.
        measured.add("retracts")
        r0, r1 = src_first.get("retracts_total"), last_m.get("retracts_total")
        metrics["retracts"] = round(r1 - r0) if can_rate and _number(r0) and r1 >= r0 else None
    if (
        "uncached_prompt_tokens_avg" not in measured
        and metrics.get("uncached_prompt_tokens_avg") is None
    ):
        p0, p1 = src_first.get("prompt_tokens_total"), last_m.get("prompt_tokens_total")
        c0, c1 = src_first.get("cached_tokens_total"), last_m.get("cached_tokens_total")
        q0, q1 = src_first.get("requests_total"), last_m.get("requests_total")
        if all(isinstance(v, (int, float)) for v in (p0, p1, c0, c1, q0, q1)):
            d_prompt, d_cached, d_req = p1 - p0, c1 - c0, q1 - q0
            # Prompt tokens the engine actually had to compute, per request.
            # vLLM reports cached_tokens only when the server was started with
            # --enable-prompt-tokens-details; without it d_cached stays 0 and
            # this would silently equal the full prompt length, so require a
            # real cache signal before publishing.
            #
            # d_prompt > 0 as well as d_req > 0, because the two counters move
            # at different moments: prompt_tokens climbs at prefill, the
            # request counter at completion, so a window can hold finishes
            # whose prefill landed in the previous one. Measured on the FP4
            # replica 2026-09-23: a window with d_req=8 and d_prompt=0
            # published 0.0, which reads as "nothing to compute, it was all
            # cached" and is the opposite of what happened. No prefill in the
            # window means there is no average to take, not an average of zero.
            # A genuinely fully-cached prefill still has d_prompt > 0 and
            # still publishes its 0.
            if (
                d_req > 0
                and d_prompt > 0
                and d_prompt >= d_cached >= 0
                and (c1 > 0 or d_cached > 0)
            ):
                metrics["uncached_prompt_tokens_avg"] = round((d_prompt - d_cached) / d_req, 1)
    err0, err1 = src_first.get("http_error_total"), last_m.get("http_error_total")
    if (
        can_rate
        and isinstance(err0, (int, float))
        and isinstance(err1, (int, float))
        and src_elapsed > 0
        and err1 >= err0
    ):
        metrics["http_error_per_min"] = round((err1 - err0) / src_elapsed * 60.0, 2)
    metrics.pop("requests_total", None)
    return {**last, "metrics": metrics, "measured": frozenset(measured)}


def _legacy_prompt_rates(
    metrics: dict[str, Any],
    measured: set[str],
    src_first: dict[str, Any],
    last_m: dict[str, Any],
    src_elapsed: float,
    can_rate: bool,
) -> None:
    """Prompt-token rates and the hit rate for an engine without step counters.

    An SGLang without ``realtime_tokens_total``, a vLLM without its by-source
    prompt counter, and ``/v1/loads`` without its sibling ``/metrics``.
    """
    prefill_from_tokens = _counter_rate(
        src_first.get("prompt_tokens_total"), last_m.get("prompt_tokens_total"), src_elapsed
    )
    cached_from_tokens = _counter_rate(
        src_first.get("cached_tokens_total"), last_m.get("cached_tokens_total"), src_elapsed
    )
    if cached_from_tokens is not None:
        metrics["cached_prefill_tps"] = cached_from_tokens
    # prompt_tokens_total counts whole prompts and cached_tokens_total their
    # cached part, bumped together (sglang at finish, vLLM at first token), so
    # the uncached rate is their difference. With no cached counter, a counter
    # of uncached tokens stands in: the uncached-prompt histogram (vLLM
    # 0.13-0.15 has no other), then /v1/loads' total_prefill_uncached_tokens
    # (sglang without --enable-metrics). Only an engine reporting no cached
    # tokens at all is left with the prompt rate: sglang before its first
    # cache hit, where the two agree, and vLLM <= 0.12.
    uncached_from_tokens = None
    if prefill_from_tokens is not None and cached_from_tokens is not None:
        uncached_from_tokens = round(max(0.0, prefill_from_tokens - cached_from_tokens), 1)
    else:
        uncached_hist = _counter_rate(
            src_first.get("uncached_hist_sum"), last_m.get("uncached_hist_sum"), src_elapsed
        )
        prompt_hist = _counter_rate(
            src_first.get("prompt_hist_sum"), last_m.get("prompt_hist_sum"), src_elapsed
        )
        loads_uncached = _counter_rate(
            src_first.get("uncached_tokens_total"), last_m.get("uncached_tokens_total"), src_elapsed
        )
        if uncached_hist is not None:
            uncached_from_tokens = uncached_hist
            if cached_from_tokens is None and prompt_hist is not None:
                # Both histograms are observed as each request finishes, so
                # what they differ by is the cached part of the same prompts.
                metrics["cached_prefill_tps"] = round(max(0.0, prompt_hist - uncached_hist), 1)
        elif loads_uncached is not None:
            uncached_from_tokens = loads_uncached
        else:
            uncached_from_tokens = prefill_from_tokens
    # Only /v1/loads could set this, and no sglang release has; sglang's own
    # prefill figure, the log's "input throughput", is new tokens per second.
    inst_prefill = last_m.get("prefill_tps")
    if uncached_from_tokens is not None and uncached_from_tokens > 0:
        metrics["prefill_tps"] = uncached_from_tokens
    elif inst_prefill is not None:
        metrics["prefill_tps"] = inst_prefill
    elif uncached_from_tokens is not None:
        metrics["prefill_tps"] = uncached_from_tokens

    # The window's hit rate: the cached share of its prompt tokens, else
    # vLLM's prefix-cache hits over queries. A window without prompts has none.
    c0, c1 = src_first.get("cached_tokens_total"), last_m.get("cached_tokens_total")
    p0, p1 = src_first.get("prompt_tokens_total"), last_m.get("prompt_tokens_total")
    if _number(c1) and _number(p1):
        measured.add("cache_hit_rate")
        metrics["cache_hit_rate"] = (
            round(min(1.0, max(0.0, (c1 - c0) / (p1 - p0))), 4)
            if can_rate and _number(c0) and _number(p0) and p1 > p0 and c1 >= c0
            else None
        )
    elif _number(last_m.get("prefix_cache_hits")) and _number(last_m.get("prefix_cache_queries")):
        measured.add("cache_hit_rate")
        metrics["cache_hit_rate"] = _counter_ratio(
            src_first.get("prefix_cache_hits"),
            last_m.get("prefix_cache_hits"),
            src_first.get("prefix_cache_queries"),
            last_m.get("prefix_cache_queries"),
        )


def _speculation(
    metrics: dict[str, Any],
    measured: set[str],
    src_first: dict[str, Any],
    last_m: dict[str, Any],
) -> None:
    """The window's speculative-decoding figures, as SGLang defines them, for either engine.

    The accept length is tokens per verify step, the bonus token included:
    SGLang's gauge, and vLLM's own mean acceptance length, one more than its
    accepted tokens per draft. The rate is accepted draft tokens over those
    proposed. SGLang's gauges cover its last report and hold through idle
    time, so a window with nothing decoded has neither: a step counter's 0,
    or SGLang's throughput gauge, which falls to 0 once decoding stops.
    """
    if _number(last_m.get("spec_accepted_total")):
        measured.update(("spec_accept_rate", "spec_accept_length"))
        accepted = (src_first.get("spec_accepted_total"), last_m.get("spec_accepted_total"))
        metrics["spec_accept_rate"] = _counter_ratio(
            *accepted,
            src_first.get("spec_draft_tokens_total"),
            last_m.get("spec_draft_tokens_total"),
        )
        per_draft = _counter_ratio(
            *accepted, src_first.get("spec_drafts_total"), last_m.get("spec_drafts_total")
        )
        metrics["spec_accept_length"] = round(1.0 + per_draft, 4) if per_draft is not None else None
    elif _number(last_m.get("spec_accept_length")):
        measured.update(("spec_accept_rate", "spec_accept_length"))
        if metrics.get("gen_throughput") == 0:
            metrics["spec_accept_length"] = metrics["spec_accept_rate"] = None


_DROP_COUNTERS = (
    "requests_total",
    "cached_tokens_total",
    "prefill_busy_us",
    "prefix_cache_hits",
    "prefix_cache_queries",
    "ttft_sum",
    "ttft_count",
    "queue_sum",
    "queue_count",
    "prefill_sum",
    "prefill_count",
    "chunked_sum",
    "chunked_count",
    "e2e_sum",
    "e2e_count",
    "itl_sum",
    "itl_count",
    "prompt_hist_sum",
    "prompt_hist_count",
    "uncached_hist_sum",
    "uncached_hist_count",
    "iter_hist_sum",
    "iter_hist_count",
    "gen_hist_sum",
    "gen_hist_count",
    "device_cached_tokens_total",
    "host_cached_tokens_total",
    "loadback_tokens_total",
    "evict_tokens_total",
    "backup_tokens_total",
    "http_error_total",
    "retracts_total",
    "uncached_tokens_total",
    # vLLM speculative-decoding counters, differenced in snapshot_sglang()
    # into spec_accept_rate / spec_accept_length. Raw totals are internal.
    "spec_accepted_total",
    "spec_draft_tokens_total",
    "spec_drafts_total",
    # The token counters each step moves, differenced into the token rates.
    "step_generated_total",
    "step_computed_total",
    "step_cached_total",
    "step_device_cached_total",
    "step_host_cached_total",
    "loads_generated_total",
)

# What flush() fills in from an engine-log read. For a target with no request
# counter these exist only on the flush that reads the logs, so each read's
# values are held, per target, until the next read. A target with one holds
# only the fields whose own counter it lacks (see flush()).
_LOG_FIELDS = (
    "req_per_min",
    "cached_prefill_tps",
    "prefill_tps",
    "gen_throughput",
    "cache_hit_rate",
    "ttft_avg_s",
    "prefill_avg_s",
    "tpot_avg_s",
    "e2e_latency_avg_s",
    "prompt_tokens_avg",
    "uncached_prompt_tokens_avg",
    "generation_tokens_avg",
    "spec_accept_rate",
    "spec_accept_length",
    "chunked_prefill_avg_s",
    "http_error_per_min",
)
_log_holds: dict[str, dict[str, Any]] = {}


def _fillable(metrics: dict[str, Any], measured: frozenset[str], key: str, *empty: Any) -> bool:
    """Whether a log read may fill *key*: the engine's counters did not measure it, and it is unset.

    *empty* lists values that count as unset besides None, such as a 0.
    """
    value = metrics.get(key)
    return key not in measured and (value is None or value in empty)


def engine_identity(
    args: argparse.Namespace, target: dict[str, Any], sid: str, metrics: dict[str, Any]
) -> dict[str, Any]:
    """The ``server`` part of an engine's body: which engine it is, where, on which GPUs."""
    gpu_indexes = target.get("gpu_indexes") or []
    return {
        "id": target.get("id") or sid,
        "node_id": args.node_id,
        "name": target.get("name") or target.get("id"),
        "endpoint": target.get("endpoint") or target.get("url"),
        "model_name": metrics.get("model_name")
        or target.get("model_name")
        or target.get("name")
        or target.get("id"),
        "gpu_indexes": ",".join(str(i) for i in gpu_indexes) if gpu_indexes else None,
        "engine": target.get("engine") or metrics.get("engine") or "sglang",
    }


def flush(
    args: argparse.Namespace,
    node: dict[str, Any],
    gpus: list[dict[str, Any]],
    sglang: dict[str, dict[str, Any]],
    cpu: dict[str, Any] | None,
    prev_sglang: dict[str, dict[str, Any]],
    elapsed: float,
    include_requests: bool,
    *,
    taken: float | None = None,
    log: bool = True,
) -> dict[str, dict[str, Any]]:
    """Post one snapshot of the host, its GPUs and engines, taken at *taken* (epoch seconds).

    Logs what it posted when *log* is set. Returns the engine readings the next
    rates start from.
    """
    node_meta = {
        **node,
        "metadata": {
            "interval_s": round(elapsed, 2),
            "fine_grained": True,
        },
    }
    if cpu:
        node_meta["cpu"] = cpu
    gpu_by_index = {int(g["index"]): g for g in gpus if "index" in g}
    next_prev: dict[str, dict[str, Any]] = {}
    # Every engine rides in the same request as the GPUs: one gateway request
    # per flush rather than one per engine plus one for the GPUs.
    engines: list[dict[str, Any]] = []
    commits: list[tuple[str, dict[str, Any] | None, tuple[str, ...], Any]] = []
    lines: list[str] = []
    for sid, payload in sglang.items():
        if payload.get("idle"):
            # Stopped on purpose by the proxy that starts it on demand: posted
            # as idle, with no figures. Empty metrics rather than none, which a
            # gateway from before idle engines would refuse the whole body for.
            # No counters for the next rates to start from: they start again
            # with the engine.
            server = engine_identity(args, payload["target"], sid, {})
            server["model_name"] = payload["target"].get("model_name") or payload["target"].get(
                "on_demand_model"
            )
            engines.append({"server": {**server, "state": "idle"}, "metrics": {}})
            commits.append((sid, None, (), None))
            lines.append(f"flush {sid} gpus={server['gpu_indexes']} idle")
            continue
        next_prev[sid] = payload
        agg = snapshot_sglang(payload, prev_sglang.get(sid), elapsed)
        target = agg["target"]
        gpu_indexes = target.get("gpu_indexes") or []
        sm_vals = [gpu_by_index[i].get("utilization_pct") for i in gpu_indexes if i in gpu_by_index]
        sm_util = mx(sm_vals)
        metrics = dict(agg.get("metrics") or {})
        metrics["sm_util_pct"] = sm_util
        port = urlparse(str(target.get("url") or "")).port
        log_elapsed = max(elapsed, REQUEST_FLUSH_S) if include_requests else elapsed
        # With no request counter (sglang /v1/loads without --enable-metrics),
        # req/min, the prefill token rates and the per-request averages exist
        # only on the flush that reads the engine logs, once per
        # REQUEST_FLUSH_S. That left ~11 of every 12 samples NULL, and the
        # Served totals, which sum rate x interval over every sample, counted
        # ~1/12 of the requests. So such a target reads back-to-back log
        # windows and holds each read's values until the next.
        #
        # A target with a request counter can still lack the counter behind
        # http_error_per_min: sglang:http_responses_total has no series until
        # a restarted server has counted a response, and a build need not
        # export it. Such a target holds that field alone. The log block
        # below fills nothing its counters and histograms measure, a blank
        # window included: the logs' minute would put an old window's value
        # where there is a live one, or count again what the counter samples
        # around it already hold. vLLM stays out: its access-log lines carry
        # no timestamp, so no window can place its errors.
        measured = agg.get("measured") or frozenset()
        scraped = payload.get("metrics") or {}
        has_req_counter = isinstance(scraped.get("requests_total"), (int, float))
        engine = metrics.get("engine") or target.get("engine") or "sglang"
        if not has_req_counter:
            held = _LOG_FIELDS
        elif engine == "sglang" and not isinstance(scraped.get("http_error_total"), (int, float)):
            held = ("http_error_per_min",)
        else:
            held = ()
        hold = _log_holds.get(sid) if held else None
        # One stored beside a request counter holds http_error_per_min alone
        # and cannot stand in for all of _LOG_FIELDS: a flush that needs those
        # (its /metrics scrape failed) reads afresh, as ever, and leaves it for
        # the next flush with a counter unless it reads the logs itself.
        spare = hold if hold is not None and not set(held) <= set(hold["fields"]) else None
        if spare is not None or (
            hold is not None and time.time() - hold["at"] > 2 * REQUEST_FLUSH_S
        ):
            hold = None
        if not include_requests:
            log_stats = None
        elif has_req_counter:
            log_stats = summarize_engine_logs(port, log_elapsed)
            if held:
                # Its own back-to-back window, so the held rate integrates to
                # the errors logged. The trailing read above, which still
                # gives every other field, counts every error in the ~90 s
                # `docker logs` returns and divides by 60.
                errors, errors_s, hold = read_log_window(port, hold)
                if errors is not None:
                    metrics["http_error_per_min"] = round(
                        float(errors["http_errors"]) / errors_s * 60.0, 2
                    )
        else:
            log_stats, log_elapsed, hold = read_log_window(port, hold)
        if (
            log_stats is not None
            and log_elapsed > 0
            and (
                metrics.get("req_per_min") is None
                or metrics.get("cached_prefill_tps") is None
                or metrics.get("ttft_avg_s") is None
                or (metrics.get("engine") or target.get("engine")) == "sglang"
            )
        ):
            if _fillable(metrics, measured, "req_per_min"):
                metrics["req_per_min"] = round(log_stats["posts"] / log_elapsed * 60.0, 1)
            cached_tok = log_stats["cached_tokens"]
            new_tok = log_stats["new_tokens"]
            # #new-token is the uncached part, like prefill_tps. Only where no
            # counter gave a rate: a counter's 0 is this sample's measurement.
            if _fillable(metrics, measured, "cached_prefill_tps") and cached_tok > 0:
                metrics["cached_prefill_tps"] = round(cached_tok / log_elapsed, 1)
            if _fillable(metrics, measured, "prefill_tps") and new_tok > 0:
                metrics["prefill_tps"] = round(new_tok / log_elapsed, 1)
            if _fillable(metrics, measured, "gen_throughput", 0) and log_stats.get("decode_tps"):
                metrics["gen_throughput"] = round(float(log_stats["decode_tps"]), 1)
            if "cache_hit_rate" not in measured and cached_tok + new_tok > 0:
                metrics["cache_hit_rate"] = round(cached_tok / (cached_tok + new_tok), 4)
            for key, stat in (
                ("prefill_avg_s", "req_prefill_avg_s"),
                ("tpot_avg_s", "req_tpot_avg_s"),
                ("e2e_latency_avg_s", "req_e2e_avg_s"),
                ("prompt_tokens_avg", "req_prompt_avg"),
                ("uncached_prompt_tokens_avg", "req_uncached_avg"),
                ("generation_tokens_avg", "req_gen_avg"),
                ("spec_accept_rate", "req_spec_rate"),
                ("spec_accept_length", "req_spec_len"),
                ("chunked_prefill_avg_s", "chunked_prefill_avg_s"),
                ("ttft_avg_s", "req_ttft_avg_s"),
            ):
                if _fillable(metrics, measured, key, 0):
                    metrics[key] = log_stats.get(stat)
            if metrics.get("http_error_per_min") is None:
                metrics["http_error_per_min"] = round(
                    float(log_stats.get("http_errors") or 0) / log_elapsed * 60.0, 2
                )
            n_log = log_stats.get("posts") or log_stats.get("prefill_done")
            if (
                _fillable(metrics, measured, "prefill_avg_s", 0)
                and n_log
                and log_stats.get("prefill_s")
            ):
                metrics["prefill_avg_s"] = round(float(log_stats["prefill_s"]) / n_log, 4)
        if engine != "vllm" and _fillable(metrics, measured, "ttft_avg_s", 0) and log_stats:
            n_req = log_stats["posts"] or log_stats["prefill_done"]
            if n_req > 0 and log_stats["prefill_s"] > 0:
                metrics["ttft_avg_s"] = round(log_stats["prefill_s"] / n_req, 4)
            elif n_req > 0:
                first_m = (prev_sglang.get(sid) or payload).get("metrics") or {}
                last_m = payload.get("metrics") or {}
                busy0 = first_m.get("prefill_busy_us")
                busy1 = last_m.get("prefill_busy_us")
                if (
                    isinstance(busy0, (int, float))
                    and isinstance(busy1, (int, float))
                    and busy1 > busy0
                ):
                    metrics["ttft_avg_s"] = round((busy1 - busy0) / 1e6 / n_req, 4)
                else:
                    metrics["ttft_avg_s"] = None
        if metrics.get("ttft_avg_s") == 0:
            metrics["ttft_avg_s"] = None
        if hold is not None:
            if include_requests:
                hold["values"] = {k: metrics[k] for k in held if metrics.get(k) is not None}
            else:
                # Only what this scrape left empty: a live value, such as
                # gen_throughput from /v1/loads, still wins. And only the
                # fields held now, should a hold taken without a request
                # counter meet a flush that has one.
                for key in held:
                    if _fillable(metrics, measured, key) and key in hold["values"]:
                        metrics[key] = hold["values"][key]
        if (
            _fillable(metrics, measured, "queue_avg_s")
            and metrics.get("ttft_avg_s") is not None
            and metrics.get("prefill_avg_s") is not None
        ):
            q = float(metrics["ttft_avg_s"]) - float(metrics["prefill_avg_s"])
            metrics["queue_avg_s"] = round(q, 4) if q > 0.0005 else 0.0
        for key in _DROP_COUNTERS:
            metrics.pop(key, None)
        server = engine_identity(args, target, sid, metrics)
        indexes = server["gpu_indexes"]
        requests = []
        if include_requests and log_stats and isinstance(log_stats.get("requests"), list):
            requests = log_stats["requests"][:MAX_REQUEST_EVENTS]
        engine_body: dict[str, Any] = {"server": server, "metrics": metrics}
        if requests:
            engine_body["requests"] = requests
        engines.append(engine_body)
        commits.append((sid, hold, held, spare))
        lines.append(
            f"flush {sid} gpus={indexes} sm={sm_util} run={metrics.get('num_running_reqs')} "
            f"q={metrics.get('num_queue_reqs')} used={metrics.get('num_used_tokens')} "
            f"cap={metrics.get('max_total_num_tokens')} prefill={metrics.get('prefill_tps')} "
            f"decode={metrics.get('gen_throughput')} cached_prefill={metrics.get('cached_prefill_tps')} "
            f"cache={metrics.get('cache_hit_rate')} rpm={metrics.get('req_per_min')} "
            f"ttft={metrics.get('ttft_avg_s')} queue={metrics.get('queue_avg_s')} "
            f"prefill={metrics.get('prefill_avg_s')}"
        )

    body: dict[str, Any] = {"node": node_meta}
    if taken is not None:
        body["ts"] = int(taken * 1000)
    if gpus:
        body["gpus"] = gpus
    if engines:
        body["sglang"] = engines
    if gpus or engines or not args.skip_gpu:
        result = post(args.dashboard, args.token, body)
        if log or result.get("requests_dropped"):
            print(
                f"flush gpus interval={elapsed:.1f}s "
                f"cpu={node_meta.get('cpu', {}).get('cpu_util_pct')} "
                f"util={[g.get('utilization_pct') for g in gpus]} {result}",
                flush=True,
            )
            for line in lines:
                print(line, flush=True)
    # Only once the sample is stored: if the post fails, main() retries
    # the log read next flush, from the old hold's end. A hold then
    # serves the fields of the flush that stored it, and no more.
    for sid, hold, held, spare in commits:
        if hold is not None:
            hold["fields"] = held
            _log_holds[sid] = hold
        elif spare is None or include_requests:
            _log_holds.pop(sid, None)
    return next_prev


def next_slot(now: float, interval: float) -> float:
    """When the next snapshot is taken: the middle of the next *interval* of wall-clock time.

    Taken mid-interval, a snapshot stays inside its own second (the gateway
    keeps one sample a second, by the snapshot's time) however late a busy
    host wakes. One whose time has passed is skipped, not caught up.
    """
    slot = math.floor(now / interval) * interval + interval / 2
    return slot if slot > now else slot + interval


def main() -> int:
    """Scrape and post every flush interval, or once with ``--once``."""
    global _error_interval
    args = parse_args()
    _error_interval = 0.0 if args.once else args.log_interval
    targets = [] if args.skip_sglang else sglang_targets(args)
    node = {
        "id": args.node_id,
        "hostname": args.hostname,
        "region": args.region,
        "cluster": args.cluster,
    }
    prev_sglang: dict[str, dict[str, Any]] = {}
    last_req_flush = 0.0
    last_log = -math.inf
    last_flush_at = time.time()
    tick = next_slot(last_flush_at, args.flush_interval)

    while True:
        if not args.once:
            wait = tick - time.time()
            if wait > 0:
                time.sleep(wait)
        taken = time.time()
        gpus: list[dict[str, Any]] = []
        sglang: dict[str, dict[str, Any]] = {}
        cpu: dict[str, Any] | None = None
        try:
            snap = scrape(args, targets)
            gpus = snap["gpus"] or []
            cpu = snap.get("cpu")
            sglang = snap["sglang"] or {}
        except (
            subprocess.CalledProcessError,
            urllib.error.URLError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            log_error("scrape", f"scrape error: {exc}")

        elapsed = max(taken - last_flush_at, 0.001)
        include_requests = args.once or (taken - last_req_flush) >= REQUEST_FLUSH_S
        log = args.once or taken - last_log >= args.log_interval
        try:
            prev_sglang = flush(
                args,
                node,
                gpus,
                sglang,
                cpu,
                prev_sglang,
                elapsed,
                include_requests,
                taken=taken,
                log=log,
            )
            if include_requests:
                last_req_flush = taken
            if log:
                last_log = taken
            # Only after a stored flush: a failed one keeps the previous
            # counters, so the next rates must span the time since those.
            last_flush_at = taken
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            log_error("flush", f"flush error: {exc}")
            if args.once:
                return 1
        if args.once:
            return 0
        tick = next_slot(time.time(), args.flush_interval)


if __name__ == "__main__":
    raise SystemExit(main())
