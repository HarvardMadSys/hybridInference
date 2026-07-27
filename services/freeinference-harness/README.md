# FreeInference Harness

`freeinference-harness` is a standalone black-box harness for validating the
FreeInference API through real HTTP clients and third-party SDKs.

The project intentionally does not import runtime code from `hybridInference`.
Its job is to exercise the public contract exactly as external clients see it.

## Repository Goals

- Measure provider-pinned correctness through the FreeInference API surface.
- Measure public-route stability separately from pinned-provider behavior.
- Turn flaky regressions into repeatable sampled results with artifacts.

## Initial Suites

- `chat-core`
- `embedding-core`
- `agent-loop-core` (deterministic conformance, see below)

## Quick Start

Set the required environment variables:

```bash
export FREEINFERENCE_BASE_URL="https://freeinference.org"
export FREEINFERENCE_API_KEY="hyi-..."
```

Run a dry-run to inspect target and scenario selection:

```bash
python -m freeinference_harness.cli run \
  --targets configs/targets/freeinference.yaml \
  --scenarios configs/scenarios/chat-core.yaml \
  --dry-run
```

Run a target:

```bash
python -m freeinference_harness.cli run \
  --targets configs/targets/freeinference.yaml \
  --scenarios configs/scenarios/chat-core.yaml \
  --target glm-5-public
```

Artifacts are written under `outputs/<run_id>/`.

## Agent-Loop Conformance (P-1 layer 1)

`agent-loop-core` is the deterministic protocol layer of the agent
compatibility probe (hybridInference issue #1041). A stdlib-only fake
provider replays shared scripts from
`src/freeinference_harness/agent_scripts.py`; the scenario executors assert
against the same definitions, isolating protocol failures (fragment
splicing, malformed-argument normalization, retry, truncation) from model
capability. Known production incidents are encoded as regressions: the
DeepSeek-V4/SGLang `{}""` tool-argument poison and the deepseek-v4-flash
empty-content stream (PR #935).

Run against the fake directly (no gateway needed):

```bash
python -m freeinference_harness fake-provider --port 8351 &
python -m freeinference_harness run \
  --targets configs/targets/agent-loop-local.yaml \
  --scenarios configs/scenarios/agent-loop-core.yaml \
  --target fake-direct
```

To also pin the gateway translation layer (including the Anthropic-surface
scenarios: ds4 normalization, poisoned-history echo, `count_tokens`),
register the fake in a dev gateway using
`configs/gateway/agent-loop-models.snippet.yaml` and run the
`gateway-local` target instead.

The `agent-loop-runtime` suite additionally spawns real agent CLIs (Claude
Code headless via `claude -p`, `codex exec`) against the target — missing
binaries skip cleanly. Against a gateway with the fake registered this is the
full deterministic `runtime -> gateway -> fake` chain, token-free.

Gateway-local baseline (2026-07-27, local dev gateway @ origin/dev): 12/13
core scenarios pass, plus the Claude Code runtime smoke.

The one intentionally-red scenario is a real gateway finding:
`openai_midstream_disconnect` — an upstream mid-stream disconnect is masked
with a synthesized `finish_reason: stop` + `[DONE]`, so a client cannot
detect truncation. For agent workloads that is a poisoning vector: truncated
tool-call arguments look complete.

An earlier note here claimed the gateway also swallowed upstream 429s into an
empty 200 stream. **That was wrong** — the gateway surfaces them as in-stream
SSE `error` frames; the harness simply did not parse those frames. It does
now (`stream_errors` in the stream observation), and the scenario passes. Two
narrower gateway nits remain, tracked separately: the frame types a 429 as
`server_error`, and no `[DONE]` follows it.

Unit tests for the fake and the executors live in `tests/`:

```bash
python -m pytest tests -q
```

See `PLAN.md` for the phased rollout.
