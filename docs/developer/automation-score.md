# Automation score (human vs. script)

The **automation score** rates how *script-driven* vs. *human-driven* each user's
API traffic is, on a `0.0`–`1.0` scale:

- **HIGH (→ 1.0)** — traffic is mostly driven by automatic scripts / batch jobs / cron.
- **LOW (→ 0.0)** — a human is using the service interactively (a chat UI, or a
  human-driven coding agent such as Claude Code).

It is a **heuristic for triage, not a verdict**. Always read it together with the
reported `confidence` and the per-signal breakdown.

## Where it lives

| Layer | Location |
|---|---|
| Core scoring + gather SQL | `apps/backend/serving/analytics/automation_score.py` |
| Store methods | `LogStore.get_user_automation_score` / `get_bulk_user_automation_scores` (`serving/storage/postgres_log.py`) |
| Admin endpoints | `GET /admin/users/{user_id}/automation-score`, `GET /admin/users/automation-scores` (`serving/servers/routers/admin/users.py`) |
| Admin dashboard | per-user button + bulk "Automation" column in the Users tab |
| CLI | `ops/db/analysis/user_automation_score.py` |

The scoring methodology and the SQL that gathers per-user aggregates live in **one**
module (`serving.analytics.automation_score`), shared by the admin endpoints and the
CLI, so the two can never drift. The pure scoring functions have no database
dependency and are unit-tested in `tests/unit/test_user_automation_score.py`.

## Inputs

The score is computed over a trailing **window** (default 30 days; the admin
endpoints accept `days` in `1..90`). Only `api_logs` rows with a non-null
`user_id` are considered — anonymous traffic is excluded. For each user the
following per-user aggregates are gathered:

- request count `N`, and `n_chat` = rows where `num_user_turns IS NOT NULL`
  (chat-style requests; `NULL` for embeddings / raw completions);
- one-shot count (`num_user_turns = 1`) and the p90 of `num_user_turns`;
- tool-call counts (`num_tool_calls IS NOT NULL`, and `> 0`);
- the 25/50/75th percentiles of positive `prompt_tokens`;
- the share of requests carrying a coding-agent opener (`metadata->>'agent'`
  present — see the `agent_opener_override` signal below);
- a request-weighted breakdown of `metadata->>'user_agent'`;
- a UTC hour-of-day histogram (24 buckets);
- the 25/50/75th percentiles of the inter-arrival gaps between consecutive
  requests.

## The signals

The score blends seven signals. Each maps its raw metric to an **automation
sub-score** in `[0, 1]` (HIGH = looks automated), and has a default weight. The
four signals the feature was originally specified around are *user turns*, *turn
length*, *user-agent*, and *daily activity*; two small supporting signals
disambiguate the main confounder (a high-volume but human-driven coding agent);
and `user_message_shape` measures the user's own messages specifically.

| Signal | Axis | Default weight | Available when |
|---|---|---|---|
| `turn_pattern` | user turns | 0.24 | `n_chat ≥ 5` |
| `prompt_size_dispersion` | total prompt size | 0.17 | `n_sz ≥ 8` and median > 0 |
| `user_message_shape` | user-message size & entropy | 0.15 | ≥ 1 part below |
| `client_tool_prior` | user-agent | 0.16 | always (≥ 1 request) |
| `daily_activity_shape` | daily activity | 0.27 | ≥ 1 timing part below |
| `tool_call_human_tell` | (support) | 0.08 | `n_chat ≥ 5` and some tool use |
| `agent_opener_override` | (support) | 0.08 | `agent_share ≥ 0.05` |

A signal that lacks enough data for a user is **dropped**, and the remaining
weights are re-normalized over the survivors — a missing signal is never imputed
as `0` (which would falsely pull the score toward "human"). The weights need not
sum to `1.0` (they total `1.15`); the runtime always divides by the available
weight. `user_message_shape` was added later at weight `0.15` **without changing
the original six**, so a user lacking the (newer) user-message columns — e.g.
traffic logged before the migration — drops it and gets the **same blended score
as before**, only a slightly lower `confidence`.

### 1. `turn_pattern` — user turns

An interactive session resends a growing history, so `num_user_turns` climbs
`1, 2, 3, …`; a script firing independent one-shot completions emits
`num_user_turns = 1` on essentially every request.

```text
f1          = one_shot_chat_requests / n_chat          # fraction stuck at 1 user turn
depth_factor = 0.5 if p90(num_user_turns) >= 3 else 1.0
sub          = clamp01(f1 * depth_factor)
```

