# Bug Review — 2026-07-02

Whole-repo correctness review covering the routing engine (`apps/backend/routing/`),
the FastAPI serving core and SSE path (`apps/backend/serving/`), provider adapters
(`apps/backend/serving/adapters/`), storage/auth (`apps/backend/serving/storage/`,
`auth/`), admin/observability (`apps/backend/serving/admin/`, `observability/`),
services (`services/status-monitor-worker`, `services/freeinference-harness`), and the
Next.js frontend (`apps/frontend/`). Methodology: six parallel per-subsystem deep
reads hunting for concrete failure scenarios only (no style findings), followed by
independent re-verification of every high-severity finding against the code (and,
for the SSE parser, by execution). Out of scope: performance, architecture (see
`architecture-review-2026-05-03.md`), and live testing against staging.

**Totals: 47 unique findings — 7 high, 21 medium, 19 low.** One finding (the
bootstrap backfill mis-indent, H-class impact) was independently discovered by two
reviewers.

## Executive summary — high severity

1. **SSE frames are silently dropped when a CRLF delimiter straddles a read-chunk
   boundary.** `SSEParser.feed` normalizes `\r\n` per chunk, so a `\r\n\r\n` frame
   boundary split across two 4 KB reads is never seen; two events merge, the merged
   `data` fails `json.loads`, and both deltas are dropped from the client stream and
   from usage accounting. Nondeterministic, repeats on long streams.
   ([apps/backend/serving/servers/sse.py:33-38](../../apps/backend/serving/servers/sse.py))
2. **`TimeoutMiddleware` hard-cancels streaming responses at 120 s total.**
   `anyio.move_on_after` wraps the whole ASGI call including `StreamingResponse` body
   iteration, so any stream longer than `REQUEST_TIMEOUT_SECONDS` (default 120) is cut
   mid-body with no `[DONE]` and no error event — despite the adapters deliberately
   using `ClientTimeout(total=None)` because "streaming responses can run for minutes".
   ([apps/backend/serving/servers/middleware/timeout.py:55-57](../../apps/backend/serving/servers/middleware/timeout.py))
3. **RouteWise failure recording bypasses the 4xx client-error guard.**
   `RouteWiseRouter` calls `_on_failure(...)` without `exc=`, so the
   `_is_client_error()` guard never runs and one user's 400s (e.g. context-length
   errors) open the circuit for every user of the endpoint — the exact regression the
   guard was added to prevent. `FixedRouter` passes `exc=` at all three call sites.
   ([apps/backend/routing/routewise/router.py:2620-2624, 2734-2738](../../apps/backend/routing/routewise/router.py))
4. **Streaming fallback can splice a second provider into a committed stream.** The
   `chunks_yielded` guard protects only the primary→fallback transition; inside the
   fallback loop a mid-stream failure just `continue`s to the next adapter, appending
   a fresh provider's full response to a stream that already emitted content.
   ([apps/backend/routing/routers.py:911-935, 1333-1361](../../apps/backend/routing/routers.py))
5. **Scheduled email broadcasts crash at fire time.** `_run_broadcast_sync` is a plain
   sync function, so APScheduler 3.11's `AsyncIOExecutor` runs it in a worker thread
   where `asyncio.create_task` raises `RuntimeError: no running event loop`. Every
   future-dated broadcast fails silently (it only sends if the process later restarts
   and rehydration fires it as "missed").
   ([apps/backend/serving/utils/email_scheduler.py:108-122](../../apps/backend/serving/utils/email_scheduler.py))
6. **Gemini streaming crashes on frames without `candidates`.** `candidate` is only
   assigned inside `if "candidates" in data:` but dereferenced unconditionally later;
   a blocked-prompt / usage-only frame raises `NameError` (or reads a stale candidate),
   aborting the stream. `data["candidates"][0]` also IndexErrors on an empty list.
   ([apps/backend/serving/adapters/gemini.py:450, 525](../../apps/backend/serving/adapters/gemini.py))
7. **Gemini multi-turn tool use is broken.** `_convert_messages_to_gemini` drops
   assistant `tool_calls` turns entirely (never reads `msg["tool_calls"]`) and loses
   the tool name on `role:"tool"` messages, so every post-tool-execution request gets
   a 400 from Gemini or a mismatched function identity.
   ([apps/backend/serving/adapters/gemini.py:88-116](../../apps/backend/serving/adapters/gemini.py))

