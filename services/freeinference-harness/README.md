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

See `PLAN.md` for the phased rollout.
