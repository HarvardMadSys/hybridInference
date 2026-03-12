# Installation

Detailed installation instructions for HybridInference.

## Production (Docker)

The recommended way to run HybridInference in production. Requires Docker Engine 24+
and Docker Compose v2+.

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

cp .env.example .env
# Edit .env — fill in DB_PASSWORD, JWT_SECRET_KEY, API_KEY_SECRET at minimum

make up          # Start all services
make ps          # Verify everything is healthy
```

See [Deployment](deployment.md) for full production setup including Nginx and monitoring.

## Development Setup

### System Requirements

- Python 3.10 or higher
- Node.js 22+ (for frontend)
- GPU support (recommended for local inference)
- Linux or macOS (Windows via WSL2)

### Using uv (Recommended)

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

# Set up Python environment and pre-commit hooks
make setup-dev

# Or manually:
uv venv -p 3.10
source .venv/bin/activate
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Run the backend locally
uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080

# In another terminal — run the frontend
cd frontend
npm install
npm run dev
```

### Using conda

```bash
conda create -n hybrid_inference python=3.10 -y
conda activate hybrid_inference
pip install -e .
```

## Configuration

### Environment Variables

Copy the example environment file and fill in the values:

```bash
cp .env.example .env
```

Required for production:

- **Database**: `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- **Auth**: `JWT_SECRET_KEY`, `API_KEY_SECRET`

Optional (enable providers as needed):

- **LLM APIs**: `LLAMA_API_KEY`, `ZAI_API_KEY`, `CHUTES_API_KEY`, etc.

> **Note**: When running locally without Docker, the backend connects to GPU endpoints
> via `localhost`. In Docker, these are rewritten to `host.docker.internal` in
> `config/models.yaml`. See the comment at the top of that file.

## Verification

```bash
make test          # Run unit/integration tests
make lint          # Run linters
make check         # Run all checks
```

## Troubleshooting

- **Import errors**: Ensure you've activated the virtual environment
- **Database connection**: For local dev, start just PostgreSQL: `docker compose -f infrastructure/docker/docker-compose.yml --env-file .env up -d postgres`
- **GPU issues**: Check CUDA installation and driver compatibility
