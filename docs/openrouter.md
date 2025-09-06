# OpenRouter-Compatible API Server

A production-ready OpenRouter-compatible API server that aggregates multiple LLM providers with intelligent routing, load balancing, and comprehensive logging.

## Current Architecture

```
hybridInference/
├── serving/
│   ├── servers/
│   │   └── app.py             # OpenRouter-compatible FastAPI app entry
│   ├── adapters/              # Provider adapters
│   │   ├── __init__.py       # Adapter exports
│   │   ├── base.py           # Base adapter interface
│   │   ├── vllm.py          # VLLM/Local models adapter
│   │   ├── deepseek.py      # DeepSeek API adapter
│   │   ├── gemini.py        # Google Gemini adapter
│   │   └── llama.py         # Llama API adapter
│   └── base.py              # Base classes for LLM operations
│
├── database/                # Database persistence layer
│   ├── __init__.py
│   ├── database.py         # PostgreSQL implementation
│   └── database_sqlite.py  # SQLite implementation
│
├── data/                   # Runtime data (gitignored)
│   └── db/
│       └── openrouter_logs.db
│
├── utils/                  # Utility tools
│   └── view_logs.py       # Database log viewer
│
├── test/                   # Test suite
│   ├── api/               # API-specific tests
│   └── servers/           # Server behavior tests
│
└── .env                    # Environment variables
```

## Features

### Core Capabilities
- **OpenRouter API Compatibility**: Full compliance with OpenRouter API specification
- **Multi-Provider Support**: VLLM, DeepSeek, Gemini, Llama, and custom providers
- **OFFLOAD Mode**: Set `OFFLOAD=1` to fully offload to provider APIs (skip local VLLM)
- **Intelligent Routing**: Weighted load balancing with automatic fallback
- **Usage Tracking**: Token counting for all requests (prompt_tokens, completion_tokens, total_tokens)
- **Database Logging**: Comprehensive request/response logging with SQLite/PostgreSQL
- **Streaming Support**: Server-Sent Events (SSE) for real-time responses

### Supported Models
- **Local Models** (via freeinference.org or custom VLLM):
  - Llama-4-Scout: `llama-4-scout` (provider_model_id: `/models/meta-llama_Llama-4-Scout-17B-16E`)
  - Qwen3-Coder: `qwen3-coder` (provider_model_id: `/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8`)

- **API Models**:
  - DeepSeek: `deepseek-chat`
  - Gemini: `gemini-2.5-flash`
  - Llama API: `llama-api` (when configured)

## Installation

### Using uv (Recommended)
```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Create virtual environment
uv venv .venv
source .venv/bin/activate

# Install dependencies
uv pip install fastapi uvicorn httpx python-dotenv aiosqlite asyncpg aiohttp uvloop
```

### Using conda
```bash
conda create -n hybrid_inference python=3.11
conda activate hybrid_inference
pip install fastapi uvicorn httpx python-dotenv aiosqlite asyncpg aiohttp uvloop
```

## Configuration

Create `.env` file in project root:

```bash
# Local VLLM Models (freeinference.org or your deployment)
LOCAL_BASE_URL=http://freeinference.org/v1

# OFFLOAD Mode (skip local VLLM, use only provider APIs)
OFFLOAD=0  # set to 1 to enable full offload

# API Provider Keys
DEEPSEEK_API_KEY=your-deepseek-api-key
GEMINI_API_KEY=your-gemini-api-key
LLAMA_API_KEY=your-llama-api-key
LLAMA_BASE_URL=https://your-llama-api-base/v1

# Database Configuration
USE_SQLITE_LOG=true  # Use SQLite for development
# DATABASE_URL=postgresql://user:pass@localhost/openrouter  # For production

# Server Configuration
PORT=8080
WORKERS=1  # Set to 4+ for production
```

### Model Metadata

The server exposes model metadata via the models endpoint, including:
- Model IDs and display names
- Context lengths (`context_length`) and max output tokens (`max_output_length`)
- Pricing (string USD fields: `prompt`, `completion`, `image`, `request`, `input_cache_reads`, `input_cache_writes`)
- Supported features (`tools`, `json_mode`, `structured_outputs`)
- Supported sampling parameters (`temperature`, `top_p`, `top_k`, `stop`, etc.)
- Modalities (`input_modalities`, `output_modalities`) and `quantization`

Clients should read this from the HTTP endpoint rather than a static file.

## Quick Start

### Start Server
```bash
# From project root
source .venv/bin/activate
python -m serving.servers.app

# Custom port
PORT=8888 python -m serving.servers.app

# Production mode with workers
python -m serving.servers.app --workers 4
```

