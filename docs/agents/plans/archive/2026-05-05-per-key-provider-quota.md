# Per-Key Provider Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display per-key quota in the admin dashboard when a provider has multiple API keys configured.

**Architecture:** Refactor each quota fetcher to discover multiple env vars (numbered suffixes), fetch upstream quota per key in parallel, and return grouped results. Frontend groups by provider name and renders sub-sections per key.

**Tech Stack:** Python (Pydantic, aiohttp), TypeScript/React (Next.js), Tailwind CSS

---

### Task 1: Add `key_index` to schema

**Files:**
- Modify: `apps/backend/serving/schemas_admin.py:638-651`

- [ ] **Step 1: Add `key_index` field to `ProviderQuotaResult`**

In `schemas_admin.py`, add `key_index: int | None = None` field to `ProviderQuotaResult` after `name`:

```python
class ProviderQuotaResult(BaseModel):
    name: str = Field(...)
    display_name: str = Field(...)
    key_index: int | None = Field(None, description="1-based key index when provider has multiple keys; None for single-key providers")
    key_configured: bool = Field(...)
    key_masked: str | None = Field(None)
    # ... rest unchanged
```

- [ ] **Step 2: Run lint**

Run: `make lint`

---

### Task 2: Add `_discover_env_keys` helper and refactor `fetch_zai` for multi-key

**Files:**
- Modify: `apps/backend/serving/admin/provider_quotas.py`
- Modify: `tests/servers/test_admin_provider_quotas.py`

- [ ] **Step 1: Write failing test for multi-key ZAI discovery**

Add to `test_admin_provider_quotas.py`:

```python
class TestDiscoverEnvKeys:
    def test_single_key_returns_index_1(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "key1_long_enough_1234")
        monkeypatch.delenv("ZAI_API_KEY2", raising=False)
        from serving.admin.provider_quotas import _discover_env_keys
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == [(1, "key1_long_enough_1234")]

    def test_multiple_keys_returns_all(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "key1_long_enough_1234")
        monkeypatch.setenv("ZAI_API_KEY2", "key2_long_enough_5678")
        monkeypatch.setenv("ZAI_API_KEY3", "key3_long_enough_9012")
        from serving.admin.provider_quotas import _discover_env_keys
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == [
            (1, "key1_long_enough_1234"),
            (2, "key2_long_enough_5678"),
            (3, "key3_long_enough_9012"),
        ]

    def test_no_keys_returns_empty(self, monkeypatch):
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        from serving.admin.provider_quotas import _discover_env_keys
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == []

    def test_gap_stops_discovery(self, monkeypatch):
        monkeypatch.setenv("ZAI_API_KEY", "key1_long_enough_1234")
        monkeypatch.delenv("ZAI_API_KEY2", raising=False)
        monkeypatch.setenv("ZAI_API_KEY3", "key3_long_enough_9012")
        from serving.admin.provider_quotas import _discover_env_keys
        keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
        assert keys == [(1, "key1_long_enough_1234")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/servers/test_admin_provider_quotas.py::TestDiscoverEnvKeys -v`
Expected: FAIL (import error)

- [ ] **Step 3: Implement `_discover_env_keys`**

Add to `provider_quotas.py` after `_mask_key`:

```python
def _discover_env_keys(base_var: str, numbered_prefix: str) -> list[tuple[int, str]]:
    """Discover all configured API keys via numbered env var suffixes.

    Returns list of (index, value). index=1 for base_var, index=N for
    ``{numbered_prefix}{N}``. Stops at the first missing numbered var.
    """
    keys: list[tuple[int, str]] = []
    val = os.getenv(base_var, "")
    if val:
        keys.append((1, val))
    for i in range(2, 20):
        val = os.getenv(f"{numbered_prefix}{i}", "")
        if not val:
            break
        keys.append((i, val))
    return keys
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/servers/test_admin_provider_quotas.py::TestDiscoverEnvKeys -v`
Expected: PASS

---

### Task 3: Refactor `fetch_zai` for multi-key support

**Files:**
- Modify: `apps/backend/serving/admin/provider_quotas.py:288-392`

- [ ] **Step 1: Extract `fetch_zai_for_key` inner function**

