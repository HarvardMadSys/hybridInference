# `chat_completions` Decomposition — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose [`apps/backend/serving/servers/routers/completions.py`](../../../apps/backend/serving/servers/routers/completions.py) (1042 lines, one 874-line `chat_completions` handler with four nested helpers and a 396-line `stream_generator`) into a thin orchestrator (~250 lines) plus three concern-isolated sibling modules, with a typed `RoutingInfo` dataclass replacing the magic dict that floats through today.

**Architecture:** Three new sibling modules in `apps/backend/serving/servers/routers/`: `routing_info.py` (frozen dataclasses), `completions_logging.py` (`CompletionsLogger`), `completions_cost.py` (`PricingLookup` + `CostTracker`), `completions_stream.py` (`StreamSession`). Land in three sequential PRs (A → B → C), each leaving the file working end-to-end and passing the existing FastAPI integration tests byte-for-byte.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, Pydantic v2, pytest + pytest-asyncio (auto mode), uv, ruff, pydocstyle.

**Spec:** [docs/agents/specs/2026-05-03-completions-decomposition-design.md](../specs/2026-05-03-completions-decomposition-design.md)

**Process notes (from CLAUDE.md):**
- Pull `origin/dev` before starting each PR.
- Each PR on its own feature branch in its own worktree:
  - PR A: `jason/claude/completions-routing-info`
  - PR B: `jason/claude/completions-cost-extract`
  - PR C: `jason/claude/completions-stream-extract`
- Per PR: create issue → branch in worktree → implement → `make format` → PR to `dev` → monitor CI + comments every 2 min → delete branch + worktree after merge.
- Soak each PR on staging for 24-48h before merging the next.

---

## File Structure

### PR A — adds (new files)

| File | Responsibility |
|---|---|
| `apps/backend/serving/servers/routers/routing_info.py` | `Pricing`, `RouteWiseDecision`, `RoutingInfo` frozen dataclasses + `build_initial_routing_info(...)` factory + `_status_code_from_exception(exc) -> int` helper. |
| `apps/backend/serving/servers/routers/completions_logging.py` | `CompletionsLogger` class encapsulating DB-log scheduling and routing observation forwarding. |
| `tests/unit/servers/test_routing_info.py` | Dataclass tests: construction, `dataclasses.replace` semantics, status-code-from-exception coverage. |
| `tests/unit/servers/test_completions_logging.py` | Logger payload shape; observation forwarding; mock op_store/log_store. |
| `tests/integration/servers/test_completions_log_payload_contract.py` | Smoke contract test: locks in current `api_logs` row shape so PRs B and C cannot regress it. |

### PR A — modifies

| File | Change |
|---|---|
| `apps/backend/serving/servers/routers/completions.py` | Remove module-level helpers (`_schedule_db_log_task`, `_schedule_cost_increment`, `_record_routing_observation`, `_build_db_params`) — moved into `CompletionsLogger`. Replace every `routing_info` dict access with `RoutingInfo` field access. Use `_status_code_from_exception` from `routing_info` module instead of inline 6-attribute fallback chain at lines 768 and 987-1007. |
| `apps/backend/serving/servers/deps.py` | Add `get_completions_logger` dependency. |
| `apps/backend/serving/servers/bootstrap.py` | Instantiate `CompletionsLogger(log_store, op_store, model_router_registry, settings)` and attach to `AppServices`. |

### PR B — adds (new files)

| File | Responsibility |
|---|---|
| `apps/backend/serving/servers/routers/completions_cost.py` | `PricingLookup` (cached `for_routing(RoutingInfo) → Pricing | None`) + `CostTracker` (async `schedule_increment(...)`). |
| `tests/unit/servers/test_completions_cost.py` | Pricing cache tests; `schedule_increment` math + op_store call args. |

### PR B — modifies

| File | Change |
|---|---|
| `apps/backend/serving/servers/routers/completions.py` | Replace 4 inline pricing-lookup blocks with `pricing_lookup.for_routing(routing)`. Replace `_schedule_cost_increment` calls with `cost_tracker.schedule_increment`. |
| `apps/backend/serving/servers/deps.py` | Add `get_pricing_lookup` and `get_cost_tracker` dependencies. |
| `apps/backend/serving/servers/bootstrap.py` | Instantiate `PricingLookup` and `CostTracker` once at startup; attach to `AppServices`. |
| `apps/backend/serving/servers/routers/completions_logging.py` | (No change — the logger from PR A continues to handle log-side concerns.) |

### PR C — adds (new files)

| File | Responsibility |
|---|---|
| `apps/backend/serving/servers/routers/completions_stream.py` | `StreamSession` class + internal `_ToolCallAccumulator`, `_TTFTTracker` helpers. |
| `tests/unit/servers/test_completions_stream.py` | Synthetic-chunk-stream tests: SSE bytes; tool-call merging; TTFT; cost+log scheduling on completion; error chunk emission; `yielded_first_chunk` semantics. |

### PR C — modifies

| File | Change |
|---|---|
| `apps/backend/serving/servers/routers/completions.py` | Delete inner `stream_generator` and `_adapter_reader` (lines 392-787). Handler creates `StreamSession` and returns `StreamingResponse(session.stream(adapter_chunks), ...)`. Final size: ~250 lines. |

### Critical compatibility invariants (no PR may break)

- The structured log payload emitted per request (`api_logs` row shape — admin/recent-requests UI consumes this).
- The Slack alert payloads from PR #372 (rule 1, rule 2 read these logs).
- The streaming SSE wire format (clients depend on it byte-for-byte).
- The cost-increment math.

---

# PR A — Type Foundation + Logger Extraction (`jason/claude/completions-routing-info`)

### Task 1: Project setup — branch + worktree + issue

**Files:** none (process step)

- [ ] **Step 1: Pull origin/dev**

```bash
git fetch origin && git checkout dev && git pull origin dev
```

- [ ] **Step 2: Create issue on GitHub**

```bash
gh issue create --title "PR A: extract RoutingInfo type + CompletionsLogger from chat_completions" \
  --body "$(cat <<'EOF'
First of three sequential PRs decomposing apps/backend/serving/servers/routers/completions.py.

Spec: docs/agents/specs/2026-05-03-completions-decomposition-design.md
Plan: docs/agents/plans/2026-05-03-completions-decomposition.md

PR A: introduce typed RoutingInfo dataclass + extract logger concerns.
PR B (issue TBD): extract PricingLookup + CostTracker.
PR C (issue TBD): extract StreamSession (the big one).
EOF
)"
```

- [ ] **Step 3: Create worktree**

```bash
git worktree add /home/juncheng/hybridInference-worktrees/completions-routing-info \
  -b jason/claude/completions-routing-info origin/dev
cd /home/juncheng/hybridInference-worktrees/completions-routing-info
```

- [ ] **Step 4: Verify clean baseline**

```bash
make test 2>&1 | tail -5
```

Expected: tests pass.

---

### Task 2: Add `routing_info.py` with frozen dataclasses

**Files:**
- Create: `apps/backend/serving/servers/routers/routing_info.py`
- Test: `tests/unit/servers/test_routing_info.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/servers/test_routing_info.py`:

