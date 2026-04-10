#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"

COMPOSE="docker compose -f $REPO_ROOT/infrastructure/docker/docker-compose.staging.yml --env-file $REPO_ROOT/.env"

echo "==> Pulling infrastructure images"
$COMPOSE pull

echo "==> Building staging application images"
$COMPOSE build backend frontend

echo "==> Starting staging containers"
$COMPOSE up -d

echo "==> Container status"
$COMPOSE ps