The `depth_factor` halves the sub-score for a user who has demonstrably held deep
(`p90 ≥ 3`) multi-turn threads, so a human who *also* fires many one-shot requests
is not branded a script.

### 2. `prompt_size_dispersion` — length of user turn

This uses `prompt_tokens`, which (OpenAI semantics) is the **total input** for the
request — the system prompt + the whole conversation history resent that turn +
tool definitions + the latest user message — not the user message in isolation
(`api_logs` has no per-role token breakdown). It is the only available proxy for
"length of user turn". Templated automation assembles each request from a fixed
template, so its total prompt size clusters tightly; an interactive human's
requests vary widely (a one-word follow-up, then a long paste). The discriminator
is therefore the **robust relative dispersion** (IQR / median) of that total size,
which is scale-free and resistant to a single huge pasted prompt:

```text
rcv = (p75 - p25) / median        # over positive prompt_tokens
sub = clamp01(1 - rcv / 0.5)      # rcv >= 0.5 -> 0 (human-varied); rcv = 0 -> 1 (templated)
```

### 3. `client_tool_prior` — user-agent

The User-Agent is the most *spoofable* signal, so it is a soft, low-weight prior,
never decisive. Each request's UA is classified into a client class (a Python
port of the frontend `parseClientTool`), with a per-request automation value:

| Client class | Examples | Value |
|---|---|---|
| Interactive / coding agent | `claude-code`, `cline`, `cursor`, `codex`, `browser` | `0.10` |
| Ambiguous SDK | `openai-python`, `openai-node`, `anthropic-python`/`-sdk` | `0.50` |
| Raw HTTP library / API tool | `python-requests`, `httpx`, `aiohttp`, `curl`, `wget`, `okhttp`, `axios`, `go-http`, `postman`, … | `0.85` |
| Unknown (recognized leading token) | `myagent/1.0` | `0.60` |
| Absent / unrecognized UA | — | `0.70` |

```text
ua_base = request-weighted mean of the per-request class values
sub     = clamp01(ua_base * (1 - 0.85 * min(agent_share, 1)))
```

SDKs sit at a neutral `0.5` because a human chat UI can sit on top of
`openai-python`. The `agent_share` term lets the coding-agent opener (see below)
pull the prior toward human even behind a scripty UA.

### 4. `daily_activity_shape` — daily activity

The behavioral fingerprint hardest to fake at scale. Up to four parts are fused
and re-normalized over whichever pass their data floors. Each is HIGH = automation:

| Part | Weight | Formula | Floor |
|---|---|---|---|
| Hour coverage | 0.20 | `clamp01((coverage - 0.5) / 0.5)`, `coverage = distinct active UTC hours / 24` | `N ≥ 10` |
| Hour entropy | 0.20 | `clamp01((Hnorm - 0.5) / (0.92 - 0.5))`, `Hnorm = ShannonEntropy(hours) / log2(24)` | `N ≥ 10` |
| Nightly rest gap | 0.30 | `clamp01(1 - max_quiet_gap_hours / 6)` | `N ≥ 10` |
| Inter-arrival regularity | 0.30 | `clamp01(1 - gap_rcv / 1.0)`, `gap_rcv = (p75 - p25) / median` of gaps | `≥ 3 gaps` |

`max_quiet_gap_hours` is the longest *circular* run of inactive clock-hours — a
human's nightly sleep gap pushes the rest-gap part toward 0, while 24/7 operation
leaves no gap (→ 1). The timezone-invariant parts (regularity, rest gap, entropy)
carry most of the weight, so an unknown user timezone shifts the histogram without
distorting the verdict (hours are bucketed in **UTC**).

### 5. `tool_call_human_tell` — supporting

Agentic tool use (`num_tool_calls > 0`, from OpenAI `tool_calls` / Anthropic
`tool_use` blocks) is the fingerprint of a human-driven coding loop. It is a
**one-directional** human tell: it is *dropped entirely* when there are no tool
calls (absence of tool use is not evidence of automation), and otherwise can only
pull the score toward human, never raise it:

```text
sub = clamp01(0.5 - toolcall_share)   # share>=0.5 -> 0 (strongly human); share~0 -> ~0.5 (neutral)
```

### 6. `agent_opener_override` — supporting

`metadata->>'agent'` is a coding-agent identity parsed from the system-prompt
opener (`"You are Claude Code, …"`). Because it is content-derived (not a header),
it is hard to fake incidentally and is treated as strong human evidence. It is
available only when the opener appears on ≥ 5% of requests, so it can only pull
toward human; its absence is uninformative:

```text
sub = clamp01(0.15 - agent_share)     # opener pervasive -> ~0 (human)
```