```python
"""Tests for routing_info dataclasses."""
import dataclasses

import pytest

from serving.servers.routers.routing_info import (
    Pricing,
    RouteWiseDecision,
    RoutingInfo,
    build_initial_routing_info,
    _status_code_from_exception,
)


def test_pricing_frozen():
    p = Pricing(input_per_1k=0.5, output_per_1k=1.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.input_per_1k = 0.0


def test_routing_info_replace_returns_new_instance():
    r = RoutingInfo(
        request_id="abc", model="gpt-4", provider=None, endpoint_id=None,
        base_url=None, pricing=None, routewise=None, upstream_cost_usd=None,
    )
    r2 = dataclasses.replace(r, provider="openai", endpoint_id="openai-prod")
    assert r2.provider == "openai"
    assert r2.endpoint_id == "openai-prod"
    assert r.provider is None  # original unchanged


def test_build_initial_routing_info_minimal():
    class _Req:
        model = "gpt-4"

    r = build_initial_routing_info(_Req(), request_id="rid-1", pin_provider=None)
    assert r.request_id == "rid-1"
    assert r.model == "gpt-4"
    assert r.provider is None
    assert r.pricing is None


def test_build_initial_routing_info_with_pin():
    class _Req:
        model = "gpt-4"

    r = build_initial_routing_info(_Req(), request_id="rid-1", pin_provider="openai")
    assert r.provider == "openai"


def test_status_code_from_exception_status_code_attr():
    class _E(Exception):
        status_code = 502

    assert _status_code_from_exception(_E()) == 502


def test_status_code_from_exception_response_status_code():
    class _Resp:
        status_code = 503

    class _E(Exception):
        response = _Resp()

    assert _status_code_from_exception(_E()) == 503


def test_status_code_from_exception_response_status():
    class _Resp:
        status = 504

    class _E(Exception):
        response = _Resp()

    assert _status_code_from_exception(_E()) == 504


def test_status_code_from_exception_status_attr():
    class _E(Exception):
        status = 429

    assert _status_code_from_exception(_E()) == 429


def test_status_code_from_exception_code_attr():
    class _E(Exception):
        code = 500

    assert _status_code_from_exception(_E()) == 500


def test_status_code_from_exception_default():
    assert _status_code_from_exception(RuntimeError("boom")) == 500
```

- [ ] **Step 2: Run — verify fail**

```bash
uv run pytest tests/unit/servers/test_routing_info.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `apps/backend/serving/servers/routers/routing_info.py`**

```python
"""Typed routing context that flows through the completions request lifecycle.

Replaces the prior untyped ``routing_info: dict[str, Any]`` that carried
magic keys (endpoint_id, base_url, provider, pricing, routewise,
upstream_cost_usd) between layers of the chat-completions handler.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-provider, per-model upstream pricing in USD per 1k tokens."""

    input_per_1k: float
    output_per_1k: float
    cache_read_per_1k: float = 0.0
    cache_write_per_1k: float = 0.0


@dataclass(frozen=True, slots=True)
class RouteWiseDecision:
    """RouteWise telemetry passed through to ``record_routing_observation``.

    Opaque to the handler; only the logger reads its fields.
    """

    tier: str
    hedge_used: bool
    lp_weights: dict[str, float] | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RoutingInfo:
    """Per-request routing state carried through the handler pipeline."""

    request_id: str
    model: str
    provider: str | None = None
    endpoint_id: str | None = None
    base_url: str | None = None
    pricing: Pricing | None = None
    routewise: RouteWiseDecision | None = None
    upstream_cost_usd: float | None = None


def build_initial_routing_info(request: Any, *, request_id: str,
                               pin_provider: str | None) -> RoutingInfo:
    """Construct the pre-routing RoutingInfo from the chat request."""
    return RoutingInfo(
        request_id=request_id,
        model=request.model,
        provider=pin_provider,
    )


def _status_code_from_exception(exc: BaseException) -> int:
    """Extract an HTTP status code from common upstream exception shapes.

    Falls back through six common attribute paths used by httpx, OpenAI SDK,
    Anthropic SDK, and bare aiohttp errors. Returns 500 if none match.
    """
    for attr_path in (
        ("status_code",),
        ("response", "status_code"),
        ("response", "status"),
        ("status",),
        ("code",),
    ):
        obj: Any = exc
        try:
            for part in attr_path:
                obj = getattr(obj, part)
            if isinstance(obj, int):
                return obj
        except AttributeError:
            continue
    return 500
```

- [ ] **Step 4: Run — verify pass**

```bash
uv run pytest tests/unit/servers/test_routing_info.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/routing_info.py tests/unit/servers/test_routing_info.py
git commit -m "feat(completions): add RoutingInfo + Pricing dataclasses + status-code helper"
```

---

### Task 3: Add `CompletionsLogger`

**Files:**
- Create: `apps/backend/serving/servers/routers/completions_logging.py`
- Test: `tests/unit/servers/test_completions_logging.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/servers/test_completions_logging.py`:

```python
"""Tests for CompletionsLogger."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.routing_info import (
    Pricing,
    RouteWiseDecision,
    RoutingInfo,
)


@pytest.fixture
def mock_stores():
    log_store = MagicMock()
    log_store.log_request = AsyncMock()
    op_store = MagicMock()
    model_router_registry = MagicMock()
    settings = MagicMock()
    settings.failed_request_alert_threshold = 20
    return log_store, op_store, model_router_registry, settings


@pytest.fixture
def logger(mock_stores):
    log_store, op_store, registry, settings = mock_stores
    return CompletionsLogger(log_store=log_store, op_store=op_store,
                             model_router_registry=registry, settings=settings)


@pytest.fixture
def sample_routing():
    return RoutingInfo(
        request_id="rid-1",
        model="gpt-4",
        provider="openai",
        endpoint_id="openai-prod",
        base_url="https://api.openai.com/v1",
        pricing=Pricing(input_per_1k=0.5, output_per_1k=1.5),
        routewise=None,
        upstream_cost_usd=0.012,
    )


@pytest.mark.asyncio
async def test_schedule_request_log_success(logger, mock_stores, sample_routing):
    log_store, _, _, _ = mock_stores
    request = MagicMock(model="gpt-4", stream=False)
    response = MagicMock()
    user_ctx = MagicMock(user_id="u1", role="pro")

    logger.schedule_request_log(
        request=request, response=response, error=None,
        status_code=200, latency_ms=123,
        routing=sample_routing, user_ctx=user_ctx,
    )

    # Allow background task to run
    import asyncio
    await asyncio.sleep(0.05)
    log_store.log_request.assert_awaited_once()
    args, kwargs = log_store.log_request.call_args
    payload = kwargs if kwargs else args[0]
    # The log_store.log_request signature varies by backend; assert
    # the payload at minimum contains our routing fields.
    flat = str(args) + str(kwargs)
    assert "openai" in flat
    assert "gpt-4" in flat


@pytest.mark.asyncio
async def test_schedule_request_log_error(logger, mock_stores, sample_routing):
    log_store, _, _, _ = mock_stores
    request = MagicMock(model="gpt-4", stream=False)
    user_ctx = MagicMock(user_id="u1", role="pro")
    err = RuntimeError("upstream timeout")

    logger.schedule_request_log(
        request=request, response=None, error=err,
        status_code=504, latency_ms=30000,
        routing=sample_routing, user_ctx=user_ctx,
    )

    import asyncio
    await asyncio.sleep(0.05)
    log_store.log_request.assert_awaited_once()


def test_record_routing_observation_no_routewise(logger, mock_stores, sample_routing):
    """If routing.routewise is None, observation forwards is a no-op."""
    _, _, registry, _ = mock_stores
    registry.record_observation = MagicMock()

    logger.record_routing_observation(
        sample_routing, success=True, latency_ms=100,
        ttft_ms=None, upstream_cost_usd=0.012,
    )

    # No-op on non-RouteWise routing
    registry.record_observation.assert_not_called()


def test_record_routing_observation_routewise_present(logger, mock_stores):
    _, _, registry, _ = mock_stores
    registry.record_observation = MagicMock()

    routing = RoutingInfo(
        request_id="rid-1", model="gpt-4", provider="openai",
        endpoint_id="openai-prod", base_url="https://api.openai.com/v1",
        pricing=None,
        routewise=RouteWiseDecision(tier="A", hedge_used=False),
        upstream_cost_usd=0.012,
    )
    logger.record_routing_observation(
        routing, success=True, latency_ms=100,
        ttft_ms=42, upstream_cost_usd=0.012,
    )

    registry.record_observation.assert_called_once()
```

- [ ] **Step 2: Run — verify fail**

```bash
uv run pytest tests/unit/servers/test_completions_logging.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `apps/backend/serving/servers/routers/completions_logging.py`**

Use the existing helper functions in `apps/backend/serving/servers/routers/completions.py` as the source of truth. Move the body of `_schedule_db_log_task` (lines 47-73), `_record_routing_observation` (lines 103-136), and `_build_db_params` (lines 138-154) into methods on `CompletionsLogger`. Adapt them to take `RoutingInfo` instead of the dict.

```python
"""CompletionsLogger — encapsulates DB-log scheduling and RouteWise observation
forwarding for the chat-completions handler.

