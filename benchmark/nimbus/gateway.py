"""Experiment composition of the real HTTP completion handler and Nimbus router.

Only composition is experimental: the request schema, ModelRouterRegistry,
RouterProtocol, LeafBackend, OpenAI adapter and StreamSession are the repository's
normal implementation. Account auth is replaced by a one-run loopback token;
Postgres, production quota, alerts and other HTTP surfaces are not initialized.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import uuid
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp
from fastapi import FastAPI, HTTPException, Request

from benchmark.nimbus.budget import BudgetExceeded, BudgetLedger, TokenPrices
from benchmark.nimbus.workload import canonical_json, sha256_bytes
from routing.dependencies import RouterBuildDependencies
from routing.endpoint_health import EndpointHealthRegistry
from routing.model_router_registry import ModelRouterRegistry
from routing.routers import FixedRouter
from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter
from serving.http import AsyncHTTPClient
from serving.servers.auth import verify_api_key
from serving.servers.deps import AppServices
from serving.servers.middleware.request_id import RequestIdMiddleware
from serving.servers.routers import completions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import ASGIApp, Receive, Scope, Send

_client_request: ContextVar[str | None] = ContextVar("nimbus_client_request", default=None)
_attempt: ContextVar[Attempt | None] = ContextVar("nimbus_wire_attempt", default=None)


def parse_sse(frame: str) -> dict[str, Any] | None:
    """Extract a JSON SSE event; ignore comments, terminators and malformed data."""
    data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
    if not data or data == "[DONE]":
        return None
    try:
        result = json.loads(data)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def authoritative_counts(usage: dict[str, Any] | None) -> tuple[int, int, int] | None:
    """Return provider-reported total input/output and conservatively priced cache.

    Missing cache detail is charged at the uncached rate. Missing input or output
    never becomes zero. Adapter-generated fallback usage does not enter here.
    """
    if not usage:
        return None

    def count(value: Any) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    inputs = count(usage.get("prompt_tokens"))
    outputs = count(usage.get("completion_tokens"))
    hit = count(usage.get("prompt_cache_hit_tokens"))
    miss = count(usage.get("prompt_cache_miss_tokens"))
    if inputs is None and hit is not None and miss is not None:
        inputs = hit + miss
    details = usage.get("prompt_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        return None
    cached = hit if hit is not None else count(details.get("cached_tokens"))
    if hit is not None and miss is not None and inputs != hit + miss:
        return None
    nested_cached = count(details.get("cached_tokens"))
    if hit is not None and nested_cached is not None and hit != nested_cached:
        return None
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        if key in usage and count(usage[key]) is None:
            return None
    if "cached_tokens" in details and nested_cached is None:
        return None
    if inputs is None or outputs is None or (cached is not None and cached > inputs):
        return None
    return inputs, outputs, cached or 0


class Journal:
    """Append and fsync experiment events before proceeding past audit boundaries."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._files: dict[str, Any] = {}

    def append(self, name: str, value: Any, *, durable: bool = True) -> None:
        """Append a JSONL event; stream chunks are buffered until run close."""
        if name not in self._files:
            self._files[name] = (self.directory / name).open("x", encoding="utf-8")
        stream = self._files[name]
        stream.write(canonical_json(value) + "\n")
        if durable:
            stream.flush()
            os.fsync(stream.fileno())

    def write_json(self, name: str, value: Any) -> None:
        """Replace a current-state file atomically while keeping events append-only."""
        temporary = self.directory / (name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / name)

    def close(self) -> None:
        """Close only journals owned by this run."""
        for stream in self._files.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()


@dataclass
class Attempt:
    """One exact adapter invocation and at most one paid HTTP POST."""

    attempt_id: str
    client_request_id: str
    gateway_request_id: str | None
    route: str
    max_tokens: int
    start_s: float
    wire_s: float | None = None
    end_s: float | None = None
    response_status: int | None = None
    response_model: str | None = None
    response_id: str | None = None
    upstream_usage: dict[str, Any] | None = None
    final_usage: dict[str, Any] | None = None
    usage_counts: tuple[int, int, int] | None = None
    wire_payload_sha256: str | None = None
    wire_calls: int = 0
    saw_terminal: bool = False
    error: str | None = None
    budget: dict[str, Any] | None = None
    completion: asyncio.Event = field(default_factory=asyncio.Event)