All seven were re-verified directly against the code (finding 1 also by executing the
parser; finding 5 against the pinned apscheduler 3.11.2 executor source).

---

## Routing engine (`apps/backend/routing/`)

### R1 (high) — RouteWise 4xx failures trip the circuit breaker
See executive summary #3. Three context-limit 400s reach
`CIRCUIT_FAILURE_THRESHOLD=3` and `_build_candidates` skips the healthy endpoint for
the 30 s cooldown for all users.
`routewise/router.py:2620-2624` (chat) and `:2734-2738` (stream): pass `exc=exc`.

### R2 (high) — streaming fallback loop ignores `chunks_yielded`
See executive summary #4. `BaseRouter.stream_chat_completion` sets
`chunks_yielded = True` inside the fallback loop but never re-checks it before
`continue`; `FixedRouter`'s fallback loop never sets it at all. A fallback adapter
that fails after emitting 200 chunks is followed by the next adapter's complete
response on the same SSE stream — duplicated content and corrupt usage totals.

### R3 (medium) — hedged requests misattribute health/circuit outcomes
`_execute_adapter`/`_execute_stream_adapter` capture `endpoint_id` *before* awaiting;
`HedgedAdapter` swaps `self.config` to the backup when the backup wins, so
`_on_success` credits the primary even when the primary leg failed. Separately, the
hedge event sink records per-leg failures keyed by `adapter.config.provider`, but
circuits are keyed by `endpoint_id` (`{provider}:{host}:{port}`), so leg failures
accumulate on a phantom breaker that candidate-building never consults. Net effect: a
hard-down primary's circuit never opens and every request keeps paying the hedge
delay. Requires `latency_hedge_mode: probability_target`.
`routers.py:752-756, 771-779`; `routewise/hedging.py:244-248, 507-510, 691-694`.

### R4 (medium) — double concurrency-slot release on client disconnect mid-stream
On disconnect, the outer generator's `finally` releases the `ProviderReservation`;
the orphaned inner `_execute_stream_adapter` generator is finalized later by GC, finds
no reservation, and falls through to the direct `pool.release()` fallback —
decrementing the pool a second time while other requests hold slots. Repeated
disconnects permanently overcommit concurrency-limited providers (the `_active > 0`
guard only prevents going negative).
`routewise/router.py:2528-2551, 2774-2775, 1649-1662`.

### R5 (medium) — HealthMonitor results never affect routing weights
`RoutingManager.apply()` runs exactly once at bootstrap, when `HealthMonitor._status`
is empty (everything reads healthy); nothing re-applies weights when the background
loop later marks an endpoint unhealthy. Even if it were re-run, the "remaining
adapters" carry-over loop re-adds health-excluded adapters with their old weight.
`health_check:` in `routing.yaml` therefore drains nothing; only the independent
circuit breaker reacts.
`manager.py:63-79, 110-135`; caller `serving/servers/bootstrap.py:404-406`.

### R6 (low) — hedge explorer latency samples recorded twice per non-streaming request
`_apply_hedge_execution_metadata` runs in both the try body and the `finally`;
`_record_hedge_explorer_samples` is not idempotent, so the losing leg's TTFT is
recorded twice and the history prior is re-averaged, double-weighting the newest
sample in LP TTFT inputs. `routewise/router.py:2515-2526`.

Verified clean: `config.py`, `health.py`'s check loop, `model_router_registry.py`,
`executor.py`, `strategies/`, and routewise `lp.py` / `envelope.py` /
`effective_cost.py` / `concurrency.py` / `latency.py` / `predictor.py` / `quota.py` /
`candidates.py` / `prefix_cache.py`.

## Serving core (`apps/backend/serving/`)

### S1 (high) — SSE CRLF frame boundary lost across read chunks
See executive summary #1. Repro: feed `data: {"a":1}\r\n\r` then `\ndata: {"b":2}\r\n\r\n`
— yields one message with `data == '{"a":1}\n{"b":2}'`; `openai_compat` logs "Failed
to parse chunk" and drops both deltas. Fix: normalize after buffering (or split on a
regex that accepts `\r\n\r\n`), keeping a possible trailing `\r` in the buffer.
`servers/sse.py:27-38`; drop site `adapters/openai_compat.py:843-844`.