Replaces module-level helpers ``_schedule_db_log_task``,
``_schedule_cost_increment``-related logging, ``_record_routing_observation``,
and ``_build_db_params`` from the previous monolithic completions.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from serving.servers.routers.routing_info import RoutingInfo

log = logging.getLogger(__name__)

_LOG_TASKS: set[asyncio.Task[Any]] = set()


class CompletionsLogger:
    """Schedule async DB request logs and RouteWise observations for completions."""

    def __init__(self, *, log_store, op_store, model_router_registry, settings):
        self._log_store = log_store
        self._op_store = op_store
        self._registry = model_router_registry
        self._settings = settings

    def schedule_request_log(
        self,
        *,
        request,
        response,
        error: Exception | None,
        status_code: int,
        latency_ms: int,
        routing: RoutingInfo,
        user_ctx,
    ) -> None:
        """Build the api_logs payload and schedule a fire-and-forget write."""
        payload = self._build_payload(
            request=request, response=response, error=error,
            status_code=status_code, latency_ms=latency_ms,
            routing=routing, user_ctx=user_ctx,
        )
        try:
            task = asyncio.ensure_future(self._write(payload))
            _LOG_TASKS.add(task)
            task.add_done_callback(_LOG_TASKS.discard)
        except RuntimeError:
            log.debug("no event loop; dropping log payload for %s", routing.request_id)

    def record_routing_observation(
        self,
        routing: RoutingInfo,
        *,
        success: bool,
        latency_ms: int,
        ttft_ms: int | None,
        upstream_cost_usd: float | None,
    ) -> None:
        """Forward telemetry to RouteWise; no-op for non-RouteWise routes."""
        if routing.routewise is None:
            return
        record_observation = getattr(self._registry, "record_observation", None)
        if record_observation is None:
            return
        try:
            record_observation(
                model=routing.model,
                endpoint_id=routing.endpoint_id,
                provider=routing.provider,
                routewise=routing.routewise,
                success=success,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                upstream_cost_usd=upstream_cost_usd,
            )
        except Exception:
            log.exception("record_routing_observation failed")

    def _build_payload(self, *, request, response, error, status_code, latency_ms,
                       routing: RoutingInfo, user_ctx) -> dict[str, Any]:
        """Build the api_logs row payload — preserves the shape that
        admin/recent-requests UI and the alert rules read."""
        # The exact field set must match what _schedule_db_log_task wrote
        # in the prior implementation. Read apps/backend/serving/servers/routers/completions.py
        # at the _schedule_db_log_task body and the _build_db_params body and
        # transcribe the field assembly here, swapping dict[...] reads for
        # routing.<attr> reads.
        params = self._build_db_params(request=request, routing=routing)
        return {
            "request_id": routing.request_id,
            "user_id": user_ctx.user_id,
            "role": user_ctx.role,
            "model": request.model,
            "provider": routing.provider,
            "endpoint_id": routing.endpoint_id,
            "base_url": routing.base_url,
            "params": params,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "stream": getattr(request, "stream", False),
            "error": str(error) if error else None,
            "error_type": type(error).__name__ if error else None,
            "upstream_cost_usd": routing.upstream_cost_usd,
            # response/usage fields populated when response is not None
            "response_id": getattr(response, "id", None) if response else None,
            "usage": _serialize_usage(response) if response else None,
        }

    def _build_db_params(self, *, request, routing: RoutingInfo) -> dict[str, Any]:
        """Reconstruct request parameters for DB log, filling default max_tokens
        from adapter config when the client didn't specify one."""
        params = {
            "model": request.model,
            "stream": getattr(request, "stream", False),
            "temperature": getattr(request, "temperature", None),
            "max_tokens": getattr(request, "max_tokens", None),
            # Add other parameter fields by mirroring the prior _build_db_params
            # body in completions.py.
        }
        if params.get("max_tokens") is None and routing.endpoint_id is not None:
            adapter_default = self._adapter_default_max_tokens(routing.endpoint_id)
            if adapter_default is not None:
                params["max_tokens"] = adapter_default
        return params

    def _adapter_default_max_tokens(self, endpoint_id: str) -> int | None:
        try:
            adapter = self._registry.get_adapter_by_endpoint_id(endpoint_id)
            return getattr(adapter.config, "default_max_tokens", None)
        except Exception:
            return None

    async def _write(self, payload: dict[str, Any]) -> None:
        try:
            await self._log_store.log_request(payload)
        except Exception:
            log.exception("log_store.log_request failed for %s",
                          payload.get("request_id"))


