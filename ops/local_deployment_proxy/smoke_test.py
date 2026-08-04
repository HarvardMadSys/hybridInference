#!/usr/bin/env python3
"""Smoke-test a running local_deployment_proxy.

Checks the proxy's liveness (/health, /v1/models) and then runs one chat
completion and one embedding request against the served models. Exits 0 only if
every check passes. Standard library only — no pip installs, no curl/jq needed.

Examples:
  ./smoke_test.py                                  # http://localhost:8001
  ./smoke_test.py --url http://localhost:8001
  ./smoke_test.py --chat-model Qwen/Qwen3.6-35B-A3B-FP8 --embed-model BAAI/bge-m3
  ./smoke_test.py --all --timeout 600              # every model; allow a cold start
  LOCAL_API_KEY=secret ./smoke_test.py

Run it on the GPU box, or anywhere that can reach the proxy — e.g. a router host
where the reverse tunnel binds :8001 (`./smoke_test.py --url http://localhost:8001`
over ssh). The first request to an idle model cold-starts its container and can
take a few minutes; raise --timeout to accommodate that.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Model ids that look like embedding models (served via /v1/embeddings, no chat).
EMBED_RE = re.compile(r"bge|embed|\be5\b|\bgte\b|nomic|minilm", re.I)

_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _passed(msg: str) -> None:
    print("  " + _c("32", "PASS") + "  " + msg)


def _failed(msg: str) -> None:
    print("  " + _c("31", "FAIL") + "  " + msg)


def _note(msg: str) -> None:
    print("        " + msg)


def request(method, url, api_key, payload=None, timeout=30):
    """Send an HTTP request and return ``(status, body, seconds)``.

    ``status`` is None on a transport error (connection refused, timeout, DNS
    failure); ``body`` then holds the reason string.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.time() - start
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception as read_exc:  # e.g. connection reset mid-read
            body = f"<failed to read error body: {read_exc}>"
        return exc.code, body, time.time() - start
    except Exception as exc:  # URLError, timeout, OSError, ...
        return None, f"{type(exc).__name__}: {exc}", time.time() - start


def check_health(base, timeout):
    """Probe ``GET /health``; return True on a 200."""
    status, body, dt = request("GET", f"{base}/health", None, timeout=timeout)
    if status == 200:
        _passed(f"GET /health -> 200 ({dt:.2f}s)")
        return True
    if status is None:
        _failed(f"GET /health -> unreachable: {body}")
        _note(f"Is the proxy running and listening at {base}?")
    else:
        _failed(f"GET /health -> {status}: {body[:200]}")
    return False


def check_models(base, timeout):
    """Fetch ``GET /v1/models``; return the list of model ids, or None on failure."""
    status, body, dt = request("GET", f"{base}/v1/models", None, timeout=timeout)
    if status != 200:
        _failed(f"GET /v1/models -> {status}: {body[:200]}")
        return None
    try:
        ids = [m["id"] for m in json.loads(body).get("data", [])]
    except Exception as exc:
        _failed(f"GET /v1/models -> 200 but unparseable: {exc}")
        return None
    if not ids:
        _failed(f"GET /v1/models -> 200 but no models are being served ({dt:.2f}s)")
        return ids
    _passed(f"GET /v1/models -> 200, {len(ids)} model(s) ({dt:.2f}s)")
    for model_id in ids:
        _note(f"- {model_id}")
    return ids


