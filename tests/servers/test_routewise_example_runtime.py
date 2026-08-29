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
from routing.routewise.candidates import QuotaSource
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
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


def _start_example_fixture(response_text: str, delay_ms: float):
    """Run the example's own provider fixture on a port the OS hands us.

    Port 0 and then `server_port`: reserving a port and releasing it before
    binding leaves a window in which a concurrent job -- four runner services
    share each CI host -- or even the next call in this same test can take it.
    Holding the listening socket from the start removes the window entirely.
    """
    spec = importlib.util.spec_from_file_location(
        f"_example_fixture_{id(response_text):x}",
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
        f"_Handler{id(response_text):x}",
        (module.FakeHandler,),
        {"response_text": response_text, "ttft_delay_ms": delay_ms},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_port)


def _rebind_example_to(text: str, ports: list[int]) -> str:
    """Point the shipped example's upstreams at ports this process owns.

    The example hardcodes 18351/18352 because a reader copy-pasting the README
    needs URLs that already work. A test cannot take those ports: four runner
    services share each CI host and `concurrency:` is keyed on the ref, so two
    unrelated pull requests can be running this file at the same moment.
    Binding them anyway would turn somebody else's CI red; skipping on a busy
    port would turn the guard off exactly there. So the test rewrites the URLs
    and asserts it rewrote precisely the lines it meant to -- the substitution
    tracks the shipped file, and a route that moves or is renamed is caught
    here rather than silently untested.
    """
    lines = text.splitlines()
    rewritten, hits = [], 0
    for line in lines:
        match = re.match(r"^(\s*base_url: http://127\.0\.0\.1:)(\d+)(/v1\s*)$", line)
        if match and hits < len(ports):
            rewritten.append(f"{match.group(1)}{ports[hits]}{match.group(3)}")
            hits += 1
        else:
            rewritten.append(line)
    assert hits == len(ports), (
        f"expected {len(ports)} loopback base_url lines in the example, rewrote {hits}. "
        "The routes moved, and this test is no longer driving what it thinks it is."
    )
    return "\n".join(rewritten) + "\n"


def _start_fixture_cli(script: Path, response_text: str, delay_ms: float | None):
    """Launch the fixture the way the README does, and wait for it to answer.

    Port 0 makes the OS choose; the fixture prints the address it bound, which
    is also how a reader would confirm it came up.
    """
    argv = [
        sys.executable,
        str(script),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--response-text",
        response_text,
    ]
    if delay_ms is not None:
        argv += ["--ttft-delay-ms", str(delay_ms)]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    banner = process.stdout.readline()
    match = re.search(r"127\.0\.0\.1:(\d+)", banner)
    if match is None:  # pragma: no cover - the fixture failed to start
        process.terminate()
        raise RuntimeError(f"fixture did not announce its port: {banner!r}")
    return process, int(match.group(1))


