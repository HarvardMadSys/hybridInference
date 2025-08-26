# OpenRouter-Compatible API Server

A production-ready OpenRouter-compatible API server that aggregates multiple LLM providers with intelligent routing, load balancing, and comprehensive logging.

## Current Architecture

```
hybridInference/
├── serving/
│   ├── servers/
│   │   └── openrouter.py      # Main OpenRouter server
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
│   └── test_openrouter_models.py # openrouter providing models test
│
└── .env                    # Environment variables
```

## Features

### Core Capabilities
- **OpenRouter API Compatibility**: Full compliance with OpenRouter API specification
- **Multi-Provider Support**: VLLM, DeepSeek, Gemini, Llama, and custom providers
- **Intelligent Routing**: Weighted load balancing with automatic fallback
- **Usage Tracking**: Token counting for all requests (prompt_tokens, completion_tokens, total_tokens)
- **Database Logging**: Comprehensive request/response logging with SQLite/PostgreSQL
- **Streaming Support**: Server-Sent Events (SSE) for real-time responses

### Supported Models
- **Local Models** (via freeinference.org or custom VLLM):
  - Llama-4-Scout: `/models/meta-llama_Llama-4-Scout-17B-16E` (alias: `llama-4-scout`)
  - Qwen3-Coder: `/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8` (alias: `qwen3-coder`)
  
- **API Models**:
  - DeepSeek: `deepseek-chat`
  - Gemini: `gemini-2.0-flash-exp`, `gemini-2.5-flash`
  - Llama: Various models via Llama API (if configured)

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

# API Provider Keys
DEEPSEEK_API_KEY=your-deepseek-api-key
GEMINI_API_KEY=your-gemini-api-key
LLAMA_API_KEY=your-llama-api-key

# Database Configuration
USE_SQLITE_LOG=true  # Use SQLite for development
# DATABASE_URL=postgresql://user:pass@localhost/openrouter  # For production

# Server Configuration
PORT=8080
WORKERS=1  # Set to 4+ for production
```

### Model Configuration

The `config/openrouter_models.json` file contains detailed model metadata in OpenRouter's standard format, including:
- Model IDs and display names
- Context lengths and max output tokens
- Pricing information
- Supported features (JSON mode, function calling)
- Supported sampling parameters

This metadata is exposed via the `/v1/models` endpoint for client compatibility.

## Quick Start

### Start Server
```bash
# From project root
source .venv/bin/activate
python -m serving.servers.openrouter

# Custom port
PORT=8888 python -m serving.servers.openrouter

# Production mode with workers
python -m serving.servers.openrouter --workers 4
```

### Test Installation
```bash
# Check health
curl http://localhost:8080/health

# List available models
curl http://localhost:8080/v1/models | jq

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
| `/v1/models` | GET | List available models |
| `/v1/chat/completions` | POST | Chat completion (OpenRouter/OpenAI compatible) |
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
# Using full model path
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "/models/Qwen_Qwen3-Coder-480B-A35B-Instruct-FP8",
    "messages": [{"role": "user", "content": "Write a Python hello world"}],
    "max_tokens": 100
  }'

# Using alias
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-coder",
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

### Run Integration Tests
```bash
# Start server first
python -m serving.servers.openrouter &

# Run tests
pytest test/test_openrouter_models.py -v

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
python -m serving.servers.openrouter
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
```
