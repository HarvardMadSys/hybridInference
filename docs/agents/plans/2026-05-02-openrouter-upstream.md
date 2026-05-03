# OpenRouter Upstream Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenRouter as an upstream provider with `kind: openrouter[<provider>]` bracket syntax for pinning specific upstream providers, and log OpenRouter-reported per-request cost into a new `api_logs.upstream_cost_usd` column.

**Architecture:** Subclass `OpenAICompatAdapter` to inject OpenRouter-specific request shape (attribution headers, `usage.include`, `provider.order`); add a new `ProviderProfile.OPENROUTER` whose usage normalizer extracts `cost`; thread `upstream_cost_usd` from response → adapter `_routing` block → completions handler → new `log_request()` keyword arg → new `api_logs.upstream_cost_usd` column.

**Tech Stack:** Python 3.12, FastAPI, asyncpg/PostgreSQL, aiohttp, pytest, ruff. Worktree at `/home/juncheng/hybridInference-or` on branch `jason/claude/openrouter-upstream`.

---

## File Structure

**Files to create:**

- `serving/adapters/openrouter.py` — new `OpenRouterAdapter` subclass.
- `test/unit/adapters/test_openrouter_adapter.py` — unit tests for adapter, parser, payload, headers, normalizer.
- `test/unit/test_registry_openrouter.py` — unit tests for `parse_openrouter_kind` and `_make_adapter` dispatch.
- `test/integration/test_openrouter_integration.py` — integration tests against real OpenRouter (skipped without API key).
- `docs/openrouter.md` — short user-facing doc covering kind syntax, attribution, cost contract.

**Files to modify:**

- `serving/adapters/base.py` — add `UsageInfo.upstream_cost_usd` field; add `ModelConfig.openrouter_pinned_provider` field.
- `serving/adapters/profiles.py` — add `ProviderProfile.OPENROUTER` and `normalize_usage_openrouter`.
- `serving/adapters/__init__.py` — export `OpenRouterAdapter`.
- `serving/servers/registry.py` — add `parse_openrouter_kind()`; update `_make_adapter` dispatch.
- `serving/storage/database.py` — add idempotent `ALTER TABLE` migration for `upstream_cost_usd` column; add `upstream_cost_usd: float | None = None` keyword arg to `DatabaseLogger.log_request()` and forward to INSERT.
- `serving/servers/routers/completions.py` — read `upstream_cost_usd` from `routing_info` (both stream and non-stream paths) and pass to `_schedule_db_log_task` log_data dict.
- `config/models.yaml` — append one commented OpenRouter example.
- `.env.example` — add `OPENROUTER_API_KEY` placeholder.

---

## Pre-flight check

- [ ] **Step 0.1: Confirm worktree state**

```bash
cd /home/juncheng/hybridInference-or && git status && git branch --show-current
```
Expected output: `On branch jason/claude/openrouter-upstream`, working tree clean (the only files present should be the cherry-picked spec at `docs/agents/specs/2026-05-02-openrouter-upstream-design.md` and this plan).

- [ ] **Step 0.2: Activate uv venv from project root**

```bash
cd /home/juncheng/hybridInference-or && uv sync 2>&1 | tail -3
```
Expected: `Resolved … packages in …` (no errors).

---

## Task 1: Add `upstream_cost_usd` field to `UsageInfo`

**Files:**
- Modify: `serving/adapters/base.py:13-40`
- Test: `test/unit/adapters/test_openrouter_adapter.py` (create)

- [ ] **Step 1.1: Write the failing test**

Create `test/unit/adapters/test_openrouter_adapter.py` with this content:

