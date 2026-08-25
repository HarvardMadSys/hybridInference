"""Black-box acceptance check for the runnable example distribution."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any

EXPECTED_MODEL = "example-chat"
EXPECTED_CONTENT = "RUNNABLE_EXAMPLE_OK"


def _request_json(base_url: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return json.load(response)


def _check(base_url: str) -> None:
    health = _request_json(base_url, "/health")
    if health.get("status") != "healthy":
        raise RuntimeError(f"unexpected health response: {health}")

    site = _request_json(base_url, "/site-config")
    if site.get("distribution", {}).get("id") != "example":
        raise RuntimeError(f"example manifest is not active: {site}")

    models = _request_json(base_url, "/v1/models")
    served = {item.get("id") for item in models.get("data", [])}
    if EXPECTED_MODEL not in served:
        raise RuntimeError(f"{EXPECTED_MODEL!r} not in served models: {sorted(served)}")

    completion = _request_json(
        base_url,
        "/v1/chat/completions",
        {
            "model": EXPECTED_MODEL,
            "messages": [{"role": "user", "content": "Return the example sentinel."}],
        },
    )
    content = completion.get("choices", [{}])[0].get("message", {}).get("content")
    if content != EXPECTED_CONTENT:
        raise RuntimeError(f"completion did not traverse the fake upstream: {completion}")


def main() -> None:
    """Poll until the stack passes every black-box assertion or times out."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _check(args.base_url)
        except (OSError, RuntimeError, ValueError, urllib.error.HTTPError) as exc:
            last_error = exc
            time.sleep(1)
            continue
        print("EXAMPLE_SMOKE_OK")
        return
    raise SystemExit(f"example smoke timed out after {args.timeout:g}s: {last_error}")


if __name__ == "__main__":
    main()