Rename current `fetch_zai` body to `_fetch_zai_for_key(key: str) -> ProviderQuotaResult`. This function takes a key parameter instead of reading env var. It sets `key_index=None`, `name="zai"`, `display_name="ZAI"`.

Then create new `fetch_zai()` that discovers keys and calls `_fetch_zai_for_key` for each:

```python
async def _fetch_zai_for_key(key: str) -> ProviderQuotaResult:
    """Fetch quota for a single ZAI API key."""
    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers, allow_redirects=False) as resp,
        ):
            if resp.status in (301, 302, 303, 307, 308, 401, 403):
                return _err("zai", "ZAI", key, "auth_failed")
            if resp.status >= 400:
                return _err("zai", "ZAI", key, "unexpected")
            try:
                data: dict[str, Any] = await resp.json()
            except Exception:
                return _err("zai", "ZAI", key, "parse_error")
    except asyncio.TimeoutError:
        return _err("zai", "ZAI", key, "timeout")
    except aiohttp.ClientError:
        return _err("zai", "ZAI", key, "unexpected")
    except Exception:
        logger.exception("fetch_zai: unexpected error")
        return _err("zai", "ZAI", key, "unexpected")

    body = data.get("data") if isinstance(data.get("data"), dict) else data
    limits = body.get("limits") if isinstance(body, dict) else None
    if not isinstance(limits, list):
        return _err("zai", "ZAI", key, "parse_error")

    usages: list[ProviderQuotaUsage] = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", "")).upper()
        if kind == "TOKENS_LIMIT":
            label, unit = "Tokens", "tokens"
        elif kind == "TIME_LIMIT":
            label, unit = "Time", "minutes"
        else:
            label, unit = kind.replace("_", " ").title() or "Quota", ""

        entry_reset_at = _parse_epoch_ms(entry.get("nextResetTime"))
        used_raw = entry.get("currentValue") if "currentValue" in entry else entry.get("used")
        limit_raw = entry.get("usage")

        if used_raw is None and limit_raw is None and "percentage" in entry:
            pct = entry.get("percentage")
            usages.append(
                ProviderQuotaUsage(
                    label=label,
                    used=float(pct) if isinstance(pct, (int, float)) else None,
                    limit=100.0,
                    unit="%",
                    reset_at=entry_reset_at,
                )
            )
            continue

        usages.append(
            ProviderQuotaUsage(
                label=label,
                used=float(used_raw) if isinstance(used_raw, (int, float)) else None,
                limit=float(limit_raw) if isinstance(limit_raw, (int, float)) else None,
                unit=unit,
                reset_at=entry_reset_at,
            )
        )

    return ProviderQuotaResult(
        name="zai",
        display_name="ZAI",
        key_index=None,
        key_configured=True,
        key_masked=_mask_key(key),
        fetched_at=_now(),
        ok=True,
        error=None,
        usages=usages,
    )


async def fetch_zai() -> list[ProviderQuotaResult]:
    """Fetch quota usage from ZAI for all configured API keys."""
    keys = _discover_env_keys("ZAI_API_KEY", "ZAI_API_KEY")
    if not keys:
        return [
            ProviderQuotaResult(
                name="zai",
                display_name="ZAI",
                key_index=None,
                key_configured=False,
                key_masked=None,
                fetched_at=_now(),
                ok=False,
                error="not_configured",
                usages=[],
            )
        ]

    results = await asyncio.gather(
        *[_fetch_zai_for_key(k) for _, k in keys],
        return_exceptions=True,
    )

    out: list[ProviderQuotaResult] = []
    multi = len(keys) > 1
    for (idx, _key), result in zip(keys, results):
        if isinstance(result, ProviderQuotaResult):
            r = result.model_copy(update={
                "key_index": idx if multi else None,
                "display_name": f"ZAI #{idx}" if multi else "ZAI",
            })
            out.append(r)
        else:
            logger.error("fetch_zai: key #%d raised", idx, exc_info=result)
            out.append(
                ProviderQuotaResult(
                    name="zai",
                    display_name=f"ZAI #{idx}" if multi else "ZAI",
                    key_index=idx if multi else None,
                    key_configured=True,
                    key_masked=_mask_key(_key),
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                )
            )
    return out
```

