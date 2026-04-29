#!/usr/bin/env bash
#
# Deploy the staging Docker Compose stack from the dev branch.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/srv/hybridInference}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3002}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${BACKEND_PORT}/health}"
FRONTEND_HEALTH_URL="${FRONTEND_HEALTH_URL:-http://127.0.0.1:${FRONTEND_PORT}/}"
TARGET_BRANCH="${TARGET_BRANCH:-dev}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
COMPOSE=(docker compose -f infrastructure/docker/docker-compose.yml --env-file .env)

log() {
  printf '[deploy-staging] %s\n' "$*"
}

dump_diagnostics() {
  local status=$?
  if [[ "$status" -ne 0 ]]; then
    log "Deployment failed with exit status ${status}; collecting diagnostics."
    "${COMPOSE[@]}" ps || true
    "${COMPOSE[@]}" logs --tail=100 backend frontend || true
  fi
  exit "$status"
}

trap dump_diagnostics EXIT

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  log "Missing ${APP_DIR}/.env; staging secrets must stay on the server."
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  log "Refusing to deploy because tracked local changes exist."
  git status --short --untracked-files=no
  exit 1
fi

log "Fetching origin/${TARGET_BRANCH}."
git fetch --prune origin "$TARGET_BRANCH"

if [[ -n "$DEPLOY_SHA" ]]; then
  target_sha="$(git rev-parse "${DEPLOY_SHA}^{commit}")"
  if ! git merge-base --is-ancestor "$target_sha" "origin/${TARGET_BRANCH}"; then
    log "Refusing to deploy ${target_sha}; it is not on origin/${TARGET_BRANCH}."
    exit 1
  fi
else
  target_sha="$(git rev-parse "origin/${TARGET_BRANCH}")"
fi

current_sha="$(git rev-parse HEAD)"
log "Deploying ${target_sha} (current ${current_sha})."

git reset --hard "$target_sha"
git submodule update --init --recursive

log "Syncing subscription credentials."
make sync-subscriptions

log "Rebuilding and restarting Docker Compose services."
make build

log "Current service state:"
"${COMPOSE[@]}" ps

log "Checking backend health at ${HEALTH_URL}."
curl -fsS --retry 30 --retry-delay 5 --retry-connrefused "$HEALTH_URL"
printf '\n'

log "Checking frontend health at ${FRONTEND_HEALTH_URL}."
curl -fsS --retry 30 --retry-delay 5 --retry-connrefused --output /dev/null \
  "$FRONTEND_HEALTH_URL"

log "Staging deployment completed."
