# Backend Extensions

A backend extension is a trusted Python module that a deployment loads into the
gateway at startup. It can add an adapter kind, decide who may use the Cloud
Agent, or report provider quotas. For changes that need no code, see
[Distribution customization](distribution-customization.md).

## Loading an extension

List the module names, comma-separated, in `BACKEND_EXTENSIONS` (empty by
default). Each module defines a synchronous `register()` that takes no
arguments. At startup, after loading `.env` and before building any routes, the
gateway imports each module once and calls `register()`. If either fails, the
gateway refuses to start; it never falls back to another adapter.

An extension is trusted server code, not configuration. Make it importable
through the deployment, for example a read-only mount of its overlay, and never
derive module names from a request or let an admin form choose them. Import all
of its code at startup, and restart the backend after changing it.

To add an adapter kind, call
`serving.servers.registry.register_adapter_factory(kind, factory, *, override=False)`
from `register()`. The factory receives the route's configuration dictionary,
before built-in provider defaults are applied, and returns an adapter. A kind
can be registered once, and becomes a reserved provider label; replacing a
built-in kind needs `override=True`. `register()` may also add entries to the
shared runtime-setting and provider metadata dictionaries, in place.
[Deployment-local adapters](provider-adapters.md#deployment-local-adapters) has
a minimal factory example.

## Cloud Agent access

The deployment decides who may use or administer its Cloud Agent. An extension
registers one synchronous callback with
`serving.agent_access.register_agent_access_policy(policy)`. The gateway first
checks that the user exists and is `active`, then calls the callback with a
read-only mapping holding only `user_id` and `role`. The callback returns a
`list[str]` or `tuple[str, ...]` of permissions: `agent.use` to use the Agent,
and `agent.admin`, valid only with `agent.use`, to administer it. Anything else
makes the result invalid. Without a registered policy, every role is denied.

The standalone Agent reads the decision from
`GET /internal/users/{user_id}/agent-access` with the
`GATEWAY_GRANT_DISPATCH_TOKEN` bearer token. The response holds only `user_id`,
`allowed` (true when `agent.use` is granted) and `permissions`, in the order
`agent.use`, `agent.admin`; a denial is HTTP 200 with `allowed: false`. The
policy runs on every request, but the account it sees may be up to 60 seconds
old when it was changed outside this gateway process.

| Response | When |
|---|---|
| 403 `subject_unavailable` | The account is unknown or inactive; the callback does not run |
| 503 `agent_access_unavailable` | The callback raised or returned an invalid result |
| 401 | The dispatch token is missing or wrong |
| 404 | The gateway has no dispatch token configured, so it offers no internal endpoints |

## Quota reporting

The **Quotas** section of **Admin → Providers** and RouteWise's quota-aware
routing read usage from quota sources. The gateway ships none: an extension registers one per
provider, from the provider's usage API or your own metering. Call
`serving.admin.provider_quotas.register_quota_fetcher(provider, display_name, fetch)`
from `register()`; a second registration for a provider needs `override=True`.

The gateway awaits `fetch(operational_store, services)`; RouteWise passes
`(None, None)`, so the fetcher must work without them. It returns one
`ProviderQuotaResult` per configured key, turning its own failures into
results instead of raising, and only queries usage. Each result holds
`ProviderQuotaUsage` rows with `label`, `used`, `limit`, `unit` and an optional
timezone-aware `reset_at`.

A route's `quota_source.provider`, `usage_label` and `unit` must match a
returned result and row exactly; the provider name is the deployment's own.
Report the account and window the quota pool shares, keep independent accounts
in separate sources, and never report a failed query as zero usage. RouteWise
spends one request at a time, so it needs a count-based source: a percentage
is not a request allowance. A route's `quota.limit` only cross-checks the
source's limit. A source with no successful snapshot stays unready, and a
failed refresh keeps the last snapshot plus the requests counted since.

### Run a local quota source

The [example extension](../../distributions/example/quota_extension.py) reads
`/usage` from the bundled fake provider. Its daily counter lives in memory and
resets on restart or at UTC midnight; it is a demonstration, not accounting.
From the repository root, after the developer setup, start a simulated quota
provider:

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18353 --response-text ROUTED_TO_QUOTA --quota-limit 100
```

Then a slower, priced fallback (its prices are fictional):

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18352 --response-text ROUTED_TO_FALLBACK --ttft-delay-ms 400
```

Then the gateway, with the opt-in
[quota registry](../../config/examples/models.routewise.quota.json):

```bash
export DEMO_ADMIN_TOKEN=local-quota-demo-only

PYTHONPATH=.:apps/backend \
  PYTHON_DOTENV_DISABLED=1 \
  BACKEND_EXTENSIONS=distributions.example.quota_extension \
  EXAMPLE_QUOTA_BASE_URL=http://127.0.0.1:18353 \
  MODEL_CATALOG="$(cat config/examples/models.routewise.quota.json)" \
  ROUTING_POLICY="$(cat config/examples/routing.minimal.json)" \
  DB_ENABLED=false USER_AUTH_ENABLED=false ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}" \
  JWT_SECRET_KEY=local-quota-demo-signing-secret-not-for-production \
  uv run uvicorn serving.servers.app:app --no-proxy-headers --host 127.0.0.1 --port 18080
```

These settings turn off `.env` loading and user authentication, and use a
public admin token and JWT signing secret. Keep the listener on loopback and
never use them in a real deployment. Read the result the console shows:

```bash
curl http://127.0.0.1:18080/admin/provider-quotas \
  -H "Authorization: Bearer ${DEMO_ADMIN_TOKEN}"
```

Send a few requests, and let the first five-second probe and snapshot cycle
finish:

```bash
curl http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"quota-demo","messages":[{"role":"user","content":"hi"}]}'
```

The response names the fixture that answered. Requests go to the fallback until
the quota route has a usage snapshot and evidence for its cost envelope. Chat
requests, probes included, use up the mock quota. To simulate exhaustion,
restart the quota fixture with `--quota-limit 100 --quota-used 100`; after the
next snapshot, requests go to the fallback.

To see the card in the console, configure the same extension and registry on a
local full-stack demo backend (see the [Quickstart](router-tutorial.md)), open
**Providers** and scroll to **Quotas**, below the keys. Inside Compose,
`127.0.0.1` is the backend container, not the host. Without results, the Quotas
section links back here; the rest of the Providers tab needs no quota source.

For your own integration, copy the extension and replace its query with a data
source you are authorized to use. Keep the result contract, give every request
a timeout, and keep credentials out of responses and logs.
