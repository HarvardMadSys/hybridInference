# Runnable router distribution example

This is a runnable backend/router distribution example: its manifest selects a
model registry and routing config, and a small Compose override adds a
deterministic OpenAI-compatible upstream. No provider key or local `.env` file
is required.

It intentionally stops at the first routed completion. Frontend packaging,
authentication, and visual branding belong to a later full-distribution
example; keeping them out of this path makes the router tutorial deterministic
and fast enough to run in CI.

From the repository root:

```bash
make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
```

The smoke command waits for startup and checks `/health`, `/site-config`,
`/v1/models`, and one non-streaming `/v1/chat/completions` request. A successful
run prints `EXAMPLE_SMOKE_OK`. Stop the example with:

```bash
make down DISTRIBUTION=example
```

`make build DISTRIBUTION=example` follows the same backend-only boundary when
you need to rebuild the gateway image.

The example uses its own Compose project and container names, so `down` cannot
remove another HybridInference stack. Its only shared host resource is port
8080; choose another port when one is already in use:

```bash
BACKEND_PORT=18080 make up DISTRIBUTION=example
BACKEND_PORT=18080 make smoke DISTRIBUTION=example
```

The model config uses ordinary `${VAR}` expansion. To point the same example at
a real OpenAI-compatible API, override its upstream values when starting it:

```bash
EXAMPLE_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1 \
EXAMPLE_UPSTREAM_API_KEY="$OPENROUTER_API_KEY" \
EXAMPLE_UPSTREAM_MODEL='<provider-model-id>' \
make up DISTRIBUTION=example
```

The deterministic smoke expects the bundled fake provider's sentinel. For a
real upstream, call `http://localhost:8080/v1/chat/completions` directly with
`model: example-chat` instead.