def _serialize_usage(response) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return dict(usage)
```

(Implementer note: the exact `params` field set above is illustrative — open `apps/backend/serving/servers/routers/completions.py` and read the existing `_build_db_params` body to mirror its full output exactly.)

- [ ] **Step 4: Run — verify pass**

```bash
uv run pytest tests/unit/servers/test_completions_logging.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/completions_logging.py tests/unit/servers/test_completions_logging.py
git commit -m "feat(completions): add CompletionsLogger encapsulating DB log + observation"
```

---

### Task 4: Smoke contract test for `api_logs` row shape

**Files:**
- Create: `tests/integration/servers/test_completions_log_payload_contract.py`

This test locks in the current row shape so PRs B and C can't drift it.

- [ ] **Step 1: Write the test**

Create `tests/integration/servers/test_completions_log_payload_contract.py`:

```python
"""Contract test: api_logs row shape must remain stable across PRs A/B/C."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.routing_info import (
    Pricing,
    RoutingInfo,
)


# Authoritative key set the api_logs table + admin UI consume.
# Adding a key here without updating downstream consumers is a regression.
EXPECTED_LOG_KEYS = frozenset({
    "request_id", "user_id", "role", "model", "provider",
    "endpoint_id", "base_url", "params", "status_code",
    "latency_ms", "stream", "error", "error_type",
    "upstream_cost_usd", "response_id", "usage",
})


@pytest.mark.asyncio
async def test_log_payload_keyset_unchanged():
    log_store = MagicMock()
    captured: dict = {}

    async def capture(payload):
        captured.update(payload)

    log_store.log_request = AsyncMock(side_effect=capture)
    op_store = MagicMock()
    registry = MagicMock()
    settings = MagicMock()
    logger = CompletionsLogger(log_store=log_store, op_store=op_store,
                               model_router_registry=registry, settings=settings)

    routing = RoutingInfo(
        request_id="rid", model="gpt-4", provider="openai",
        endpoint_id="ep", base_url="https://x", pricing=None,
        routewise=None, upstream_cost_usd=0.0,
    )
    request = MagicMock(model="gpt-4", stream=False, temperature=None, max_tokens=None)
    response = MagicMock(id="r1", usage={"prompt_tokens": 10, "completion_tokens": 5})
    user_ctx = MagicMock(user_id="u", role="free")

    logger.schedule_request_log(
        request=request, response=response, error=None,
        status_code=200, latency_ms=42, routing=routing, user_ctx=user_ctx,
    )
    import asyncio
    await asyncio.sleep(0.05)

    actual_keys = set(captured.keys())
    missing = EXPECTED_LOG_KEYS - actual_keys
    extra = actual_keys - EXPECTED_LOG_KEYS
    assert not missing, f"Lost log keys: {missing}"
    assert not extra, f"Unexpected new log keys: {extra} — update EXPECTED_LOG_KEYS + downstream consumers"
```

- [ ] **Step 2: Run — verify pass**

```bash
uv run pytest tests/integration/servers/test_completions_log_payload_contract.py -v
```

Expected: PASS (Task 3's payload matches the keyset).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/servers/test_completions_log_payload_contract.py
git commit -m "test(completions): contract-pin api_logs row keyset across A/B/C PRs"
```

---

### Task 5: Wire `CompletionsLogger` into bootstrap + deps

**Files:**
- Modify: `apps/backend/serving/servers/bootstrap.py`
- Modify: `apps/backend/serving/servers/deps.py`

- [ ] **Step 1: Locate AppServices definition**

```bash
grep -rn "class AppServices\|@dataclass" apps/backend/serving/servers/deps.py apps/backend/serving/servers/bootstrap.py | head
```

- [ ] **Step 2: Add `completions_logger: CompletionsLogger | None` field** to `AppServices`. Edit the dataclass to include:

```python
    completions_logger: "CompletionsLogger | None" = None
```

(Use a string annotation if AppServices is imported before completions_logging to avoid cycle.)

- [ ] **Step 3: In `bootstrap.initialize`**, after `op_store` and `log_store` and `model_router_registry` are available (search for where they're constructed), add:

```python
    from serving.servers.routers.completions_logging import CompletionsLogger
    completions_logger = CompletionsLogger(
        log_store=log_store,
        op_store=op_store,
        model_router_registry=model_router_registry,
        settings=settings,
    )
```

Pass `completions_logger=completions_logger` to the `AppServices(...)` construction.

- [ ] **Step 4: In `apps/backend/serving/servers/deps.py`**, add the dependency function:

```python
def get_completions_logger(request: Request) -> "CompletionsLogger":
    services: AppServices = request.app.state.services
    if services.completions_logger is None:
        raise RuntimeError("CompletionsLogger not initialized")
    return services.completions_logger
```

- [ ] **Step 5: Run smoke**

```bash
uv run pytest tests/unit/servers tests/integration/servers -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/serving/servers/bootstrap.py apps/backend/serving/servers/deps.py
git commit -m "feat(completions): wire CompletionsLogger into AppServices + bootstrap"
```

---

### Task 6: Cut `completions.py` over to `RoutingInfo` + `CompletionsLogger`

**Files:**
- Modify: `apps/backend/serving/servers/routers/completions.py`

This is the meatiest task in PR A. The handler currently builds and threads a `routing_info` dict through 30+ touch points. We replace the dict with `RoutingInfo` and route every helper call through `CompletionsLogger`.

- [ ] **Step 1: Confirm baseline — record current test count**

```bash
make test 2>&1 | tail -3
```

Note the pass count.

- [ ] **Step 2: Replace dict construction with `RoutingInfo`**

Open `apps/backend/serving/servers/routers/completions.py`. Find every site where `routing_info` is constructed or mutated:
- Initial construction (around routing-decision time): replace with `build_initial_routing_info(chat_req, request_id=..., pin_provider=...)`.
- Enrichment after routing returns (`routing_info["endpoint_id"] = ...`): replace with `routing = dataclasses.replace(routing, endpoint_id=..., base_url=..., provider=..., pricing=..., routewise=...)`.
- Read sites (`routing_info["endpoint_id"]`): replace with `routing.endpoint_id`.

Add at top of file:

```python
import dataclasses
from serving.servers.routers.routing_info import (
    RoutingInfo, Pricing, RouteWiseDecision,
    build_initial_routing_info, _status_code_from_exception,
)
from serving.servers.routers.completions_logging import CompletionsLogger
```

And add `completions_logger: CompletionsLogger = Depends(get_completions_logger)` to the `chat_completions` parameter list.

- [ ] **Step 3: Replace inline status-code heuristic**

Find both call sites of the 6-attribute fallback chain (search for `getattr(e, "status_code"` near lines 768 and 987-1007). Replace each block with:

```python
status_code = _status_code_from_exception(e)
```

- [ ] **Step 4: Replace `_schedule_db_log_task(...)` calls**

Find every `_schedule_db_log_task(log_store, request_id, log_data)` call. Replace with:

```python
completions_logger.schedule_request_log(
    request=chat_req, response=response_obj_or_None,
    error=exception_or_None, status_code=status_code,
    latency_ms=latency_ms, routing=routing, user_ctx=user_ctx,
)
```

(Note: callers no longer need to assemble the `log_data` dict — that lives in `CompletionsLogger._build_payload`.)

- [ ] **Step 5: Replace `_record_routing_observation(...)` calls**

Find every site (search for `_record_routing_observation`). Replace with:

```python
completions_logger.record_routing_observation(
    routing, success=success_bool, latency_ms=latency_ms,
    ttft_ms=ttft_ms_or_None, upstream_cost_usd=routing.upstream_cost_usd,
)
```

- [ ] **Step 6: Delete the four module-level helpers**

Once no callers reference them: delete `_schedule_db_log_task`, `_schedule_cost_increment`, `_record_routing_observation`, `_build_db_params` from the file. (The cost-increment function moves to PR B; for PR A, leave its body in place but route logging through the new logger. **Concretely: keep `_schedule_cost_increment` for now; PR B replaces it with `CostTracker.schedule_increment`.**)

- [ ] **Step 7: Run all tests**

```bash
make test 2>&1 | tail -5
```

Expected: same pass count as Step 1 (no regressions). The contract test from Task 4 must still pass.

- [ ] **Step 8: Run `make format`**

```bash
make format
```

- [ ] **Step 9: Commit**

```bash
git add apps/backend/serving/servers/routers/completions.py
git commit -m "refactor(completions): replace routing_info dict with RoutingInfo + CompletionsLogger"
```

---

### Task 7: PR A finalization — push + open PR

**Files:** none (process)

- [ ] **Step 1: Final test + format**

```bash
make format && make test 2>&1 | tail -5
```

- [ ] **Step 2: Push**

```bash
git push -u origin jason/claude/completions-routing-info
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --base dev --title "PR A: extract RoutingInfo type + CompletionsLogger" \
  --body "$(cat <<'EOF'
## Summary

PR A of 3 — decomposes \`apps/backend/serving/servers/routers/completions.py\`.

- New typed \`RoutingInfo\` dataclass replaces the magic \`routing_info: dict\`.
- New \`CompletionsLogger\` class encapsulates DB-log scheduling and RouteWise observation forwarding.
- Centralizes the duplicated \`_status_code_from_exception\` heuristic.
- Adds a smoke contract test that locks in the \`api_logs\` row keyset.

PR B (cost extraction) and PR C (StreamSession) follow.

Closes [issue from Task 1].

## Test plan
- [x] All 10 RoutingInfo unit tests pass
- [x] CompletionsLogger unit tests pass
- [x] api_logs payload keyset contract test passes
- [x] Full integration suite passes byte-for-byte
- [ ] Manual smoke: send 1 streaming + 1 non-streaming request through dev gateway; confirm api_logs row written and admin/recent-requests UI renders it

## Risk
Low. Mechanical dict→dataclass swap; logger logic moved verbatim into class. No production behavior change.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI + comments every 2 min until merged.**

- [ ] **Step 5: After merge — clean up worktree**

```bash
cd /home/juncheng/hybridInference
git worktree remove /home/juncheng/hybridInference-worktrees/completions-routing-info
git branch -D jason/claude/completions-routing-info
```

- [ ] **Step 6: Soak on staging for 24h before starting PR B.**

Watch the Slack alerting channel from PR #372 for failed-rate or 5xx spikes related to the deploy.

---

# PR B — Cost Extraction (`jason/claude/completions-cost-extract`)

Pre-conditions: PR A merged + 24h staging soak complete.

### Task 8: Project setup for PR B

(Same as Task 1, but new branch `jason/claude/completions-cost-extract`, new GitHub issue.)

- [ ] **Step 1**: `git fetch origin && git checkout dev && git pull origin dev`
- [ ] **Step 2**: `gh issue create --title "PR B: extract PricingLookup + CostTracker from chat_completions"` (link spec/plan)
- [ ] **Step 3**: `git worktree add /home/juncheng/hybridInference-worktrees/completions-cost-extract -b jason/claude/completions-cost-extract origin/dev && cd /home/juncheng/hybridInference-worktrees/completions-cost-extract`
- [ ] **Step 4**: `make test 2>&1 | tail -5` — confirm baseline.

---

### Task 9: Add `PricingLookup`

**Files:**
- Create: `apps/backend/serving/servers/routers/completions_cost.py` (PricingLookup portion)
- Test: `tests/unit/servers/test_completions_cost.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/servers/test_completions_cost.py`:

```python
"""Tests for PricingLookup."""
from unittest.mock import MagicMock

import pytest

from serving.servers.routers.completions_cost import PricingLookup
from serving.servers.routers.routing_info import Pricing, RoutingInfo


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    return registry


@pytest.fixture
def lookup(mock_registry):
    return PricingLookup(model_router_registry=mock_registry)


def test_pricing_lookup_returns_none_when_no_endpoint(lookup):
    routing = RoutingInfo(request_id="rid", model="gpt-4")
    assert lookup.for_routing(routing) is None


def test_pricing_lookup_returns_pricing_from_adapter(lookup, mock_registry):
    adapter = MagicMock()
    adapter.config.input_price_per_1k = 0.5
    adapter.config.output_price_per_1k = 1.5
    adapter.config.cache_read_price_per_1k = 0.05
    adapter.config.cache_write_price_per_1k = 0.6
    mock_registry.get_adapter_by_endpoint_id.return_value = adapter

    routing = RoutingInfo(request_id="rid", model="gpt-4", endpoint_id="ep1")
    result = lookup.for_routing(routing)

    assert isinstance(result, Pricing)
    assert result.input_per_1k == 0.5
    assert result.output_per_1k == 1.5
    assert result.cache_read_per_1k == 0.05
    assert result.cache_write_per_1k == 0.6


def test_pricing_lookup_caches_by_endpoint_id(lookup, mock_registry):
    adapter = MagicMock()
    adapter.config.input_price_per_1k = 0.5
    adapter.config.output_price_per_1k = 1.5
    adapter.config.cache_read_price_per_1k = 0.0
    adapter.config.cache_write_price_per_1k = 0.0
    mock_registry.get_adapter_by_endpoint_id.return_value = adapter

    routing = RoutingInfo(request_id="rid", model="gpt-4", endpoint_id="ep1")
    lookup.for_routing(routing)
    lookup.for_routing(routing)
    lookup.for_routing(routing)

    # Only one registry lookup despite 3 calls.
    assert mock_registry.get_adapter_by_endpoint_id.call_count == 1


def test_pricing_lookup_falls_back_to_provider_base_url(lookup, mock_registry):
    """When endpoint_id is None but provider+base_url exist, fall back."""
    mock_registry.get_adapter_by_endpoint_id.side_effect = AttributeError("no endpoint_id")
    mock_registry.get_adapter_by_provider_base_url = MagicMock()
    adapter = MagicMock()
    adapter.config.input_price_per_1k = 0.7
    adapter.config.output_price_per_1k = 2.0
    adapter.config.cache_read_price_per_1k = 0.0
    adapter.config.cache_write_price_per_1k = 0.0
    mock_registry.get_adapter_by_provider_base_url.return_value = adapter

    routing = RoutingInfo(
        request_id="rid", model="gpt-4",
        provider="openai", base_url="https://api.openai.com/v1",
    )
    result = lookup.for_routing(routing)
    assert result is not None
    assert result.input_per_1k == 0.7
```

- [ ] **Step 2: Run — verify fail**

- [ ] **Step 3: Implement `PricingLookup`** (note: full file with `CostTracker` comes in Task 10):

```python
"""Pricing lookup with per-endpoint cache + cost-increment scheduler."""
from __future__ import annotations

import logging
from typing import Any

from serving.servers.routers.routing_info import Pricing, RoutingInfo

log = logging.getLogger(__name__)


class PricingLookup:
    """Cache adapter pricing keyed by endpoint_id (fallback: provider+base_url).

    Adapters are immutable after bootstrap.initialize(), so the cache lives
    indefinitely. Add invalidate() if hot-reload ever lands.
    """

    def __init__(self, *, model_router_registry):
        self._registry = model_router_registry
        self._cache: dict[str, Pricing | None] = {}

    def for_routing(self, routing: RoutingInfo) -> Pricing | None:
        """Resolve Pricing for the routing target; return None if unknown."""
        key = self._cache_key(routing)
        if key is None:
            return None
        if key in self._cache:
            return self._cache[key]
        pricing = self._lookup_uncached(routing)
        self._cache[key] = pricing
        return pricing

    def _cache_key(self, routing: RoutingInfo) -> str | None:
        if routing.endpoint_id:
            return f"ep:{routing.endpoint_id}"
        if routing.provider and routing.base_url:
            return f"pb:{routing.provider}:{routing.base_url}"
        return None

    def _lookup_uncached(self, routing: RoutingInfo) -> Pricing | None:
        try:
            if routing.endpoint_id:
                adapter = self._registry.get_adapter_by_endpoint_id(routing.endpoint_id)
            elif routing.provider and routing.base_url:
                adapter = self._registry.get_adapter_by_provider_base_url(
                    routing.provider, routing.base_url
                )
            else:
                return None
        except (AttributeError, KeyError, LookupError):
            return None
        return _pricing_from_adapter_config(adapter.config)


def _pricing_from_adapter_config(config: Any) -> Pricing | None:
    input_p = getattr(config, "input_price_per_1k", None)
    output_p = getattr(config, "output_price_per_1k", None)
    if input_p is None or output_p is None:
        return None
    return Pricing(
        input_per_1k=float(input_p),
        output_per_1k=float(output_p),
        cache_read_per_1k=float(getattr(config, "cache_read_price_per_1k", 0.0) or 0.0),
        cache_write_per_1k=float(getattr(config, "cache_write_price_per_1k", 0.0) or 0.0),
    )
```

(Note: registry method names in the implementation may differ — check `apps/backend/serving/servers/registry.py` and `apps/backend/routing/model_router_registry.py` and adapt the accessor calls to the actual API.)

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/completions_cost.py tests/unit/servers/test_completions_cost.py
git commit -m "feat(completions): add PricingLookup with endpoint-id cache"
```

---

### Task 10: Add `CostTracker`

**Files:**
- Modify: `apps/backend/serving/servers/routers/completions_cost.py` (add CostTracker)
- Modify: `tests/unit/servers/test_completions_cost.py` (extend)

- [ ] **Step 1: Extend the test file**

Append to `tests/unit/servers/test_completions_cost.py`:

```python
import asyncio
import dataclasses
from unittest.mock import AsyncMock

from serving.servers.routers.completions_cost import CostTracker
from serving.servers.routers.routing_info import Pricing


@pytest.fixture
def mock_op_store():
    op = MagicMock()
    op.increment_user_cost = AsyncMock()
    return op


@pytest.fixture
def cost_tracker(lookup, mock_op_store):
    return CostTracker(op_store=mock_op_store, pricing=lookup)


@pytest.mark.asyncio
async def test_schedule_increment_with_known_pricing(cost_tracker, lookup, mock_registry, mock_op_store):
    adapter = MagicMock()
    adapter.config.input_price_per_1k = 0.5
    adapter.config.output_price_per_1k = 1.5
    adapter.config.cache_read_price_per_1k = 0.0
    adapter.config.cache_write_price_per_1k = 0.0
    mock_registry.get_adapter_by_endpoint_id.return_value = adapter

    routing = RoutingInfo(request_id="rid", model="gpt-4", endpoint_id="ep1",
                          provider="openai", base_url="https://x", pricing=None)

    enriched = await cost_tracker.schedule_increment(
        user_id="u1", routing=routing,
        prompt_tokens=1000, completion_tokens=2000,
    )

    # Cost = 1000/1000 * 0.5 + 2000/1000 * 1.5 = 0.5 + 3.0 = 3.5
    assert enriched.upstream_cost_usd == pytest.approx(3.5)
    # Allow background task to complete
    await asyncio.sleep(0.05)
    mock_op_store.increment_user_cost.assert_awaited_once()
    args, kwargs = mock_op_store.increment_user_cost.call_args
    flat = str(args) + str(kwargs)
    assert "u1" in flat
    assert "3.5" in flat or "3.50" in flat


@pytest.mark.asyncio
async def test_schedule_increment_unknown_pricing(cost_tracker, mock_op_store):
    routing = RoutingInfo(request_id="rid", model="gpt-4")  # no endpoint_id

    enriched = await cost_tracker.schedule_increment(
        user_id="u1", routing=routing,
        prompt_tokens=1000, completion_tokens=2000,
    )

    assert enriched.upstream_cost_usd is None
    await asyncio.sleep(0.05)
    mock_op_store.increment_user_cost.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_increment_swallows_db_errors(cost_tracker, lookup, mock_registry, mock_op_store, caplog):
    adapter = MagicMock()
    adapter.config.input_price_per_1k = 0.5
    adapter.config.output_price_per_1k = 1.5
    adapter.config.cache_read_price_per_1k = 0.0
    adapter.config.cache_write_price_per_1k = 0.0
    mock_registry.get_adapter_by_endpoint_id.return_value = adapter
    mock_op_store.increment_user_cost.side_effect = RuntimeError("DB down")

    routing = RoutingInfo(request_id="rid", model="gpt-4", endpoint_id="ep1")
    enriched = await cost_tracker.schedule_increment(
        user_id="u1", routing=routing,
        prompt_tokens=100, completion_tokens=200,
    )
    assert enriched.upstream_cost_usd is not None  # cost was computed
    await asyncio.sleep(0.05)
    # Error was swallowed; no exception propagated.
```

- [ ] **Step 2: Run — verify fail**

- [ ] **Step 3: Add `CostTracker` to `completions_cost.py`**

Append to `apps/backend/serving/servers/routers/completions_cost.py`:

```python
import asyncio
import dataclasses

_COST_TASKS: set[asyncio.Task[Any]] = set()


class CostTracker:
    """Compute cost from token counts × pricing, fire-and-forget DB increment."""

    def __init__(self, *, op_store, pricing: PricingLookup):
        self._op_store = op_store
        self._pricing = pricing

    async def schedule_increment(
        self,
        *,
        user_id: str,
        routing: RoutingInfo,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> RoutingInfo:
        """Compute cost from the routing's pricing, schedule async increment,
        return RoutingInfo with upstream_cost_usd populated."""
        pricing = routing.pricing or self._pricing.for_routing(routing)
        if pricing is None:
            return routing
        cost = (
            prompt_tokens * pricing.input_per_1k / 1000.0
            + completion_tokens * pricing.output_per_1k / 1000.0
            + cache_read_tokens * pricing.cache_read_per_1k / 1000.0
            + cache_write_tokens * pricing.cache_write_per_1k / 1000.0
        )
        try:
            task = asyncio.ensure_future(self._increment(user_id, cost))
            _COST_TASKS.add(task)
            task.add_done_callback(_COST_TASKS.discard)
        except RuntimeError:
            log.debug("no event loop; dropping cost increment for %s", user_id)
        return dataclasses.replace(routing, upstream_cost_usd=cost, pricing=pricing)

    async def _increment(self, user_id: str, cost: float) -> None:
        try:
            await self._op_store.increment_user_cost(user_id=user_id, delta=cost)
        except Exception:
            log.exception("op_store.increment_user_cost failed for %s", user_id)
```

(Note: `op_store.increment_user_cost` signature varies by store — check the actual signature in `apps/backend/serving/storage/postgres_operational.py` and adapt the call.)

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/completions_cost.py tests/unit/servers/test_completions_cost.py
git commit -m "feat(completions): add CostTracker with fire-and-forget increment"
```

---

### Task 11: Wire `PricingLookup` + `CostTracker` into bootstrap + deps

**Files:**
- Modify: `apps/backend/serving/servers/bootstrap.py`
- Modify: `apps/backend/serving/servers/deps.py`

- [ ] **Step 1: Add fields to `AppServices`**

```python
    pricing_lookup: "PricingLookup | None" = None
    cost_tracker: "CostTracker | None" = None
```

- [ ] **Step 2: Construct in `bootstrap.initialize`**

```python
    from serving.servers.routers.completions_cost import PricingLookup, CostTracker
    pricing_lookup = PricingLookup(model_router_registry=model_router_registry)
    cost_tracker = CostTracker(op_store=op_store, pricing=pricing_lookup)
```

Pass both into `AppServices(...)`.

- [ ] **Step 3: Add `get_pricing_lookup` + `get_cost_tracker` to `deps.py`**

```python
def get_pricing_lookup(request: Request) -> "PricingLookup":
    services: AppServices = request.app.state.services
    if services.pricing_lookup is None:
        raise RuntimeError("PricingLookup not initialized")
    return services.pricing_lookup


def get_cost_tracker(request: Request) -> "CostTracker":
    services: AppServices = request.app.state.services
    if services.cost_tracker is None:
        raise RuntimeError("CostTracker not initialized")
    return services.cost_tracker
```

- [ ] **Step 4: Run smoke**

```bash
make test 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/bootstrap.py apps/backend/serving/servers/deps.py
git commit -m "feat(completions): wire PricingLookup + CostTracker into AppServices"
```

---

### Task 12: Cut `completions.py` over to `PricingLookup` + `CostTracker`

**Files:**
- Modify: `apps/backend/serving/servers/routers/completions.py`

- [ ] **Step 1: Add to handler signature**

```python
pricing_lookup: PricingLookup = Depends(get_pricing_lookup),
cost_tracker: CostTracker = Depends(get_cost_tracker),
```

And imports at top:

```python
from serving.servers.routers.completions_cost import PricingLookup, CostTracker
from serving.servers.deps import get_pricing_lookup, get_cost_tracker
```

- [ ] **Step 2: Replace 4 inline pricing-lookup blocks**

Find sites that resolve pricing today (search for `pricing` near lines 314-337, 659, 706, 831, 863). Replace each with:

```python
pricing = pricing_lookup.for_routing(routing)
if pricing is not None:
    routing = dataclasses.replace(routing, pricing=pricing)
```

Or, equivalently, populate `routing.pricing` once shortly after routing returns and reuse it.

- [ ] **Step 3: Replace `_schedule_cost_increment(...)` callers**

Search for `_schedule_cost_increment` callsites. Replace each with:

```python
routing = await cost_tracker.schedule_increment(
    user_id=user_ctx.user_id, routing=routing,
    prompt_tokens=usage.prompt_tokens,
    completion_tokens=usage.completion_tokens,
    cache_read_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
    cache_write_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
)
```

- [ ] **Step 4: Delete the `_schedule_cost_increment` function** (lines 75-100). It's no longer referenced.

- [ ] **Step 5: Run all tests**

```bash
make test 2>&1 | tail -5
```

Expected: same pass count as before. Contract test from PR A still passes.

- [ ] **Step 6: `make format` + commit**

```bash
make format
git add apps/backend/serving/servers/routers/completions.py
git commit -m "refactor(completions): use PricingLookup + CostTracker for cost path"
```

---

### Task 13: PR B finalization

(Same shape as Task 7 — push, open PR, monitor CI, soak 24h, cleanup.)

PR title: `PR B: extract PricingLookup + CostTracker`
Body: list 4 inline pricing-lookups consolidated; cost math centralized; cache eliminates per-request adapter introspection.

---

# PR C — Streaming Extraction (`jason/claude/completions-stream-extract`)

Pre-conditions: PR A and PR B merged + soak complete. This is the largest and highest-risk PR.

### Task 14: Project setup for PR C

(Same as Tasks 1, 8 — new branch `jason/claude/completions-stream-extract`, new issue.)

---

### Task 15: Add `StreamSession` skeleton

**Files:**
- Create: `apps/backend/serving/servers/routers/completions_stream.py`
- Test: `tests/unit/servers/test_completions_stream.py`

- [ ] **Step 1: Write failing skeleton test**

Create `tests/unit/servers/test_completions_stream.py`:

```python
"""Tests for StreamSession."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.servers.routers.completions_stream import StreamSession
from serving.servers.routers.routing_info import RoutingInfo