class ExperimentRuntime:
    """Run-scoped ownership of audit records, attempt identities and cloud budget."""

    def __init__(
        self,
        config: dict[str, Any],
        run_id: str,
        ledger: BudgetLedger,
        journal: Journal,
        allowed_requests: set[str],
        gateway_token: str,
    ) -> None:
        self.config = config
        self.run_id = run_id
        self.ledger = ledger
        self.prices = TokenPrices(**config["prices"])
        self.journal = journal
        self.allowed_requests = allowed_requests
        self.gateway_token = gateway_token
        self.claimed_requests: set[str] = set()
        self.attempts: dict[str, list[Attempt]] = {}
        self.decisions: dict[str, list[dict[str, Any]]] = {}
        self.origin = time.monotonic()
        self.cloud_wire_count = 0
        self.http_clients: list[OneAttemptHTTP] = []

    def now(self) -> float:
        """Return seconds since the replay's monotonic origin."""
        return time.monotonic() - self.origin

    def decision_sink(self, event: dict[str, Any]) -> None:
        """Persist router-provided admission features and elapsed decision time."""
        self.journal.append("decisions.jsonl", {"observed_s": self.now(), **event})
        request_id = event.get("request_id")
        if request_id:
            self.decisions.setdefault(request_id, []).append(event)

    def begin(self, route: str, max_tokens: int, gateway_request_id: str | None) -> Attempt:
        """Associate a real router adapter call with the replay HTTP request."""
        client_id = _client_request.get()
        if client_id not in self.allowed_requests:
            raise RuntimeError("adapter call lacks an admitted experiment request")
        attempts = self.attempts.setdefault(client_id, [])
        attempt = Attempt(
            attempt_id=f"{self.run_id}.{uuid.uuid4().hex}",
            client_request_id=client_id,
            gateway_request_id=gateway_request_id,
            route=route,
            max_tokens=max_tokens,
            start_s=self.now(),
        )
        attempts.append(attempt)
        return attempt

    def reserve_and_dispatch(self, attempt: Attempt) -> None:
        """Fail closed before one cloud POST and record its dispatch durably."""
        if attempt.wire_calls:
            raise RuntimeError("a Nimbus attempt may issue only one HTTP POST")
        if attempt.route == "cloud":
            limit = self.config.get("replay", {}).get("max_cloud_attempts")
            if limit is not None and self.cloud_wire_count >= limit:
                raise BudgetExceeded("run max_cloud_attempts reached")
            attempt.budget = self.ledger.reserve(
                attempt_id=attempt.attempt_id,
                run_id=self.run_id,
                prices=self.prices,
                input_tokens_upper_bound=self.config["cloud"]["input_tokens_upper_bound"],
                max_output_tokens=attempt.max_tokens,
            )
            try:
                attempt.budget = self.ledger.mark_dispatched(attempt.attempt_id)
            except BaseException:
                self.ledger.cancel_before_dispatch(attempt.attempt_id)
                raise
            self.cloud_wire_count += 1
        attempt.wire_s = self.now()
        attempt.wire_calls += 1
        self.journal.append(
            "attempts.jsonl",
            {
                "event": "wire_dispatch",
                "attempt_id": attempt.attempt_id,
                "client_request_id": attempt.client_request_id,
                "gateway_request_id": attempt.gateway_request_id,
                "route": attempt.route,
                "wire_s": attempt.wire_s,
                "wire_payload_sha256": attempt.wire_payload_sha256,
                "max_tokens": attempt.max_tokens,
                "budget": attempt.budget,
            },
        )

    def finish(self, attempt: Attempt) -> None:
        """Settle final authoritative usage, otherwise retain the full liability."""
        if attempt.completion.is_set():
            return
        attempt.end_s = self.now()
        attempt.usage_counts = (
            authoritative_counts(attempt.final_usage) if attempt.saw_terminal else None
        )
        if attempt.route == "cloud" and attempt.wire_calls:
            if attempt.usage_counts is None:
                attempt.budget = self.ledger.mark_unknown(attempt.attempt_id)
            else:
                inputs, outputs, cached = attempt.usage_counts
                attempt.budget = self.ledger.settle(
                    attempt.attempt_id,
                    input_tokens=inputs,
                    output_tokens=outputs,
                    cached_input_tokens=cached,
                )
        self.journal.append(
            "attempts.jsonl",
            {
                "event": "attempt_end",
                **{key: value for key, value in vars(attempt).items() if key != "completion"},
            },
        )
        attempt.completion.set()

    async def close(self) -> None:
        """Close experiment-only upstream HTTP sessions."""
        for client in self.http_clients:
            await client.close()


