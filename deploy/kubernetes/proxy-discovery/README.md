# Discovering a changing proxy Pod address

This optional mode is for a gateway reached through a controlled Kubernetes
proxy workload. Instead of a hand-maintained list of proxy IPs, the gateway
discovers the proxy Pods with short-lived, read-only API reads. It is off by
default, and installing it changes or deploys no cluster resources.

## How discovery decides

Each refresh reads one Service, the Pods matching its selector, and the Service
again; the Service's namespace, name, immutable UID and resource version must
agree. A Pod is admitted only if it is Running, not being deleted, directly
owned by the configured controller UID, matching the selector and on Pod
networking. Only the admitted Pods' exact IPv4 and IPv6 addresses become
trusted proxies.

`KUBERNETES_PROXY_WORKLOAD_UID` must be the UID of the Pods' **direct**
controller, the single `ownerReferences` entry with `controller: true`; owner
chains are not followed. Pinning a Deployment's UID is therefore
**unsupported** (its Pods are owned by a ReplicaSet, so none matches). Pinning
the current ReplicaSet works only until a template change creates a new one:
its Pods are never admitted, and once the old Pods are gone membership is
empty, every identity is `unknown` and readiness fails. Each such rollout needs
a reviewed UID change and a gateway restart, coordinated with the proxy
rollout. A controller that owns its Pods directly and keeps its UID across
rollouts, such as a StatefulSet, avoids this.

Pod readiness is not required, so that a new Running proxy is discoverable
before its readiness check lets it serve. An empty set, an incomplete or
malformed API result, a denied request, a timeout or an expired snapshot grants
no trust. Discovery never writes `TRUSTED_PROXIES`, other application settings
or Kubernetes objects.

A Pod is excluded as soon as a refresh sees its `deletionTimestamp`, even while
it is still draining. Once the gateway has seen the deletion (within one
snapshot age), new requests the draining proxy forwards, including on reused
keep-alive connections, are `unknown` with `resolved=false`; requests admitted
earlier, long streams included, keep the identity fixed at admission. If those
new requests must keep a resolved identity, stop routing traffic through a
proxy before deleting it.

## Prerequisites

- The gateway's actual TCP peer must be the proxy Pod, without SNAT. Keep
  uvicorn's `--no-proxy-headers` launch option.
- The proxy must accept client-provenance headers only from its controlled
  ingress, which must overwrite them or append the source it observed. The
  frontend's existing header copying does not authenticate arbitrary caller
  headers. Verify ordinary rewrites and streaming routes, including a
  direct-to-frontend bypass attempt.
- Only the deployment authority may create or modify the Service, its
  selectors, the workload, Pod labels and owner references. Metadata is not
  cryptographic workload authentication.
- Network isolation must stop other workloads from reaching the gateway as
  proxies or bypassing the controlled frontend ingress. Confirm the cluster's
  network plugin actually enforces the policy; a stored NetworkPolicy alone
  does not prove it.
- The namespace-scoped service account needs only `get` on the named Service
  and `list` on Pods: no Secret access, writes, cluster-wide list, exec, logs
  or database credentials. Adapt `rbac.example.yaml`; its names are not
  deployment defaults.

## Configuration

These settings come only from the environment: Admin cannot change them and
they cannot be imported into `app_config`. Restart the gateway to change them.
Use the current immutable UIDs from the control plane, never resource names or
yesterday's Pod IP.

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

The projected token is re-read for every API request. TLS verification is
mandatory; redirects and environment HTTP proxies are disabled. Reads and
responses are bounded, and a refresh must finish within its freshness budget.
`KUBERNETES_PROXY_MAX_AGE_SECONDS` defaults to 1 and must be greater than 0 and
at most 5.

Separately review the **effective database-backed** application settings:

```text
TRUST_PROXY_HEADERS=true
AUTH_FAILURE_BLOCK_ENABLED=false
TRUST_CLOUDFLARE_HEADERS=false
TRUST_X_REAL_IP=false
```

This mode neither turns on auth-failure blocking nor changes these settings.
`TRUST_PROXY_HEADERS` stays the master kill switch: while it is effectively
off, discovery makes no API reads, every identity is `unknown` with
`resolved=false`, and readiness depends only on the store checks, so the switch
does not drain the gateways. Turning it back on requires fresh membership from
the next request. In this mode the static proxy list, direct-client exemptions
and Cloudflare trust are not fallbacks: a missing snapshot or an unrecognized
peer is `unknown` with `resolved=false`, even if its address looks public.
Discovered peers use the existing right-to-left XFF rule; X-Real-IP still needs
its own opt-in and is not needed here.

### API load and limits

