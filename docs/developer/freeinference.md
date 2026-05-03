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
| **Cloudflare** | CDN, DDoS protection, edge SSL termination. SSL/TLS mode set to **Full (strict)** so Cloudflare verifies the origin certificate. `CF-Connecting-IP` header carries the real client IP. |
| **Nginx** | TLS termination (Let's Encrypt cert), path-based routing (see below), per-location body size limits (`/v1/` is bumped to 50 MB to accommodate large completion payloads and Qdrant vector upserts via the `/v1/qdrant` proxy; everything else uses the Nginx 1 MB default), WebSocket upgrade. |
| **FastAPI** | API logic — request authentication, model routing, backpressure, Qdrant proxy, and observability. Listens on `127.0.0.1:8080`. |

Nginx path routing:

- `/v1/`, `/auth/`, `/user/`, `/admin/`, `/internal/playground/` → FastAPI
- `/pgadmin/` → pgAdmin — gated by `auth_request` against FastAPI's `/internal/verify-admin` endpoint, so only admins reach it
- everything else → frontend

Docker Compose manages all services (backend, frontend, PostgreSQL, Alertmanager,
alert-logger, plus pgAdmin behind the `admin` profile) with automatic restarts
via `restart: unless-stopped`.

### Deployment

All services are defined in `deploy/docker/docker-compose.yml`. From the project root:

```bash
cp .env.example .env   # Configure secrets
make up                # Start all services
make ps                # Verify health
```

Nginx runs on the host (not containerized) for SSL termination. See
[Deployment](deployment.md) for the full guide.

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
