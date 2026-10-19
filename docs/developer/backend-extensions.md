# Backend Extensions

A backend extension is a trusted Python module that a deployment loads into the
gateway at startup. It can add an adapter kind, decide who may use the Cloud
Agent, or report provider quotas. This page is for developers writing one; for
configuration-only changes, see
[Distribution customization](distribution-customization.md).

## Loading an extension

`BACKEND_EXTENSIONS` is an optional comma-delimited list of trusted, local Python
module names. It is empty by default. Each module must expose a synchronous
`register()` function that takes no arguments. The gateway imports and registers
each module once per process, after loading dotenv and before constructing
runtime routes or consuming their registries. An import or registration failure
aborts startup; the gateway does not silently use a different adapter.

Extensions register factories through
`serving.servers.registry.register_adapter_factory(kind, factory, *, override=False)`.
A factory receives a configuration dictionary and returns an adapter, before
built-in provider defaults are applied. Registering a kind that another
extension has already registered is an error. Replacing a built-in kind requires `override=True` and emits a log
entry. Registered kinds also become reserved provider labels. The startup
`register()` function may populate the existing runtime-setting and provider
metadata dictionaries before their consumers run; it must mutate those shared
dictionaries rather than replace them.

The deployment must make these modules importable, for example through its
read-only overlay mount. This executes trusted server code, not user-supplied
configuration: do not derive module names from requests or allow an admin form
to choose them. The loader does not fetch remote modules or load code per
request. Import all extension code during startup, avoid later lazy imports or
live code reloads, and restart the backend when changing the mounted extension.
See [Deployment-local adapters](provider-adapters.md#deployment-local-adapters)
for a minimal factory example.

## Cloud Agent access

The gateway keeps its fixed role hierarchy and resolves current account state.
The deployment owns the rule for who can use or administer its Cloud Agent.
A trusted extension registers one synchronous callback during `register()` with
`serving.agent_access.register_agent_access_policy(policy)`. The callback receives
a read-only mapping containing exactly `user_id` and `role`, after the gateway
has verified that the user exists and has status `active`. It receives no
profile, credentials, or database connection.

The callback returns a `list[str]` or `tuple[str, ...]` of explicit permissions:
`agent.use` permits Agent use, and `agent.admin` permits Agent administration.
Administration requires both permissions. Unknown, repeated, or non-string
permissions are invalid; values are not coerced. Registering a second policy is
an error. With no registered policy, Agent access is denied for every role.
The gateway imposes no deployment-specific role-to-permission rule.

The standalone Agent reads `GET /internal/users/{user_id}/agent-access` using
the existing `GATEWAY_GRANT_DISPATCH_TOKEN` bearer token. A successful response
contains exactly `user_id`, `allowed`, and `permissions`; `allowed` is true
exactly when `agent.use` is present. Permissions are returned in the order
`agent.use`, `agent.admin`. An intentional denial is HTTP 200 with
`allowed: false` and `permissions: []`. The endpoint reads the gateway's
operational store and evaluates the policy on each request; it does not cache
permission decisions. Account details can be up to 60 seconds old: a change
made through this gateway process is seen at once, but one made directly in the
database or by another process is seen when the cached account expires.

Unknown or inactive accounts receive HTTP 403 with error type
`subject_unavailable`, before the callback runs. A callback exception or invalid
result receives HTTP 503 with error type `agent_access_unavailable`, without
policy details. Both use the gateway's standard `{"error": {"type": ...,
"message": ...}}` envelope. A request without a valid dispatch token gets 401,
and a gateway with the internal API turned off answers 404.

## Quota reporting

The admin console's **Providers → Quotas** tab and RouteWise's quota-aware
routing both read usage from quota sources. The gateway ships with none: a
backend extension registers one per provider, reading the provider's usage API
or your own metering service. Nothing requires a particular supplier, website
login or cookie.

An extension's `register()` calls
`serving.admin.provider_quotas.register_quota_fetcher(provider, display_name, fetch)`;
`fetch` is awaited as `fetch(operational_store, services)` and returns one
`ProviderQuotaResult` per configured key, converting its own failures into
results rather than raising. The admin endpoint supplies the store and services;
RouteWise currently calls it with `(None, None)`, so the fetcher must also work
without those objects. Registration runs at startup, not when the module is
merely imported. A fetcher queries usage; it does not send user inference
requests or change the route's inference protocol.

Each result contains `ProviderQuotaUsage` rows with `label`, `used`, `limit`,
`unit` and an optional timezone-aware `reset_at`. The deployment's provider
identifier links the source to its routes; it is not a hard-coded vendor
enum. `quota_source.provider`, `usage_label` and `unit` must exactly match a
returned result and usage row. Report the account/window shared by that quota
pool, and keep independent accounts in distinct sources. Do not sum unrelated
keys or present a failed query as zero usage.

The UI can display percentages, currencies and other units, but RouteWise's
quota admission consumes one **request** at a time and requires a compatible
count-based source. A percentage alone is not a request allowance. The source
owns window boundaries and reset times; `quota.limit` is a cross-check against
its reported limit, not a replacement measurement. A source with no successful
snapshot stays unready. A failed refresh does not manufacture a fresh balance;
an existing pool retains its last successful snapshot and local increments.

### Run a local quota source

The complete [example extension](../../distributions/example/quota_extension.py)
reads `/usage` from the bundled fake provider. Its in-memory daily counter is
for demonstration only: it resets on process restart or at UTC midnight, and is
not production accounting. It uses no real account or credential and is loaded
only when explicitly selected through `BACKEND_EXTENSIONS`.

After the developer setup, run these commands from the repository root in
three terminals. First, start a simulated quota provider:

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18353 --response-text ROUTED_TO_QUOTA --quota-limit 100
```