@contextlib.contextmanager
def _example_providers(tmp_path: Path):
    """Start the two upstreams and yield an example config pointed at them."""
    routes = _example_model()["route"]
    assert len(routes) == 2, "the demo needs exactly two upstreams to choose between"
    for route in routes:
        parsed = urlparse(route["base_url"])
        assert parsed.hostname == "127.0.0.1", (
            f"the example must stay runnable with no account: {route['base_url']}"
        )

    servers, ports = [], []
    try:
        for text, delay in (("ROUTED_TO_PREMIUM", 0.0), ("ROUTED_TO_BUDGET", 400.0)):
            server, port = _start_example_fixture(text, delay)
            servers.append(server)
            ports.append(port)
        config = tmp_path / "models.routewise.bound.yaml"
        config.write_text(_rebind_example_to(_EXAMPLE.read_text(), ports))
        yield config
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

    def __init__(self, routes: dict[str, object], *, expected_key: str) -> None:
        self._routes = routes
        self._expected_key = expected_key
        self.requested: list[str] = []
        self.seen_keys: set[str] = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url: str, **kwargs):
        self.requested.append(url)
        # The fetcher must arrive holding the route's own key. Accepting any
        # Authorization header would let the quota chain pass while wired to a
        # credential the route never declared.
        supplied = (kwargs.get("headers") or {}).get("Authorization", "")
        self.seen_keys.add(supplied)
        if supplied != f"Bearer {self._expected_key}":
            return _FakeResponse(401, {"error": "unauthorized"})
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

        route_key = "sk-chutes-from-the-route"

        # Only the variables the example itself names. A route that asks for a
        # variable nothing sets resolves to nothing and never registers.
        monkeypatch.setenv("CHUTES_BASE_URL", "https://llm.chutes.ai/v1")
        monkeypatch.setenv("CHUTES_API_KEY", route_key)
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

        # Take the variable away now that registration is done. Whatever the
        # fetcher finds from here has to come from the pool the route itself
        # registered -- which is the only thing that ties the credential the
        # gateway infers with to the credential it accounts quota against.
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)

        session = _FakeSession(
            _chutes_wire_payloads(daily_cap, requests_today=7), expected_key=route_key
        )
        monkeypatch.setattr(
            provider_quotas.aiohttp,
            "ClientSession",
            lambda *a, **k: session,
        )

        # The router's own pools, not a hand-built stand-in: this is the object
        # that decides at route time whether the quota leg is eligible.
        router = RouteWiseRouter(route_table=fixed, config=RouteWiseConfig())
        pool_id = route["quota_pool"]
        assert pool_id in router.quota_pools, (
            f"the router built no quota pool for {pool_id!r}: {sorted(router.quota_pools)}"
        )
        pool = router.quota_pools[pool_id]
        assert pool.source == source, f"pool bound to {pool.source}, example documents {source}"
        assert pool.ready is False, "a pool cannot be ready before its first snapshot lands"

        store = router.quota_snapshots
        assert source.provider in store._fetchers, (
            f"{source.provider!r} has no registered fetcher; RouteWise registers "
            f"{sorted(store._fetchers)}"
        )
        await store.refresh_once([source])

        assert session.requested, "the real fetcher never reached the wire"
        assert session.seen_keys == {f"Bearer {route_key}"}, (
            f"the fetcher authenticated with {session.seen_keys}, not the key the "
            f"example's route declares. Inference and quota accounting would then "
            "be running on different credentials."
        )
        snapshot = store.get(source)
        assert snapshot is not None, (
            f"the example documents quota_source {(source.provider, source.usage_label, source.unit)}, "
            f"but nothing in the fetcher's output matched it. Requested: {session.requested}"
        )
        assert snapshot.limit == float(daily_cap)
        assert snapshot.used == 7.0

        assert pool.ready is True, (
            "the router's quota pool never became ready, so RouteWise would keep "
            "skipping this provider as unpriceable"
        )
        assert pool.remaining == daily_cap - 7


# Booted in a subprocess so each budget_alpha gets a genuinely fresh process:
# latency profiles, probe tasks and router state are all process-local, so an
# in-process second boot would inherit the first one's measurements and prove
# nothing about the restart the README tells the reader to perform.
_BOOT_AND_ASK = """
import json, os, sys, time
from collections import Counter

from fastapi.testclient import TestClient

from serving.servers.app import app

expect = os.environ["DEMO_EXPECT"]
settle = int(os.environ["DEMO_SETTLE_REQUESTS"])
deadline = time.monotonic() + float(os.environ["DEMO_DEADLINE_SEC"])
error = None


def ask(client):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "routewise-demo", "messages": [{"role": "user", "content": "hi"}]},
    )
    if response.status_code != 200:
        return None, f"completion {response.status_code}: {response.text[:200]}"
    return response.json()["choices"][0]["message"]["content"], None


# TestClient as a context manager runs the real lifespan: bootstrap builds the
# routers, starts them, and the RouteWise probe loop begins polling upstreams.
with TestClient(app) as client:
    models = client.get("/v1/models")
    if models.status_code != 200 or not models.json().get("data"):
        print(json.dumps({"settled": None, "error": f"/v1/models {models.status_code}"}))
        sys.exit(0)

    # Before the first probe cycle both endpoints are unprofiled and cost breaks
    # the tie, so wait for the measurements rather than judging the cold answer.
    seen = None
    while time.monotonic() < deadline:
        seen, error = ask(client)
        if seen == expect:
            break
        time.sleep(0.5)

    # Then require the choice to be STABLE. One sighting proves nothing: a mixed
    # policy lands on both providers, so a single hit passes at any alpha.
    counts = Counter()
    for _ in range(settle):
        served, error = ask(client)
        if error:
            break
        counts[served] += 1

print(json.dumps({"settled": dict(counts), "first_seen": seen, "error": error}))
"""


def _boot_and_ask(
    config: Path, expect: str, deadline_sec: float = 25.0, settle_requests: int = 8
) -> dict:
    """Boot the real app against `config` and report where a batch of requests went."""
    env = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT / "apps" / "backend"),
        "MODELS_CONFIG_PATH": str(config),
        "ROUTING_CONFIG_PATH": str(_REPO_ROOT / "config" / "examples" / "routing.minimal.yaml"),
        "DB_ENABLED": "false",
        "USER_AUTH_ENABLED": "false",
        "DEMO_EXPECT": expect,
        "DEMO_DEADLINE_SEC": str(deadline_sec),
        "DEMO_SETTLE_REQUESTS": str(settle_requests),
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


