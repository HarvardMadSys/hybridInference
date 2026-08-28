# Trusted Proxies and Client IPs

Almost every deployment puts something in front of the gateway — a CDN, a
reverse proxy, a tunnel, a load balancer. Once that is true, the socket peer
the gateway sees is the proxy, not the caller, and the caller's address only
survives in a request header that anyone can also set by hand.

`apps/backend/serving/utils/request_ip.py` is the single place that decides
which address to believe. Everything downstream — request logs, per-IP rate
limits, the auth-failure blocklist, sticky routing affinity, and the rows
written to `api_logs` — reads its answer. This page describes what that module
does, how to configure it, and what ends up stored as a result.

## The two trust flags

Nothing in a forwarding header is trusted unless you say so. Two independent
environment variables gate that, and they assert two different facts.

| Variable | What setting it to `1` asserts | Ships as |
|---|---|---|
| `TRUST_PROXY_HEADERS` | *Some* trusted proxy in front of the gateway overwrites `X-Forwarded-For` / `X-Real-IP`. | `0` in `.env.example`; `deploy/docker/docker-compose.yml` defaults the backend service to `1` |
| `TRUST_CLOUDFLARE_HEADERS` | The **immediate** proxy is Cloudflare, so `CF-Connecting-IP` is edge-set and authoritative. | `0` in both — explicit opt-in |

`TRUST_CLOUDFLARE_HEADERS` is read only when `TRUST_PROXY_HEADERS` is also `1`;
on its own it does nothing.

They are separate because they are separate facts. A non-Cloudflare proxy may
rewrite `X-Forwarded-For` perfectly well while forwarding a client-supplied
`CF-Connecting-IP` untouched — trusting the Cloudflare header on the strength
of generic proxy trust would hand that caller the spoof it was denied via
`X-Forwarded-For`. **If you front this service with anything other than
Cloudflare, leave `TRUST_CLOUDFLARE_HEADERS=0`.**

:::{warning}
Before enabling either flag, restrict the origin. Every one of these headers is
attacker-controlled on any request that reaches the origin without passing
through the proxy you are trusting. For Cloudflare specifically, "Full (strict)"
TLS does **not** prevent that — it authenticates the origin to Cloudflare, not
Cloudflare to the origin. Until the origin enforces
[Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/)
or an edge IP allowlist, anyone who learns the origin address can send a forged
`CF-Connecting-IP` straight to it. The same caveat applies to
`TRUST_PROXY_HEADERS` and `X-Forwarded-For`.
:::

With `TRUST_PROXY_HEADERS=0`, resolution short-circuits to the socket peer and
no header influences the result — the correct behaviour for a gateway exposed
directly. The raw header values are still captured on the returned
`ClientIpInfo` and logged, so a misconfiguration is visible rather than silent.

## Resolution order

`get_client_ip_info()` returns a frozen `ClientIpInfo` with the resolved
`client_ip`, the `peer_ip` it was resolved against, and a `source` label naming
the rung that won. First match wins:

1. **`CF-Connecting-IP`** — only when `TRUST_CLOUDFLARE_HEADERS=1`. Source
   `cf-connecting-ip`, or `cf-connecting-ipv6` for a corroborated Pseudo IPv4
   pair (below). Note that this rung returns the header value as it stands: the
   routability filter applied to the `X-Forwarded-For` rungs is not applied
   here, because the edge is assumed to have written it.
2. **The first *routable* `X-Forwarded-For` hop**, scanned left to right.
   Source `x-forwarded-for`.
3. **`X-Real-IP`**, when it is routable. Source `x-real-ip`.
4. **The socket peer** (`request.client.host`, or the literal `"unknown"` when
   Starlette reports no client). Source `socket`. This is both the direct-
   connection case and the last resort when no forwarded hop was usable.

`CF-Connecting-IP` is checked before `X-Forwarded-For` deliberately: Cloudflare
overwrites its own header on every request, but it *appends* to a client-supplied
`X-Forwarded-For`, so reading the leftmost entry there would let any caller
dictate the address the gateway logs and rate-limits on.

### What counts as routable

`_is_reportable_ip()` rejects anything unparseable, plus loopback, link-local,
multicast and unspecified addresses, and these networks:

```text
10.0.0.0/8        172.16.0.0/12     192.168.0.0/16    (RFC 1918)
100.64.0.0/10     (CGNAT, RFC 6598)
fc00::/7          (IPv6 unique local)
```