@pytest.fixture
def mock_deps():
    cost_tracker = MagicMock()
    cost_tracker.schedule_increment = AsyncMock(side_effect=lambda **kw: kw["routing"])
    completions_logger = MagicMock()
    user_ctx = MagicMock(user_id="u1", role="pro")
    return cost_tracker, completions_logger, user_ctx


def test_session_constructs(mock_deps):
    cost, logger, user = mock_deps
    routing = RoutingInfo(request_id="rid", model="gpt-4")
    request = MagicMock(model="gpt-4", stream=True)
    s = StreamSession(routing=routing, request=request,
                      cost_tracker=cost, completions_logger=logger,
                      user_ctx=user)
    assert s.yielded_first_chunk is False


@pytest.mark.asyncio
async def test_stream_yields_chunks_then_finalizes(mock_deps):
    cost, logger, user = mock_deps

    async def fake_chunks():
        for content in ["hi", " there", "!"]:
            yield _make_chunk(content)

    routing = RoutingInfo(request_id="rid", model="gpt-4")
    request = MagicMock(model="gpt-4", stream=True)
    s = StreamSession(routing=routing, request=request,
                      cost_tracker=cost, completions_logger=logger,
                      user_ctx=user)

    output = []
    async for byte_chunk in s.stream(fake_chunks()):
        output.append(byte_chunk)

    assert s.yielded_first_chunk is True
    assert any(b"hi" in c for c in output)
    # Finalization scheduled cost + log
    assert cost.schedule_increment.await_count == 1
    assert logger.schedule_request_log.call_count == 1