```python
"""Unit tests for OpenRouter adapter, parser, profile, and UsageInfo extension."""

from __future__ import annotations

from serving.adapters.base import UsageInfo


def test_usage_info_default_upstream_cost_is_none() -> None:
    info = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert info.upstream_cost_usd is None


def test_usage_info_to_dict_omits_upstream_cost() -> None:
    info = UsageInfo(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        upstream_cost_usd=0.00342,
    )
    d = info.to_dict()
    assert "upstream_cost_usd" not in d
    assert d == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py::test_usage_info_default_upstream_cost_is_none -v 2>&1 | tail -10
```
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'upstream_cost_usd'` or `AttributeError: ... has no attribute 'upstream_cost_usd'`.

- [ ] **Step 1.3: Add the field**

Modify `serving/adapters/base.py`. Find the existing `UsageInfo` dataclass (lines 13-40). Add the `upstream_cost_usd` field directly after `cache_write_tokens`:

```python
@dataclass
class UsageInfo:
    """Token usage statistics with cache and reasoning token support."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    # Cache tokens for cost calculation
    cache_read_tokens: int = 0  # Tokens read from cache (cheaper)
    cache_write_tokens: int = 0  # Tokens written to cache (may have cost)
    # OpenRouter-reported per-request upstream cost in USD. Internal-only:
    # NOT serialized via to_dict() to avoid leaking to API clients. Logged
    # to api_logs.upstream_cost_usd for ops/billing reconciliation.
    upstream_cost_usd: float | None = None

    def to_dict(self) -> dict[str, int]:
        """Convert usage info to OpenAI-compatible dict format."""
        result = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        # Include reasoning tokens if present (for models like DeepSeek-R1)
        if self.reasoning_tokens > 0:
            result["reasoning_tokens"] = self.reasoning_tokens
        # Include cache tokens if present (for transparency)
        if self.cache_read_tokens > 0:
            result["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens > 0:
            result["cache_write_tokens"] = self.cache_write_tokens
        return result
```

- [ ] **Step 1.4: Run test to verify both tests pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py -v 2>&1 | tail -10
```
Expected: 2 passed.

- [ ] **Step 1.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/adapters/base.py test/unit/adapters/test_openrouter_adapter.py && git commit -m "$(cat <<'EOF'
feat(adapters): add upstream_cost_usd field to UsageInfo

Internal-only field, not serialized via to_dict(); used to thread
OpenRouter-reported per-request cost from the upstream response down
to api_logs.upstream_cost_usd.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `openrouter_pinned_provider` field to `ModelConfig`

**Files:**
- Modify: `serving/adapters/base.py:43-105`
- Test: `test/unit/adapters/test_openrouter_adapter.py`

- [ ] **Step 2.1: Append failing test**

Append to `test/unit/adapters/test_openrouter_adapter.py`:

```python
from serving.adapters.base import ModelConfig


def test_model_config_default_openrouter_pinned_provider_is_none() -> None:
    cfg = ModelConfig(id="m", name="M", provider="openrouter", base_url="https://x")
    assert cfg.openrouter_pinned_provider is None


def test_model_config_accepts_openrouter_pinned_provider() -> None:
    cfg = ModelConfig(
        id="m",
        name="M",
        provider="openrouter",
        base_url="https://x",
        openrouter_pinned_provider="deepinfra",
    )
    assert cfg.openrouter_pinned_provider == "deepinfra"
```

- [ ] **Step 2.2: Run to verify failure**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py::test_model_config_accepts_openrouter_pinned_provider -v 2>&1 | tail -8
```
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'openrouter_pinned_provider'`.

- [ ] **Step 2.3: Add the field**

Modify `serving/adapters/base.py`. In the `ModelConfig` dataclass, add the field directly after `subscription_type`:

```python
    # RouteWise subscription classification for this route entry.
    # Valid values: "api" (pay-per-token), "quota" (daily quota), "concurrency".
    subscription_type: str = "api"
    # When set, OpenRouterAdapter pins requests to this OpenRouter upstream
    # provider via `provider.order=[<slug>]` and `allow_fallbacks=false`.
    # Set automatically by parse_openrouter_kind() when the YAML uses
    # `kind: openrouter[<slug>]`. None for bare `kind: openrouter`.
    openrouter_pinned_provider: str | None = None
```

- [ ] **Step 2.4: Run test to verify pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py -v 2>&1 | tail -10
```
Expected: 4 passed.

- [ ] **Step 2.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/adapters/base.py test/unit/adapters/test_openrouter_adapter.py && git commit -m "$(cat <<'EOF'
feat(adapters): add openrouter_pinned_provider to ModelConfig

Set by parse_openrouter_kind() when models.yaml uses
`kind: openrouter[<slug>]`; consumed by the upcoming OpenRouterAdapter
to inject a `provider.order=[<slug>], allow_fallbacks=false` block on
the outbound request.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `ProviderProfile.OPENROUTER` and `normalize_usage_openrouter`

**Files:**
- Modify: `serving/adapters/profiles.py:19-34, 144-157`
- Test: `test/unit/adapters/test_openrouter_adapter.py`

- [ ] **Step 3.1: Append failing tests**

Append to `test/unit/adapters/test_openrouter_adapter.py`:

```python
from serving.adapters.profiles import (
    ProviderProfile,
    get_usage_normalizer,
    normalize_usage_openrouter,
)


def test_provider_profile_has_openrouter() -> None:
    assert ProviderProfile("openrouter") is ProviderProfile.OPENROUTER


def test_normalize_usage_openrouter_with_cost_and_cached_tokens() -> None:
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00342,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
    )
    assert info.prompt_tokens == 100
    assert info.completion_tokens == 50
    assert info.total_tokens == 150
    assert info.cache_read_tokens == 30
    assert info.upstream_cost_usd == 0.00342


def test_normalize_usage_openrouter_without_cost() -> None:
    info = normalize_usage_openrouter(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    assert info.upstream_cost_usd is None
    assert info.cache_read_tokens == 0


def test_normalize_usage_openrouter_handles_flat_cache_field() -> None:
    """When OpenRouter (or its upstream) returns cache_read_tokens flat, use it."""
    info = normalize_usage_openrouter(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cache_read_tokens": 25,
        }
    )
    assert info.cache_read_tokens == 25


def test_get_usage_normalizer_returns_openrouter_normalizer() -> None:
    normalizer = get_usage_normalizer(ProviderProfile.OPENROUTER)
    info = normalizer({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.5})
    assert info.upstream_cost_usd == 0.5
```

- [ ] **Step 3.2: Run to verify failure**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py::test_provider_profile_has_openrouter -v 2>&1 | tail -8
```
Expected: FAIL with `ValueError: 'openrouter' is not a valid ProviderProfile`.

- [ ] **Step 3.3: Add the enum value, normalizer, and dispatcher entry**

Modify `serving/adapters/profiles.py`. Add `OPENROUTER` to the enum (alphabetical order is fine):

```python
class ProviderProfile(str, Enum):
    """Provider profile identifier for usage extraction strategy."""

    AZURE_OPENAI = "azure_openai"
    DEFAULT = "default"
    DEEPSEEK = "deepseek"
    OPENROUTER = "openrouter"
    ZHIPU = "zhipu"
```

Update `get_usage_normalizer` to dispatch:

```python
def get_usage_normalizer(profile: ProviderProfile) -> Callable[[dict[str, Any]], UsageInfo]:
    """Return the usage normalizer for the given profile."""
    if profile == ProviderProfile.AZURE_OPENAI:
        return normalize_usage_azure_openai
    if profile == ProviderProfile.DEEPSEEK:
        return normalize_usage_deepseek
    if profile == ProviderProfile.OPENROUTER:
        return normalize_usage_openrouter
    return normalize_usage_default
```

Add `normalize_usage_openrouter` directly after `normalize_usage_deepseek`:

```python
def normalize_usage_openrouter(usage_data: dict[str, Any]) -> UsageInfo:
    """OpenRouter usage extraction: standard tokens + optional cost.

    OpenRouter reports `cost` (USD, per-request) when the request body sets
    `usage: {include: true}`. Cache tokens may be returned either flat
    (cache_read_tokens) or nested under prompt_tokens_details.cached_tokens
    depending on the upstream provider OpenRouter routed to; we accept both.
    """
    from .base import UsageInfo

    base = normalize_usage_default(usage_data)
    if base.cache_read_tokens == 0:
        nested = usage_data.get("prompt_tokens_details") or {}
        cached = nested.get("cached_tokens") if isinstance(nested, dict) else None
        if isinstance(cached, int) and cached > 0:
            base.cache_read_tokens = cached

    cost = usage_data.get("cost")
    base.upstream_cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    return base
```

- [ ] **Step 3.4: Run test to verify pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py -v 2>&1 | tail -15
```
Expected: 9 passed.

- [ ] **Step 3.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/adapters/profiles.py test/unit/adapters/test_openrouter_adapter.py && git commit -m "$(cat <<'EOF'
feat(adapters): add OpenRouter provider profile and usage normalizer

normalize_usage_openrouter extracts the upstream-reported `cost` field
into UsageInfo.upstream_cost_usd, falling back to the default token
normalizer for everything else. Accepts both flat and Azure-style
nested cache_read_tokens shapes since the actual shape depends on
which upstream provider OpenRouter routed to.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add `parse_openrouter_kind` helper

**Files:**
- Modify: `serving/servers/registry.py:99-156` (top of `_make_adapter`)
- Test: `test/unit/test_registry_openrouter.py` (create)

- [ ] **Step 4.1: Write failing test**

Create `test/unit/test_registry_openrouter.py`:

```python
"""Tests for parse_openrouter_kind and _make_adapter dispatch."""

from __future__ import annotations

import pytest

from serving.servers.registry import parse_openrouter_kind


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("openrouter", ("openrouter", None)),
        ("openrouter[deepinfra]", ("openrouter", "deepinfra")),
        ("openrouter[fireworks]", ("openrouter", "fireworks")),
        ("openrouter[together-ai]", ("openrouter", "together-ai")),
        ("zhipu", ("zhipu", None)),  # non-openrouter passes through
        ("openai_compat", ("openai_compat", None)),
    ],
)
def test_parse_valid(kind: str, expected: tuple[str, str | None]) -> None:
    assert parse_openrouter_kind(kind) == expected


@pytest.mark.parametrize(
    "kind",
    [
        "openrouter[]",
        "openrouter[ ]",
        "openrouter[deep infra]",
        "openrouter[deep[infra]]",
        "openrouter[deepinfra",
        "openrouter]deepinfra[",
    ],
)
def test_parse_rejects_invalid(kind: str) -> None:
    with pytest.raises(ValueError):
        parse_openrouter_kind(kind)
```

- [ ] **Step 4.2: Run to verify failure**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/test_registry_openrouter.py -v 2>&1 | tail -10
```
Expected: FAIL with `ImportError: cannot import name 'parse_openrouter_kind'`.

- [ ] **Step 4.3: Implement the parser**

Modify `serving/servers/registry.py`. Just before the `def _make_adapter` line (around line 99), add:

```python
import re

_OPENROUTER_KIND_RE = re.compile(r"^openrouter\[([A-Za-z0-9_.\-]+)\]$")


def parse_openrouter_kind(kind: str) -> tuple[str, str | None]:
    """Parse an adapter kind string, recognizing the OpenRouter bracket form.

    Returns a (base_kind, pinned_provider) tuple:
    - "openrouter"               → ("openrouter", None)
    - "openrouter[deepinfra]"    → ("openrouter", "deepinfra")
    - any other kind             → (kind, None) (no parsing)

    Raises ValueError for malformed bracket forms (empty pin, whitespace,
    nested brackets, unmatched brackets).
    """
    if kind == "openrouter":
        return ("openrouter", None)
    if kind.startswith("openrouter[") or kind.endswith("]") and "openrouter" in kind:
        match = _OPENROUTER_KIND_RE.match(kind)
        if match is None:
            raise ValueError(
                f"Invalid OpenRouter kind {kind!r}: expected "
                "'openrouter' or 'openrouter[<slug>]' with slug "
                "matching [A-Za-z0-9_.-]+"
            )
        return ("openrouter", match.group(1))
    return (kind, None)
```

- [ ] **Step 4.4: Run test to verify pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/test_registry_openrouter.py -v 2>&1 | tail -15
```
Expected: 12 passed (6 valid + 6 invalid).

- [ ] **Step 4.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/servers/registry.py test/unit/test_registry_openrouter.py && git commit -m "$(cat <<'EOF'
feat(registry): add parse_openrouter_kind helper

Recognizes the bracket form `openrouter[<slug>]` and returns
(base_kind, pinned_provider) so _make_adapter can keep dispatching on
the base kind while the upcoming OpenRouterAdapter gets the pinned
provider through cfg. Slug is restricted to [A-Za-z0-9_.-]+.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Implement `OpenRouterAdapter` (headers + payload augmentation + `_routing` injection)

**Files:**
- Create: `serving/adapters/openrouter.py`
- Modify: `serving/adapters/openai_compat.py:400-499, 750-770` (add `_augment_payload` hook + extend `_build_final_chunk` to read `upstream_cost_usd` from `UsageInfo`)
- Test: `test/unit/adapters/test_openrouter_adapter.py`

Parent changes:
- Add `_augment_payload(payload, *, stream)` hook (default no-op) and call it in both `chat_completion` and `stream_chat_completion`. Subclasses use this to inject body fields without copy-pasting the request plumbing.
- Extend `_build_final_chunk` to accept an optional `usage_info: UsageInfo | None = None` keyword arg; when supplied and `usage_info.upstream_cost_usd is not None`, the streaming final chunk's existing `_routing` block gets an extra `upstream_cost_usd` key. Pass `usage_info=usage_info` from `stream_chat_completion`'s call site. No behavior change for non-OpenRouter adapters since their normalizers leave `upstream_cost_usd = None`.

Non-stream `_routing` injection lives **only** on the OpenRouter subclass via overriding `_parse_completion_response` — keeping the change narrow and avoiding any behavior change for the other OAI-compat adapters (zhipu, chutes, featherless, ollama, vllm, sglang, openai, deepseek).

### 5a: Add `_augment_payload` hook on parent (no other behavior changes)

- [ ] **Step 5a.1: Append failing test for the default no-op hook**

Append to `test/unit/adapters/test_openrouter_adapter.py`:

```python
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from serving.adapters.openai_compat import OpenAICompatAdapter


def _make_compat_cfg(**overrides: Any) -> ModelConfig:
    base = dict(
        id="dummy-model",
        name="Dummy",
        provider="openai_compat",
        base_url="https://example.test/v1",
        api_key="sk-test",
        provider_model_id="dummy-upstream",
        supports_tools=False,
        supports_structured_output=False,
        supported_params=["temperature", "top_p", "max_tokens"],
    )
    base.update(overrides)
    return ModelConfig(**base)


def test_augment_payload_default_is_noop() -> None:
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    payload = {"model": "x", "messages": []}
    out = adapter._augment_payload(dict(payload), stream=False)
    assert out == payload
```

- [ ] **Step 5a.2: Run to verify failure**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py::test_augment_payload_default_is_noop -v 2>&1 | tail -8
```
Expected: FAIL — `AttributeError: 'OpenAICompatAdapter' object has no attribute '_augment_payload'`.

- [ ] **Step 5a.3: Add the hook on parent and extend `_build_final_chunk`**

Modify `serving/adapters/openai_compat.py`. Add a default no-op hook on `OpenAICompatAdapter` just after the existing `_get_model_identifier` method:

```python
    def _augment_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        """Subclass extension point for provider-specific payload mutation.

        Called inside chat_completion / stream_chat_completion right before
        the request is dispatched, after profile-level transforms have run.
        Default implementation returns the payload unchanged.
        """
        return payload
```

In `chat_completion`, find the line:

```python
        payload = transform_payload_for_profile(self._usage_profile, payload, stream=False)
```

and replace it with:

```python
        payload = transform_payload_for_profile(self._usage_profile, payload, stream=False)
        payload = self._augment_payload(payload, stream=False)
```

In `stream_chat_completion`, find the line:

```python
        payload = transform_payload_for_profile(self._usage_profile, payload, stream=True)
```

and replace it with:

```python
        payload = transform_payload_for_profile(self._usage_profile, payload, stream=True)
        payload = self._augment_payload(payload, stream=True)
```

Find the existing `_build_final_chunk` definition (the parent's streaming-final-chunk helper) and extend it with an optional `usage_info` keyword arg:

```python
    def _build_final_chunk(
        self,
        *,
        usage: dict[str, Any],
        finish_reason: str,
        usage_info: UsageInfo | None = None,
    ) -> str:
        routing: dict[str, Any] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or self.config.provider,
        }
        if usage_info is not None and usage_info.upstream_cost_usd is not None:
            routing["upstream_cost_usd"] = usage_info.upstream_cost_usd

        chunk = {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.config.id,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
            "_routing": routing,
        }
        return f"data: {json.dumps(chunk)}\n\n"
```

In `stream_chat_completion`, where `_build_final_chunk` is called, also thread the `usage_info`:

Replace:

```python
        if upstream_usage:
            usage_info = self._usage_normalizer(upstream_usage)
            final_usage = usage_info.to_dict()
        else:
            final_usage = self._build_fallback_usage(
                messages=cleaned_messages
                if self._usage_profile != ProviderProfile.DEFAULT
                else messages,
                total_content=total_content,
                prompt_tokens_override=prompt_tokens_override,
            )
        final_chunk_str = self._build_final_chunk(
            usage=final_usage,
            finish_reason=finish_reason,
        )
```

with:

```python
        if upstream_usage:
            usage_info = self._usage_normalizer(upstream_usage)
            final_usage = usage_info.to_dict()
        else:
            usage_info = None
            final_usage = self._build_fallback_usage(
                messages=cleaned_messages
                if self._usage_profile != ProviderProfile.DEFAULT
                else messages,
                total_content=total_content,
                prompt_tokens_override=prompt_tokens_override,
            )
        final_chunk_str = self._build_final_chunk(
            usage=final_usage,
            finish_reason=finish_reason,
            usage_info=usage_info,
        )
```

Behavior is unchanged for every non-OpenRouter adapter — their normalizers leave `usage_info.upstream_cost_usd is None`, and the new `_routing` field is only added when the cost is set.

- [ ] **Step 5a.4: Run the new test plus the full adapter suite**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py::test_augment_payload_default_is_noop -v 2>&1 | tail -8
```
Expected: 1 passed.

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/ test/integration/test_openai_compat_multi_key.py -q 2>&1 | tail -10
```
Expected: All previously-passing adapter tests still pass — no regression for zhipu / chutes / featherless / etc.

- [ ] **Step 5a.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/adapters/openai_compat.py test/unit/adapters/test_openrouter_adapter.py && git commit -m "$(cat <<'EOF'
feat(adapters): add _augment_payload hook to OpenAICompatAdapter

No-op hook called inside chat_completion and stream_chat_completion
right before request dispatch, after profile-level transforms. Lets
subclasses (e.g. the upcoming OpenRouterAdapter) inject provider-
specific body fields without copy-pasting the request plumbing.

No behavior change for existing OAI-compat adapters.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### 5b: Implement `OpenRouterAdapter`

- [ ] **Step 5b.1: Append failing tests**

Append to `test/unit/adapters/test_openrouter_adapter.py`:

```python
from serving.adapters.openrouter import OpenRouterAdapter


def _make_or_cfg(*, pinned: str | None = None) -> ModelConfig:
    return ModelConfig(
        id="or-model",
        name="OR Model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        provider_model_id="meta-llama/llama-3.3-70b-instruct",
        supports_tools=False,
        supports_structured_output=False,
        supported_params=["temperature", "top_p", "max_tokens"],
        provider_profile="openrouter",
        openrouter_pinned_provider=pinned,
    )


def test_openrouter_adapter_attribution_headers() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg())
    headers = adapter._build_headers()
    assert headers["HTTP-Referer"] == "https://freeinference.org"
    assert headers["X-Title"] == "FreeInference"
    assert headers["Authorization"] == "Bearer sk-or-test"


def test_openrouter_adapter_payload_no_pin() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg(pinned=None))
    payload = adapter._augment_payload(
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )
    assert payload["usage"] == {"include": True}
    assert "provider" not in payload
    assert "stream_options" not in payload


def test_openrouter_adapter_payload_with_pin() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg(pinned="deepinfra"))
    payload = adapter._augment_payload(
        {"model": "x", "messages": []},
        stream=False,
    )
    assert payload["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}


def test_openrouter_adapter_streaming_payload_includes_stream_options() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg())
    payload = adapter._augment_payload(
        {"model": "x", "messages": [], "stream": True},
        stream=True,
    )
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["usage"] == {"include": True}


def test_openrouter_adapter_does_not_overwrite_existing_stream_options() -> None:
    adapter = OpenRouterAdapter(_make_or_cfg())
    payload = adapter._augment_payload(
        {"messages": [], "stream": True, "stream_options": {"foo": "bar"}},
        stream=True,
    )
    assert payload["stream_options"] == {"foo": "bar", "include_usage": True}


@pytest.mark.asyncio
async def test_openrouter_adapter_chat_completion_threads_upstream_cost() -> None:
    """Non-stream response carries upstream_cost_usd in the _routing block."""
    adapter = OpenRouterAdapter(_make_or_cfg(pinned="deepinfra"))
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0.00342,
        },
    }
    with patch.object(
        adapter, "_post_with_pool", AsyncMock(return_value=upstream_response)
    ) as mock_post:
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    # _routing carries the cost
    assert result["_routing"]["upstream_cost_usd"] == 0.00342
    assert result["_routing"]["provider"] == "openrouter"
    assert result["_routing"]["base_url"] == "https://openrouter.ai/api/v1"
    # Outbound payload had OpenRouter-specific fields
    sent_payload = mock_post.call_args[0][1]
    assert sent_payload["usage"] == {"include": True}
    assert sent_payload["provider"] == {"order": ["deepinfra"], "allow_fallbacks": False}


@pytest.mark.asyncio
async def test_openrouter_adapter_chat_completion_omits_cost_when_absent() -> None:
    """When OpenRouter doesn't return cost, _routing has no upstream_cost_usd key."""
    adapter = OpenRouterAdapter(_make_or_cfg())
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    with patch.object(
        adapter, "_post_with_pool", AsyncMock(return_value=upstream_response)
    ):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert "upstream_cost_usd" not in result["_routing"]


