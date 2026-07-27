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
uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080

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


> **Note**: When running locally without Docker, the backend connects to GPU endpoints
> via `localhost`. In Docker, these are rewritten to `host.docker.internal` in
> `config/models.yaml`. See the comment at the top of that file.

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
  `docs/developer/` and is published to <https://internaldoc.freeinference.org/>.
- **User-facing documentation** (API quickstart, models, IDE integrations)
  lives at `distributions/freeinference/content/docs/docs/source/` and is published to
  <https://doc.freeinference.org/>.

Both sites are deployed automatically by Cloudflare Pages on push to `main`.
To update either site, edit the relevant Markdown/reStructuredText files and
open a pull request against this repository. Cloudflare Pages builds both
sites on each push and surfaces Sphinx errors as failed deployments.

## Troubleshooting

- **Import errors**: Ensure you've activated the virtual environment
- **Database connection**: For local dev, start just PostgreSQL: `docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres`
- **GPU issues**: Check CUDA installation and driver compatibility