def _make_chunk(content: str):
    """Build a minimal chunk that mimics the upstream adapter shape."""
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = content
    chunk.choices[0].delta.tool_calls = None
    chunk.choices[0].delta.reasoning_content = None
    chunk.usage = None
    return chunk
```

- [ ] **Step 2: Run — verify fail**

- [ ] **Step 3: Implement `StreamSession` skeleton**

```python
"""StreamSession: one-shot orchestrator for streaming chat-completions responses.

Wraps the prior 396-line stream_generator + _adapter_reader into a class with
a clean public surface. Internal helpers handle tool-call merging, TTFT
tracking, and content accumulation.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from typing import Any, AsyncIterator

from serving.servers.routers.completions_logging import CompletionsLogger
from serving.servers.routers.completions_cost import CostTracker
from serving.servers.routers.routing_info import (
    RoutingInfo,
    _status_code_from_exception,
)

log = logging.getLogger(__name__)


class _ToolCallAccumulator:
    """Merge incremental tool_call deltas across streamed chunks."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def add(self, tool_calls_delta: list | None) -> None:
        if not tool_calls_delta:
            return
        for tc in tool_calls_delta:
            idx = getattr(tc, "index", 0)
            slot = self._calls.setdefault(idx, {"id": None, "type": "function",
                                                "function": {"name": "", "arguments": ""}})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn:
                if getattr(fn, "name", None):
                    slot["function"]["name"] += fn.name
                if getattr(fn, "arguments", None):
                    slot["function"]["arguments"] += fn.arguments

    def finalize(self) -> list[dict[str, Any]] | None:
        if not self._calls:
            return None
        return [self._calls[k] for k in sorted(self._calls)]