### S2 (high) — `TimeoutMiddleware` kills long streams
See executive summary #2. Because the cancellation is a `CancelledError`
(BaseException), it also bypasses `StreamSession`'s finalizers (see S3), so the
truncated request is never logged or billed. Streaming endpoints need an exemption or
an idle-based timeout. `servers/middleware/timeout.py:55-57`; wired in
`servers/app.py:84`.

### S3 (medium) — `StreamSession.stream` never finalizes on disconnect/cancellation
`_finalize_success` runs only on normal completion, `_finalize_failure` only under
`except Exception`; `GeneratorExit`/`CancelledError` bypass both — no cost increment,
no `api_logs` row, no routing observation. A client can stream 99 % of a completion,
disconnect before `[DONE]`, and never be charged. The Anthropic Messages handler
handles this exact case in a `finally` block (`anthropic_messages.py:1054-1096`),
confirming the omission. `servers/routers/completions_stream.py:287-432`.

### S4 (medium) — provider-stats backfill is mis-indented into a failure branch
*(independently found by two reviewers)* The `_run_backfill` task (`backfill_if_empty`
+ `backfill_token_columns`) sits inside `elif settings.slack_webhook_url.strip():` —
the branch taken only when APScheduler *failed to start* and Slack is configured. On
every normal startup the 30-day backfill silently never runs; the migration comment in
`database.py:908-912` that relies on it is dead. `servers/bootstrap.py:466-500`.

### S5 (medium) — runtime `user_auth_enabled` toggle silently reverts to env
`is_user_auth_enabled()` reads only `RuntimeSettings.get_cached(...)`, which expires
after the 30 s TTL and is never repopulated (the bootstrap warmup is its only writer).
An admin PATCH of `/admin/settings/user_auth_enabled` invalidates the cache and the
very next call falls back to the env value; the admin UI reads the DB directly and
shows the override as active, masking the failure. Security-relevant when env has
auth disabled and an admin "re-enables" it. `servers/auth.py:25-40`,
`config/runtime_settings.py:273-288`, `servers/routers/admin/settings.py:137`.

### S6 (medium) — non-streaming responses filtered through `response_model` drop fields
`sanitize_response` passthrough mode deliberately preserves
`reasoning`/`thinking`/`reasoning_content`, but FastAPI then re-serializes through
`ChatCompletionResponse` (`ChoiceMessage` has no `extra="allow"`), stripping
`reasoning`, `thinking`, `usage.prompt_tokens_details`/`completion_tokens_details`,
`system_fingerprint`, and `logprobs` — while streaming passes raw bytes and keeps them
all. Also: a provider usage object missing any required int field fails response
validation → client 500 *after* the DB log and cost increment fired.
`servers/routers/completions.py:356-359`, `schemas.py:89-130`,
`openai_chat_serializer.py:145-146`.

### S7 (low) — `models.yaml` env expansion doesn't support documented `${VAR:-default}`
`registry.py` does `os.getenv(val[2:-1])`, so `${VLLM_URL:-http://…}` looks up the
literal name `VLLM_URL:-http://…` and returns None even when the var is set → the
model is dropped at boot. `routing/config.py:18` and `observability/alert_config.py:16`
both implement the documented form. `servers/registry.py:282-284`.

### S8 (low) — keepalive path raises `HTTPException` mid-response-body
`_streaming_response_with_keepalive` raises on an upstream error chunk; after ≥1
keepalive byte has been sent the response is committed, so the raise aborts the
connection and the client gets a truncated whitespace body instead of the JSON error
object the generator could have yielded. `servers/routers/completions.py:260-268`.

## Provider adapters (`apps/backend/serving/adapters/`)

### A1 (high) — Gemini streaming `NameError` on candidate-less frames
See executive summary #6. `gemini.py:450, 525`.

### A2 (high) — Gemini drops assistant `tool_calls` turns from history
See executive summary #7. `gemini.py:88-116`.

### A3 (medium) — Gemini streamed tool calls all use `index: 0` and colliding IDs
Parallel function calls are emitted as separate deltas with hardcoded `index: 0`, so
OpenAI SDK clients concatenate the second call's arguments onto the first;
`call_{int(time.time()*1000)}` IDs collide within the same millisecond.
`gemini.py:495-496, 316`.

### A4 (medium) — Gemini non-streaming returns `finish_reason: "stop"` for tool calls
The streaming path maps `has_function_call → "tool_calls"` (`gemini.py:574-575`); the
non-streaming path has no equivalent, so agent frameworks branching on
`finish_reason == "tool_calls"` never execute the tool. `gemini.py:374-388`.