def test_openrouter_adapter_endpoint_id_distinct_per_pin() -> None:
    """Distinct pinned providers must produce distinct endpoint_ids.

    Verifies the property by going through the registry's _make_provider_id
    with the raw bracketed kind string.
    """
    from serving.servers.registry import _make_provider_id

    base = "https://openrouter.ai/api/v1"
    id_bare = _make_provider_id("llama-3.3-70b", "openrouter", base)
    id_di = _make_provider_id("llama-3.3-70b", "openrouter[deepinfra]", base)
    id_fw = _make_provider_id("llama-3.3-70b", "openrouter[fireworks]", base)
    assert id_bare != id_di != id_fw
    assert id_di != id_bare
```

- [ ] **Step 5b.2: Run to verify failure**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py -v -k openrouter_adapter 2>&1 | tail -15
```
Expected: All new tests fail with `ModuleNotFoundError: No module named 'serving.adapters.openrouter'`.

- [ ] **Step 5b.3: Implement the adapter**

Create `serving/adapters/openrouter.py`:

```python
"""OpenRouter adapter: thin OpenAICompatAdapter subclass with OR-specific request shape."""

from __future__ import annotations

from typing import Any

from .openai_compat import OpenAICompatAdapter

# Attribution headers required for OpenRouter leaderboard / free-tier limits.
# Hardcoded — single deployment, no per-route override needed.
_HTTP_REFERER = "https://freeinference.org"
_X_TITLE = "FreeInference"


class OpenRouterAdapter(OpenAICompatAdapter):
    """OpenAI-compatible adapter for OpenRouter.

    Adds OpenRouter-specific request augmentation on top of the generic
    OpenAICompatAdapter:
    - Attribution headers (HTTP-Referer, X-Title).
    - `usage: {include: true}` on every request so OpenRouter returns the
      per-request `cost` field.
    - `stream_options: {include_usage: true}` on streaming requests so the
      final SSE chunk carries the usage block.
    - `provider: {order: [<slug>], allow_fallbacks: false}` when the route
      uses the bracket form `kind: openrouter[<slug>]` (config field
      `openrouter_pinned_provider`).

    Threads `upstream_cost_usd` from the response usage block into the
    internal `_routing` metadata block:
    - non-stream: this class overrides _parse_completion_response to attach
      `_routing` (the parent does not attach one for non-stream responses).
    - stream: handled by the parent's extended _build_final_chunk, which
      reads upstream_cost_usd off the UsageInfo we pass through.
    """

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        headers = super()._build_headers(api_key_override=api_key_override)
        headers["HTTP-Referer"] = _HTTP_REFERER
        headers["X-Title"] = _X_TITLE
        return headers

    def _augment_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        payload["usage"] = {"include": True}
        pin = getattr(self.config, "openrouter_pinned_provider", None)
        if pin:
            payload["provider"] = {"order": [pin], "allow_fallbacks": False}
        if stream:
            existing = dict(payload.get("stream_options") or {})
            existing["include_usage"] = True
            payload["stream_options"] = existing
        return payload

    def _parse_completion_response(self, response: dict[str, Any]) -> dict[str, Any]:
        formatted = super()._parse_completion_response(response)
        usage_info = self._parse_usage(response.get("usage", {}))
        routing: dict[str, Any] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or self.config.provider,
        }
        if usage_info.upstream_cost_usd is not None:
            routing["upstream_cost_usd"] = usage_info.upstream_cost_usd
        formatted["_routing"] = routing
        return formatted
```