An IPv4-mapped IPv6 literal is judged by its embedded IPv4 address, so a mapped
private peer is still rejected.

Skipping these hops is the point of the left-to-right scan: an intermediary that
inserts its own internal address as the leftmost hop would otherwise have that
internal address reported — and logged — as the client.

The list is written out explicitly rather than delegating to
`ipaddress.is_private` / `is_global`, because those reclassified the
documentation and benchmark ranges between CPython 3.12.4 and 3.13; hard-coding
the stable RFC ranges keeps IP resolution from depending on the interpreter
version.

### Known limitation

A spoofed *public* leftmost `X-Forwarded-For` entry is still taken at face
value. Stripping it correctly requires a configured trusted-proxy CIDR set, so
the gateway can tell its own proxies from client-supplied hops; taking the
rightmost public hop instead would misattribute every client sitting behind a
shared public intermediary. The module says so in its own docstring, and the
hardening is not implemented. If you need it, either terminate `X-Forwarded-For`
at your proxy (overwrite rather than append) or trust only an edge header your
proxy is known to overwrite.

### Cloudflare Pseudo IPv4

`CF-Connecting-IPv6` outranks `CF-Connecting-IP`, but **only when the two
corroborate each other**.

Cloudflare emits `CF-Connecting-IPv6` solely when
[Pseudo IPv4](https://developers.cloudflare.com/network/pseudo-ipv4/) is set to
"Overwrite headers" — in that mode `CF-Connecting-IP` holds a synthetic Class E
(`240.0.0.0/4`) address derived from the visitor rather than the visitor's real
one. Preferring the synthetic would push an IPv6 client down the IPv4 bucketing
path, handing every rotated privacy address its own rate-limit bucket and
defeating the `/64` grouping below.

The corroboration matters because with Pseudo IPv4 off the header is *absent*
rather than cleared, so any caller can supply one. `_pseudo_ipv4_origin()`
therefore honours it only when the IPv6 header parses as IPv6 *and*
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

Logs and analytics keep the full address; only the buckets fold. Callers of
`normalize_ip_bucket()` at this revision are signup rate limiting, login rate
limiting, the repeated-auth-failure blocklist, and routing affinity.

`derive_affinity_key()` is the sticky-routing variant. It falls through caller
identities in order of how precisely each names one caller: the presented API
key's hash, then an inference-grant id (`grant:<id>`), then
`ip:<bucket>` for traffic with no credential at all. It lives beside the IP
helpers rather than on a router so that every surface dispatching to a pooled
adapter derives the caller identity the same way.

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
headers are logged but not persisted on either surface.

**`login_events`** (`apps/backend/serving/storage/postgres_operational.py`)
stores `ip` and `user_agent` per login attempt alongside the outcome.

### Retention is yours to set

Nothing in this repository expires either table on a timer. The only deletions
that exist are operator-initiated:

- `DELETE /admin/login-events?older_than_days=N` — purge `login_events` by age.
- `DELETE /admin/login-events?user_id=...` — purge one user's login events.
- `hard_delete_user_data(user_id)` — wipes that user's `api_logs` rows.
- `delete_recent_error_requests(hours=N)` — drops recent error rows.

If your deployment is subject to a data-protection regime, or you simply do not
want to hold client addresses indefinitely, you must decide on and implement a
retention policy yourself. This project does not ship one and does not pick a
default on your behalf.

Two knobs that reduce what there is to retain in the first place:

- Leaving both trust flags at `0` where no proxy is in front means only the
  socket peer is ever recorded.
- Prompt and response content is governed separately by
  `DB_STORE_FULL_CONTENT` (default `false`, which hashes content rather than
  storing it verbatim; see `apps/backend/serving/config/settings.py`).

## Testing your setup

`tests/unit/utils/test_request_ip.py` covers the resolution table, the
Pseudo IPv4 corroboration and the bucketing rules;
`tests/unit/middleware/test_request_log.py` covers the log fields. Run them
with:

```bash
uv run pytest tests/unit/utils/test_request_ip.py tests/unit/middleware/test_request_log.py
```

To check a live gateway, send a request with a deliberately absurd forwarded
header and look at the `ip_source` in the resulting log line — it tells you
which rung the gateway actually believed.
