"""The RouteWise example's runtime path: real sockets, real probe, no database.

These live outside `tests/unit/` on purpose. That tier stubs `aiohttp` with a
placeholder session, so a probe there cannot make a request and a green run
would prove nothing about the behaviour the README promises.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from routing.routers import FixedRouter
from routing.routewise.candidates import QuotaPolicy, QuotaSource
from routing.routewise.quota import ProviderQuotaSnapshotStore, QuotaPool
from serving.admin import provider_quotas
from serving.servers.registry import register_from_models_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "config" / "examples" / "models.routewise.yaml"


_PROVIDER_QUOTAS = _REPO_ROOT / "apps" / "backend" / "serving" / "admin" / "provider_quotas.py"


def _example_model() -> dict:
    return yaml.safe_load(_EXAMPLE.read_text())["models"][0]


def _commented_reference_routes() -> list[dict]:
    """Uncomment the `route:` entries the example ships as documentation."""
    import re

    lines = _EXAMPLE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "route:")
    body: list[str] = []
    for line in lines[start + 1 :]:
        match = re.match(r"^      # ?(.*)$", line)
        if not match:
            continue
        entry = match.group(1)
        if entry.startswith("- kind:") or (entry.startswith("  ") and entry.strip()):
            body.append(entry)
    routes = yaml.safe_load("\n".join(body))
    assert routes, "the example no longer carries commented reference routes"
    return routes


def _start_example_fixture(port: int, response_text: str, delay_ms: float):
    """Run the example's own provider fixture in-process on loopback."""
    spec = importlib.util.spec_from_file_location(
        f"_example_fixture_{port}",
        _REPO_ROOT
        / "distributions"
        / "example"
        / "fixtures"
        / "fake-openai-provider"
        / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Per-server subclass: the fixture keeps its settings on the handler class,
    # so two servers with different delays cannot share one handler.
    handler = type(
        f"_Handler{port}",
        (module.FakeHandler,),
        {"response_text": response_text, "ttft_delay_ms": delay_ms},
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@contextlib.contextmanager
def _example_providers():
    """Bring up the two upstreams the example's base URLs point at."""
    routes = _example_model()["route"]
    ports = [int(urlparse(route["base_url"]).port) for route in routes]
    servers = []
    try:
        for port, (text, delay) in zip(
            ports, [("ROUTED_TO_PREMIUM", 0.0), ("ROUTED_TO_BUDGET", 400.0)], strict=True
        ):
            try:
                servers.append(_start_example_fixture(port, text, delay))
            except OSError as exc:  # pragma: no cover - environment, not logic
                # Deliberately not a skip. These ports are named by the shipped
                # example, and a skip here is indistinguishable from a pass: the
                # guard silently stops guarding on exactly the machines where
                # something else is already listening.
                raise RuntimeError(
                    f"port {port} is in use, so the RouteWise example's upstream "
                    f"cannot be started and its guarantees cannot be checked. "
                    f"Free it and re-run: {exc}"
                ) from exc
        yield
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Serves the three Chutes endpoints the real fetcher calls, nothing else.

    Only the wire is fake. `_parse_chutes_usage`, `_fetch_chutes_daily_cap`
    and `_fetch_chutes_request_counts` all run for real against these
    payloads, so a change in how any of them parses breaks the test.
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = routes
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url: str, **_kwargs):
        self.requested.append(url)
        if url not in self._routes:
            return _FakeResponse(404, {})
        return _FakeResponse(200, self._routes[url])


def _chutes_wire_payloads(daily_cap: int, requests_today: int) -> dict[str, object]:
    """Responses shaped like the endpoints in serving/admin/provider_quotas.py."""
    now = datetime.now(timezone.utc)
    bucket = now.replace(minute=0, second=0, microsecond=0)
    return {
        "https://api.chutes.ai/users/me/subscription_usage": {
            "four_hour": {"usage": 0.5, "cap": 10.0},
            "monthly": {"usage": 3.0, "cap": 100.0},
        },
        "https://api.chutes.ai/users/me/quotas": [
            {"chute_id": "*", "quota": daily_cap},
        ],
        "https://api.chutes.ai/users/me/usage?limit=2000": {
            "items": [{"bucket": bucket.isoformat(), "count": requests_today}],
        },
    }


def _example_with_reference_routes_enabled(tmp_path: Path) -> Path:
    """The shipped example with its commented reference routes turned on.

    The quota route ships commented because it needs a real subscription. Its
    contract is still testable: uncomment it exactly as a reader would, and the
    registry, key discovery and fetcher all see what the example told them to.
    """
    text = _EXAMPLE.read_text()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "route:")
    out = lines[: start + 1]
    for line in lines[start + 1 :]:
        match = re.match(r"^      # ?(.*)$", line)
        if match:
            entry = match.group(1)
            if entry.startswith("- kind:") or (entry.startswith("  ") and entry.strip()):
                out.append("      " + entry)
                continue
            continue
        out.append(line)
    path = tmp_path / "models.routewise.enabled.yaml"
    path.write_text("\n".join(out) + "\n")
    return path