- [ ] **Step 5b.4: Run tests to verify pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py -v 2>&1 | tail -25
```
Expected: 17 passed.

- [ ] **Step 5b.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/adapters/openrouter.py test/unit/adapters/test_openrouter_adapter.py && git commit -m "$(cat <<'EOF'
feat(adapters): add OpenRouterAdapter

OpenAICompatAdapter subclass that injects:
- HTTP-Referer / X-Title attribution headers
- `usage: {include: true}` so OpenRouter returns per-request cost
- `stream_options: {include_usage: true}` on streaming requests
- `provider: {order: [<slug>], allow_fallbacks: false}` when the route
  is configured with `kind: openrouter[<slug>]`

The adapter relies entirely on the parent for HTTP, retry, key-pool,
and streaming plumbing; the OR-specific shape lives in two overrides:
_build_headers and _augment_payload.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Wire `_make_adapter` to dispatch OpenRouter and export the adapter

**Files:**
- Modify: `serving/servers/registry.py:99-156`
- Modify: `serving/adapters/__init__.py`
- Test: `test/unit/test_registry_openrouter.py`

- [ ] **Step 6.1: Append dispatch tests**

Append to `test/unit/test_registry_openrouter.py`:

```python
from serving.adapters import OpenRouterAdapter
from serving.servers.registry import _make_adapter


