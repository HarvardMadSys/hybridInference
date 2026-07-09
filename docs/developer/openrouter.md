# OpenRouter-Compatible API Gateway

A FastAPI-based gateway that serves OpenRouter-compatible traffic, fans out to local and remote LLM adapters, and exposes observability interfaces for operations. In production the application runs as a Docker container on port 8080 behind Nginx; for local development it can run on any port via `uvicorn` directly.

## Architecture

```
hybridInference/
├── docs/                       # Deployment and integration guides
├── serving/
│   ├── servers/
│   │   ├── app.py              # FastAPI entry point (exposes /v1/*)
│   │   ├── bootstrap.py        # Service bootstrap: models, routing, DB
│   │   └── routers/            # API routers (health, models, completions, admin, ...)
│   ├── adapters/               # Provider adapters: openai_compat.py (vllm/sglang/
│   │                           #   ollama/chutes/featherless/deepseek/zai/minimax),
│   │                           #   openrouter.py, gemini.py, anthropic.py, claude.py,
│   │                           #   plus shared profiles.py
│   ├── storage/                # PostgreSQL-backed operational and log stores
│   ├── observability/          # Structured request logging
│   └── utils/                  # Logging, configuration helpers
├── routing/                    # Routing manager and execution strategies
├── config/
│   ├── models.yaml             # Canonical model definitions + adapters
│   └── routing.yaml (optional) # Weighted routing configuration
└── deploy/docker/      # Dockerfiles and docker-compose.yml
```

### Key Components
- **FastAPI app (`serving.servers.app:create_app`)**: Hosts OpenRouter-compatible endpoints plus admin and metrics routes.
- **Bootstrap (`serving.servers.bootstrap`)**: Loads environment, registers models, applies routing weights, and wires database logging.
- **Adapters (`serving.adapters.*`)**: Translate requests to providers — `OpenAICompatAdapter` (vLLM, SGLang, Ollama, DeepSeek, Zhipu, MiniMax, Chutes, Featherless), `OpenRouterAdapter`, `GeminiAdapter`, `AnthropicAdapter`, `ClaudeAdapter` (Vertex).
- **Routing (`routing.*`)**: Supports fixed-ratio and future strategies for splitting traffic across adapters.
- **Observability (`serving.observability`)**: Structured request logging.

## Features

- **OpenRouter API compatibility**: Implements `/v1/chat/completions`, `/v1/models`, and related schemas.
- **Hybrid routing**: Combine local VLLM workers with hosted APIs.
- **Resilient adapters**: Automatic retry/fallback when a provider returns errors.
- **Usage accounting**: Prompt/completion token tracking and persisted request logs.
- **Streaming responses**: Server-Sent Events (SSE) for incremental output.
- **Observability hooks**: Structured request logs in PostgreSQL.

## Development Setup

### Prerequisites
- Python 3.10-3.13 (3.12 recommended)
- [uv](https://github.com/astral-sh/uv) (recommended) or conda

### Create Environment
```bash
# Clone and bootstrap
git clone <repository-url>
cd hybridInference
uv venv -p 3.12
source .venv/bin/activate
uv sync
```

### Local Environment Variables
Create `.env` from the template:
```bash
cp .env.example .env
```
Populate it with provider credentials and runtime configuration:
```bash
LOCAL_DEPLOYMENT_URL=http://host.docker.internal:8001/v1
DEEPSEEK_API_KEY=your-deepseek-api-key
GEMINI_API_KEY=your-gemini-api-key
DB_HOST=localhost
DB_NAME=freeinference_db
DB_USER=postgres
DB_PASSWORD=postgres
JWT_SECRET_KEY=replace-me
API_KEY_SECRET=replace-me
```

### Run Locally
```bash
# Development server with reload on port 8080
uvicorn serving.servers.app:app --reload --host 0.0.0.0 --port 8080

# Alternate: respect PORT env var
PORT=9000 uvicorn serving.servers.app:app --host 0.0.0.0 --port $PORT
```

When the app starts it will:
1. Load environment variables (dotenv).
2. Register models from `config/models.yaml`.
3. Apply routing overrides from `config/routing.yaml` if present.
4. Initialize the PostgreSQL database logger and operational store.

### Quick Checks
```bash
# Health
curl http://localhost:8080/health

# Models (OpenRouter schema)
curl http://localhost:8080/v1/models | jq

# Chat completion
env \
  http_proxy= \
  curl -X POST http://localhost:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
          "messages": [{"role": "user", "content": "Ping"}],
          "max_tokens": 64
        }'
```

## Production Deployment

All services run via Docker Compose. Nginx on the host terminates TLS;
Cloudflare provides CDN and DDoS protection in front of Nginx.

```bash
make up      # Start all services
make ps      # Verify health
```

Runtime operations:
- Restart: `make restart` or `make restart s=backend`
- Logs: `make logs` or `make logs s=backend`

See [Deployment](deployment.md) for the full guide.
- Health: `curl https://freeinference.org/health`

## API Surface

| Method | Path | Auth | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/v1/models` | API key | Enumerate available models with OpenRouter metadata |
| POST | `/v1/chat/completions` | API key | OpenRouter/OpenAI-compatible chat completion |
| GET | `/health` | Public | Liveness and dependency checks |
| GET | `/routing` | Public | Current routing weights for each model |
| GET | `/admin/routing` | Admin | Admin-authenticated alias of `/routing` |
| GET | `/admin/stats` | Admin | Aggregated usage statistics. The previous unauthenticated `/stats` alias has been removed; use this endpoint instead. |

### Example Requests
```bash
# Streaming response
env \
  http_proxy= \
  curl -N -X POST http://localhost:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
          "model": "glm-5.1",
          "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Describe the architecture."}
          ],
          "stream": true,
          "temperature": 0.7,
          "max_tokens": 256
        }'
```

## Logging and Metrics

Logs and operational state go to PostgreSQL. Connection parameters come from
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD`.

- **Metrics**: Prometheus instrumentation has been removed; structured logs in the configured database are the supported observability surface today.

Inspect logs (PostgreSQL backend):
```bash
docker exec -it hybridinference-postgres psql -U $DB_USER -d $DB_NAME \
  -c "SELECT model_id, COUNT(*) FROM api_logs GROUP BY model_id;"
```

## Testing

```bash
# Fast unit/integration tests
pytest -m "not external" -q

# Focused server tests
pytest tests/servers/test_bootstrap.py -q
```

## Troubleshooting

- **Port already in use**: `sudo lsof -ti :80 | xargs sudo kill -9`
- **Missing models**: Verify `config/models.yaml` contains the expected entries and that `LOCAL_DEPLOYMENT_URL` is reachable.
- **No logs written**: Confirm PostgreSQL is reachable and the configured database credentials are correct.

## Related Docs

- [FreeInference Deployment](freeinference.md): Current production deployment architecture (Cloudflare + Nginx + FastAPI) and historical deployment iterations.
- [Routing](routing.md): Detailed routing manager configuration and strategy extension guide.
- [Adding Models](adding-models.md): How to add new models (YAML) and integrate new providers (adapter) in one place.
