#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"

if [ -f "$HOME/.local/bin/env" ]; then
  source "$HOME/.local/bin/env"
fi

COMPOSE="docker compose -f $REPO_ROOT/infrastructure/docker/docker-compose.staging.yml --env-file $REPO_ROOT/.env"

echo "==> Installing Python dependencies"
uv sync

echo "==> Pulling infrastructure images"
$COMPOSE pull

echo "==> Starting infrastructure containers"
$COMPOSE up -d

echo "==> Container status"
$COMPOSE ps

echo "==> Restarting FastAPI service"
sudo systemctl restart hybrid_inference.staging
sudo systemctl status hybrid_inference.staging --no-pager
