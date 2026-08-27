# Installation

Detailed installation instructions for HybridInference.

To see a gateway answer a request before configuring anything, then extend the
same local project into the Web/Admin Console with accounts and API keys,
follow the [Router Tutorial](router-tutorial.md). It needs no provider key or
host `.env`. This page covers configuring a production deployment.

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

- Python 3.10-3.13 (3.12 recommended)
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
uv venv -p 3.12
source .venv/bin/activate
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Run the backend locally
PYTHONPATH=apps/backend uv run uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080

# In another terminal — run the frontend
cd apps/frontend
npm install
npm run dev
```

### Using conda

```bash
conda create -n hybrid_inference python=3.12 -y
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


> **Note**: A backend running directly on the host can reach a host inference
> server through `localhost`. A backend in Docker must use a Docker-reachable
> address such as `host.docker.internal`, written explicitly in the active
> model registry; HybridInference does not rewrite provider URLs.

## Verification

```bash
make test          # Run unit/integration tests
make lint          # Run linters
make format        # Auto-format code
make check         # Run all checks
```

## Documentation Structure

This repository hosts both documentation sites used by the project:

- **Developer documentation** (deployment, architecture, internals) lives at
  `docs/developer/` and is published by whichever distribution hosts it —
  often on an internal network, so read the sources here if you cannot reach
  one.
- **User-facing documentation** (API quickstart, models, IDE integrations)
  lives in the active distribution's overlay, under
  `<overlay>/content/docs/docs/source/`, and each deployment publishes its own.

Both sites are deployed automatically by Cloudflare Pages on push to `main`.
To update either site, edit the relevant Markdown/reStructuredText files and
open a pull request against this repository. Cloudflare Pages builds both
sites on each push and surfaces Sphinx errors as failed deployments.

## Troubleshooting

- **Import errors**: Ensure you've activated the virtual environment
- **Database connection**: For local dev, start just PostgreSQL: `docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres`
- **GPU issues**: Check CUDA installation and driver compatibility
