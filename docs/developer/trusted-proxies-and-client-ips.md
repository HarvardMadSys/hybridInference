# Trusted Proxies and Client IPs

When something sits in front of the gateway (a CDN, a reverse proxy, a tunnel,
a load balancer), the socket peer is the proxy, and the caller's address arrives
only in a header that anyone can forge. The gateway therefore believes a
forwarded address only from peers you authorize. Find your setup:

| In front of the gateway | Set |
|---|---|
| Nothing: clients connect directly | Nothing. Forwarding headers are ignored by default |
| A reverse proxy or load balancer at a known address | `TRUST_PROXY_HEADERS=1` and `TRUSTED_PROXIES=<that proxy's address>/32` |
| Cloudflare, connecting straight to the gateway | `TRUST_PROXY_HEADERS=1`, `TRUST_CLOUDFLARE_HEADERS=1` and `TRUSTED_CLOUDFLARE_NETWORKS=<Cloudflare's published ranges>` |
| Cloudflare, then your own proxy, then the gateway | As above, but `TRUSTED_CLOUDFLARE_NETWORKS=<your proxy's address>/32`, and only if that proxy accepts connections from Cloudflare alone and handles the header correctly |
| Clients on a private network (such as `10.x`) connecting directly | `TRUSTED_DIRECT_CLIENT_NETWORKS=<those client ranges>` |
| The bundled Compose file's console, and your edge in front of it | Trust the console only for the header your edge sets; see [Behind the console](#behind-the-console) |

These are settings on the **Network** tab of **Admin → Settings**. A new
deployment's first start copies them from the environment (the `NAME=value`
forms on this page); after that the stored values win, so change them in Admin
or with `python -m serving.config.manage set`, not in `.env`. The bundled
Compose file passes `TRUST_PROXY_HEADERS` and `TRUST_CLOUDFLARE_HEADERS` as `0`
unless `.env` sets them, and the first start stores that. If every client
appears with your proxy's address, check these settings in Admin first.

`apps/backend/serving/utils/request_ip.py` is the one place in the code that
decides a client's address.

## The server underneath

uvicorn has its own proxy-header handling, on by default: it rewrites the
socket peer and URL scheme from `X-Forwarded-For` and `X-Forwarded-Proto` for
peers in `--forwarded-allow-ips` (default `127.0.0.1`, or `FORWARDED_ALLOW_IPS`),
before the application sees the request. That would bypass every rule on this
page. Every launch configuration in this repository passes `--no-proxy-headers`
(`deploy/docker/Dockerfile.backend` and the systemd unit in `deploy/systemd/`),
and a test fails if one stops. Under your own process manager, pass
`--no-proxy-headers` too; leaving out `--proxy-headers` is not enough.

Because the server never reads forwarded headers, `peer_ip` is always the real
TCP peer, and `request.url.scheme` is `http` behind a TLS-terminating proxy. Set
`BASE_URL` (on the **General** tab of **Admin → Settings**) so the links in
signup and password-reset emails use the right scheme.

## Behind the console

In the bundled Compose file every request through the console reaches the
gateway from the console's address. The console relays the caller's
`X-Forwarded-For`, `X-Real-IP`, `CF-Connecting-IP` and `CF-Connecting-IPv6`
unchanged, and adds no address of its own: a Next.js route handler cannot see
the connection's address once the caller has sent `X-Forwarded-For`. (When the
caller sent none, Next fills it in on the gateway's API paths, but not on the
rewritten `/health`, `/site-updates`, `/site-config` and `/public-stats`.)

So trusting the console trusts whatever reached it. That is safe only when every
request reaches the console through one edge that sets the header the gateway
reads:

