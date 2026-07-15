# `ops/db/analysis`

Offline utilities for inspecting exported `api_logs`, tokenizing prompts, inferring request sessions, and building Qwen-trace-style replay streams.

These scripts are intended for one-off analysis work after exporting logs from `ops/db/export_logs.py`.

## Directory contents

- `pretty_print_logs.py`
  - human-readable inspection for raw `api_logs` JSONL exports
- `tokenize_log_prompts.py`
  - converts raw prompt payloads into tokenized JSONL rows
  - can also emit hashed prompt chunks or Qwen-trace-style `hash_ids`
- `split_api_logs_sessions.py`
  - groups tokenized rows into inferred prompt sessions using prompt-prefix similarity
- `interleave_per_session_requests.py`
  - merges per-session Qwen-trace files into a controlled-concurrency replay stream
- `user_usage_pattern.py`
  - profiles one user's traffic shape (per-day/hour, models, clients, sizes) by querying the live DB
- `user_automation_score.py`
  - scores how script-driven vs. human-driven each user is, by querying the live DB
- `sample_trajectories.py`
  - infers agent trajectories from the live DB by message-context overlap
    (robust to history compaction), prints per-(model, harness) statistics, and
    writes the longest trajectories per pair to per-pair JSON files (last turn
    of each request only)
- `geo_hourly_export.py`
  - aggregates the live DB into privacy-safe hourly country demand buckets and
    `country x provider x served-endpoint` flows
    (`data.json` + CSV), resolving `metadata->>'ip'` with offline DB-IP Country
    Lite data; `--demo` emits synthetic data with the same shape
    (no DB / GeoIP needed)
- `geo_globe.html`
  - standalone browser viewer for `geo_hourly_export.py` output: rotating globe
    with request-origin heat, day/night terminator, flows to local providers,
    external-API rail, per-continent demand ribbon, and a range-wide
    pooling-potential KPI

`user_usage_pattern.py`, `user_automation_score.py`, `sample_trajectories.py`, and `geo_hourly_export.py` (without `--demo`) connect directly to PostgreSQL (via `.env` / `DB_*` env vars) rather than reading a JSONL export.

## Typical workflows

### 1. Inspect a raw export

Use this when you want to read prompts and responses directly.

```bash
uv run python ops/db/analysis/pretty_print_logs.py api_logs_export.jsonl
```

### 2. Analyze prompt shape and token counts

Use this when you want token counts, normalized prompt text, or hashed token chunks.

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  -o prompt_tokens.jsonl
```

### 3. Infer sessions from tokenized rows

Use this after tokenization when you want per-session files.

```bash
uv run python ops/db/analysis/split_api_logs_sessions.py prompt_tokens.jsonl \
  --output-dir per_session
```

### 4. Build a replay stream for Qwen-trace-style workloads

`interleave_per_session_requests.py` expects rows that already have Qwen-trace fields such as `chat_id`, `parent_chat_id`, `timestamp`, `input_length`, `output_length`, and `hash_ids`.

That means the replay pipeline is:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --hash-n 16 \
  --qwen-trace-format \
  -o qwen_trace.jsonl

uv run python ops/db/analysis/split_api_logs_sessions.py qwen_trace.jsonl \
  --output-dir per_session_qwen

uv run python ops/db/analysis/interleave_per_session_requests.py per_session_qwen \
  --concurrency 32 \
  --output replay.jsonl
```

### 5. Geo-temporal demand globe

Use this to study where requests come from, how demand moves with time zones,
and how much cross-region pooling could save. Aggregates only — no raw IPs,
user ids, or prompts leave the database.

```bash
# Download the current UTC monthly release (no account or secret required).
ops/setup/update_dbip_country_lite.sh

uv run python ops/db/analysis/geo_hourly_export.py --days 30 \
  --geoip-country var/data/geoip/dbip-country-lite.mmdb \
  --geoip-provider dbip-lite \
  --out data.json --csv geo_hourly.csv

# viewer (same directory as data.json)
cp ops/db/analysis/geo_globe.html .
python3 -m http.server 8000   # open http://localhost:8000/geo_globe.html
```

