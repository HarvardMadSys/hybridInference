# Discovering a changing proxy Pod address

This optional mode is for a gateway reached through a controlled Kubernetes
proxy workload. It replaces manually maintained proxy IPs with short-lived,
read-only discovery. It is disabled by default and does not change or deploy
any cluster resources when installed.

The gateway reads one Service and the Pods matching its selector, then checks
the Service again. The Service's namespace, name, immutable UID and resource
version must agree. Each admitted Pod must be Running, not deleting, directly
owned by the configured controller UID, match the selector, and use Pod
networking. Only its exact IPv4/IPv6 addresses become trusted proxies. A
Deployment whose Pods are owned by ReplicaSets is **not** accepted by pinning
the Deployment UID; this version supports direct controller ownership only.

Pod readiness is deliberately not a discovery requirement. A newly Running
proxy must be discoverable before a readiness check allows it to serve traffic.
An empty set, incomplete or malformed API result, denied request, timeout or
expired snapshot grants no trust. Discovery never writes `TRUSTED_PROXIES`,
other application settings, or Kubernetes objects.

## Prerequisites

- Confirm that the gateway's actual TCP peer is the proxy Pod, without SNAT.
  Keep uvicorn's `--no-proxy-headers` launch option.
- The proxy must only accept client-provenance headers from its controlled
  ingress. The ingress must overwrite them or append the source it observed.
  Existing frontend header copying does not authenticate arbitrary caller
  headers. Verify both ordinary rewrites and streaming routes, including a
  direct-to-frontend bypass attempt.
- Restrict creation and modification of the Service, its selectors, workload,
  Pod labels and owner references to the deployment authority. Metadata is
  not cryptographic workload authentication.
- Enforce network isolation so other workloads cannot reach the gateway as
  proxies or bypass the controlled frontend ingress. Confirm the cluster's
  network plugin actually enforces the policy; a stored NetworkPolicy alone
  does not establish that it does.
- A namespace-scoped service account needs only `get` on the named Service
  and `list` on Pods. It needs no Secret access, writes, cluster-wide list,
  exec, logs or database credentials. Adapt the accompanying example RBAC;
  the example names are not deployment defaults.

## Configuration

The following are **environment-only deployment wiring**. They cannot be
changed through Admin Configuration or imported into `app_config`. Restart
the gateway to change them. Use the current immutable UIDs from the control
plane; do not substitute resource names or yesterday's Pod IP.

```text
TRUST_KUBERNETES_PROXY_DISCOVERY=true
KUBERNETES_PROXY_NAMESPACE=example-inference
KUBERNETES_PROXY_SERVICE_NAME=frontend
KUBERNETES_PROXY_SERVICE_UID=<verified-service-uid>
KUBERNETES_PROXY_WORKLOAD_UID=<verified-direct-controller-uid>
KUBERNETES_PROXY_API_URL=https://kubernetes.default.svc
KUBERNETES_PROXY_CA_FILE=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
KUBERNETES_PROXY_TOKEN_FILE=/var/run/secrets/kubernetes.io/serviceaccount/token
KUBERNETES_PROXY_MAX_AGE_SECONDS=1
```

The projected token is read again for every API request. TLS verification is
mandatory; redirects and environment HTTP proxies are disabled. API reads and
responses are bounded, and a multi-read snapshot must finish within its
freshness budget. The default maximum age is one second; the supported range
is greater than zero and no more than five seconds. A short negative cache
limits retry storms after errors.

Separately review the **effective database-backed** application settings:

```text
TRUST_PROXY_HEADERS=true
AUTH_FAILURE_BLOCK_ENABLED=false
TRUST_CLOUDFLARE_HEADERS=false
TRUST_X_REAL_IP=false
```

This mode does not turn on auth-failure blocking or mutate these settings.
`TRUST_PROXY_HEADERS` remains the master kill switch. In dynamic mode, the
static proxy list, direct-client exemptions and Cloudflare trust cannot act as
fallbacks. A missing snapshot or an unrecognized peer is `unknown` with
`resolved=false`, even if the peer's address looks public. Valid discovered
peers use the existing right-to-left XFF parsing rule; X-Real-IP still requires
its separate opt-in and is not needed here.

## Readiness, replacement and failure

Use `/health/ready` as gateway readiness. With discovery enabled it requires a
fresh, nonempty proxy set as well as the existing store checks. `/health`
remains liveness and never waits for discovery. A Kubernetes API outage must
not cause a gateway restart loop. Each worker discovers independently;
readiness must reach every serving gateway instance, not just one lucky
response through a load balancer.

Replace the frontend in an isolated test deployment and verify the new socket
peer resolves to each test client's actual egress, then verify the old address
is no longer accepted after the snapshot deadline. Each request's identity is
fixed at admission, so a long stream is not reclassified midway through it.
Requests admitted before membership has refreshed can be unresolved; this
feature does **not** provide a zero-window frontend traffic gate. Deployments
requiring one must gate each new proxy's traffic until all relevant gateways
have verified it. Requests with unresolved provenance retain the application's
existing unresolved-identity behavior; do not enable IP blocking to compensate.

There is a bounded stale-membership window of up to the configured snapshot
age. Watching UIDs or using exact addresses does not eliminate IP reuse during
that window. To exclude unrelated workloads, enforce the network/RBAC boundary
above; where that cannot be guaranteed, use authenticated proxy connections
instead of enabling this mode. Do not advertise this as per-connection
authentication. Failures never extend a cached snapshot's lifetime.

## Activation and rollback acceptance

1. Review the release, UIDs, service-account permission and network policies.
   Prove Pod-to-gateway source preservation and ingress header handling.
2. Validate in development, then staging: replacement, scale-down, same-name
   different-owner Pod, old-IP reuse, missing token, API 403/timeouts, selector
   changes, partial lists, token rotation, IPv6 and all gateway workers.
3. From two different egresses, test both streaming and ordinary proxy routes.
   Check `peer_ip`, `ip_source=x-forwarded-for`, resolved client IP, and normal
   response completion. Submit forged leftmost XFF, CF and X-Real-IP values;
   they must not determine client identity.
4. Verify auth-failure blocking stays disabled throughout. A green unit suite
   or successful DB readback is not production acceptance.
5. Production activation is a separately approved deployment. On discovery
   trouble, keeping this mode enabled preserves the unknown-source safeguard.
   Disabling it restores legacy parsing and may resurrect a stale static list;
   a rollback therefore needs a newly reviewed proxy-trust plan and identity
   acceptance, not an automatic restore of old Pod IPs.
