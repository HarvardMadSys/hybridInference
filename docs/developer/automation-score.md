# Automation score (human vs. script)

The automation score rates how much of a user's API traffic comes from scripts rather than from a person, on a scale from `0.0` to `1.0`. High means scripts, batch jobs or cron; low means a person working interactively, in a chat UI or through a coding agent they drive, such as Claude Code. It is a heuristic for triage, not proof: always read it with the reported `confidence` and the per-signal breakdown.

```{note}
The score profiles a user's traffic. It reads that user's `api_logs` rows over the window: when they sent requests, how long their prompts were, which client they used, and precomputed statistics of their newest user message (length, character entropy and a hash; see signal 7). Only the admin endpoints and the operator CLI below compute it; users never see their own score, and no public route exposes it. A deployment that runs it on real traffic should say so in its privacy policy, and should use a score as input to a human decision, not as grounds for automated enforcement.
```

## Inputs

The score covers a trailing window, 30 days by default; the admin endpoints accept `days` from 1 to 90. Only rows with a `user_id` count, so anonymous traffic is left out. For each user the score gathers:

- the request count `N`, and `n_chat`, the rows where `num_user_turns IS NOT NULL` (chat requests; embeddings and raw completions leave it `NULL`);
- the number of one-shot requests (`num_user_turns = 1`) and the p90 of `num_user_turns`;
- the rows where `num_tool_calls IS NOT NULL`, and those where it is `> 0`;
- the 25th, 50th and 75th percentiles of positive `prompt_tokens`, and `n_sz`, the rows with `prompt_tokens > 0`;
- `agent_share`, the share of requests with a coding-agent opener (`metadata->>'agent'` present);
- a request-weighted breakdown of `metadata->>'user_agent'`;
- a UTC hour-of-day histogram (24 buckets) and the quartiles of the gaps between consecutive requests;
- the newest user message's length, entropy and hash statistics (signal 7).

## The signals

Seven signals each map a metric to a sub-score in `[0, 1]`, where high looks automated. The first four cover the axes the admin dashboard shows: user turns, turn length, user-agent and daily activity. Two supporting signals keep a person driving a high-volume coding agent from looking like a script, and `user_message_shape` looks at the user's own messages.

| Signal | Axis | Default weight | Available when |
|---|---|---|---|
| `turn_pattern` | user turns | 0.24 | `n_chat ≥ 5` |
| `prompt_size_dispersion` | total prompt size | 0.17 | `n_sz ≥ 8` and median > 0 |
| `user_message_shape` | user-message size & entropy | 0.15 | ≥ 1 part below |
| `client_tool_prior` | user-agent | 0.16 | always (≥ 1 request) |
| `daily_activity_shape` | daily activity | 0.27 | ≥ 1 timing part below |
| `tool_call_human_tell` | (support) | 0.08 | `n_chat ≥ 5` and some tool use |
| `agent_opener_override` | (support) | 0.08 | `agent_share ≥ 0.05` |

A signal without enough data is dropped and the remaining weights are re-normalized. It never counts as `0`, which would wrongly pull the score toward human. The weights total `1.15`; the score always divides by the weight available.

### 1. `turn_pattern`: user turns

An interactive session resends a growing history, so `num_user_turns` climbs `1, 2, 3, …`. A script that fires independent completions sends `num_user_turns = 1` almost every time.

```text
f1          = one_shot_chat_requests / n_chat          # fraction stuck at 1 user turn
depth_factor = 0.5 if p90(num_user_turns) >= 3 else 1.0
sub          = clamp01(f1 * depth_factor)
```

`depth_factor` halves the sub-score for a user who holds deep threads, so a person who also sends many one-shot requests is not marked as a script.

### 2. `prompt_size_dispersion`: turn length

`api_logs` has no per-role token counts, so this signal uses `prompt_tokens`: the whole input of a request, with the system prompt, the resent history, tool definitions and the latest message. A template produces near-constant sizes; a person's requests vary from a one-word follow-up to a long paste. The signal measures the relative spread (IQR / median), which ignores scale and resists one huge prompt:

```text
rcv = (p75 - p25) / median        # over positive prompt_tokens
sub = clamp01(1 - rcv / 0.5)      # rcv >= 0.5 -> 0 (human-varied); rcv = 0 -> 1 (templated)
```

### 3. `client_tool_prior`: user-agent

The User-Agent is the easiest signal to fake, so it is a soft, low-weight prior. Each request's User-Agent falls into a client class (a Python port of the frontend's `parseClientTool`) with an automation value:

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

SDKs are neutral because a chat UI can run on `openai-python`. The `agent_share` term lets a coding-agent opener (signal 6) pull toward human even behind a script-like User-Agent.

### 4. `daily_activity_shape`: daily activity

This is the hardest behavior to fake at scale. Up to four parts are combined, re-normalized over those that have enough data; high means automated:

| Part | Weight | Formula | Floor |
|---|---|---|---|
| Hour coverage | 0.20 | `clamp01((coverage - 0.5) / 0.5)`, `coverage = distinct active UTC hours / 24` | `N ≥ 10` |
| Hour entropy | 0.20 | `clamp01((Hnorm - 0.5) / (0.92 - 0.5))`, `Hnorm = ShannonEntropy(hours) / log2(24)`; the part saturates at the tuning constant `0.92` | `N ≥ 10` |
| Nightly rest gap | 0.30 | `clamp01(1 - max_quiet_gap_hours / 6)` | `N ≥ 10` |
| Inter-arrival regularity | 0.30 | `clamp01(1 - gap_rcv / 1.0)`, `gap_rcv = (p75 - p25) / median` of gaps | `≥ 3 gaps` |