| In front of the console | What to trust | Why it holds |
|---|---|---|
| Cloudflare, as a proxy or a tunnel | `CF-Connecting-IP`: `TRUST_PROXY_HEADERS=1`, `TRUST_CLOUDFLARE_HEADERS=1`, `TRUSTED_CLOUDFLARE_NETWORKS=<the console's address>/32` | Cloudflare overwrites that header on every request |
| A reverse proxy that writes `X-Forwarded-For` (nginx's `$proxy_add_x_forwarded_for` or `$remote_addr`) | `X-Forwarded-For`: `TRUST_PROXY_HEADERS=1`, `TRUSTED_PROXIES=<the console's address>/32` | The right-most entry, the one the gateway reads, is the one that proxy wrote |
| Nothing: clients reach the console directly | Nothing | A client chooses every header the console relays |

Keep the console's port reachable from that edge alone. Loopback publishing
(`FRONTEND_HOST=127.0.0.1`) still lets every process on the host and every
container on its network reach it and claim any address. Without such an edge,
leave the console untrusted: every browser then resolves to `"unknown"` (see
[The "unknown" outcome](#the-unknown-outcome)). An entry that sends `/v1` and
`/anthropic` straight to the gateway is a second peer; trust it by its exact
address, never a range (see
[Splitting inference traffic at the entry](deployment.md#splitting-inference-traffic-at-the-entry)).

## Trust configuration

Forwarding headers are attacker-controlled on every request that does not pass
through a trusted proxy, so each source of them needs explicit authorization.

| Setting | Effect |
|---|---|
| `TRUST_PROXY_HEADERS` | Master switch (default `0`). With it on, `X-Forwarded-For` is read from peers in `TRUSTED_PROXIES` |
| `TRUSTED_PROXIES` | Comma-separated CIDRs of the proxies that forward to the gateway |
| `TRUST_CLOUDFLARE_HEADERS` | Reads `CF-Connecting-IP` from peers in `TRUSTED_CLOUDFLARE_NETWORKS`; needs `TRUST_PROXY_HEADERS` |
| `TRUSTED_CLOUDFLARE_NETWORKS` | The immediate peers allowed to send Cloudflare's header |
| `TRUST_X_REAL_IP` | Also reads `X-Real-IP` from trusted proxies, only when no `X-Forwarded-For` is present; needs `TRUST_PROXY_HEADERS` |
| `TRUSTED_DIRECT_CLIENT_NETWORKS` | Private client ranges whose socket peers count as clients; authorizes no headers |

A flag with an empty network list does nothing, so the default fails closed.
Admin refuses a combination that breaks these rules, and an invalid CIDR. At
startup, an environment value that conflicts with a stored one is set aside with
a warning; a combination the environment breaks on its own, or an invalid CIDR
with no valid stored value, stops the gateway.

Trust the narrowest addresses: only the proxies that accept connections from
the internet. A broad internal subnet would let any host on it claim any client
address.

```bash
# Example: a single nginx reverse proxy at a known internal address
TRUSTED_PROXIES=172.19.0.2/32
```

`TRUSTED_CLOUDFLARE_NETWORKS` lists socket peers, not Cloudflare's ranges as
such. With Cloudflare → nginx → gateway, list only nginx's exact address, and
only when nginx is reachable from Cloudflare alone and overwrites the header. A
generic trusted proxy never makes a client-supplied `CF-Connecting-IP` safe,
which is why the two lists are separate.

```bash
# Example: Cloudflare → application directly
# These are Cloudflare's origin-facing IP ranges (the socket peer),
# NOT the visitor addresses in CF-Connecting-IP.
# See: https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/
TRUSTED_CLOUDFLARE_NETWORKS=173.245.48.0/20  # example Cloudflare origin range
```

Private (RFC 1918, CGNAT, ULA) socket peers stay unresolved by default, because
a container bridge or shared proxy is not a client. If clients really connect
directly over such a network, list only their ranges, never a shared proxy
network:

```bash
TRUSTED_DIRECT_CLIENT_NETWORKS=10.42.0.0/16,100.64.0.0/10,fd00:42::/64
```

### Kubernetes proxy discovery (optional)

For a Kubernetes proxy whose Pod addresses change, set
`TRUST_KUBERNETES_PROXY_DISCOVERY=true`. This switch and the
`KUBERNETES_PROXY_*` settings are environment-only, need a restart, and cannot
be changed in Admin; `TRUST_PROXY_HEADERS` stays the master switch.

Discovery pins a Service by UID, admits only the exact addresses of Pods owned
by the pinned controller, and replaces the static proxy list. Anything else
(stale or failed discovery, an unknown peer, the master switch off) resolves to
`unknown`, with no fallback; Cloudflare trust is disabled in this mode. Each
worker refreshes its own snapshot, valid one second by default
(`KUBERNETES_PROXY_MAX_AGE_SECONDS`, at most five), with three API GETs per
refresh. More than 500 matching Pods or a response over 2,000,000 bytes fails
closed. With `TRUST_PROXY_HEADERS` on, `/health/ready` fails until a fresh,
nonempty set exists. This mode cannot serve an entry split.

Before enabling it, check that the gateway sees the proxy Pods' real addresses,
protect Service selectors and Pod ownership with RBAC, and isolate the network.
Keep auth-failure blocking off during acceptance. The deployment guide
{download}`README.md <../../deploy/kubernetes/proxy-discovery/README.md>` and
the RBAC example
{download}`rbac.example.yaml <../../deploy/kubernetes/proxy-discovery/rbac.example.yaml>`
cover identity, TLS, tokens, limits and rollout.

## Resolution order

`get_client_ip_info()` returns a `ClientIpInfo` with the resolved `client_ip`,
the `peer_ip`, a `source` naming the rule that won, and `resolved`. In static
mode:

1. Peer not authorized: forwarding headers are ignored. The socket peer is the
   client if it is routable; otherwise the result is `"unknown"`.
2. `CF-Connecting-IP`, when the peer is in `TRUSTED_CLOUDFLARE_NETWORKS` and
   `TRUST_CLOUDFLARE_HEADERS=1` (see
   [Cloudflare Pseudo IPv4](#cloudflare-pseudo-ipv4) for `CF-Connecting-IPv6`).
3. `X-Forwarded-For`, when the peer is in `TRUSTED_PROXIES` and
   `TRUST_PROXY_HEADERS=1`. The chain is read right to left, skipping trusted
   proxies. The first untrusted hop ends the walk: it is the client if
   routable, and `"unknown"` otherwise. The walk never continues further left,
   where the values are the caller's.
4. `X-Real-IP`, when `TRUST_X_REAL_IP=1` and no `X-Forwarded-For` is present at
   all. A malformed or overlong `X-Forwarded-For` never falls through to it.
5. The socket peer, if routable; otherwise `"unknown"`.

For example, a caller who prepends fake addresses gains nothing:

```
XFF: "8.8.8.8, 1.2.3.4, 8.8.4.4, 172.16.0.5"
trusted_proxies: 172.16.0.5 (peer)
```

Reading from the right, `172.16.0.5` is trusted and skipped, and `8.8.4.4` is
the client. `8.8.8.8` and `1.2.3.4` are never reached.

Repeated `X-Forwarded-For` lines are joined in order; a repeated `X-Real-IP` or
`CF-Connecting-*` header is rejected. At most 32 hops are inspected, and a chain
that needs more fails closed. An unresolved request logs the
`client_ip_resolution_unresolved` event, the signal to watch for malformed
headers or missing trust settings.

### What counts as routable

The resolver rejects anything unparseable, loopback, link-local, multicast and
unspecified addresses, and these networks:

```text
0.0.0.0/8         (this network)   192.0.0.0/24      (IETF protocol assignments)
10.0.0.0/8        172.16.0.0/12     192.168.0.0/16    (RFC 1918)
100.64.0.0/10     (CGNAT, RFC 6598)
192.0.2.0/24      198.51.100.0/24  203.0.113.0/24    (TEST-NET)
198.18.0.0/15     (benchmarking)   240.0.0.0/4       (reserved Class E)
fc00::/7          (IPv6 unique local)
2001:db8::/32     (IPv6 documentation)
64:ff9b:1::/48    100::/64        100:0:0:1::/64     (IPv6 special-use)
2001:2::/48       3fff::/20       5f00::/16          (IPv6 special-use)
fec0::/10         (deprecated IPv6 site-local)
```

An IPv4-mapped IPv6 address is judged by its IPv4 address. The list is an
explicit snapshot of the IANA registry, not Python's `is_private`, whose answer
changes between releases; update it when the registry changes.

## The "unknown" outcome

When no trustworthy routable address exists, the result is `"unknown"` with
`resolved=False`, not an internal address passed off as the client. Rate
limits, the auth-failure blocklist and routing affinity must check `resolved`;
never pass an unresolved result to `normalize_ip_bucket()`, which would merge
unrelated callers into one key.

Unresolved sign-ins have no address to count by, so they are limited per
account only: five per username in 15 minutes from all unresolved browsers
together, plus the account-wide limit. With no shared per-address window, one
source's guesses cannot lock unresolved browsers out of other accounts;
guessing across many accounts is bounded only per account and by the
password-hashing capacity. A browser with the account's device cookie keeps its
own attempts; see
[The first admin account](deployment.md#the-first-admin-account). Phone codes to
unresolved clients share their own budget (`SMS_OTP_SEND_UNRESOLVED_PER_HOUR`,
200, and `SMS_OTP_SEND_UNRESOLVED_PER_DAY`, 1000). This keeps users from locking
each other out but cannot tell them apart; behind the bundled console, set up
trust as described in [Behind the console](#behind-the-console).

## Cloudflare Pseudo IPv4

With Cloudflare's [Pseudo IPv4](https://developers.cloudflare.com/network/pseudo-ipv4/)
set to "Overwrite headers", `CF-Connecting-IP` holds a synthetic `240.0.0.0/4`
address and the real one is in `CF-Connecting-IPv6`. The gateway uses
`CF-Connecting-IPv6` only when it parses as IPv6 and `CF-Connecting-IP` is an
IPv4 address inside `240.0.0.0/4`. A caller cannot forge that pair, because no
real client address comes from that range; otherwise `CF-Connecting-IP` wins.

## Bucketing: why IPv6 folds to a /64

An IPv6 client usually holds at least a `/64` and rotates privacy addresses
within it, so `normalize_ip_bucket()` groups IPv6 addresses by their `/64`
(`IPV6_BUCKET_PREFIXLEN = 64`). IPv4 addresses, and IPv4-mapped literals such as
`::ffff:192.0.2.1`, stay whole; anything unparseable, including `"unknown"`, is
returned unchanged. Signup and login rate limits, the auth-failure blocklist and
routing affinity group callers this way; logs keep the full address.
`derive_affinity_key()` keys sticky routing on the API key's hash, then an
inference grant (`grant:<id>`), then `ip:<bucket>`.

Seeing IPv6 clients on an IPv4-only origin is normal: a CDN accepts the client
over IPv6 and connects to the origin over IPv4.

## What is logged

`apps/backend/serving/servers/middleware/request_log.py` writes one
`http_request` line per request with the verdict and its inputs: `remote_ip`,
`peer_ip`, `ip_source`, `x_forwarded_for`, `x_real_ip`, `cf_connecting_ip`,
`cf_connecting_ipv6`, `user_agent`, `host`, `origin`, `referer`, `request_id` and
`session_id`. Each value is cut to 256 characters (the path to 512) with control
characters escaped, and `origin` and `referer` lose their query strings. At
`DEBUG`, an `http_request_headers` line lists every header, with credentials
(`authorization`, `cookie`, `x-api-key` and any header naming a key, token,
secret, password, session, signature or auth) replaced by `***`.

## What is persisted

`api_logs.metadata` (written in `apps/backend/serving/storage/postgres_log.py`)
stores per request:

| Surface | Handler | IP-related keys stored |
|---|---|---|
| `/v1/chat/completions` | `apps/backend/serving/servers/routers/completions.py` | `ip`, `user_agent`, `referer` |
| `/v1/embeddings` | `apps/backend/serving/servers/routers/embeddings.py` | `ip`, `peer_ip`, `ip_source`, `user_agent`, `referer` |
| `/v1/messages`, `/anthropic/v1/messages` | `apps/backend/serving/servers/routers/anthropic_messages.py` | `ip`, `peer_ip`, `ip_source`, `x_forwarded_for`, `x_real_ip`, `user_agent`, `referer` |
| Rejected requests (when rejection logging is on) | `apps/backend/serving/observability/rejection_log.py` | `ip` |

The `CF-Connecting-*` headers are logged but not stored. `login_events`
(`apps/backend/serving/storage/postgres_operational.py`) stores `ip` and
`user_agent` for each sign-in attempt.

### Retention is yours to set

Nothing expires these tables on a timer. The deletions that exist are run by an
administrator:

- `DELETE /admin/login-events?older_than_days=N` purges `login_events` by age.
- `DELETE /admin/login-events?user_id=...` purges one user's login events.
- `POST /admin/users/{user_id}/hard-delete` deletes the user and their
  `api_logs` rows, once permanent deletion is on; see
  [Secrets for deleting accounts](database.md#secrets-for-deleting-accounts).
- `POST /admin/recent-requests/clear-errors` drops recent error rows.

If you are subject to a data-protection regime, or do not want to keep client
addresses indefinitely, set and run a retention policy yourself; the project
ships none. Prompts and responses are stored only with `DB_STORE_FULL_CONTENT`
on; see [Request logging and privacy](database.md#request-logging-and-privacy).

## Testing your setup

```bash
uv run pytest tests/unit/utils/test_request_ip.py tests/unit/config/test_trusted_proxies.py tests/unit/middleware/test_request_log.py
```

These cover resolution, Pseudo IPv4, bucketing and spoofing cases; CIDR
validation; and the log fields. On a live gateway, send a request with an
absurd forwarded header and read `ip_source` in its log line to see which rule
the gateway believed.