### A5 (medium) — Vertex (claude.py) streaming can't parse SSE-framed responses
`json.loads(line)` is called without stripping the `data: ` prefix; the HTTP helper
auto-detects SSE for `text/event-stream` or unknown Content-Types and yields
`data: {json}` lines, so every event fails and the whole stream is silently dropped.
`claude.py:241-256` vs. `http.py:276-277, 314-320`.

### A6 (medium) — `json_post_with_retry` retries deterministic 4xx/429
The retry loop catches all `aiohttp.ClientError`, which includes `ClientResponseError`
for every status: a 400 is re-POSTed up to 3×, a 429 is retried after 0.5 s/1 s
against the same key, and a post-generation 5xx re-triggers full non-streaming
generations (duplicate spend). `http.py:110-116`; callers in `claude.py`,
`anthropic.py`, `gemini.py`, `openai_compat.py`.

### A7 (medium) — `timeout=None` means *no* timeout, not the session default
aiohttp only substitutes the session default for the `sentinel` value; passing
`timeout=None` yields `ClientTimeout(total=None)`. Anthropic and Gemini non-streaming
calls pass it, so a stuck upstream hangs the request (and its concurrency slot)
forever. `http.py:56-91`; `anthropic.py:159-165, 322-328`; `gemini.py:288`.

### A8 (medium) — GLM/QwenCoder/ThinkBlock processors swallow the terminal chunk
The empty-delta + `finish_reason` chunk returns `[]` from `process_stream_chunk`, so
the real finish reason (`"length"`, `"content_filter"`) never reaches
`format_and_yield` and the gateway reports `"stop"` — clients cannot detect
truncation. `processors.py:92-143, 305-350, 519-527`; `openai_compat.py:730-733`.

### A9 (medium) — Anthropic→OpenAI translation emits user text before tool results
A user message containing `tool_result` blocks plus text becomes
`[assistant(tool_calls), user(text), tool(...)]`; strict upstreams reject with 400
because the tool message no longer follows the `tool_calls` message.
`anthropic_translator.py:121-137`.

### A10 (medium) — OpenAI→Anthropic stream translation never reports `input_tokens`
`message_start` fires with zeroed usage (real usage arrives only in the final OpenAI
chunk) and the terminal `message_delta` includes only `output_tokens` + cache fields,
so Anthropic-SDK clients see 0 input tokens on every streamed request.
`anthropic_translator.py:361-374, 432-449`.

### A11 (medium) — claude.py streaming: 120 s total timeout and no end-of-stream flush
`ClientTimeout(total=120)` kills long streams mid-body, and when the upstream ends
without `message_stop`, buffered tool calls are discarded and neither the usage chunk
nor `[DONE]` is emitted. The sibling `AnthropicAdapter` explicitly fixed both
(`anthropic.py:281-304`). `claude.py:238-443`.