`max_quiet_gap_hours` is the longest run of inactive clock hours, wrapping past midnight: a person's sleep pushes the rest-gap part toward 0, and round-the-clock operation toward 1. Hours are bucketed in UTC. The parts that ignore the timezone (regularity, rest gap and entropy) carry most of the weight, so an unknown timezone shifts the histogram without distorting the result.

### 5. `tool_call_human_tell`: supporting

Tool use (`num_tool_calls > 0`, from OpenAI `tool_calls` or Anthropic `tool_use` blocks) is typical of a coding loop a person drives. It only points toward human: without tool calls the signal is dropped, since no tool use says nothing about automation, and otherwise it never raises the score.

```text
sub = clamp01(0.5 - toolcall_share)   # share>=0.5 -> 0 (strongly human); share~0 -> ~0.5 (neutral)
```

### 6. `agent_opener_override`: supporting

`metadata->>'agent'` is a coding-agent identity parsed from the system-prompt opener (`"You are Claude Code, …"`). It comes from content, not a header, so it is hard to fake by accident and counts as strong evidence of a person. It is available only when the opener appears on at least 5% of requests, and it only pulls toward human:

```text
sub = clamp01(0.15 - agent_share)     # opener pervasive -> ~0 (human)
```

It also drives the hard human clamp in [Combining the signals](#combining-the-signals).

### 7. `user_message_shape`: the user's own messages

When a request is logged, `user_message_stats` (next to `conversation_shape`) stores three properties of its newest user message as cheap columns: length in characters, Shannon character entropy in bits per character, and a stable 64-bit hash of the stripped text. The score reads these columns, never the stored prompt. Three parts are combined, re-normalized over those with enough data; high means automated:

| Part | Weight | Formula | Floor |
|---|---|---|---|
| Size dispersion | 0.40 | `clamp01(1 - size_rcv / 0.5)`, `size_rcv = (p75 - p25)/median` of `last_user_msg_chars` | `≥ 8 sized msgs` |
| Per-message entropy | 0.25 | `clamp01(1 - mean_entropy / 4.0)` (natural language ≈ 4 bits/char) | `≥ 5 msgs` |
| Cross-message repetition | 0.35 | `clamp01((1 - distinct_ratio) / 0.5)`, `distinct_ratio = distinct(hash)/count` | `≥ 8 hashed msgs` |

A template sends messages of near-constant length, low-entropy structured payloads, and the same message again and again; a person varies all three. A script can look human here only by really varying what it sends. The columns are filled only for requests logged since they were added, so a user whose requests all predate them lacks this signal: the other six blend as usual, with slightly lower `confidence`.

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

- Re-normalization keeps the blend to the signals that had data, so a user who sends only embeddings (all `num_*_turns` columns `NULL`) is still scored on user-agent and daily activity.
- Shrinkage pulls low-traffic users toward `0.5`. Data and prior weigh the same at `N = 30`; at `N = 5`, `alpha ≈ 0.14`, so the score moves about 86% of the way to `0.5`.
- `confidence` falls with sparse data and with missing signals.
- Users with fewer than 5 requests are flagged `insufficient_data`.

## Bands

The score maps to an advisory band. Each band includes its lower bound (`SCORE_BANDS` is scanned from the top with `score >= lower`), so `0.35`, `0.60` and `0.80` fall in the higher band:

| Band | Range |
|---|---|
| `likely_human` | `0.00 ≤ s < 0.35` |
| `mixed_or_uncertain` | `0.35 ≤ s < 0.60` |
| `likely_automated` | `0.60 ≤ s < 0.80` |
| `scripted_batch` | `0.80 ≤ s ≤ 1.00` |

## Caveats

- No single signal decides, and each can be faked on its own, so the user-agent weighs little and behavior dominates.
- An account used across several timezones can show inflated hour coverage.
- A shared or team account mixes human and script traffic and rightly scores as mixed. `internal` and `admin` accounts may run automation legitimately; the score ignores roles, so exclude them from any automated enforcement.

## Reproducing it from the CLI

```bash
# rank the most script-like users in the last 30 days
python ops/db/analysis/user_automation_score.py --min-requests 20

# full per-signal breakdown for one user
python ops/db/analysis/user_automation_score.py --email user@example.com
```

## Code map

| Layer | Location |
|---|---|
| Core scoring + gather SQL | `apps/backend/serving/analytics/automation_score.py` |
| Store methods | `LogStore.get_user_automation_score` / `get_bulk_user_automation_scores` (`apps/backend/serving/storage/postgres_log.py`) |
| Admin endpoints | `GET /admin/users/{user_id}/automation-score`, `GET /admin/users/automation-scores` (`apps/backend/serving/servers/routers/admin/users.py`) |
| Admin dashboard | A per-user button and a bulk "Automation" column in the Users tab |
| CLI | `ops/db/analysis/user_automation_score.py` |

The admin endpoints and the CLI share one module, `serving.analytics.automation_score`, for the method and the SQL. Its pure scoring functions have no database dependency and are tested in `tests/unit/test_user_automation_score.py`.