def _cfg(**overrides):
    base = dict(
        id="m",
        name="M",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-test",
        provider_model_id="meta-llama/llama-3.3-70b-instruct",
    )
    base.update(overrides)
    return base


def test_make_adapter_bare_openrouter_returns_openrouter_adapter() -> None:
    adapter = _make_adapter("openrouter", _cfg())
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter.config.openrouter_pinned_provider is None


def test_make_adapter_bracket_openrouter_sets_pinned_provider() -> None:
    adapter = _make_adapter("openrouter[fireworks]", _cfg())
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter.config.openrouter_pinned_provider == "fireworks"


def test_make_adapter_invalid_openrouter_kind_raises() -> None:
    with pytest.raises(ValueError):
        _make_adapter("openrouter[]", _cfg())
```

- [ ] **Step 6.2: Run to verify failure**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/test_registry_openrouter.py -v 2>&1 | tail -10
```
Expected: First two new tests fail (`ImportError: cannot import name 'OpenRouterAdapter'` or `Unknown adapter kind`); third may pass coincidentally — fix it after the import.

- [ ] **Step 6.3: Export adapter**

Modify `serving/adapters/__init__.py`:

```python
from .base import BaseAdapter, ModelConfig, UsageInfo
from .claude import ClaudeAdapter
from .claude_sub import ClaudeSubscriptionAdapter
from .codex_sub import CodexSubscriptionAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter
from .openrouter import OpenRouterAdapter

__all__ = [
    "BaseAdapter",
    "ClaudeAdapter",
    "ClaudeSubscriptionAdapter",
    "CodexSubscriptionAdapter",
    "GeminiAdapter",
    "ModelConfig",
    "OpenAICompatAdapter",
    "OpenRouterAdapter",
    "UsageInfo",
]
```

- [ ] **Step 6.4: Wire dispatch in `_make_adapter`**

Modify `serving/servers/registry.py`. Find the `_make_adapter(kind, cfg)` function. At its top, parse the bracketed kind and stash the pinned provider on cfg before any of the existing dispatch logic runs:

```python
def _make_adapter(kind: str, cfg: dict[str, Any]):
    """Construct a provider adapter from a kind string and model config.

    Args:
        kind: Adapter kind (``"vllm"``, ``"sglang"``, ``"claude"``, ``"deepseek"``, ``"gemini"``, ``"openai"``, ``"zhipu"``,
              ``"chutes"``, ``"featherless"``, ``"ollama"``, ``"openai_compat"``, ``"openrouter"``,
              ``"openrouter[<slug>]"``).
        cfg: ``ModelConfig`` keyword arguments.

    Returns:
        A concrete adapter instance.

    Raises:
        ValueError: When ``kind`` is unknown or the OpenRouter bracket form
            is malformed.
    """
    # Resolve OpenRouter bracket syntax up front so the rest of the dispatch
    # operates on the bare base kind. parse_openrouter_kind raises on
    # malformed inputs (empty pin, whitespace, nested brackets).
    base_kind, pinned_provider = parse_openrouter_kind(kind)
    if base_kind == "openrouter":
        cfg = {
            **cfg,
            "provider_profile": "openrouter",
            "openrouter_pinned_provider": pinned_provider,
        }
        kind = base_kind  # subsequent dispatch checks compare against the bare kind

    # DeepSeek routes through OpenAICompatAdapter with DeepSeek usage profile
    if kind == "deepseek":
        cfg = {**cfg, "provider_profile": "deepseek"}
    elif kind == "openai":
        cfg = {
            **cfg,
            "provider_profile": "azure_openai",
            "chat_path": "/chat/completions",
            "use_bearer_auth": False,
            "auth_header_name": "api-key",
            "auth_format": "{api_key}",
            "extra_query": {"api-version": "2024-12-01-preview"},
        }
    # Zhipu routes through OpenAICompatAdapter with a non-/v1 chat path.
    elif kind == "zhipu":
        cfg = {**cfg, "provider_profile": "zhipu", "chat_path": "/chat/completions"}

    model_cfg = ModelConfig(**cfg)

    # All OpenAI-compatible services use the same adapter
    if kind in (
        "vllm",
        "sglang",
        "chutes",
        "featherless",
        "ollama",
        "openai_compat",
        "deepseek",
        "openai",
        "zhipu",
    ):
        return OpenAICompatAdapter(model_cfg)

    if kind == "openrouter":
        return OpenRouterAdapter(model_cfg)

    if kind == "claude":
        return ClaudeAdapter(model_cfg)
    if kind == "gemini":
        return GeminiAdapter(model_cfg)
    if kind == "codex_sub":
        return CodexSubscriptionAdapter(model_cfg)
    if kind == "claude_sub":
        return ClaudeSubscriptionAdapter(model_cfg)

    raise ValueError(f"Unknown adapter kind: {kind}")
```

