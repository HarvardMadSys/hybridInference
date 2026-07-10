#!/usr/bin/env python3
"""Throughput/latency benchmark against the H200 idle proxy (DeepSeek-V4-Flash TP=3).

Stdlib-only. Hits an OpenAI-compatible /v1/chat/completions endpoint through the
idle proxy, measures wall-clock latency and tokens/s from response usage.

Examples:
  # Wait for cold start (up to 40 min), then run the default matrix:
  python bench_proxy.py --wait-ready 2400

  # Decode concurrency sweep only:
  python bench_proxy.py --skip-prefill --decode-concurrencies 1,2,4,8
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_URL = os.environ.get("BENCH_URL", "http://127.0.0.1:8003/v1")
DEFAULT_MODEL = os.environ.get("BENCH_MODEL", "deepseek-v4-flash")
DEFAULT_API_KEY = os.environ.get("LOCAL_API_KEY", "freeinference_api")
RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


@dataclass
class Sample:
    ok: bool
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    error: str = ""


def _request(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> Sample:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        dt = time.perf_counter() - t0
        usage = body.get("usage") or {}
        return Sample(
            ok=True,
            latency_s=dt,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
    except Exception as exc:  # noqa: BLE001 — surface any transport/HTTP failure
        return Sample(
            ok=False,
            latency_s=time.perf_counter() - t0,
            prompt_tokens=0,
            completion_tokens=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def _approx_prompt(n_tokens: int) -> str:
    # ~1 token per word for English; pad deterministically.
    unit = "word "
    return ("Please summarize: " + unit * max(1, n_tokens - 4)).strip()


def wait_ready(url: str, api_key: str, timeout_s: float) -> None:
    """Poll until a short non-stream completion succeeds."""
    deadline = time.time() + timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        sample = _request(
            url,
            api_key,
            {
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
                "stream": False,
                "temperature": 0,
            },
            timeout=min(120.0, timeout_s),
        )
        if sample.ok and sample.completion_tokens > 0:
            print(f"[ready] attempt={attempt} latency={sample.latency_s:.1f}s", flush=True)
            return
        print(
            f"[wait] attempt={attempt} ok={sample.ok} err={sample.error[:160]!r}",
            flush=True,
        )
        time.sleep(15)
    raise SystemExit(f"server not ready within {timeout_s}s")


def run_once(
    url: str,
    api_key: str,
    model: str,
    input_len: int,
    max_tokens: int,
    timeout: float,
) -> Sample:
    return _request(
        url,
        api_key,
        {
            "model": model,
            "messages": [{"role": "user", "content": _approx_prompt(input_len)}],
            "max_tokens": max_tokens,
            "stream": False,
            "temperature": 0,
        },
        timeout=timeout,
    )


def summarize(samples: list[Sample]) -> dict[str, Any]:
    ok = [s for s in samples if s.ok]
    if not ok:
        return {
            "n": len(samples),
            "n_ok": 0,
            "errors": [s.error for s in samples[:3]],
        }
    lats = [s.latency_s for s in ok]
    ptok = sum(s.prompt_tokens for s in ok)
    ctok = sum(s.completion_tokens for s in ok)
    wall = sum(lats)
    return {
        "n": len(samples),
        "n_ok": len(ok),
        "latency_mean_s": statistics.mean(lats),
        "latency_p50_s": statistics.median(lats),
        "latency_min_s": min(lats),
        "latency_max_s": max(lats),
        "prompt_tokens_total": ptok,
        "completion_tokens_total": ctok,
        "prompt_tok_per_s": (ptok / wall) if wall else 0.0,
        "completion_tok_per_s": (ctok / wall) if wall else 0.0,
        # Concurrent-friendly: total completion tokens / max wall among samples is wrong;
        # for concurrent runs callers pass wall_clock_s separately.
    }


def run_prefill(
    url: str,
    api_key: str,
    model: str,
    input_lens: list[int],
    repeats: int,
    timeout: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for n_in in input_lens:
        print(f"[prefill] input_len≈{n_in} x{repeats}", flush=True)
        samples = [
            run_once(url, api_key, model, n_in, max_tokens=1, timeout=timeout)
            for _ in range(repeats)
        ]
        row = {"phase": "prefill", "input_len": n_in, "concurrency": 1, **summarize(samples)}
        # Prefill-ish throughput: prompt tokens / latency (output is 1 token).
        if row.get("n_ok"):
            ok = [s for s in samples if s.ok]
            row["prefill_tok_per_s_mean"] = statistics.mean(
                [(s.prompt_tokens / s.latency_s) if s.latency_s else 0.0 for s in ok]
            )
        rows.append(row)
        print(json.dumps(row, indent=2), flush=True)
    return rows


def run_decode(
    url: str,
    api_key: str,
    model: str,
    concurrencies: list[int],
    input_len: int,
    output_len: int,
    timeout: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for conc in concurrencies:
        print(
            f"[decode] concurrency={conc} in≈{input_len} out={output_len}",
            flush=True,
        )
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as pool:
            futs = [
                pool.submit(
                    run_once,
                    url,
                    api_key,
                    model,
                    input_len,
                    output_len,
                    timeout,
                )
                for _ in range(conc)
            ]
            samples = [f.result() for f in futs]
        wall = time.perf_counter() - t0
        row = {
            "phase": "decode",
            "input_len": input_len,
            "output_len": output_len,
            "concurrency": conc,
            "wall_clock_s": wall,
            **summarize(samples),
        }
        if row.get("n_ok"):
            ctok = row["completion_tokens_total"]
            row["aggregate_completion_tok_per_s"] = ctok / wall if wall else 0.0
            row["per_req_completion_tok_per_s_mean"] = statistics.mean(
                [
                    (s.completion_tokens / s.latency_s) if s.latency_s else 0.0
                    for s in samples
                    if s.ok
                ]
            )
        rows.append(row)
        print(json.dumps(row, indent=2), flush=True)
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--api-key", default=DEFAULT_API_KEY)
    p.add_argument("--wait-ready", type=float, default=0.0, help="seconds to wait for readiness")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--prefill-input-lens", default="1024,4096,16384")
    p.add_argument("--decode-concurrencies", default="1,2,4,8")
    p.add_argument("--decode-input-len", type=int, default=1024)
    p.add_argument("--decode-output-len", type=int, default=256)
    p.add_argument("--prefill-repeats", type=int, default=3)
    p.add_argument("--skip-prefill", action="store_true")
    p.add_argument("--skip-decode", action="store_true")
    p.add_argument("--out", type=Path, default=RESULTS_DIR / "summary.json")
    args = p.parse_args()

    if args.wait_ready > 0:
        wait_ready(args.url, args.api_key, args.wait_ready)

    # Warmup
    print("[warmup] short completion", flush=True)
    warm = run_once(args.url, args.api_key, args.model, 64, 16, args.timeout)
    print(json.dumps(asdict(warm), indent=2), flush=True)
    if not warm.ok:
        print("warmup failed; aborting", file=sys.stderr)
        return 1

    rows: list[dict[str, Any]] = []
    meta = {
        "url": args.url,
        "model": args.model,
        "started_unix": time.time(),
        "hostname": os.uname().nodename,
        "notes": "H200 idle proxy; sglang TP=3 on GPUs 0,2,3; input_len is approximate word count",
    }

    if not args.skip_prefill:
        lens = [int(x) for x in args.prefill_input_lens.split(",") if x.strip()]
        rows.extend(
            run_prefill(
                args.url,
                args.api_key,
                args.model,
                lens,
                args.prefill_repeats,
                args.timeout,
            )
        )
    if not args.skip_decode:
        concs = [int(x) for x in args.decode_concurrencies.split(",") if x.strip()]
        rows.extend(
            run_decode(
                args.url,
                args.api_key,
                args.model,
                concs,
                args.decode_input_len,
                args.decode_output_len,
                args.timeout,
            )
        )

    out = {
        "meta": meta,
        "results": rows,
        "finished_unix": time.time(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(f"[done] wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