@pytest.mark.unit
class TestRouteWiseExampleQuotaCredentialChain:
    """The quota reference route, driven through everything but the wire.

    An earlier version of this guard asserted on strings and then injected a
    whole fake fetcher, so it stayed green when the example named a variable
    nothing reads. Here the only stub is `aiohttp.ClientSession`: registry
    registration, `_discover_provider_keys`, `fetch_chutes`, its three parsers,
    `_find_usage` and `QuotaPool` all run for real.
    """

    async def test_example_quota_route_reaches_a_ready_pool(self, tmp_path, monkeypatch):
        route = next(r for r in _commented_reference_routes() if r["provider_type"] == "quota")
        source = QuotaSource.from_raw(route["quota_source"])
        daily_cap = int(route["quota"]["limit"])

        # Only the variables the example itself names. A route that asks for a
        # variable nothing sets resolves to nothing and never registers.
        monkeypatch.setenv("CHUTES_BASE_URL", "https://llm.chutes.ai/v1")
        monkeypatch.setenv("CHUTES_API_KEY", "sk-chutes-test")
        monkeypatch.setenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1")
        monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-featherless-test")

        fixed = FixedRouter()
        register_from_models_yaml(fixed, _example_with_reference_routes_enabled(tmp_path))
        adapters = fixed.routes["routewise-demo"].adapters
        kinds = {getattr(a.config, "kind", None) or a.config.provider for a, _w in adapters}
        assert source.provider in kinds, (
            f"no {source.provider!r} route registered from the example: {sorted(kinds)}. "
            "Either the kind or the credential variable names something that does "
            "not resolve, and the quota fetcher would never see this route."
        )

        session = _FakeSession(_chutes_wire_payloads(daily_cap, requests_today=7))
        monkeypatch.setattr(
            provider_quotas.aiohttp,
            "ClientSession",
            lambda *a, **k: session,
        )

        # The real fetcher, discovering the real key from the real env var.
        store = ProviderQuotaSnapshotStore()
        assert source.provider in store._fetchers, (
            f"{source.provider!r} has no registered fetcher; RouteWise registers "
            f"{sorted(store._fetchers)}"
        )
        await store.refresh_once([source])

        assert session.requested, "the real fetcher never reached the wire"
        snapshot = store.get(source)
        assert snapshot is not None, (
            f"the example documents quota_source {(source.provider, source.usage_label, source.unit)}, "
            f"but nothing in the fetcher's output matched it. Requested: {session.requested}"
        )
        assert snapshot.limit == float(daily_cap)
        assert snapshot.used == 7.0

        pool = QuotaPool(
            store,
            source,
            policy=QuotaPolicy.from_raw(route["quota"], context="quota"),
        )
        assert pool.ready is True


# Booted in a subprocess so each budget_alpha gets a genuinely fresh process:
# latency profiles, probe tasks and router state are all process-local, so an
# in-process second boot would inherit the first one's measurements and prove
# nothing about the restart the README tells the reader to perform.
_BOOT_AND_ASK = """
import json, os, sys, time

from fastapi.testclient import TestClient

from serving.servers.app import app

deadline = time.monotonic() + float(os.environ["DEMO_DEADLINE_SEC"])
served = None
error = None
# TestClient as a context manager runs the real lifespan: bootstrap builds the
# routers, starts them, and the RouteWise probe loop begins polling upstreams.
with TestClient(app) as client:
    models = client.get("/v1/models")
    if models.status_code != 200 or not models.json().get("data"):
        print(json.dumps({"served": None, "error": f"/v1/models {models.status_code}"}))
        sys.exit(0)
    while time.monotonic() < deadline:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "routewise-demo", "messages": [{"role": "user", "content": "hi"}]},
        )
        if response.status_code != 200:
            error = f"completion {response.status_code}: {response.text[:200]}"
            time.sleep(0.5)
            continue
        served = response.json()["choices"][0]["message"]["content"]
        error = None
        if served == os.environ["DEMO_EXPECT"]:
            break
        # Before the first probe cycle both endpoints are unprofiled and cost
        # breaks the tie; keep asking until the measurements land.
        time.sleep(0.5)
print(json.dumps({"served": served, "error": error}))
"""


