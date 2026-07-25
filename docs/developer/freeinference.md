# FreeInference Deployment

## Cloudflare + Nginx + FastAPI (current)

Traffic flows through three layers before reaching the application:

```
Client ──▶ Cloudflare ──▶ Nginx (:443) ──▶ FastAPI  (:8080)
                                      ├──▶ pgAdmin  (:5050)   [admin-only]
                                      └──▶ Frontend (:3001)
```

| Layer | Role |
|-------|------|
| **Cloudflare** | CDN, DDoS protection, edge SSL termination. SSL/TLS mode set to **Full (strict)** so Cloudflare verifies the origin certificate. `CF-Connecting-IP` header carries the real client IP, and is what the gateway reads first (see [Client IP resolution](#client-ip-resolution)). |
| **Nginx** | TLS termination (Let's Encrypt cert), path-based routing (see below), per-location body size limits (`/v1/` is bumped to 50 MB to accommodate large completion payloads and Qdrant vector upserts via the `/v1/qdrant` proxy; everything else uses the Nginx 1 MB default), WebSocket upgrade. |
| **FastAPI** | API logic — request authentication, model routing, backpressure, Qdrant proxy, and observability. Listens on `127.0.0.1:8080`. |

Nginx path routing:

- `/v1/`, `/auth/`, `/user/`, `/admin/`, `/internal/playground/` → FastAPI
- `/pgadmin/` → pgAdmin — gated by `auth_request` against FastAPI's `/internal/verify-admin` endpoint, so only admins reach it
- everything else → frontend

Docker Compose manages all services (backend, frontend, PostgreSQL, plus
pgAdmin behind the `admin` profile) with automatic restarts via
`restart: unless-stopped`.

### Deployment

All services are defined in `deploy/docker/docker-compose.yml`. From the project root:

```bash
cp .env.example .env   # Configure secrets
make up                # Start all services
make ps                # Verify health
```

Nginx runs on the host (not containerized) for SSL termination. See
[Deployment](deployment.md) for the full guide.

### Client IP resolution

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
> not Cloudflare to the origin. Until Nginx enforces
> [Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/)
> or a Cloudflare IP allowlist, anyone who learns the origin address can send a
> forged `CF-Connecting-IP` directly. This caveat is not new to the Cloudflare
> header — it applies equally to `TRUST_PROXY_HEADERS` and `X-Forwarded-For` —
> but neither flag should be enabled on the assumption alone.

`CF-Connecting-IP` comes first deliberately. Cloudflare always overwrites that
header, but it **appends** to a client-supplied `X-Forwarded-For` — so reading
the leftmost `X-Forwarded-For` entry would let any caller dictate the IP the
gateway logs and rate-limits on, unless the host Nginx config also rewrites the
header.

The two flags are separate because they are separate facts. A non-Cloudflare
proxy may rewrite `X-Forwarded-For` perfectly well while forwarding a
client-supplied `CF-Connecting-IP` untouched — trusting the Cloudflare header on
the strength of generic proxy trust would hand that caller the spoof it was
denied via `X-Forwarded-For`. **If you front this service with anything other
than Cloudflare, leave `TRUST_CLOUDFLARE_HEADERS=0`.**

Each request log line carries `remote_ip` (the resolved client), `peer_ip` (the
socket peer — Nginx on loopback in this topology), `ip_source`, and the raw
header values, so the provenance of any address is visible after the fact.

**IPv6 addresses in the logs are expected even though the origin is
IPv4-only.** Cloudflare publishes an AAAA record regardless of origin support:
a client connects to the edge over IPv6, and Cloudflare opens a separate IPv4
connection to the origin carrying the original address in `CF-Connecting-IP`.
The client's address family is decoupled from ours. `peer_ip` should always be
IPv4 here — an IPv6 `peer_ip` would be a genuine surprise.

Because a client typically holds an entire IPv6 prefix (a `/64` at minimum) and
RFC 4941 privacy addresses rotate within it, full IPv6 addresses make poor
identity keys. Logs and analytics keep the full address, but rate-limit and
routing-affinity buckets fold IPv6 to its `/64` via `normalize_ip_bucket()`.
IPv4 continues to bucket per address, and IPv4-mapped literals
(`::ffff:192.0.2.1`, which a dual-stack listener reports for IPv4 peers) bucket
on the embedded IPv4 rather than being folded by prefix.

### Runtime Operations

- Restart: `make restart` or `make restart s=backend`
- Follow logs: `make logs` or `make logs s=backend`
- Health check: `curl https://freeinference.org/health`
- List registered models: `curl https://freeinference.org/v1/models | jq`

### Why Nginx Is Back

Nginx was briefly removed (see Legacy section below) when FreeInference was API-only and Cloudflare handled all edge concerns. It was re-introduced when we added:

- **Frontend**: The Next.js web UI runs on port 3001 and needs to share the `freeinference.org` domain with the API. Path-based routing (`/v1/*` → backend, `/*` → frontend) is a natural fit for Nginx.
- **Body size limits**: Qdrant vector upserts can be large. Nginx's `client_max_body_size` gives a clear, configurable gate before traffic hits FastAPI.
- **WebSocket upgrade**: Nginx handles the `Upgrade` / `Connection` headers cleanly for SSE and WebSocket-based streaming.

## Legacy Architectures

### FastAPI direct (v3, abandoned)

We previously served OpenRouter-compatible traffic directly through FastAPI listening on port 80, without Nginx. This was simpler but could not support frontend co-hosting or fine-grained body size limits. Once the frontend was added, we moved back to Nginx.

### Nginx (v2, abandoned)

We briefly fronted FastAPI (running on port 8080) with vanilla Nginx that listened on port 80 (redirecting to HTTPS) and terminated TLS on port 443 for `https://freeinference.org`. Once Cloudflare took over edge SSL duties, the extra hop mostly added deployment and observability complexity without material benefit, so the setup was removed.

### Nginx + Lua via OpenResty (v1, abandoned)

We previously relied on OpenResty (Nginx + Lua) to provide a production routing tier across multiple LLM backends. The stack handled model mapping, load balancing, health checks, and error handling. We keep the installation notes for posterity.

#### Overview

```bash
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│   Client    │─────▶│  OpenResty       │─────▶│  Backend 1      │
│  (API Call) │      │  (Router)        │      │  (Qwen@8000)    │
└─────────────┘      │                  │      └─────────────────┘
                     │  - Model Mapping │
                     │  - Load Balancing│      ┌─────────────────┐
                     │  - Health Checks │─────▶│  Backend 2      │
                     │  - Error Handling│      │  (Test@8001)   │
                     └──────────────────┘      └─────────────────┘
```

#### Installation Notes

```bash
# Add repository
wget -O - https://openresty.org/package/pubkey.gpg | sudo apt-key add -
echo "deb http://openresty.org/package/ubuntu $(lsb_release -sc) main" | \
    sudo tee /etc/apt/sources.list.d/openresty.list

# Install
sudo apt-get update
sudo apt-get install openresty
```

```bash
# Create directory
sudo mkdir -p /usr/local/openresty/nginx/conf/sites-available
sudo mkdir -p /usr/local/openresty/nginx/conf/sites-enabled

# Copy Config file
sudo cp <your config file> /usr/local/openresty/nginx/conf/sites-available/vllm

# Enable the site
sudo ln -s /usr/local/openresty/nginx/conf/sites-available/vllm \
           /usr/local/openresty/nginx/conf/sites-enabled/vllm
```

```bash
http {
    # ... Others ...

    # Lua settings
    lua_package_path "/usr/local/openresty/lualib/?.lua;;";
    lua_shared_dict model_cache 10m;

    # Include Site Configuration
    include /usr/local/openresty/nginx/conf/sites-enabled/*;
}
```

```bash
# test openresty config
sudo openresty -t

# Start
sudo systemctl start openresty

# Enable auto-start
sudo systemctl enable openresty

# reload openresty
sudo openresty -s reload
```

The model paths below are historical and may no longer match the registry; query `/v1/models` for the currently registered models.

```bash
# Chat with Qwen3-Coder
curl -X POST http://freeinference.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 50}'

# Chat with llama4-scout
curl -X POST http://freeinference.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "/models/meta-llama_Llama-4-Scout-17B-16E", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 50}'
```

### Nginx (v0, abandoned)

```bash
sudo vim /etc/nginx/sites-available/vllm
sudo nginx -t
sudo systemctl reload nginx

# to test the endpoint
curl https://freeinference.org/v1/models
```
