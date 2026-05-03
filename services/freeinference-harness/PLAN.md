# FreeInference Harness Plan

## Goal

Build a standalone black-box harness for FreeInference that exercises
the public API through real HTTP clients and third-party SDKs.

The harness must answer two different questions without mixing them together:

- Is a single provider-backed route correct and stable?
- Is the public logical model stable after routing, fallback, and health logic?

## Scope

This project only talks to deployed APIs.

It must not import internal gateway code such as:

- adapters
- router executors
- processors
- request serializers

The only required inputs are:

- base URL
- API key
- model ID
- suite definition
- sampling count

The standalone harness is now the only maintained black-box entry point for
this workflow. The earlier pytest prototype under `test/external/` is being
retired to avoid duplicate logic and drifting pass/fail rules; migration is
tracked as part of Phase 2.

## Test Layers

### 1. Gateway-Pinned

Purpose:

- Test a single provider-backed route behind the FreeInference API surface.
- Avoid routing randomness and fallback interference.

Requirements:

- Each target must use a provider-pinned model ID such as
  `glm-5-zhipu-only`.
- Each pinned target should come from a dedicated harness deployment config or
  a separate gateway environment.
- Avoid adding long-lived test-only targets to the primary production
  `config/models.yaml`.
- Each pinned model should be `admin_only: true` when exposed through a shared
  gateway.
- Each pinned model must have exactly one active route.

Questions answered:

- Does this route actually satisfy the OpenAI-compatible contract?
- Is the route stable across repeated samples?

### 2. Gateway-Public

Purpose:

- Test the public logical model exactly as a real user sees it.

Requirements:

- Use public route model IDs such as `glm-5` or `minimax-m2.5`.
- Preserve routing, fallback, and health-manager behavior.

Questions answered:

- How stable is the user-facing API?
- Is routing introducing flakiness even when some individual providers pass?

### 3. Provider-Direct

Purpose:

- Optional debugging layer for proving whether a failure is upstream-only or
  introduced by the gateway path.

Notes:

- This is not the first shipping milestone.
- It is a diagnostic layer, not the main production gate.

## Phase Plan

### Phase 0: Prerequisites in `hybridInference`

Create provider-pinned targets in a harness-specific gateway configuration.

Preferred implementation:

- use a dedicated models config for the harness
- or use a separate gateway environment for pinned targets
- do not rely on long-lived test-only entries in the primary `config/models.yaml`

Initial pinned targets:

- `glm-4.7-flash-local-only`
- `glm-4.7-zhipu-only`
- `glm-5-zhipu-only`
- `glm-5-ollama-only`
- `minimax-m2.5-minimax-only`
- `minimax-m2.5-ollama-only`
- `qwen3-coder-30b-local-only`

Constraints:

- `admin_only: true`
- exactly one route per pinned model
- keep them out of the shared production-facing route set when possible
- do not add these IDs to `routing.yaml`

### Phase 1a: Repository Skeleton

Build the standalone repository structure with:

- CLI entrypoint
- YAML config loading
- target capability model
- scenario runner
- artifact writer
- markdown summary writer

### Phase 1b: Target and Scenario Definitions

Add YAML-driven target and scenario definitions.

Targets must declare `capabilities`, including:

- `chat`
- `streaming`
- `tools`
- `structured_output`
- `embeddings`
- `anthropic_messages`
- `admin_required`

The runner must skip unsupported scenarios automatically.

### Phase 2: Migrate Core Chat Scenarios

Move the old external pytest prototype logic into the standalone harness and
retire the duplicate implementation.

Initial scenarios:

- `non_stream_basic`
- `stream_basic`
- `forced_tool_call`

`forced_tool_call` pass criteria:

- streamed `tool_calls` must appear
- text-only fallback does not count as success
- tool-only responses are valid

### Phase 3: Repeated Sampling and Reporting

Add repeated sampling and artifact output.

Artifacts:

- `run.json`
- `attempts.jsonl`
- `summary.md`

Each run must capture:

- `run_id`
- `timestamp`
- target name
- scenario name
- status
- failure type
- latency
- selected model ID
- sampled response evidence

Add a minimal run-diff command to compare pass rates across runs.

### Phase 4: Public Route Stability

Add public-route suites for logical models such as:

- `glm-5`
- `glm-4.7-flash`
- `minimax-m2.5`

This layer measures user-visible stability, not provider purity.

### Phase 5: Additional Surfaces

Add:

- embedding suites
- Anthropic-surface client scaffolding
- optional provider-direct suites

## Scenario Inventory

### Chat Core

- `non_stream_basic`
- `stream_basic`
- `forced_tool_call`

### Embedding Core

- `embedding_basic`

## Result Classification

### Count as Pass

- non-streaming response with visible assistant `content` or `tool_calls`
- streaming response that reaches `[DONE]` and emits visible `content` or
  `tool_calls`
- forced tool-call response that actually emits `tool_calls`

### Count as Failure

- `200 OK` with empty assistant output
- stream ends with `[DONE]` but never emits visible content or tool calls
- forced tool-call request degrades into plain text
- provider timeout
- network error
- `5xx` response

### Count Separately

- `4xx` request-shape or auth problems should be recorded as `client_error`
  rather than provider regressions
- `429` should be recorded as `rate_limited`

## Deliverables

### Deliverable A

Repository skeleton that can load YAML targets and scenarios, perform dry runs,
and write artifacts.

### Deliverable B

Pinned targets available through a harness-specific deployment path.

### Deliverable C

First runnable `chat-core` suite against pinned FreeInference targets.

### Deliverable D

Repeated-sampling summaries that make flakiness measurable rather than anecdotal.