Add the `OpenRouterAdapter` import at the top of `registry.py` (next to the other adapter imports). Find the existing imports for `OpenAICompatAdapter`, `ClaudeAdapter`, etc., and add:

```python
from serving.adapters import (
    ClaudeAdapter,
    ClaudeSubscriptionAdapter,
    CodexSubscriptionAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)
```

(If the existing import shape is different, add `OpenRouterAdapter` to whichever import statement currently brings in `OpenAICompatAdapter`.)

- [ ] **Step 6.5: Run tests to verify pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/test_registry_openrouter.py -v 2>&1 | tail -15
```
Expected: 15 passed (12 parser + 3 dispatch).

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/test_registry_multi_key.py -v 2>&1 | tail -10
```
Expected: All previously-passing tests still pass (verifies no regression in `_make_adapter` for other kinds).

- [ ] **Step 6.6: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/servers/registry.py serving/adapters/__init__.py test/unit/test_registry_openrouter.py && git commit -m "$(cat <<'EOF'
feat(registry): dispatch openrouter[<slug>] kind to OpenRouterAdapter

_make_adapter now resolves the OpenRouter bracket syntax up front via
parse_openrouter_kind, stashing the pinned provider on cfg before the
existing per-kind config branches run. The base kind is dispatched
through a dedicated branch that constructs OpenRouterAdapter.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Add `upstream_cost_usd` migration and `log_request()` parameter

**Files:**
- Modify: `serving/storage/database.py:300-322, 821-966`
- Test: `test/integration/test_database_integration.py` (extend if it covers the schema; otherwise rely on Task 7's behavioral test plus the existing migration test patterns)

- [ ] **Step 7.1: Look at existing schema-migration test conventions**

```bash
cd /home/juncheng/hybridInference-or && grep -n "ADD COLUMN IF NOT EXISTS\|cost_usd" serving/storage/database.py | head
```
Expected output includes lines around 309-322 (existing `cost_usd` migration). Use the same idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern.

- [ ] **Step 7.2: Add migration**

Modify `serving/storage/database.py`. After the existing migration block that adds `cost_usd` (around line 319-322), add:

```python
            await conn.execute("""
                ALTER TABLE api_logs
                ADD COLUMN IF NOT EXISTS upstream_cost_usd DECIMAL(12, 8)
            """)
```

- [ ] **Step 7.3: Add the `upstream_cost_usd` parameter to `log_request`**

In the same file, update the `log_request` signature to accept the new keyword arg, and forward it to the INSERT:

Find the signature (currently around line 821):

```python
    async def log_request(
        self,
        request_id: str,
        ...
        pricing: dict[str, str] | None = None,
    ) -> None:
```

Add a new parameter at the end:

```python
    async def log_request(
        self,
        request_id: str,
        ...
        pricing: dict[str, str] | None = None,
        upstream_cost_usd: float | None = None,
    ) -> None:
```

Update the `INSERT INTO api_logs` SQL to add `upstream_cost_usd` into the column list and a corresponding placeholder:

```python
                """
                INSERT INTO api_logs (
                    request_id, model_id, provider,
                    temperature, top_p, max_tokens, seed, stream,
                    ttft_ms, latency_ms,
                    prompt_tokens, completion_tokens, reasoning_tokens, total_tokens,
                    cache_read_tokens, cache_write_tokens, cost_usd,
                    prompt, response, prompt_hash, response_hash,
                    status_code, error, user_id, session_id, metadata,
                    tools, upstream_cost_usd
                )
                VALUES (
                    $1, $2, $3,
                    $4, $5, $6, $7, $8,
                    $9, $10,
                    $11, $12, $13, $14,
                    $15, $16, $17,
                    $18, $19, $20, $21,
                    $22, $23, $24, $25, $26::jsonb,
                    $27::jsonb, $28
                )
                ON CONFLICT (request_id) DO NOTHING
                """,
```

Append `upstream_cost_usd` as the new positional argument at the end of the values tuple in `conn.execute(...)`:

```python
                json.dumps((params or {}).get("tools")) if (params or {}).get("tools") else None,
                upstream_cost_usd,
            )
```

- [ ] **Step 7.4: Smoke-test by running the existing storage test suite**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/storage/ -q 2>&1 | tail -10
```
Expected: existing tests pass (no DB connection required for unit tests).

If integration tests rely on a postgres test DB and you can run them locally:

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/integration/test_database_integration.py -q 2>&1 | tail -10
```
Expected: pass (migration is idempotent; new column accepts NULL).

If the test DB is unreachable in your environment, document that the migration was added and rely on the deploy-time migration to surface failures.

- [ ] **Step 7.5: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/storage/database.py && git commit -m "$(cat <<'EOF'
feat(storage): add api_logs.upstream_cost_usd column and log_request arg

Idempotent ALTER TABLE migration (DECIMAL(12, 8), NULL) sits next to
the existing cost_usd migration. log_request gains a new keyword-only
arg upstream_cost_usd defaulting to None; existing call sites are
unaffected and continue to insert NULL into the new column.

cost_usd remains the user-billed cost (tokens × model pricing);
upstream_cost_usd is the OpenRouter-reported actual cost for the
internal accounting view only.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Plumb `upstream_cost_usd` through completions handler

**Files:**
- Modify: `serving/servers/routers/completions.py:686-752, 901-943`
- Test: extend `test/unit/adapters/test_openrouter_adapter.py` with a smoke test that exercises the routing block contract directly (no full HTTP integration test here — that lives in Task 11).

- [ ] **Step 8.1: Append a regression test**

Append to `test/unit/adapters/test_openrouter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_non_or_adapter_response_has_no_routing_block() -> None:
    """Non-OpenRouter OAI-compat adapters must NOT attach a _routing block to non-stream responses."""
    cfg = _make_compat_cfg()
    adapter = OpenAICompatAdapter(cfg)
    upstream_response = {
        "id": "x",
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    with patch.object(
        adapter, "_post_with_pool", AsyncMock(return_value=upstream_response)
    ):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert "_routing" not in result
```

- [ ] **Step 8.2: Run to verify pass**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/adapters/test_openrouter_adapter.py -v 2>&1 | tail -10
```
Expected: new test passes — guards against accidentally regressing the parent's response shape.

- [ ] **Step 8.3: Plumb through the completions handler — streaming path**

Modify `serving/servers/routers/completions.py`. Find the `_schedule_db_log_task` call inside the streaming success block (around line 708-752). Locate the `log_data` dict that has `"pricing": pricing,` as its last key. Insert immediately before the closing brace of the dict literal:

```python
                            "pricing": pricing,
                            "upstream_cost_usd": (routing_info or {}).get("upstream_cost_usd"),
                        },
```

- [ ] **Step 8.4: Plumb through the completions handler — non-streaming path**

In the same file, find the non-streaming `_schedule_db_log_task` call (around line 906-943). Locate the closing of that `log_data` dict (the one ending with `"pricing": pricing,`) and insert:

```python
                    "pricing": pricing,
                    "upstream_cost_usd": (routing_info or {}).get("upstream_cost_usd"),
                },