### A12 (low) — `top_k` silently dropped by Claude/Anthropic adapters
`validate_params()` never returns `top_k`, so the adapters' `if "top_k" in
validated_params` checks are dead even when `supported_params` lists it.
`base.py:261-280`; `claude.py:114-115, 200-201`; `anthropic.py:145-146`.

### A13 (low) — vLLM `guided_json` only attached on the streaming path
`response_format.schema` produces schema-constrained output when `stream: true` and
unconstrained `json_object` mode otherwise. `openai_compat.py:683-689` vs `:622-624`.

### A14 (low) — `int(usage.get("completion_tokens", …))` TypeErrors on explicit null
The neighboring `prompt_tokens` line defends with `or 0`; this one doesn't, and a
null kills the Anthropic-SSE translation mid-finalization.
`anthropic_translator.py:404-406`.

### A15 (low) — GLM tool-XML parser merges all parallel tool calls into one
Only the first `<tool_call>` name is matched and `<arg_key>/<arg_value>` pairs from
*all* blocks are merged into one arguments object with a second-resolution ID.
`processors.py:209-248`.

### A16 (low) — Gemini adapter silently discards image content blocks
`_extract_text` drops `image_url` parts although the reference config declares
`input_modalities: ["text", "image"]` — vision requests reach the model with the
image stripped. `gemini.py:44-58`.

## Storage & auth (`apps/backend/serving/storage/`, `auth/`)

Note: there is no Cloudflare D1 backend under `apps/backend/serving/` — the storage
layer is Postgres-only (asyncpg). The D1 claim in CLAUDE.md §6.5 appears stale.

### D1 (medium) — admin API-key `metadata` dict is bound straight to a JSONB param
`CreateAPIKeyRequest.metadata`/`UpdateAPIKeyRequest.metadata` are `dict | None`, but
`create_key`/`update_key` expect a JSON *string* (`$9::jsonb`); no json codec is
registered on the pool, and asyncpg's jsonb codec only accepts str/bytes. Any admin
key create/update with non-null metadata → `asyncpg.DataError` → HTTP 500. Every
other JSONB write in the codebase goes through `json.dumps` first.
`servers/routers/admin/api_keys.py:80, 258`; `postgres_operational.py:1442-1459,
1534-1551`.

### D2 (medium) — api_keys operations keyed on bare `user_id` break with >1 key row
The regenerate flow (`revoke_key` + `create_key`) guarantees multiple rows per user.
`get_key_detail` (`SELECT … WHERE user_id = $1`, no status filter/ORDER BY) can return
the revoked row; `UPDATE api_keys SET … WHERE user_id = $1` rewrites *all* rows —
PATCHing `status: "active"` on a user with an active + revoked row violates
`idx_api_keys_user_unique` → 500; `regenerate_key` writes the same `key_hash` to every
row (guaranteed unique violation; latent, no HTTP caller today).
`postgres_operational.py:1522-1584`; `user_routes.py:623-630`.

### D3 (medium) — bootstrap backfill mis-indent
Same as S4 (cross-reported by the storage reviewer). `servers/bootstrap.py:466-500`.

### D4 (low) — `DatabaseLogger.get_stats` binds int to a text-typed param
`($1 || ' hours')::interval` infers `$1` as text; passing `hours` (int) raises
`DataError`. Latent — the live route uses `PostgresLogStore.get_stats`, which passes
`str(hours)`. `database.py:1201-1221`.

### D5 (low) — `NOW() AT TIME ZONE 'UTC'` day/month windows depend on session TZ
The naive timestamp is re-interpreted in the session TimeZone when compared to
`timestamptz` columns; nothing pins the pool's timezone. On a non-UTC Postgres server
all "today"/"month" dashboards shift by the offset while actual quota enforcement
(Python-computed UTC day key) stays true UTC — admin views and enforcement disagree.
`postgres_log.py` (9 sites), `postgres_operational.py` (7 sites), pool setup
`database.py:71-73`.

### D6 (low) — integration test calls `create_key(status=…)`, which doesn't exist
`pytest -m dbtest` fails in the helper before exercising `apply_role_quota`; masked in
CI because dbtest is excluded from `make test`.
`tests/integration/storage/test_postgres_role_quota.py:118-129`.

Verified clean: API-key HMAC auth (fails closed on empty key/secret), admin JWT+role
checks, signup allowlist, Argon2id hashing, parameterized SQL throughout (dynamic
UPDATE columns come from hardcoded allowlists), cache invalidation on revoke/role
changes.

## Admin & observability (`apps/backend/serving/admin/`, `observability/`)

### O1 (high) — scheduled broadcasts crash at fire time
See executive summary #5. `utils/email_scheduler.py:108-122`; verified against
apscheduler 3.11.2 (`AsyncIOExecutor._do_submit_job` dispatches non-coroutine funcs
via `run_in_executor`).

### O2 (medium) — alert cooldown stamped before Slack delivery
`_LAST_FIRED[key] = now` is set before `_post_to_slack`; a failed post loses the alert
*and* suppresses all retriggers of the same key for the full cooldown (900–3600 s) —
precisely when Slack/egress is flaky at incident start. `observability/alerts.py:254-266`.

### O3 (medium) — `AlertingLogHandler.emit` mutates an `asyncio.Queue` from foreign threads
The handler is on the root logger; `send_email` (via `asyncio.to_thread`), the
alert-host-facts daemon thread, and starlette threadpool code all log from non-loop
threads. `asyncio.Queue.put_nowait` is not thread-safe: waiter futures get completed
from the wrong thread (RuntimeError under debug mode; delayed/racy drain wakeups and
possible queue corruption otherwise). `observability/log_handler.py:20-31`;
attached at `servers/bootstrap.py:677`.

### O4 (medium) — Slack mrkdwn injection via request paths and upstream error text
`_format_message` interpolates context values raw; `top_paths` comes from
`record.path` verbatim and `sample_error` from `api_logs.error`. A client hitting
`/v1/<!channel>` >min_samples times pings the whole channel when the failed-request
rule fires; `<https://evil|text>` renders as a spoofed link. `escape_slack_text`
exists for exactly this but is only used in `routing/routers.py`.
`observability/alerts.py:190-212`; `alert_rules.py:138-157`;
`admin/failed_request_alerter.py:128-129, 236`.