- [ ] **Step 2: Update `gather_all` to flatten lists**

Change `gather_all` to expect each fetcher to return `list[ProviderQuotaResult]` and flatten:

```python
async def gather_all() -> list[ProviderQuotaResult]:
    fetchers = [
        ("chutes", "Chutes", fetch_chutes),
        ("zai", "ZAI", fetch_zai),
        ("minimax", "MiniMax", fetch_minimax),
        ("ollama", "Ollama Cloud", fetch_ollama),
    ]
    raw = await asyncio.gather(
        *(f() for _, _, f in fetchers),
        return_exceptions=True,
    )
    out: list[ProviderQuotaResult] = []
    for (name, display_name, _), result in zip(fetchers, raw, strict=True):
        if isinstance(result, list):
            out.extend(result)
        elif isinstance(result, Exception):
            logger.error("gather_all: %s fetcher raised", name, exc_info=result)
            out.append(
                ProviderQuotaResult(
                    name=name,
                    display_name=display_name,
                    key_configured=False,
                    key_masked=None,
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                )
            )
        else:
            out.append(
                ProviderQuotaResult(
                    name=name,
                    display_name=display_name,
                    key_configured=False,
                    key_masked=None,
                    fetched_at=_now(),
                    ok=False,
                    error="unexpected",
                    usages=[],
                )
            )
    return out
```

- [ ] **Step 3: Run existing ZAI + gather_all tests**

Run: `uv run pytest tests/servers/test_admin_provider_quotas.py -v`
Expected: All pass (single-key backward compat preserved)

---

### Task 4: Refactor remaining fetchers (Chutes, MiniMax, Ollama) for multi-key

**Files:**
- Modify: `apps/backend/serving/admin/provider_quotas.py`

Apply the same pattern from Task 3 to `fetch_chutes`, `fetch_minimax`, `fetch_ollama`:

- Extract `_fetch_chutes_for_key(key: str) -> ProviderQuotaResult`
- Extract `_fetch_minimax_for_key(cookie: str) -> ProviderQuotaResult`
- Extract `_fetch_ollama_for_key(cookie: str) -> ProviderQuotaResult`
- Each public fetcher becomes `async def fetch_X() -> list[ProviderQuotaResult]`
- Env var discovery:
  - Chutes: `_discover_env_keys("CHUTES_API_KEY", "CHUTES_API_KEY")`
  - MiniMax: `_discover_env_keys("MINIMAX_SESSION_COOKIE", "MINIMAX_SESSION_COOKIE")`
  - Ollama: `_discover_env_keys("OLLAMA_SESSION_COOKIE", "OLLAMA_SESSION_COOKIE")`

Run: `uv run pytest tests/servers/test_admin_provider_quotas.py -v`

---

### Task 5: Update `gather_all` tests for new list-returning fetchers

**Files:**
- Modify: `tests/servers/test_admin_provider_quotas.py:642-671`

Update `TestGatherAll` tests:

- `test_gather_all_returns_four_results_even_if_one_raises` — each not-configured fetcher now returns a list with 1 item. Total length is still 4.
- `test_gather_all_handles_unexpected_exception` — monkeypatch fetcher to return a list or raise.
- Add test for multi-key ZAI: set `ZAI_API_KEY` + `ZAI_API_KEY2`, mock aiohttp, verify `gather_all` returns 5 results (1 chutes + 2 zai + 1 minimax + 1 ollama).

Run: `uv run pytest tests/servers/test_admin_provider_quotas.py -v`

---

### Task 6: Update frontend TypeScript types

**Files:**
- Modify: `apps/frontend/src/lib/api/admin.ts:678-687`

Add `key_index` to the `ProviderQuotaResult` interface:

```typescript
export interface ProviderQuotaResult {
  name: string;
  display_name: string;
  key_index: number | null;
  key_configured: boolean;
  key_masked: string | null;
  fetched_at: string | null;
  ok: boolean;
  error: string | null;
  usages: ProviderQuotaUsage[];
}
```

---

### Task 7: Update `ProviderCard` to group by provider and show per-key sub-sections

**Files:**
- Modify: `apps/frontend/src/app/dashboard/admin/page.tsx:584-657` (ProviderCard)
- Modify: `apps/frontend/src/app/dashboard/admin/page.tsx:1439-1443` (grid rendering)