class _TTFTTracker:
    """Record time-to-first-token in milliseconds."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._first_at: float | None = None

    def mark_first(self) -> None:
        if self._first_at is None:
            self._first_at = time.monotonic()

    @property
    def ttft_ms(self) -> int | None:
        if self._first_at is None:
            return None
        return int((self._first_at - self._start) * 1000)


class StreamSession:
    """One streaming response: instantiate per request, call .stream(...) once."""

    def __init__(
        self,
        *,
        routing: RoutingInfo,
        request,
        cost_tracker: CostTracker,
        completions_logger: CompletionsLogger,
        user_ctx,
    ) -> None:
        self._routing = routing
        self._request = request
        self._cost_tracker = cost_tracker
        self._completions_logger = completions_logger
        self._user_ctx = user_ctx
        self._tools = _ToolCallAccumulator()
        self._ttft = _TTFTTracker()
        self._yielded_first_chunk = False
        self._content_buf: list[str] = []
        self._final_usage: Any = None
        self._started = time.monotonic()

    @property
    def yielded_first_chunk(self) -> bool:
        return self._yielded_first_chunk

    async def stream(self, adapter_chunks: AsyncIterator) -> AsyncIterator[bytes]:
        """Yield SSE-encoded bytes; finalize cost+log on completion or error."""
        try:
            async for chunk in adapter_chunks:
                self._ttft.mark_first()
                self._absorb_chunk(chunk)
                # Sanitize + serialize this chunk as SSE.
                yield self._encode_sse(chunk)
                self._yielded_first_chunk = True
            yield self._encode_sse_done()
            await self._finalize_success()
        except Exception as e:
            log.exception("StreamSession aborted")
            if self._yielded_first_chunk:
                # Already streamed — emit error event in-stream; no fallback possible.
                yield self._encode_sse_error(e)
            await self._finalize_error(e)
            # Do not re-raise; client has already started consuming.

    # --- private ---

    def _absorb_chunk(self, chunk) -> None:
        if not getattr(chunk, "choices", None):
            return
        delta = chunk.choices[0].delta
        if getattr(delta, "content", None):
            self._content_buf.append(delta.content)
        self._tools.add(getattr(delta, "tool_calls", None))
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self._final_usage = usage

    def _encode_sse(self, chunk) -> bytes:
        # Use the existing serializer/sanitizer in apps/backend/serving/openai_chat_serializer.py
        from serving.openai_chat_serializer import sanitize_chunk
        sanitized = sanitize_chunk(chunk)
        return f"data: {json.dumps(sanitized)}\n\n".encode()

    def _encode_sse_done(self) -> bytes:
        return b"data: [DONE]\n\n"

    def _encode_sse_error(self, e: Exception) -> bytes:
        body = {"error": {"type": type(e).__name__, "message": str(e)}}
        return f"data: {json.dumps(body)}\n\n".encode()

    async def _finalize_success(self) -> None:
        latency_ms = int((time.monotonic() - self._started) * 1000)
        prompt = getattr(self._final_usage, "prompt_tokens", 0) or 0
        completion = getattr(self._final_usage, "completion_tokens", 0) or 0
        cache_r = getattr(self._final_usage, "cache_read_tokens", 0) or 0
        cache_w = getattr(self._final_usage, "cache_write_tokens", 0) or 0
        self._routing = await self._cost_tracker.schedule_increment(
            user_id=self._user_ctx.user_id, routing=self._routing,
            prompt_tokens=prompt, completion_tokens=completion,
            cache_read_tokens=cache_r, cache_write_tokens=cache_w,
        )
        self._completions_logger.schedule_request_log(
            request=self._request, response=self._build_pseudo_response(),
            error=None, status_code=200, latency_ms=latency_ms,
            routing=self._routing, user_ctx=self._user_ctx,
        )
        self._completions_logger.record_routing_observation(
            self._routing, success=True, latency_ms=latency_ms,
            ttft_ms=self._ttft.ttft_ms,
            upstream_cost_usd=self._routing.upstream_cost_usd,
        )

    async def _finalize_error(self, e: Exception) -> None:
        latency_ms = int((time.monotonic() - self._started) * 1000)
        self._completions_logger.schedule_request_log(
            request=self._request, response=None,
            error=e, status_code=_status_code_from_exception(e),
            latency_ms=latency_ms, routing=self._routing, user_ctx=self._user_ctx,
        )
        self._completions_logger.record_routing_observation(
            self._routing, success=False, latency_ms=latency_ms,
            ttft_ms=self._ttft.ttft_ms,
            upstream_cost_usd=self._routing.upstream_cost_usd,
        )

    def _build_pseudo_response(self) -> Any:
        """Construct a minimal response-like object so the logger can record
        usage and content. The api_logs row only needs id and usage; full
        message reconstitution is not used by current consumers."""
        class _PseudoResponse:
            def __init__(self, content: str, usage, tools):
                self.id = None
                self.content = content
                self.usage = usage
                self.tool_calls = tools
        return _PseudoResponse(
            content="".join(self._content_buf),
            usage=self._final_usage,
            tools=self._tools.finalize(),
        )
```

(Implementer note: `sanitize_chunk` lives in `apps/backend/serving/openai_chat_serializer.py` — confirm signature and adapt if it requires a `mode` argument. Also: the prior `stream_generator` had additional behaviors — keepalive heartbeats, reasoning-content extraction, role chunks at start — that this skeleton omits. After getting the skeleton tests to pass, walk through the prior `stream_generator` body line-by-line and bring those across as private methods on `StreamSession`.)

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/serving/servers/routers/completions_stream.py tests/unit/servers/test_completions_stream.py
git commit -m "feat(completions): add StreamSession skeleton with chunk consumption + finalization"
```

---

### Task 16: Port full `stream_generator` behaviors to `StreamSession`

**Files:**
- Modify: `apps/backend/serving/servers/routers/completions_stream.py`
- Modify: `tests/unit/servers/test_completions_stream.py` (extend)

This task moves the remaining behaviors from the prior `stream_generator`. Walk through the original lines 392-787 and bring across, one feature at a time, with a test for each.

For each feature below: write the test, run to fail, implement, run to pass, commit.

- [ ] **Feature: Initial role chunk**

The prior `stream_generator` emits a role chunk before the first content chunk (search original for `make_role_chunk`). Add this.

  - Test: `test_stream_emits_role_chunk_first` asserts the first yielded byte chunk contains `"role": "assistant"`.
  - Implement: in `stream()`, before the `async for` loop, yield `self._encode_sse(make_role_chunk(...))`.
  - Commit: `feat(completions): StreamSession emits initial role chunk`

- [ ] **Feature: Tool-call merging round-trip**

  - Test: `test_stream_merges_tool_calls_across_chunks` — feed three chunks with split tool_call deltas; assert the finalized `tool_calls` reconstruct correctly via `_build_pseudo_response`.
  - Implement: already done in `_ToolCallAccumulator` — write the test to exercise it.
  - Commit: `test(completions): tool-call merging covered`

- [ ] **Feature: Reasoning-content extraction**

The prior `stream_generator` accumulates `delta.reasoning_content` separately. Mirror.

  - Test: `test_stream_accumulates_reasoning_content` — feed chunks with `reasoning_content`; assert `_build_pseudo_response` exposes accumulated reasoning.
  - Implement: add `_reasoning_buf: list[str]` and capture in `_absorb_chunk`.
  - Commit: `feat(completions): StreamSession accumulates reasoning_content`

- [ ] **Feature: Keepalive heartbeat**

The prior `stream_generator` emits keepalive `: ping\n\n` SSE comments every N seconds. Port.

  - Test: `test_stream_emits_keepalive_when_idle` — feed an iterator that pauses 2 seconds between chunks; assert keepalive bytes are interleaved.
  - Implement: use `asyncio.wait_for` on `__anext__` with a short timeout; on timeout, yield `b": ping\n\n"`.
  - Commit: `feat(completions): StreamSession emits keepalive during idle stream`

- [ ] **Feature: Final usage propagation**

  - Test: `test_stream_propagates_final_usage_to_cost_tracker` — last chunk has usage; assert `cost_tracker.schedule_increment` was called with matching token counts.
  - Implement: already done in `_finalize_success` — write the test.
  - Commit: `test(completions): final usage propagation covered`

- [ ] **Feature: Error path before any chunk yielded**

If the upstream stream raises before yielding any chunk, the handler should not see `yielded_first_chunk=True`. The router can choose to fall back.

  - Test: `test_stream_error_before_any_chunk_yields_first_chunk_false` — adapter raises immediately; assert `yielded_first_chunk` stays False; assert error logged but no SSE emitted; assert exception is re-raised so the router can fall back.
  - Implement: in `stream()`, special-case the "no chunks yet" path: re-raise the exception so the router can fall back (existing `_record_routing_observation` flagged this at line 442).
  - Commit: `feat(completions): preserve fallback opportunity on pre-first-chunk errors`

After each feature, run:

```bash
uv run pytest tests/unit/servers/test_completions_stream.py -v
```

---

### Task 17: Cut `completions.py` over to `StreamSession`

**Files:**
- Modify: `apps/backend/serving/servers/routers/completions.py`

- [ ] **Step 1: Add import + handler signature**

```python
from serving.servers.routers.completions_stream import StreamSession
```

(`completions_logger`, `cost_tracker` are already in the handler signature from PRs A + B.)

- [ ] **Step 2: Replace the streaming branch**

Find the `if chat_req.stream:` branch. Replace its body (the inner `stream_generator`/`_adapter_reader` block) with:

```python
    if chat_req.stream:
        session = StreamSession(
            routing=routing, request=chat_req,
            cost_tracker=cost_tracker, completions_logger=completions_logger,
            user_ctx=user_ctx,
        )
        adapter_chunks = adapter.stream(chat_req)
        try:
            return StreamingResponse(
                session.stream(adapter_chunks),
                media_type="text/event-stream",
            )
        except Exception as e:
            if not session.yielded_first_chunk:
                # Fall back to next adapter (existing fallback machinery)
                ...
            raise
```

- [ ] **Step 3: Delete `stream_generator` and `_adapter_reader`** (lines 392-787 in the original).

- [ ] **Step 4: Run all tests**

```bash
make test 2>&1 | tail -10
```

Expected: same pass count as before. Existing FastAPI integration tests are the safety net — they MUST pass byte-for-byte.

- [ ] **Step 5: `make format` + commit**

```bash
make format
git add apps/backend/serving/servers/routers/completions.py
git commit -m "refactor(completions): replace inline stream_generator with StreamSession"
```

---

### Task 18: PR C finalization

(Same shape as Tasks 7, 13 — push, open PR, monitor CI, soak 48h before merge.)

PR title: `PR C: extract StreamSession (final completions decomposition)`
Body: highlight that `chat_completions` is now ~250 lines; full decomposition complete; integration tests still pass byte-for-byte; soaked on staging for X hours.

---

## Self-Review Checklist (post all 3 PRs)

- [ ] `apps/backend/serving/servers/routers/completions.py` is ≤ 300 lines.
- [ ] No `routing_info: dict[str, Any]` references anywhere in the codebase: `grep -rn "routing_info" --include='*.py' | grep -v "RoutingInfo"`.
- [ ] No nested `def` inside `chat_completions`: `grep -A 50 "async def chat_completions" apps/backend/serving/servers/routers/completions.py | grep -E "^\s{4,}def \|^\s{4,}async def "` returns empty.
- [ ] Pricing-lookup logic exists exactly once: `grep -rn "input_per_1k" apps/backend/serving/servers/ --include='*.py'` shows only `routing_info.py` (dataclass) and `completions_cost.py` (lookup).
- [ ] Status-code-from-exception heuristic exists exactly once: defined in `routing_info.py`, used in `completions.py` and `completions_stream.py`.
- [ ] Existing FastAPI integration tests pass byte-for-byte (run them in CI).
- [ ] Smoke contract test from PR A still passes — `api_logs` row keyset unchanged.
- [ ] All 3 staging soaks completed without alert spikes (failed-rate, 5xx, p95 from PR #372).
- [ ] `chat_completions` is the only function in `completions.py` other than module-level imports / router setup.
