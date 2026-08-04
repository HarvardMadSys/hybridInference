# Cloud Agent Split: MCP Ownership Amendment

Status: accepted design amendment, 2026-08-04.

This document supersedes the MCP-related parts of
`2026-08-03-cloud-agent-repo-split.md` and
`cloud-agent-split-manifest.md`. The canonical split plan is currently carried
on the newer split branches; this amendment is intentionally standalone so it
can be applied to that PR stack without rewriting its history.

## Decision

MCP belongs entirely to `freeinference-cloud-agent`.

HybridInference remains the model inference gateway. It owns user identity,
model visibility, inference authorization, user quota, cost accounting, and
the short-lived `agr` inference grant. It does not own the Cloud Agent MCP
registry, MCP credentials, MCP proxy, tool filtering, or MCP authorization.

The reason is ownership of the underlying state:

| Concern | Owner | Why |
|---|---|---|
| Model catalog and inference access | HybridInference | These are gateway/provider policies. |
| User inference quota and usage | HybridInference | All model spend is already accounted there. |
| Job, attempt, lease, and fence | Cloud Agent | These are agent execution state. |
| MCP registry and credentials | Cloud Agent | MCP servers are agent capabilities and their secrets are consumed only by the agent service. |
| Per-job MCP selection and tool filtering | Cloud Agent | The job and its requested tools live there, so no cross-service copy of that policy is needed. |

This is an ownership boundary, not a new entitlement system. The current MCP
registry has no per-role policy, and this change must not invent one. Unknown
MCP server names still fail job creation instead of being silently dropped.

## Resulting request paths

```text
sandbox -- agr --> HybridInference model endpoint --> model provider
        -- ajt --> Cloud Agent MCP endpoint --------> MCP upstream
```

The two credentials are deliberately not interchangeable:

- `agr` is minted and verified by HybridInference. It is scoped to one user,
  job, attempt, an allowed model set, a short TTL, and revocation. It is never
  accepted by Cloud Agent's MCP endpoint.
- `ajt` is minted and verified by Cloud Agent from
  `AGENT_CONTROL_TOKEN_SECRET`. Its sandbox scope becomes `mcp`, bound to
  `(job_id, attempt_id, lease_generation)`. It is never accepted by the model
  gateway. The MCP endpoint also checks the live attempt fence and the job's
  requested MCP server set on every call.

The runner injects both credentials into the sandbox. A leaked MCP token
cannot buy model calls; a leaked inference grant cannot call MCP or reveal MCP
credentials.

## Cloud Agent MCP surface

Cloud Agent owns a dedicated endpoint such as
`POST /v1/agent/mcp/{server_name}`. It must:

1. authenticate an `ajt` token with `scope=mcp`;
2. load the bound job and current attempt;
3. reject a stale lease generation, superseded/cancelled/finished attempt, or
   server not requested by that job;
4. apply the registry's existing server/tool allowlist;
5. add upstream credentials only while proxying the request; and
6. redact credentials and upstream-sensitive headers from logs and errors.

The public job/config API may return MCP names and safe display metadata. It
must not return upstream headers, tokens, secret-bearing URLs, or other
credential material.

Registry configuration and credentials move with `mcp_registry.py` to Cloud
Agent. Prefer file-mounted secrets or secret-manager references over inline
environment values. They do not enter HybridInference's database and never
enter a runner or sandbox environment.

## Network boundary

After the split, one base URL cannot represent both capabilities. The sandbox
receives two narrow destinations:

- `AGENT_GATEWAY_URL` for model inference only;
- `AGENT_MCP_URL` for the Cloud Agent MCP proxy only.

For a remote runner, deployment may expose these through two authenticated
host relays or two pinned service aliases on the closed agent-egress network.
The sandbox must not receive general access to the Cloud Agent control API or
direct network access to MCP upstreams. DNS resolution is pinned at connection
time, redirects are disabled, and the egress allowlist names only these two
destinations.

## Changes to the split plan

### Phase C: HybridInference contracts

- C5 `agent_grants` drops `allowed_mcp` from its request, response, token
  claims, DDL, storage API, and tests. A grant contains `allowed_models` only.
- C6 removes `authenticate_agent_tool_call` and every MCP-scope check from the
  `agr` path. The existing user quota and cost-accounting work remains required
  for model calls.
- C8 removes MCP fields and MCP endpoints from the cross-repo inference-grant
  contract.
- C9 removes `GET /internal/mcp-registry`. The gateway lookup contract contains
  the model catalog and user-status reads only.

### Phase D/E: Cloud Agent implementation

- Move `agent_mcp.py`, `mcp_proxy.py`, `mcp_registry.py`, and their tests into
  `freeinference-cloud-agent` instead of leaving them in HybridInference.
- E3 replaces the sandbox `SCOPE_MODEL` control token with `SCOPE_MCP` when E9
  introduces inference grants. Runner `SCOPE_FULL` behavior stays unchanged.
- E4/E6 validate requested MCP servers against the local Cloud Agent registry;
  they do not call HybridInference for MCP metadata.
- E9 injects both `agr` plus `AGENT_GATEWAY_URL` for inference and the scoped
  `ajt` plus `AGENT_MCP_URL` for MCP. Grant renewal applies only to `agr`; the
  MCP credential is fenced by Cloud Agent's attempt/lease state.
- D7/E11 deployment and smoke tests allow the two narrow sandbox destinations
  and prove that direct MCP-upstream access remains blocked.

### Phase H: old-repo removal

H4 deletes the gateway copies of `agent_mcp.py`, `mcp_proxy.py`, and
`mcp_registry.py` after Cloud Agent MCP cutover. They are no longer exceptions
that survive removal. HybridInference keeps only inference grant verification
and inference accounting.

The manifest rows for those three modules and their tests change from
`stay:contract` to `move:control-plane`. References claiming that MCP is a
second gateway capability-token consumer are obsolete.

## Required acceptance tests

- The inference grant OpenAPI schema, token claims, and `agent_grants` table
  contain no `allowed_mcp` or other MCP field.
- HybridInference exposes neither `/v1/agent/mcp/{server_name}` nor
  `/internal/mcp-registry` after cutover.
- Job creation rejects an unknown MCP server without silently narrowing the
  requested set.
- Cloud Agent rejects an MCP call for an unrequested server or tool.
- Cloud Agent rejects MCP calls from a stale lease generation and from a
  superseded, cancelled, completed, or expired attempt.
- MCP credentials never appear in the browser config response, sandbox
  environment, runner environment, logs, events, or error bodies.
- The sandbox cannot connect directly to an MCP upstream.
- An end-to-end job can call both a model and MCP: the model request is
  authorized and metered by HybridInference, while the MCP request is
  authorized and proxied by Cloud Agent.

## Non-goals

- No per-task monetary budget.
- No MCP cost accounting in HybridInference.
- No per-role MCP entitlement until a separate product policy defines one.
- No sharing of signing secrets between the two repositories.