Then start a slower, priced fallback (its prices in the example are fictional):

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18352 --response-text ROUTED_TO_FALLBACK --ttft-delay-ms 400
```

Finally start the gateway with the opt-in
[quota registry](../../config/examples/models.routewise.quota.yaml). Name the
demo token once so the admin call below can reuse it:

```bash
export DEMO_ADMIN_TOKEN=local-quota-demo-only

PYTHONPATH=.:apps/backend \
  PYTHON_DOTENV_DISABLED=1 \
  BACKEND_EXTENSIONS=distributions.example.quota_extension \
  EXAMPLE_QUOTA_BASE_URL=http://127.0.0.1:18353 \
  MODELS_CONFIG_PATH=config/examples/models.routewise.quota.yaml \
  ROUTING_CONFIG_PATH=config/examples/routing.minimal.yaml \
  DB_ENABLED=false USER_AUTH_ENABLED=false ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}" \
  JWT_SECRET_KEY=local-quota-demo-signing-secret-not-for-production \
  uv run uvicorn serving.servers.app:app --no-proxy-headers --host 127.0.0.1 --port 18080
```

These settings disable dotenv loading and user authentication and use a public
demo-only admin token and JWT signing secret: keep the listener on loopback and
never use them for a real deployment. The admin endpoint needs the signing
secret even when authenticating with the demo token. Read the same result the
console uses:

```bash
curl http://127.0.0.1:18080/admin/provider-quotas \
  -H "Authorization: Bearer ${DEMO_ADMIN_TOKEN}"
```

Send several requests to calibrate the cost envelope and allow the first
five-second probe/snapshot cycle to finish:

```bash
curl http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"quota-demo","messages":[{"role":"user","content":"hi"}]}'
```

The response identifies the selected fixture. Initial requests use the fallback
until the quota route has both a usage snapshot and cost-envelope evidence.
Accepted chat requests, including active probes, consume the mock quota;
health, model discovery and `/usage` reads do not. To simulate exhaustion,
restart only the quota fixture with `--quota-limit 100 --quota-used 100`; after
the next snapshot refresh, requests continue through the fallback. Stop each
local process with Ctrl+C when finished.

To see the card in the authenticated Web/Admin Console, configure this same
extension and registry on a local full-stack demo backend, then open
**Providers → Quotas**. Use a quota endpoint reachable from that backend:
inside Compose, `127.0.0.1` denotes the backend container, not the host. The
[Quickstart](router-tutorial.md) covers the full-stack demo and its
authentication. With no returned results the Quotas tab stays visible and
links back here; the Overview, Keys, Availability and Performance tabs work
without any quota source.

For your own integration, copy the extension and replace its local HTTP query
and mapping with your authorized data source. Preserve the result contract, bound network
requests with a timeout, handle failures explicitly and keep credentials out
of responses and logs. The provider's own terms still govern how you may query
its usage.
