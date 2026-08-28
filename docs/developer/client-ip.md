<!-- Extracted from the FreeInference deployment runbook when that page moved
     to the distribution repository: this half documents neutral gateway
     behavior (serving/utils/request_ip.py) and applies to any deployment. -->
# Client IP Resolution

`apps/backend/serving/utils/request_ip.py` resolves the client IP, gated by two
independent flags:

| Flag | Asserts | Default |
|---|---|---|
| `TRUST_PROXY_HEADERS` | Some trusted proxy rewrites `X-Forwarded-For` / `X-Real-IP` | `0`, but `1` in `deploy/docker/docker-compose.yml` |
| `TRUST_CLOUDFLARE_HEADERS` | The **immediate** proxy is Cloudflare, so `CF-Connecting-IP` is authoritative | `0` everywhere — explicit opt-in |

With both set, resolution order is `CF-Connecting-IPv6` → `CF-Connecting-IP` →
`X-Forwarded-For` (first **routable** entry — leading private/loopback/ULA hops an
intermediary inserted are skipped) → `X-Real-IP` → socket peer.

`CF-Connecting-IPv6` outranks `CF-Connecting-IP`, but **only when the two
corroborate each other**. Cloudflare sends the IPv6 header solely when
[Pseudo IPv4](https://developers.cloudflare.com/network/pseudo-ipv4/) is set to
"Overwrite headers" — in which case `CF-Connecting-IP` holds a synthetic Class E
(`240.0.0.0/4`) address derived from the visitor rather than the visitor's real
address. Preferring the synthetic would send an IPv6 client down the IPv4
bucketing path, handing every rotated privacy address its own rate-limit bucket
and defeating the `/64` grouping below.

The corroboration matters because Cloudflare *omits* `CF-Connecting-IPv6` when
Pseudo IPv4 is off rather than clearing it — so any caller can supply one.
Only `CF-Connecting-IP` is overwritten on every request. The gateway therefore
honors the IPv6 header only when it parses as IPv6 *and* `CF-Connecting-IP`
holds the accompanying Class E synthetic; a real client address is never drawn
from that reserved range, so the pairing cannot be forged from outside.
Otherwise `CF-Connecting-IP` remains authoritative. Both headers are logged, so
a synthetic — or a forgery attempt — stays visible.

> **Before setting `TRUST_CLOUDFLARE_HEADERS=1`, restrict the origin.** Every
> one of these headers is attacker-controlled on any request that reaches the
> origin without passing through the edge, and Cloudflare's "Full (strict)" TLS
> mode does **not** prevent that — it authenticates the origin to Cloudflare,
> not Cloudflare to the origin. Until your origin enforces
> [Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/)
> or a Cloudflare IP allowlist, anyone who learns the origin address can send a
> forged `CF-Connecting-IP` directly. This caveat is not new to the Cloudflare
> header — it applies equally to `TRUST_PROXY_HEADERS` and `X-Forwarded-For` —
> but neither flag should be enabled on the assumption alone.

`CF-Connecting-IP` comes first deliberately. Cloudflare always overwrites that
header, but it **appends** to a client-supplied `X-Forwarded-For` — so reading
the leftmost `X-Forwarded-For` entry would let any caller dictate the IP the
gateway logs and rate-limits on, unless your fronting proxy also rewrites the
header.

The two flags are separate because they are separate facts. A non-Cloudflare
proxy may rewrite `X-Forwarded-For` perfectly well while forwarding a
client-supplied `CF-Connecting-IP` untouched — trusting the Cloudflare header on
the strength of generic proxy trust would hand that caller the spoof it was
denied via `X-Forwarded-For`. **If you front this service with anything other
than Cloudflare, leave `TRUST_CLOUDFLARE_HEADERS=0`.**

Each request log line carries `remote_ip` (the resolved client), `peer_ip` (the
socket peer), `ip_source`, and the raw
header values, so the provenance of any address is visible after the fact.

**IPv6 addresses in the logs are expected even for an IPv4-only origin.** Cloudflare publishes an AAAA record regardless of origin support:
a client connects to the edge over IPv6, and Cloudflare opens a separate IPv4
connection to the origin carrying the original address in `CF-Connecting-IP`.
The client's address family is decoupled from the origin's: for an IPv4-only
origin, an IPv6 `peer_ip` would be a genuine surprise.

Because a client typically holds an entire IPv6 prefix (a `/64` at minimum) and
RFC 4941 privacy addresses rotate within it, full IPv6 addresses make poor
identity keys. Logs and analytics keep the full address, but rate-limit and
routing-affinity buckets fold IPv6 to its `/64` via `normalize_ip_bucket()`.
IPv4 continues to bucket per address, and IPv4-mapped literals
(`::ffff:192.0.2.1`, which a dual-stack listener reports for IPv4 peers) bucket
on the embedded IPv4 rather than being folded by prefix.