It also drives the **hard human clamp** in the combination step.

### 7. `user_message_shape` — user-message size & entropy

Where `prompt_size_dispersion` looks at the *whole* input, this signal looks at
the **user's own messages**. Three properties of the **newest user-role message**
per request are precomputed at log time (in `user_message_stats`, alongside
`conversation_shape`) as cheap columns — char length, Shannon character entropy
(bits/char), and a stable 64-bit hash of the stripped text — so the score never
de-TOASTs the prompt. Three parts are fused and re-normalized over whichever pass
their floors, each HIGH = automation:

| Part | Weight | Formula | Floor |
|---|---|---|---|
| Size dispersion | 0.40 | `clamp01(1 - size_rcv / 0.5)`, `size_rcv = (p75 - p25)/median` of `last_user_msg_chars` | `≥ 8 sized msgs` |
| Per-message entropy | 0.25 | `clamp01(1 - mean_entropy / 4.0)` (natural language ≈ 4 bits/char) | `≥ 5 msgs` |
| Cross-message repetition | 0.35 | `clamp01((1 - distinct_ratio) / 0.5)`, `distinct_ratio = distinct(hash)/count` | `≥ 8 hashed msgs` |

Templated automation sends near-constant-length user messages (low size
dispersion), low-entropy structured payloads, and resends the same message over
and over (low distinct ratio → high repetition); an interactive human varies all
three. Because the columns are populated only for new traffic, the signal is
simply unavailable (dropped) for users whose requests all predate the migration.

This signal isolates the user input, which the metadata in `metadata->>'agent'`
and the per-message hash make hard to spoof without actually varying the content.

## Combining the signals

```text
A          = signals available for this user
weight_sum = sum(weight_i for i in A)
raw        = sum(sub_i * weight_i for i in A) / weight_sum      # re-normalized blend

# Hard human clamp: a high-volume coding-agent user with a real nightly rest gap
# can never be branded above "mixed" on volume alone.
if agent_share >= 0.3 and rest_gap_part_available and rest_gap_score < 0.5:
    raw = min(raw, 0.5)

# Confidence shrinkage toward the neutral 0.5 prior for low-volume users.
alpha = N / (N + 30)
score = clamp01(alpha * raw + (1 - alpha) * 0.5)

# coverage = available weight / total signal weight, so confidence stays in [0,1].
confidence = alpha * (weight_sum / TOTAL_WEIGHT)
```

- **Re-normalization** (`raw`) makes the blend depend only on the signals that
  actually had data, so a pure-embeddings batch user — whose `num_*_turns` columns
  are all `NULL` — is still scored on its user-agent and daily-activity shape.
- **Shrinkage** (`alpha = N / (N + 30)`) pulls users with little traffic toward
  the neutral `0.5` prior. Data outweighs the prior at `N = 30` (`alpha = 0.5`);
  at `N = 5`, `alpha ≈ 0.14` (the score is pulled ~86% to `0.5`).
- **`confidence`** combines the request volume (`alpha`) with the share of the
  total signal weight that was actually available (`weight_sum / TOTAL_WEIGHT`),
  so sparse verdicts — and users missing the newer signals — read as
  lower-confidence.
- Users with `N < 5` requests are flagged **`insufficient_data`**.

## Bands

The final score maps to a band label (advisory — always read with `confidence`):

| Band | Range |
|---|---|
| `likely_human` | `0.00 – 0.35` |
| `mixed_or_uncertain` | `0.35 – 0.60` |
| `likely_automated` | `0.60 – 0.80` |
| `scripted_batch` | `0.80 – 1.00` |

## Caveats

- **It's a heuristic.** No single signal is decisive; each is individually
  spoofable, so the user-agent is weighted low and behavioral signals dominate.
  Treat the score as triage, not proof.
- **Timezone.** Hour-of-day is bucketed in UTC; the design leans on the
  timezone-invariant daily-activity parts so a non-UTC human is not mislabeled as
  night-active, but a genuinely multi-timezone account can inflate hour coverage.
- **Shared / role accounts.** Shared or team accounts blend human and script
  traffic into a mid "mixed" score (correct). `internal` / `admin` accounts may
  legitimately run automation — the score stays role-agnostic, so exclude them
  from any automated enforcement and read the score with their role in mind.

## Reproducing it from the CLI

```bash
# rank the most script-like users in the last 30 days
python ops/db/analysis/user_automation_score.py --min-requests 20

# full per-signal breakdown for one user
python ops/db/analysis/user_automation_score.py --email a@x.com
```