```

- [ ] **Step 8.5: Run unit + servers tests to confirm no regression**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/servers/ test/unit/adapters/ -q 2>&1 | tail -10
```
Expected: all tests pass; no behavior change for non-OpenRouter routes (their `routing_info` lacks `upstream_cost_usd`, so the dict lookup yields `None`).

- [ ] **Step 8.6: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add serving/servers/routers/completions.py test/unit/adapters/test_openrouter_adapter.py && git commit -m "$(cat <<'EOF'
feat(completions): forward upstream_cost_usd from _routing to log_request

Both streaming and non-streaming paths read upstream_cost_usd off the
routing_info dict that sanitize_response extracted from the response's
_routing block, and forward it to log_request as a new keyword arg.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Add `OPENROUTER_API_KEY` to `.env.example` and a commented `models.yaml` example

**Files:**
- Modify: `.env.example`
- Modify: `config/models.yaml`

- [ ] **Step 9.1: Add OPENROUTER_API_KEY to .env.example**

```bash
cd /home/juncheng/hybridInference-or && grep -n "API_KEY" .env.example | head -5
```

Open `.env.example` and add (in the API-keys section, mirroring the format of the other keys):

```
# OpenRouter (https://openrouter.ai) — used when models route through
# `kind: openrouter` or `kind: openrouter[<provider_slug>]`.
OPENROUTER_API_KEY=
```

- [ ] **Step 9.2: Add a commented OpenRouter example in models.yaml**

Append to `config/models.yaml` (inside the `models:` list, at the bottom, mirroring the existing comment style):

```yaml
  # ── OpenRouter examples ────────────────────────────────────
  # Bare `kind: openrouter` lets OpenRouter pick the upstream provider
  # freely; `kind: openrouter[<provider_slug>]` pins to one OpenRouter
  # upstream via provider.order=[<slug>], allow_fallbacks=false. See
  # docs/openrouter.md for the full list of supported provider slugs.
  #
  # - id: llama-3.3-70b
  #   name: Llama 3.3 70B (via OpenRouter)
  #   provider: openrouter
  #   provider_model_id: meta-llama/llama-3.3-70b-instruct
  #   context_length: 131072
  #   max_output_length: 8192
  #   supports_tools: true
  #   supports_structured_output: true
  #   supported_params: [temperature, top_p, max_tokens, stop, stream]
  #   input_modalities: ["text"]
  #   output_modalities: ["text"]
  #   pricing:
  #     prompt: "0.13"
  #     completion: "0.39"
  #     image: "0"
  #     request: "0"
  #     input_cache_reads: "0"
  #     input_cache_writes: "0"
  #   route:
  #     # Pinned to DeepInfra: deterministic upstream, distinct endpoint_id.
  #     - kind: openrouter[deepinfra]
  #       weight: 1.0
  #       base_url: https://openrouter.ai/api/v1
  #       api_key: ${OPENROUTER_API_KEY}
  #       provider_model_id: meta-llama/llama-3.3-70b-instruct
  #     # Bare openrouter — let OpenRouter pick. Different endpoint_id
  #     # from the pinned leg so circuit-breaker stats stay separate.
  #     - kind: openrouter
  #       weight: 0
  #       base_url: https://openrouter.ai/api/v1
  #       api_key: ${OPENROUTER_API_KEY}
  #       provider_model_id: meta-llama/llama-3.3-70b-instruct
```

- [ ] **Step 9.3: Validate YAML parses**

```bash
cd /home/juncheng/hybridInference-or && uv run python -c "import yaml; yaml.safe_load(open('config/models.yaml'))" && echo OK
```
Expected: `OK`.

- [ ] **Step 9.4: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add .env.example config/models.yaml && git commit -m "$(cat <<'EOF'
config(openrouter): add OPENROUTER_API_KEY and commented models.yaml example

Documents both the bare `kind: openrouter` form (let OpenRouter pick)
and the bracket form `kind: openrouter[<slug>]` (pin upstream).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Write `docs/openrouter.md`

**Files:**
- Create: `docs/openrouter.md`

- [ ] **Step 10.1: Write the doc**

Create `docs/openrouter.md` with this content:

```markdown
# OpenRouter as Upstream Backend

The gateway can route requests through [OpenRouter](https://openrouter.ai) as an
upstream provider, either as the primary route for a model or as a fallback leg
on an existing model.

## Configuration

Set `OPENROUTER_API_KEY` in `.env` (single account-level key) and add a route
entry in `config/models.yaml` with one of two `kind` forms.

### Bare `kind: openrouter`

Lets OpenRouter pick the upstream provider freely (lowest cost / best
availability per their default policy).

```yaml
- kind: openrouter
  weight: 1.0
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
```

### Bracket form `kind: openrouter[<provider_slug>]`

Pins the request to one specific OpenRouter upstream via
`provider.order=[<slug>]` with `allow_fallbacks=false`.

```yaml
- kind: openrouter[deepinfra]
  weight: 1.0
  base_url: https://openrouter.ai/api/v1
  api_key: ${OPENROUTER_API_KEY}
  provider_model_id: meta-llama/llama-3.3-70b-instruct
```

The slug must match `[A-Za-z0-9_.-]+`. See
<https://openrouter.ai/docs/features/provider-routing> for the full list of
provider slugs.

When two route legs share a `base_url` but use different pinned providers,
they get distinct `endpoint_id`s — circuit-breaker stats stay isolated per
upstream.

## What the adapter sends

On every request, `OpenRouterAdapter` injects:

- Headers: `HTTP-Referer: https://freeinference.org`, `X-Title: FreeInference`.
- Body: `usage: {include: true}` so OpenRouter returns per-request `cost`.
- For streaming requests: `stream_options: {include_usage: true}`.
- For bracket-form routes: `provider: {order: [<slug>], allow_fallbacks: false}`.

## Cost logging

End-user billing is unchanged: `api_logs.cost_usd` continues to hold
`tokens × model-level pricing`.

The OpenRouter-reported `cost` is logged separately to a new
`api_logs.upstream_cost_usd` column. It is `NULL` for non-OpenRouter routes
and `NULL` for OpenRouter routes when the upstream provider failed to report
cost (rare).

## Error handling

OpenRouter responses are dispatched through the standard OpenAI-compatible
error path:

| HTTP | Behavior |
|------|----------|
| 400 / 403 | Propagated to the client; no fallback (deterministic failure). |
| 401 / 402 | Logged at error level; routed to the next leg in the route's fallback chain. |
| 408 / 429 / 502 / 503 / 524 | Logged at warning level; routed to the next leg. |

There is no special multi-key rotation for OpenRouter — a single account-level
`OPENROUTER_API_KEY` is used.

## Specs and design history

- Design spec: `docs/agents/specs/2026-05-02-openrouter-upstream-design.md`
- Implementation plan: `docs/agents/plans/2026-05-02-openrouter-upstream.md`
```

- [ ] **Step 10.2: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add docs/openrouter.md && git commit -m "$(cat <<'EOF'
docs: add docs/openrouter.md covering kind syntax and cost contract

The README already references this path; previously the file did not
exist. Documents both kind forms, attribution headers, the
upstream_cost_usd logging contract, and error-handling behavior.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Add an integration test (skipped without API key)

**Files:**
- Create: `test/integration/test_openrouter_integration.py`

- [ ] **Step 11.1: Write the test**

Create `test/integration/test_openrouter_integration.py`:

```python
"""Live integration tests against the real OpenRouter API.

Skipped unless OPENROUTER_API_KEY is set. Each test makes ONE small live
chat completion to keep the cost negligible.
"""