def _boot_and_ask(config: Path, expect: str, deadline_sec: float = 25.0) -> dict:
    """Boot the real app against `config` and return what upstream answered."""
    env = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT / "apps" / "backend"),
        "MODELS_CONFIG_PATH": str(config),
        "ROUTING_CONFIG_PATH": str(_REPO_ROOT / "config" / "examples" / "routing.minimal.yaml"),
        "DB_ENABLED": "false",
        "USER_AUTH_ENABLED": "false",
        "DEMO_EXPECT": expect,
        "DEMO_DEADLINE_SEC": str(deadline_sec),
        # A loopback test must not be routed through an ambient proxy.
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    for proxy_var in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        env.pop(proxy_var, None)
    proc = subprocess.run(
        [sys.executable, "-c", _BOOT_AND_ASK],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=deadline_sec + 60,
    )
    assert proc.returncode == 0, f"boot failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _example_with_alpha(tmp_path: Path, alpha: float, probe_interval: float) -> Path:
    """The shipped example with only budget_alpha and the probe interval moved.

    The interval is shortened so the test does not sit through the deployment
    default; whether probing works at all is what is under test, and the
    shipped value is asserted separately.
    """
    text = _EXAMPLE.read_text()
    text = text.replace("      budget_alpha: 0.0", f"      budget_alpha: {alpha}", 1)
    text = text.replace(
        "      routewise_probe_interval_sec: 5.0",
        f"      routewise_probe_interval_sec: {probe_interval}",
        1,
    )
    path = tmp_path / f"models.alpha{alpha}.yaml"
    path.write_text(text)
    return path


@pytest.mark.integration
class TestRouteWiseExampleProbeLifecycle:
    """The README's demo, run the way the README tells a reader to run it.

    Not `run_probe_once` and a private selector: the real app boots through its
    own lifespan, the background probe loop it starts is the only thing that
    can measure the upstreams, and the answer is read off an HTTP completion.
    A broken probe task, a lost fixture delay, a database requirement or a
    dispatch regression all turn this red.
    """

    def test_shipped_example_enables_probing_on_a_sane_interval(self):
        params = _example_model()["router_params"]
        assert params["routewise_probe_enabled"] is True
        assert params["routewise_probe_idle_only"] is False
        assert 0 < float(params["routewise_probe_interval_sec"]) <= 30.0

    def test_alpha_zero_serves_budget_and_a_restart_at_one_serves_premium(self, tmp_path):
        with _example_providers():
            cheap = _boot_and_ask(
                _example_with_alpha(tmp_path, 0.0, probe_interval=1.0),
                expect="ROUTED_TO_BUDGET",
            )
            assert cheap["error"] is None, cheap["error"]
            assert cheap["served"] == "ROUTED_TO_BUDGET", (
                f"budget_alpha=0.0 must not spend above the cheapest provider, got {cheap}"
            )

            # A separate process: the restart the README asks for, with none of
            # the first run's measurements carried over.
            fast = _boot_and_ask(
                _example_with_alpha(tmp_path, 1.0, probe_interval=1.0),
                expect="ROUTED_TO_PREMIUM",
            )
            assert fast["error"] is None, fast["error"]
            assert fast["served"] == "ROUTED_TO_PREMIUM", (
                "budget_alpha=1.0 should buy the faster provider once the probe "
                f"has measured both, got {fast}"
            )


@pytest.mark.integration
class TestExampleFixtureLatencyContract:
    """The fixture's `--ttft-delay-ms` is what makes the demo demonstrate anything.

    Asserted directly rather than through RouteWise: with no delay the two
    upstreams differ only by jitter, and the routing test can still pick the
    faster one by luck. That would leave a silently pointless demo behind a
    green suite.
    """

    def test_ttft_delay_actually_delays_the_first_byte(self):
        import urllib.request

        delay_ms = 400.0
        immediate = _start_example_fixture(18361, "IMMEDIATE", 0.0)
        delayed = _start_example_fixture(18362, "DELAYED", delay_ms)
        try:

            def _timed(port: int) -> float:
                body = json.dumps(
                    {"model": "example-upstream", "messages": [{"role": "user", "content": "hi"}]}
                ).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                started = time.monotonic()
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                return (time.monotonic() - started) * 1000.0

            fast_ms = _timed(18361)
            slow_ms = _timed(18362)
        finally:
            for server in (immediate, delayed):
                server.shutdown()
                server.server_close()

        assert slow_ms - fast_ms > delay_ms * 0.5, (
            f"--ttft-delay-ms is not delaying the response: {fast_ms:.0f} ms vs "
            f"{slow_ms:.0f} ms. The RouteWise example's two upstreams would then "
            "differ only by jitter, and its cost/latency demo would prove nothing."
        )