- [ ] **Step 1: Group providers by `name` in the quota grid rendering**

Replace the direct `providerQuotas.map(...)` at line 1439-1443 with grouping:

```tsx
{providerQuotas.length === 0 ? (
  <div className="py-24 text-center">
    <p className="text-[13px] text-gray-400">No provider data.</p>
  </div>
) : (
  <div className="grid gap-3 sm:grid-cols-2">
    {Array.from(
      providerQuotas.reduce((acc, p) => {
        const group = acc.get(p.name) || [];
        group.push(p);
        acc.set(p.name, group);
        return acc;
      }, new Map<string, ProviderQuotaResult[]>()),
    ).map(([name, group]) => (
      <ProviderCard key={name} group={group} />
    ))}
  </div>
)}
```

- [ ] **Step 2: Rewrite `ProviderCard` to accept a group**

```tsx
function ProviderCard({ group }: { group: ProviderQuotaResult[] }) {
  const first = group[0];
  const providerName = first.display_name.replace(/ #\d+$/, '');
  const isMultiKey = group.length > 1 || first.key_index !== null;
  const stripeColor = group.some(p => p.ok)
    ? 'bg-emerald-500'
    : group[0].error === 'not_configured'
      ? 'bg-gray-300'
      : 'bg-red-400';

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className={`h-1 ${stripeColor}`} />
      <div className="p-4">
        <h3 className="text-[15px] font-semibold text-gray-900">{providerName}</h3>

        {group.map((provider) => (
          <div key={provider.key_index ?? 'single'} className={isMultiKey ? 'mt-3' : ''}>
            {isMultiKey && (
              <div className="flex items-baseline justify-between mb-1">
                <span className="inline-flex items-center rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600">
                  #{provider.key_index}
                </span>
                <span className="tabular-nums text-[11px] text-gray-500">
                  {provider.key_masked ?? 'Not configured'}
                </span>
              </div>
            )}
            {!isMultiKey && (
              <div className="flex items-baseline justify-between gap-3 mt-1">
                <span />
                <span className={`tabular-nums text-[11px] ${provider.key_configured ? 'text-gray-500' : 'text-gray-400'}`}>
                  {provider.key_masked ?? 'Not configured'}
                </span>
              </div>
            )}

            {provider.ok ? (
              provider.usages.length === 0 ? (
                <p className="text-[12px] text-gray-400">No usage data returned.</p>
              ) : (
                <div className={isMultiKey ? 'mt-1.5 space-y-2' : 'mt-3 space-y-3'}>
                  {provider.usages.map((u, i) => {
                    const p = pct(u.used, u.limit);
                    return (
                      <div key={i}>
                        <div className="flex items-baseline justify-between text-[12px]">
                          <span className="text-gray-600">{u.label}</span>
                          <span className="tabular-nums text-gray-700">
                            {formatNum(u.used)}
                            {u.limit != null && ` / ${formatNum(u.limit)}`} {u.unit}
                            {p != null && <span className="ml-1 text-gray-400">({p.toFixed(0)}%)</span>}
                          </span>
                        </div>
                        {p != null && (
                          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-100">
                            <div
                              className={`h-full ${
                                p >= 90 ? 'bg-red-400' : p >= 70 ? 'bg-amber-400' : 'bg-gray-900'
                              }`}
                              style={{ width: `${p}%` }}
                            />
                          </div>
                        )}
                        {u.reset_at && (
                          <p className="mt-1 text-[11px] text-gray-400">
                            Resets at{' '}
                            {new Date(u.reset_at).toLocaleString('en-US', {
                              year: 'numeric', month: 'short', day: 'numeric',
                              hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
                            })}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )
            ) : (
              <p className={isMultiKey ? 'mt-1 text-[12px] text-gray-400' : 'mt-3 text-[12px] text-gray-400'}>
                Quota unavailable — <span className="text-gray-500">{provider.error}</span>
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
```

---

### Task 8: Run full test suite and lint

- [ ] **Step 1: Run lint**

Run: `make lint`

- [ ] **Step 2: Run tests**

Run: `make test`

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: per-key provider quota display in admin dashboard"
```