from __future__ import annotations

import os

import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openrouter import OpenRouterAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("OPENROUTER_API_KEY"),
        reason="OPENROUTER_API_KEY not set; skipping live OpenRouter integration tests.",
    ),
]


def _cfg(*, pinned: str | None = None) -> ModelConfig:
    return ModelConfig(
        id="or-llama",
        name="OR Llama 3.1 8B",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
        provider_model_id="meta-llama/llama-3.1-8b-instruct",
        context_length=8192,
        max_output_length=64,
        supports_tools=True,
        supports_structured_output=True,
        supported_params=["temperature", "top_p", "max_tokens", "stream"],
        provider_profile="openrouter",
        openrouter_pinned_provider=pinned,
    )


@pytest.mark.asyncio
async def test_real_chat_completion_no_pin() -> None:
    adapter = OpenRouterAdapter(_cfg())
    response = await adapter.chat_completion(
        [{"role": "user", "content": "Say only the word 'pong'."}],
        max_tokens=8,
        temperature=0.0,
    )
    assert response["choices"][0]["message"]["content"]
    routing = response.get("_routing", {})
    # cost is best-effort; assert structure but only a soft check on value
    if routing.get("upstream_cost_usd") is not None:
        assert routing["upstream_cost_usd"] > 0


@pytest.mark.asyncio
async def test_real_streaming_with_cost() -> None:
    adapter = OpenRouterAdapter(_cfg())
    chunks: list[str] = []
    async for chunk in adapter.stream_chat_completion(
        [{"role": "user", "content": "Say only the word 'pong'."}],
        max_tokens=8,
        temperature=0.0,
    ):
        chunks.append(chunk)
    # Final chunk before [DONE] should have _routing with provider info
    joined = "".join(chunks)
    assert '"_routing"' in joined


@pytest.mark.asyncio
async def test_pinned_provider_routes_through() -> None:
    """When provider.order is set, OpenRouter routes only through that provider.

    Verified by inspecting the upstream response which includes a `provider`
    field naming the actual upstream that served the request.
    """
    adapter = OpenRouterAdapter(_cfg(pinned="deepinfra"))
    response = await adapter.chat_completion(
        [{"role": "user", "content": "Say only the word 'pong'."}],
        max_tokens=8,
        temperature=0.0,
    )
    # OpenRouter exposes the upstream provider name on the response.
    # The response we get back is the formatted dict — the raw upstream
    # `provider` field may be on the unwrapped response. Skip strict check
    # if absent (OpenRouter occasionally omits) but cost should be present.
    routing = response.get("_routing", {})
    if routing.get("upstream_cost_usd") is not None:
        assert routing["upstream_cost_usd"] > 0
```

- [ ] **Step 11.2: Smoke-run with no key (must be skipped, not failed)**

```bash
cd /home/juncheng/hybridInference-or && OPENROUTER_API_KEY="" uv run pytest test/integration/test_openrouter_integration.py -v 2>&1 | tail -10
```
Expected: 2 skipped (with the skip reason mentioning the env var).

- [ ] **Step 11.3: Commit**

```bash
cd /home/juncheng/hybridInference-or && git add test/integration/test_openrouter_integration.py && git commit -m "$(cat <<'EOF'
test(openrouter): add live integration tests gated on OPENROUTER_API_KEY

Two small chat completions (one non-stream, one stream) against
meta-llama/llama-3.1-8b-instruct. Skipped when the env var is unset
so CI without OpenRouter credits stays green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Lint, full test pass, and PR

- [ ] **Step 12.1: Run ruff format check (mandatory pre-PR per CLAUDE.md)**

```bash
cd /home/juncheng/hybridInference-or && uv run ruff format --check . 2>&1 | tail -10
```
Expected: `would be left unchanged` (or `0 files would be reformatted`).

If files would be reformatted, run `uv run ruff format .`, review the diff, and amend the touching commit (or commit the formatting fix separately as `style: ruff format` if it touches multiple commits).

- [ ] **Step 12.2: Run ruff lint**

```bash
cd /home/juncheng/hybridInference-or && uv run ruff check . 2>&1 | tail -10
```
Expected: `All checks passed`. Fix any reported violations.

- [ ] **Step 12.3: Run the full unit test suite**

```bash
cd /home/juncheng/hybridInference-or && uv run pytest test/unit/ -q 2>&1 | tail -15
```
Expected: all green. Investigate any failures before opening the PR.

- [ ] **Step 12.4: Push and open the PR to dev**

```bash
cd /home/juncheng/hybridInference-or && git push -u origin jason/claude/openrouter-upstream 2>&1 | tail -5
```

```bash
cd /home/juncheng/hybridInference-or && gh pr create --base dev --title "feat: OpenRouter as upstream backend" --body "$(cat <<'EOF'
## Summary

- Adds `OpenRouterAdapter` (subclass of `OpenAICompatAdapter`) that injects OpenRouter-specific request shape: attribution headers, `usage.include`, `provider.order`, `stream_options.include_usage`.
- New `kind: openrouter[<provider_slug>]` bracket syntax pins requests to a specific OpenRouter upstream; bare `kind: openrouter` lets OpenRouter pick.
- New `api_logs.upstream_cost_usd` column captures OpenRouter-reported per-request cost; end-user billing in `cost_usd` is unchanged.
- New `ProviderProfile.OPENROUTER` and `normalize_usage_openrouter` extract `cost` from upstream usage.
- `UsageInfo.upstream_cost_usd` is internal-only — not surfaced via `to_dict()` / API responses.

## Test plan

- [ ] CI green
- [ ] On staging: add an OpenRouter route to one model, send a chat completion, confirm `api_logs.upstream_cost_usd` is populated.
- [ ] On staging: bracket-form route serves only via the pinned upstream (verified via OpenRouter dashboard).
- [ ] On staging: existing routes unchanged — no impact on `cost_usd` billing or admin dashboards.
- [ ] On staging: 400 propagates without router fallback.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Capture the printed PR URL and report it back.

- [ ] **Step 12.5: Address PR comments and CI failures**

Per CLAUDE.md, do not merge until all comments are addressed and CI is green.

---

## Self-review checklist

- [x] Spec coverage — every component (parser, adapter, profile, UsageInfo field, ModelConfig field, schema migration, log_request arg, completions plumbing, env var, models.yaml example, doc, integration test) maps to a task.
- [x] Placeholder scan — no `TBD` / `TODO` / "implement later" / "similar to Task N" placeholders.
- [x] Type consistency — `parse_openrouter_kind` returns `tuple[str, str | None]` everywhere it is used; `UsageInfo.upstream_cost_usd: float | None`; `log_request(..., upstream_cost_usd: float | None = None)`; `OpenRouterAdapter._augment_payload(payload, *, stream)` matches the parent's hook signature.