A refresh is three GETs: the Service, the Pod list and the Service again. Each
gateway worker keeps its own cache and refreshes lazily, when a request finds
its snapshot expired; concurrent requests share one refresh, and readiness
probes use the same cache. Under continuous traffic the healthy API load is
about

```text
3 × (total gateway workers across all replicas) / KUBERNETES_PROXY_MAX_AGE_SECONDS
```

GETs per second, and less when idle. After a failure a worker grants no trust
and waits 0.25 seconds before trying again; an attempt stops at its first
failing read (one to three GETs), so a failing API sees at most four attempts
per second per busy worker.

The Pod list is requested with `limit=501`, and more than 500 Pods matching the
selector fails the refresh. That count includes **all** selector-matching Pods,
before Pods that are not Running, are being deleted, use host networking or
have another controller are excluded. Each API response body is capped at
2,000,000 bytes. Either limit fails closed like any refresh failure (no trust,
`unknown` identities, failed readiness), never with a partial list. Keep the
selector narrow, clean up completed Pods it matches, and measure your Pod list
size against the cap.

### Diagnostics

When a worker's discovery starts failing it logs one WARNING,
`kubernetes_proxy_discovery_unavailable`, with a fixed `reason`; nothing more
while it keeps failing, then an INFO `kubernetes_proxy_discovery_recovered` on
the next success. Reasons include:

- `api_status_<HTTP status>`, for example `api_status_403` for missing RBAC;
- `refresh_timeout` (the whole refresh exceeded its budget) and `api_timeout`
  (one API read timed out);
- `api_transport_error` (a connection or TLS failure);
- `incomplete_pod_list` (pagination, or more than 500 Pods);
- `api_response_too_large`, `service_identity_mismatch`,
  `token_file_unavailable` and `ca_file_unavailable`;
- `discovery_unavailable` for anything unclassified.

The warning carries only the reason code, never exception text, tokens, URLs or
API responses. Other logs, such as the HTTP client library's request logging,
are outside that guarantee and may include request URLs.

Each `http_request` log line carries `proxy_discovery_ms`, the time the request
spent in discovery, including waiting for another request's refresh: near zero
on a cache hit, about the refresh time when it refreshed or waited, and the
time to fail on a failed refresh. `duration_ms` does not include it. It is null
when discovery did not run (discovery disabled, `TRUST_PROXY_HEADERS` off, or
`/health` liveness). Probe paths log their `http_request` line at DEBUG.

## Readiness, replacement and failure

Use `/health/ready` for gateway readiness. With discovery enabled and
`TRUST_PROXY_HEADERS` effectively on, it requires a fresh, nonempty proxy set
as well as the store checks; with the switch off, only the store checks.
`/health` stays the liveness check and never waits for discovery, and a
Kubernetes API outage must not cause a gateway restart loop. Each worker
discovers independently, so readiness must reach every serving gateway
instance, not one lucky response through a load balancer.

Test frontend replacement in an isolated deployment: the new socket peer must
resolve to each test client's actual egress, and the old address must stop
being accepted after the snapshot deadline. Requests admitted before membership
refreshes can be unresolved: this mode has **no** zero-window
frontend traffic gate; a deployment that needs one must hold each new proxy's
traffic until every relevant gateway has verified it. Unresolved requests keep
the application's existing unresolved-identity behavior; do not enable IP
blocking to compensate.

Membership can be stale for up to the snapshot age, and watching UIDs or exact
addresses does not prevent IP reuse within that window. Keep unrelated
workloads out with the network and RBAC boundary above; where you cannot
guarantee it, use authenticated proxy connections instead of this mode. Do not
present it as per-connection authentication. Failures never extend a cached
snapshot's lifetime.

## Activation and rollback acceptance

1. Review the release, UIDs, service-account permission and network policies.
   Prove that Pod-to-gateway traffic keeps its source address and that the
   ingress handles provenance headers correctly.
2. Validate in development, then staging: replacement, scale-down, a same-name
   Pod with a different owner, old-IP reuse, a missing token, API 403s and
   timeouts, selector changes, partial lists, token rotation, IPv6 and every
   gateway worker.
3. From two different egresses, test streaming and ordinary proxy routes: check
   `peer_ip`, `ip_source=x-forwarded-for`, the resolved client IP and normal
   completion. Submit forged leftmost XFF, CF and X-Real-IP values; they must
   not determine client identity.
4. Verify that auth-failure blocking stays disabled throughout. A green unit
   suite or a successful database readback is not production acceptance.
5. Production activation is a separately approved deployment. On discovery
   trouble, keeping the mode enabled preserves the unknown-source safeguard;
   disabling it restores legacy parsing and may revive a stale static list, so a
   rollback needs a newly reviewed proxy-trust plan and identity acceptance,
   not an automatic restore of old Pod IPs.