### Start in OFFLOAD-only Mode (Meta Llama API, DeepSeek, Gemini)
```bash
export OFFLOAD=1
export LLAMA_BASE_URL=https://your-llama-api-base/v1
export LLAMA_API_KEY=your-llama-api-key
export DEEPSEEK_API_KEY=your-deepseek-api-key
export GEMINI_API_KEY=your-gemini-api-key
python -m serving.servers.openrouter
python -m serving.servers.app
```

### Test Installation
```bash
# Check health
curl http://localhost:8080/health

# List available models
curl http://localhost:8080/models | jq

# Test chat completion
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-4-scout",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 50
  }'
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API information and version |
| `/health` | GET | Health status (routes_configured, database_connected) |
| `/models` | GET | List available models (OpenRouter schema; also `/v1/models`, `/openrouter/models`) |
| `/v1/chat/completions` | POST | Chat completion (OpenRouter/OpenAI compatible) |
| `/completion` | POST | Single-shot completion (alias for chat completions) |
| `/routing` | GET | Show routing configuration |
| `/stats` | GET | Usage statistics with filters |

## Usage Examples

### Basic Chat Completion
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "What is 2+2?"}
    ],
    "max_tokens": 100,
    "temperature": 0.7
  }'
```

### Local Model (freeinference.org)
```bash
# Using public id (recommended)
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-coder",
    "messages": [{"role": "user", "content": "Write a Python hello world"}],
    "max_tokens": 100
  }'

# Using provider_model_id (still supported as alias)
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8",
    "messages": [{"role": "user", "content": "Write a Python hello world"}],
    "max_tokens": 100
  }'
```

### Streaming Response
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-4-scout",
    "messages": [{"role": "user", "content": "Tell me a story"}],
    "stream": true,
    "max_tokens": 200
  }'
```

## Response Format

Standard OpenRouter/OpenAI format:

```json
{
  "id": "chatcmpl-1756123456789",
  "object": "chat.completion",
  "created": 1756123456,
  "model": "llama-4-scout",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The answer is 4."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 15,
    "completion_tokens": 10,
    "total_tokens": 25
  }
}
```

## Models Endpoint Response

Example item (schema similar to OpenRouter provider requirements):

```json
{
  "id": "llama-4-scout",
  "name": "Llama 4 Scout 17B",
  "object": "model",
  "created": 1756123456,
  "owned_by": "vllm",
  "input_modalities": ["text"],
  "output_modalities": ["text"],
  "quantization": "bf16",
  "context_length": 262144,
  "max_output_length": 16384,
  "pricing": {
    "prompt": "0",
    "completion": "0",
    "image": "0",
    "request": "0",
    "input_cache_reads": "0",
    "input_cache_writes": "0"
  },
  "supported_sampling_parameters": ["temperature", "top_p", "top_k", "stop", "max_tokens"],
  "supported_features": ["tools", "json_mode", "structured_outputs"],
  "openrouter": {"slug": "llama-4-scout"}
}
```

## Database

### Schema
```sql
CREATE TABLE api_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    model_id TEXT NOT NULL,
    provider TEXT,
    prompt TEXT,
    response TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    latency_ms INTEGER,
    status_code INTEGER,
    error TEXT,
    params TEXT,
    metadata TEXT
);
```

### View Logs
```bash
# Use the log viewer utility
python utils/view_logs.py

# Direct database query
sqlite3 data/db/openrouter_logs.db "
  SELECT model_id, COUNT(*) as requests,
         SUM(total_tokens) as tokens,
         AVG(latency_ms) as avg_latency
  FROM api_logs
  WHERE timestamp > datetime('now', '-1 day')
  GROUP BY model_id;"
```

## Testing

### Run Tests
```bash
# Unit and server tests (no external calls)
pytest -m "not external" -q

# Or run a subset
pytest test/servers -q
```

## Troubleshooting

### Port Already in Use
```bash
# Find and kill process
lsof -ti :8080 | xargs kill -9
```

### Module Import Errors
```bash
# Ensure you're in project root
cd /root/hybridInference

# Run as module
python -m serving.servers.app
```

### Database Not Found
```bash
# Create data directory if missing
mkdir -p data/db

# Check database path
ls -la data/db/openrouter_logs.db
```

### Environment Variables Not Loading
```bash
# Check .env file exists in project root
ls -la .env

# Verify environment variables
python -c "import os; print(os.getenv('LOCAL_BASE_URL'))"