def check_chat(base, api_key, model, timeout, thinking):
    """Run a chat completion against ``model``; return True if it answers."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with a short greeting."}],
        "max_tokens": 64,
        "temperature": 0,
        # Reasoning models otherwise spend the budget "thinking" and return
        # empty content. Qwen-style chat templates read ``enable_thinking``
        # while DeepSeek-V4 templates read ``thinking``; send both so the
        # --thinking toggle works everywhere (templates ignore unknown kwargs).
        "chat_template_kwargs": {"enable_thinking": thinking, "thinking": thinking},
    }
    _note(f"chat: {model} (cold start may take minutes; timeout {timeout}s) ...")
    status, body, dt = request("POST", f"{base}/v1/chat/completions", api_key, payload, timeout)
    if status != 200:
        _failed(f"chat {model} -> {status}: {body[:300]}")
        return False
    try:
        message = json.loads(body)["choices"][0]["message"]
        # The answer must be in ``content``: standard OpenAI clients never read
        # ``reasoning_content``, so falling back to it here would mask a broken
        # deployment (e.g. a reasoning parser that classifies the entire
        # generation as reasoning and leaves ``content`` empty).
        text = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or "").strip()
    except Exception as exc:
        _failed(f"chat {model} -> 200 but unparseable: {exc}")
        return False
    if not text:
        if reasoning:
            _failed(
                f"chat {model} -> 200 but content is empty; the answer landed in "
                f"reasoning_content ({dt:.1f}s) — reasoning parser misconfigured, "
                f"or the thinking budget swallowed the reply"
            )
        else:
            _failed(f"chat {model} -> 200 but empty content ({dt:.1f}s)")
        return False
    _passed(f"chat {model} -> 200 ({dt:.1f}s)")
    _note(f'reply: "{text[:120]}"')
    return True


def check_embed(base, api_key, model, timeout):
    """Run an embedding request against ``model``; return True on a valid vector."""
    payload = {"model": model, "input": "hello from the smoke test"}
    _note(f"embed: {model} (cold start may take a minute; timeout {timeout}s) ...")
    status, body, dt = request("POST", f"{base}/v1/embeddings", api_key, payload, timeout)
    if status != 200:
        _failed(f"embed {model} -> {status}: {body[:300]}")
        return False
    try:
        embedding = json.loads(body)["data"][0]["embedding"]
    except Exception as exc:
        _failed(f"embed {model} -> 200 but unparseable: {exc}")
        return False
    if not embedding:
        _failed(f"embed {model} -> 200 but empty embedding ({dt:.1f}s)")
        return False
    _passed(f"embed {model} -> 200, dim={len(embedding)} ({dt:.1f}s)")
    return True


def main():
    """Parse args, run the checks, print a summary, and return an exit code."""
    parser = argparse.ArgumentParser(description="Smoke-test a local_deployment_proxy.")
    parser.add_argument(
        "--url",
        default=os.environ.get("LOCAL_DEPLOYMENT_URL", "http://localhost:8001"),
        help="proxy base URL (default %(default)s; a trailing /v1 is stripped)",
    )
    parser.add_argument(
        "--api-key",
        # `or`, not a get() default: a blank LOCAL_API_KEY must land on the same
        # value the proxy itself falls back to, or the smoke test 401s and reads
        # as a proxy fault.
        default=os.environ.get("LOCAL_API_KEY", "").strip() or "freeinference_api",
        help="bearer key for /v1 endpoints (default: env LOCAL_API_KEY or 'freeinference_api')",
    )
    parser.add_argument(
        "--chat-model", help="chat model id to test (default: first non-embedding model)"
    )
    parser.add_argument(
        "--embed-model", help="embedding model id to test (default: first embedding-looking model)"
    )
    parser.add_argument(
        "--all", action="store_true", help="test every served model, not just one of each kind"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="per-request timeout in seconds (default %(default)s)",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="send enable_thinking=true (default false, matching the gateway)",
    )
    parser.add_argument("--no-chat", action="store_true", help="skip chat-completion checks")
    parser.add_argument("--no-embed", action="store_true", help="skip embedding checks")
    args = parser.parse_args()

    # Flush each line as it prints, so cold-start progress is visible even when
    # stdout is redirected to a file or pipe (block-buffered by default).
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)

    base = args.url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    print(f"Target: {base}")

    results = []
    results.append(check_health(base, min(args.timeout, 15)))
    ids = check_models(base, min(args.timeout, 15))
    results.append(bool(ids))

    if ids:
        embed_ids = [i for i in ids if EMBED_RE.search(i)]
        chat_ids = [i for i in ids if not EMBED_RE.search(i)]

        if not args.no_chat:
            targets = (
                [args.chat_model] if args.chat_model else (chat_ids if args.all else chat_ids[:1])
            )
            if targets and targets[0]:
                for model in targets:
                    results.append(
                        check_chat(base, args.api_key, model, args.timeout, args.thinking)
                    )
            else:
                _failed(
                    "chat check enabled but no chat model is served (use --chat-model or --no-chat)"
                )
                results.append(False)

        if not args.no_embed:
            targets = (
                [args.embed_model]
                if args.embed_model
                else (embed_ids if args.all else embed_ids[:1])
            )
            if targets and targets[0]:
                for model in targets:
                    results.append(check_embed(base, args.api_key, model, args.timeout))
            else:
                _failed(
                    "embedding check enabled but no embedding model is served (use --embed-model or --no-embed)"
                )
                results.append(False)

    passes, total = sum(results), len(results)
    print()
    print(_c("32" if passes == total else "31", f"{passes}/{total} checks passed"))
    return 0 if passes == total else 1


if __name__ == "__main__":
    sys.exit(main())