def _example_with_alpha(source: Path, tmp_path: Path, alpha: float, probe_interval: float) -> Path:
    """`source` with budget_alpha and the probe interval set, and verified set.

    Written through the YAML rather than by text substitution: a `str.replace`
    that misses -- because the shipped default moved -- leaves both variants
    identical, and a mixed-alpha policy still lands on each provider often
    enough to satisfy a test that only looks for one hit. The interval is
    shortened so the suite does not sit through the deployment default; the
    shipped value is asserted separately.
    """
    document = yaml.safe_load(source.read_text())
    params = document["models"][0]["router_params"]
    params["budget_alpha"] = alpha
    params["routewise_probe_interval_sec"] = probe_interval

    path = tmp_path / f"models.alpha{alpha}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))

    written = yaml.safe_load(path.read_text())["models"][0]["router_params"]
    assert written["budget_alpha"] == alpha, written
    assert written["routewise_probe_interval_sec"] == probe_interval, written
    assert written["routewise_probe_enabled"] is True, (
        "the rendered config must keep probing on, or alpha cannot matter"
    )
    return path


def _assert_settled_on(result: dict, expected: str, *, alpha: float) -> None:
    """Every settled request must have gone to one provider, not merely some."""
    assert result["error"] is None, result["error"]
    settled = result["settled"] or {}
    assert settled, f"no settled requests at budget_alpha={alpha}: {result}"
    assert set(settled) == {expected}, (
        f"budget_alpha={alpha} should serve every request from {expected}, but the "
        f"settled batch split {settled}. A mixture means the budget sits between "
        "the two providers rather than pinned to one end."
    )


@pytest.mark.integration
class TestRouteWiseExampleProbeLifecycle:
    """The README's demo, run the way the README tells a reader to run it.

    Not `run_probe_once` and a private selector: the real app boots through its
    own lifespan, the background probe loop it starts is the only thing that
    can measure the upstreams, and the answer is read off an HTTP completion.
    A broken probe task, a lost fixture delay, a database requirement or a
    dispatch regression all turn this red.
    """

    def test_shipped_example_matches_what_the_readme_promises(self):
        """The behavioural test sets alpha itself, so the default needs its own guard."""
        params = _example_model()["router_params"]
        assert params["budget_alpha"] == 0.0, (
            "README.md tells the reader the example ships budget_alpha: 0.0 and that "
            f"every reply is ROUTED_TO_BUDGET; the example says {params['budget_alpha']}"
        )
        assert params["routewise_probe_enabled"] is True
        assert params["routewise_probe_idle_only"] is False
        assert 0 < float(params["routewise_probe_interval_sec"]) <= 30.0

    def test_alpha_zero_serves_budget_and_a_restart_at_one_serves_premium(self, tmp_path):
        with _example_providers(tmp_path) as bound:
            cheap = _boot_and_ask(
                _example_with_alpha(bound, tmp_path, 0.0, probe_interval=1.0),
                expect="ROUTED_TO_BUDGET",
            )
            _assert_settled_on(cheap, "ROUTED_TO_BUDGET", alpha=0.0)

            # A separate process: the restart the README asks for, with none of
            # the first run's measurements carried over.
            fast = _boot_and_ask(
                _example_with_alpha(bound, tmp_path, 1.0, probe_interval=1.0),
                expect="ROUTED_TO_PREMIUM",
            )
            _assert_settled_on(fast, "ROUTED_TO_PREMIUM", alpha=1.0)


@pytest.mark.integration
class TestExampleFixtureLatencyContract:
    """The fixture's `--ttft-delay-ms` is what makes the demo demonstrate anything.

    Asserted directly rather than through RouteWise: with no delay the two
    upstreams differ only by jitter, and the routing test can still pick the
    faster one by luck. That would leave a silently pointless demo behind a
    green suite.
    """

    def test_ttft_delay_actually_delays_the_first_byte(self):
        """Driven through the CLI the README tells the reader to type.

        Setting the handler attribute in-process would leave `--ttft-delay-ms`
        itself — the argparse wiring and the class assignment behind it —
        untested, and that flag is the public surface here.
        """
        import urllib.request

        delay_ms = 400.0
        fixture = (
            _REPO_ROOT
            / "distributions"
            / "example"
            / "fixtures"
            / "fake-openai-provider"
            / "server.py"
        )
        immediate, fast_port = _start_fixture_cli(fixture, "IMMEDIATE", None)
        delayed, slow_port = _start_fixture_cli(fixture, "DELAYED", delay_ms)
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

            fast_ms = _timed(fast_port)
            slow_ms = _timed(slow_port)
        finally:
            for process in (immediate, delayed):
                process.terminate()
                process.wait(timeout=10)

        assert slow_ms - fast_ms > delay_ms * 0.5, (
            f"--ttft-delay-ms is not delaying the response: {fast_ms:.0f} ms vs "
            f"{slow_ms:.0f} ms. The RouteWise example's two upstreams would then "
            "differ only by jitter, and its cost/latency demo would prove nothing."
        )