### O5 (low) — broadcasts stuck in `sending` are never recovered
Rehydration selects only `status = 'scheduled'`; a crash mid-send orphans the
broadcast permanently with pending recipients. `utils/email_scheduler.py:66-83, 137-151`.

### O6 (low) — missed hourly rollups leave permanent gaps
`hourly_job` only aggregates `[now-1h, now)` and `backfill_if_empty` runs only on an
empty table, so any run missed beyond the 600 s misfire grace loses that hour forever
(false zero-traffic hour in dashboards). `admin/provider_stats_rollup.py:200-223, 352-370`.

### O7 (low) — numbered-key discovery off-by-one: max 19 keys despite `_MAX_KEYS = 20`
`range(2, _MAX_KEYS)` scans suffixes 2..19, so `{PREFIX}20` is never read.
`admin/provider_quotas.py:58, 74`.

## Services & frontend

### F1 (medium) — UserTable stale-response race can save one user's data onto another
`toggleDetail` has no sequence guard (unlike `RequestsTab`/`AuditTab` in the same
codebase): expand user A (slow fetch), expand user B; if A's response lands last, the
panel shows A's quota/disabled-models under B's row, and Save diffs against A's data
and PATCHes it onto B. A late *rejection* also collapses B's freshly opened panel.
`apps/frontend/src/app/dashboard/admin/users/UserTable.tsx:99-151`. **Verified.**

### F2 (medium) — every detail-panel Save PATCHes twice; first PATCH failure is silent
`doSave` awaits `apiUpdateUser(expandedId, patch)` and then `props.onUpdate(...)`,
which calls `updateUser(id, patch)` again — two identical PATCHes per save (duplicate
audit-log entries). `doSave` has `try/finally` with no `catch`: if the first PATCH
fails the admin gets no toast and the change silently didn't persist.
`UserTable.tsx:143-147` + `users/index.tsx:223-231`. **Verified.**

### F3 (low) — Analytics/TokenUsage period switches lack stale-response guards
Switch Day→Week; a slow "day" response landing last displays day numbers under the
week label. Same pattern in `TokenUsageTab`. `AnalyticsTab.tsx:301-316`,
`TokenUsageTab.tsx:78-93`.

### F4 (low) — harness schema validation passes booleans as integers
`isinstance(True, (int,))` is True, so `{"count": true}` passes an
`"integer"`-typed parameter and the harness false-passes exactly the tool-call
regressions it exists to catch.
`services/freeinference-harness/src/freeinference_harness/tool_validation.py:81-86`.

### F5 (low) — status-monitor cycle aborts without recording failure on D1 write errors
A throw in `recordResults`/`reconcileModels`/`prune` skips `finalizeCycle(ok:false)`
and the cycle alert; `/api/health` serves the previous cycle's green for up to
`CYCLE_FRESHNESS_MS` (60 min) while detected outages are neither shown nor alerted.
`services/status-monitor-worker/src/index.ts:143-151`.

Verified clean: the worker's cycle lock, edge-triggered Slack alerting, SSE probe
parsing, `mapPool`; harness runner control flow, stream accumulation, secret masking;
frontend auth/refresh single-flight, react-query hooks, playground SSE streaming.

---

## Suggested fix order

1. **S1 + S2 + S3** — the streaming data-loss cluster (dropped SSE frames, 120 s
   stream kill, unbilled/unlogged disconnects) directly corrupts user responses and
   billing.
2. **R1 + R2** — circuit-breaker poisoning and mid-stream provider splices affect
   availability and response integrity for all users.
3. **A1/A2** (if/when Gemini routes are enabled) and **O1** — hard crashes.
4. **S4/D3, S5, D1, D2, O2–O4** — operational correctness and admin-surface bugs.
5. The remaining mediums/lows opportunistically, each is small and localized.
