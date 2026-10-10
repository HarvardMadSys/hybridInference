# Trusted Proxies and Client IPs

Almost every deployment puts something in front of the gateway — a CDN, a
reverse proxy, a tunnel, a load balancer. Once that is true, the socket peer
the gateway sees is the proxy, not the caller, and the caller's address only
survives in a request header that anyone can also set by hand.

The gateway therefore believes a forwarded address only from peers you have
authorized. Start with the row that matches what sits in front of your
gateway:

| In front of the gateway | Set |
|---|---|
| Nothing: clients connect directly | Nothing. Forwarding headers are ignored by default. |
| A reverse proxy or load balancer at a known address | `TRUST_PROXY_HEADERS=1` and `TRUSTED_PROXIES=<that proxy's address>/32` |
| Cloudflare, connecting straight to the gateway | `TRUST_PROXY_HEADERS=1`, `TRUST_CLOUDFLARE_HEADERS=1` and `TRUSTED_CLOUDFLARE_NETWORKS=<Cloudflare's published ranges>` |
| Cloudflare, then your own proxy, then the gateway | As above, but `TRUSTED_CLOUDFLARE_NETWORKS=<your proxy's address>/32`, and only if that proxy accepts connections from Cloudflare alone and handles the header correctly |
| Clients on a private network (such as `10.x`) connecting directly | `TRUSTED_DIRECT_CLIENT_NETWORKS=<those client ranges>` |