class OneAttemptHTTP(AsyncHTTPClient):
    """Existing SSE parser with a single POST, no hidden retry and no redirects.

    The production HTTP reader transparently retries a connect-phase failure.
    This deliberately narrow benchmark seam replaces only opening the response;
    it preserves ``_iterate_response_body`` and all adapter normalization.
    """

    def __init__(self, runtime: ExperimentRuntime, route: str) -> None:
        super().__init__()
        self.runtime = runtime
        self.route = route

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Honor a configured cloud proxy while keeping local inference direct."""
        if self._session is None or self._session.closed:
            trust_env = self.route == "cloud" and self.runtime.config["cloud"].get(
                "trust_env_proxy", False
            )
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60, connect=15, sock_connect=15),
                connector=aiohttp.TCPConnector(limit=200, limit_per_host=50),
                trust_env=trust_env,
            )
        return self._session

    @staticmethod
    def bounded_timeout(timeout: aiohttp.ClientTimeout | None) -> aiohttp.ClientTimeout:
        """Bound connecting to 15 seconds without changing existing read/total limits."""
        current = timeout or aiohttp.ClientTimeout(total=None)

        def limit(value: float | None) -> float:
            return min(value, 15.0) if value is not None and value > 0 else 15.0

        return aiohttp.ClientTimeout(
            total=current.total,
            connect=limit(current.connect),
            sock_connect=limit(current.sock_connect),
            sock_read=current.sock_read,
            ceil_threshold=current.ceil_threshold,
        )

    async def stream_post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        mode: str = "sse",
    ) -> AsyncIterator[str]:
        """Open exactly one budgeted response and inspect raw authoritative usage."""
        attempt = _attempt.get()
        if attempt is None:
            raise RuntimeError("Nimbus HTTP POST lacks attempt ownership")
        if not isinstance(json, dict) or json.get("max_tokens") != attempt.max_tokens:
            raise RuntimeError("wire output cap does not match its budget reservation")
        if json.get("n", 1) != 1 or "max_completion_tokens" in json:
            raise RuntimeError("one completion with one explicit cap is required")
        attempt.wire_payload_sha256 = sha256_bytes(canonical_json(json).encode())
        session = await self._ensure_session()
        self.runtime.reserve_and_dispatch(attempt)
        async with session.post(
            url,
            json=json,
            headers=headers,
            timeout=self.bounded_timeout(timeout),
            allow_redirects=False,
        ) as response:
            attempt.response_status = response.status
            if 300 <= response.status < 400:
                raise RuntimeError("upstream redirects are disabled for experiment requests")
            async for frame in self._iterate_response_body(response, mode, url):
                event = parse_sse(frame)
                if event:
                    choices = event.get("choices") or []
                    finishes = any(choice.get("finish_reason") for choice in choices)
                    if isinstance(event.get("usage"), dict):
                        attempt.upstream_usage = event["usage"]
                        if finishes or (attempt.saw_terminal and not choices):
                            attempt.final_usage = event["usage"]
                    attempt.response_model = event.get("model") or attempt.response_model
                    attempt.response_id = event.get("id") or attempt.response_id
                    if finishes:
                        attempt.saw_terminal = True
                if frame.strip() == "data: [DONE]":
                    attempt.saw_terminal = True
                yield frame


class MeteredAdapter(OpenAICompatAdapter):
    """The normal adapter with explicit per-invocation audit and single-wire HTTP."""

    def __init__(self, config: ModelConfig, runtime: ExperimentRuntime, route: str) -> None:
        super().__init__(config)
        if config.api_keys:
            raise ValueError("Nimbus experiments do not permit key-pool rotation")
        self.runtime = runtime
        self.route = route
        self.http = OneAttemptHTTP(runtime, route)
        runtime.http_clients.append(self.http)

    async def stream_chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncIterator[str]:
        """Meter every invocation before the existing adapter executes it."""
        max_tokens = params.get("max_tokens")
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= self.config.max_output_length
        ):
            raise ValueError("every experiment request requires an explicit valid output cap")
        attempt = self.runtime.begin(self.route, max_tokens, params.get("request_id"))
        token = _attempt.set(attempt)
        try:
            async with aclosing(super().stream_chat_completion(messages, **params)) as stream:
                async for frame in stream:
                    yield frame
        except BaseException as exc:
            # Exception strings may include provider bodies; retain the type and
            # numeric HTTP status rather than risk persisting authentication data.
            attempt.error = type(exc).__name__
            if getattr(exc, "status", None) is not None:
                attempt.response_status = exc.status
            raise
        finally:
            try:
                self.runtime.finish(attempt)
            finally:
                _attempt.reset(token)

    async def chat_completion(
        self, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Refuse an unmetered alternate execution surface."""
        raise ValueError("Nimbus campaign supports streaming completions only")


