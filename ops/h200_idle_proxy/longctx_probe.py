#!/usr/bin/env python3
"""Honest long-context prefill/decode probe for DeepSeek-V4-Flash-0731.

Why not sglang.bench_serving here: it issues a warm-up request using the same seeded
prompt as the measured run, so with a small --num-prompts the measured request is served
almost entirely from the radix prefix cache. At 1M it reported 330k tok/s input
throughput and a 2.8s TTFT, while the server log showed the measured request as
"#new-token: 256, #cached-token: 999936" -- a cache hit, not a prefill. Flushing before
the run does not help: the warm-up repopulates the cache before the measured request.

This probe streams one request per length with freshly randomised token ids (no prefix
can be shared), flushes the cache first, and takes prefill from time-to-first-token and
decode from the gaps between subsequent tokens -- both from the same request, so no
cross-request subtraction. `cached` must read 0 for the numbers to mean anything.

Run ON the node under test (inside the sglang container).
"""

from __future__ import annotations

import contextlib
import json
import random
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8001"
VOCAB_LO, VOCAB_HI = 1000, 120000  # steer clear of special ids at either end


def _flush() -> None:
    with contextlib.suppress(Exception):
        urllib.request.urlopen(
            urllib.request.Request(BASE + "/flush_cache", method="POST"), timeout=60
        ).read()
    time.sleep(2)


def probe(n_tokens: int, decode_tokens: int, rng: random.Random) -> dict:
    # Fresh ids per call: any shared prefix would be served from the radix cache and the
    # measurement would report cache-hit latency instead of real prefill.
    ids = [rng.randint(VOCAB_LO, VOCAB_HI) for _ in range(n_tokens)]
    _flush()

    payload = {
        "input_ids": ids,
        # ignore_eos: a prompt of random ids makes the model emit EOS after ~25 tokens,
        # which is too short to measure decode rate stably.
        "sampling_params": {
            "max_new_tokens": decode_tokens,
            "temperature": 0,
            "ignore_eos": True,
        },
        "stream": True,
    }
    req = urllib.request.Request(
        BASE + "/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    stamps: list[float] = []
    meta: dict = {}
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=3600) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            stamps.append(time.perf_counter())
            with __import__("contextlib").suppress(Exception):
                meta = json.loads(body).get("meta_info", {}) or meta

    if not stamps:
        raise RuntimeError("no tokens streamed")

    ttft = stamps[0] - t0
    # Each SSE chunk can carry several accepted tokens under speculative decoding, so
    # divide the streaming window by generated tokens rather than by chunk count.
    generated = int(meta.get("completion_tokens") or len(stamps))
    window = stamps[-1] - stamps[0]
    return {
        "in": n_tokens,
        "ttft_s": ttft,
        "prefill_tok_s": n_tokens / ttft,
        "gen": generated,
        "chunks": len(stamps),
        "decode_tok_s": (generated - 1) / window if window > 0 and generated > 1 else 0.0,
        "itl_ms": window * 1000.0 / (generated - 1) if generated > 1 else 0.0,
        "cached": int(meta.get("cached_tokens", -1)),
    }


def main() -> None:
    lengths = [int(x) for x in sys.argv[1:]] or [32768, 131072, 262144, 524288, 1000000]
    hdr = (
        f"{'input':>9} {'TTFT s':>8} {'prefill tok/s':>14} {'gen':>5} {'chunks':>7} "
        f"{'decode tok/s':>13} {'ITL ms':>8} {'cached':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    rng = random.Random(20260804)
    for n in lengths:
        try:
            r = probe(n, 128, rng)
        except Exception as exc:  # a length that OOMs must not hide the remaining rows
            print(f"{n:>9} FAILED: {type(exc).__name__}: {exc}")
            continue
        print(
            f"{r['in']:>9} {r['ttft_s']:>8.2f} {r['prefill_tok_s']:>14.0f} {r['gen']:>5} "
            f"{r['chunks']:>7} {r['decode_tok_s']:>13.1f} {r['itl_ms']:>8.2f} {r['cached']:>7}"
        )


if __name__ == "__main__":
    main()