Each of these is a setting under **Network** on the admin console's
**Configuration** tab; the `NAME=value` forms on this page are how they look
in the environment, from which a new deployment's first start copies them. See
[Settings stored in the database](configuration.md#settings-stored-in-the-database).

The rest of this page explains each setting, how the address is resolved, and
what is logged and stored. `apps/backend/serving/utils/request_ip.py` is the one
place in the code that makes this decision.

## The server underneath

For the module to be the single decision point, the server it runs inside must
not make the same decision first. uvicorn carries its own proxy-header
handling, and it defaults **on**: unless told otherwise, it rewrites
`request.client` — the socket peer as the application sees it — and the URL
scheme from `X-Forwarded-For` / `X-Forwarded-Proto` whenever the TCP peer is
in `--forwarded-allow-ips` (default `127.0.0.1`, also settable through the
`FORWARDED_ALLOW_IPS` environment variable). That rewrite happens before any
application code runs, upstream of everything this page describes, so left
enabled it hands `request_ip.py` an already-forged "socket peer" while
`TRUST_PROXY_HEADERS=0` promises that no header influences the result. With
`--forwarded-allow-ips "*"`, the leftmost `X-Forwarded-For` entry — whatever
the caller wrote — would become the socket peer on every request.

Every launch configuration in this repository therefore passes
`--no-proxy-headers` explicitly — `deploy/docker/Dockerfile.backend`
and both systemd units —
and a test fails if one stops doing so. If you run the gateway under your own
process manager, pass `--no-proxy-headers` there too: leaving out
`--proxy-headers` is not enough, because the default is on.

Two consequences of the server never interpreting forwarded headers:

- `request.url.scheme` is always `http` behind a TLS-terminating proxy. Set
  `BASE_URL` (under **General** on the **Configuration** tab) so absolute
  URLs — signup verification and password-reset email links — do not fall
  back to the request scheme.
- `peer_ip` below is the genuine TCP peer again, which is what makes it usable
  as the un-forgeable anchor the rest of this page treats it as.

## Trust configuration

Forwarding headers (`X-Forwarded-For`, `CF-Connecting-IP`, etc.) are
**attacker-controlled on every request** that reaches the origin without passing
through a trusted proxy. The gateway requires explicit authorization before any
header influences the result.

### Trusted proxies

The `trusted_proxies` setting is a comma-separated list of CIDR ranges
authorized to assert forwarding provenance via `X-Forwarded-For` / `X-Real-IP`:

```bash
# Example: a single nginx reverse proxy at a known internal address
TRUSTED_PROXIES=172.19.0.2/32
```

Trust the narrowest possible addresses. Only the specific proxy IP(s) that
terminate connections from the internet and forward to the gateway should be
trusted. Do not trust broad internal subnets — that would allow any host
within that subnet to assert client identity on any request.

An invalid CIDR is refused: the **Configuration** tab will not save it, and in
the environment it stops the gateway at startup.

### Direct private client networks

Private RFC1918, CGNAT, and ULA socket peers remain unresolved by default
because a container bridge or shared internal proxy is not an individual
client. If a deployment has clients connecting directly over one of these
networks, authorize only those client CIDRs with
`TRUSTED_DIRECT_CLIENT_NETWORKS`. This setting does not authorize forwarding
headers and must not include shared proxy networks.

```bash
TRUSTED_DIRECT_CLIENT_NETWORKS=10.42.0.0/16,100.64.0.0/10,fd00:42::/64
```

### Cloudflare-header-authorized peers

`CF-Connecting-IP` requires **separate** authorization. A generic trusted
reverse proxy does NOT make a client-supplied `CF-Connecting-IP` safe — only
operators who have verified a Cloudflare header-authority path should populate
this:

```bash
# Example: Cloudflare → application directly
# These are Cloudflare's origin-facing IP ranges (the socket peer),
# NOT the visitor addresses in CF-Connecting-IP.
# See: https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/
TRUSTED_CLOUDFLARE_NETWORKS=173.245.48.0/20  # example Cloudflare origin range
```

`TRUSTED_CLOUDFLARE_NETWORKS` means the immediate socket peers authorized to
assert Cloudflare semantics; it does not have to contain Cloudflare's public
ranges. If your topology is Cloudflare → nginx → HybridInference, then
HybridInference sees nginx as the socket peer. In that case, list only nginx's
exact address when nginx is exclusively reachable from Cloudflare and correctly
sanitizes/overwrites the header. Cloudflare itself recommends restricting
origin access to Cloudflare addresses to prevent direct-origin spoofing.

This separation ensures that a misconfigured generic proxy cannot accidentally
authorize attacker-supplied Cloudflare headers.

### Trust flags

Three settings control header processing:

- `TRUST_PROXY_HEADERS=1`: enables processing of `X-Forwarded-For` and
  `X-Real-IP` headers, but **only** when the immediate peer is in
  `trusted_proxies`.
- `TRUST_CLOUDFLARE_HEADERS=1`: enables processing of `CF-Connecting-IP`,
  but **only** when the immediate peer is in `trusted_cloudflare_networks`.
- `TRUST_X_REAL_IP=1`: separately opts in to the `X-Real-IP` assertion scheme.
  It is ignored by default, and it is never consulted when XFF is present.

All three flags default to `0` (disabled). Setting a flag alone does nothing if the
corresponding network list is empty — this is the fail-closed default.
`TRUST_CLOUDFLARE_HEADERS=1` additionally requires the master
`TRUST_PROXY_HEADERS=1` flag and a non-empty Cloudflare-authorized network list;
these combinations are refused, both on save in the **Configuration** tab and
at startup from the environment.
`TRUST_X_REAL_IP=1` likewise requires `TRUST_PROXY_HEADERS=1` and uses the same
trusted-proxy network list, but it is consulted only when XFF is completely
absent.

### Why this matters

Without these gates, a client can send `X-Forwarded-For: <anything>` and the
gateway logs and rate-limits on the address the client chose. Naming the exact
peers that may forward an address closes that hole, and anything not named is
ignored.

## Resolution order

`get_client_ip_info()` returns a frozen `ClientIpInfo` with the resolved
`client_ip`, the `peer_ip` it was resolved against, and a `source` label naming
the rung that won. The `trusted_proxy_headers` field describes whether any
forwarding identity header was **actually authorized** for this request. The
more specific `trusted_forwarded_headers` and `trusted_cloudflare_headers`
fields identify which authority applied.

Resolution walks the trust boundary correctly:

1. **Peer not authorized** — forwarding headers are ignored entirely. The
   socket peer is used if routable; otherwise `"unknown"`.
2. **`CF-Connecting-IP`** — only when the peer is in
   `trusted_cloudflare_networks` **and** `TRUST_CLOUDFLARE_HEADERS=1`. The
   operator must explicitly configure which networks are Cloudflare-authorized;
   a generic reverse proxy does not make this header safe. A corroborated
   Pseudo IPv4 pair yields the real IPv6 address from `CF-Connecting-IPv6`.
3. **`X-Forwarded-For`** — only when the peer is in `trusted_proxies` **and**
   `TRUST_PROXY_HEADERS=1`. Walked **right-to-left**: trusted-proxy hops are
   skipped. The **first untrusted hop terminates provenance**:
   * if routable → it is the client;
   * if non-routable or malformed → return `"unknown"`.
   **Never continue leftward** — that would cross the trust boundary and
   consume attacker-controlled values.
4. **`X-Real-IP`** — only when `TRUST_X_REAL_IP=1`, trusted, and routable, and
   only when XFF is completely absent. A present XFF chain that is malformed,
   ambiguous, overlong, or contains only trusted hops terminates provenance;
   it never falls through to this second assertion scheme.
5. **The socket peer** — a direct connection, or the last resort when no
   forwarded hop is usable. If the peer itself is non-routable, the result is
   `"unknown"`.

Request logs carry `ip_source` together with the socket peer and raw forwarding
fields. The structured `client_ip_resolution_unresolved` event is the canonical
signal for malformed-header and degraded-enforcement spikes; its structured
`event="client_ip_resolution"` metadata identifies the resolution event
without turning an unresolved peer into a client identity.

### Example: multi-hop chain

```
XFF: "1.2.3.4, fdbd:dc02::153, 10.0.0.1, 172.16.0.5"
trusted_proxies: 172.16.0.5 (peer), 10.0.0.1
```

Walking right-to-left:

| Hop | Trusted? | Routable? | Action |
|-----|----------|-----------|--------|
| 172.16.0.5 | yes (peer) | no | skip (trusted) |
| 10.0.0.1 | yes | no | skip (trusted) |
| fdbd:dc02::153 | no | no (ULA) | **provenance terminates → unknown** |

We do NOT continue to `1.2.3.4` — that would cross the trust boundary.

### Example: attacker prepends fake addresses

```
XFF: "8.8.8.8, 1.2.3.4, 8.8.4.4, 172.16.0.5"
trusted_proxies: 172.16.0.5 (peer)
```

Walking right-to-left:

| Hop | Trusted? | Routable? | Action |
|-----|----------|-----------|--------|
| 172.16.0.5 | yes | no | skip (trusted) |
| 8.8.4.4 | no | yes | **return as client** |

The attacker's prepended `8.8.8.8` and `1.2.3.4` are never reached. The old
leftmost-trust model would have returned `8.8.8.8`.

Duplicate field handling is fail closed. All physical `X-Forwarded-For` field
lines are joined in wire order before parsing, preserving empty hops. The
singleton `X-Real-IP` and `CF-Connecting-*` fields are rejected when repeated;
the resolver never selects first or last based on framework ordering. At most
32 hops are inspected from the trusted side. Entries left of the first
untrusted boundary are ignored, while a provenance chain that requires more
than 32 inspected hops fails closed.

### What counts as routable

The resolver rejects anything unparseable, plus loopback, link-local,
multicast and unspecified addresses, and these networks:

```text
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

An IPv4-mapped IPv6 literal is judged by its embedded IPv4 address. A mapped
private peer is accepted only when its embedded address falls inside
`TRUSTED_DIRECT_CLIENT_NETWORKS`.

The list is an explicit IANA-derived snapshot rather than a delegation to
`ipaddress.is_private` / `is_global`, because those reclassify special-use
ranges between CPython releases. It requires maintenance when the IANA
special-purpose registry changes.

## The "unknown" outcome

When the gateway cannot determine a trustworthy routable client address, it
returns `"unknown"` — with `ClientIpInfo.resolved=False` — rather than passing
off an internal address as the client.
This is correct: if the socket peer is a Docker bridge and there is no
trustworthy forwarding provenance, `"unknown"` is more useful than
`172.19.0.1`.

Downstream consumers that key on `client_ip` (rate limits, auth-failure
blocklist, routing affinity) must inspect `ClientIpInfo.resolved` and handle
`"unknown"` explicitly — it is a legitimate outcome, not an error. New
callers never pass unresolved provenance to `normalize_ip_bucket()`, because
that would collapse unrelated callers onto one shared key. The deprecated
`get_client_ip_bucket()` helper retains its legacy behavior and must not be
used for new enforcement or affinity code.

## Cloudflare Pseudo IPv4

Within rung 2, `CF-Connecting-IPv6` wins over `CF-Connecting-IP` — it is not a
rung of its own — but **only when the two corroborate each other**.

Cloudflare emits `CF-Connecting-IPv6` solely when
[Pseudo IPv4](https://developers.cloudflare.com/network/pseudo-ipv4/) is set to
"Overwrite headers" — in that mode `CF-Connecting-IP` holds a synthetic Class E
(`240.0.0.0/4`) address derived from the visitor rather than the visitor's real
one. Preferring the synthetic would push an IPv6 client down the IPv4 bucketing
path, handing every rotated privacy address its own rate-limit bucket and
defeating the `/64` grouping below.

The corroboration matters because with Pseudo IPv4 off the header is *absent*
rather than cleared, so any caller can supply one. The gateway therefore
honours it only when the IPv6 header parses as IPv6 *and*
`CF-Connecting-IP` parses as IPv4 *and* that IPv4 falls inside `240.0.0.0/4`.
Cloudflare controls that second value and a real client address is never drawn
from the reserved Class E range, so the pairing cannot be forged from outside.
Otherwise `CF-Connecting-IP` stays authoritative. Both raw headers are carried
on `ClientIpInfo` and logged, so a synthetic — or a forgery attempt — stays
visible after the fact.

## Bucketing: why IPv6 folds to a /64

A single IPv6 client is typically delegated an entire prefix (a `/64` at
minimum, often a `/56` or `/48`), and RFC 4941 privacy addresses rotate within
it. A full IPv6 address is therefore a poor identity key: a client can present
effectively unlimited distinct ones.

`normalize_ip_bucket()` is the grouping key used wherever an address has to
stand in for a caller:

- IPv6 → the `/64` network it sits in (`IPV6_BUCKET_PREFIXLEN = 64`).
- IPv4 → the address itself.
- IPv4-mapped literals (`::ffff:192.0.2.1`, which a dual-stack listener reports
  for IPv4 peers) → the embedded IPv4 address. Folding these by prefix would
  collapse every IPv4 client into a single `::/64`.
- Anything unparseable (including the `"unknown"` fallback and scoped literals)
  → returned unchanged.

Logs and analytics keep the full address; only the buckets fold. Signup rate
limiting, login rate limiting, the repeated-auth-failure blocklist and routing
affinity all group callers this way.

`derive_affinity_key()` is the sticky-routing variant. It falls through caller
identities in order of how precisely each names one caller: the presented API
key's hash, then an inference-grant id (`grant:<id>`), then
`ip:<bucket>` for traffic with no credential at all. It lives beside the IP
helpers rather than on a router so that every surface dispatching to a pooled
adapter derives the caller identity the same way.

### An IPv6 client on an IPv4-only origin is normal

Seeing IPv6 addresses in the logs does not mean the origin gained IPv6. A CDN
that publishes an AAAA record accepts the client over IPv6 and then opens a
separate IPv4 connection to the origin, carrying the original address in the
forwarding header. The client's address family is decoupled from the origin's,
so for an IPv4-only origin it is an IPv6 `peer_ip` — not an IPv6 `remote_ip` —
that would be the genuine surprise.

## What is logged

`apps/backend/serving/servers/middleware/request_log.py` emits one structured
`http_request` line per request carrying the resolved address *and* its
provenance: `remote_ip`, `peer_ip`, `ip_source`, `x_forwarded_for`, `x_real_ip`,
`cf_connecting_ip`, `cf_connecting_ipv6`, alongside `user_agent`, `host`,
`origin`, `referer`, `request_id` and `session_id`.

Keeping the raw headers next to the verdict is what makes a wrong address
diagnosable: you can see which rung fired and what the alternatives said.

At `DEBUG` the middleware additionally emits an `http_request_headers` line with
every request header, truncated to 256 characters each and with `authorization`
and `x-api-key` replaced by `***`.

## What is persisted

The output of this module does not stay in the log file. It is written to the
database.

**`api_logs.metadata`** (a JSONB column; see
`apps/backend/serving/storage/log_schema.py` and the insert in
`apps/backend/serving/storage/postgres_log.py`) receives, per request:

| Surface | Handler | IP-related keys stored |
|---|---|---|
| `/v1/chat/completions` | `apps/backend/serving/servers/routers/completions.py` | `ip`, `user_agent`, `referer` |
| `/v1/messages`, `/anthropic/v1/messages` | `apps/backend/serving/servers/routers/anthropic_messages.py` | `ip`, `peer_ip`, `ip_source`, `x_forwarded_for`, `x_real_ip`, `user_agent`, `referer` |
| Rejected requests (when rejection logging is enabled) | `apps/backend/serving/observability/rejection_log.py` | `ip` |

The Anthropic surface stores the full provenance, not just the verdict — so a
disputed address can be re-derived from the row. The two `CF-Connecting-*`
headers are logged but not persisted on any surface.

**`login_events`** (`apps/backend/serving/storage/postgres_operational.py`)
stores `ip` and `user_agent` per login attempt alongside the outcome.

### Retention is yours to set

Nothing in this repository expires either table on a timer. The only deletions
that exist are operator-initiated:

- `DELETE /admin/login-events?older_than_days=N` — purge `login_events` by age.
- `DELETE /admin/login-events?user_id=...` — purge one user's login events.
- `POST /admin/users/{user_id}/hard-delete` — deletes the user, including their
  `api_logs` rows. It works only once permanent deletion is turned on; see
  [Secrets for deleting accounts](database.md#secrets-for-deleting-accounts).
- `POST /admin/recent-requests/clear-errors` — drops recent error rows.

If your deployment is subject to a data-protection regime, or you simply do not
want to hold client addresses indefinitely, you must decide on and implement a
retention policy yourself. This project does not ship one and does not pick a
default on your behalf.

Two knobs that reduce what there is to retain in the first place:

- Leaving `trusted_proxies` empty where no proxy is in front means only the
  socket peer is ever recorded.
- Prompt and response content is governed separately by
  `DB_STORE_FULL_CONTENT`. At its default, `false`, prompts and responses are
  not stored at all; see
  [Request logging and privacy](database.md#request-logging-and-privacy).

## Testing your setup

`tests/unit/utils/test_request_ip.py` covers the resolution table, the
Pseudo IPv4 corroboration, the bucketing rules, and adversarial cases (spoofed
headers, multi-hop chains, malformed inputs, all-private chains);
`tests/unit/config/test_trusted_proxies.py` covers CIDR validation;
`tests/unit/middleware/test_request_log.py` covers the log fields. Run them
with:

```bash
uv run pytest tests/unit/utils/test_request_ip.py tests/unit/config/test_trusted_proxies.py tests/unit/middleware/test_request_log.py
```

To check a live gateway, send a request with a deliberately absurd forwarded
header and look at the `ip_source` in the resulting log line — it tells you
which rung the gateway actually believed.