class ExperimentContextMiddleware:
    """Carry a replay ID independently of the handler's generated request ID."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Preserve run ownership across real serving and streaming tasks."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        identifier = headers.get(b"x-nimbus-request-id", b"").decode("utf-8")
        token = _client_request.set(identifier or None)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_request.reset(token)


def validate_endpoint(endpoint: dict[str, Any], route: str) -> None:
    """Reject credential-bearing URLs and output-cap asymmetries before startup."""
    parsed = urlsplit(endpoint["base_url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{route}.base_url must be an HTTP(S) endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint URLs must not contain credentials, query strings or fragments")
    if not isinstance(endpoint.get("trust_env_proxy", False), bool):
        raise ValueError("trust_env_proxy must be a boolean")
    if route == "local" and endpoint.get("trust_env_proxy", False):
        raise ValueError("local inference must not use the environment proxy")
    if (
        route == "cloud"
        and parsed.scheme != "https"
        and parsed.hostname not in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("cloud credentials require HTTPS (except local test servers)")
    for name in ("context_length", "max_output_tokens"):
        value = endpoint.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{route}.{name} must be a positive integer")
    if route == "cloud" and endpoint.get("input_tokens_upper_bound") != endpoint["context_length"]:
        raise ValueError("cloud input_tokens_upper_bound must equal the configured context limit")


def create_experiment_app(runtime: ExperimentRuntime, api_key: str | None) -> FastAPI:
    """Compose the real completion router with explicitly scoped experiment services."""
    from routing.nimbus import NimbusPoolRegistry

    config = runtime.config
    model_id = config.get("model_id", "nimbus-dsv41")
    health = EndpointHealthRegistry()
    fixed = FixedRouter(health_registry=health)
    adapters = []
    for route in ("local", "cloud"):
        endpoint = config[route]
        validate_endpoint(endpoint, route)
        adapter_config = ModelConfig(
            id=model_id,
            name=model_id,
            provider=route,
            endpoint_id=f"nimbus-{route}",
            base_url=endpoint["base_url"],
            api_key=api_key if route == "cloud" else None,
            provider_model_id=endpoint["model"],
            provider_profile=endpoint.get("provider_profile", "default"),
            processor="default",
            context_length=endpoint["context_length"],
            max_output_length=endpoint["max_output_tokens"],
            supported_params=["temperature", "top_p", "max_tokens", "seed", "thinking"],
            supports_tools=True,
            extra_body=endpoint.get("extra_body", {}),
            chat_path=endpoint.get("chat_path"),
            include_usage_in_stream=True,
        )
        adapters.append((MeteredAdapter(adapter_config, runtime, route), 1.0))
    fixed.register_route(model_id, adapters)
    params = {
        "local_endpoint_id": "nimbus-local",
        "cloud_endpoint_id": "nimbus-cloud",
        "pool_id": "nimbus-tp4",
        "policy": config["policy"],
        "profile": config["profile"],
        "ttft_slo_s": config["slo"]["ttft_s"],
        "estimated_output_tokens": config["replay"].get("estimated_output_tokens", 128),
        "remote_input_cost_per_million": float(runtime.prices.input_cny_per_million),
        "remote_output_cost_per_million": float(runtime.prices.output_cny_per_million),
        "batch_window_s": config["replay"].get("dispatch_window_s", 0),
        "max_pending": config["replay"].get("max_pending", 4096),
        "cancel_grace_s": config["replay"].get("cancel_grace_s", 5),
    }
    dependencies = RouterBuildDependencies(
        health_registry=health,
        nimbus_pools=NimbusPoolRegistry(),
        nimbus_decision_sink=runtime.decision_sink,
    )
    registry = ModelRouterRegistry(
        {model_id: {"router": "nimbus", "router_params": params}},
        shared_fixed_router=fixed,
        dependencies=dependencies,
    )
    registry.get_router(model_id)  # Fail before serving if the strategy cannot bind.
    app = FastAPI(title="Nimbus experiment gateway", docs_url=None, redoc_url=None)
    app.state.services = AppServices(router=fixed, model_router_registry=registry)

    async def experiment_identity(request: Request) -> dict[str, Any]:
        expected = "Bearer " + runtime.gateway_token
        if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "run credential required")
        identifier = request.headers.get("x-nimbus-request-id")
        if identifier not in runtime.allowed_requests:
            raise HTTPException(400, "request is outside the immutable workload")
        if identifier in runtime.claimed_requests:
            raise HTTPException(409, "replay request IDs may not be reused")
        runtime.claimed_requests.add(identifier)
        return {
            "user_id": "nimbus-experiment",
            "role": "admin",
            "is_admin": True,
            "authenticated": True,
        }

    app.dependency_overrides[verify_api_key] = experiment_identity
    app.include_router(completions.router)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(ExperimentContextMiddleware)
    return app