The deploy scripts run the same updater before each build on a best-effort
basis. A failed download retains the last good monthly database; without any
Country MMDB the endpoint and exporter still run, but origins degrade to
country `?`. The Python `maxminddb` package is only the generic MMDB reader.
Real [DB-IP Country Lite](https://db-ip.com/db/download/ip-to-country-lite)
data is lower-accuracy/coverage than DB-IP's paid database and is licensed
under CC BY 4.0. The viewer renders the required clickable "IP Geolocation by
DB-IP" attribution; synthetic demo data does not.

`--demo` generates synthetic data for viewer development (clearly badged in
the UI). Local provider coordinates are a hand-maintained map
(`PROVIDER_SITES`) — edit it when deployments move; API providers are
deliberately shown without a location claim.

## `pretty_print_logs.py`

Pretty-prints raw `api_logs` JSONL rows for manual inspection.

It can:

- show prompts and responses with simple role-based coloring
- decode chat-style prompt arrays
- decode tool-call arguments when possible
- filter by record ID
- print a summary table instead of full payloads

### Common commands

Default view:

```bash
uv run python ops/db/analysis/pretty_print_logs.py api_logs_export.jsonl
```

Summary only:

```bash
uv run python ops/db/analysis/pretty_print_logs.py api_logs_export.jsonl --summary
```

Single-record debugging:

```bash
uv run python ops/db/analysis/pretty_print_logs.py api_logs_export.jsonl --id 12345
```

Hide responses and limit large blocks:

```bash
uv run python ops/db/analysis/pretty_print_logs.py api_logs_export.jsonl \
  --no-response \
  --truncate 500
```

### CLI options

- `--truncate N`
  - max characters per displayed content block
  - `0` means unlimited
- `--no-prompt`
  - hide prompt output
- `--no-response`
  - hide response output
- `--no-color`
  - disable ANSI colors
- `--id ID [ID ...]`
  - only print selected record IDs
- `--summary`
  - print a compact table instead of prompt/response bodies

## `tokenize_log_prompts.py`

Converts exported `api_logs` prompt payloads into JSONL rows containing token counts, raw token IDs, deterministic seeded integer prompt-chunk hashes, or Qwen-trace-style integer `hash_ids`.

This is the main normalization step for downstream analysis.

### What it accepts

The default input is the JSONL export produced by `ops/db/export_logs.py`.

A typical row looks like:

```json
{
  "id": 123,
  "request_id": "req_abc",
  "timestamp": "2026-05-08T00:00:00",
  "model_id": "glm-4.5",
  "provider": "zhipu",
  "prompt_tokens": 791,
  "completion_tokens": 532,
  "prompt": "[{\"role\":\"user\",\"content\":\"hello\"}]",
  "tools": "[{\"type\":\"function\",\"function\":{\"name\":\"bash\",\"parameters\":{\"type\":\"object\"}}}]"
}
```

The tokenizer handles three main input cases:

1. Normal exported `api_logs` rows with `prompt` and optional `tools`
2. Chat-style prompt payloads stored as JSON strings
3. Existing Qwen-trace-style rows that already contain `hash_ids`

It normalizes:

- plain text prompts
- JSON chat-message arrays
- OpenAI-style tool definitions
- OpenAI-style tool call argument strings
- some incomplete tool-call payloads by repairing missing names and IDs when possible

### Output modes

The script supports three main output modes:

1. Default tokenized output with `prompt_token_ids`
2. Hashed output with `--hash-n`
3. Qwen-trace-style output with `--hash-n --qwen-trace-format`

### Default tokenized output

Without `--hash-n`, the script emits the common tokenized schema.

Example:

```json
{
  "line_number": 1,
  "id": 123,
  "request_id": "req_abc",
  "timestamp": "2026-05-08T00:00:00",
  "model_id": "glm-4.5",
  "provider": "zhipu",
  "tokenizer": "zai-org/GLM-5.1",
  "tool_count": 1,
  "logged_prompt_tokens": 791,
  "logged_output_tokens": 532,
  "computed_prompt_tokens": 812,
  "prompt_tokens_delta": 21,
  "prompt_token_ids": [151329, 151330, 42]
}
```

Optional fields:

- `prompt_text` is included with `--include-text`
- `prompt_token_ids` is omitted with `--count-only`
- `prompt_tokens_delta` is only present when the input row has `prompt_tokens`
- `logged_output_tokens` is the logged completion/output token count when present

### Hashed output with `--hash-n`

When `--hash-n N` is used, the script replaces raw `prompt_token_ids` with deterministic chained integer hashes over consecutive token chunks of size `N`.

Example:

```json
{
  "id": 123,
  "computed_prompt_tokens": 812,
  "prompt_token_ids": [
    4037369005,
    1777168435,
    332557981
  ],
  "token_id_hash_n": 16,
  "token_id_hash_algorithm": "seeded-int-chained"
}
```

Each chunk hash depends on:

- the current token chunk
- the previous chunk hash

That makes the chunk sequence order-sensitive while remaining deterministic for the same prompt.

### Qwen-trace-style output

When both `--hash-n` and `--qwen-trace-format` are used, the script emits rows shaped like a Qwen trace file.

Example command:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --hash-n 16 \
  --qwen-trace-format \
  --workers 1 \
  -o qwen_trace.jsonl
```

Example output row:

```json
{
  "chat_id": 123,
  "parent_chat_id": -1,
  "timestamp": "2026-05-08T00:00:00",
  "input_length": 812,
  "output_length": 532,
  "type": "text",
  "turn": 1,
  "hash_ids": [0, 1, 2]
}
```

Field mapping in Qwen-trace mode:

| Output field | Source |
|---|---|
| `chat_id` | `record["id"]` |
| `parent_chat_id` | always `-1` |
| `timestamp` | `record["timestamp"]` |
| `input_length` | computed prompt token count |
| `output_length` | first available of `completion_tokens`, `output_tokens`, `response_tokens`, else `0` |
| `type` | `record["type"]` if present, else `"text"` |
| `turn` | `record["turn"]` if present, else `1` |
| `hash_ids` | deterministic integer hashes assigned per token chunk |

In Qwen-trace mode, the script does not build a compact per-run vocabulary. It writes the deterministic hashed chunk integers directly into `hash_ids`.

### Existing Qwen-trace input rows

If an input row already contains `hash_ids`, the script recognizes it automatically and converts it into the common internal tokenized schema.

Example output for Qwen-trace input:

```json
{
  "line_number": 1,
  "id": 3,
  "request_id": null,
  "timestamp": 0.25,
  "model_id": null,
  "provider": "qwen-trace",
  "tokenizer": "qwen-trace-format",
  "tool_count": 0,
  "logged_prompt_tokens": 64,
  "computed_prompt_tokens": 3,
  "chat_id": 3,
  "parent_chat_id": -1,
  "type": "text",
  "turn": 1,
  "input_length": 64,
  "output_length": 12,
  "prompt_token_ids": [7, 8, 9],
  "token_id_hash_algorithm": "qwen-trace-hash-ids"
}
```

This is useful when you want to run downstream tools such as `split_api_logs_sessions.py` on an existing trace file.

### CLI reference

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py INPUT_JSONL [options]
```

Arguments:

- `input`
  - path to the input JSONL file

Options:

- `--tokenizer TOKENIZER`
  - Hugging Face tokenizer name or local path
  - default: `zai-org/GLM-5.1`
- `-o, --output PATH`
  - write output JSONL to a file
  - default: stdout
- `--trust-remote-code`
  - pass `trust_remote_code=True` to `AutoTokenizer.from_pretrained`
- `--no-add-generation-prompt`
  - do not append the assistant generation marker to chat-template rendering
- `--count-only`
  - write token counts only, omitting `prompt_token_ids`
- `--include-text`
  - also include the normalized prompt text that was tokenized
- `--hash-n N`
  - replace raw token IDs with deterministic chained integer hashes over chunks of `N`
- `--qwen-trace-format`
  - when used with `--hash-n`, emit Qwen-trace-style rows with integer `hash_ids`
- `--debug-failing-tools`
  - print row identifiers and raw/parsed tool payloads before re-raising tokenization failures
- `--workers N`
  - number of rows to tokenize in parallel
- `--id ID [ID ...]`
  - only convert rows with the listed `id` values

### Validation rules

The script rejects these combinations:

- `--hash-n <= 0`
- `--workers <= 0`
- `--qwen-trace-format` without `--hash-n`
- `--qwen-trace-format` together with `--count-only`

### Common commands

Default tokenization:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  -o prompt_tokens.jsonl
```

Include normalized prompt text:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --include-text \
  -o prompt_tokens_with_text.jsonl
```

Hashed prompt chunks as deterministic chained integers:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --hash-n 16 \
  -o prompt_hashes.jsonl
```

Qwen-trace-style output with integer `hash_ids`:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --hash-n 16 \
  --qwen-trace-format \
  --workers 16 \
  -o qwen_traceA_blksz_16.jsonl
```

Single-ID debugging:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --id 12345 \
  --include-text \
  --workers 1
```

Debug failing tool payloads:

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --id 12345 \
  --workers 1 \
  --debug-failing-tools
```

### Performance and memory notes

The script streams input parsing and output writing instead of buffering the entire file in memory.

Memory use is still affected by:

- tokenizer size
- prompt length
- number of worker processes
- whether raw `prompt_token_ids` are emitted
- the number of hashed chunks emitted per prompt

For the lowest memory footprint:

- prefer `--count-only` when raw IDs are unnecessary
- prefer `--hash-n` over full raw token IDs for very long prompts
- use fewer workers for large tokenizers
- use more workers when tokenizer startup or prompt rendering dominates runtime

## `split_api_logs_sessions.py`

Splits tokenized JSONL rows into inferred sessions using prompt-prefix similarity.

It reads tokenized rows, sorts them by timestamp when possible, and groups later requests into an earlier session when their prompt prefixes are similar enough.

### Accepted input

The splitter accepts rows produced by `tokenize_log_prompts.py` in either of these forms:

- common tokenized rows with `prompt_token_ids`
- Qwen-trace-style rows with `hash_ids`

Internally it normalizes both forms into a comparable ordered sequence.

### Matching logic

For each row with prompt hashes or token IDs:

1. sort by timestamp, then original line order
2. compare against a sliding window of previous prompt-bearing rows
3. compute positional prefix similarity over the shorter prompt prefix
4. reuse the most recent matching session when similarity is high enough
5. otherwise start a new session

Rows without usable prompt units become unclassified.

Default thresholds:

- `--min-hashes 1`
- `--prefix-similarity 0.60`
- `--window-size 20000`

### Output layout

By default the splitter writes into `per_session/` relative to the input file's directory.

Generated files:

- `session-000001.jsonl`, `session-000002.jsonl`, ...
  - original input rows for each inferred session
- `unclassified.jsonl`
  - only written when some rows cannot be classified
- `manifest.json`
  - summary of the run, including thresholds, invalid lines, and per-session metadata

The splitter can overwrite an existing output directory only when `--overwrite` is passed and the directory contains only previously generated files.

### CLI reference

```bash
uv run python ops/db/analysis/split_api_logs_sessions.py INPUT_JSONL [options]
```

Options:

- `--output-dir PATH`
  - directory where session JSONL files will be written
  - default: `per_session`
- `--overwrite`
  - replace an existing non-empty generated output directory
- `--min-hashes N`
  - minimum hash count required to join an existing session
- `--prefix-similarity F`
  - required prefix similarity from `0.0` to `1.0`
- `--window-size N`
  - number of previous requests to compare against
- `--no-manifest`
  - skip writing `manifest.json`

### Common commands

Default split:

```bash
uv run python ops/db/analysis/split_api_logs_sessions.py prompt_tokens.jsonl \
  --output-dir per_session
```

Stricter matching:

```bash
uv run python ops/db/analysis/split_api_logs_sessions.py prompt_hashes.jsonl \
  --output-dir per_session_strict \
  --min-hashes 4 \
  --prefix-similarity 0.8
```

Overwrite previous generated output:

```bash
uv run python ops/db/analysis/split_api_logs_sessions.py qwen_trace.jsonl \
  --output-dir per_session_qwen \
  --overwrite
```

### CLI output

After a successful run the script prints summary stats to stderr, including:

- total lines
- valid rows
- invalid lines
- classified and unclassified row counts
- session-size percentiles
- effective threshold values

## `interleave_per_session_requests.py`

Merges per-session Qwen-trace JSONL files into a replay stream with bounded concurrency.

This script is intended for replay-style workloads where each `session-*.jsonl` file represents a conversation timeline and you want to launch multiple sessions in parallel while preserving ordering within each session.

### Important requirement

The input session files must contain Qwen-trace rows with all of these fields:

- `chat_id`
- `parent_chat_id`
- `timestamp`
- `input_length`
- `output_length`
- `type`
- `turn`
- `hash_ids`

In practice, this means the session files should usually come from:

1. `tokenize_log_prompts.py --hash-n N --qwen-trace-format`
2. `split_api_logs_sessions.py` run on that Qwen-trace output

If you split the common tokenized schema instead, the resulting session files will not have the required Qwen-trace fields and this script will reject them.

### Scheduling behavior

For each `session-*.jsonl` file the script:

1. loads and validates rows
2. sorts rows by timestamp, then original line order
3. rewrites timestamps into relative offsets from that session's first row
4. launches up to `--concurrency` sessions at time `0`
5. starts each additional session when an earlier session finishes
6. rewrites `parent_chat_id` so each row points to the previous row's `chat_id` inside the same replayed session

The final merged stream is sorted by:

- rewritten timestamp
- session launch order
- row index within the session

### CLI reference

```bash
uv run python ops/db/analysis/interleave_per_session_requests.py INPUT_DIR [options]
```

Options:

- `--concurrency N`
  - number of sessions to keep active when possible
  - default: `4`
- `--output PATH`
  - merged JSONL output path
  - default: stdout
- `--overwrite`
  - allow replacing an existing output file

### Common commands

Write replay output to a file:

```bash
uv run python ops/db/analysis/interleave_per_session_requests.py per_session_qwen \
  --concurrency 16 \
  --output replay.jsonl
```

Write to stdout for piping:

```bash
uv run python ops/db/analysis/interleave_per_session_requests.py per_session_qwen \
  --concurrency 8
```

Replace an existing replay file:

```bash
uv run python ops/db/analysis/interleave_per_session_requests.py per_session_qwen \
  --concurrency 32 \
  --output replay.jsonl \
  --overwrite
```

## Recommended end-to-end examples

### Prompt-shape analysis only

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --hash-n 16 \
  -o prompt_hashes.jsonl

uv run python ops/db/analysis/split_api_logs_sessions.py prompt_hashes.jsonl \
  --output-dir per_session
```

### Replay dataset generation

```bash
uv run python ops/db/analysis/tokenize_log_prompts.py api_logs_export.jsonl \
  --hash-n 16 \
  --qwen-trace-format \
  --workers 16 \
  -o qwen_trace.jsonl

uv run python ops/db/analysis/split_api_logs_sessions.py qwen_trace.jsonl \
  --output-dir per_session_qwen

uv run python ops/db/analysis/interleave_per_session_requests.py per_session_qwen \
  --concurrency 32 \
  --output replay.jsonl
```

## `user_automation_score.py`

Scores how **script-driven vs. human-driven** each user's API usage is, by querying the live
`api_logs` / `users` tables (it needs the same `DB_*` env vars / `.env` as the other live-DB
tools, e.g. `user_usage_pattern.py`). Each user gets an `automation_score` in `[0, 1]` where
**HIGH means mostly automatic scripts / batch / cron** and **LOW means an interactive human**
(a chat UI, or a human-driven coding agent such as Claude Code).

> The exact signals, normalization formulas, weighting, and combination are documented in
> [docs/developer/automation-score.md](../../../docs/developer/automation-score.md). This CLI and
> the admin dashboard share that one implementation (`serving.analytics.automation_score`).

The score blends the four requested signals — plus two small supporting human tells — each mapped
to a `[0, 1]` automation sub-score and combined with re-normalized weights:

| Signal | Requested axis | What it measures |
|---|---|---|
| `turn_pattern` | user turns | fraction of one-shot (`num_user_turns = 1`) chat requests, dampened when the user holds deep multi-turn threads |
| `prompt_size_dispersion` | length of user turn | robust dispersion (IQR / median) of `prompt_tokens` — templated scripts are near-constant |
| `client_tool_prior` | user-agent | client class (interactive vs. raw HTTP lib vs. ambiguous SDK), overridable by the coding-agent opener |
| `daily_activity_shape` | daily activity | hour coverage, hour entropy, longest nightly quiet gap, and inter-arrival regularity (24/7 + metronomic ⇒ cron) |
| `tool_call_human_tell` | (support) | agentic tool use is a one-directional human tell |
| `agent_opener_override` | (support) | a `metadata->>'agent'` coding-agent opener pulls toward human (with a hard clamp) |

Signals lacking enough data for a user are **dropped and the remaining weights re-normalized**
(never imputed as 0), and the final score is shrunk toward a neutral `0.5` prior for low-volume
users, with a `confidence` reported alongside. Bands: `likely_human` (< 0.35),
`mixed_or_uncertain` (< 0.6), `likely_automated` (< 0.8), `scripted_batch` (≥ 0.8).

### Common commands

Rank the most script-like users in the last 30 days:

```bash
python ops/db/analysis/user_automation_score.py --min-requests 20
```

Profile one user with a full per-signal breakdown:

```bash
python ops/db/analysis/user_automation_score.py --email a@x.com
```

Machine-readable output (one record per scored user):

```bash
python ops/db/analysis/user_automation_score.py --min-requests 20 --json
```

### CLI options

- `--email EMAIL` — profile a single user instead of ranking all users
- `-n, --days N` — trailing window in days (default: 30)
- `--min-requests N` — only rank users with at least this many requests (default: 20)
- `--top N` — show the top-N most script-like users (default: 40)
- `--json` — emit JSON instead of the human-readable report
- `--env-file PATH` — path to `.env` (default: auto-detect)

The score is a heuristic for triage, not a verdict: always read it together with the reported
`confidence` and per-signal sub-scores, and treat shared/team and `internal`/`admin` accounts
(which legitimately mix human and automated traffic) with care.

## Limitations and notes

- `tokenize_log_prompts.py` uses `zai-org/GLM-5.1` by default; choose a different tokenizer when analysis must match another model family's chat template
- Qwen-trace output currently assumes `parent_chat_id = -1` when converting normal `api_logs` rows
- Qwen-trace output uses default `type = "text"` and `turn = 1` when those fields are absent
- hashed prompt chunks are deterministic seeded integers, not raw token IDs
- split-session quality depends on prompt-prefix similarity; it is heuristic rather than exact conversation reconstruction
- `interleave_per_session_requests.py` only accepts Qwen-trace-shaped rows
